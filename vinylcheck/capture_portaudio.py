"""Recording through PortAudio (the `sounddevice` package): Core Audio on macOS, WASAPI and ASIO on Windows.

The Raspberry Pi records with `arecord` (capture.ArecordSource). This source gives macOS, Windows and Linux desktops
without arecord the same behaviour: samples arrive as integers in the configured format (S16_LE, S24_3LE, S32_LE),
the channels are picked as configured (capture_channels/input_channels), dropouts are counted like arecord's
"overrun", and nothing is resampled. If the device cannot run at the configured rate, opening fails with a
CaptureError instead of a converter being slipped in.

How each host API is opened, and why:

- Core Audio: PortAudio may switch the device to the configured rate (as Audio MIDI Setup would) and must fail if
  it cannot. Without these two flags PortAudio quietly puts a sample rate converter in between.
- WASAPI exclusive (`wasapi_exclusive`, on by default): direct access to the converter, past the Windows mixer and
  its effects, in the configured sample format (it must be supported natively). If the device is busy, exclusive
  use is not allowed or the format is not supported, the shared mode follows and the status says so.
- WASAPI shared: through the Windows mixer, which runs at the format set in the Windows sound settings. PortAudio's
  automatic conversion stays off, so a different rate is an error instead of a resampler.
- ASIO (`asio`, "auto" by default): studio interfaces bring an ASIO driver with direct access to the converter.
  sounddevice only loads PortAudio's ASIO build when SD_ENABLE_ASIO is set before it is imported; `load_sounddevice`
  does that on Windows unless `asio = false`. The ASIO build carries WASAPI as well, so both are always offered; ASIO
  drivers are listed first and preferred by the automatic choice, WASAPI serves interfaces without one.
- MME and DirectSound always pass through the Windows mixer and its resampler; they are refused and not listed.

Sample conversion: where the native path is integer (ASIO, WASAPI exclusive) PortAudio delivers the configured
format directly and only shifts bits (dithering off). Core Audio, WASAPI shared and the Linux host APIs work in
32-bit float; for them float32 is requested and rounded here, which gives back the converter's 16- or 24-bit values
exactly. PortAudio's own float-to-integer conversion truncates after scaling by 2^31 - 1 and would move every sample
one step towards zero.

Device ids are "<host API>: <device name>" (for example "Core Audio: USB Audio CODEC"), with " #2" for a second
device of the same name. Unlike PortAudio's device index they survive a restart and re-plugging.

`sounddevice` is imported on first use, so the Raspberry Pi does not need it.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time

import numpy as np

from . import i18n
from .capture import FORMATS, CaptureError, _decode, channel_pick

log = logging.getLogger("vinylcheck.capture")

# PortAudio sample formats that are byte for byte the capture formats (little-endian; 24 bit packed in 3 bytes)
NATIVE_DTYPE = {"S16_LE": "int16", "S24_3LE": "int24", "S32_LE": "int32"}

COREAUDIO, WASAPI, ASIO = "Core Audio", "Windows WASAPI", "ASIO"
REFUSED = ("MME", "Windows DirectSound")          # always resampled by the Windows mixer
# On Windows every device appears once per host API; WASAPI and ASIO are the ones worth choosing
HIDDEN = (*REFUSED, "Windows WDM-KS")
HOSTAPI_ORDER = {ASIO: 0, WASAPI: 1, COREAUDIO: 1, "ALSA": 2, "JACK Audio Connection Kit": 3}

# PortAudio error codes (PaErrorCode) with a message of their own
PA_INVALID_CHANNEL_COUNT, PA_INVALID_SAMPLE_RATE, PA_INVALID_DEVICE = -9998, -9997, -9996
PA_SAMPLE_FORMAT_NOT_SUPPORTED, PA_DEVICE_UNAVAILABLE = -9994, -9985

CACHE_S = 3.0        # the device list is reused this long (profile pages ask once per profile)
STALL_S = 5.0        # no audio for this long while the stream should run: the device is gone
BUFFER_S = 10.0      # audio kept for a reader that falls behind; beyond that blocks are dropped (a dropout)
SILENCE_S = 3.0      # only exact zeros for this long at the start: a note (macOS: microphone access missing)

_lock = threading.RLock()       # PortAudio calls that touch the device list: initialize, query, open, close
_open_streams = 0               # PortAudio is only re-initialized (to see newly plugged devices) while this is 0
_cache: dict = {}


def asio_mode(value) -> str | bool:
    """The `asio` setting: "auto" (the default: on Windows PortAudio's ASIO build is loaded where the installation has
    it, and ASIO drivers are offered and preferred), True (the same, with a warning if ASIO is missing) or False
    (WASAPI only, for a misbehaving driver). Raises ValueError for anything else (a typo stops the start)."""
    if isinstance(value, bool) or value == "auto":
        return value
    low = str(value).strip().lower()
    if low in ("auto", "true", "false"):
        return {"auto": "auto", "true": True, "false": False}[low]
    raise ValueError(f'asio must be "auto", true or false, not {value!r}')


def _enable_asio(asio) -> None:
    """sounddevice loads its PortAudio build with ASIO only if SD_ENABLE_ASIO is set when it is imported. Without the
    ASIO build in this installation the variable must stay unset: sounddevice would fail to load at all."""
    if asio is False or sys.platform != "win32" or "SD_ENABLE_ASIO" in os.environ:
        return
    if "sounddevice" in sys.modules:
        if asio is True:
            log.warning("asio = true has no effect: PortAudio was already loaded without ASIO; restart vinyl-check")
        return
    if not asio_available():
        if asio is True:
            log.warning("asio = true has no effect: this installation has no ASIO build of PortAudio")
        return
    os.environ["SD_ENABLE_ASIO"] = "1"


def load_sounddevice(asio="auto"):
    """The sounddevice module, imported on first use. Raises CaptureError if it or PortAudio is missing."""
    _enable_asio(asio)
    try:
        import sounddevice
    except ImportError as e:
        raise CaptureError(i18n.msg("audio.sounddevice_missing")) from e
    except OSError as e:                     # the PortAudio library itself is missing (Linux: libportaudio2)
        raise CaptureError(i18n.msg("audio.portaudio_missing", error=str(e))) from e
    return sounddevice


# ─── Devices ────────────────────────────────────────────────────────────────

def _refresh(sd) -> None:
    """Re-initialize PortAudio so that devices plugged in after the start show up (PortAudio reads its device list
    once, at initialization). Only while no stream is open: terminating PortAudio would close it."""
    term, init = getattr(sd, "_terminate", None), getattr(sd, "_initialize", None)
    if _open_streams or not (term and init):
        return
    try:
        term()
        init()
    except Exception as e:  # noqa: BLE001 — a failed refresh only means an older device list
        log.warning("PortAudio re-initialization failed: %s", e)


def _scan(sd) -> list[dict]:
    """Every input device PortAudio knows, with its stable id."""
    apis = sd.query_hostapis()
    out, seen = [], {}
    for d in sd.query_devices():
        if d.get("max_input_channels", 0) < 1:
            continue
        api = apis[d["hostapi"]]
        base = f"{api['name']}: {d['name']}"
        seen[base] = n = seen.get(base, 0) + 1
        dev_id = base if n == 1 else f"{base} #{n}"
        out.append({"id": dev_id, "device": dev_id, "index": d["index"], "name": d["name"], "hostapi": api["name"],
                    "channels": int(d["max_input_channels"]), "default_rate": int(round(d["default_samplerate"])),
                    "default": d["index"] == api.get("default_input_device"),
                    "usb": "usb" in d["name"].lower(), "backend": "portaudio", "info": "",
                    "kind": device_kind(d["name"], int(d["max_input_channels"]))})
    return out


def _order(d: dict):
    return HOSTAPI_ORDER.get(d["hostapi"], 5), d["index"]


def _all_devices(sd, asio: bool, fresh: bool = False) -> list[dict]:
    with _lock:
        key = (id(sd), asio)
        if fresh or _cache.get("key") != key or time.monotonic() - _cache["t"] > CACHE_S:
            _refresh(sd)
            _cache.update(key=key, t=time.monotonic(), val=_scan(sd))
        return [dict(d) for d in _cache["val"]]


def devices(asio="auto") -> list[dict]:
    """Input devices for the device selection, best host API first (ASIO, then WASAPI), without the Windows host APIs
    that resample (MME, DirectSound) or duplicate WASAPI (WDM-KS), and without ASIO drivers when `asio` is False.
    Raises CaptureError if PortAudio is not available."""
    sd = load_sounddevice(asio)
    hidden = HIDDEN if asio is not False else (*HIDDEN, ASIO)
    return sorted((d for d in _all_devices(sd, asio) if d["hostapi"] not in hidden), key=_order)


def find_device(found: list[dict], device: str) -> dict | None:
    """The device for an id, or for a name or part of a name (`device = "Scarlett"` in the configuration); among
    several matches the best host API wins."""
    if not device:
        return None
    for d in found:
        if d["id"] == device:
            return d
    usable = sorted((d for d in found if d["hostapi"] not in HIDDEN), key=_order)
    low = device.lower()
    return (next((d for d in usable if d["name"] == device), None)
            or next((d for d in usable if low in d["name"].lower()), None))


# ─── Source ─────────────────────────────────────────────────────────────────

def _code(e: Exception) -> int | None:
    """PortAudio error code of a sounddevice.PortAudioError (second argument), if any."""
    args = getattr(e, "args", ())
    return args[1] if len(args) > 1 and isinstance(args[1], int) else None


class SoundDeviceSource:
    """Samples from a PortAudio input device, with the interface of capture.ArecordSource: `read(frames)` returns
    integer arrays (frames, channels) in the configured format's range or None at the end, `xruns` counts dropouts,
    `errors` collects problems, `close()` stops. `notes` holds messages for the status (exclusive mode unavailable,
    only digital silence)."""

    def __init__(self, device: str, rate: int, channels: int, fmt: str, capture_channels: int = 0,
                 input_channels: tuple[int, ...] = (), *, wasapi_exclusive: bool = True, asio="auto"):
        global _open_streams
        if fmt not in NATIVE_DTYPE:
            raise CaptureError(i18n.msg("audio.format_unknown", format=fmt))
        sd = load_sounddevice(asio)
        self.fmt, self.channels, self.rate = fmt, channels, rate
        self.cap, self.pick = channel_pick(channels, capture_channels, input_channels)
        self.full = FORMATS[fmt][2]
        self.xruns = 0
        self.errors: list = []
        self.notes: list = []
        self._cond = threading.Condition()
        self._buf = bytearray()
        self._dropping = False
        self._closed = False
        self._silent = 0                 # frames of exact zeros since the start (None once something was heard)
        with _lock:
            found = _all_devices(sd, asio)
            dev = find_device(found, device)
            if dev is None:
                dev = find_device(_all_devices(sd, asio, fresh=True), device)   # plugged in since the last listing
            if dev is None:
                raise CaptureError(i18n.msg("audio.device_not_found", device=device))
            if dev["hostapi"] in REFUSED:
                raise CaptureError(i18n.msg("audio.hostapi_resamples", device=dev["id"]))
            if self.cap > dev["channels"]:
                raise CaptureError(i18n.msg("audio.channels_unsupported", device=dev["id"], channels=self.cap,
                                            max=dev["channels"]))
            self.device = dev
            # sets mode ("exclusive", "shared", "asio", "coreaudio", "default"), native and bytes_per_frame
            self.stream = self._open(sd, dev, wasapi_exclusive)
            _open_streams += 1
        self._last = time.monotonic()
        try:
            self.stream.start()
        except Exception as e:  # noqa: BLE001
            self.close()
            raise CaptureError(i18n.msg("audio.open_failed", device=dev["id"], error=str(e))) from e

    def _plans(self, sd, dev: dict, exclusive: bool) -> list[tuple[str, bool, object]]:
        """How to open the device, in order of preference: (mode, integer format natively?, extra settings)."""
        api = dev["hostapi"]
        if api == WASAPI:
            plans = []
            if exclusive:
                try:
                    excl = sd.WasapiSettings(exclusive=True, explicit_sample_format=True)
                except TypeError:            # older sounddevice: PortAudio may then pick a neighbouring format
                    excl = sd.WasapiSettings(exclusive=True)
                plans.append(("exclusive", True, excl))
            return plans + [("shared", False, sd.WasapiSettings(exclusive=False))]   # auto_convert stays off
        if api == COREAUDIO:
            return [("coreaudio", False, sd.CoreAudioSettings(change_device_parameters=True,
                                                               fail_if_conversion_required=True))]
        if api == ASIO:
            return [("asio", True, None)]
        return [("default", False, None)]

    def _open(self, sd, dev: dict, exclusive: bool):
        """Open the input stream (first plan that works); a refusal becomes a CaptureError with a reason."""
        failed: list[tuple[str, Exception]] = []
        for mode, native, extra in self._plans(sd, dev, exclusive):
            dtype = NATIVE_DTYPE[self.fmt] if native else "float32"
            try:
                sd.check_input_settings(device=dev["index"], channels=self.cap, dtype=dtype,
                                        extra_settings=extra, samplerate=self.rate)
                stream = sd.RawInputStream(samplerate=self.rate, blocksize=0, device=dev["index"], channels=self.cap,
                                           dtype=dtype, latency="high", extra_settings=extra,
                                           callback=self._callback, dither_off=True)
            except (sd.PortAudioError, ValueError) as e:
                failed.append((mode, e))
                continue
            self.mode, self.native = mode, native
            self.bytes_per_frame = (FORMATS[self.fmt][0] if native else 4) * self.cap
            self._max_bytes = int(BUFFER_S * self.rate) * self.bytes_per_frame
            if failed and failed[0][0] == "exclusive":
                self.notes.append(i18n.msg("audio.exclusive_unavailable", error=str(failed[0][1])))
                log.warning("%s: WASAPI exclusive mode not available (%s), using shared mode", dev["id"], failed[0][1])
            return stream
        # Report what the configuration can fix: if exclusive mode failed on the rate, the format or the channels,
        # that is the reason (the shared mode then usually fails only because of the mixer's format); otherwise
        # (device busy, exclusive use not allowed) the shared mode's reason
        first = failed[0]
        if len(failed) > 1 and _code(first[1]) not in (PA_INVALID_SAMPLE_RATE, PA_SAMPLE_FORMAT_NOT_SUPPORTED,
                                                       PA_INVALID_CHANNEL_COUNT):
            first = failed[-1]
        raise self._error(dev, *first)

    def _error(self, dev: dict, mode: str, e: Exception) -> CaptureError:
        code, name = _code(e), dev["id"]
        if code == PA_INVALID_SAMPLE_RATE or "sample rate" in str(e).lower():
            # ASIO drivers often keep the rate their control panel sets (or an external clock dictates)
            hint = {"shared": "audio.hint_rate_wasapi", "asio": "audioset.hint_rate_asio"}.get(mode, "audio.hint_rate")
            return CaptureError(i18n.msg("audio.rate_unsupported", device=name, rate=self.rate,
                                         default_rate=dev["default_rate"], hint=i18n.msg(hint)))
        if code == PA_SAMPLE_FORMAT_NOT_SUPPORTED:
            return CaptureError(i18n.msg("audio.format_unsupported", device=name, format=self.fmt))
        if code == PA_INVALID_CHANNEL_COUNT:
            return CaptureError(i18n.msg("audio.channels_unsupported", device=name, channels=self.cap,
                                         max=dev["channels"]))
        if code == PA_DEVICE_UNAVAILABLE:
            return CaptureError(i18n.msg("audio.device_busy", device=name))
        if code == PA_INVALID_DEVICE:
            return CaptureError(i18n.msg("audio.device_not_found", device=name))
        return CaptureError(i18n.msg("audio.open_failed", device=name, error=str(e)))

    def _callback(self, indata, frames, time_info, status) -> None:
        """PortAudio's audio thread: only copy the block and count dropouts, never wait for the reader."""
        if getattr(status, "input_overflow", False) or getattr(status, "input_underflow", False):
            self.xruns += 1                  # the driver lost samples (what arecord reports as "overrun")
        data = bytes(indata)
        with self._cond:
            if len(self._buf) + len(data) > self._max_bytes:
                if not self._dropping:       # the recording thread fell behind: one dropout per gap
                    self.xruns += 1
                    self._dropping = True
            else:
                self._dropping = False
                self._buf += data
            self._last = time.monotonic()
            self._cond.notify()

    def read(self, frames: int) -> np.ndarray | None:
        """Up to `frames` frames of the picked channels, or None at the end of the stream (stopped, device gone)."""
        want = frames * self.bytes_per_frame
        with self._cond:
            while len(self._buf) < want and not self._closed:
                if not self.stream.active:                   # aborted by PortAudio (device unplugged, error)
                    break
                if time.monotonic() - self._last > STALL_S:
                    self.errors.append(i18n.msg("audio.device_stalled", device=self.device["id"], s=int(STALL_S)))
                    self.notes.append(self.errors[-1])
                    break
                self._cond.wait(0.5)
            usable = min(len(self._buf), want)
            usable -= usable % self.bytes_per_frame
            if usable == 0:
                return None
            raw = bytes(self._buf[:usable])
            del self._buf[:usable]
        a = self._convert(raw)
        self._check_silence(a)
        return a if self.pick == list(range(self.cap)) else a[:, self.pick]

    def _convert(self, raw: bytes) -> np.ndarray:
        """Stream bytes → integers (frames, capture channels) in the range of the configured format."""
        if self.native:
            return _decode(raw, self.fmt, self.cap)
        x = np.frombuffer(raw, dtype="<f4").astype(np.float64).reshape(-1, self.cap)
        return np.clip(np.round(x * self.full), -self.full, self.full - 1).astype(np.int64)

    def _check_silence(self, a: np.ndarray) -> None:
        """Only exact zeros right from the start: on macOS the sign that the app may not use the microphone (Core
        Audio then delivers silence instead of an error); elsewhere a muted or wrong input."""
        if self._silent is None:
            return
        if np.any(a):
            self._silent = None
            return
        self._silent += len(a)
        if self._silent >= SILENCE_S * self.rate:
            self._silent = None
            self.notes.append(i18n.msg("audio.digital_silence_mac" if sys.platform == "darwin"
                                       else "audio.digital_silence"))

    def close(self) -> None:
        global _open_streams
        with self._cond:
            if self._closed:
                return
            self._closed = True
            self._cond.notify_all()
        for step in ("stop", "close"):
            try:
                getattr(self.stream, step)()
            except Exception as e:  # noqa: BLE001 — the device may already be gone
                log.debug("PortAudio %s: %s", step, e)
        with _lock:
            _open_streams = max(0, _open_streams - 1)


