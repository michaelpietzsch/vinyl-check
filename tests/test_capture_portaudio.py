"""Recording through PortAudio, tested with a stand-in for the sounddevice module (no audio hardware needed).

The stand-in mimics what matters of PortAudio: host APIs and their devices, Pa_IsFormatSupported's refusals
(sample rate, channels, format, a busy device), and an input stream whose callback the test drives.
"""
import importlib.abc
import importlib.util
import logging
import sys
import threading
import time
import types

import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient

from vinylcheck import capture, i18n
from vinylcheck import capture_portaudio as cpa
from vinylcheck.config import Config, _build
from vinylcheck.web import create_app


# ─── The sounddevice stand-in ───────────────────────────────────────────────

class PortAudioError(Exception):
    pass


class Flags:
    def __init__(self, overflow=False, underflow=False):
        self.input_overflow, self.input_underflow = overflow, underflow


class WasapiSettings:
    def __init__(self, exclusive=False, auto_convert=False, explicit_sample_format=False):
        self.exclusive, self.auto_convert, self.explicit = exclusive, auto_convert, explicit_sample_format


class CoreAudioSettings:
    def __init__(self, channel_map=None, change_device_parameters=False, fail_if_conversion_required=False,
                 conversion_quality="max"):
        self.change, self.strict = change_device_parameters, fail_if_conversion_required


def make_sd(hostapis, devices):
    """hostapis: names; devices: dicts with name, api (index), inputs, rate (default/mixer rate), rates (what the
    hardware can run at), formats (native integer formats), busy (exclusive access refused), default."""
    sd = types.ModuleType("sounddevice")
    sd.PortAudioError, sd.WasapiSettings, sd.CoreAudioSettings = PortAudioError, WasapiSettings, CoreAudioSettings
    sd.devices, sd.streams, sd.inits = [dict(d) for d in devices], [], 0     # tests may add devices

    def query_hostapis():
        return tuple({"name": n, "devices": [i for i, d in enumerate(sd.devices) if d["api"] == k],
                      "default_input_device": next((i for i, d in enumerate(sd.devices)
                                                    if d["api"] == k and d.get("default")), -1)}
                     for k, n in enumerate(hostapis))

    def query_devices():
        return [{"name": d["name"], "index": i, "hostapi": d["api"], "max_input_channels": d.get("inputs", 2),
                 "max_output_channels": 0, "default_samplerate": float(d.get("rate", 48000))}
                for i, d in enumerate(sd.devices)]

    def check_input_settings(device=None, channels=None, dtype=None, extra_settings=None, samplerate=None):
        d, api = sd.devices[device], hostapis[sd.devices[device]["api"]]
        if channels > d.get("inputs", 2):
            raise PortAudioError("Invalid number of channels", -9998)
        rates = d.get("rates", (d.get("rate", 48000),))
        if isinstance(extra_settings, WasapiSettings) and extra_settings.exclusive:
            if d.get("busy"):
                raise PortAudioError("Unanticipated host error", -9999, (2, -2004287478, "device in use"))
            if extra_settings.explicit and dtype not in d.get("formats", ("int16", "int24", "int32")):
                raise PortAudioError("Sample format not supported", -9994)
            if samplerate not in rates:
                raise PortAudioError("Invalid sample rate", -9997)
            return
        if api == "Windows WASAPI":                      # shared: the mixer's rate only (auto_convert is off)
            assert not extra_settings.auto_convert
            if samplerate != d.get("rate", 48000):
                raise PortAudioError("Invalid sample rate", -9997)
        elif api == "Core Audio":                        # without both flags PortAudio would resample
            assert extra_settings.change and extra_settings.strict
            if samplerate not in rates:
                raise PortAudioError("Invalid sample rate", -9997)
        elif api == "ASIO":                              # the driver sets the rate, or refuses (control panel, clock)
            if samplerate not in rates:
                raise PortAudioError("Invalid sample rate", -9997)

    class RawInputStream:
        def __init__(self, **kw):
            self.kw, self.active, self.closed = kw, False, False
            sd.streams.append(self)

        def start(self):
            self.active = True

        def stop(self):
            self.active = False

        def close(self):
            self.active, self.closed = False, True

        def feed(self, data, status=None):
            raw = data.tobytes() if isinstance(data, np.ndarray) else bytes(data)
            self.kw["callback"](raw, 0, None, status or Flags())

    def _initialize():
        sd.inits += 1

    sd.query_hostapis, sd.query_devices, sd.check_input_settings = query_hostapis, query_devices, check_input_settings
    sd.RawInputStream, sd._initialize, sd._terminate = RawInputStream, _initialize, lambda: None
    return sd


