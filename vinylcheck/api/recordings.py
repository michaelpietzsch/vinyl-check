"""Recordings and what was found in them: overview, events and their verdicts, groove skips, the "what if" count,
audio excerpts, the spectrogram, and comparisons of two recordings.

Part of the API of `vinylcheck.web`: `install` adds the routes to the app. What the areas share lives in the
`Station` (`vinylcheck/station.py`).
"""

from __future__ import annotations

import io
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import soundfile as sf
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response, StreamingResponse
from starlette.background import BackgroundTask

from .. import analysis, grading, i18n, learning, spectro, skips
from ..db import copy_key
from ..jobs import trim_audio
from .common import (_check_sensitivity, _clean_findings, CompareIn, KINDS, LabelIn, log, RecordingPatch,
                     SkipLabelIn, TRIM_MARGIN_S, TRIM_MIN_S, WhatIfIn)

if TYPE_CHECKING:
    from ..station import Station


def install(app: FastAPI, station: Station) -> None:
    cfg = station.cfg
    db = station.db
    worker = station.worker
    recorder = station.recorder
    mover = station.mover
    ref_cache = station.ref_cache

    # ─── Recordings ─────────────────────────────────────────────────────────
    @app.get("/api/recordings")
    def list_recordings(record_id: int | None = None):
        return db.recordings(record_id)

    @app.get("/api/recordings/{rec_id}")
    def get_recording(rec_id: int):
        return station.get_recording(rec_id)

    def _audio_gone_msg(r: dict) -> dict:
        """Why the audio of a recording is not available (message)."""
        if r.get("audio_deleted_at"):
            return i18n.msg("api.audio_retention_deleted", n=cfg.retention_days)
        return i18n.msg("api.audio_missing")

    @app.patch("/api/recordings/{rec_id}")
    def patch_recording(rec_id: int, p: RecordingPatch):
        f = {k: v for k, v in p.model_dump(exclude_unset=True).items() if v is not None or k == "sensitivity"}
        if "kind" in f and f["kind"] not in KINDS:
            raise HTTPException(422, i18n.msg("api.kind_invalid", allowed=", ".join(KINDS)))
        _check_sensitivity(f.get("sensitivity"))
        rec = db.recording(rec_id)
        if not rec:
            raise HTTPException(404, i18n.msg("api.recording_not_found"))
        side_changed = False
        if "side" in f:                                            # as at the start: trimmed, at most 8 characters
            f["side"] = f["side"].strip()[:8]
            if not f["side"]:
                raise HTTPException(422, i18n.msg("api.side_empty"))
            side_changed = f["side"].upper() != str(rec.get("side") or "").strip().upper()
        findings = f.pop("findings", None)
        if findings is not None:                                   # findings: for the copy and side of this recording
            if rec.get("record_id") is None:
                raise HTTPException(409, i18n.msg("api.findings_need_record"))
            db.set_copy_findings(rec["record_id"], f.get("copy_label", rec.get("copy_label")), rec["side"], _clean_findings(findings))
        if f:
            db.update_recording(rec_id, **f)
        r = station.get_recording(rec_id)
        if (("sensitivity" in f or side_changed) and r["audio_available"] and r["kind"] != "calibration"
                and r["status"] in ("analyzed", "error")):
            worker.analyze(rec_id)          # new sensitivity or another side (Discogs tracks) → recompute
            r["reanalyzing"] = True
        return r

    @app.post("/api/recordings/{rec_id}/trim")
    def trim_recording(rec_id: int):
        """Remove the overhang: cut the recording shortly after the end of the groove (what follows is the chain's
        noise with the stylus up), store the new length and analyse it again. The file is checked against its
        checksum first; the new checksum is stored by that analysis."""
        r = station.get_recording(rec_id)
        if r["status"] == "recording" or (recorder.active and (recorder.state.get("meta") or {}).get("recording_id") == rec_id):
            raise HTTPException(409, i18n.msg("api.recording_in_progress", id=rec_id))
        if r["status"] in ("queued", "analyzing"):
            raise HTTPException(409, i18n.msg("api.trim_busy"))
        if mover.active:
            raise HTTPException(409, i18n.msg("storage.busy"))
        if not r["audio_available"]:
            raise HTTPException(409, _audio_gone_msg(r))
        over = r["overhang_s"]
        if over is None:
            raise HTTPException(409, i18n.msg("api.trim_nothing", s=TRIM_MIN_S))
        try:
            worker.verify_audio(db.recording(rec_id))
        except RuntimeError as e:
            raise HTTPException(409, i18n.error_message(e)) from e
        keep = r["analysis"]["metrics"]["groove_end_s"] + TRIM_MARGIN_S
        new_len = trim_audio(r["path"], keep)
        db.update_recording(rec_id, duration_s=new_len, sha256=None)
        log.info("Recording %s: overhang of %.1f s removed, %.1f s kept", rec_id, r["duration_s"] - new_len, new_len)
        worker.analyze(rec_id)
        return {"duration_s": new_len, "removed_s": round(r["duration_s"] - new_len, 1)}

    @app.post("/api/recordings/{rec_id}/analyze")
    def reanalyze(rec_id: int):
        r = station.get_recording(rec_id)
        if r["status"] == "recording":                # its analysis follows by itself once the side has ended
            raise HTTPException(409, i18n.msg("api.recording_in_progress", id=rec_id))
        if not r["audio_available"]:
            raise HTTPException(409, _audio_gone_msg(r))
        worker.analyze(rec_id)
        return {"queued": True}

    @app.delete("/api/recordings/{rec_id}")
    def delete_recording(rec_id: int):
        if recorder.active and recorder.state.get("meta", {}).get("recording_id") == rec_id:
            raise HTTPException(409, i18n.msg("api.recording_running"))
        station.remove_audio(db.delete_recording(rec_id))
        ref_cache["key"] = None
        return {"ok": True}

    @app.get("/api/recordings/{rec_id}/overview")
    def overview(rec_id: int):
        ov = db.overview(rec_id)
        if ov is None:
            raise HTTPException(404, i18n.msg("api.not_analyzed_short"))
        # [t, kind, region, strength, judgment, second-opinion score]: for deciding right in the chart
        ov["events"] = [[e["t"], e["kind"], e["region"], e["strength"], e.get("label"), e.get("gc")] for e in db.events(rec_id)]
        return ov

    @app.get("/api/recordings/{rec_id}/events")
    def events(rec_id: int, order: str = "strength", limit: int = 200):
        """The detections, each with `why` it does not count ("label", "rule", "group" or null), `grp` (the number
        of impulses of its audible group, on the representative) and `grp_of` (the representative's time, on the
        other members); `lp` is the learned model's estimate; `r_mm` the estimated groove radius (33⅓ only)."""
        evs = db.events(rec_id, limit=min(limit, 20000), order=order)
        if not evs:
            return evs
        all_evs = db.events(rec_id)                 # grouping, counting and the "density" feature need every event
        ea = analysis.event_arrays(all_evs)
        acfg = station.acfg_for(rec_id)
        span = db.one("SELECT json_extract(metrics, '$.music_start_s') AS ms, json_extract(metrics, '$.music_end_s') AS me, "
                      "json_extract(metrics, '$.groove_end_s') AS ge, json_extract(metrics, '$.groove_s') AS gs, "
                      "json_extract(metrics, '$.rpm') AS rpm FROM analyses WHERE recording_id = ?", (rec_id,)) or {}
        region = analysis.music_region(ea["region"], ea["t"], (span.get("ms"), span.get("me")))
        if span.get("ge") is not None and span.get("gs") is not None:        # place on the record, like a skip's
            radius = skips.radius_mm(ea["t"], (span["ge"] - span["gs"], span["ge"]), span.get("me"), span.get("rpm") or 100 / 3)
        else:
            radius = [None] * len(all_evs)
        _c, _r, why, _i = analysis.count_mask(ea["kind"], region, ea["label"], ea["rep"], ea["t"],
                                              ea["strength"], acfg, shk=ea["shock"])
        rep_of = analysis.group_impulses(ea["t"], region, ea["kind"], ea["strength"], acfg.click_group_ms)
        size = np.bincount(rep_of, minlength=len(all_evs))
        extra = {round(e["t"], 5): {"why": why[i], "grp": int(size[i]) if size[i] > 1 else None,
                                    "grp_of": None if rep_of[i] == i else all_evs[rep_of[i]]["t"],
                                    "region": str(region[i]),                # lead-in/run-out clicks: quiet, not music
                                    "r_mm": radius[i]}
                 for i, e in enumerate(all_evs)}
        model = station.learn_model()
        lp = {}
        if model:
            r = db.one("SELECT json_extract(a.metrics, '$.music_ref_dbfs') AS ref, r.profile_id FROM analyses a "
                       "JOIN recordings r ON r.id = a.recording_id WHERE a.recording_id = ?", (rec_id,)) or {}
            _, p = learning.estimate(all_evs, model, r.get("ref"), station.learn_zp(r))
            lp = {round(e["t"], 5): (None if p[i] != p[i] else round(float(p[i]), 3)) for i, e in enumerate(all_evs)}
        for e in evs:
            e.update(extra.get(round(e["t"], 5)) or {"why": None, "grp": None, "grp_of": None, "region": e["region"], "r_mm": None})
            if model:
                e["lp"] = lp.get(round(e["t"], 5))
        return evs

    @app.put("/api/recordings/{rec_id}/events/label")
    def label_event(rec_id: int, p: LabelIn):
        """Audit: the user's judgment of one event. "music" (a false alarm) drops out of count and grade at once,
        survives a recomputation and is available for training (GET /api/audit/export)."""
        if p.label is not None and p.label not in analysis.LABELS:
            raise HTTPException(422, i18n.msg("api.label_invalid", allowed=", ".join(analysis.LABELS)))
        r = db.recording(rec_id)
        if not r or not r.get("analysis"):
            raise HTTPException(404, i18n.msg("api.recording_not_analyzed"))
        hit = db.set_label(rec_id, p.t, p.label)
        if not hit:
            raise HTTPException(404, i18n.msg("api.no_event_at_time"))
        metrics, grading = station.recount(rec_id) or (r["analysis"]["metrics"], r["analysis"]["grading"])
        ref_cache["key"] = None
        return {"t": p.t, "label": p.label, "grade": grading.get("grade"), "score": grading.get("score"),
                "counts": metrics.get("counts"), "clicks_quiet_per_min": metrics.get("clicks_quiet_per_min"),
                "clicks_music_per_min": metrics.get("clicks_music_per_min"), "pops_per_min": metrics.get("pops_per_min")}

    @app.put("/api/recordings/{rec_id}/skips/label")
    def label_skip(rec_id: int, p: SkipLabelIn):
        """Audit of groove skips: "run_out" (run-out groove, no damage) or "no_skip" (rhythm, no skip). Recounts
        at once (the F rule included) and survives a recomputation."""
        if p.label is not None and p.label not in analysis.SKIP_LABELS:
            raise HTTPException(422, i18n.msg("api.label_invalid", allowed=", ".join(analysis.SKIP_LABELS)))
        r = db.recording(rec_id)
        if not r or not r.get("analysis"):
            raise HTTPException(404, i18n.msg("api.recording_not_analyzed"))
        if not any(abs(e["t"] - p.t) <= 2.0 for e in r["analysis"]["metrics"].get("skips") or []):
            raise HTTPException(404, i18n.msg("api.no_skip_at_time"))
        db.set_skip_label(rec_id, p.t, p.label)
        metrics, grading = station.recount(rec_id) or (r["analysis"]["metrics"], r["analysis"]["grading"])
        ref_cache["key"] = None
        return {"t": p.t, "label": p.label, "grade": grading.get("grade"), "skips_back": metrics.get("skips_back"),
                "skips_weak": metrics.get("skips_weak")}

    @app.post("/api/recordings/{rec_id}/whatif")
    def what_if(rec_id: int, p: WhatIfIn):
        """What-if: side and record grade if the given events did not count. Stores nothing: no judgments, no
        training; the stored grade and counts stay as they are."""
        if len(p.ignore_events) > 20000 or len(p.ignore_skips) > 500:
            raise HTTPException(422, i18n.msg("api.too_many_items"))
        r = db.recording(rec_id)
        if not r or not r.get("analysis"):
            raise HTTPException(404, i18n.msg("api.recording_not_analyzed"))
        a = r["analysis"]
        m, g, hit = analysis.what_if(a["metrics"], db.events(rec_id), db.skip_labels(rec_id), p.ignore_events,
                                     p.ignore_skips, cfg.grading, learned=station.learn_model(),
                                     learn_grade=cfg.learn_grade, zero_point=station.learn_zp(r),
                                     acfg=station.acfg_for(rec_id))
        base, bm = a.get("grading") or {}, a["metrics"]
        keys = ("clicks_music_per_min", "clicks_quiet_per_min", "ticks_quiet_per_min", "pops_per_min", "skips_back")
        out = {"grade": g.get("grade"), "score": g.get("score"), "explanation": g.get("explanation"), "ignored": hit,
               "metrics": {k: m.get(k) for k in keys}, "counts": m.get("counts"),
               "base": {"grade": base.get("grade"), "score": base.get("score"), "metrics": {k: bm.get(k) for k in keys}},
               "per_metric": {k: {"label": v.get("label"), "value": v.get("value"), "grade": v.get("grade")}
                              for k, v in (g.get("per_metric") or {}).items()},
               "record": None}
        if r.get("record_id") and r["kind"] not in ("alignment", "calibration"):
            for c in db.copy_grades(r["record_id"]).get(r["record_id"], []):
                if copy_key(c["copy"]) != copy_key(r.get("copy_label")):
                    continue
                sides = [dict(s) for s in c["sides"]]
                decisive = any(s["id"] == rec_id for s in sides)        # only the latest recording per side counts
                for s in sides:
                    if s["id"] == rec_id:
                        s["grade"] = g.get("grade")
                out["record"] = {"copy": c["copy"], "base": c["grade"], "decisive": decisive,
                                 "grade": grading.record_grade([s["grade"] for s in sides]) if decisive else c["grade"],
                                 "sides": [{"side": s["side"], "grade": s["grade"]} for s in sides]}
        return out

    @app.get("/api/recordings/{rec_id}/snippet")
    def snippet(rec_id: int, t: float, before: float = 0.5, after: float = 1.0):
        """A short excerpt of the recording around t as WAV (16 bit)."""
        r = db.recording(rec_id)
        if not r:
            raise HTTPException(404, i18n.msg("api.recording_not_found"))
        if r.get("audio_deleted_at"):
            raise HTTPException(410, _audio_gone_msg(r))
        if not r.get("path") or not Path(r["path"]).exists():
            raise HTTPException(404, i18n.msg("api.audio_missing"))
        with sf.SoundFile(r["path"]) as f:
            sr = f.samplerate
            start = max(0, int((t - min(before, 5)) * sr))
            f.seek(min(start, f.frames))
            data = f.read(int((min(before, 5) + min(after, 60)) * sr), dtype="int16")   # 60 s: "play from here"
        buf = io.BytesIO()
        sf.write(buf, data, sr, format="WAV", subtype="PCM_16")
        return Response(buf.getvalue(), media_type="audio/wav")

    @app.get("/api/recordings/{rec_id}/clicks.wav")
    def clicks_wav(rec_id: int, kinds: str = "pop,click,tick", min_strength: float = 0.0):
        """Difference track (idea from grooveclean): only the detected clicks, silence in between. Played loud, you hear
        exactly what was counted; if music sounds through, it is a false alarm. Built block by block so that a whole
        side does not have to fit in memory; the file is deleted after sending."""
        r = db.recording(rec_id)
        if not r:
            raise HTTPException(404, i18n.msg("api.recording_not_found"))
        if r.get("audio_deleted_at"):
            raise HTTPException(410, _audio_gone_msg(r))
        if not r.get("path") or not Path(r["path"]).exists():
            raise HTTPException(404, i18n.msg("api.audio_missing"))
        want = {k.strip() for k in kinds.split(",") if k.strip()}
        evs = [e for e in db.events(rec_id, limit=200000, order="t")
               if e["kind"] in want and e["region"] != "excluded" and e["strength"] >= min_strength]
        tmp_dir = cfg.data_dir / "tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        tmp = tmp_dir / f"clicks-{rec_id}-{int(time.time() * 1000)}.wav"
        with sf.SoundFile(r["path"]) as f:
            sr = f.samplerate
            starts = np.array([max(0, int((e["t"] - 0.001) * sr)) for e in evs], dtype=np.int64)
            ends = np.array([int((e["t"] + max(float(e["dur_ms"]), 1.0) / 1000 + 0.0015) * sr) for e in evs], dtype=np.int64)
            ramp_n = int(0.0005 * sr)
            with sf.SoundFile(str(tmp), "w", samplerate=sr, channels=f.channels, subtype="PCM_16") as out:
                block = 10 * sr
                for b0 in range(0, f.frames, block):
                    f.seek(b0)
                    data = f.read(block, dtype="int16", always_2d=True)
                    n = len(data)
                    y = np.zeros_like(data)
                    for i in np.flatnonzero((ends > b0) & (starts < b0 + n)):
                        s, e = int(max(starts[i], b0) - b0), int(min(ends[i], b0 + n) - b0)
                        w = e - s
                        if w <= 0:
                            continue
                        ramp = np.ones(w)
                        m = min(w // 2, ramp_n)
                        if m:
                            ramp[:m] = 0.5 - 0.5 * np.cos(np.pi * np.arange(m) / m)
                            ramp[w - m:] = ramp[:m][::-1]
                        cand = (data[s:e].astype(np.float64) * ramp[:, None]).astype(np.int16)
                        y[s:e] = np.where(np.abs(cand) > np.abs(y[s:e]), cand, y[s:e])
                    out.write(y)
        size = tmp.stat().st_size

        def stream():
            # streamed in blocks as a plain download (no range requests, see the cover image); the file goes once
            # the response is sent
            with open(tmp, "rb") as f:
                while chunk := f.read(1024 * 1024):
                    yield chunk
        headers = {"X-Events": str(len(evs)), "Content-Length": str(size),
                   "Content-Disposition": f'attachment; filename="{rec_id:05d}-clicks.wav"'}
        return StreamingResponse(stream(), media_type="audio/wav", headers=headers,
                                 background=BackgroundTask(os.unlink, str(tmp)))

    sg_cache: dict = {}            # (recording, section) → finished image; the latest 16 (zoom)

    def _sg_response(meta: dict, img: bytes, stored: bool) -> Response:
        headers = {"X-Rows": str(meta["rows"]), "X-Cols": str(meta["cols"]), "X-T0": f"{meta['t0']:.4f}",
                   "X-T1": f"{meta['t1']:.4f}", "X-Fmin": str(meta["fmin"]), "X-Fmax": str(meta["fmax"]),
                   "X-Top-Db": str(meta["top_db"]), "X-Floor-Db": str(meta["floor_db"]),
                   "X-Stored": "1" if stored else "0", "Cache-Control": "private, max-age=3600"}
        return Response(img, media_type="application/octet-stream", headers=headers)

    @app.get("/api/recordings/{rec_id}/spectrogram")
    def spectrogram(rec_id: int, cols: int = spectro.FULL_COLS, rows: int = spectro.FULL_ROWS,
                    t0: float = 0.0, t1: float | None = None, fmax: float = 16000.0, top: float | None = None):
        """Spectrogram as a uint8 matrix (rows × cols, row 0 = lowest frequency), metadata in headers.
        `top` = level (dB) mapped to 255, so that two recordings get the same color scale.

        The whole recording at the default size comes from the database (precomputed by the worker, kept after the
        audio is deleted). Sections (zoom) are computed from the audio."""
        r = db.recording(rec_id)
        if not r:
            raise HTTPException(404, i18n.msg("api.recording_not_found"))
        def display(meta: dict, img: np.ndarray, stored: bool) -> Response:
            # computed with STORE_RANGE, shown with DISPLAY_RANGE, top level optionally given
            new_top = meta["top_db"] if top is None else float(top)
            out = spectro.rescale(img, meta["top_db"], new_top)
            return _sg_response(meta | {"top_db": new_top, "floor_db": round(new_top - spectro.DISPLAY_RANGE, 2)},
                                out.tobytes(), stored)

        full = t0 == 0 and t1 is None and cols == spectro.FULL_COLS and rows == spectro.FULL_ROWS and fmax == 16000.0
        if full:
            stored = db.spectrogram(rec_id)
            if stored:
                meta, img = stored
                return display(meta, np.frombuffer(img, np.uint8), True)
        if r.get("audio_deleted_at"):
            raise HTTPException(410, _audio_gone_msg(r))
        if not r.get("path") or not Path(r["path"]).exists():
            raise HTTPException(404, i18n.msg("api.audio_missing"))
        if full and r.get("analysis"):
            meta = worker.store_spectrogram(rec_id, r["path"])     # older analysis: compute it now and keep it
            if meta:
                return spectrogram(rec_id, cols, rows, t0, t1, fmax, top)
        cols, rows = max(50, min(cols, 4000)), max(40, min(rows, 400))
        key = (rec_id, cols, rows, round(t0, 3), None if t1 is None else round(t1, 3), fmax)
        if key not in sg_cache:
            try:
                res = spectro.compute(r["path"], cols, rows, fmax=fmax, t0=t0, t1=t1)
            except (ValueError, RuntimeError) as e:
                raise HTTPException(422, i18n.msg("api.spectrogram_failed", error=i18n.error_message(e))) from e
            if len(sg_cache) >= 16:
                del sg_cache[next(iter(sg_cache))]
            sg_cache[key] = res
        res = sg_cache[key]
        return display({k: res[k] for k in ("rows", "cols", "t0", "t1", "fmin", "fmax", "top_db", "floor_db")},
                       res["img"], False)

    # ─── Comparisons ────────────────────────────────────────────────────────
    @app.post("/api/comparisons")
    def add_comparison(c: CompareIn):
        """Queue a before/after comparison of two recordings of the same record side."""
        a, b = db.recording(c.a_id), db.recording(c.b_id)
        if not a or not b:
            raise HTTPException(404, i18n.msg("api.recording_not_found"))
        if "alignment" in (a["kind"], b["kind"]):
            raise HTTPException(409, i18n.msg("api.compare_alignment"))
        if not (a.get("analysis") and b.get("analysis")):
            raise HTTPException(409, i18n.msg("api.compare_not_analyzed"))
        if a.get("record_id") and b.get("record_id") and a["record_id"] != b["record_id"]:
            raise HTTPException(409, i18n.msg("api.compare_different_records", a=a["id"], b=b["id"]))
        if a.get("record_id") and a.get("side") != b.get("side"):
            raise HTTPException(409, i18n.msg("api.compare_different_sides", a=a["id"], b=b["id"],
                                              side_a=a["side"], side_b=b["side"]))
        for r in (a, b):
            if r.get("audio_deleted_at") or not (r.get("path") and Path(r["path"]).exists()):
                raise HTTPException(409, i18n.msg("api.compare_audio_missing", id=r["id"], reason=_audio_gone_msg(r)))
        return {"id": worker.compare(c.a_id, c.b_id)}

    @app.get("/api/comparisons/{cid}")
    def get_comparison(cid: int):
        c = db.comparison(cid)
        if not c:
            raise HTTPException(404, i18n.msg("api.comparison_not_found"))
        return c
