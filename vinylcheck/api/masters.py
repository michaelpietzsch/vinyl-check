"""Engineer mode and reports: the masters of a record, the master comparison of a recording, the test pressing
approval, and the PDF reports.

Part of the API of `vinylcheck.web`: `install` adds the routes to the app. What the areas share lives in the
`Station` (`vinylcheck/station.py`).
"""

from __future__ import annotations

import io
import re
import shutil
import time
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import soundfile as sf
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response

from .. import __version__, compare, grading, i18n, learning, report_pdf, spectro
from ..jobs import MASTER_KINDS
from .common import (APPROVE, APPROVE_NOTES, AUDIBLE_STRENGTH, FINDINGS_LABEL, KIND_LABEL, _localize, log,
                     POLARITY_LABEL, REJECT, VERDICT_TEXT)

if TYPE_CHECKING:
    from ..station import Station


def install(app: FastAPI, station: Station) -> None:
    cfg, db, worker, mover = station.cfg, station.db, station.worker, station.mover

    # Grade by ear (engineer mode): audible genuine clicks per side instead of count rates. Rates can grade a side
    # harshly that sounds fine: the count finds hundreds of quiet events under the music where a listener hears two
    # clicks. Genuine = not in the music according to the master (or judged a click), audible = strength
    # ≥ AUDIBLE_STRENGTH; events judged false alarms never count. Computed from stored data, instantly.
    # Audible clicks per side up to: NM, VG+, VG, G+, G; above that G. One or two dust clicks are still NM.
    EAR_LIMITS = (2, 6, 15, 40, 100)

    def ear_grade(rec_id: int) -> dict | None:
        """Grade by ear from the stored master comparison, or None without a usable one."""
        c = db.master_check(rec_id)
        res = (c or {}).get("result") or {}
        clicks = res.get("clicks") or {}
        if not res.get("ok") or "events" not in clicks:     # no result, or an old one without per-event classification
            return None
        src = {round(x[0], 4): x[1] for x in clicks["events"]}
        aud = [e for e in db.events(rec_id) if e["region"] == "music" and e.get("label") != "music"
               and (e.get("label") == "defect" or src.get(round(e["t"], 4)) == "v")
               and e["strength"] >= AUDIBLE_STRENGTH]
        n = len(aud)
        g = next((grading.GRADES[i] for i, lim in enumerate(EAR_LIMITS) if n <= lim), "G")
        rec = db.recording(rec_id) or {}
        if (((rec.get("analysis") or {}).get("metrics") or {}).get("skips_back") or 0) > 0:
            g = "F"
        return {"grade": g, "audible": n, "threshold": AUDIBLE_STRENGTH, "limits": EAR_LIMITS,
                "top": [{"t": e["t"], "kind": e["kind"], "ch": e["ch"], "strength": e["strength"], "label": e.get("label")}
                        for e in sorted(aud, key=lambda e: -e["strength"])[:30]]}

    def master_grade(rec_id: int, res: dict) -> dict | None:
        """Grade against the master: the same calculation as the normal grade (current scheme), but only with the clicks
        and pops in the music that the master does not explain. Recomputed on every request so that it never goes
        stale after a scale change."""
        k = res.get("clicks") or {}
        if not res.get("ok") or k.get("vinyl_music_per_min") is None:
            return None
        am = (((db.recording(rec_id) or {}).get("analysis") or {}).get("metrics") or {})
        return grading.grade({"clicks_music_per_min": k.get("vinyl_music_per_min"), "pops_per_min": k.get("vinyl_pops_music_per_min"),
                              "noise_rel_db": am.get("noise_rel_db"), "clip_in_groove": am.get("clip_in_groove"),
                              "skips_back": am.get("skips_back")}, cfg.grading)
    MASTER_MAX_BYTES = 3 * 1024 ** 3
    MASTER_FORMATS = {"WAV": "wav", "WAVEX": "wav", "FLAC": "flac", "AIFF": "aiff", "W64": "w64"}

    @app.get("/api/records/{rid}/masters")
    def record_masters(rid: int):
        if not db.record(rid):
            raise HTTPException(404, i18n.msg("api.record_not_found"))
        return [station.master_info(m) for m in db.masters(rid)]

    @app.put("/api/records/{rid}/masters/{side}")
    async def put_master(rid: int, side: str, request: Request, name: str = ""):
        """Upload the master of a side: raw bytes in the body (WAV, FLAC or AIFF, at most 3 GB), streamed to disk.
        Replaces an existing one. In engineer mode every analyzed recording of this side is then measured against it;
        the upload is only accepted in that mode (the largest write the interface allows)."""
        if not cfg.engineer_mode:
            raise HTTPException(403, i18n.msg("api.engineer_mode_required"))
        if not db.record(rid):
            raise HTTPException(404, i18n.msg("api.record_not_found"))
        side = side.strip().upper()[:1]
        if not re.fullmatch(r"[A-Z]", side):
            raise HTTPException(422, i18n.msg("api.side_letter"))
        length = int(request.headers.get("content-length") or 0)
        if length > MASTER_MAX_BYTES:
            raise HTTPException(413, i18n.msg("api.master_too_large"))
        if mover.active:
            raise HTTPException(409, i18n.msg("storage.busy_master"))
        station.master_dir().mkdir(parents=True, exist_ok=True)
        if length and shutil.disk_usage(station.master_dir()).free - length < cfg.min_free_gb * 1e9:
            raise HTTPException(507, i18n.msg("api.master_no_space", gb=length / 1e9))
        tmp = station.master_dir() / f".upload-{rid}-{side}-{int(time.time() * 1000)}"
        size = 0
        try:
            with open(tmp, "wb") as f:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > MASTER_MAX_BYTES:
                        raise HTTPException(413, i18n.msg("api.master_too_large"))
                    f.write(chunk)
            try:
                info = sf.info(str(tmp))
            except Exception as e:  # noqa: BLE001 — anything soundfile cannot read
                raise HTTPException(415, i18n.msg("api.master_unreadable")) from e
            ext = MASTER_FORMATS.get(info.format)
            if not ext:
                raise HTTPException(415, i18n.msg("api.master_format", format=info.format))
            if info.duration < 30:
                raise HTTPException(422, i18n.msg("api.master_too_short", s=info.duration))
            final = station.master_dir() / f"{rid}-{side}.{ext}"
            old = db.master(rid, side)
            tmp.replace(final)
            if old and Path(old["path"]).resolve() != final.resolve():
                station.remove_master_file(old)
        finally:
            tmp.unlink(missing_ok=True)
        mid = db.set_master(rid, side, name=(name or final.name)[:200], path=str(final), samplerate=info.samplerate,
                            channels=info.channels, subtype=info.subtype, duration_s=round(info.duration, 2), bytes=size)
        queued = []
        if cfg.engineer_mode:
            for r in db.recordings(rid):
                if (str(r.get("side") or "").strip().upper()[:1] == side and r.get("kind") in MASTER_KINDS
                        and r.get("status") == "analyzed" and r.get("path") and not r.get("audio_deleted_at")):
                    worker.master_check(r["id"])
                    queued.append(r["id"])
        log.info("Master for record %s side %s: %s (%.0f s, %d Hz, %s), %d recording(s) queued", rid, side, name,
                 info.duration, info.samplerate, info.subtype, len(queued))
        return station.master_info(db.master_by_id(mid)) | {"queued": queued}

    @app.delete("/api/records/{rid}/masters/{side}")
    def delete_master(rid: int, side: str):
        m = db.master(rid, side)
        if not m:
            raise HTTPException(404, i18n.msg("api.no_master_for_side"))
        station.remove_master_file(m)
        db.delete_master(m["id"])                          # results stay, their master_id becomes empty
        return {"ok": True}

    @app.get("/api/recordings/{rec_id}/master")
    def recording_master(rec_id: int):
        r = db.recording(rec_id)
        if not r:
            raise HTTPException(404, i18n.msg("api.recording_not_found"))
        m = db.master(r["record_id"], r["side"]) if r.get("record_id") else None
        c = db.master_check(rec_id)
        return {"master": station.master_info(m), "check": c, "engineer_mode": cfg.engineer_mode,
                "grading_master": master_grade(rec_id, (c or {}).get("result") or {}), "loud": ear_grade(rec_id)}

    @app.post("/api/recordings/{rec_id}/master")
    def run_master_check(rec_id: int):
        r = db.recording(rec_id)
        if not r:
            raise HTTPException(404, i18n.msg("api.recording_not_found"))
        if not (r.get("record_id") and db.master(r["record_id"], r["side"])):
            raise HTTPException(409, i18n.msg("api.no_master_upload", side=r["side"]))
        if r.get("audio_deleted_at") or not r.get("path"):
            raise HTTPException(410, i18n.msg("api.audio_deleted"))
        if r.get("status") != "analyzed":
            raise HTTPException(409, i18n.msg("api.analyze_first"))
        worker.master_check(rec_id)
        return {"queued": True}

    @app.get("/api/records/{rid}/master/common")
    def master_common(rid: int, side: str = "A", tol_ms: float = 3.0, audible: float = AUDIBLE_STRENGTH):
        """Copies of one side against each other (engineer mode): the genuine events in the music (according to the
        master, without those judged false alarms) of every copy, mapped onto the master's time axis. What sits at the
        same spot (±3 ms) on several copies is in the stamper or the cut (or is tracing distortion at the same spot in
        the music); the rest belongs to the copy. Per copy the latest recording with a master comparison counts. On
        real test pressings two copies shared about a hundred spots where chance would predict less than one."""
        if not db.record(rid):
            raise HTTPException(404, i18n.msg("api.record_not_found"))
        side = side.strip().upper()[:1]
        tol = tol_ms / 1000
        latest: dict = {}
        for r in db.recordings(rid):
            if str(r.get("side") or "").strip().upper()[:1] != side or r.get("kind") not in MASTER_KINDS:
                continue
            c = db.master_check(r["id"])
            res = (c or {}).get("result") or {}
            if not res.get("ok"):
                continue
            key = " ".join((r.get("copy_label") or f"#{r['id']}").split()).lower()
            if key not in latest or r["created_at"] > latest[key][0]["created_at"]:
                latest[key] = (r, res)
        copies, allev = [], []
        for r, res in sorted(latest.values(), key=lambda x: x[0].get("copy_label") or ""):
            mp = res["map"]
            sl, ic = mp["slope"], mp["intercept"]
            v2m = compare._mapper(mp["v"], mp["m"], 1 / sl, -ic / sl)
            src = {round(x[0], 4): x[1] for x in (res.get("clicks") or {}).get("events") or []}
            name = r.get("copy_label") or f"#{r['id']}"
            evs = [{"copy": name, "recording_id": r["id"], "t": e["t"], "tm": round(float(v2m(e["t"])), 5), "kind": e["kind"],
                    "ch": e["ch"], "strength": e["strength"], "amp_dbfs": e["amp_dbfs"]}
                   for e in db.events(r["id"]) if e["region"] == "music" and e.get("label") != "music"
                   and src.get(round(e["t"], 4)) == "v"]
            gm = master_grade(r["id"], res) or {}
            copies.append({"copy": name, "recording_id": r["id"], "grade": r.get("grade"), "grade_master": gm.get("grade"),
                           "events": len(evs), "audible": sum(1 for e in evs if e["strength"] >= audible)})
            allev += evs
        allev.sort(key=lambda e: e["tm"])
        clusters, i = [], 0
        while i < len(allev):                              # spots: events of different copies within ±tol of the first
            grp, seen, j = [allev[i]], {allev[i]["copy"]}, i + 1
            while j < len(allev) and allev[j]["tm"] - allev[i]["tm"] <= 2 * tol:
                if allev[j]["copy"] not in seen:
                    grp.append(allev[j])
                    seen.add(allev[j]["copy"])
                j += 1
            if len(grp) >= 2:
                clusters.append(grp)
                i = j
            else:
                i += 1
        shared = {(e["copy"], e["t"]) for g in clusters for e in g}
        for c in copies:
            mine = [e for e in allev if e["copy"] == c["copy"]]
            c["shared"] = sum(1 for e in mine if (e["copy"], e["t"]) in shared)
            c["audible_own"] = sum(1 for e in mine if e["strength"] >= audible and (e["copy"], e["t"]) not in shared)
            c["audible_shared"] = sum(1 for e in mine if e["strength"] >= audible and (e["copy"], e["t"]) in shared)
        dur = max((m["duration_s"] for m in db.masters(rid) if m["side"] == side), default=1200.0)
        n = [c["events"] for c in copies]
        expected = sum(n[a] * n[b] for a in range(len(n)) for b in range(a + 1, len(n))) * 2 * tol / dur
        out = [{"tm": round(sum(e["tm"] for e in g) / len(g), 4), "n": len(g), "max_strength": max(e["strength"] for e in g),
                "kind": max(g, key=lambda e: e["strength"])["kind"], "ch": max(g, key=lambda e: e["strength"])["ch"],
                "copies": {e["copy"]: {"recording_id": e["recording_id"], "t": e["t"], "kind": e["kind"], "ch": e["ch"],
                                       "strength": e["strength"]} for e in g}} for g in clusters]
        out.sort(key=lambda c: (-c["n"], -c["max_strength"]))
        return {"side": side, "tol_ms": tol_ms, "audible": audible, "copies": copies, "common_count": len(out),
                "on_all": sum(1 for c in out if c["n"] == len(copies)) if len(copies) >= 2 else 0,
                "expected_random": round(expected, 1), "common": out[:300]}

    # Test pressing approval: per side with a master, all copies (latest recording with a master comparison) against
    # the master, check items with suggested thresholds and a recommendation. The thresholds are suggestions, not a
    # standard.
    # stamper_hint_max: up to this many audible spots on all copies are a note to the pressing plant; only above that
    # "do not approve". Calibrated on a mastering engineer's verdict on real test pressings: two small clicks on one
    # side, present on all three copies, were no reason for new test pressings, and the pressing was approved.
    APPROVAL = {"cut_ok": 0.3, "cut_warn": 0.6, "ecc_ok": 0.2, "ecc_warn": 0.35, "pre_echo_warn": -45.0, "hf_warn": -6.0,
                "stamper_hint_max": 3}

    @app.get("/api/records/{rid}/approval")
    def approval(rid: int):
        """Test pressing approval: per side with a master the check items (`key`, `name`, `status`, `text`), the copies
        and a verdict (`verdict` value, `verdict_text` for display), plus the overall verdict."""
        rec = db.record(rid)
        if not rec:
            raise HTTPException(404, i18n.msg("api.record_not_found"))
        med = lambda xs: (sorted(xs)[len(xs) // 2] if len(xs) % 2 else sum(sorted(xs)[len(xs) // 2 - 1:len(xs) // 2 + 1]) / 2) if xs else None
        check = lambda key, status, text, **kw: {"key": key, "name": i18n.msg(f"labels.approval_check.{key}"),
                                                 "status": status, "text": text, **kw}
        sides = []
        for m in db.masters(rid):
            com = master_common(rid, side=m["side"])
            copies = []
            for c in com["copies"]:
                res = (db.master_check(c["recording_id"]) or {}).get("result") or {}
                k, wo, fr, ga = res.get("clicks") or {}, res.get("wow") or {}, res.get("freq") or {}, res.get("gaps") or {}
                sp, ch = res.get("speed") or {}, res.get("channels") or {}
                copies.append(c | {"speed_pct": sp.get("vs_master_pct"), "cut_pct": sp.get("cut_pct"), "ecc_mm": wo.get("ecc_mm"),
                                   "wow_pct_max": wo.get("wow_pct_max"), "hf_loss_end_db": fr.get("hf_loss_end_db"),
                                   "bass_side_db": fr.get("bass_side_db"), "polarity": res.get("polarity"), "swapped": bool(ch.get("swapped")),
                                   "pre_echo_db": ga.get("pre_echo_max_db"), "noise_a_dbfs": ga.get("noise_a_median_dbfs"),
                                   "clicks_vinyl": k.get("vinyl_music_per_min"), "pops_vinyl": k.get("vinyl_pops_music_per_min")})
            n = len(copies)
            checks = []
            loud = [x for x in com["common"] if x["max_strength"] >= AUDIBLE_STRENGTH and x["n"] >= 2]
            if not n:
                checks.append(check("copies", "bad", i18n.msg("api.approval_no_copies")))
            else:
                on_all = [x for x in loud if x["n"] == n and n >= 2]
                many = len(on_all) > APPROVAL["stamper_hint_max"]
                if loud:
                    times = ", ".join(f"{int(x['tm'] // 60)}:{x['tm'] % 60:04.1f}" for x in loud[:8])
                    spots = (i18n.msg("api.approval_stamper_spots_all", n=len(loud), on_all=len(on_all), copies=n,
                                      times=times) if on_all
                             else i18n.msg("api.approval_stamper_spots", n=len(loud), times=times))
                    advice = (i18n.msg("api.approval_stamper_rework", max=APPROVAL["stamper_hint_max"]) if many
                              else i18n.msg("api.approval_stamper_report"))
                    text = i18n.msg_join([spots, advice])
                else:
                    text = i18n.msg("api.approval_stamper_none")
                checks.append(check("stamper", "bad" if many else "warn" if loud else "ok", text,
                                    detail=i18n.msg("api.approval_stamper_detail", common=com["common_count"],
                                                    on_all=com["on_all"], expected=com["expected_random"])))
                cut = [x["cut_pct"] if x["cut_pct"] is not None else x["speed_pct"] for x in copies if (x["cut_pct"] if x["cut_pct"] is not None else x["speed_pct"]) is not None]
                if cut:
                    mc = med(cut)
                    st = "ok" if abs(mc) <= APPROVAL["cut_ok"] else "warn" if abs(mc) <= APPROVAL["cut_warn"] else "bad"
                    checks.append(check("cut_speed", st,
                                        i18n.msg("api.approval_cut_speed", pct=mc, limit=APPROVAL["cut_ok"])))
                ecc = [x["ecc_mm"] for x in copies if x["ecc_mm"] is not None]
                if ecc:
                    me = med(ecc)
                    st = "ok" if me <= APPROVAL["ecc_ok"] else "warn" if me <= APPROVAL["ecc_warn"] else "bad"
                    checks.append(check("center_hole", st, i18n.msg(
                        "api.approval_center_hole", mm=me, limit=APPROVAL["ecc_ok"],
                        values=", ".join(i18n.num(x, 2) for x in ecc))))
                gm = [grading.GRADES.index(x["grade_master"]) for x in copies if x.get("grade_master") in grading.GRADES]
                if gm:
                    mg = grading.GRADES[int(med(gm) + 0.5)]
                    bad_copies = [x["copy"] for x in copies if x.get("grade_master") in grading.GRADES
                                  and grading.GRADES.index(x["grade_master"]) >= int(med(gm) + 0.5) + 2]
                    text = (i18n.msg("api.approval_vs_master_outliers", grade=mg, n=len(gm),
                                     outliers=", ".join(bad_copies)) if bad_copies
                            else i18n.msg("api.approval_vs_master", grade=mg, n=len(gm)))
                    checks.append(check("vs_master", "ok" if mg in ("NM", "VG+") else "warn" if mg == "VG" else "bad",
                                        text))
                pe = [x["pre_echo_db"] for x in copies if x["pre_echo_db"] is not None]
                if pe:
                    checks.append(check("pre_echo", "warn" if max(pe) >= APPROVAL["pre_echo_warn"] else "ok",
                                        i18n.msg("api.approval_pre_echo", db=max(pe), limit=APPROVAL["pre_echo_warn"])))
                else:
                    checks.append(check("pre_echo", "ok", i18n.msg("api.approval_pre_echo_none")))
                hf = [x["hf_loss_end_db"] for x in copies if x["hf_loss_end_db"] is not None]
                if hf:
                    checks.append(check("hf_end", "warn" if med(hf) <= APPROVAL["hf_warn"] else "info",
                                        i18n.msg("api.approval_hf_end", db=med(hf))))
                pols = {x["polarity"] for x in copies}
                swapped = [x["copy"] for x in copies if x["swapped"]]
                pol_text = (i18n.msg("api.approval_polarity_inverted") if pols == {"inverted"}
                            else i18n.msg("api.approval_polarity_ok") if pols == {"ok"}
                            else i18n.msg("api.approval_polarity_mixed", values=i18n.msg_join(
                                [POLARITY_LABEL.get(p, p) for p in sorted(p or "–" for p in pols)], ", ")))
                swap_text = i18n.msg("api.approval_channels_swapped", copies=", ".join(swapped)) if swapped else None
                checks.append(check("polarity", "warn" if swapped else "info", i18n.msg_join([swap_text, pol_text])))
            stats = [c["status"] for c in checks]
            verdict = REJECT if "bad" in stats else APPROVE_NOTES if "warn" in stats else APPROVE
            sides.append({"side": m["side"], "master": station.master_info(m), "copies": copies, "checks": checks, "verdict": verdict,
                          "verdict_text": VERDICT_TEXT[verdict],
                          "loud_common": loud[:20], "common_count": com["common_count"], "on_all": com["on_all"]})
        order = [APPROVE, APPROVE_NOTES, REJECT]
        overall = max((s["verdict"] for s in sides), key=order.index) if sides else None
        return {"record": {"id": rid, "artist": rec["artist"], "title": rec["title"]}, "sides": sides, "verdict": overall,
                "verdict_text": VERDICT_TEXT.get(overall),
                "criteria": APPROVAL, "audible": AUDIBLE_STRENGTH, "created_at": time.time(), "version": __version__}

    def approval_report_data(rid: int) -> dict:
        """Everything for the approval PDF: the approval, shared spots, per copy the master comparison and the genuine
        clicks (master time), the spectrogram of the side (first copy) and close-ups of the audible stamper spots."""
        d = approval(rid)
        for sd in d["sides"]:
            ncop = len(sd["copies"])
            com = master_common(rid, side=sd["side"])
            sd["common"] = [x for x in com["common"] if x["n"] >= 2]
            det = []
            for c in sd["copies"]:
                res = (db.master_check(c["recording_id"]) or {}).get("result") or {}
                mp = res.get("map") or {}
                v, m_ = np.array(mp.get("v") or [0.0, 1.0], float), np.array(mp.get("m") or [0.0, 1.0], float)
                ov, om = np.argsort(v), np.argsort(m_)
                to_m = lambda t, v=v[ov], m=m_[ov]: float(np.interp(t, v, m))
                to_v = lambda t, v=v[om], m=m_[om]: float(np.interp(t, m, v))
                evs = {round(e["t"], 3): e for e in db.events(c["recording_id"])}
                vinyl = []
                for x in ((res.get("clicks") or {}).get("events") or []):
                    if len(x) >= 2 and x[1] == "v":
                        e = evs.get(round(float(x[0]), 3)) or {}
                        vinyl.append({"tm": to_m(float(x[0])), "kind": e.get("kind") or "click", "strength": e.get("strength") or 0.0})
                det.append({"copy": c["copy"], "recording_id": c["recording_id"], "vinyl": vinyl, "freq": res.get("freq"),
                            "wow": res.get("wow"), "levels": res.get("levels"), "to_v": to_v, "music_v": res.get("music_v")})
            sd["copies_detail"] = det
            loud = [x for x in sd["common"] if x["max_strength"] >= AUDIBLE_STRENGTH]
            # spectrogram of the whole side: first copy, precomputed in the database, cropped to the music
            sd["spectrogram"] = None
            if det:
                c0 = det[0]
                stored = db.spectrogram(c0["recording_id"])
                if stored:
                    meta, raw = stored
                    img = spectro.rescale(np.frombuffer(raw, np.uint8).reshape(meta["rows"], meta["cols"]), meta["top_db"], meta["top_db"])
                    t0, t1 = float(meta["t0"]), float(meta["t1"])
                    mv = c0.get("music_v") or [t0, t1]
                    a, b = max(t0, float(mv[0]) - 3), min(t1, float(mv[1]) + 3)
                    i0 = int((a - t0) / (t1 - t0) * meta["cols"])
                    i1 = max(i0 + 2, int(np.ceil((b - t0) / (t1 - t0) * meta["cols"])))
                    sd["spectrogram"] = {"copy": c0["copy"], "img": img[:, i0:i1], "t0": t0 + i0 / meta["cols"] * (t1 - t0),
                                         "t1": t0 + i1 / meta["cols"] * (t1 - t0), "fmin": meta["fmin"], "fmax": meta["fmax"],
                                         "markers": [{"t": (x.get("copies") or {}).get(c0["copy"], {}).get("t") or c0["to_v"](x["tm"]),
                                                      "status": "bad" if x["n"] >= ncop else "warn",
                                                      "label": report_pdf.fmt_time(x["tm"])}
                                                     for x in loud]}
            # close-ups of ±0.25 s per audible spot and copy, computed from the audio
            zooms = []
            for x in sorted(loud, key=lambda x: x["tm"])[:6]:
                cells = []
                for c in det:
                    r = db.recording(c["recording_id"])
                    if not r or r.get("audio_deleted_at") or not r.get("path") or not Path(r["path"]).exists():
                        continue
                    t = (x.get("copies") or {}).get(c["copy"], {}).get("t") or c["to_v"](x["tm"])
                    z = spectro.compute(r["path"], cols=300, rows=140, t0=max(0.0, t - 0.25), t1=t + 0.25)
                    cells.append({"copy": c["copy"], "img": spectro.rescale(z["img"], z["top_db"], z["top_db"]), "t0": z["t0"],
                                  "t1": z["t1"], "fmin": z["fmin"], "fmax": z["fmax"], "t": t})
                if cells:
                    zooms.append({"tm": x["tm"], "kind": x["kind"], "n": x["n"], "max_strength": x["max_strength"], "cells": cells})
            sd["zooms"] = zooms
        return d

    def recording_report_data(rec_id: int) -> dict:
        """Everything for the PDF report of one recording (texts are i18n messages, rendered by recording_pdf)."""
        r = station.get_recording(rec_id)
        a = r.get("analysis") or {}
        if not a.get("metrics"):
            raise HTTPException(409, i18n.msg("api.not_analyzed_yet"))
        m, g = a["metrics"], a.get("grading") or {}
        rec = db.record(r["record_id"]) if r.get("record_id") else None
        prof = db.profile(r["profile_id"]) if r.get("profile_id") else None
        ov = db.overview(rec_id) or {}
        evs = db.events(rec_id)
        ov["events"] = [[e["t"], e["kind"], e["region"], e["strength"], e.get("label"), e.get("gc")] for e in evs]
        model = station.learn_model()
        _, lp = learning.estimate(evs, model, m.get("music_ref_dbfs"), station.learn_zp(r)) if model else (None, None)
        top = sorted((i for i in range(len(evs)) if evs[i].get("region") != "excluded"), key=lambda i: -(evs[i].get("strength") or 0))[:30]
        top_events = [evs[i] | {"lp": None if lp is None or lp[i] != lp[i] else round(float(lp[i]), 3)} for i in sorted(top, key=lambda i: evs[i]["t"])]
        sg = None
        stored = db.spectrogram(rec_id)
        if stored:
            meta, raw = stored
            img = spectro.rescale(np.frombuffer(raw, np.uint8).reshape(meta["rows"], meta["cols"]), meta["top_db"], meta["top_db"])
            marks = [{"t": x["t"], "status": "bad", "label": i18n.msg("labels.skip_back")}
                     for x in m.get("skips") or [] if x.get("kind") == "backward" and not x.get("label")]
            sg = {"img": img, "t0": meta["t0"], "t1": meta["t1"], "fmin": meta["fmin"], "fmax": meta["fmax"], "markers": marks}
        t = m.get("technical") or {}
        fnum = report_pdf.fmt_num
        lr = lambda x, u="", d=1: "–" if not x else f"L {fnum(x[0], d)}{u} · R {fnum(x[1], d)}{u}"
        lab = lambda key: i18n.msg(f"labels.tech.{key}")
        counts = m.get("counts") or {}
        tech = [(lab("peak"), lr(t.get("peak_dbfs"), " dBFS")),
                (lab("true_peak"), lr(t.get("true_peak_dbtp"), " dBTP")),
                (lab("rms"), lr(t.get("rms_dbfs"), " dBFS")), (lab("crest"), lr(t.get("crest_db"), " dB")),
                (lab("loudness"), report_pdf.fmt_num(t.get("loudness_lufs"), 1, unit="LUFS")),
                (lab("correlation"), report_pdf.fmt_num(t.get("correlation"), 2)),
                (lab("stereo_width"), report_pdf.fmt_num(t.get("stereo_width_db"), 1, sign=True, unit="dB")),
                (lab("rumble"), report_pdf.fmt_num(t.get("rumble_dbfs"), 1, unit="dBFS")),
                (lab("hum"), report_pdf.fmt_num(t.get("hum_dbfs"), 1, unit="dBFS")),
                (lab("noise"), i18n.msg("api.report_noise", noise=report_pdf.fmt_num(m.get("noise_dbfs"), 1),
                                        noise_a=report_pdf.fmt_num(m.get("noise_a_dbfs"), 1),
                                        rel=report_pdf.fmt_num(m.get("noise_rel_db"), 1))),
                (lab("chain_noise"), i18n.msg("api.report_not_calibrated") if m.get("chain_db") is None
                 else f'{report_pdf.fmt_num(m.get("chain_db"), 1)} dBFS'),
                (lab("clipping"), i18n.msg("api.report_clip_in_groove", n=m.get("clip_in_groove"))
                 if m.get("clip_in_groove") else i18n.msg("api.report_no_clip")),
                (lab("level_vs_profile"), report_pdf.fmt_num(m.get("level_vs_profile_db"), 1, sign=True, unit="dB")),
                (lab("events"), i18n.msg("api.report_events", pops=counts.get("pop", 0), clicks=counts.get("click", 0),
                                         ticks=counts.get("tick", 0))),
                (lab("recording"), i18n.msg("api.report_recording",
                                            duration=report_pdf.fmt_min(r.get("duration_s") or 0),
                                            groove=report_pdf.fmt_min(m.get("groove_s") or 0),
                                            quiet=report_pdf.fmt_num(m.get("quiet_s"), 0), rate=r.get("samplerate"),
                                            device=r.get("device") or "–"))]
        tech = [(k, v) for k, v in tech if v not in ("–", "– dBFS", "– dB", "– LUFS")]
        master = None
        mc = db.master_check(rec_id) if cfg.engineer_mode else None
        res = (mc or {}).get("result") or {}
        if res.get("ok"):
            mg = master_grade(rec_id, res) or {}
            mm = db.master_by_id(mc["master_id"]) if mc.get("master_id") else None
            k = res.get("clicks") or {}
            master = {"grade": mg.get("grade"), "clicks": k.get("vinyl_music_per_min"), "pops": k.get("vinyl_pops_music_per_min"),
                      "cut_pct": (res.get("speed") or {}).get("cut_pct"), "ecc_mm": (res.get("wow") or {}).get("ecc_mm"),
                      "polarity": res.get("polarity"),
                      "name": (mm or {}).get("name", "")}
        fnd = i18n.msg_join([FINDINGS_LABEL.get(f, f) for f in r["findings"]], ", ") if r.get("findings") else ""
        return {"record": rec or {}, "recording": r, "metrics": m, "grading": g, "warnings": a.get("warnings") or [],
                "kind_label": KIND_LABEL.get(r["kind"], r["kind"]), "profile": (prof or {}).get("name"), "overview": ov,
                "spectrogram": sg, "top_events": top_events, "tech": tech, "master": master, "findings": fnd, "version": __version__}

    @app.get("/api/recordings/{rec_id}/report.pdf")
    def recording_pdf(rec_id: int):
        """PDF report of one recording, in the language of the request."""
        data = recording_report_data(rec_id)
        pdf = report_pdf.build_recording(_localize(data))
        rec, r = data["record"], data["recording"]
        head = f'{rec.get("artist", "")} {rec.get("title", "")}'.strip() or i18n.t("api.filename_recording")
        side, copy = r.get("side", ""), f', {r["copy_label"]}' if r.get("copy_label") else ""
        base = i18n.t("api.filename_recording_base", head=head, side=side, id=r["id"])
        base = re.sub(r"[^A-Za-z0-9]+", "_", base).strip("_")[:90]
        from urllib.parse import quote
        full = i18n.t("api.filename_recording_report", head=head, side=side, kind=KIND_LABEL.get(r["kind"], r["kind"]),
                      copy=copy, id=r["id"]) + ".pdf"
        return Response(pdf, media_type="application/pdf",
                        headers={"Content-Disposition": f"attachment; filename=\"{base}.pdf\"; filename*=UTF-8''{quote(full)}",
                                 "Cache-Control": "no-store"})

    @app.get("/api/records/{rid}/approval.pdf")
    def approval_pdf(rid: int):
        """Test pressing approval as a PDF report (title page, per side the check items, copies and charts, appendix),
        in the language of the request."""
        rec = db.record(rid)
        if not rec:
            raise HTTPException(404, i18n.msg("api.record_not_found"))
        if not db.masters(rid):
            raise HTTPException(409, i18n.msg("api.no_master_for_record"))
        pdf = report_pdf.build(_localize(approval_report_data(rid)))
        base = i18n.t("api.filename_approval_base", artist=rec["artist"], title=rec["title"])
        base = re.sub(r"[^A-Za-z0-9]+", "_", base).strip("_")[:80]
        name = f'{base}_{time.strftime("%Y-%m-%d")}.pdf'
        from urllib.parse import quote
        full = i18n.t("api.filename_approval", artist=rec["artist"], title=rec["title"],
                      date=time.strftime("%Y-%m-%d")) + ".pdf"
        return Response(pdf, media_type="application/pdf",
                        headers={"Content-Disposition": f"attachment; filename=\"{name}\"; filename*=UTF-8''{quote(full)}",
                                 "Cache-Control": "no-store"})

    @app.get("/api/recordings/{rec_id}/master/snippet")
    def master_snippet(rec_id: int, t: float, before: float = 0.5, after: float = 1.0):
        """The same spot from the master (A/B listening): recording time t mapped via the master comparison."""
        c = db.master_check(rec_id)
        res = (c or {}).get("result") or {}
        m = db.master_by_id(c["master_id"]) if c and c.get("master_id") else None
        if not res.get("ok") or not m or not Path(m["path"]).exists():
            raise HTTPException(404, i18n.msg("api.no_master_map"))
        mp = res["map"]
        sl, ic = mp["slope"], mp["intercept"]
        tm = float(compare._mapper(mp["v"], mp["m"], 1 / sl, -ic / sl)(t))
        with sf.SoundFile(m["path"]) as f:
            msr = f.samplerate
            start = max(0, int((tm - min(before, 5)) * msr))
            f.seek(min(start, f.frames))
            data = f.read(int((min(before, 5) + min(after, 60)) * msr), dtype="int16")
        buf = io.BytesIO()
        sf.write(buf, data, msr, format="WAV", subtype="PCM_16")
        return Response(buf.getvalue(), media_type="audio/wav")
