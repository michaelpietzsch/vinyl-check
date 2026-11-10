"""What the modules of the API share that is no state: the request bodies, the names of kinds, findings and
verdicts, the settings that can be changed in the interface, small checks of what a request sent, and the
figures about storage and system load.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
from pathlib import Path

from fastapi import HTTPException
from pydantic import BaseModel

from .. import discogs, i18n, vinylmind
from ..config import Config


log = logging.getLogger("vinylcheck.web")
STATIC = Path(__file__).parent.parent / "static"
KINDS = ("calibration", "before", "after", "after_wet", "after_dry", "other", "alignment")
CHECK_KINDS = ("before", "after", "after_wet", "after_dry", "other")       # recordings of a record side
TRIM_MARGIN_S = 2.0      # "Remove overhang" keeps this much after the end of the groove …
TRIM_MIN_S = 30.0        # … and is offered from this much stylus-up time after it
KIND_LABEL = {k: i18n.msg(f"labels.kind.{k}") for k in KINDS}


# Visible defects ("findings") a user can tick per copy and side
FINDINGS = ("scratch", "hairline_scratch", "warp", "dirt", "fingerprints", "off_center")
FINDINGS_LABEL = {f: i18n.msg(f"labels.finding.{f}") for f in FINDINGS}


# Event strength from which a genuine click counts as audible (provisional). Calibrated on real test pressings: on a
# side where the mastering engineer heard two clicks, exactly two genuine events reached this strength.
AUDIBLE_STRENGTH = 80.0
RPMS = (100 / 3, 45.0)


# Where an expected side length comes from (a code, shown next to the length with its label)
SIDE_SOURCE_MEASURED, SIDE_SOURCE_DISCOGS, SIDE_SOURCE_TRACKS = "measured", "discogs", "track_list"
SIDE_SOURCE_LABEL = {s: i18n.msg(f"labels.side_source.{s}")
                     for s in (SIDE_SOURCE_MEASURED, SIDE_SOURCE_DISCOGS, SIDE_SOURCE_TRACKS)}


# Polarity of a recording against its master (values from master.py) and how they are shown
POLARITY_LABEL = {p: i18n.msg(f"labels.polarity.{p}")
                  for p in ("ok", "inverted", "left_inverted", "right_inverted", "none")}


# Verdicts of the test pressing approval. The values are compared by the web interface and the PDF report;
# VERDICT_TEXT is what readers see.
APPROVE, APPROVE_NOTES, REJECT = "approve", "approve_with_notes", "reject"
VERDICT_TEXT = {v: i18n.msg(f"labels.verdict.{v}") for v in (APPROVE, APPROVE_NOTES, REJECT)}


def _localize(obj):
    """i18n.localize, plus messages that were stored as JSON text in the database (i18n.dumps)."""
    if isinstance(obj, str):
        m = i18n.loads_text(obj)
        return i18n.render(m) if m is not obj else obj
    if isinstance(obj, dict):
        return i18n.render(obj) if i18n.is_msg(obj) else {k: _localize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_localize(v) for v in obj]
    return obj


class RecordIn(BaseModel):
    artist: str = ""
    title: str = ""
    discogs_release_id: int | None = None
    discogs_media_condition: str | None = None
    notes: str = ""
    mono: bool | None = None                           # a mono record: its sides are analysed as L+R


class TrackIn(BaseModel):
    pos: str = ""
    title: str = ""
    dur_s: float | None = None


class RecordPatch(BaseModel):
    artist: str | None = None
    title: str | None = None
    discogs_release_id: int | None = None
    discogs_media_condition: str | None = None
    notes: str | None = None
    tracks: dict[str, list[TrackIn]] | None = None    # hand-entered track list (record not on Discogs), {"A": [...]}
    repeat_rule: str | None = None                     # "auto" | "on" | "off": clicks that repeat with the music
    mono: bool | None = None                           # analyse the sides as L+R (mono record on a stereo cartridge)


class StartIn(BaseModel):
    record_id: int | None = None
    setup_id: int | None = None
    profile_id: int | None = None          # empty = active turntable profile
    max_s: float | None = None             # e.g. calibration in the wizard: 25 s, stops by itself
    side: str = "A"
    kind: str = "before"
    rpm: float = 100 / 3
    cleaning_method: str = ""
    copy_label: str = ""                   # copy, when the same record exists more than once ("Version 1", "Version 2")
    findings: list[str] = []               # visible findings of this copy's side; only set when sent
    notes: str = ""
    device: str | None = None


class ProfileIn(BaseModel):
    name: str
    target_map: str | None = None
    cartridge: str = ""
    device: str = ""
    tracking_force_g: float | None = None
    antiskating: str = ""
    weight: str = ""
    notes: str = ""
    fixed_gain: bool = False               # level not adjustable on the device (USB turntable): no gain advice
    chain_source: str | None = None        # Vinyl-Mind chain class; null = guessed from device and cartridge
    chain_cartridge: str | None = None
    test_record: str | None = None         # id from testrecords; null = the AAPT 1
    stylus: str | None = None              # stylus profile (STYLI); null = not given


class ProfilePatch(BaseModel):
    target_map: str | None = None
    sensitivity: float | None = None       # detector 0…1, 0.5 = calibrated point; null = default
    fixed_gain: bool | None = None
    name: str | None = None
    cartridge: str | None = None
    device: str | None = None
    tracking_force_g: float | None = None
    antiskating: str | None = None
    weight: str | None = None
    notes: str | None = None
    chain_source: str | None = None
    chain_cartridge: str | None = None
    test_record: str | None = None
    stylus: str | None = None


class MapIn(BaseModel):
    """Target map from the web interface. Must live at module level: with `from __future__ import annotations`
    FastAPI resolves the type via the module globals; a class inside create_app() would become a query parameter."""
    name: str = ""
    note: str = ""
    targets: dict = {}


class ConditionIn(BaseModel):
    recording_id: int | None = None   # grade of this one recording (legacy)
    copy: str | None = None           # … or the record grade of this copy ("" = no copy label)
    instance_id: int | None = None    # the copy in the Discogs collection (several copies of one release)


class LabelIn(BaseModel):
    t: float
    label: str | None = None               # defect | music | unsure | null = clear the judgment


class SkipLabelIn(BaseModel):
    t: float
    label: str | None = None               # run_out | no_skip | null = clear the judgment


class WhatIfIn(BaseModel):
    ignore_events: list[float] = []        # times of clicks that should not count (±3 ms)
    ignore_skips: list[float] = []         # times of skips back that should not count (±2 s)


# vinyl-check grades that can be written to Discogs (the app never gives "Mint")
GRADE_TO_DISCOGS = discogs.GRADE_TO_CONDITION


# Settings row with the personal access token and user name entered under Settings → Discogs (JSON). The token goes
# out only to Discogs: API responses carry discogs.mask(token), and nothing logs it.
DISCOGS_ACCOUNT = "discogs_account"


# A token goes into an HTTP header: printable ASCII without spaces (Discogs issues 40 letters and digits)
DISCOGS_TOKEN_RE = re.compile(r"[\x21-\x7e]{8,200}")
DISCOGS_USER_RE = re.compile(r"\S{1,100}")


def _discogs_field(body: dict, key: str) -> str:
    """A text field of the Discogs form, trimmed ("" if empty). Raises ValueError whose only argument is an i18n
    message; the value itself is never part of it (it may be a token)."""
    raw = body.get(key)
    if raw is None:
        return ""
    v = raw.strip() if isinstance(raw, str) else None
    if v is None:
        raise ValueError(i18n.msg(f"api.discogs_{key}_invalid"))
    if v and not (DISCOGS_TOKEN_RE if key == "token" else DISCOGS_USER_RE).fullmatch(v):
        raise ValueError(i18n.msg(f"api.discogs_{key}_invalid"))
    return v


class AcceptIn(BaseModel):
    setup_id: int | None = None            # empty = the profile's calibration run


class SetupIn(BaseModel):
    profile_id: int | None = None          # empty = active profile
    label: str = ""
    test_record: str = "AAPT 1"
    tracking_force_g: float | None = None
    bias: str = ""
    vta: str = ""
    needle_hours: float | None = None
    input_level: str = ""
    notes: str = ""


class SetupPatch(BaseModel):
    label: str | None = None
    test_record: str | None = None
    tracking_force_g: float | None = None
    bias: str | None = None
    vta: str | None = None
    needle_hours: float | None = None
    input_level: str | None = None
    notes: str | None = None


class RecordingPatch(BaseModel):
    side: str | None = None
    kind: str | None = None
    cleaning_method: str | None = None
    copy_label: str | None = None
    findings: list[str] | None = None
    notes: str | None = None
    sensitivity: float | None = None       # this recording only; null = back to the profile's value


def _clean_findings(v) -> str:
    """Validate findings and return them as JSON in the canonical order of FINDINGS."""
    bad = [f for f in (v or []) if f not in FINDINGS]
    if bad:
        raise HTTPException(422, i18n.msg("api.finding_unknown", bad=", ".join(map(str, bad)),
                                          allowed=", ".join(FINDINGS)))
    return json.dumps([f for f in FINDINGS if f in (v or [])])


def _check_sensitivity(v) -> None:
    if v is not None and not 0.0 <= float(v) <= 1.0:
        raise HTTPException(422, i18n.msg("api.sensitivity_range"))


# Stylus profiles: how much of the groove wall the stylus touches decides how it reads wear and dirt, so grades of
# different chains are put in context with it. Stays on the station (not part of the Vinyl-Mind chain class).
STYLI = ("spherical", "elliptical", "line_contact")


def _check_chain(f: dict) -> None:
    """The profile's chain class for Vinyl-Mind (the hub's values, or empty/null for "guess from the profile") and
    its stylus profile (STYLI, or empty/null for not given)."""
    for k, allowed in (("chain_source", vinylmind.SOURCES), ("chain_cartridge", vinylmind.CARTRIDGES), ("stylus", STYLI)):
        if f.get(k) == "":
            f[k] = None
        if f.get(k) is not None and f[k] not in allowed:
            raise HTTPException(422, i18n.msg("api.stylus_invalid" if k == "stylus" else "vinylmind.chain_invalid"))


class CompareIn(BaseModel):
    a_id: int
    b_id: int


def _read(path: str) -> str | None:
    try:
        return Path(path).read_text()
    except OSError:
        return None


def system_load(prev: dict) -> dict:
    """CPU, memory and temperature of the host and of the service (Linux: /proc and /sys; elsewhere what is available).
    `prev` keeps the last CPU sample so that the load covers the polling interval."""
    out: dict = {"cores": os.cpu_count(), "cpu_percent": None, "load": None, "mem_total_mb": None, "mem_used_mb": None,
                 "temp_c": None, "rss_mb": None, "uptime_s": None, "per_core": None}
    try:
        out["load"] = [round(v, 2) for v in os.getloadavg()]
    except (OSError, AttributeError):
        pass
    stat = _read("/proc/stat")
    if stat:
        now = {}
        for line in stat.splitlines():
            if line.startswith("cpu"):
                f = [int(v) for v in line.split()[1:]]
                now[line.split()[0]] = (f[3] + (f[4] if len(f) > 4 else 0), sum(f))
        if prev.get("cpu"):
            pct = {}
            for name, (idle, total) in now.items():
                pi, pt = prev["cpu"].get(name, (idle, total))
                dt = total - pt
                pct[name] = round(100.0 * (1 - (idle - pi) / dt), 1) if dt > 0 else None
            out["cpu_percent"] = pct.get("cpu")
            out["per_core"] = [pct[k] for k in sorted(pct) if k != "cpu"]
        prev["cpu"] = now
    mem = _read("/proc/meminfo")
    if mem:
        kv = {}
        for line in mem.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0].endswith(":") and parts[1].isdigit():
                kv[parts[0][:-1]] = int(parts[1])
        if "MemTotal" in kv and "MemAvailable" in kv:
            out["mem_total_mb"] = round(kv["MemTotal"] / 1024)
            out["mem_used_mb"] = round((kv["MemTotal"] - kv["MemAvailable"]) / 1024)
    t = _read("/sys/class/thermal/thermal_zone0/temp")
    if t and t.strip().lstrip("-").isdigit():
        out["temp_c"] = round(int(t.strip()) / 1000, 1)
    st = _read("/proc/self/status")
    if st:
        for line in st.splitlines():
            if line.startswith("VmRSS:"):
                out["rss_mb"] = round(int(line.split()[1]) / 1024)
    up = _read("/proc/uptime")
    if up:
        out["uptime_s"] = round(float(up.split()[0]))
    return out


def storage_status(cfg: Config) -> dict:
    """Whether new recordings can be written: data drive mounted (if required) and enough free space."""
    st = {"data_dir": str(cfg.data_dir), "require_mount": cfg.require_mount or None}
    if cfg.require_mount and not os.path.ismount(cfg.require_mount):
        return st | {"ok": False, "mounted": False,
                     "reason": i18n.msg("api.storage_not_mounted", path=cfg.require_mount)}
    try:
        cfg.rec_dir.mkdir(parents=True, exist_ok=True)
        free_gb = shutil.disk_usage(cfg.rec_dir).free / 1e9
    except OSError as e:
        return st | {"ok": False, "reason": i18n.msg("api.storage_unusable", error=str(e))}
    ok = free_gb >= cfg.min_free_gb
    return st | {"ok": ok, "mounted": True if cfg.require_mount else None, "free_gb": round(free_gb, 1),
                 "reason": None if ok else i18n.msg("api.storage_low", free_gb=free_gb, min_gb=cfg.min_free_gb)}


def _setting_text(key: str) -> dict:
    """Label and help text of a setting (labels.setting.<key>.label / .help)."""
    return {"label": i18n.msg(f"labels.setting.{key}.label"), "help": i18n.msg(f"labels.setting.{key}.help")}


# Settings that can be changed in the web interface (stored in the database, effective immediately and kept across
# restarts). The audio interface (device, rate, bit depth, inputs) has a section of its own (audio_settings.py).
# Everything else (paths, network) stays in /etc/vinyl-check/config.toml because the service may not write there
# (sandbox).
SETTINGS = {
    "retention_days": {"type": int, "min": 0, "max": 3650, "unit": i18n.msg("labels.unit.days"),
                       **_setting_text("retention_days")},
    "autostop_end_groove": {"type": bool, **_setting_text("autostop_end_groove")},
    "max_record_minutes": {"type": int, "min": 5, "max": 180, "unit": "min", **_setting_text("max_record_minutes")},
    "min_free_gb": {"type": float, "min": 0.0, "max": 200.0, "unit": "GB", **_setting_text("min_free_gb")},
    "discogs_sync_minutes": {"type": int, "min": 1, "max": 1440, "unit": "min",
                             **_setting_text("discogs_sync_minutes")},
    "clip_grace_s": {"type": float, "min": 0.0, "max": 30.0, "unit": "s", **_setting_text("clip_grace_s")},
    "clip_mention_samples": {"type": int, "min": 0, "max": 100000, "unit": i18n.msg("labels.unit.samples"),
                             **_setting_text("clip_mention_samples")},
    "clip_max_samples": {"type": int, "min": 0, "max": 1000000, "unit": i18n.msg("labels.unit.samples"),
                         **_setting_text("clip_max_samples")},
    "engineer_mode": {"type": bool, **_setting_text("engineer_mode")},
    "learn_enabled": {"type": bool, **_setting_text("learn_enabled")},
    "learn_grade": {"type": bool, **_setting_text("learn_grade")},
    "update_check": {"type": bool, **_setting_text("update_check")},
}


def _setting_value(spec: dict, raw):
    """Parse and range-check a raw setting value. Raises ValueError whose only argument is an i18n message."""
    t = spec["type"]
    if t is bool:
        if isinstance(raw, bool):
            return raw
        if str(raw).lower() in ("1", "true", "yes", "ja"):
            return True
        if str(raw).lower() in ("0", "false", "no", "nein"):
            return False
        raise ValueError(i18n.msg("api.setting_expected_bool"))
    try:
        v = t(raw)
    except (TypeError, ValueError):
        raise ValueError(i18n.msg("api.setting_expected_number")) from None
    if v < spec["min"] or v > spec["max"]:
        if spec.get("unit"):
            raise ValueError(i18n.msg("api.setting_out_of_range_unit", min=spec["min"], max=spec["max"],
                                      unit=spec["unit"]))
        raise ValueError(i18n.msg("api.setting_out_of_range", min=spec["min"], max=spec["max"]))
    return v