# ─── For the audio settings: what a device is, which rates and formats it offers ───────────────────────────────
# The settings page (audio_settings.py) lists the devices so that the right one is obvious and only offers what the
# chosen device can do. None of this opens a stream.

# Parts of names (lower case) of inputs that are not an audio interface for a turntable. They are listed after the
# interfaces and never chosen automatically (capture.default_device). Virtual devices come from conferencing,
# streaming and routing software, or are the playback looped back to an input; phones are Continuity microphones.
VIRTUAL_NAMES = ("blackhole", "soundflower", "loopback", "microsoft teams", "zoomaudiodevice", "zoom audio device",
                 "webex", "obs virtual", "virtual", "vb-audio", "voicemeeter", "stereo mix", "stereomix",
                 "what u hear", "krisp", "nvidia broadcast", "steam streaming", "background music", "camo ",
                 "wave link", "voicemod", "aggregat", "asio4all", "flexasio", "asio2wasapi")
PHONE_NAMES = ("iphone", "ipad")
BUILTIN_NAMES = ("macbook", "imac", "mac mini", "mac studio", "built-in", "builtin", "internal", "internes",
                 "integriert", "microphone array", "mikrofonarray", "headset")
# The microphone input of a computer's onboard sound (its line input may well carry a turntable)
ONBOARD_NAMES = ("realtek", "high definition audio", "conexant", "cirrus logic")
MIC_WORDS = ("microphone", "mikrofon")

