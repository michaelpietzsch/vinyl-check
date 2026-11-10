"""Records: the list, a record with its copies and grades, its place and condition in the Discogs collection, cover,
tracks, and writing the measured condition back.

Part of the API of `vinylcheck.web`: `install` adds the routes to the app. What the areas share lives in the
`Station` (`vinylcheck/station.py`).
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response

from .. import __version__, analysis, discogs, grading, i18n, tls
from ..db import copy_key
from .common import (CHECK_KINDS, ConditionIn, GRADE_TO_DISCOGS, RecordIn, RecordPatch, SIDE_SOURCE_DISCOGS,
                     SIDE_SOURCE_LABEL, SIDE_SOURCE_MEASURED, SIDE_SOURCE_TRACKS, TrackIn)

if TYPE_CHECKING:
    from ..station import Station


def install(app: FastAPI, station: Station) -> None:
    cfg, db, worker, dc, ref_cache = station.cfg, station.db, station.worker, station.dc, station.ref_cache

    def clean_tracks(raw: dict[str, list[TrackIn]]) -> dict:
        """Bring a hand-entered track list into the shape of discogs.tracks_by_side: side = one letter, missing
        position = side + running number, duration > 0 or unknown."""
        out: dict[str, list[dict]] = {}
        for side, lst in raw.items():
            s = str(side).strip().upper()
            if not re.fullmatch(r"[A-Z]", s):
                raise HTTPException(422, i18n.msg("api.track_side_invalid", side=str(side)))
            out[s] = [{"pos": (t.pos.strip().upper() or f"{s}{i + 1}")[:8], "title": t.title.strip()[:200],
                       "dur_s": round(t.dur_s, 2) if t.dur_s and t.dur_s > 0 else None} for i, t in enumerate(lst)]
        return out

    # ─── Records ────────────────────────────────────────────────────────────
    # Custom cover for records that are not (yet) on Discogs, e.g. test pressings: an image in the state directory
    # (covers/<id>.<ext>) that takes precedence over the Discogs cover. The browser downsizes it before uploading.
    cover_dir = cfg.state_dir / "covers"
    COVER_TYPES = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}

    def local_cover(rid: int) -> Path | None:
        return next((p for ext in COVER_TYPES if (p := cover_dir / f"{rid}.{ext}").is_file()), None)

    def remove_local_cover(rid: int) -> None:
        for ext in COVER_TYPES:
            (cover_dir / f"{rid}.{ext}").unlink(missing_ok=True)

    def pressing_grade(copies: list[dict]) -> dict | None:
        """Quality of the pressing across all copies: per copy and side the latest graded recording (the grades of the
        recordings list); from 4 sides on, the best and the worst are dropped as outliers and the rest is averaged in
        grade steps (half steps round to the worse grade, F stays F)."""
        sides = [(i18n.msg_join([c.get("copy") or i18n.msg("labels.no_copy"), sd["side"]]), sd)
                 for c in copies for sd in (c.get("sides") or []) if sd.get("grade")]
        if not sides:
            return None

        def trimmed(items):
            items = sorted(items, key=lambda x: grading.GRADES.index(x[1]))
            dropped = [items[0], items[-1]] if len(items) >= 4 else []
            kept = items[1:-1] if dropped else items
            st = sum(grading.GRADES.index(g) for _, g in kept) / len(kept)
            g = "F" if any(g == "F" for _, g in kept) else grading.GRADES[min(len(grading.GRADES) - 1, int(st + 0.5))]
            return {"grade": g, "steps": round(st, 2), "counted": len(kept),
                    "dropped": [{"side": lab, "grade": gr} for lab, gr in dropped]}

        out = {"sides": len(sides), "copies": len([c for c in copies if c.get("sides")])}
        out |= trimmed([(lab, sd["grade"]) for lab, sd in sides])
        return out

    @app.get("/api/records")
    def records():
        rows = db.records()
        for r in rows:
            r["cover_local"] = local_cover(r["id"]) is not None
        return rows

    @app.post("/api/records")
    def add_record(r: RecordIn):
        body = r.model_dump()
        e = (dc.entry(r.discogs_release_id, r.artist, r.title) or {}) if r.discogs_release_id and dc.enabled else {}
        if body["mono"] is None and e:
            # Discogs marks mono pressings in the format ("LP, Album, Mono"): such a record starts as mono
            body["mono"] = True if "mono" in str(e.get("format") or "").lower() else None
        if "genres" in e:                             # the default of the repeat rule (sequenced music)
            body["genres"] = json.dumps({"genres": e["genres"], "styles": e.get("styles") or []})
        return {"id": db.add_record(**body)}

    @app.get("/api/records/{rid}")
    def get_record(rid: int):
        r = db.record(rid)
        if not r:
            raise HTTPException(404, i18n.msg("api.record_not_found"))
        station.ensure_genres(r)                                     # before the grades: they may be counted again
        r["repeat_rule_effective"] = analysis.record_repeat_rule(r, cfg.analysis.repeat_rule)
        r["recordings"] = db.recordings(rid)
        r["copies"] = db.copy_grades(rid).get(rid, [])              # record grade per copy (average of its sides)
        r["pressing"] = pressing_grade(r["copies"])
        r["findings"] = db.copy_findings(rid)                        # findings per copy and side
        r["comparisons"] = db.comparisons(rid)
        sides = station.ensure_sides(r)
        r["sides"] = sides
        r["tracks"] = station.ensure_tracks(r)
        r["cover_local"] = local_cover(rid) is not None
        r["masters"] = [station.master_info(m) for m in db.masters(rid)]
        r["master_states"] = db.master_check_states(rid)
        src = SIDE_SOURCE_DISCOGS if r.get("discogs_release_id") else SIDE_SOURCE_TRACKS
        r["side_lengths"] = {s: {"s": v, "source": src, "source_label": SIDE_SOURCE_LABEL[src]}
                             for s, v in sides.items()}
        for x in r["recordings"]:
            ml = (x.get("summary") or {}).get("music_len_s")
            if ml:
                r["side_lengths"][str(x["side"]).strip().upper()[:1]] = {
                    "s": ml, "source": SIDE_SOURCE_MEASURED, "source_label": SIDE_SOURCE_LABEL[SIDE_SOURCE_MEASURED]}
        return r

    # Media condition as stored in the Discogs collection (the client's cache). When a record is created it is only
    # stored as a snapshot; it is changed on Discogs. The sleeve condition is not of interest here.

    def instance_map(r: dict) -> dict:
        """Which copy in the Discogs collection each copy of the record is (copy key → instance id), as chosen when
        a condition was written."""
        try:
            m = json.loads(r.get("discogs_instances") or "{}")
        except ValueError:
            return {}
        return m if isinstance(m, dict) else {}

    def collection_copies(rel: int) -> list[dict]:
        return [e for e in dc.releases() if e.get("release_id") == int(rel)]

    @app.get("/api/records/{rid}/collection")
    def record_collection(rid: int, live: bool = False, copy: str | None = None):
        """The record in the Discogs collection: from the synced collection, or with `live` as Discogs has it now
        (one request; the cache follows). `copies` lists every copy of the release in the collection, `suggested`
        the one chosen before for the copy `copy`."""
        r = db.record(rid)
        if not r:
            raise HTTPException(404, i18n.msg("api.record_not_found"))
        if not dc.enabled:
            raise HTTPException(404, station.discogs_off())
        rel = r.get("discogs_release_id")
        if live and rel:
            try:
                copies = dc.instances(int(rel))
            except discogs.DiscogsError as e:
                raise HTTPException(502, i18n.error_message(e)) from e
            if not copies:
                raise HTTPException(404, i18n.msg("discogs.not_in_collection_now"))
        else:
            e = dc.entry(rel, r["artist"], r["title"])
            copies = collection_copies(e["release_id"]) if e else []
            if not copies:
                raise HTTPException(404, i18n.msg("api.not_in_collection"))
        mapped = instance_map(r).get(copy_key(copy)) if copy is not None else None
        ids = [c.get("instance_id") for c in copies]
        e = copies[ids.index(mapped)] if mapped in ids else copies[0]
        if len(copies) == 1 and e.get("media") and e.get("media") != r.get("discogs_media_condition"):
            db.update_record(rid, discogs_media_condition=e["media"])      # the snapshot in the record list follows
        return {"media_condition": e.get("media"), "in_collection_since": e.get("date_added"), "notes": e.get("notes") or "",
                "format": e.get("format"), "year": e.get("year"), "release_id": e.get("release_id"),
                "synced_at": dc.state["synced_at"], "live": bool(live and rel),
                "copies": [{"instance_id": c.get("instance_id"), "media": c.get("media"), "date_added": c.get("date_added"),
                            "notes": c.get("notes") or "", "folder_id": c.get("folder_id")} for c in copies],
                "suggested": mapped if mapped in ids else None}

    # Covers from Discogs: the release API returns image URLs without a token too. Each release is asked once; the
    # URLs are kept in covers.json in the state directory and the browser loads the image itself from i.discogs.com.
    covers_file = cfg.state_dir / "covers.json"
    try:
        covers: dict = json.loads(covers_file.read_text())
    except (OSError, ValueError):
        covers = {}

    @app.get("/api/records/{rid}/cover")
    def record_cover(rid: int):
        r = db.record(rid)
        if not r:
            raise HTTPException(404, i18n.msg("api.record_not_found"))
        lc = local_cover(rid)
        if lc:
            url = f"/api/records/{rid}/cover/image?v={int(lc.stat().st_mtime)}"
            return {"release_id": r.get("discogs_release_id"), "url": url, "thumb": url, "local": True}
        rel = r.get("discogs_release_id")
        if not rel:
            raise HTTPException(404, i18n.msg("api.no_release_id_short"))
        key = str(rel)
        if key not in covers:
            req = urllib.request.Request(f"https://api.discogs.com/releases/{int(rel)}",
                                         headers={"User-Agent": f"vinyl-check/{__version__}",
                                                  **({"Authorization": dc.auth_header()} if dc.enabled else {})})
            try:
                with urllib.request.urlopen(req, timeout=8, context=tls.context()) as resp:
                    data = json.loads(resp.read())
            except Exception as e:  # noqa: BLE001 — Discogs unreachable: no cover, nothing else fails
                raise HTTPException(502, i18n.msg("api.discogs_unreachable", error=str(e))) from e
            imgs = data.get("images") or []
            img = next((i for i in imgs if i.get("type") == "primary"), imgs[0] if imgs else None)
            covers[key] = {"url": (img or {}).get("uri") or data.get("thumb") or "",
                           "thumb": (img or {}).get("uri150") or data.get("thumb") or ""}
            try:
                covers_file.write_text(json.dumps(covers))
            except OSError:
                pass
        if not covers[key]["url"]:
            raise HTTPException(404, i18n.msg("api.discogs_no_image"))
        return {"release_id": rel, **covers[key]}

    @app.post("/api/records/{rid}/discogs-refresh")
    def refresh_from_discogs(rid: int):
        """Take the release data again from Discogs: tracks and side lengths (replacing a hand-entered track list),
        genres and styles, the cover. vinyl-check takes them once and keeps them; an entry corrected on Discogs
        (durations added, tracks fixed, another image) arrives this way. Sides whose tracks changed are analysed
        again (the track bounds are part of the analysis), a changed default of the repeat rule recounts them."""
        r = db.record(rid)
        if not r:
            raise HTTPException(404, i18n.msg("api.record_not_found"))
        rel = r.get("discogs_release_id")
        if not rel:
            raise HTTPException(409, i18n.msg("api.no_release_id"))
        try:
            tracks = dc.release_tracks(int(rel))
            g = dc.release_genres(int(rel), fresh=True)
        except discogs.DiscogsError as e:
            raise HTTPException(502, i18n.error_message(e)) from e
        try:
            old_tracks = json.loads(r.get("tracks") or "{}")
        except ValueError:
            old_tracks = {}
        rule_before = analysis.record_repeat_rule(r, cfg.analysis.repeat_rule)["mode"]
        db.update_record(rid, tracks=json.dumps(tracks), sides=json.dumps(station.side_lengths(tracks)),
                         genres=json.dumps(g))
        if covers.pop(str(rel), None) is not None:
            try:
                covers_file.write_text(json.dumps(covers))
            except OSError:
                pass
        changed = {"tracks": tracks != old_tracks, "rule": False, "reanalyzing": 0}
        r = db.record(rid)
        for rec in db.recordings(rid):
            x = station.get_recording(rec["id"])
            side = str(x.get("side") or "").strip().upper()[:1]
            if (changed["tracks"] and (tracks.get(side) != old_tracks.get(side)) and x["audio_available"]
                    and x["status"] in ("analyzed", "error") and x["kind"] in CHECK_KINDS):
                worker.analyze(rec["id"])
                changed["reanalyzing"] += 1
        if analysis.record_repeat_rule(r, cfg.analysis.repeat_rule)["mode"] != rule_before:
            changed["rule"] = True
            for rec in db.recordings(rid):
                station.recount(rec["id"])
        ref_cache["key"] = None
        return {**get_record(rid), "refreshed": changed}

    @app.get("/api/records/{rid}/cover/image", include_in_schema=False)
    def record_cover_image(rid: int):
        lc = local_cover(rid)
        if not lc:
            raise HTTPException(404, i18n.msg("api.no_own_cover"))
        # whole file, no range requests: at most 5 MB, and FileResponse's range handling had a weakness in older
        # Starlette releases (the Debian packages a service runs on)
        return Response(lc.read_bytes(), media_type=COVER_TYPES[lc.suffix[1:]],
                        headers={"Cache-Control": "public, max-age=31536000, immutable"})       # ?v= changes

    @app.put("/api/records/{rid}/cover")
    async def put_record_cover(rid: int, request: Request):
        """Upload a custom cover: raw bytes in the body (JPEG, PNG or WebP, at most 5 MB)."""
        if not db.record(rid):
            raise HTTPException(404, i18n.msg("api.record_not_found"))
        data = await request.body()
        if len(data) > 5 * 1024 * 1024:
            raise HTTPException(413, i18n.msg("api.image_too_large"))
        ext = ("jpg" if data[:3] == b"\xff\xd8\xff" else "png" if data[:8] == b"\x89PNG\r\n\x1a\n"
               else "webp" if data[:4] == b"RIFF" and data[8:12] == b"WEBP" else None)
        if not ext:
            raise HTTPException(415, i18n.msg("api.image_type"))
        cover_dir.mkdir(parents=True, exist_ok=True)
        tmp = cover_dir / f".{rid}.tmp"
        tmp.write_bytes(data)
        remove_local_cover(rid)
        tmp.replace(cover_dir / f"{rid}.{ext}")
        return {"ok": True, "type": COVER_TYPES[ext], "bytes": len(data)}

    @app.delete("/api/records/{rid}/cover")
    def delete_record_cover(rid: int):
        remove_local_cover(rid)
        return {"ok": True}

    @app.post("/api/records/{rid}/condition")
    def record_condition_to_collection(rid: int, body: ConditionIn):
        """Write the measured condition to the Discogs collection as media condition: the record grade of a copy
        (average of its sides, `copy`) or the grade of a single recording (`recording_id`)."""
        r = db.record(rid)
        if not r:
            raise HTTPException(404, i18n.msg("api.record_not_found"))
        if not dc.enabled:
            raise HTTPException(503, station.discogs_off())
        if not r.get("discogs_release_id"):
            raise HTTPException(409, i18n.msg("api.no_release_id"))
        if body.recording_id is not None:
            rec = db.recording(body.recording_id)
            if not rec or rec.get("record_id") != rid:
                raise HTTPException(404, i18n.msg("api.recording_not_of_record"))
            grade = ((rec.get("analysis") or {}).get("grading") or {}).get("grade")
            if grade not in GRADE_TO_DISCOGS:
                raise HTTPException(409, i18n.msg("api.recording_no_grade"))
        else:
            key = " ".join((body.copy or "").split()).lower()
            c = next((c for c in db.copy_grades(rid).get(rid, []) if " ".join(c["copy"].split()).lower() == key), None)
            if not c or c["grade"] not in GRADE_TO_DISCOGS:
                raise HTTPException(409, i18n.msg("api.copy_no_grade"))
            grade = c["grade"]
        key = copy_key(body.copy) if body.recording_id is None else copy_key((rec or {}).get("copy_label"))
        mapping = instance_map(r)
        inst = body.instance_id if body.instance_id is not None else mapping.get(key)
        rel = int(r["discogs_release_id"])
        try:
            try:
                out = dc.set_media_condition(rel, grade, inst)
            except discogs.DiscogsError as e:
                if body.instance_id is not None or inst is None or e.instances is None:
                    raise
                mapping.pop(key, None)                 # the copy chosen before is gone on Discogs: as if never chosen
                out = dc.set_media_condition(rel, grade, None)
        except discogs.DiscogsError as e:
            # several copies and none chosen (or the chosen one is gone): 409, the page asks which one
            raise HTTPException(409 if e.instances is not None else 502, i18n.error_message(e)) from e
        mapping[key] = out["instance_id"]
        multi = len(collection_copies(rel)) > 1
        db.update_record(rid, discogs_instances=json.dumps(mapping),
                         **({} if multi else {"discogs_media_condition": out["current"]}))  # snapshot; Discogs leads
        return {"ok": True, "grade": grade, "previous": out["previous"], "current": out["current"],
                "instance_id": out["instance_id"]}

    @app.patch("/api/records/{rid}")
    def patch_record(rid: int, p: RecordPatch):
        fields = {k: v for k, v in p.model_dump(exclude={"tracks"}).items() if v is not None}
        if p.tracks is not None:                       # a hand-entered track list replaces Discogs' (side lengths too)
            tracks = clean_tracks(p.tracks)
            fields.update(tracks=json.dumps(tracks), sides=json.dumps(station.side_lengths(tracks)))
        if p.repeat_rule is not None and p.repeat_rule not in analysis.REPEAT_RULES:
            raise HTTPException(422, i18n.msg("api.repeat_rule_invalid", allowed=", ".join(analysis.REPEAT_RULES)))
        before = db.record(rid) or {}
        if p.mono is not None:
            fields["mono"] = int(p.mono)
        db.update_record(rid, **fields)
        if p.mono is not None and p.mono != bool(before.get("mono")):
            # mono changes what the detector sees (L+R instead of two channels): every side with its audio still
            # there is analysed again; sides without audio keep their stereo analysis
            for rec in db.recordings(rid):
                r = station.get_recording(rec["id"])
                if r["audio_available"] and r["status"] in ("analyzed", "error") and r["kind"] in CHECK_KINDS:
                    worker.analyze(rec["id"])
            ref_cache["key"] = None
        elif p.repeat_rule is not None and (analysis.record_repeat_rule({**before, "repeat_rule": p.repeat_rule})["mode"]
                                            != analysis.record_repeat_rule(before, cfg.analysis.repeat_rule)["mode"]):
            # the rule changes what counts: every analysed side of the record is counted and graded again
            for rec in db.recordings(rid):
                station.recount(rec["id"])
            ref_cache["key"] = None
        return get_record(rid)

    @app.delete("/api/records/{rid}")
    def delete_record(rid: int):
        for rec in db.recordings(rid):
            station.remove_audio(rec.get("path"))
        for m in db.masters(rid):
            station.remove_master_file(m)
        db.delete_record(rid)
        remove_local_cover(rid)
        return {"ok": True}
