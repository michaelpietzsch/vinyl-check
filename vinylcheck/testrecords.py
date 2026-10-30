"""Test records: which ones vinyl-check knows, what is on their sides and what they let the station measure.

The alignment analysis (alignment.py) finds the bands of a test record by their signal, not by their position, so it
is not tied to one record. This catalogue tells the turntable wizard what to put on and which side to record, which
checks a record makes possible (and which adjustments it cannot measure, so that the user sets those by the maker's
figures), and which findings to expect. A finding a verified record should yield but did not is reported as not
found instead of quietly missing.

Checks are groups of bands (stored values, see CHECKS): the reference tone, crosstalk, speed, the anti-skating ramp,
and so on. Support levels (stored values):

- ``verified``: the band layout was measured on real recordings of this record;
- ``partial``: some bands are of the kinds the analysis knows and should be recognised by their signal, but no
  recording of this record has been checked;
- ``none``: nothing the analysis can use; listed so that someone who owns it learns that before recording.

Records the user describes (*another test record*: name, speed, sides, which kinds of bands it has) are kept in the
settings (`test_records_custom`) with the id ``custom-<n>``. Profiles store the id of their test record; an empty
value means the AAPT 1, the record vinyl-check was built and verified with.

Server catalog: ``testrecords``. The interface texts (check names, the chooser) are in the web catalog
``testrecords``.
"""

from __future__ import annotations

import json
import re

from . import i18n
from .i18n import msg

S_CUSTOM = "test_records_custom"     # settings key: {id: record} of the records users described
DEFAULT = "aapt1"

# Check → the findings (keys of alignment.TARGET_FIELDS and of the results) it yields
CHECKS = {
    "reference": ("ref_level", "balance_db"),                          # 1 kHz lateral, 0 dB
    "azimuth": ("crosstalk_lr_db", "crosstalk_rl_db", "azimuth_asym_db"),   # 1 kHz left only, right only
    "phase": ("mono_cancel_db",),                                      # 1 kHz vertical
    "speed": ("speed_pct", "wf_weighted_pct"),                         # 3150 Hz
    "tracking": ("antiskating", "tracking_reserve_db"),               # level ramp at 250–400 Hz
    "response": ("hf_10k_db", "sweep_hf_dev_db", "sweep_lf_dev_db", "lf_100_db"),   # sweeps, 10 kHz, 100 Hz
    "resonance": ("resonance_hz",),                                    # vertical bass sweep
    "vta": ("imd_pct",),                                               # two-tone 60 Hz + 4 kHz
    "silent": ("rumble_db", "floor_clicks_per_min"),                   # silent groove, 30 s or longer
}
SUPPORT = ("verified", "partial", "none")

