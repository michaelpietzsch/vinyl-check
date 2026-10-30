"""First steps: the guided first alignment of a new station, and when a turntable is good enough to measure records.

A new installation (no turntable profile accepted yet) offers a tutorial once: choose the test record (from the
catalogue in testrecords.py, or describe another one), create the turntable profile, measure the chain noise, record
the sides, then judge the result. The tutorial leads through the existing profile wizard; it only adds the choice of
the record before it and the verdict after it. It can be skipped at any step, and *Don't ask again* keeps it from
coming back (setting `onboarding`, shared by every device that opens the station). Accepting a profile ends it.

The verdict (``readiness``) sorts the findings of a run by how much they matter for measuring records:

- **must**: input level (clipping, the pop threshold), anti-skating and tracking reserve (a mistracking stylus
  distorts and crackles like a worn record), speed (skip detection, pitch);
- **should**: channel balance, crosstalk and azimuth, wow & flutter, arm resonance, VTA: they change the sound and
  the comparison between turntables, but not what the record check counts;
- everything else (frequency response, which is mostly the phono stage, rumble, phase, the click floor) is a note.

Verdicts (stored values): ``ready`` (nothing to do), ``ready_notes`` (good enough, some figures could be better),
``adjust`` (a must or should figure is out of range: adjust first, or accept anyway), ``remeasure`` (a verified
record should have yielded a must figure but did not: record that side again, or accept anyway), ``incomplete`` (no
chain noise or no reference level yet: cannot be accepted). Each finding that is not ok names the guide that explains the
adjustment; the guides themselves (title, why, steps, how to check) are texts of the web catalog ``guides``.

Server catalog: ``onboarding``.
"""

from __future__ import annotations

import json
import time

from . import alignment, testrecords
from .i18n import msg

S_STATE = "onboarding"         # settings key: {"state": "skipped" | "done", "at": time}; absent = open

MUST = ("ref_level", "antiskating", "tracking_reserve_db", "speed_pct")
SHOULD = ("balance_db", "azimuth_asym_db", "crosstalk_lr_db", "crosstalk_rl_db", "wf_weighted_pct", "resonance_hz",
          "imd_pct")

# Finding → guide (ids of the web catalog `guides`)
GUIDE_FOR = {
    "ref_level": "input_level", "antiskating": "antiskating", "tracking_reserve_db": "tracking_force",
    "balance_db": "balance", "mono_cancel_db": "balance", "crosstalk_lr_db": "azimuth", "crosstalk_rl_db": "azimuth",
    "azimuth_asym_db": "azimuth", "speed_pct": "speed", "wf_weighted_pct": "wow_flutter",
    "resonance_hz": "resonance", "imd_pct": "vta", "hf_10k_db": "loading", "sweep_hf_dev_db": "loading",
    "sweep_lf_dev_db": "phono_eq", "lf_100_db": "phono_eq", "rumble_db": "rumble",
    "floor_clicks_per_min": "stylus_care",
}
# Check a test record cannot make → the guide for setting it without a measurement
GUIDE_FOR_CHECK = {"reference": "input_level", "azimuth": "azimuth", "speed": "speed", "tracking": "antiskating",
                   "resonance": "resonance", "vta": "vta"}
# Every guide, in the order of a basic set-up (the web catalog has title, why, s1…sN and check for each)
GUIDES = ("level_platter", "overhang", "tracking_force", "antiskating", "vta", "azimuth", "input_level", "speed",
          "wow_flutter", "balance", "resonance", "loading", "phono_eq", "rumble", "stylus_care")
BASICS = ("level_platter", "overhang", "tracking_force", "antiskating")     # before the first measurement


def _label(key: str):
    if key in alignment.TARGET_FIELDS:
        return alignment.TARGET_FIELDS[key][0]
    return msg("alignment.result.antiskating.label" if key == "antiskating" else "alignment.result.click_floor.label")


def _level(key: str, fixed_gain: bool) -> str:
    if key == "ref_level" and fixed_gain:
        return "info"                          # a USB turntable's level cannot be changed: a note only
    return "must" if key in MUST else "should" if key in SHOULD else "info"


