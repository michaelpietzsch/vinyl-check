"""Settings, status and upkeep: the settings form, the status the page lives on (also pushed over the WebSocket),
system load and jobs, the error log, the capture devices, the learned model, regrading and the retention run.

Part of the API of `vinylcheck.web`: `install` adds the routes to the app. What the areas share lives in the
`Station` (`vinylcheck/station.py`).
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect

from .. import __version__, grading, i18n, learning, second_opinion
from ..config import config_file
from ..db import error_key
from .common import KIND_LABEL, _localize, _setting_value, SETTINGS, STATIC, storage_status, system_load

if TYPE_CHECKING:
    from ..station import Station


def install(app: FastAPI, station: Station) -> None:
    cfg = station.cfg
    db = station.db
    worker = station.worker
    recorder = station.recorder
    aud = station.aud
    dc = station.dc
    mover = station.mover
    checker = station.checker
    desktop = station.desktop
    app_started_at = station.app_started_at
    audio_backend = station.audio_backend
    defaults = station.defaults
    learn = station.learn
    ref_cache = station.ref_cache
    started_at = station.started_at
    ui_events = station.ui_events

    def last_reference() -> dict | None:
        """Level of the reference tone in the latest alignment run: the yardstick for setting the input level (live
        meter). Re-read only when the worker finished something or something was deleted (cache key), because
        status() runs five times per second during a recording."""
        key = worker.completed
        if key != ref_cache["key"]:
            val = None
            for row in db.q("""SELECT r.id, r.setup_id, a.metrics, a.analyzed_at FROM analyses a
                                 JOIN recordings r ON r.id = a.recording_id WHERE r.kind = 'alignment'
                                ORDER BY a.analyzed_at DESC LIMIT 10"""):
                v = json.loads(row["metrics"]).get("summary", {}).get("ref_level_dbfs")
                if v is not None:
                    val = {"ref_level_dbfs": v, "recording_id": row["id"], "setup_id": row["setup_id"],
                           "at": row["analyzed_at"]}
                    break
            ref_cache.update(key=key, val=val)
        return ref_cache["val"]

    def status() -> dict:
        """Everything the web interface polls; also pushed over the WebSocket."""
        prof = db.active_profile()
        rs = recorder.status()
        if rs.get("active") and rs.get("expected_end_s"):
            rs["remaining_s"] = round(rs["expected_end_s"] - rs["frames"] / rs["rate"], 1)
        cal = (db.latest_calibration(prof["id"]) if prof else None) or db.latest_calibration()
        return {"version": __version__, "recorder": rs, "worker": worker.status(), "events": ui_events[-5:],
                "storage": storage_status(cfg), "storage_move": mover.status(), "update": checker.status(), "desktop": desktop,
                "calibration": {"id": cal["id"], "chain_db": cal["chain_db"], "created_at": cal["created_at"]}
                if cal else None,
                "fake_source": bool(cfg.fake_source), "discogs": dc.enabled, "discogs_collection": dc.status(),
                "retention_days": cfg.retention_days, "engineer_mode": cfg.engineer_mode,
                "learn": {"enabled": cfg.learn_enabled, "grade": cfg.learn_grade, "model_id": (station.learn_model() or {}).get("id"),
                          "usable": [k for k, v in ((station.learn_model() or {}).get("kinds") or {}).items() if v.get("usable")]},
                "ui_build": _ui_build(),
                "restart_pending": station.restart_pending(),
                "started_at": round(started_at, 3),         # changes with every restart: the interface reloads its view
                "clip_limits": {"mention": cfg.clip_mention_samples, "max": cfg.clip_max_samples, "grace_s": cfg.clip_grace_s},
                "second_opinion": second_opinion.info(),
                "discogs_write": dc.enabled,
                "profile": {"id": prof["id"], "name": prof["name"], "device": prof["device"],
                            "accepted": bool(prof["accepted_at"]), "ref_dbfs": prof["ref_dbfs"],
                            "speed_pct": prof["speed_pct"], "targets": station.targets_for(prof["id"])[0],
                            "floor_clicks_per_min": prof.get("floor_clicks_per_min"),
                            "floor_ticks_per_min": prof.get("floor_ticks_per_min"),
                            "sensitivity": prof.get("sensitivity"), "fixed_gain": bool(prof.get("fixed_gain"))} if prof else None,
                "targets": station.active_map_id(),
                "grading_calibrated": cfg.grading.calibrated,
                "grading_limits": {k: list(getattr(cfg.grading, k)) for k in cfg.grading.metrics},
                "grading": {"version": grading.VERSION, "calibrated": cfg.grading.calibrated, "grades": grading.GRADES,
                            "bands": [list(b) for b in grading.BANDS], "graded": list(cfg.grading.graded),
                            "secondary": list(cfg.grading.secondary), "secondary_gap": cfg.grading.secondary_gap,
                            "labels": {k: grading.METRIC_LABELS[k] for k in cfg.grading.metrics}},
                "level_target": {"ref_dbfs": cfg.alignment.target_ref_dbfs, "tol_db": cfg.alignment.target_ref_tol_db,
                                 "headroom_db": cfg.alignment.headroom_db, "last": last_reference()}}

    # ─── Status ─────────────────────────────────────────────────────────────
    @app.get("/api/settings")
    def get_settings():
        audio = aud.effective()           # what the next level check would use (Audio interface, else configuration)
        return {"values": {k: getattr(cfg, k) for k in SETTINGS}, "defaults": defaults,
                "spec": {k: {kk: (vv.__name__ if kk == "type" else vv) for kk, vv in v.items()} for k, v in SETTINGS.items()},
                "info": {"version": __version__, "device": audio["device"] or i18n.msg("labels.device_auto"),
                         "rate": audio["rate"],
                         "channels": audio["channels"], "format": audio["sample_format"],
                         "audio_system": aud.system(),
                         "data_dir": str(cfg.data_dir), "state_dir": str(cfg.state_dir),
                         "discogs": i18n.msg(f"api.discogs_info_{station.discogs_source() or 'off'}", user=dc.username),
                         "fake_source": bool(cfg.fake_source),
                         "audio_backend": audio_backend,
                         "config_file": str(config_file()[0]), "desktop": desktop}}

    @app.put("/api/settings")
    def put_settings(body: dict):
        clean, errors = {}, {}
        for k, raw in body.items():
            if k not in SETTINGS:
                errors[k] = i18n.msg("api.setting_unknown")
                continue
            try:
                clean[k] = _setting_value(SETTINGS[k], raw)
            except ValueError as e:
                errors[k] = i18n.error_message(e)
        if errors:
            raise HTTPException(422, {"fields": errors})
        for k, v in clean.items():
            object.__setattr__(cfg, k, v)
            db.set_setting(k, json.dumps(v) if SETTINGS[k]["type"] is bool else str(v))
        dc.ttl = cfg.discogs_sync_minutes * 60
        return get_settings()

    sys_prev: dict = {}
    _static = STATIC.resolve()

    def _ui_build() -> int:
        """Build stamp of the web interface files (latest change of app.js/style.css/index.html). When it changes while
        a page is open, the interface asks for a reload."""
        try:
            return int(max(os.stat(_static / n).st_mtime for n in ("app.js", "style.css", "index.html")))
        except OSError:
            return 0

    def _job_label(kind: str, ident: int) -> dict:
        """Readable name of a worker job, e.g. "Recording #12: Artist – Title, side A, uncleaned" (a message)."""
        if kind in ("analyze", "master"):
            r = db.recording(ident) or {}
            rec = db.record(r["record_id"]) if r.get("record_id") else None
            who = f"{rec['artist']} – {rec['title']}" if rec else None
            side = i18n.msg("api.job_side", side=r.get("side", "?"))
            if kind == "analyze":
                if not rec and r.get("setup_id"):
                    who = i18n.msg("api.job_setup_run", id=r["setup_id"])
                head = i18n.msg("api.job_recording", id=ident)
                parts = [who, side, KIND_LABEL.get(r.get("kind"), r.get("kind"))]
            else:
                head, parts = i18n.msg("api.job_master", id=ident), [who, side, r.get("copy_label")]
            return i18n.msg_join([head, i18n.msg_join(parts, ", ")], ": ")
        c = db.comparison(ident) or {}
        return i18n.msg("api.job_comparison", id=ident, a=c.get("a_id"), b=c.get("b_id"))

    @app.get("/api/system")
    def system_status():
        """Jobs (running, waiting, recent) and host load, for the "System" tab."""
        w = worker.status()
        cur = dict(w["current"]) if w.get("current") else None
        if cur:
            cur["label"] = _job_label(cur["kind"], cur["id"])
            cur["elapsed_s"] = round(time.time() - cur["since"], 1)
            r = db.recording(cur["id"]) if cur["kind"] == "analyze" else None
            cur["duration_s"] = (r or {}).get("duration_s")
        rs = recorder.state
        return {"load": system_load(sys_prev),
                "service": {"version": __version__, "started_at": app_started_at, "second_opinion": second_opinion.available()},
                "worker": {"current": cur,
                           "pending": [p | {"label": _job_label(p["kind"], p["id"])} for p in w.get("pending", [])],
                           "history": [h | {"label": _job_label(h["kind"], h["id"]), "error_key": error_key(h.get("error"))}
                                       for h in w.get("history", [])]},
                "recorder": {"active": recorder.active, "mode": rs.get("mode"), "elapsed_s": rs.get("elapsed_s"),
                             "recording_id": (rs.get("meta") or {}).get("recording_id")},
                "storage": storage_status(cfg)}

    # ─── Error log: what failed, where and when (System tab); kept across restarts ───────────────────────
    @app.get("/api/problems")
    def problems(limit: int = 200):
        out = []
        for p in db.problems(max(1, min(limit, 500))):
            src, ref = p["source"], p["ref_id"]
            where = {}
            if src in ("analyze", "master", "recording") and ref:
                r = db.recording(ref) or {}
                where = {"recording_id": ref if r else None, "record_id": r.get("record_id"), "setup_id": r.get("setup_id"),
                         "status": r.get("status")}
            label = _job_label("analyze" if src == "recording" else src, ref) if ref else None
            out.append({"id": p["id"], "at": p["at"], "source": src, "label": label, "message": p["message"],
                        "key": p["key"], **where})
        return out

    @app.delete("/api/problems")
    def problems_clear():
        db.clear_problems()
        return []

    @app.get("/api/health")
    def health():
        return {"ok": True, "version": __version__}

    @app.get("/api/status")
    def get_status():
        return status()

    @app.websocket("/api/ws")
    async def ws(sock: WebSocket):
        await sock.accept()
        try:
            while True:
                await sock.send_text(json.dumps(_localize(status())))
                await asyncio.sleep(0.2 if recorder.active else 1.0)
        except (WebSocketDisconnect, RuntimeError):
            return

    @app.get("/api/devices")
    def devices():
        devs = station.capture_devices()
        if cfg.fake_source:
            fake = "file:" + Path(cfg.fake_source).name
            devs.insert(0, {"id": "file", "name": i18n.msg("labels.test_file", name=Path(cfg.fake_source).name),
                            "alsa": fake, "device": fake, "usb": False, "info": ""})
        return devs

    @app.get("/api/learn")
    def learn_status():
        """Learned model: state per kind (examples, cross-validation, usable), without weights."""
        return {"enabled": cfg.learn_enabled, "grade": cfg.learn_grade, **learning.summary(learn["model"]),
                "applied_at": learn["applied_at"], "applied": learn["applied"], "last_error": learn["last_error"],
                "settings": {"min_per_class": learning.MIN_PER_CLASS, "min_master_recordings": learning.MIN_MASTER_RECORDINGS,
                             "max_est_error": learning.MAX_EST_ERROR, "manual_weight": learning.MANUAL_WEIGHT,
                             "feature_version": cfg.feature_version}}

    @app.post("/api/learn")
    def learn_now():
        """Retrain now and recount all recordings (otherwise this runs by itself every 30 s when something changed)."""
        model = station.learn_train()
        learn["fp"], learn["pending_fp"] = station.learn_fp(), None
        n = station.learn_apply()
        return {"model_id": model["id"], "applied": n, **learning.summary(model)}

    @app.get("/api/audit/export")
    def audit_export():
        rows = db.labeled_events()
        return {"model": second_opinion.info(), "count": len(rows), "events": rows}

    @app.post("/api/analyses/regrade")
    def analyses_regrade():
        """Regrade everything with the current scheme and limits (without recomputing the audio)."""
        return {"regraded": station.regrade_all(force=True), "grading_version": grading.VERSION}

    @app.post("/api/retention/run")
    def retention_run():
        """Run the retention cleanup now (otherwise it runs hourly)."""
        return {"retention_days": cfg.retention_days, "deleted": worker.cleanup()}
