"""Web interface and REST API (FastAPI).

Serves the single-page web interface from ``static/`` and the JSON API behind it: records and recordings, starting and
stopping captures (live level meter, listen-in stream), analysis results, judgments of single events, before/after
comparisons, turntable profiles and alignment runs, master comparison and test pressing approval (engineer mode), PDF
reports, Discogs collection sync and settings. A WebSocket pushes the status (recorder, job queue, notifications) to
the browser.

User-facing texts are i18n messages (``i18n.msg``), not finished strings: ``LocalizedJSONResponse`` renders them in the
language of the request (``LanguageMiddleware``: ``?lang=``, the interface's language cookie, Accept-Language), HTTP
error details and the WebSocket included. Server catalogs used here: ``api`` (messages) and ``labels`` (display names).

Intended for a trusted local network. Access from other devices can require a code (network.py: always in the
desktop app, on a service only if `access_code` is configured).
"""

from __future__ import annotations

import asyncio
import html
import io
import json
import logging
import os
import queue
import re
import shutil
import signal
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import asynccontextmanager
from pathlib import Path

import numpy as np
import soundfile as sf
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, RedirectResponse, Response, StreamingResponse
from starlette.background import BackgroundTask
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import HTTPConnection
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import __version__, analysis, capture, compare, discogs, grading, alignment, i18n, learning, report_pdf, spectro, second_opinion
from . import audio_settings, licenses, network, storage, tls, updates, vinylmind
from .config import Config, config_file, load
from .db import DB, copy_key
from .jobs import MASTER_KINDS, Worker

log = logging.getLogger("vinylcheck.web")
STATIC = Path(__file__).parent / "static"
KINDS = ("calibration", "before", "after", "after_wet", "after_dry", "other", "alignment")
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


class LocalizedJSONResponse(JSONResponse):
    """JSON response whose messages (i18n.msg) are rendered in the language of the request."""

    def render(self, content) -> bytes:
        return super().render(_localize(content))


class LanguageMiddleware:
    """Sets the language of each request (?lang=, cookie vc_lang, the configured default, Accept-Language only with
    language = "auto") for i18n.t and responses."""

    def __init__(self, app, default: str = "auto"):
        self.app = app
        self.default = default

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        conn = HTTPConnection(scope)
        lang = i18n.resolve(conn.query_params.get("lang"), conn.cookies.get(i18n.COOKIE),
                            conn.headers.get("accept-language"), self.default)
        with i18n.use_lang(lang):
            await self.app(scope, receive, send)


class _WriteClock:
    """Notes when a request last changed something (any method but GET/HEAD/OPTIONS) in `clock["last_write"]`: the
    restart when idle waits for a quiet spell after it."""

    def __init__(self, app, clock: dict):
        self.app, self.clock = app, clock

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("method") not in ("GET", "HEAD", "OPTIONS"):
            self.clock["last_write"] = time.time()
        await self.app(scope, receive, send)


class RecordIn(BaseModel):
    artist: str = ""
    title: str = ""
    discogs_release_id: int | None = None
    discogs_media_condition: str | None = None
    notes: str = ""


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


class MapIn(BaseModel):
    """Target map from the web interface. Must live at module level: with `from __future__ import annotations`
    FastAPI resolves the type via the module globals; a class inside create_app() would become a query parameter."""
    name: str = ""
    note: str = ""
    targets: dict = {}


class ConditionIn(BaseModel):
    recording_id: int | None = None   # grade of this one recording (legacy)
    copy: str | None = None           # … or the record grade of this copy ("" = no copy label)


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


def _check_chain(f: dict) -> None:
    """The profile's chain class for Vinyl-Mind: the hub's values, or empty/null for "guess from the profile"."""
    for k, allowed in (("chain_source", vinylmind.SOURCES), ("chain_cartridge", vinylmind.CARTRIDGES)):
        if f.get(k) == "":
            f[k] = None
        if f.get(k) is not None and f[k] not in allowed:
            raise HTTPException(422, i18n.msg("vinylmind.chain_invalid"))


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


