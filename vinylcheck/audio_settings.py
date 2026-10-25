"""Audio interface settings in the web interface: recording device, sample rate, bit depth, the pair of inputs that
carries the turntable, and on Windows WASAPI exclusive mode and ASIO.

Desktop users have no configuration file to edit, so these values can be set under Settings → Audio interface. Each
value comes from, in this order:

1. the web interface (stored in the database, one settings row `audio_interface` holding the values set there);
2. the configuration file, when the key is set there (`Config.file_keys`, or a value other than the built-in default);
3. a default. With PortAudio (macOS, Windows) the default follows the device: its current sample rate when that is one
   of the offered rates, so that vinyl-check does not switch the interface's clock under other programs, and 24 bit
   for an interface with two or more inputs. The arecord path (Raspberry Pi) has no such information and keeps the
   built-in defaults, so a service behaves exactly as before.

A turntable profile's device still wins over the device chosen here (web.py, rec_start). The values are read whenever a
stream opens (recording, level check, test), so a change applies from the next one without a restart. ASIO is the
exception: PortAudio is loaded with or without ASIO once per process.

Routes (`install`): GET/PUT/DELETE /api/audio (state, save, back to the configuration file) and POST /api/audio/test,
which opens the device with the given values for about a second through the recorder (no file is written) and reports
the peak level per channel or the reason it failed.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import re
import sys
import threading
from dataclasses import fields
from pathlib import Path

from fastapi import FastAPI, HTTPException

from . import capture, capture_portaudio, i18n
from .config import Config

log = logging.getLogger("vinylcheck.audio_settings")

KEY = "audio_interface"                       # settings row: JSON object of the values set in the web interface
FIELDS = ("device", "rate", "sample_format", "inputs", "wasapi_exclusive", "asio")
RATES = capture_portaudio.STD_RATES           # 44.1 … 192 kHz
BASE_RATES = (44100, 48000, 88200, 96000)     # offered when the device's rates are unknown
BITS = {"S16_LE": 16, "S24_3LE": 24, "S32_LE": 32}
MAX_INPUTS = 64
TEST_S = 1.0                                  # how long the test button records
_DEFAULTS = {f.name: f.default for f in fields(Config)}

# Values of the form while the test button runs: the source factory reads them instead of the stored ones
_trying: contextvars.ContextVar[dict | None] = contextvars.ContextVar("vc_audio_trying", default=None)


def _err(key: str, **params) -> ValueError:
    return ValueError(i18n.msg(f"audioset.{key}", **params))


def parse(key: str, raw, channels: int = 2):
    """A value as the web interface sends it → the stored value, or None for "not set here" (null or ""). Raises
    ValueError whose only argument is an i18n message."""
    if raw is None or raw == "":
        return None
    if key == "device":
        if not isinstance(raw, str) or len(raw) > 200:
            raise _err("device_invalid")
        return raw.strip() or None
    if key == "rate":
        if isinstance(raw, bool) or str(raw).strip() not in {str(r) for r in RATES}:
            raise _err("rate_invalid", rates=", ".join(str(r) for r in RATES))
        return int(raw)
    if key == "sample_format":
        by_bits = {str(b): f for f, b in BITS.items()}
        if not isinstance(raw, bool) and str(raw) in by_bits:        # 16, 24, 32 also work
            return by_bits[str(raw)]
        if raw not in BITS:
            raise _err("format_invalid")
        return raw
    if key == "inputs":
        try:
            pair = [int(x) for x in raw if not isinstance(x, bool)] if isinstance(raw, (list, tuple)) else []
        except (TypeError, ValueError):
            pair = []
        if (len(pair) != channels or len(set(pair)) != len(pair)
                or not all(1 <= x <= MAX_INPUTS for x in pair)):
            raise _err("inputs_invalid", n=channels, max=MAX_INPUTS)
        return pair
    if key == "asio" and raw == "auto":
        return raw
    if key in ("wasapi_exclusive", "asio"):
        if isinstance(raw, bool):
            return raw
        if str(raw).lower() in ("true", "1", "on"):
            return True
        if str(raw).lower() in ("false", "0", "off"):
            return False
        raise _err("bool_invalid")
    raise _err("unknown")


def alsa_caps(info: str) -> dict:
    """Channels, rates and formats of a USB card's capture side from /proc/asound/cardN/stream0 (only for what the
    settings page offers; never used as a default, so the service's behaviour does not change)."""
    sect = info.split("Capture:", 1)[1].split("Playback:", 1)[0] if "Capture:" in info else ""
    channels = [int(x) for x in re.findall(r"Channels:\s*(\d+)", sect)]
    rates: set[int] = set()
    for line in re.findall(r"Rates:\s*([^\n]+)", sect):
        span = re.match(r"\s*(\d+)\s*-\s*(\d+)", line)
        if span:
            rates |= {r for r in RATES if int(span[1]) <= r <= int(span[2])}
        else:
            rates |= {int(x) for x in re.findall(r"\d+", line)}
    formats = sorted(set(re.findall(r"Format:\s*(\S+)", sect)) & set(BITS), key=BITS.get)
    return {"channels": max(channels) if channels else None, "rates": sorted(rates & set(RATES)) or None,
            "formats": formats or None, "current_rate": None}