MAC = (["Core Audio"], [
    {"name": "MacBook Pro Microphone", "api": 0, "inputs": 1, "default": True},
    {"name": "USB Audio CODEC", "api": 0, "inputs": 2, "rate": 48000, "rates": (44100, 48000)},
    {"name": "Scarlett 2i2 USB", "api": 0, "inputs": 2, "rate": 48000, "rates": (44100, 48000, 96000)},
])
WIN = (["MME", "Windows DirectSound", "Windows WASAPI", "Windows WDM-KS"], [
    {"name": "Microphone (Realtek(R) Audio)", "api": 2, "inputs": 2, "default": True},
    {"name": "Line (USB Audio CODEC)", "api": 0, "inputs": 2, "rate": 44100},
    {"name": "Line (USB Audio CODEC)", "api": 1, "inputs": 2, "rate": 44100},
    {"name": "Line (USB Audio CODEC)", "api": 2, "inputs": 2, "rate": 44100, "rates": (44100, 48000),
     "formats": ("int16",)},
    {"name": "Line (USB Audio CODEC)", "api": 3, "inputs": 2, "rate": 48000},
])


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    cpa._cache.clear()
    monkeypatch.setattr(cpa, "_open_streams", 0)
    yield
    cpa._cache.clear()


@pytest.fixture
def mac(monkeypatch):
    sd = make_sd(*MAC)
    monkeypatch.setitem(sys.modules, "sounddevice", sd)
    return sd


@pytest.fixture
def win(monkeypatch):
    sd = make_sd(*WIN)
    monkeypatch.setitem(sys.modules, "sounddevice", sd)
    return sd


def _source(device, fmt="S24_3LE", rate=48000, channels=2, **kw):
    return capture.open_source("portaudio", device, rate, channels, fmt, **kw)


# ─── Backend selection ──────────────────────────────────────────────────────