def create_app(cfg: Config | None = None, source_factory=None, *, desktop: bool = False) -> FastAPI:
    """Build the app. `cfg` defaults to the configuration file; `source_factory(device, rate, channels, fmt)` replaces
    the audio source (tests). `desktop`: the app runs inside the desktop app (desktop.py), not as a service: no idle
    restart, network access only by setting and with the access code."""
    cfg = cfg or load()
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    db = DB(cfg.db_path)
    config_data_dir = storage.apply_saved(cfg, db)                       # user-data folder chosen under Settings → Storage
    defaults = {k: getattr(cfg, k) for k in SETTINGS}
    for k, raw in db.get_settings().items():           # stored settings apply from startup
        if k in SETTINGS:
            try:
                object.__setattr__(cfg, k, _setting_value(SETTINGS[k], raw))   # Config is frozen; one shared instance
            except ValueError:
                pass
    worker = Worker(cfg, db)

    # arecord (ALSA) on the Pi, PortAudio on macOS/Windows: chosen once at startup (a typo in audio_backend stops here)
    audio_backend = capture.resolve_backend(cfg.audio_backend)
    # Device, rate, bit depth and inputs as set under Settings → Audio interface, else as configured
    # (audio_settings.py); read whenever a stream opens, so changes apply from the next recording
    aud = audio_settings.AudioSettings(cfg, db, audio_backend)

    def capture_devices() -> list[dict]:
        return aud.devices()

    def default_factory(device, rate, channels, fmt):
        if cfg.fake_source:
            return capture.FileSource(cfg.fake_source, rate, channels, fmt, cfg.fake_speed)
        return capture.open_source(audio_backend, device, rate, channels, fmt, **aud.source_kwargs(device))

    recorder = capture.Recorder(source_factory or default_factory)
    # Moving the user-data folder (storage.py): no recording, master upload or retention cleanup while it runs
    mover = storage.Mover(cfg, db, recording=lambda: recorder.active,
                          analysing=lambda: bool(worker.status()["current"]), service=not desktop,
                          config_dir=config_data_dir)
    worker.storage_busy = lambda: mover.active
    checker = updates.Checker(cfg, db)
    # Discogs: the collection is cached in the state directory, synced at startup (in the background) and periodically.
    # Credentials, in this order: a personal access token saved under Settings → Discogs (settings row DISCOGS_ACCOUNT;
    # desktop users have no configuration file to edit), the one in the configuration file, an OAuth authorization.
    dc = discogs.Discogs(cfg.discogs_token, cfg.discogs_username, cfg.state_dir / "discogs-collection.json",
                         f"vinyl-check/{__version__} +https://github.com/vinyl-check",
                         consumer_key=cfg.discogs_consumer_key, consumer_secret=cfg.discogs_consumer_secret)

    def discogs_account() -> dict:
        """Token and user name saved in the settings ({} if none)."""
        try:
            d = json.loads(db.setting(DISCOGS_ACCOUNT) or "{}")
        except ValueError:
            return {}
        return d if isinstance(d, dict) and d.get("token") else {}

    def discogs_apply() -> bool:
        """Hand the credentials in effect to the client, so a change applies without a restart. True if they changed
        (the caller syncs)."""
        acct, st = discogs_account(), db.get_settings()
        otok, osec = st.get("discogs_oauth_token") or "", st.get("discogs_oauth_secret") or ""
        if acct:
            token, user = acct["token"], acct.get("username") or ""
        elif cfg.discogs_token:
            token, user = cfg.discogs_token, cfg.discogs_username
        else:                                   # OAuth: the name Discogs reported when the account was authorized
            token, user = "", (st.get("discogs_username") if otok and osec else "") or cfg.discogs_username
        return dc.configure(token, user, otok, osec)

    def discogs_source() -> str | None:
        """Where the credentials in use come from: "ui", "config", "oauth", or None (Discogs is off)."""
        if not dc.enabled:
            return None
        if dc.token:
            return "ui" if discogs_account() else "config"
        return "oauth"

    def discogs_off() -> dict:
        """Why a Discogs feature is unavailable, with where to set it up (the settings first; a service also has its
        configuration file)."""
        return i18n.msg("api.discogs_off_desktop" if desktop else "api.discogs_off_service")

    discogs_apply()
    dc.ttl = cfg.discogs_sync_minutes * 60
    if dc.stale():
        dc.sync_async()

    def _discogs_loop():
        """Re-sync the collection regularly (setting discogs_sync_minutes) so that records newly added on Discogs show
        up without any action."""
        while True:
            time.sleep(60)
            try:
                dc.ttl = cfg.discogs_sync_minutes * 60
                if dc.stale():
                    dc.sync_async()
            except Exception:  # noqa: BLE001
                pass
    threading.Thread(target=_discogs_loop, daemon=True, name="vinylcheck-discogs-loop").start()

    # ─── Restart when idle (deploy) ──────────────────────────────────────────
    # deploy.sh stages new code in /opt/vinyl-check.staged and sets this flag while something is running. As soon as
    # neither a recording nor a job is running, the service stops cleanly (SIGTERM to itself), systemd starts it again
    # (Restart=always) and ExecStartPre swaps the code in. A flag older than this process does not count: otherwise a
    # failed delete would cause a restart loop. The file name is shared with deploy.sh; versions before 1.0 used
    # "restart-wenn-frei", and deploy.sh writes both names, because the service running during a deploy may be old.
    restart_flags = [cfg.state_dir / "restart-when-idle", cfg.state_dir / "restart-wenn-frei"]
    started_at = time.time()

    def restart_pending() -> bool:
        if desktop:            # only a service is restarted (by systemd); the desktop app has no staged code
            return False
        for flag in restart_flags:
            try:
                if flag.stat().st_mtime >= started_at - 1:
                    return True
            except OSError:
                pass
        return False

    def _restart_now() -> None:
        for flag in restart_flags:
            try:
                flag.unlink()
            except OSError:
                pass
        os.kill(os.getpid(), signal.SIGTERM)
        threading.Timer(15, lambda: os._exit(0)).start()   # in case uvicorn hangs (open WebSockets)

    # "quiet": seconds without a change made in the interface (any request but GET/HEAD/OPTIONS, noted by
    # _WriteClock) before the restart, so that it does not cut into someone working there; tests replace do/poll/quiet
    restart_cfg = {"poll": 3.0, "do": _restart_now, "requested": False, "quiet": 60.0, "last_write": 0.0}

    def _restart_loop() -> None:
        while True:
            time.sleep(restart_cfg["poll"])
            if restart_cfg["requested"] or not restart_pending():
                continue
            ws = worker.status()
            if recorder.active or ws["current"] or ws["queued"] or mover.active:     # a move continues after a
                continue                                                           # restart, but from its start
            if time.time() - restart_cfg["last_write"] < restart_cfg["quiet"]:
                continue
            log.info("New code is staged and nothing is running: restarting the service")
            restart_cfg["requested"] = True
            restart_cfg["do"]()
    oauth_pending: dict = {}                              # request_token → secret (until Discogs redirects back)
    ui_events: list = []                                  # latest notifications for the browser (push without external
                                                          # services); not "events": a route function has that name

    def push_event(kind: str, text, rec_id: int | None = None) -> None:
        """Queue a notification for the browser (toast / desktop notification); `text` is an i18n message."""
        ui_events.append({"id": int(time.time() * 1000), "kind": kind, "text": text, "recording_id": rec_id, "at": time.time()})
        del ui_events[:-20]

    def side_length_for(record_id: int | None, side: str) -> tuple[float | None, str]:
        """Expected length of a side in seconds and where it comes from (SIDE_SOURCE_*): measured on an earlier
        recording of the same side, else from the Discogs or hand-entered track list."""
        if not record_id:
            return None, ""
        v = db.measured_side_length(record_id, side)
        if v:
            return v, SIDE_SOURCE_MEASURED
        r = db.record(record_id) or {}
        sides = ensure_sides(r) if r else {}
        v = sides.get(str(side).strip().upper()[:1]) if sides else None
        if not v:
            return None, ""
        return float(v), SIDE_SOURCE_DISCOGS if r.get("discogs_release_id") else SIDE_SOURCE_TRACKS

    def side_lengths(tracks: dict) -> dict:
        """Side length = sum of its tracks, only when every duration on that side is known."""
        return {s: round(sum(x["dur_s"] for x in lst), 1) for s, lst in tracks.items()
                if lst and all(x["dur_s"] is not None for x in lst)}

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

    def ensure_tracks(r: dict) -> dict:
        """Fetch the tracks per side from Discogs once and keep them on the record ({} = nothing found); also derive
        the side lengths when every duration of a side is known."""
        if r.get("tracks"):
            try:
                return json.loads(r["tracks"])
            except ValueError:
                pass
        if not r.get("discogs_release_id"):
            return {}
        try:
            tracks = dc.release_tracks(int(r["discogs_release_id"]))
        except discogs.DiscogsError:
            return {}
        sides = side_lengths(tracks)
        db.update_record(r["id"], tracks=json.dumps(tracks), sides=json.dumps(sides))
        r["tracks"], r["sides"] = json.dumps(tracks), json.dumps(sides)
        return tracks

    def ensure_sides(r: dict) -> dict:
        """Fetch the side lengths from Discogs once and keep them on the record ({} = nothing found)."""
        if r.get("sides"):
            try:
                return json.loads(r["sides"])
            except ValueError:
                pass
        if not r.get("discogs_release_id"):
            return {}
        ensure_tracks(r)
        if r.get("sides"):
            try:
                return json.loads(r["sides"])
            except ValueError:
                pass
        try:
            sides = dc.release_side_lengths(int(r["discogs_release_id"]))
        except discogs.DiscogsError:
            return {}
        db.update_record(r["id"], sides=json.dumps(sides))
        return sides

    worker.on_master = lambda rec_id, res: push_event(
        "master", i18n.msg("api.event_master_done" if res.get("ok") else "api.event_master_mismatch", id=rec_id),
        rec_id)
    worker.on_analyzed = lambda rec_id, res: push_event(
        "analyzed", i18n.msg("api.event_analyzed_grade", id=rec_id, grade=res["grading"]["grade"])
        if (res.get("grading") or {}).get("grade") else i18n.msg("api.event_analyzed", id=rec_id), rec_id)

    app_started_at = time.time()

    # ─── Learning: model from master comparisons and judgments, estimated genuine clicks ─────────────────────────
    try:
        _lm = json.loads(db.setting("learn_model") or "null")
    except ValueError:
        _lm = None
    if _lm is not None and learning.model_version(_lm) != cfg.feature_version:
        # a model of another feature version computes other features: never apply it; the learning loop retrains
        log.info("Stored learned model is feature version %s, configured is %s: retraining",
                 learning.model_version(_lm), cfg.feature_version)
        _lm = None
    learn = {"model": _lm, "fp": None, "pending_fp": None, "lock": threading.Lock(), "last_error": None,
             "applied_at": None, "applied": 0, "data": None}

    def learn_model() -> dict | None:
        return learn["model"] if cfg.learn_enabled else None

    def learn_zp(rec: dict | None) -> dict | None:
        """The recording's zero point (learning.zero_point of its turntable profile) for a learned model of feature
        version 2; None for version 1, which does not use it (and then costs no lookup)."""
        model = learn_model()
        if not model or learning.model_version(model) == "1" or not rec:
            return None
        pid = rec.get("profile_id")
        return learning.zero_point(db.profile(pid) if pid else None)

    def learn_recount(rec_id: int, r: dict | None = None) -> tuple[dict, dict] | None:
        """Recount a stored analysis with event judgments, skip judgments and the current learned model, and save it."""
        r = r or db.recording(rec_id)
        if not r or not r.get("analysis") or not (r["analysis"].get("metrics") or {}).get("counts"):
            return None
        warnings = list(r["analysis"].get("warnings") or [])
        metrics, grading_ = analysis.recount(r["analysis"]["metrics"], db.events(rec_id), cfg.grading,
                                             skip_labels=db.skip_labels(rec_id), warnings=warnings,
                                             learned=learn_model(), learn_grade=cfg.learn_grade,
                                             zero_point=learn_zp(r))
        db.update_analysis(rec_id, metrics, grading_, warnings)
        return metrics, grading_

    def learn_train() -> dict:
        """Retrain (takes a fraction of a second) and store the model. A Vinyl-Mind prior (an adopted global model or
        an imported package) is where the ridge penalty pulls the weights instead of towards zero. Features of the
        configured feature version; the prior is only used if it has the same version (vm.prior_info)."""
        with learn["lock"]:
            data, overview = learning.training_data(db, version=cfg.feature_version)
            prior = vm.prior_info()
            model = learning.train(data, prior=prior["kinds"] if prior else None, version=cfg.feature_version)
            model["data"] = overview
            if prior:
                model["prior"] = {k: prior[k] for k in ("source", "type", "ref")}
            learn["model"], learn["data"] = model, overview
            db.set_setting("learn_model", json.dumps(model))
            log.info("Learned model %s: %s", model["id"], {k: v.get("usable") for k, v in model["kinds"].items()})
        vm.note_retrained()                       # contributions may have changed: the next round looks
        return model

    # Vinyl-Mind (vinylmind.py): contributions and the global model; nothing is sent while a recording runs
    vm = vinylmind.VinylMind(cfg, db, busy=lambda: recorder.active, retrain=learn_train, desktop=desktop)

    def learn_apply(limit: int | None = None) -> int:
        """Recount every analysis whose estimate does not match the current model and switches."""
        mid = (learn_model() or {}).get("id")
        n = 0
        for r in db.q("SELECT recording_id, json_extract(metrics, '$.learned.model_id') AS mid, "
                      "json_extract(metrics, '$.learned.graded') AS gr, json_extract(metrics, '$.counts') AS c FROM analyses"):
            if not r["c"]:
                continue
            want_graded = bool(mid) and cfg.learn_grade
            if r["mid"] == mid and (bool(r["gr"]) == want_graded or not mid):
                continue
            try:
                learn_recount(r["recording_id"])
                n += 1
            except Exception:  # noqa: BLE001
                log.exception("Recount with learned model failed: recording %s", r["recording_id"])
            if limit and n >= limit:
                break
        if n:
            ref_cache["key"] = None
            learn["applied_at"], learn["applied"] = time.time(), n
            log.info("Learned model %s applied to %d recordings", mid, n)
        return n

    def _learn_fp():
        r = db.one("SELECT (SELECT COUNT(*) FROM event_labels) AS a, (SELECT MAX(created_at) FROM event_labels) AS b, "
                   "(SELECT COUNT(*) FROM master_checks WHERE status = 'done') AS c, (SELECT MAX(created_at) FROM master_checks "
                   "WHERE status = 'done') AS d") or {}
        fp = (r.get("a"), r.get("b"), r.get("c"), r.get("d"))
        if cfg.feature_version != "1":
            # feature version 2 computes the features from the profiles: a newly accepted or re-measured profile
            # changes the training data as much as a new judgement
            fp += tuple((p["id"], p.get("accepted_at"), p.get("chain_db"), p.get("ref_dbfs"),
                         p.get("floor_clicks_per_min"), p.get("floor_ticks_per_min"))
                        for p in db.q("SELECT * FROM profiles ORDER BY id"))
        return fp

    def _learn_loop() -> None:
        """Every 30 s: if judgments or master comparisons changed (and stayed so for one more round), retrain; then
        recount everything that does not match the model (freshly analyzed recordings too)."""
        while True:
            time.sleep(30)
            try:
                fp = _learn_fp()
                if fp != learn["fp"]:
                    if learn["pending_fp"] == fp or learn["model"] is None:
                        learn_train()
                        learn["fp"], learn["pending_fp"] = fp, None
                    else:
                        learn["pending_fp"] = fp          # wait one more round in case several judgments are coming in
                        continue
                learn_apply()
                learn["last_error"] = None
            except Exception as e:  # noqa: BLE001
                learn["last_error"] = str(e)
                log.exception("Learning failed")

    def regrade_all(force: bool = False) -> int:
        """Regrade all stored analyses with the current grading scheme (without recomputing the audio): at startup
        when grading.VERSION changed, or on request. Analyses without counts (very old ones) are left alone."""
        if not force and db.setting("grading_version") == str(grading.VERSION):
            return 0
        n = 0
        for r in db.q("SELECT a.recording_id, a.metrics, r.profile_id FROM analyses a "
                      "LEFT JOIN recordings r ON r.id = a.recording_id"):
            try:
                m = json.loads(r["metrics"] or "{}")
            except ValueError:
                continue
            if not m.get("counts"):
                continue
            # recount from the stored events: a new scheme may also change the metrics (e.g. pops counted only in
            # the music); the audio is not recomputed
            m, g = analysis.recount(m, db.events(r["recording_id"]), cfg.grading,
                                    learned=learn_model(), learn_grade=cfg.learn_grade, zero_point=learn_zp(r))
            db.update_analysis(r["recording_id"], m, g)
            n += 1
        db.set_setting("grading_version", str(grading.VERSION))
        ref_cache["key"] = None
        log.info("Grading scheme %s: %d analyses regraded", grading.VERSION, n)
        return n

    @asynccontextmanager
    async def lifespan(app):
        # recordings that were still running at the last shutdown are incomplete
        for r in db.q("SELECT id FROM recordings WHERE status = 'recording'"):
            db.update_recording(r["id"], status="error", error=i18n.dumps(i18n.msg("api.recording_interrupted")))
        worker.start()
        regrade_all()                                              # grading scheme changed? then regrade everything
        if not desktop:
            threading.Thread(target=_restart_loop, daemon=True, name="vinylcheck-restart").start()
        threading.Thread(target=_learn_loop, daemon=True, name="vinylcheck-learning").start()
        vm.start()
        checker.start()
        yield
        checker.stop()
        vm.stop()
        worker.close()
        if recorder.active:
            recorder.stop()

    # No generated API documentation (/docs, /redoc, /openapi.json): the interface does not use it, and a service in
    # a network has no reason to describe itself to whoever asks
    app = FastAPI(title="vinyl-check", version=__version__, lifespan=lifespan, default_response_class=LocalizedJSONResponse,
                  docs_url=None, redoc_url=None, openapi_url=None)
    network.install(app, cfg, db, desktop)        # access check + /access, /api/network; inside the language middleware
    licenses.install(app)                         # /licenses: own license and third-party notices
    vinylmind.install(app, vm)                    # /api/vinylmind*: Settings → Vinyl-Mind
    storage.install(app, cfg, db, mover)          # /api/storage*: Settings → Storage, moving the user-data folder
    updates.install(app, checker)                 # /api/updates*: is there a newer version
    if cfg.language != "auto" and not i18n.supported(cfg.language):
        raise ValueError(f"Unknown language {cfg.language!r}: use 'auto' or one of {', '.join(i18n.languages())}")
    app.add_middleware(LanguageMiddleware, default=cfg.language)
    app.add_middleware(_WriteClock, clock=restart_cfg)
    app.state.desktop = desktop

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        # detail may be an i18n message (or contain some); it is rendered in the language of the request
        return LocalizedJSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=getattr(exc, "headers", None))
    app.state.restart = restart_cfg
    app.state.cfg, app.state.db, app.state.worker, app.state.recorder = cfg, db, worker, recorder
    app.state.mover, app.state.updates = mover, checker
    audio_settings.install(app, aud, recorder, config_file=str(config_file()[0]), active_profile=db.active_profile)

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

    ref_cache: dict = {"key": None, "val": None}

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
                "learn": {"enabled": cfg.learn_enabled, "grade": cfg.learn_grade, "model_id": (learn_model() or {}).get("id"),
                          "usable": [k for k, v in ((learn_model() or {}).get("kinds") or {}).items() if v.get("usable")]},
                "ui_build": _ui_build(),
                "restart_pending": restart_pending(),
                "started_at": round(started_at, 3),         # changes with every restart: the interface reloads its view
                "clip_limits": {"mention": cfg.clip_mention_samples, "max": cfg.clip_max_samples, "grace_s": cfg.clip_grace_s},
                "second_opinion": second_opinion.info(),
                "discogs_write": dc.enabled,
                "profile": {"id": prof["id"], "name": prof["name"], "device": prof["device"],
                            "accepted": bool(prof["accepted_at"]), "ref_dbfs": prof["ref_dbfs"],
                            "speed_pct": prof["speed_pct"], "targets": _targets_for(prof["id"])[0],
                            "floor_clicks_per_min": prof.get("floor_clicks_per_min"),
                            "floor_ticks_per_min": prof.get("floor_ticks_per_min"),
                            "sensitivity": prof.get("sensitivity"), "fixed_gain": bool(prof.get("fixed_gain"))} if prof else None,
                "targets": _active_map_id(),
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
                         "discogs": i18n.msg(f"api.discogs_info_{discogs_source() or 'off'}", user=dc.username),
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
    _static = Path(__file__).resolve().parent / "static"

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
                           "history": [h | {"label": _job_label(h["kind"], h["id"])} for h in w.get("history", [])]},
                "recorder": {"active": recorder.active, "mode": rs.get("mode"), "elapsed_s": rs.get("elapsed_s"),
                             "recording_id": (rs.get("meta") or {}).get("recording_id")},
                "storage": storage_status(cfg)}

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
        devs = capture_devices()
        if cfg.fake_source:
            fake = "file:" + Path(cfg.fake_source).name
            devs.insert(0, {"id": "file", "name": i18n.msg("labels.test_file", name=Path(cfg.fake_source).name),
                            "alsa": fake, "device": fake, "usb": False, "info": ""})
        return devs

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
        return {"id": db.add_record(**r.model_dump())}

    @app.get("/api/records/{rid}")
    def get_record(rid: int):
        r = db.record(rid)
        if not r:
            raise HTTPException(404, i18n.msg("api.record_not_found"))
        r["recordings"] = db.recordings(rid)
        r["copies"] = db.copy_grades(rid).get(rid, [])              # record grade per copy (average of its sides)
        r["pressing"] = pressing_grade(r["copies"])
        r["findings"] = db.copy_findings(rid)                        # findings per copy and side
        r["comparisons"] = db.comparisons(rid)
        sides = ensure_sides(r)
        r["sides"] = sides
        r["tracks"] = ensure_tracks(r)
        r["cover_local"] = local_cover(rid) is not None
        r["masters"] = [master_info(m) for m in db.masters(rid)]
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

    @app.get("/api/records/{rid}/collection")
    def record_collection(rid: int):
        r = db.record(rid)
        if not r:
            raise HTTPException(404, i18n.msg("api.record_not_found"))
        if not dc.enabled:
            raise HTTPException(404, discogs_off())
        e = dc.entry(r.get("discogs_release_id"), r["artist"], r["title"])
        if not e:
            raise HTTPException(404, i18n.msg("api.not_in_collection"))
        return {"media_condition": e.get("media"), "in_collection_since": e.get("date_added"), "notes": e.get("notes") or "",
                "format": e.get("format"), "year": e.get("year"), "release_id": e.get("release_id"), "synced_at": dc.state["synced_at"]}

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

    # ─── Master (engineer mode) ──────────────────────────────────────────────
    # The digital master per record side (e.g. the vinyl masters from the mastering studio as WAV). Stored in the data
    # directory as masters/<record>-<side>.<ext>; the retention period applies to recordings only, not to masters.
    def master_dir() -> Path:                        # follows a move of the user-data folder at run time
        return cfg.data_dir / "masters"
    MASTER_MAX_BYTES = 3 * 1024 ** 3
    MASTER_FORMATS = {"WAV": "wav", "WAVEX": "wav", "FLAC": "flac", "AIFF": "aiff", "W64": "w64"}

    def master_info(m: dict | None) -> dict | None:
        return None if not m else {k: m.get(k) for k in ("id", "side", "name", "samplerate", "channels", "subtype",
                                                           "duration_s", "bytes", "created_at")}

    def remove_master_file(m: dict | None) -> None:
        if not m or not m.get("path"):
            return
        f = Path(m["path"]).resolve()
        if master_dir().resolve() in f.parents and f.exists():
            f.unlink()

    @app.get("/api/records/{rid}/masters")
    def record_masters(rid: int):
        if not db.record(rid):
            raise HTTPException(404, i18n.msg("api.record_not_found"))
        return [master_info(m) for m in db.masters(rid)]

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
        master_dir().mkdir(parents=True, exist_ok=True)
        if length and shutil.disk_usage(master_dir()).free - length < cfg.min_free_gb * 1e9:
            raise HTTPException(507, i18n.msg("api.master_no_space", gb=length / 1e9))
        tmp = master_dir() / f".upload-{rid}-{side}-{int(time.time() * 1000)}"
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
            final = master_dir() / f"{rid}-{side}.{ext}"
            old = db.master(rid, side)
            tmp.replace(final)
            if old and Path(old["path"]).resolve() != final.resolve():
                remove_master_file(old)
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
        return master_info(db.master_by_id(mid)) | {"queued": queued}

    @app.delete("/api/records/{rid}/masters/{side}")
    def delete_master(rid: int, side: str):
        m = db.master(rid, side)
        if not m:
            raise HTTPException(404, i18n.msg("api.no_master_for_side"))
        remove_master_file(m)
        db.delete_master(m["id"])                          # results stay, their master_id becomes empty
        return {"ok": True}

    @app.get("/api/recordings/{rec_id}/master")
    def recording_master(rec_id: int):
        r = db.recording(rec_id)
        if not r:
            raise HTTPException(404, i18n.msg("api.recording_not_found"))
        m = db.master(r["record_id"], r["side"]) if r.get("record_id") else None
        c = db.master_check(rec_id)
        return {"master": master_info(m), "check": c, "engineer_mode": cfg.engineer_mode,
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
            sides.append({"side": m["side"], "master": master_info(m), "copies": copies, "checks": checks, "verdict": verdict,
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
        r = get_recording(rec_id)
        a = r.get("analysis") or {}
        if not a.get("metrics"):
            raise HTTPException(409, i18n.msg("api.not_analyzed_yet"))
        m, g = a["metrics"], a.get("grading") or {}
        rec = db.record(r["record_id"]) if r.get("record_id") else None
        prof = db.profile(r["profile_id"]) if r.get("profile_id") else None
        ov = db.overview(rec_id) or {}
        evs = db.events(rec_id)
        ov["events"] = [[e["t"], e["kind"], e["region"], e["strength"], e.get("label"), e.get("gc")] for e in evs]
        _, lp = (learning.estimate(evs, learn_model(), m.get("music_ref_dbfs"), learn_zp(r)) if learn_model()
                 else (None, None))
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

    @app.post("/api/records/{rid}/condition")
    def record_condition_to_collection(rid: int, body: ConditionIn):
        """Write the measured condition to the Discogs collection as media condition: the record grade of a copy
        (average of its sides, `copy`) or the grade of a single recording (`recording_id`)."""
        r = db.record(rid)
        if not r:
            raise HTTPException(404, i18n.msg("api.record_not_found"))
        if not dc.enabled:
            raise HTTPException(503, discogs_off())
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
        try:
            out = dc.set_media_condition(int(r["discogs_release_id"]), grade)
        except discogs.DiscogsError as e:
            raise HTTPException(502, i18n.error_message(e)) from e
        db.update_record(rid, discogs_media_condition=out["current"])   # snapshot; Discogs stays the source of truth
        return {"ok": True, "grade": grade, "previous": out["previous"], "current": out["current"]}

    @app.patch("/api/records/{rid}")
    def patch_record(rid: int, p: RecordPatch):
        fields = {k: v for k, v in p.model_dump(exclude={"tracks"}).items() if v is not None}
        if p.tracks is not None:                       # a hand-entered track list replaces Discogs' (side lengths too)
            tracks = clean_tracks(p.tracks)
            fields.update(tracks=json.dumps(tracks), sides=json.dumps(side_lengths(tracks)))
        db.update_record(rid, **fields)
        return get_record(rid)

    @app.delete("/api/records/{rid}")
    def delete_record(rid: int):
        for rec in db.recordings(rid):
            _remove_audio(rec.get("path"))
        for m in db.masters(rid):
            remove_master_file(m)
        db.delete_record(rid)
        remove_local_cover(rid)
        return {"ok": True}

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
            if prof and prof.get("device") and prof["device"] in {d["device"] for d in capture_devices()}:
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
            present = {d["device"] for d in capture_devices()}
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
            elif (state.get("duration_s") or 0) < 3:
                db.update_recording(rec_id, status="error", error=i18n.dumps(i18n.msg("api.recording_too_short")),
                                    **fields)
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

            def on_event(name: str, st: dict):
                if name == "side_end":
                    push_event("side_end", i18n.msg("api.event_side_end"), rec_id)
                elif name == "end_groove":
                    push_event("end_groove", i18n.msg("api.event_end_groove"), rec_id)

            res = _start(lambda dev: path, {"recording_id": rec_id, "kind": s.kind, "setup_id": s.setup_id,
                                            "profile_id": prof["id"] if prof else None,
                                            "side_length_source": side_src,
                                            "side_length_source_label": SIDE_SOURCE_LABEL.get(side_src)}, s.device,
                         max_s=max_s, on_done=done,
                         endstop_rpm=rpm if cfg.autostop_end_groove and s.kind != "calibration" else None,
                         music_thr_dbfs=thr, side_length_s=side_len, on_event=on_event)
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

    # ─── Recordings ─────────────────────────────────────────────────────────
    @app.get("/api/recordings")
    def list_recordings(record_id: int | None = None):
        return db.recordings(record_id)

    @app.get("/api/recordings/{rec_id}")
    def get_recording(rec_id: int):
        r = db.recording(rec_id)
        if not r:
            raise HTTPException(404, i18n.msg("api.recording_not_found"))
        r["kind_label"] = KIND_LABEL.get(r["kind"], r["kind"])
        if r["kind"] == "alignment" and r.get("analysis") and r["analysis"].get("metrics", {}).get("results"):
            _, targets = _targets_for(r.get("profile_id"))
            r["analysis"]["metrics"]["results"] = alignment.apply_targets(r["analysis"]["metrics"]["results"], targets)
        r["file_exists"] = bool(r.get("path")) and Path(r["path"]).exists()
        r["audio_available"] = r["file_exists"] and not r.get("audio_deleted_at")
        return r

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
        r = get_recording(rec_id)
        if (("sensitivity" in f or side_changed) and r["audio_available"] and r["kind"] != "calibration"
                and r["status"] in ("analyzed", "error")):
            worker.analyze(rec_id)          # new sensitivity or another side (Discogs tracks) → recompute
            r["reanalyzing"] = True
        return r

    @app.post("/api/recordings/{rec_id}/analyze")
    def reanalyze(rec_id: int):
        r = get_recording(rec_id)
        if not r["audio_available"]:
            raise HTTPException(409, _audio_gone_msg(r))
        worker.analyze(rec_id)
        return {"queued": True}

    @app.delete("/api/recordings/{rec_id}")
    def delete_recording(rec_id: int):
        if recorder.active and recorder.state.get("meta", {}).get("recording_id") == rec_id:
            raise HTTPException(409, i18n.msg("api.recording_running"))
        _remove_audio(db.delete_recording(rec_id))
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
        evs = db.events(rec_id, limit=min(limit, 20000), order=order)
        model = learn_model()
        if model and evs:
            # the "density" feature needs all events of the recording, not just the selected ones
            all_evs = db.events(rec_id)
            r = db.one("SELECT json_extract(a.metrics, '$.music_ref_dbfs') AS ref, r.profile_id FROM analyses a "
                       "JOIN recordings r ON r.id = a.recording_id WHERE a.recording_id = ?", (rec_id,)) or {}
            _, p = learning.estimate(all_evs, model, r.get("ref"), learn_zp(r))
            lp = {round(e["t"], 5): (None if p[i] != p[i] else round(float(p[i]), 3)) for i, e in enumerate(all_evs)}
            for e in evs:
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
        metrics, grading = analysis.recount(r["analysis"]["metrics"], db.events(rec_id), cfg.grading,
                                            learned=learn_model(), learn_grade=cfg.learn_grade, zero_point=learn_zp(r))
        db.update_analysis(rec_id, metrics, grading)
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
        warnings = list(r["analysis"].get("warnings") or [])
        metrics, grading = analysis.recount(r["analysis"]["metrics"], db.events(rec_id), cfg.grading,
                                            skip_labels=db.skip_labels(rec_id), warnings=warnings,
                                            learned=learn_model(), learn_grade=cfg.learn_grade, zero_point=learn_zp(r))
        db.update_analysis(rec_id, metrics, grading, warnings)
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
                                     p.ignore_skips, cfg.grading, learned=learn_model(), learn_grade=cfg.learn_grade,
                                     zero_point=learn_zp(r))
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
        model = learn_train()
        learn["fp"], learn["pending_fp"] = _learn_fp(), None
        n = learn_apply()
        return {"model_id": model["id"], "applied": n, **learning.summary(model)}

    @app.get("/api/audit/export")
    def audit_export():
        rows = db.labeled_events()
        return {"model": second_opinion.info(), "count": len(rows), "events": rows}

    @app.post("/api/analyses/regrade")
    def analyses_regrade():
        """Regrade everything with the current scheme and limits (without recomputing the audio)."""
        return {"regraded": regrade_all(force=True), "grading_version": grading.VERSION}

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

    def _remove_audio(path: str | None):
        if not path:
            return
        p = Path(path).resolve()
        if cfg.rec_dir.resolve() in p.parents and p.exists():
            p.unlink()

    # ─── Alignment: target maps ─────────────────────────────────────────────
    # Built-in maps come from alignment.TARGET_MAPS; changes and custom maps are stored in the database (settings
    # target_maps, target_map_active). The map that applies: the profile's, else the global one.
    def _maps() -> dict:
        maps = {k: {"id": k, "name": v["name"], "note": v["note"], "builtin": True, "modified": False, "targets": v["targets"]}
                for k, v in alignment.TARGET_MAPS.items()}
        try:
            custom = json.loads(db.get_settings().get("target_maps") or "{}")
        except ValueError:
            custom = {}
        for k, v in custom.items():
            base = maps.get(k)
            maps[k] = {"id": k, "name": v.get("name") or (base or {}).get("name") or k, "note": v.get("note", (base or {}).get("note", "")),
                       "builtin": bool(base), "modified": bool(base), "targets": {**((base or {}).get("targets") or {}), **v.get("targets", {})}}
        return maps

    def _active_map_id() -> str:
        mid = db.get_settings().get("target_map_active") or "standard"
        return mid if mid in _maps() else "standard"

    def _targets_for(profile_id: int | None) -> tuple[str, dict]:
        maps = _maps()
        mid = None
        if profile_id:
            p = db.profile(profile_id)
            mid = (p or {}).get("target_map")
        if not mid or mid not in maps:
            mid = _active_map_id()
        return mid, maps[mid]["targets"]

    def _setup_findings(sid: int) -> tuple[list[dict], dict, list[dict]]:
        recs = db.setup_recordings(sid)
        summary, items = alignment.merge_summaries([r for r in recs if r["is_alignment"]])
        s = db.setup(sid) or {}
        _, targets = _targets_for(s.get("profile_id"))
        return recs, summary, alignment.apply_targets(items, targets)

    @app.get("/api/targets")
    def get_targets():
        maps = _maps()
        return {"active": _active_map_id(), "fields": {k: {"label": v[0], "unit": v[1], "kind": v[2], "hint": v[3]} for k, v in alignment.TARGET_FIELDS.items()},
                "maps": list(maps.values())}

    @app.put("/api/targets/{mid}")
    def put_targets(mid: str, m: MapIn):
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,39}", mid):
            raise HTTPException(422, i18n.msg("api.map_id_invalid"))
        try:
            targets = alignment.validate_targets(m.targets)
        except ValueError as e:
            raise HTTPException(422, i18n.error_message(e)) from e
        maps = _maps()
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
        if _active_map_id() == mid and mid not in alignment.TARGET_MAPS:
            db.set_setting("target_map_active", "standard")
        return get_targets()

    @app.post("/api/targets/active")
    def set_active_targets(body: dict):
        mid = str(body.get("id", ""))
        if mid not in _maps():
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
        steps = {
            "profile": True,
            "calibration": {"recording_id": cal["id"], "chain_db": cal["chain_db"], "created_at": cal["created_at"]} if cal else None,
            "run_id": run["id"] if run else None,
            "side1": side_state("1"), "side2": side_state("2"),
            "accepted": p.get("accepted_at"), "remeasure": remeasure,
        }
        if p.get("accepted_at") and not remeasure:
            nxt = "done"
        else:
            nxt = ("calibration" if not cal else "side1" if not (steps["side1"] and steps["side1"]["status"] == "analyzed")
                   else "side2" if not (steps["side2"] and steps["side2"]["status"] == "analyzed") else "accept")
        return steps | {"next": nxt}

    def _profile_out(p: dict) -> dict:
        p = dict(p)
        p["baseline"] = json.loads(p["baseline"]) if p.get("baseline") else None
        mid, targets = _targets_for(p["id"])
        p["targets_used"] = {"id": mid, "name": _maps()[mid]["name"], "own": bool(p.get("target_map"))}
        if p["baseline"] and p["baseline"].get("results"):
            p["baseline"]["results"] = alignment.apply_targets(p["baseline"]["results"], targets)
        p["progress"] = _profile_progress(p)
        p["connected"] = (not p["device"]) or bool(cfg.fake_source) or p["device"] in {
            d["device"] for d in capture_devices()}
        p["chain"], p["chain_guess"] = vinylmind.chain_of(p), vinylmind.guess_chain(p)   # Vinyl-Mind chain class
        return p

    @app.get("/api/profiles")
    def profiles():
        return [_profile_out(p) for p in db.profiles()]

    @app.post("/api/profiles")
    def add_profile(p: ProfileIn):
        if not p.name.strip():
            raise HTTPException(422, i18n.msg("api.profile_name_required"))
        f = p.model_dump()
        _check_chain(f)
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
        return out

    @app.patch("/api/profiles/{pid}")
    def patch_profile(pid: int, p: ProfilePatch):
        prof = db.profile(pid)
        if not prof:
            raise HTTPException(404, i18n.msg("api.profile_not_found"))
        f = p.model_dump(exclude_unset=True)
        _check_sensitivity(f.get("sensitivity"))
        _check_chain(f)
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
        sid = db.add_setup(profile_id=pid, label=i18n.t("labels.calibration_run", name=p["name"]), test_record="AAPT 1",
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
        cal = db.latest_calibration(pid)
        base = {"setup_id": sid, "summary": summary,
                "results": [{k: it.get(k) for k in ("key", "label", "value", "unit", "status", "target")}
                            for it in items]}
        db.update_profile(pid, baseline_setup_id=sid, wizard_setup_id=None, ref_dbfs=summary.get("ref_level_dbfs"),
                          speed_pct=summary.get("speed_pct"), chain_db=cal["chain_db"] if cal else None,
                          calibration_id=cal["id"] if cal else None, baseline=json.dumps(base), accepted_at=time.time(),
                          floor_clicks_per_min=summary.get("floor_clicks_per_min"),
                          floor_ticks_per_min=summary.get("floor_ticks_per_min"))
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
            _remove_audio(path)
        ref_cache["key"] = None
        return {"ok": True}

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

    @app.post("/api/retention/run")
    def retention_run():
        """Run the retention cleanup now (otherwise it runs hourly)."""
        return {"retention_days": cfg.retention_days, "deleted": worker.cleanup()}

    @app.get("/api/comparisons/{cid}")
    def get_comparison(cid: int):
        c = db.comparison(cid)
        if not c:
            raise HTTPException(404, i18n.msg("api.comparison_not_found"))
        return c

    # ─── Discogs (collection search and sync, access token, OAuth) ─────────
    @app.get("/api/discogs/search")
    def discogs_search(q: str):
        if not dc.enabled:
            raise HTTPException(404, discogs_off())
        hits = dc.search(q)
        if not hits and len(q.strip()) >= 3 and (dc.state["synced_at"] is None or time.time() - dc.state["synced_at"] > 60):
            try:
                dc.sync()                                 # nothing found: sync now (1–2 s) and search again
            except discogs.DiscogsError:
                pass
            hits = dc.search(q)
        elif dc.stale():
            dc.sync_async()
        return hits

    @app.get("/api/discogs/status")
    def discogs_status():
        """Collection state and how the account is connected. The token only ever appears masked."""
        acct = discogs_account()
        saved = {"token_hint": discogs.mask(acct["token"]), "username": acct.get("username") or ""} if acct else None
        return dc.status() | {"oauth_ready": dc.oauth_ready,
                              "via": "token" if dc.token else ("oauth" if dc.oauth_token else None),
                              "source": discogs_source(), "token_hint": discogs.mask(dc.token), "saved": saved,
                              "config_token": bool(cfg.discogs_token), "desktop": desktop}

    def _discogs_form(body: dict) -> tuple[str, str]:
        """Token and user name from the form; HTTPException 422 with a message per field (never the value)."""
        out, errors = {}, {}
        for key in ("token", "username"):
            try:
                out[key] = _discogs_field(body, key)
            except ValueError as e:
                errors[key] = i18n.error_message(e)
        if errors:
            raise HTTPException(422, {"fields": errors})
        return out["token"], out["username"]

    @app.put("/api/discogs/account")
    def discogs_account_put(body: dict):
        """Save a personal access token and user name (Settings → Discogs). They apply at once and take precedence over
        the configuration file. An empty token keeps the saved one (the form never receives it back); an empty user
        name is asked from Discogs."""
        token, user = _discogs_form(body)
        token = token or discogs_account().get("token", "")
        if not token:
            raise HTTPException(422, {"fields": {"token": i18n.msg("api.discogs_token_missing")}})
        if not user:
            try:
                user = dc.identity(token)["username"]
            except discogs.DiscogsError as e:
                raise HTTPException(502, i18n.error_message(e)) from e
        db.set_setting(DISCOGS_ACCOUNT, json.dumps({"token": token, "username": user}))
        log.info("Discogs: access token for %s saved in the settings", user)
        if discogs_apply():
            dc.sync_async()
        return discogs_status()

    @app.delete("/api/discogs/account")
    def discogs_account_delete():
        """Remove the token saved in the settings; the configuration file's or an OAuth authorization applies again."""
        db.set_setting(DISCOGS_ACCOUNT, "")
        log.info("Discogs: access token removed from the settings")
        if discogs_apply():
            dc.sync_async()
        return discogs_status()

    @app.post("/api/discogs/test")
    def discogs_test(body: dict | None = None):
        """Ask Discogs whom a token belongs to (GET /oauth/identity): the form's token (not saved) or, without one, the
        credentials in use. Compares the answer with the form's user name, else the one in use."""
        token, user = _discogs_form(body or {})
        if not token and not (dc.token or dc.oauth_token):
            return {"ok": False, "error": i18n.msg("api.discogs_token_missing")}
        expected = user or (dc.username if not token else "")
        try:
            ident = dc.identity(token or None)
        except discogs.DiscogsError as e:
            return {"ok": False, "error": e.message}
        name = ident["username"]
        return {"ok": True, "username": name, "expected": expected or None,
                "matches": name.lower() == expected.lower() if expected else None}

    @app.post("/api/discogs/connect")
    def discogs_connect(request: Request):
        """OAuth step 1: get a request token; the browser then goes to Discogs' authorization page, which redirects back
        to /api/discogs/callback (at the address under which the browser reaches this server)."""
        if not dc.oauth_ready:
            raise HTTPException(409, i18n.msg("api.discogs_app_missing"))
        host = request.headers.get("host") or f"{cfg.host}:{cfg.port}"
        callback = f"{request.url.scheme}://{host}/api/discogs/callback"
        try:
            r = dc.oauth_request_token(callback)
        except discogs.DiscogsError as e:
            raise HTTPException(502, i18n.error_message(e)) from e
        oauth_pending.clear()
        oauth_pending[r["token"]] = r["secret"]
        return {"authorize_url": r["authorize_url"]}

    @app.get("/api/discogs/callback", include_in_schema=False)
    def discogs_callback(oauth_token: str = "", oauth_verifier: str = "", denied: str = ""):
        """OAuth step 2: Discogs redirects here after the user granted (or denied) access."""
        if denied or oauth_token not in oauth_pending:
            return Response(f"<p>{html.escape(i18n.t('api.oauth_denied'))}</p>",
                            media_type="text/html; charset=utf-8", status_code=400)
        try:
            r = dc.oauth_access_token(oauth_token, oauth_pending.pop(oauth_token), oauth_verifier)
        except discogs.DiscogsError as e:
            return Response(f"<p>{html.escape(i18n.t('api.oauth_failed', error=i18n.error_message(e)))}</p>",
                            media_type="text/html; charset=utf-8", status_code=502)
        db.set_setting("discogs_oauth_token", r["token"])
        db.set_setting("discogs_oauth_secret", r["secret"])
        db.set_setting("discogs_username", r["username"])
        discogs_apply()                     # a personal token, if there is one, still takes precedence
        dc.sync_async()
        return RedirectResponse("/#system", status_code=303)

    @app.post("/api/discogs/disconnect")
    def discogs_disconnect():
        for k in ("discogs_oauth_token", "discogs_oauth_secret"):
            db.set_setting(k, "")
        discogs_apply()
        return discogs_status()

    @app.post("/api/discogs/sync")
    def discogs_sync():
        if not dc.enabled:
            raise HTTPException(404, discogs_off())
        try:
            return dc.sync()
        except discogs.DiscogsError as e:
            raise HTTPException(502, i18n.error_message(e)) from e

    # Never cache index.html itself and request app.js/style.css with the version: otherwise, after an update, the
    # browser could run the new page with the old script.
    index_html = (STATIC / "index.html").read_text(encoding="utf-8").replace("__VERSION__", __version__)

    @app.get("/", include_in_schema=False)
    @app.get("/index.html", include_in_schema=False)
    def index():
        # the page declares the language the interface will be shown in
        page = index_html.replace('<html lang="en">', f'<html lang="{i18n.current_lang()}">', 1)
        return Response(page, media_type="text/html", headers={"Cache-Control": "no-cache"})

    # Catalog of the web interface in the language of the request, loaded by index.html before app.js
    @app.get("/i18n/catalog.js", include_in_schema=False)
    def i18n_catalog():
        return Response(i18n.web_catalog_js(i18n.current_lang()), media_type="text/javascript; charset=utf-8",
                        headers={"Cache-Control": "no-cache"})

    app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
    return app