def arecord_message(line: str, device: str, eff: dict) -> dict:
    """An arecord complaint (stderr) as a message the user can act on."""
    low = line.lower()
    if "format non available" in low:
        return i18n.msg("audioset.alsa_format", device=device, bits=BITS.get(eff["sample_format"], "?"),
                        format=eff["sample_format"], detail=line)
    if "channels count non available" in low:
        return i18n.msg("audioset.alsa_channels", device=device, channels=eff["capture_channels"] or eff["channels"],
                        detail=line)
    if "rate is not accurate" in low or "rate doesn't match" in low or "rate non available" in low:
        return i18n.msg("audioset.alsa_rate", device=device, rate=eff["rate"], detail=line)
    if "busy" in low:
        return i18n.msg("audio.device_busy", device=device)
    if "no such file" in low or "no such device" in low or "cannot find card" in low:
        return i18n.msg("audio.device_not_found", device=device)
    return i18n.msg("audioset.alsa_error", detail=line)


class AudioSettings:
    """The audio settings of one engine: what is stored, what applies, what the device offers."""

    def __init__(self, cfg: Config, db, backend: str):
        self.cfg, self.db, self.backend = cfg, db, backend
        capture_portaudio.asio_mode(cfg.asio)          # a typo in the configuration stops the start

    # ─── Stored values ──────────────────────────────────────────────────────
    def stored(self) -> dict:
        """The values set in the web interface (invalid leftovers are ignored)."""
        try:
            data = json.loads(self.db.setting(KEY) or "{}")
        except ValueError:
            data = {}
        out = {}
        for k, raw in (data.items() if isinstance(data, dict) else ()):
            try:
                v = parse(k, raw, self.cfg.channels)
            except ValueError:
                log.warning("Ignoring the stored audio setting %s = %r", k, raw)
                continue
            if v is not None:
                out[k] = v
        return out

    def merge(self, changes: dict, base: dict | None = None) -> dict:
        """`base` (default: the stored values) with `changes` applied; null removes a value. Raises HTTPException 422
        with a message per field if anything is invalid."""
        out = dict(self.stored() if base is None else base)
        errors = {}
        for k, raw in changes.items():
            if k not in FIELDS:
                errors[k] = i18n.msg("audioset.unknown")
                continue
            try:
                v = parse(k, raw, self.cfg.channels)
            except ValueError as e:
                errors[k] = i18n.error_message(e)
                continue
            if v is None:
                out.pop(k, None)
            else:
                out[k] = v
        if errors:
            raise HTTPException(422, {"fields": errors})
        return out

    def save(self, values: dict) -> None:
        self.db.set_setting(KEY, json.dumps(values, sort_keys=True))

    def _values(self, values: dict | None) -> dict:
        if values is not None:
            return values
        trying = _trying.get()
        return self.stored() if trying is None else trying

    @contextlib.contextmanager
    def trying(self, values: dict):
        """Within the block, streams open with `values` instead of the stored ones (the test button)."""
        token = _trying.set(values)
        try:
            yield
        finally:
            _trying.reset(token)

    def _in_file(self, key: str) -> bool:
        """Whether the configuration sets `key` (in the file or by environment, or a value other than the default)."""
        return key in self.cfg.file_keys or getattr(self.cfg, key) != _DEFAULTS[key]

    # ─── Devices ────────────────────────────────────────────────────────────
    def asio(self, values: dict | None = None):
        """ASIO setting in effect: "auto", True or False (capture_portaudio.asio_mode)."""
        v = self._values(values)
        return v["asio"] if "asio" in v else capture_portaudio.asio_mode(self.cfg.asio)

    def devices(self, values: dict | None = None) -> list[dict]:
        """Capture devices of the backend (with ASIO drivers if ASIO is on)."""
        return capture.input_devices(self.backend, self.asio(values))

    def lookup(self, device: str | None) -> dict | None:
        """The device list entry of a device id (with PortAudio also a name or a part of one, as in the
        configuration), or None (not connected, the test file, unknown)."""
        if not device or device.startswith("file:") or self.cfg.fake_source:
            return None
        devs = self.devices()
        if self.backend == "portaudio":
            return capture_portaudio.find_device(devs, device)
        return next((d for d in devs if d.get("device") == device), None)

    def needed_inputs(self, values: dict | None = None) -> int:
        """How many inputs the device must have for the chosen pair (for the automatic choice)."""
        v = self._values(values)
        if "inputs" in v:
            return max(self.cfg.channels, *v["inputs"])
        return self.cfg.capture_channels or self.cfg.channels

    def auto_device(self, values: dict | None = None) -> str | None:
        d = capture.default_device(self.devices(values), self.needed_inputs(values))
        return d["device"] if d else None

    def choose(self, values: dict | None = None) -> tuple[str | None, str]:
        """(device, where it comes from: "ui", "config", "test_file" or "auto") when no profile asks for one."""
        v = self._values(values)
        if v.get("device"):
            return v["device"], "ui"
        if self.cfg.device:
            return self.cfg.device, "config"
        if self.cfg.fake_source:
            return "file:" + Path(self.cfg.fake_source).name, "test_file"
        return self.auto_device(v), "auto"

    def backend_problem(self, values: dict | None = None):
        return capture.backend_problem(self.backend, self.asio(values))

    # ─── Effective values ───────────────────────────────────────────────────
    def caps(self, info: dict | None, exclusive: bool) -> dict:
        """Rates, current rate and native formats of a listed device (see capture_portaudio.device_caps)."""
        if not info:
            return {"rates": None, "current_rate": None, "formats": None}
        if info.get("hostapi"):
            return capture_portaudio.device_caps(info, exclusive)
        return alsa_caps(info.get("info") or "")

    def effective(self, device: str | None = None, values: dict | None = None) -> dict:
        """The values a stream opens with on `device` (default: the device chosen as without a profile), and where
        each one comes from (`sources`: "ui", "config", "device", "default"; for the device also "auto",
        "test_file" or "request" for a device asked for explicitly, such as a turntable profile's)."""
        v = self._values(values)
        cfg, src = self.cfg, {}
        if device is None:
            device, src["device"] = self.choose(v)
        else:
            src["device"] = ("ui" if device == v.get("device") else "config" if device == cfg.device
                             else "request")
        info = self.lookup(device)
        pa = info if info is not None and info.get("hostapi") else None

        def take(key, device_default=None):
            if key in v:
                src[key] = "ui"
                return v[key]
            if self._in_file(key):
                src[key] = "config"
                return getattr(cfg, key)
            if device_default is not None:
                src[key] = "device"
                return device_default
            src[key] = "default"
            return getattr(cfg, key)

        exclusive = take("wasapi_exclusive")
        asio = capture_portaudio.asio_mode(take("asio"))
        caps = self.caps(pa, exclusive) if pa else {}
        current = caps.get("current_rate")
        rate = take("rate", current if current in (caps.get("rates") or BASE_RATES) else None)
        fmt_default = None
        if pa and pa.get("kind", "interface") == "interface" and (pa.get("channels") or 0) >= 2:
            native = caps.get("formats")
            fmt_default = next((f for f in ("S24_3LE", "S32_LE", "S16_LE") if not native or f in native), None)
        fmt = take("sample_format", fmt_default)
        n = cfg.channels
        if "inputs" in v:
            src["inputs"] = "ui"
            pair = tuple(v["inputs"])
            # Inputs beyond the first ones: open the device up to them and pick the pair (capture.channel_pick);
            # a configured capture_channels (devices that only record with all inputs open) still counts
            cap = max(cfg.capture_channels, max(pair)) if max(pair) > n else cfg.capture_channels
        elif cfg.input_channels:
            src["inputs"] = "config"
            pair, cap = tuple(cfg.input_channels), cfg.capture_channels
        else:
            src["inputs"] = "config" if cfg.capture_channels else "default"
            pair, cap = tuple(range(1, n + 1)), cfg.capture_channels
        return {"device": device, "found": info is not None, "rate": int(rate), "sample_format": fmt,
                "channels": n, "inputs": list(pair), "capture_channels": cap, "input_channels": list(pair),
                "wasapi_exclusive": bool(exclusive), "asio": asio, "sources": src}

    def source_kwargs(self, device: str) -> dict:
        """What the audio source needs beyond device, rate, channels and format (capture.open_source)."""
        e = self.effective(device)
        return {"capture_channels": e["capture_channels"], "input_channels": tuple(e["input_channels"]),
                "wasapi_exclusive": e["wasapi_exclusive"], "asio": e["asio"]}

    def system(self) -> str:
        """The audio system in use, as a key of the web catalog (audioset.system_<key>)."""
        if self.backend == "alsa":
            return "alsa"
        if sys.platform == "darwin":
            return "coreaudio"
        if sys.platform == "win32":
            return "wasapi_asio" if capture_portaudio.asio_loaded() and self.asio() is not False else "wasapi"
        return "portaudio"

    def windows(self) -> bool:
        """Whether WASAPI and ASIO matter here (the switches are shown)."""
        return self.backend == "portaudio" and sys.platform == "win32"

    # ─── For the settings page ──────────────────────────────────────────────
    def _entry(self, d: dict) -> dict:
        """A device for the selection: name, inputs, current rate, and whether it is an audio interface."""
        current = d.get("default_rate")
        if d.get("hostapi") == capture_portaudio.COREAUDIO:
            current = (capture_portaudio.coreaudio_devices().get(d["name"]) or {}).get("current") or current
        channels = d.get("channels")
        if channels is None and d.get("info"):
            channels = alsa_caps(d["info"])["channels"]
        return {"device": d.get("device"), "name": d.get("name"), "hostapi": d.get("hostapi"),
                "channels": channels, "current_rate": current, "kind": d.get("kind", "interface"),
                "usb": bool(d.get("usb")), "default": bool(d.get("default"))}

    def page(self, device: str | None = None, *, locked: bool = False, profile: dict | None = None,
             config_file: str = "") -> dict:
        """Everything the settings section shows. `device`: the device chosen in the form ("" = none chosen there),
        for showing what applies with it before saving."""
        stored = self.stored()
        cand = dict(stored)
        if device is not None:
            if device:
                cand["device"] = device
            else:
                cand.pop("device", None)
        devs = [] if self.cfg.fake_source else self.devices(cand)      # the test file stands in for every device
        eff = self.effective(values=cand)
        info = self.lookup(eff["device"])
        caps = self.caps(info, eff["wasapi_exclusive"])
        rates = list(caps.get("rates") or BASE_RATES)
        if eff["rate"] not in rates:
            rates = sorted({*rates, eff["rate"]})
        top = (self._entry(info)["channels"] if info else None) or 0
        n = top or max(8, *eff["inputs"])
        pairs = [[i, i + 1] for i in range(1, n, 2)]
        if eff["inputs"] not in pairs:
            pairs.append(eff["inputs"])
        cfg = self.cfg
        windows = self.windows()
        return {
            "backend": self.backend, "system": self.system(), "windows": windows, "locked": locked,
            "fake_source": bool(cfg.fake_source), "channels": cfg.channels,
            # ASIO is loaded once per process: switching it on after PortAudio came up without it needs a restart
            "asio": {"available": capture_portaudio.asio_available(), "loaded": capture_portaudio.asio_loaded(),
                     "restart": eff["asio"] is not False and capture_portaudio.asio_available()
                     and not capture_portaudio.asio_loaded() and "sounddevice" in sys.modules}
            if windows else None,
            "stored": stored, "selected": cand.get("device") or "",
            "config": {"device": cfg.device, "rate": cfg.rate, "sample_format": cfg.sample_format,
                       "inputs": list(cfg.input_channels) or None, "capture_channels": cfg.capture_channels,
                       "wasapi_exclusive": cfg.wasapi_exclusive, "asio": cfg.asio, "file": config_file},
            "devices": [self._entry(d) for d in devs],
            "auto": self.auto_device(cand) if not cfg.fake_source else None,
            "effective": eff, "device": self._entry(info) if info else None,
            "options": {"rates": rates, "rates_known": bool(caps.get("rates")),
                        "current_rate": caps.get("current_rate"),
                        "pairs": pairs, "inputs_known": bool(top), "formats": list(BITS),
                        "native_formats": caps.get("formats")},
            "profile": profile,
        }