# The built-in records. Ids are stored in profiles and must not change. Names are proper names (not translated);
# `layout` lists per side the checks whose bands the analysis recognises there, in the order the wizard records them
# (only sides with such bands); `ref_cms` is the velocity of the 1 kHz reference tone where the publisher states it.
# Band lists from the publishers' track lists and sleeve notes; only the AAPT 1 has been checked on recordings.
BUILTIN: dict[str, dict] = {
    "aapt1": {
        "name": "The Ultimate Analogue Test LP", "maker": "Analogue Productions", "catalog": "AAPT 1", "rpm": 33,
        "layout": {"1": ("reference", "azimuth", "response", "vta", "speed"),
                   "2": ("tracking", "phase", "resonance", "silent")},
        "support": "verified", "ref_cms": 7.0, "note": msg("testrecords.note.aapt1"),
        "url": "https://store.acousticsounds.com/d/35532/",
    },
    "unitra_wt2": {
        "name": "Turntable Test Vinyl Record, Laboratory Edition", "maker": "Unitra", "catalog": "WT-2", "rpm": 33,
        "layout": {"C": ("reference", "azimuth", "speed"), "D": ("tracking", "response", "phase"), "B": ("silent",)},
        "support": "partial", "ref_cms": 7.0, "note": msg("testrecords.note.unitra_wt2"),
        "url": "https://unitra.com/en/products/turntable-test-vinyl-record-laboratory-edition-13.html",
    },
    "clearaudio_smart": {
        "name": "smart test vinyl", "maker": "Clearaudio", "catalog": "LP 83070", "rpm": 33,
        "layout": {"A": ("speed", "azimuth", "reference", "phase")},
        "support": "partial", "ref_cms": 8.0, "note": msg("testrecords.note.clearaudio_smart"),
        "url": "https://www.analogshop.de/en/Testschallplatten/smart-test-vinyl/LP83070",
    },
    "clearaudio_trackability": {
        "name": "Trackability Test Record", "maker": "Clearaudio", "catalog": "LPT 83063", "rpm": 33,
        "layout": {"A": ("azimuth", "reference")},
        "support": "partial", "ref_cms": None, "note": msg("testrecords.note.clearaudio_trackability"),
        "url": "https://www.analogshop.de/downloads/LPT83063_Trackability_Test-Record_DE.pdf",
    },
    "ortofon": {
        "name": "Test Record", "maker": "Ortofon", "catalog": "", "rpm": 33,
        "layout": {"A": ("azimuth",)},
        "support": "partial", "ref_cms": None, "note": msg("testrecords.note.ortofon"),
        "url": "https://ortofon.com/products/ortofon-test-record",
    },
    "elipson": {
        "name": "Ultimate Technical Record", "maker": "Elipson", "catalog": "", "rpm": 33,
        "layout": {"A": ("azimuth", "response"), "B": ("speed",)},
        "support": "partial", "ref_cms": None, "note": msg("testrecords.note.elipson"), "url": "",
    },
    "hifinews": {
        "name": "Analogue Test LP", "maker": "Hi-Fi News", "catalog": "", "rpm": 33, "layout": {},
        "support": "none", "ref_cms": None, "note": msg("testrecords.note.hifinews"), "url": "",
    },
    "cardas": {
        "name": "Frequency Sweep & Burn-In Record", "maker": "Cardas", "catalog": "", "rpm": 33, "layout": {},
        "support": "none", "ref_cms": None, "note": msg("testrecords.note.cardas"), "url": "",
    },
}


class TestRecordError(i18n.MessageError, ValueError):
    pass


def _custom(db) -> dict[str, dict]:
    if db is None:
        return {}
    try:
        d = json.loads(db.setting(S_CUSTOM) or "{}")
    except ValueError:
        return {}
    return d if isinstance(d, dict) else {}


def public(rid: str, rec: dict) -> dict:
    """A record as the interface and the wizard see it."""
    custom = rid.startswith("custom-")
    layout = None if custom else {s: list(c) for s, c in rec["layout"].items()}
    checks = rec["checks"] if custom else [c for c in CHECKS if any(c in v for v in rec["layout"].values())]
    sides = [str(s) for s in range(1, int(rec.get("sides") or 2) + 1)] if custom else list(rec["layout"])
    return {"id": rid, "name": rec["name"], "maker": rec.get("maker", ""), "catalog": rec.get("catalog", ""),
            "rpm": rec.get("rpm", 33), "sides": sides, "layout": layout, "checks": checks,
            "missing_checks": [c for c in CHECKS if c not in checks], "reference": "reference" in checks,
            "ref_cms": rec.get("ref_cms"), "support": rec.get("support", "partial"), "note": rec.get("note"),
            "url": rec.get("url", ""), "custom": custom}


def all_records(db) -> list[dict]:
    """Built-in records first (in catalogue order), then the ones users described."""
    return ([public(k, v) for k, v in BUILTIN.items()]
            + [public(k, v) for k, v in sorted(_custom(db).items(), key=lambda kv: kv[1].get("name", "").lower())])


def usable(rec: dict | None) -> bool:
    """Can the wizard measure with it (at least one side with bands the analysis knows)?"""
    return bool(rec) and rec["support"] != "none" and bool(rec["sides"])


def get(db, rid: str | None) -> dict | None:
    """The record with this id; an empty id is the default (AAPT 1). None if it does not exist (any more)."""
    rid = rid or DEFAULT
    if rid in BUILTIN:
        return public(rid, BUILTIN[rid])
    rec = _custom(db).get(rid)
    return public(rid, rec) if rec else None


def of_profile(db, profile: dict | None) -> dict:
    """The test record of a profile; a record that was deleted falls back to the default."""
    return get(db, (profile or {}).get("test_record")) or get(db, DEFAULT)


