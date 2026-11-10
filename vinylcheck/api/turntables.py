"""Turntables: target maps, turntable profiles with their guided alignment, and alignment runs.

Part of the API of `vinylcheck.web`: `install` adds the routes to the app. What the areas share lives in the
`Station` (`vinylcheck/station.py`).
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI, HTTPException

from .. import alignment, i18n, onboarding, testrecords, vinylmind
from .common import AcceptIn, _check_chain, _check_sensitivity, MapIn, ProfileIn, ProfilePatch, SetupIn, SetupPatch

if TYPE_CHECKING:
    from ..station import Station


def install(app: FastAPI, station: Station) -> None:
    cfg = station.cfg
    db = station.db
    worker = station.worker
    recorder = station.recorder
    vm = station.vm
    ref_cache = station.ref_cache

    def _setup_findings(sid: int) -> tuple[list[dict], dict, list[dict]]:
        recs = db.setup_recordings(sid)
        summary, items = alignment.merge_summaries([r for r in recs if r["is_alignment"]])
        s = db.setup(sid) or {}
        _, targets = station.targets_for(s.get("profile_id"))
        return recs, summary, alignment.apply_targets(items, targets)

    @app.get("/api/targets")
    def get_targets():
        maps = station.maps()
        fields = {k: {"label": v[0], "unit": v[1], "kind": v[2], "hint": v[3]}
                  for k, v in alignment.TARGET_FIELDS.items()}
        return {"active": station.active_map_id(), "fields": fields, "maps": list(maps.values())}

    @app.put("/api/targets/{mid}")
    def put_targets(mid: str, m: MapIn):
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,39}", mid):
            raise HTTPException(422, i18n.msg("api.map_id_invalid"))
        try:
            targets = alignment.validate_targets(m.targets)
        except ValueError as e:
            raise HTTPException(422, i18n.error_message(e)) from e
        maps = station.maps()
        base = alignment.TARGET_MAPS.get(mid)
        if base:
            # Built-in maps (DIN 45500, manufacturers, standard) are read-only: they come from outside sources and
            # should stay as they are. Changing one means copying it as a custom map.
            raise HTTPException(409, i18n.msg("api.map_builtin_readonly", name=base["name"]))
        if set(targets) != set(alignment.TARGET_FIELDS):
            missing = [alignment.TARGET_FIELDS[k][0] for k in alignment.TARGET_FIELDS if k not in targets]
            raise HTTPException(422, i18n.msg("api.map_missing_fields", fields=i18n.msg_join(missing, ", ")))
        custom = json.loads(db.get_settings().get("target_maps") or "{}")
        name = m.name.strip() or (maps.get(mid) or {}).get("name") or mid
        custom[mid] = {"name": name, "note": m.note.strip() if m.note else (maps.get(mid) or {}).get("note", ""), "targets": targets}
        db.set_setting("target_maps", json.dumps(custom))
        return get_targets()

    @app.delete("/api/targets/{mid}")
    def delete_targets(mid: str):
        """Delete a custom map, or reset a built-in one to its shipped values."""
        custom = json.loads(db.get_settings().get("target_maps") or "{}")
        if mid not in custom:
            raise HTTPException(404, i18n.msg("api.map_nothing_to_delete"))
        del custom[mid]
        db.set_setting("target_maps", json.dumps(custom))
        if station.active_map_id() == mid and mid not in alignment.TARGET_MAPS:
            db.set_setting("target_map_active", "standard")
        return get_targets()

    @app.post("/api/targets/active")
    def set_active_targets(body: dict):
        mid = str(body.get("id", ""))
        if mid not in station.maps():
            raise HTTPException(404, i18n.msg("api.map_not_found"))
        db.set_setting("target_map_active", mid)
        return get_targets()

    @app.get("/api/setups")
    def setups(profile_id: int | None = None):
        out = []
        for s in db.setups(profile_id):
            _, summary, items = _setup_findings(s["id"])
            s["status_counts"] = {k: sum(1 for it in items if it["status"] == k) for k in ("ok", "warn", "bad")}
            s["summary"] = {k: summary.get(k) for k in ("antiskate_break_l_db", "antiskate_break_r_db",
                                                        "tracking_reserve_db", "resonance_hz", "ref_level_dbfs")}
            out.append(s)
        return out

    @app.post("/api/setups")
    def add_setup(s: SetupIn):
        f = s.model_dump()
        if f["profile_id"] is None:
            act = db.active_profile()
            f["profile_id"] = act["id"] if act else None
        return {"id": db.add_setup(**f)}

    # ─── Turntable profiles ─────────────────────────────────────────────────
    # A profile = turntable + cartridge + capture device. The wizard calibrates it with the test record; the active
    # profile sets the device, the chain noise, the pop threshold (relative to the reference level) and the speed
    # correction of the skip detection. The wizard's progress follows from the recordings (resumable).
    def _profile_progress(p: dict) -> dict:
        cal = db.latest_calibration(p["id"])
        runs = db.setups(p["id"])
        rid = p.get("wizard_setup_id") or p.get("baseline_setup_id")
        run = db.setup(rid) if rid else (runs[0] if runs else None)
        remeasure = bool(p.get("wizard_setup_id") and p.get("accepted_at"))
        sides = {}
        if run:
            for r in db.setup_recordings(run["id"]):           # oldest first → the latest per side remains
                sides[str(r["side"])] = r
        def side_state(k):
            r = sides.get(k)
            return None if not r else {"recording_id": r["id"], "status": r["status"], "error": r.get("error"),
                                       "created_at": r["created_at"]}
        rec_sides = testrecords.of_profile(db, p)["sides"]          # the sides of its test record to record
        steps = {
            "profile": True,
            "calibration": {"recording_id": cal["id"], "chain_db": cal["chain_db"], "created_at": cal["created_at"]} if cal else None,
            "run_id": run["id"] if run else None,
            "sides": rec_sides, **{"side" + k: side_state(k) for k in rec_sides},
            "accepted": p.get("accepted_at"), "remeasure": remeasure,
        }
        if p.get("accepted_at") and not remeasure:
            nxt = "done"
        elif not cal:
            nxt = "calibration"
        else:
            nxt = next(("side" + k for k in rec_sides
                        if not (steps["side" + k] and steps["side" + k]["status"] == "analyzed")), "accept")
        return steps | {"next": nxt}

    def _profile_out(p: dict) -> dict:
        p = dict(p)
        p["baseline"] = json.loads(p["baseline"]) if p.get("baseline") else None
        mid, targets = station.targets_for(p["id"])
        p["targets_used"] = {"id": mid, "name": station.maps()[mid]["name"], "own": bool(p.get("target_map"))}
        if p["baseline"] and p["baseline"].get("results"):
            p["baseline"]["results"] = alignment.apply_targets(p["baseline"]["results"], targets)
        p["progress"] = _profile_progress(p)
        p["connected"] = (not p["device"]) or bool(cfg.fake_source) or p["device"] in {
            d["device"] for d in station.capture_devices()}
        p["chain"], p["chain_guess"] = vinylmind.chain_of(p), vinylmind.guess_chain(p)   # Vinyl-Mind chain class
        p["test_record_info"] = testrecords.of_profile(db, p)
        return p

    def _profile_readiness(p: dict) -> dict | None:
        """Is the turntable good enough to measure records? From the wizard's run while it waits for acceptance,
        else from the accepted reference (onboarding.readiness)."""
        pr, rec = p["progress"], p["test_record_info"]
        if pr["next"] == "accept" and pr.get("run_id"):
            _, _, items = _setup_findings(pr["run_id"])
            calibrated = bool(pr.get("calibration"))
        elif pr["next"] == "done" and p.get("baseline"):
            items, calibrated = p["baseline"].get("results") or [], p.get("chain_db") is not None
        else:
            return None
        return onboarding.readiness(items, rec, calibrated=calibrated, fixed_gain=bool(p.get("fixed_gain")))

    def _check_test_record(f: dict) -> None:
        if f.get("test_record") == "":
            f["test_record"] = None
        if f.get("test_record"):
            rec = testrecords.get(db, f["test_record"])
            if not rec:
                raise HTTPException(422, i18n.msg("testrecords.not_found"))
            if not testrecords.usable(rec):
                raise HTTPException(422, i18n.msg("testrecords.not_usable", name=rec["name"]))

    @app.get("/api/profiles")
    def profiles():
        return [_profile_out(p) for p in db.profiles()]

    @app.post("/api/profiles")
    def add_profile(p: ProfileIn):
        if not p.name.strip():
            raise HTTPException(422, i18n.msg("api.profile_name_required"))
        f = p.model_dump()
        _check_chain(f)
        _check_test_record(f)
        pid = db.add_profile(**f)
        if not db.active_profile():
            db.activate_profile(pid)
        return get_profile(pid)

    @app.get("/api/profiles/{pid}")
    def get_profile(pid: int):
        p = db.profile(pid)
        if not p:
            raise HTTPException(404, i18n.msg("api.profile_not_found"))
        out = _profile_out(p)
        out["setups"] = db.setups(pid)
        out["readiness"] = _profile_readiness(out)
        return out

    @app.patch("/api/profiles/{pid}")
    def patch_profile(pid: int, p: ProfilePatch):
        prof = db.profile(pid)
        if not prof:
            raise HTTPException(404, i18n.msg("api.profile_not_found"))
        f = p.model_dump(exclude_unset=True)
        _check_sensitivity(f.get("sensitivity"))
        _check_chain(f)
        _check_test_record(f)
        db.update_profile(pid, **f)
        if {"chain_source", "chain_cartridge", "device", "cartridge"} & set(f):
            vm.note_retrained()                   # the chain class of its contribution may have changed
        if "sensitivity" in f and prof.get("accepted_at") and prof.get("baseline_setup_id"):
            # The drive's noise floor is only valid for the sensitivity it was measured with: recompute side 2 of
            # the accepted run (the worker then updates the floor)
            for r in db.setup_recordings(prof["baseline_setup_id"]):
                if str(r["side"]) == "2" and r.get("path") and Path(r["path"]).exists() and not r.get("audio_deleted_at"):
                    worker.analyze(r["id"])
        return get_profile(pid)

    @app.post("/api/profiles/{pid}/activate")
    def activate_profile(pid: int):
        if not db.profile(pid):
            raise HTTPException(404, i18n.msg("api.profile_not_found"))
        if recorder.active:
            raise HTTPException(409, i18n.msg("api.profile_switch_while_recording"))
        db.activate_profile(pid)
        return get_profile(pid)

    @app.post("/api/profiles/{pid}/run")
    def profile_run(pid: int, new: bool = False):
        """Create the wizard's calibration run (or return the current one). `new`: calibrate again; the previous
        reference stays valid until the new result is accepted. Label and notes are stored in the request's language."""
        p = db.profile(pid)
        if not p:
            raise HTTPException(404, i18n.msg("api.profile_not_found"))
        if p.get("wizard_setup_id") and db.setup(p["wizard_setup_id"]):
            return {"id": p["wizard_setup_id"]}
        if not new and not p.get("accepted_at") and p.get("baseline_setup_id") and db.setup(p["baseline_setup_id"]):
            return {"id": p["baseline_setup_id"]}
        sid = db.add_setup(profile_id=pid, label=i18n.t("labels.calibration_run", name=p["name"]),
                           test_record=testrecords.label(testrecords.of_profile(db, p)),
                           tracking_force_g=p.get("tracking_force_g"), bias=p.get("antiskating") or "",
                           notes=i18n.t("labels.record_weight", weight=p["weight"]) if p.get("weight") else "")
        db.update_profile(pid, wizard_setup_id=sid)
        return {"id": sid}

    @app.post("/api/profiles/{pid}/accept")
    def profile_accept(pid: int, a: AcceptIn):
        """Take the result of a run as the profile's reference: reference level, speed, chain noise, findings."""
        p = db.profile(pid)
        if not p:
            raise HTTPException(404, i18n.msg("api.profile_not_found"))
        sid = a.setup_id or _profile_progress(p)["run_id"]
        if not sid or not db.setup(sid):
            raise HTTPException(409, i18n.msg("api.no_calibration_run"))
        recs, summary, items = _setup_findings(sid)
        if summary.get("ref_level_dbfs") is None:
            raise HTTPException(409, i18n.msg("api.run_missing_reference"))
        rec = testrecords.of_profile(db, p)
        if not rec["reference"]:         # its 1 kHz tone (if any) is not at a reference level: no pop threshold from it
            raise HTTPException(409, i18n.msg("testrecords.no_reference", name=testrecords.label(rec)))
        cal = db.latest_calibration(pid)
        base = {"setup_id": sid, "summary": summary,
                "results": [{k: it.get(k) for k in ("key", "label", "value", "unit", "status", "target")}
                            for it in items]}
        db.update_profile(pid, baseline_setup_id=sid, wizard_setup_id=None, ref_dbfs=summary.get("ref_level_dbfs"),
                          speed_pct=summary.get("speed_pct"), chain_db=cal["chain_db"] if cal else None,
                          calibration_id=cal["id"] if cal else None, baseline=json.dumps(base), accepted_at=time.time(),
                          floor_clicks_per_min=summary.get("floor_clicks_per_min"),
                          floor_ticks_per_min=summary.get("floor_ticks_per_min"))
        if onboarding.state(db).get("state") != "skipped":
            onboarding.set_state(db, "done")          # the first turntable is measured: the tutorial is over
        return get_profile(pid)

    @app.delete("/api/profiles/{pid}")
    def delete_profile(pid: int):
        p = db.profile(pid)
        if not p:
            raise HTTPException(404, i18n.msg("api.profile_not_found"))
        if p["active"] and len(db.profiles()) > 1:
            raise HTTPException(409, i18n.msg("api.profile_active_delete"))
        db.delete_profile(pid)                         # recordings and runs stay, without a profile
        return {"ok": True}

    @app.get("/api/setups/compare")
    def compare_setups(a: int, b: int):
        sa, sb = db.setup(a), db.setup(b)
        if not sa or not sb:
            raise HTTPException(404, i18n.msg("api.setup_not_found"))
        _, sum_a, _ = _setup_findings(a)
        _, sum_b, _ = _setup_findings(b)
        rows = alignment.compare_summaries(sum_a, sum_b)
        notes = []
        ra, rb = sum_a.get("ref_level_dbfs"), sum_b.get("ref_level_dbfs")
        if ra is not None and rb is not None and abs(ra - rb) > 1.0:
            notes.append(i18n.msg("api.setups_ref_higher" if rb > ra else "api.setups_ref_lower", db=abs(rb - ra)))
        return {"a": sa, "b": sb, "rows": rows, "notes": notes}

    @app.get("/api/setups/{sid}")
    def get_setup(sid: int):
        s = db.setup(sid)
        if not s:
            raise HTTPException(404, i18n.msg("api.setup_not_found"))
        recs, summary, items = _setup_findings(sid)
        for r in recs:
            res = r.pop("results")
            r.pop("summary")
            r["status_counts"] = {k: sum(1 for it in res if it["status"] == k) for k in ("ok", "warn", "bad")}
            r["audio_available"] = bool(r.get("path")) and Path(r["path"]).exists() and not r.get("audio_deleted_at")
        return s | {"recordings": recs, "summary": summary, "results": items}

    @app.patch("/api/setups/{sid}")
    def patch_setup(sid: int, p: SetupPatch):
        if not db.setup(sid):
            raise HTTPException(404, i18n.msg("api.setup_not_found"))
        # exclude_unset: a null that is sent clears the field (e.g. stylus hours), a missing one stays
        db.update_setup(sid, **p.model_dump(exclude_unset=True))
        return get_setup(sid)

    @app.delete("/api/setups/{sid}")
    def delete_setup(sid: int):
        if recorder.active and recorder.state.get("meta", {}).get("setup_id") == sid:
            raise HTTPException(409, i18n.msg("api.setup_recording_running"))
        for path in db.delete_setup(sid):
            station.remove_audio(path)
        ref_cache["key"] = None
        return {"ok": True}