def _guide(it: dict) -> str | None:
    if it["key"] == "antiskating" and it.get("value") == "balanced":
        return "tracking_force"                # both channels break up together: the force, not the bias
    return GUIDE_FOR.get(it["key"])


def _side_of(rec: dict) -> dict[str, str]:
    """Finding → the side of the record that measures it (empty for records without a known layout)."""
    out = {}
    for side, checks in (rec.get("layout") or {}).items():
        for c in checks:
            for k in testrecords.CHECKS[c]:
                out[k] = side
    return out


def readiness(items: list[dict], rec: dict, *, calibrated: bool, fixed_gain: bool = False) -> dict:
    """Verdict on the findings of a run (already judged against the profile's targets). See the module docstring."""
    by = {it["key"]: it for it in items}
    side = _side_of(rec)
    rank = {"must": 0, "should": 1, "info": 2}
    out = []
    for it in items:
        if it.get("status") not in ("warn", "bad"):
            continue
        lvl = _level(it["key"], fixed_gain)
        out.append({k: it.get(k) for k in ("key", "label", "value", "unit", "status", "target", "text")}
                   | {"level": lvl, "guide": _guide(it), "side": side.get(it["key"])})
    out.sort(key=lambda x: (rank[x["level"]], 0 if x["status"] == "bad" else 1))
    exp = testrecords.expected(rec)
    missing = [{"key": k, "label": _label(k), "side": side.get(k)}
               for c in testrecords.CHECKS.values() for k in c if k in exp and k not in by]
    has_reference = "ref_level" in by and rec.get("reference", True)    # a record without one cannot be the reference
    if not calibrated or not has_reference:
        verdict = "incomplete"
    elif any(x["status"] == "bad" and x["level"] != "info" for x in out):
        verdict = "adjust"
    elif any(m["key"] in MUST for m in missing):
        verdict = "remeasure"                  # a band that matters was not recognised: record its side again
    elif any(x["level"] != "info" for x in out) or any(m["key"] in SHOULD for m in missing):
        verdict = "ready_notes"
    else:
        verdict = "ready"
    guides = list(dict.fromkeys(x["guide"] for x in out if x["guide"]))
    return {"verdict": verdict, "items": out, "missing": missing, "calibrated": calibrated,
            "has_reference": has_reference, "record_has_reference": bool(rec.get("reference", True)), "guides": guides,
            "not_covered": [{"check": c, "guide": GUIDE_FOR_CHECK.get(c)} for c in rec["missing_checks"]
                            if c in GUIDE_FOR_CHECK]}


def state(db) -> dict:
    try:
        st = json.loads(db.setting(S_STATE) or "{}")
    except ValueError:
        st = {}
    return st if isinstance(st, dict) else {}


def status(db) -> dict:
    """What the interface needs at start: offer the tutorial? Which profile is half way through?"""
    st = state(db)
    profiles = db.profiles()
    accepted = any(p.get("accepted_at") for p in profiles)
    pending = next((p for p in sorted(profiles, key=lambda p: -p["id"]) if not p.get("accepted_at")), None)
    s = st.get("state") or ("done" if accepted else "open")
    return {"state": s, "offer": s == "open" and not accepted, "accepted": accepted,
            "pending": {"id": pending["id"], "name": pending["name"]} if pending else None,
            "guides": list(GUIDES), "basics": list(BASICS)}


def set_state(db, value: str) -> dict:
    if value == "open":
        db.set_setting(S_STATE, "{}")
    else:
        db.set_setting(S_STATE, json.dumps({"state": value, "at": time.time()}))
    return status(db)


def install(app, db) -> None:
    """Routes: GET /api/onboarding, POST /api/onboarding {"state": "open" | "skipped" | "done"}."""
    from fastapi import HTTPException

    @app.get("/api/onboarding")
    def onboarding_status():
        return status(db)

    @app.post("/api/onboarding")
    def onboarding_set(body: dict | None = None):
        value = (body or {}).get("state")
        if value not in ("open", "skipped", "done"):
            raise HTTPException(422, msg("onboarding.state_invalid"))
        return set_state(db, value)
