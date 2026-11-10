"""Recording: the level check, starting and stopping a recording, and the listen-in stream.

Part of the API of `vinylcheck.web`: `install` adds the routes to the app. What the areas share lives in the
`Station` (`vinylcheck/station.py`).
"""

from __future__ import annotations

import os
import queue
import time
from typing import TYPE_CHECKING

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse

from .. import capture, i18n, storage
from .common import (CHECK_KINDS, _clean_findings, KINDS, RPMS, SIDE_SOURCE_DISCOGS, SIDE_SOURCE_LABEL,
                     SIDE_SOURCE_MEASURED, SIDE_SOURCE_TRACKS, StartIn, storage_status)

if TYPE_CHECKING:
    from ..station import Station


def install(app: FastAPI, station: Station) -> None:
    cfg = station.cfg
    db = station.db
    worker = station.worker
    recorder = station.recorder
    aud = station.aud
    mover = station.mover

    def side_length_for(record_id: int | None, side: str) -> tuple[float | None, str]:
        """Expected length of a side in seconds and where it comes from (SIDE_SOURCE_*): measured on an earlier
        recording of the same side, else from the Discogs or hand-entered track list."""
        if not record_id:
            return None, ""
        v = db.measured_side_length(record_id, side)
        if v:
            return v, SIDE_SOURCE_MEASURED
        r = db.record(record_id) or {}
        sides = station.ensure_sides(r) if r else {}
        v = sides.get(str(side).strip().upper()[:1]) if sides else None
        if not v:
            return None, ""
        return float(v), SIDE_SOURCE_DISCOGS if r.get("discogs_release_id") else SIDE_SOURCE_TRACKS

    def pick_device(requested: str | None) -> str:
        """Capture device: the requested one (a turntable profile's), the one set under Audio interface or in the
        configuration, the test file, else the first suitable audio interface (AudioSettings.choose)."""
        if requested:
            return requested
        dev, _ = aud.choose()
        if not dev:
            # no device at all: say why if the backend itself is missing (arecord, sounddevice, PortAudio)
            problem = aud.backend_problem()
            raise HTTPException(409, problem or i18n.msg("api.no_capture_device"))
        return dev

    # ─── Capture ────────────────────────────────────────────────────────────
    def _start(path_for, meta: dict, device: str | None, max_s: float, on_done=None, endstop_rpm=None, **kw):
        """Start the recorder (recording or level check); a refusal of the recorder becomes 409."""
        dev = pick_device(device)
        path = path_for(dev) if path_for else None
        audio = aud.effective(dev)        # rate and format of the Audio interface settings, else the configuration
        try:
            return recorder.start(device=dev, rate=audio["rate"], channels=audio["channels"],
                                  fmt=audio["sample_format"], path=path, max_s=max_s, meta=meta,
                                  on_done=on_done, endstop_rpm=endstop_rpm, **kw)
        except RuntimeError as e:
            raise HTTPException(409, i18n.error_message(e)) from e

    @app.post("/api/monitor/start")
    def monitor_start(device: str | None = None):
        if not device and not cfg.fake_source:
            # The level check listens where the recording will: the active turntable profile's device, if connected
            prof = db.active_profile()
            if prof and prof.get("device") and prof["device"] in {d["device"] for d in station.capture_devices()}:
                device = prof["device"]
        return _start(None, {"mode": "monitor"}, device, max_s=900)

    @app.post("/api/recordings/start")
    def rec_start(s: StartIn):
        if s.kind not in KINDS:
            raise HTTPException(422, i18n.msg("api.kind_invalid", allowed=", ".join(KINDS)))
        if not any(abs(s.rpm - r) < 0.5 for r in RPMS):
            raise HTTPException(422, i18n.msg("api.rpm_invalid"))
        if s.kind == "alignment":
            if s.setup_id is None:
                raise HTTPException(422, i18n.msg("api.alignment_needs_setup"))
            if not db.setup(s.setup_id):
                raise HTTPException(404, i18n.msg("api.setup_not_found"))
            s.record_id = None
        else:
            s.setup_id = None
        if s.kind not in ("calibration", "alignment") and s.record_id is None:
            raise HTTPException(422, i18n.msg("api.record_required"))
        if s.record_id is not None and not db.record(s.record_id):
            raise HTTPException(404, i18n.msg("api.record_not_found"))
        if mover.active:
            raise HTTPException(409, i18n.msg("storage.busy_recording"))
        st = storage_status(cfg)
        if not st["ok"]:
            raise HTTPException(409, st["reason"])
        if recorder.active:
            raise HTTPException(409, i18n.msg("api.recorder_busy"))
        if s.setup_id is not None and s.profile_id is None:
            s.profile_id = (db.setup(s.setup_id) or {}).get("profile_id")
        prof = db.profile(s.profile_id) if s.profile_id else db.active_profile()
        if s.profile_id and not prof:
            raise HTTPException(404, i18n.msg("api.profile_not_found"))
        if prof and prof["device"] and not s.device and not cfg.fake_source:
            present = {d["device"] for d in station.capture_devices()}
            if prof["device"] not in present:
                raise HTTPException(409, i18n.msg("api.profile_not_connected", name=prof["name"],
                                                  device=prof["device"]))
            s.device = prof["device"]
        rpm = min(RPMS, key=lambda r: abs(s.rpm - r))
        s.device = pick_device(s.device)
        audio = aud.effective(s.device)   # the format the recording is made in (Audio interface, else configuration)
        rec_id = db.add_recording(record_id=s.record_id, setup_id=s.setup_id, profile_id=prof["id"] if prof else None,
                                  side=s.side.strip()[:8] or "A",
                                  kind=s.kind, rpm=rpm, cleaning_method=s.cleaning_method, notes=s.notes,
                                  copy_label=" ".join(s.copy_label.split())[:40],
                                  samplerate=audio["rate"], channels=audio["channels"], format=audio["sample_format"],
                                  status="recording")
        if "findings" in s.model_fields_set and s.record_id is not None:      # findings belong to the copy and side
            db.set_copy_findings(s.record_id, s.copy_label, s.side, _clean_findings(s.findings))
        path = cfg.rec_dir / storage.recording_name(db.recording(rec_id), db.record(s.record_id) if s.record_id else None,
                                                    prof if not s.record_id else None)

        def done(state: dict):
            fields = {"duration_s": state.get("duration_s"), "clip_samples": state.get("clip_samples", 0),
                      "xruns": state.get("xruns", 0)}
            if state.get("error"):
                db.update_recording(rec_id, status="error", error=i18n.dumps(state["error"]), **fields)
                db.add_problem("recording", rec_id, state["error"])
            elif (state.get("duration_s") or 0) < 3:
                db.update_recording(rec_id, status="error", error=i18n.dumps(i18n.msg("api.recording_too_short")),
                                    **fields)
                db.add_problem("recording", rec_id, i18n.msg("api.recording_too_short"))
            else:
                db.update_recording(rec_id, status="recorded", **fields)
                worker.analyze(rec_id)

        try:
            max_s = cfg.max_record_minutes * 60
            if s.max_s:
                max_s = min(max_s, max(5.0, float(s.max_s)))
            side_len, side_src = (None, "") if s.kind in ("calibration", "alignment") else side_length_for(s.record_id, s.side)
            thr = None
            if s.kind != "calibration":
                ref = prof.get("ref_dbfs") if prof and prof.get("accepted_at") else None
                thr = (ref - 28.0) if ref is not None else -40.0           # music starts: 1 s above this threshold
            # Record checks also stop once the stylus has been lifted after the music (the analysis' boundary:
            # chain noise + 6 dB); only with a measured chain noise, and with the run-out stop switched on
            chain = worker.profile_trim({"profile_id": prof["id"] if prof else None})["chain_db"]
            lifted = (chain + 6.0 if chain is not None and cfg.autostop_end_groove and s.kind in CHECK_KINDS
                      else None)

            def on_event(name: str, st: dict):
                if name == "side_end":
                    station.push_event("side_end", i18n.msg("api.event_side_end"), rec_id)
                elif name == "end_groove":
                    station.push_event("end_groove", i18n.msg("api.event_end_groove"), rec_id)
                elif name == "stylus_up":
                    station.push_event("stylus_up", i18n.msg("api.event_stylus_up"), rec_id)

            res = _start(lambda dev: path, {"recording_id": rec_id, "kind": s.kind, "setup_id": s.setup_id,
                                            "record_id": s.record_id,
                                            "profile_id": prof["id"] if prof else None,
                                            "side_length_source": side_src,
                                            "side_length_source_label": SIDE_SOURCE_LABEL.get(side_src)}, s.device,
                         max_s=max_s, on_done=done,
                         endstop_rpm=rpm if cfg.autostop_end_groove and s.kind != "calibration" else None,
                         music_thr_dbfs=thr, side_length_s=side_len, on_event=on_event, stylus_up_dbfs=lifted)
        except HTTPException:
            db.delete_recording(rec_id)
            raise
        db.update_recording(rec_id, path=str(path), device=res["device"])
        return {"recording_id": rec_id, "status": res}

    @app.post("/api/recordings/stop")
    @app.post("/api/monitor/stop")
    def rec_stop():
        return recorder.stop()

    @app.get("/api/live/pcm")
    def live_pcm(start: float | None = None):
        """Listen in: the running audio as raw PCM (16 bit, little-endian, channels interleaved) until the capture ends.
        The browser plays it through the Web Audio API (app.js, listen-in).

        `start` = second of the recording to rewind to. The audio then comes from the WAV file being written, first as
        fast as possible up to the current position, then live."""
        if not recorder.active:
            raise HTTPException(409, i18n.msg("api.nothing_running"))
        st = recorder.status()
        headers = {"X-Rate": str(st["rate"]), "X-Channels": str(st["channels"]),
                   "X-Format": "S16_LE", "Cache-Control": "no-store"}
        if start is not None:
            path = st.get("path")
            if not path:
                raise HTTPException(409, i18n.msg("api.rewind_recording_only"))
            fmt, ch, rate = st["format"], st["channels"], st["rate"]
            bpf = capture.FORMATS[fmt][0] * ch

            def same_recording():
                s = recorder.status()
                return s.get("active") and s.get("path") == path

            def gen_file():
                off = capture.wav_data_offset(path)
                while off is None and same_recording():
                    time.sleep(0.1)
                    off = capture.wav_data_offset(path)
                if off is None:
                    return
                size = os.path.getsize(path)
                pos = min(off + max(0, int(start * rate)) * bpf, off + (size - off) // bpf * bpf)
                while True:
                    avail = (os.path.getsize(path) - pos) // bpf * bpf
                    if avail <= 0:
                        if not same_recording():          # the file is complete and everything has been sent
                            break
                        time.sleep(0.1)
                        continue
                    with open(path, "rb") as f:
                        f.seek(pos)
                        raw = f.read(min(avail, rate // 2 * bpf))     # at most 0.5 s per block
                    pos += len(raw)
                    yield capture.pcm16(raw, fmt, ch)

            return StreamingResponse(gen_file(), media_type="application/octet-stream", headers=headers)
        q = recorder.tap()

        def gen():
            try:
                while True:
                    try:
                        item = q.get(timeout=5.0)
                    except queue.Empty:
                        if not recorder.active:
                            break
                        continue
                    if item is None:
                        break
                    yield item
            finally:
                recorder.untap(q)

        return StreamingResponse(gen(), media_type="application/octet-stream", headers=headers)