def test_backend_auto_keeps_arecord_on_the_pi(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(capture.shutil, "which", lambda name: "/usr/bin/arecord")
    assert capture.resolve_backend("auto") == "alsa"
    monkeypatch.setattr(capture.shutil, "which", lambda name: None)       # Linux desktop without alsa-utils …
    monkeypatch.setattr(capture.importlib.util, "find_spec", lambda name: None)
    assert capture.resolve_backend("auto") == "alsa"                       # … and without sounddevice: arecord message
    monkeypatch.setattr(capture.importlib.util, "find_spec", lambda name: object())
    assert capture.resolve_backend("auto") == "portaudio"
    for platform in ("darwin", "win32"):
        monkeypatch.setattr(sys, "platform", platform)
        assert capture.resolve_backend("auto") == "portaudio"
        assert capture.resolve_backend("alsa") == "alsa"
    with pytest.raises(ValueError, match="audio_backend"):
        capture.resolve_backend("pulse")


def test_config_keys_and_defaults():
    cfg = Config()
    assert (cfg.audio_backend, cfg.wasapi_exclusive, cfg.asio) == ("auto", True, "auto")
    cfg = _build(Config, {"audio_backend": "portaudio", "wasapi_exclusive": False, "asio": True})
    assert (cfg.audio_backend, cfg.wasapi_exclusive, cfg.asio) == ("portaudio", False, True)
    # asio: "auto" (default), true and false; anything else stops the start
    assert [cpa.asio_mode(v) for v in ("auto", True, False, "TRUE", "false")] == ["auto", True, False, True, False]
    with pytest.raises(ValueError, match="asio"):
        cpa.asio_mode("yes")


def test_open_source_uses_arecord_for_alsa(monkeypatch):
    monkeypatch.setattr(capture.shutil, "which", lambda name: None)
    with pytest.raises(capture.CaptureError) as ei:
        capture.open_source("alsa", "hw:CARD=CODEC,DEV=0", 48000, 2, "S16_LE")
    assert ei.value.message == {"$t": "capture.arecord_missing"}


# ─── Devices ────────────────────────────────────────────────────────────────

def test_windows_lists_wasapi_only_and_prefers_the_usb_device(win):
    devs = capture.input_devices("portaudio")
    assert [d["device"] for d in devs] == ["Windows WASAPI: Microphone (Realtek(R) Audio)",
                                           "Windows WASAPI: Line (USB Audio CODEC)"]
    usb = devs[1]
    assert (usb["name"], usb["hostapi"], usb["channels"], usb["default_rate"], usb["usb"]) == \
        ("Line (USB Audio CODEC)", "Windows WASAPI", 2, 44100, True)
    assert devs[0]["default"] and not usb["default"]
    assert capture.default_device(devs, 2)["device"] == "Windows WASAPI: Line (USB Audio CODEC)"


def test_asio_devices_come_first(monkeypatch):
    sd = make_sd(["Windows WASAPI", "ASIO"], [
        {"name": "Analogue 1 + 2 (Focusrite USB Audio)", "api": 0, "inputs": 2},
        {"name": "Focusrite USB ASIO", "api": 1, "inputs": 18}])
    monkeypatch.setitem(sys.modules, "sounddevice", sd)
    assert [d["device"] for d in capture.input_devices("portaudio", asio=True)] == \
        ["ASIO: Focusrite USB ASIO", "Windows WASAPI: Analogue 1 + 2 (Focusrite USB Audio)"]


def test_default_device_needs_enough_inputs(mac):
    devs = capture.input_devices("portaudio")
    assert [d["channels"] for d in devs] == [1, 2, 2]
    assert capture.default_device(devs, 2)["device"] == "Core Audio: USB Audio CODEC"   # not the 1-channel mic
    assert capture.default_device([], 2) is None
    # ALSA entries have no channel information: first USB card, else the first card (as before)
    alsa = [{"device": "hw:CARD=PCH,DEV=0", "usb": False}, {"device": "hw:CARD=CODEC,DEV=0", "usb": True}]
    assert capture.default_device(alsa, 2)["device"] == "hw:CARD=CODEC,DEV=0"


def test_ids_survive_a_restart_and_name_twins_get_a_number(monkeypatch, mac):
    mac.devices.append({"name": "USB Audio CODEC", "api": 0, "inputs": 2})
    ids = [d["device"] for d in capture.input_devices("portaudio")]
    assert ids == ["Core Audio: MacBook Pro Microphone", "Core Audio: USB Audio CODEC", "Core Audio: Scarlett 2i2 USB",
                   "Core Audio: USB Audio CODEC #2"]
    # After a restart the indexes differ (another device came first): the id still finds the Scarlett
    mac.devices.insert(0, {"name": "BlackHole 2ch", "api": 0, "inputs": 2})
    cpa._cache.clear()
    src = _source("Core Audio: Scarlett 2i2 USB")
    assert mac.streams[-1].kw["device"] == 3 and src.device["name"] == "Scarlett 2i2 USB"
    src.close()


def test_device_by_name_or_part_of_it(mac):
    found = cpa._all_devices(mac, False)
    assert cpa.find_device(found, "scarlett")["id"] == "Core Audio: Scarlett 2i2 USB"
    assert cpa.find_device(found, "USB Audio CODEC")["id"] == "Core Audio: USB Audio CODEC"
    assert cpa.find_device(found, "Turntable") is None and cpa.find_device(found, "") is None


def test_a_device_plugged_in_later_is_found(mac):
    assert len(capture.input_devices("portaudio")) == 3
    inits = mac.inits
    mac.devices.append({"name": "AT-LP120XUSB", "api": 0, "inputs": 2})
    src = _source("Core Audio: AT-LP120XUSB")          # not in the cached list: PortAudio is re-initialized
    assert mac.inits > inits and src.device["name"] == "AT-LP120XUSB"
    inits = mac.inits
    cpa._cache.clear()
    capture.input_devices("portaudio")
    assert mac.inits == inits                          # never while a stream is open (it would be closed)
    src.close()
    cpa._cache.clear()
    capture.input_devices("portaudio")
    assert mac.inits == inits + 1


def test_without_sounddevice(monkeypatch):
    monkeypatch.setitem(sys.modules, "sounddevice", None)          # import fails as if it were not installed
    assert capture.input_devices("portaudio") == []
    assert capture.backend_problem("portaudio") == {"$t": "audio.sounddevice_missing"}
    with pytest.raises(capture.CaptureError) as ei:
        _source("Core Audio: USB Audio CODEC")
    assert "pip install sounddevice" in str(ei.value)
    with i18n.use_lang("de"):
        assert str(ei.value).startswith("Für Aufnahmen auf diesem System fehlt")


def test_without_the_portaudio_library(monkeypatch):
    class Broken(importlib.abc.MetaPathFinder, importlib.abc.Loader):
        def find_spec(self, name, path=None, target=None):
            return importlib.util.spec_from_loader(name, self) if name == "sounddevice" else None

        def create_module(self, spec):
            return None

        def exec_module(self, module):
            raise OSError("PortAudio library not found")

    monkeypatch.delitem(sys.modules, "sounddevice", raising=False)
    monkeypatch.setattr(sys, "meta_path", [Broken(), *sys.meta_path])
    msg = capture.backend_problem("portaudio")
    assert msg["$t"] == "audio.portaudio_missing" and "libportaudio2" in i18n.render(msg, "en")


def test_asio_is_enabled_before_the_import(monkeypatch, caplog):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(cpa, "asio_available", lambda: True)
    monkeypatch.delenv("SD_ENABLE_ASIO", raising=False)
    monkeypatch.delitem(sys.modules, "sounddevice", raising=False)
    cpa._enable_asio(False)
    assert "SD_ENABLE_ASIO" not in cpa.os.environ
    cpa._enable_asio("auto")                                    # the default on Windows: the ASIO build (with WASAPI)
    assert cpa.os.environ["SD_ENABLE_ASIO"] == "1"
    monkeypatch.delenv("SD_ENABLE_ASIO")
    cpa._enable_asio(True)
    assert cpa.os.environ["SD_ENABLE_ASIO"] == "1"
    # An installation without the ASIO build must not ask for it: sounddevice would not load at all
    monkeypatch.delenv("SD_ENABLE_ASIO")
    monkeypatch.setattr(cpa, "asio_available", lambda: False)
    cpa._enable_asio("auto")
    assert "SD_ENABLE_ASIO" not in cpa.os.environ
    monkeypatch.setattr(cpa, "asio_available", lambda: True)
    # Too late once PortAudio is loaded: only a warning
    monkeypatch.setitem(sys.modules, "sounddevice", types.ModuleType("sounddevice"))
    with caplog.at_level(logging.WARNING, "vinylcheck.capture"):
        cpa._enable_asio(True)
    assert "SD_ENABLE_ASIO" not in cpa.os.environ and "restart" in caplog.text
    monkeypatch.setattr(sys, "platform", "darwin")             # elsewhere the setting does nothing
    monkeypatch.delitem(sys.modules, "sounddevice")
    cpa._enable_asio(True)
    assert "SD_ENABLE_ASIO" not in cpa.os.environ


# ─── Samples ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("fmt,bits", [("S16_LE", 16), ("S24_3LE", 24), ("S32_LE", 32)])
def test_core_audio_float_is_converted_bit_exact(mac, fmt, bits):
    """Core Audio works in float: a 24-bit converter's values come back exactly, without PortAudio's truncation."""
    rng = np.random.default_rng(bits)
    src = _source("Core Audio: USB Audio CODEC", fmt)
    st = mac.streams[-1]
    assert st.kw["dtype"] == "float32" and st.kw["dither_off"] and st.kw["samplerate"] == 48000
    assert st.kw["extra_settings"].change and st.kw["extra_settings"].strict
    k = rng.integers(-2 ** 23, 2 ** 23, size=(480, 2))                 # what a 24-bit converter delivers
    st.feed((k / 2 ** 23).astype("<f4"))
    st.feed(np.array([[1.0, -1.0]], "<f4"))                           # full scale: clipped to the integer range
    a = src.read(481)
    full = 2 ** (bits - 1)
    want = np.round(k / 2 ** 23 * full) if bits == 16 else k * (full // 2 ** 23)
    assert a.dtype == np.int64 and a[:480].tolist() == want.astype(np.int64).tolist()
    assert a[480].tolist() == [full - 1, -full]
    src.close()
    assert st.closed and cpa._open_streams == 0


def test_wasapi_exclusive_delivers_the_native_format(monkeypatch):
    sd = make_sd(["Windows WASAPI"], [{"name": "Line (USB Audio CODEC)", "api": 0, "rate": 44100,
                                       "rates": (44100, 48000), "formats": ("int24",)}])
    monkeypatch.setitem(sys.modules, "sounddevice", sd)
    src = _source("Windows WASAPI: Line (USB Audio CODEC)", "S24_3LE")
    st = sd.streams[-1]
    assert src.mode == "exclusive" and st.kw["dtype"] == "int24" and st.kw["dither_off"]
    assert st.kw["extra_settings"].exclusive and st.kw["extra_settings"].explicit and not src.notes
    ints = np.array([[1, -1], [8388607, -8388608], [-5, 1234567]])
    raw = b"".join(int(v).to_bytes(3, "little", signed=True) for v in ints.ravel())     # S24_3LE
    st.feed(raw)
    assert src.read(3).tolist() == ints.tolist()
    src.close()


def test_exclusive_falls_back_to_shared_with_a_note(monkeypatch):
    sd = make_sd(["Windows WASAPI"], [{"name": "Line (USB Audio CODEC)", "api": 0, "rate": 48000, "busy": True}])
    monkeypatch.setitem(sys.modules, "sounddevice", sd)
    src = _source("Windows WASAPI: Line (USB Audio CODEC)")
    assert src.mode == "shared" and sd.streams[-1].kw["dtype"] == "float32"
    assert not sd.streams[-1].kw["extra_settings"].exclusive
    assert [n["$t"] for n in src.notes] == ["audio.exclusive_unavailable"]
    assert "device in use" in i18n.render(src.notes[0], "en")
    src.close()
    src = _source("Windows WASAPI: Line (USB Audio CODEC)", wasapi_exclusive=False)    # not requested: no note
    assert src.mode == "shared" and not src.notes
    src.close()


def test_channels_are_picked_from_a_four_channel_device(monkeypatch):
    sd = make_sd(["Core Audio"], [{"name": "Notepad", "api": 0, "inputs": 4}])
    monkeypatch.setitem(sys.modules, "sounddevice", sd)
    src = _source("Core Audio: Notepad", "S16_LE", capture_channels=4, input_channels=(3, 4))
    st = sd.streams[-1]
    assert st.kw["channels"] == 4
    frames = np.array([[1, 2, 3, 4], [-5, -6, -7, -8]]) / 32768
    st.feed(frames.astype("<f4"))
    assert src.read(2).tolist() == [[3, 4], [-7, -8]]
    src.close()


def test_overflows_count_as_dropouts(monkeypatch, mac):
    monkeypatch.setattr(cpa, "BUFFER_S", 0.01)                         # 480 frames of room for the reader
    src = _source("Core Audio: USB Audio CODEC", "S16_LE")
    st = mac.streams[-1]
    block = np.zeros((240, 2), "<f4")
    st.feed(block, Flags(overflow=True))                               # the driver lost samples
    assert src.xruns == 1
    st.feed(block)
    for _ in range(3):                                                 # the reader is too slow: one gap, one dropout
        st.feed(block)
    assert src.xruns == 2
    assert len(src.read(480)) == 480
    st.feed(block)
    st.feed(block)
    st.feed(block)                                                     # a second gap
    assert src.xruns == 3
    src.close()


# ─── Errors ─────────────────────────────────────────────────────────────────

def test_rate_the_device_cannot_do_is_refused_not_resampled(mac):
    with pytest.raises(capture.CaptureError) as ei:
        _source("Core Audio: USB Audio CODEC", rate=96000)
    m = ei.value.message
    assert m["$t"] == "audio.rate_unsupported" and m["$p"]["hint"] == {"$t": "audio.hint_rate"}
    assert str(ei.value).startswith("“Core Audio: USB Audio CODEC” cannot record at 96000 Hz without resampling")
    with i18n.use_lang("de"):
        assert "ohne Umrechnung mit 96000 Hz" in str(ei.value)
    assert cpa._open_streams == 0 and not mac.streams


def test_wasapi_shared_rate_error_explains_the_windows_format(monkeypatch, win):
    with pytest.raises(capture.CaptureError) as ei:
        _source("Windows WASAPI: Microphone (Realtek(R) Audio)", rate=44100, wasapi_exclusive=False)
    assert ei.value.message["$p"]["hint"] == {"$t": "audio.hint_rate_wasapi"}
    assert "Properties → Advanced" in str(ei.value)


def test_exclusive_format_error_wins_over_the_shared_rate_error(win):
    # 24 bit is not offered exclusively and the mixer runs at 44.1 kHz: changing sample_format is the fix
    with pytest.raises(capture.CaptureError) as ei:
        _source("Windows WASAPI: Line (USB Audio CODEC)", "S24_3LE", rate=48000)
    assert ei.value.message["$t"] == "audio.format_unsupported"
    src = _source("Windows WASAPI: Line (USB Audio CODEC)", "S16_LE", rate=48000)
    assert src.mode == "exclusive"
    src.close()


@pytest.mark.parametrize("device,kw,key", [
    ("Core Audio: Turntable", {}, "audio.device_not_found"),
    ("Core Audio: MacBook Pro Microphone", {}, "audio.channels_unsupported"),
    ("Core Audio: USB Audio CODEC", {"fmt": "FLOAT"}, "audio.format_unknown"),
])
def test_open_errors(mac, device, kw, key):
    with pytest.raises(capture.CaptureError) as ei:
        _source(device, **kw)
    assert ei.value.message["$t"] == key
    with i18n.use_lang("de"):
        assert str(ei.value) != i18n.render(ei.value.message, "en")


def test_mixer_host_apis_are_refused(win):
    with pytest.raises(capture.CaptureError) as ei:
        _source("MME: Line (USB Audio CODEC)")
    assert ei.value.message["$t"] == "audio.hostapi_resamples" and "WASAPI" in str(ei.value)


def test_a_device_that_stops_delivering_ends_the_recording(monkeypatch, mac):
    monkeypatch.setattr(cpa, "STALL_S", 0.2)
    src = _source("Core Audio: USB Audio CODEC")
    t0 = time.monotonic()
    assert src.read(4800) is None and time.monotonic() - t0 < 3
    assert src.errors[-1]["$t"] == "audio.device_stalled" and src.notes[-1] == src.errors[-1]
    src.close()


def test_an_aborted_stream_returns_what_is_left(mac):
    src = _source("Core Audio: USB Audio CODEC", "S16_LE")
    st = mac.streams[-1]
    st.feed(np.full((100, 2), 0.5, "<f4"))
    st.active = False                                     # PortAudio stopped the stream (device unplugged)
    assert len(src.read(4800)) == 100 and src.read(4800) is None
    src.close()


def test_digital_silence_gets_a_note(monkeypatch, mac):
    monkeypatch.setattr(cpa, "SILENCE_S", 0.01)
    monkeypatch.setattr(sys, "platform", "darwin")
    src = _source("Core Audio: USB Audio CODEC")
    mac.streams[-1].feed(np.zeros((960, 2), "<f4"))
    src.read(960)
    assert [n["$t"] for n in src.notes] == ["audio.digital_silence_mac"]
    assert "Privacy & Security" in i18n.render(src.notes[0], "en")
    src.close()


# ─── With the recorder ──────────────────────────────────────────────────────

def test_recorder_records_bit_exact_with_live_meter_and_dropouts(tmp_path, mac):
    """Recording, live meter, listening in and dropout count work with the PortAudio source as with arecord."""
    rate = 48000
    t = np.arange(int(rate * 1.5)) / rate
    k = np.round(np.sin(2 * np.pi * 1000 * t) * 0.1 * 2 ** 23).astype(np.int64)     # 1 kHz at -20 dBFS, 24 bit
    k = np.stack([k, -k], axis=1)
    rec = capture.Recorder(lambda dev, r, ch, fmt: capture.open_source("portaudio", dev, r, ch, fmt))
    q = rec.tap(max_chunks=1000)
    done = threading.Event()
    path = tmp_path / "side.wav"
    rec.start(device="Core Audio: Scarlett 2i2 USB", rate=rate, channels=2, fmt="S24_3LE", path=path, max_s=1.0,
              on_done=lambda st: done.set())
    st = mac.streams[-1]
    for i in range(0, len(k), 512):
        st.feed((k[i:i + 512] / 2 ** 23).astype("<f4"), Flags(overflow=i == 1024))
    assert done.wait(10)
    s = rec.status()
    assert not s["active"] and s["frames"] == rate and s["xruns"] == 1 and s["error"] is None
    assert s["stopped_reason"] == {"$t": "capture.stopped_max_duration"} and s["source_notes"] == []
    assert s["live"]["seq"] >= 1 and abs(s["level"]["rms_dbfs"][0] + 23.0) < 0.5          # sine RMS: -20 - 3 dB
    data, sr = sf.read(path, dtype="int32")
    assert sr == rate and data.shape == (rate, 2) and ((data >> 8) == k[:rate]).all()
    pcm = []
    while (item := q.get(timeout=5)) is not None:
        pcm.append(item)
    assert b"".join(pcm) == (k[:rate] >> 8).astype("<i2").tobytes()
    assert st.closed and cpa._open_streams == 0


def test_recorder_shows_notes_live(monkeypatch):
    sd = make_sd(["Windows WASAPI"], [{"name": "Line (USB Audio CODEC)", "api": 0, "busy": True}])
    monkeypatch.setitem(sys.modules, "sounddevice", sd)
    rec = capture.Recorder(lambda dev, r, ch, fmt: capture.open_source("portaudio", dev, r, ch, fmt))
    out = rec.start(device="Windows WASAPI: Line (USB Audio CODEC)", rate=48000, channels=2, fmt="S16_LE", path=None,
                    max_s=0.2)
    assert [n["$t"] for n in out["source_notes"]] == ["audio.exclusive_unavailable"]
    sd.streams[-1].feed(np.zeros((9600, 2), "<f4"))
    rec.stop()
    assert [n["$t"] for n in rec.status()["source_notes"]] == ["audio.exclusive_unavailable"]


# ─── Web interface ──────────────────────────────────────────────────────────

class _Src:
    def __init__(self):
        self.xruns, self.errors = 0, []

    def read(self, frames):
        time.sleep(0.01)
        return np.zeros((frames, 2), np.int64)

    def close(self):
        pass


def test_web_lists_portaudio_devices_and_picks_the_usb_one(tmp_path, mac):
    cfg = Config(data_dir=tmp_path / "d", state_dir=tmp_path / "s", min_free_gb=0.0, audio_backend="portaudio")
    opened = []
    with TestClient(create_app(cfg, source_factory=lambda dev, r, ch, fmt: opened.append(dev) or _Src())) as c:
        devs = c.get("/api/devices").json()
        assert [d["device"] for d in devs][1:] == ["Core Audio: USB Audio CODEC", "Core Audio: Scarlett 2i2 USB"]
        assert devs[1]["hostapi"] == "Core Audio" and devs[1]["default_rate"] == 48000
        assert c.get("/api/settings").json()["info"]["audio_backend"] == "portaudio"
        assert c.post("/api/monitor/start").status_code == 200
        c.post("/api/monitor/stop")
        assert opened == ["Core Audio: USB Audio CODEC"]
        p = c.post("/api/profiles", json={"name": "Turntable", "device": "Core Audio: Scarlett 2i2 USB"}).json()
        assert c.get(f"/api/profiles/{p['id']}").json()["connected"]
        q = c.post("/api/profiles", json={"name": "Other", "device": "Windows WASAPI: Line (USB Audio CODEC)"}).json()
        assert not c.get(f"/api/profiles/{q['id']}").json()["connected"]


def test_web_names_the_missing_package_instead_of_no_device(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "sounddevice", None)
    cfg = Config(data_dir=tmp_path / "d", state_dir=tmp_path / "s", min_free_gb=0.0, audio_backend="portaudio")
    with TestClient(create_app(cfg)) as c:
        assert c.get("/api/devices").json() == []
        r = c.post("/api/monitor/start", params={"lang": "de"})
        assert r.status_code == 409 and r.json()["detail"].startswith("Für Aufnahmen auf diesem System fehlt")