def install(app: FastAPI, aud: AudioSettings, recorder, *, config_file: str = "", active_profile=lambda: None):
    """Add /api/audio (GET, PUT, DELETE) and /api/audio/test to `app`."""

    def profile_info():
        p = active_profile()
        return {"name": p["name"], "device": p["device"]} if p and p.get("device") else None

    def page(device: str | None = None):
        return aud.page(device, locked=recorder.active, profile=profile_info(), config_file=config_file)

    def refuse_while_recording(key: str) -> None:
        if recorder.active:
            raise HTTPException(409, i18n.msg(f"audioset.{key}"))

    @app.get("/api/audio")
    def audio_get(device: str | None = None):
        return page(device)

    @app.put("/api/audio")
    def audio_put(body: dict):
        refuse_while_recording("locked")
        aud.save(aud.merge(body))
        return page()

    @app.delete("/api/audio")
    def audio_reset():
        refuse_while_recording("locked")
        aud.save({})
        return page()

    @app.post("/api/audio/test")
    def audio_test(body: dict | None = None):
        """Open the device with the form's values (not saved) for about a second, as a level check would."""
        refuse_while_recording("test_busy")
        values = aud.merge(body or {})
        device, _ = aud.choose(values)
        if not device:
            raise HTTPException(409, aud.backend_problem(values) or i18n.msg("api.no_capture_device"))
        eff = aud.effective(device, values)
        bits = BITS.get(eff["sample_format"])
        out = {"ok": False, "error": None, "device": device, "rate": eff["rate"], "sample_format": eff["sample_format"],
               "bits": bits, "inputs": eff["inputs"], "capture_channels": eff["capture_channels"],
               "peak_dbfs": None, "seconds": 0.0, "xruns": 0, "notes": []}
        done, result = threading.Event(), {}

        def finished(state: dict):
            result.update(state)
            done.set()

        try:
            with aud.trying(values):
                recorder.start(device=device, rate=eff["rate"], channels=eff["channels"], fmt=eff["sample_format"],
                               path=None, max_s=TEST_S, meta={"mode": "audio_test"}, on_done=finished)
        except capture.CaptureError as e:
            if e.message == {"$t": "capture.busy"}:
                raise HTTPException(409, i18n.msg("audioset.test_busy")) from e
            return out | {"error": e.message}
        except Exception as e:  # noqa: BLE001 — any refusal of the source is the test's answer
            return out | {"error": i18n.error_message(e)}
        if not done.wait(TEST_S + capture_portaudio.STALL_S + 5):
            recorder.stop(3.0)
            done.wait(1.0)
        st = result or recorder.status()
        frames = st.get("frames") or 0
        err = st.get("error")
        first = next(iter(st.get("source_errors") or []), None)
        if not err and first is not None:
            err = arecord_message(first, device, eff) if isinstance(first, str) else first
        if not err and not frames:
            err = i18n.msg("audioset.test_no_audio")
        return out | {"ok": err is None, "error": err, "peak_dbfs": st.get("peak_hold_dbfs"),
                      "seconds": round(frames / eff["rate"], 2), "xruns": st.get("xruns") or 0,
                      "notes": list(st.get("source_notes") or [])}

    return aud