STD_RATES = (44100, 48000, 88200, 96000, 176400, 192000)
CAPS_CACHE_S = 30.0             # PortAudio's format checks (WASAPI) are kept this long per device
_caps_cache: dict = {}
_ca_cache: dict = {}


def device_kind(name: str, channels: int) -> str:
    """"interface" for an input that can carry a turntable, otherwise why not: "virtual", "phone", "builtin" (a
    computer's own or a headset microphone) or "mono" (one input only)."""
    n = name.lower()
    if any(v in n for v in VIRTUAL_NAMES):
        return "virtual"
    if any(p in n for p in PHONE_NAMES):
        return "phone"
    onboard = any(o in n for o in ONBOARD_NAMES)
    if any(b in n for b in BUILTIN_NAMES) or (onboard and (any(m in n for m in MIC_WORDS) or "asio" in n)):
        return "builtin"
    return "interface" if channels >= 2 else "mono"


def _coreaudio_read() -> dict[str, dict]:
    """{device name: {"ranges": [(min, max), …], "current": Hz}} of every Core Audio device with inputs, from the
    device properties (kAudioDevicePropertyAvailableNominalSampleRates and …NominalSampleRate) through ctypes."""
    import ctypes
    import ctypes.util

    class Address(ctypes.Structure):                 # AudioObjectPropertyAddress
        _fields_ = [("selector", ctypes.c_uint32), ("scope", ctypes.c_uint32), ("element", ctypes.c_uint32)]

    class Range(ctypes.Structure):                   # AudioValueRange
        _fields_ = [("minimum", ctypes.c_double), ("maximum", ctypes.c_double)]

    u32, ptr = ctypes.c_uint32, ctypes.c_void_p
    ca = ctypes.CDLL(ctypes.util.find_library("CoreAudio"))
    cf = ctypes.CDLL(ctypes.util.find_library("CoreFoundation"))
    ca.AudioObjectGetPropertyDataSize.argtypes = [u32, ctypes.POINTER(Address), u32, ptr, ctypes.POINTER(u32)]
    ca.AudioObjectGetPropertyData.argtypes = [u32, ctypes.POINTER(Address), u32, ptr, ctypes.POINTER(u32), ptr]
    ca.AudioObjectGetPropertyDataSize.restype = ca.AudioObjectGetPropertyData.restype = ctypes.c_int32
    cf.CFStringGetCString.argtypes = [ptr, ctypes.c_char_p, ctypes.c_long, u32]
    cf.CFStringGetCString.restype = ctypes.c_bool
    cf.CFRelease.argtypes = [ptr]

    def read(obj: int, selector: str, scope: str = "glob") -> bytes:
        addr = Address(int.from_bytes(selector.encode(), "big"), int.from_bytes(scope.encode(), "big"), 0)
        size = u32(0)
        if ca.AudioObjectGetPropertyDataSize(obj, ctypes.byref(addr), 0, None, ctypes.byref(size)) or not size.value:
            return b""
        buf = ctypes.create_string_buffer(size.value)
        if ca.AudioObjectGetPropertyData(obj, ctypes.byref(addr), 0, None, ctypes.byref(size), buf):
            return b""
        return buf.raw[:size.value]

    def name(obj: int) -> str | None:
        raw = read(obj, "lnam")                      # kAudioObjectPropertyName: a CFStringRef we own
        if len(raw) != ctypes.sizeof(ptr):
            return None
        ref = ptr.from_buffer_copy(raw)
        buf = ctypes.create_string_buffer(1024)
        ok = cf.CFStringGetCString(ref, buf, len(buf), 0x08000100)       # kCFStringEncodingUTF8
        cf.CFRelease(ref)
        return buf.value.decode("utf-8", "replace") if ok else None

    out: dict[str, dict] = {}
    ids = read(1, "dev#")                            # kAudioObjectSystemObject, kAudioHardwarePropertyDevices
    for dev_id in (u32 * (len(ids) // 4)).from_buffer_copy(ids) if ids else ():
        n = name(dev_id)
        # Only devices with input streams: a USB device may appear as an input and an output device of the same name
        if not n or n in out or not read(dev_id, "stm#", "inpt"):
            continue
        rr, cur = read(dev_id, "nsr#"), read(dev_id, "nsrt")
        size = ctypes.sizeof(Range)
        ranges = [(r.minimum, r.maximum) for r in (Range * (len(rr) // size)).from_buffer_copy(rr)] if rr else []
        out[n] = {"ranges": ranges,
                  "current": int(round(ctypes.c_double.from_buffer_copy(cur).value)) if len(cur) == 8 else None}
    return out


def coreaudio_devices() -> dict[str, dict]:
    """Rates of the Core Audio devices by name (see _coreaudio_read), empty elsewhere or on any failure. Read-only on
    purpose: PortAudio's format check opens a stream, and with the settings used on Core Audio (change the device's
    rate, never convert) that would switch the interface's clock under other programs."""
    if sys.platform != "darwin":
        return {}
    with _lock:
        if time.monotonic() - _ca_cache.get("t", -1e9) > CACHE_S:
            try:
                val = _coreaudio_read()
            except Exception as e:  # noqa: BLE001 — without it the page offers the usual rates and the test tells
                log.warning("Core Audio rates: %s", e)
                val = {}
            _ca_cache.update(t=time.monotonic(), val=val)
        return _ca_cache["val"]


def device_caps(dev: dict, exclusive: bool = True) -> dict:
    """What the settings page may offer for a listed device: {"rates": the rates out of STD_RATES it records at
    without a converter, or None if that cannot be found out without disturbing it; "current_rate": the rate it runs
    at now; "formats": the capture formats it delivers natively, or None if not known (any works)}.

    Core Audio: the device's own list of rates. WASAPI exclusive and ASIO: PortAudio's format check, which only asks
    the driver (for ASIO: ASIOCanSampleRate, the rate is not changed); in WASAPI shared mode only the Windows mixer's
    rate works. The rest stays unknown, because asking would open the device; the test button in the settings
    tells."""
    out = {"rates": None, "current_rate": dev.get("default_rate"), "formats": None}
    api = dev.get("hostapi")
    if api == COREAUDIO:
        info = coreaudio_devices().get(dev.get("name"))
        if info:
            out["current_rate"] = info["current"] or out["current_rate"]
            out["rates"] = [r for r in STD_RATES if any(lo - 1 <= r <= hi + 1 for lo, hi in info["ranges"])] or None
        return out
    if api not in (WASAPI, ASIO):
        return out
    if api == WASAPI and not exclusive:
        out["rates"] = [out["current_rate"]] if out["current_rate"] in STD_RATES else None
        return out
    key = (dev.get("id"), dev.get("index"))
    with _lock:
        hit = _caps_cache.get(key)
        if hit is None or time.monotonic() - hit[0] > CAPS_CACHE_S:
            if api == ASIO and _open_streams:
                return out           # PortAudio answers for no other ASIO driver while one is open
            sd = sys.modules.get("sounddevice") or load_sounddevice()
            hit = (time.monotonic(), _probe_caps(sd, dev, out["current_rate"]))
            _caps_cache[key] = hit
    return out | hit[1]


def _probe_caps(sd, dev: dict, current) -> dict:
    """Rates and native formats of a WASAPI device in exclusive mode or of an ASIO driver, through PortAudio's format
    check (IsFormatSupported)."""
    ch = max(1, min(2, dev["channels"]))

    def ok(dtype, extra, rate) -> bool:
        try:
            sd.check_input_settings(device=dev["index"], channels=ch, dtype=dtype, extra_settings=extra,
                                    samplerate=rate)
            return True
        except (sd.PortAudioError, ValueError):
            return False

    if dev.get("hostapi") == ASIO:
        rates = [r for r in STD_RATES if ok("int32", None, r)]
        return {"rates": rates or None, "formats": None}     # PortAudio converts from the driver's format
    rates = [r for r in STD_RATES if ok("float32", sd.WasapiSettings(exclusive=True), r)]
    formats = None
    try:
        strict = sd.WasapiSettings(exclusive=True, explicit_sample_format=True)
    except TypeError:                                # older sounddevice: the format cannot be pinned
        strict = None
    if strict is not None and rates:
        rate = current if current in rates else rates[0]
        formats = [f for f, dtype in NATIVE_DTYPE.items() if ok(dtype, strict, rate)] or None
    # Nothing at all (device busy, exclusive use not allowed): unknown rather than "none"
    return {"rates": rates or None, "formats": formats}


def _portaudio_binary_dirs() -> list:
    """Where sounddevice keeps its PortAudio builds (the package _sounddevice_data; in a built app next to it)."""
    import importlib.util
    from pathlib import Path
    dirs = []
    try:
        spec = importlib.util.find_spec("_sounddevice_data")
    except (ImportError, ValueError):
        spec = None
    for base in (spec.submodule_search_locations or []) if spec else []:
        dirs.append(Path(base) / "portaudio-binaries")
    if getattr(sys, "_MEIPASS", None):
        dirs.append(Path(sys._MEIPASS) / "_sounddevice_data" / "portaudio-binaries")
    return dirs


def asio_available() -> bool:
    """Whether this installation carries the ASIO build of PortAudio (Windows only). A build of the desktop app may
    leave it out; the settings then show the ASIO switch greyed out."""
    if sys.platform != "win32":
        return False
    return any(any(d.glob("libportaudio*-asio.dll")) for d in _portaudio_binary_dirs() if d.is_dir())


def asio_loaded() -> bool:
    """Whether PortAudio was loaded with ASIO in this process (_enable_asio sets SD_ENABLE_ASIO before the import)."""
    return sys.modules.get("sounddevice") is not None and "SD_ENABLE_ASIO" in os.environ