def expected(rec: dict) -> set[str]:
    """Findings a record should yield (verified records only: for the others the band kinds are not checked)."""
    if rec.get("support") != "verified":
        return set()
    return {k for c in rec["checks"] for k in CHECKS[c]}


def label(rec: dict) -> str:
    """Short name for runs and messages, e.g. "AAPT 1" or the user's name for their record."""
    return rec.get("catalog") or rec["name"]


def validate(body: dict) -> dict:
    """A record a user described: name, maker, speed, sides, checks, note. Raises TestRecordError."""
    name = " ".join(str(body.get("name") or "").split())
    if not name:
        raise TestRecordError(msg("testrecords.name_required"))
    if len(name) > 80:
        raise TestRecordError(msg("testrecords.name_too_long", n=80))
    try:
        rpm = float(body.get("rpm") or 33)
        sides = int(body.get("sides") or 2)
    except (TypeError, ValueError) as e:
        raise TestRecordError(msg("testrecords.rpm_invalid")) from e
    rpm = 45 if abs(rpm - 45) < 1 else 33 if abs(rpm - 33.3) < 1 else None
    if rpm is None:
        raise TestRecordError(msg("testrecords.rpm_invalid"))
    if sides not in (1, 2):
        raise TestRecordError(msg("testrecords.sides_invalid"))
    checks = body.get("checks") or []
    if not isinstance(checks, list) or any(c not in CHECKS for c in checks):
        raise TestRecordError(msg("testrecords.check_unknown", allowed=", ".join(CHECKS)))
    return {"name": name, "maker": " ".join(str(body.get("maker") or "").split())[:80], "rpm": rpm, "sides": sides,
            "checks": [c for c in CHECKS if c in checks], "note": str(body.get("note") or "").strip()[:500],
            "support": "partial" if checks else "none"}


def save_custom(db, body: dict, rid: str | None = None) -> dict:
    custom = _custom(db)
    rec = validate(body)
    if rid is None:
        n = max([int(m[1]) for k in custom if (m := re.fullmatch(r"custom-(\d+)", k))] or [0]) + 1
        rid = f"custom-{n}"
    elif rid not in custom:
        raise KeyError(rid)
    custom[rid] = rec
    db.set_setting(S_CUSTOM, json.dumps(custom))
    return public(rid, rec)


def delete_custom(db, rid: str) -> None:
    custom = _custom(db)
    if rid not in custom:
        raise KeyError(rid)
    del custom[rid]
    db.set_setting(S_CUSTOM, json.dumps(custom))


def install(app, db) -> None:
    """Routes: GET /api/testrecords, POST /api/testrecords (describe a record), PUT/DELETE /api/testrecords/{id}."""
    from fastapi import HTTPException

    def body_of(body) -> dict:
        return body if isinstance(body, dict) else {}

    @app.get("/api/testrecords")
    def testrecords_list():
        return {"records": all_records(db), "default": DEFAULT, "checks": list(CHECKS)}

    @app.post("/api/testrecords")
    def testrecords_add(body: dict | None = None):
        try:
            return save_custom(db, body_of(body))
        except TestRecordError as e:
            raise HTTPException(422, e.message) from e

    @app.put("/api/testrecords/{rid}")
    def testrecords_put(rid: str, body: dict | None = None):
        if rid in BUILTIN:
            raise HTTPException(409, msg("testrecords.builtin_readonly"))
        try:
            return save_custom(db, body_of(body), rid)
        except TestRecordError as e:
            raise HTTPException(422, e.message) from e
        except KeyError as e:
            raise HTTPException(404, msg("testrecords.not_found")) from e

    @app.delete("/api/testrecords/{rid}")
    def testrecords_delete(rid: str):
        if rid in BUILTIN:
            raise HTTPException(409, msg("testrecords.builtin_readonly"))
        used = [p["name"] for p in db.profiles() if p.get("test_record") == rid]
        if used:
            raise HTTPException(409, msg("testrecords.in_use", names=", ".join(used)))
        try:
            delete_custom(db, rid)
        except KeyError as e:
            raise HTTPException(404, msg("testrecords.not_found")) from e
        return {"records": all_records(db), "default": DEFAULT, "checks": list(CHECKS)}
