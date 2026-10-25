"""Settings → Audio interface: validation, precedence (web interface > configuration file > device > default), the
values reaching the next recording, the device list for the selection, and the test button (PortAudio stand-in,
arecord stand-in and the test file)."""
import io
import sys
import threading
import time
from dataclasses import replace

import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient

from vinylcheck import audio_settings, capture, i18n
from vinylcheck import capture_portaudio as cpa
from vinylcheck.config import Config, load
from vinylcheck.db import DB
from vinylcheck.web import create_app

from test_capture_portaudio import make_sd

ALL_RATES = (44100, 48000, 88200, 96000, 176400, 192000)
# A desk as it often looks: a phone and a USB microphone, the computer's microphone, conferencing and routing
# software, and the interface the turntable is connected to (20 inputs, running at 96 kHz)
STUDIO = (["Core Audio"], [
    {"name": "Microphone of iPhone", "api": 0, "inputs": 1},
    {"name": "USB Microphone", "api": 0, "inputs": 1, "default": True},
    {"name": "MacBook Pro Microphone", "api": 0, "inputs": 1},
    {"name": "Microsoft Teams Audio", "api": 0, "inputs": 1},
    {"name": "BlackHole 2ch", "api": 0, "inputs": 2},
    {"name": "Studio Interface 20", "api": 0, "inputs": 20, "rate": 96000, "rates": ALL_RATES},
    {"name": "USB Audio CODEC", "api": 0, "inputs": 2, "rate": 44100, "rates": (44100, 48000)},
])
IFACE = "Core Audio: Studio Interface 20"
CODEC = "Core Audio: USB Audio CODEC"


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    cpa._cache.clear()
    cpa._caps_cache.clear()
    monkeypatch.setattr(cpa, "_open_streams", 0)
    yield
    cpa._cache.clear()
    cpa._caps_cache.clear()


def _feeding(sd, levels_db):
    """Streams of the stand-in deliver a 1 kHz sine per channel at `levels_db` (None = digital silence), several times
    faster than real time, until they are stopped."""
    base = sd.RawInputStream

    class Stream(base):
        def start(self):
            super().start()

            def run():
                ch, sr, pos = self.kw["channels"], self.kw["samplerate"], 0
                while self.active:
                    t = np.arange(pos, pos + 960) / sr
                    block = np.zeros((960, ch), "<f4")
                    for i, db in enumerate(levels_db[:ch]):
                        if db is not None:
                            block[:, i] = 10 ** (db / 20) * np.sin(2 * np.pi * 1000 * t)
                    pos += 960
                    self.feed(block)
                    time.sleep(0.004)
            threading.Thread(target=run, daemon=True).start()

    sd.RawInputStream = Stream
    return sd


@pytest.fixture
def studio(monkeypatch):
    """The desk above on macOS, with the rates Core Audio reports for each device."""
    sd = make_sd(*STUDIO)
    monkeypatch.setitem(sys.modules, "sounddevice", sd)
    monkeypatch.setattr(sys, "platform", "darwin")
    rates = {d["name"]: {"ranges": [(r, r) for r in d.get("rates", (d.get("rate", 48000),))],
                         "current": d.get("rate", 48000)} for d in STUDIO[1]}
    monkeypatch.setattr(cpa, "coreaudio_devices", lambda: rates)
    return sd


def _tone(seconds: float, sr: int) -> np.ndarray:
    """A test file: a 1 kHz tone at -20 dBFS over a little noise, stereo."""
    t = np.arange(int(seconds * sr)) / sr
    x = 0.1 * np.sin(2 * np.pi * 1000 * t) + np.random.default_rng(1).normal(0, 1e-3, len(t))
    return np.stack([x, x], axis=1)


def _cfg(tmp_path, **kw):
    return replace(Config(), data_dir=tmp_path / "data", state_dir=tmp_path / "state", min_free_gb=0.0, **kw)


def _aud(tmp_path, backend="portaudio", **kw):
    cfg = _cfg(tmp_path, **kw)
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    return audio_settings.AudioSettings(cfg, DB(cfg.db_path), backend)


# ─── Devices ────────────────────────────────────────────────────────────────

def test_devices_are_classified_and_automatic_takes_the_interface(studio):
    devs = capture.input_devices("portaudio", "auto")
    kinds = {d["name"]: d["kind"] for d in devs}
    assert kinds == {"Microphone of iPhone": "phone", "USB Microphone": "mono", "MacBook Pro Microphone": "builtin",
                     "Microsoft Teams Audio": "virtual", "BlackHole 2ch": "virtual",
                     "Studio Interface 20": "interface", "USB Audio CODEC": "interface"}
    assert capture.default_device(devs, 2)["device"] == CODEC                  # USB first among the interfaces
    without_codec = [d for d in devs if d["device"] != CODEC]
    assert capture.default_device(without_codec, 2)["device"] == IFACE        # never the (USB, default) microphone
    assert capture.default_device(without_codec, 22) is None                  # nothing suitable: ask the user
    for name, kind in [("Line In (Realtek(R) Audio)", "interface"), ("Microphone (Realtek(R) Audio)", "builtin"),
                       ("Microphone (USB Audio CODEC)", "interface"), ("Stereo Mix (Realtek(R) Audio)", "virtual"),
                       ("Realtek ASIO", "builtin"), ("ASIO4ALL v2", "virtual"), ("ZoomAudioDevice", "virtual"),
                       ("Zoom UAC-2", "interface"), ("Headset Microphone (USB)", "builtin")]:
        assert cpa.device_kind(name, 2) == kind, name


def test_asio_drivers_come_first_and_win_the_automatic_choice(monkeypatch):
    sd = make_sd(["Windows WASAPI", "ASIO"], [
        {"name": "Line (USB Audio CODEC)", "api": 0, "inputs": 2},
        {"name": "Analog (1+2) (Studio Interface 20)", "api": 0, "inputs": 2},
        {"name": "ASIO Studio Interface", "api": 1, "inputs": 20, "rate": 48000, "rates": (44100, 48000)},
        {"name": "Realtek ASIO", "api": 1, "inputs": 2}])
    monkeypatch.setitem(sys.modules, "sounddevice", sd)
    devs = capture.input_devices("portaudio", "auto")
    assert [d["device"] for d in devs][:2] == ["ASIO: ASIO Studio Interface", "ASIO: Realtek ASIO"]
    assert capture.default_device(devs, 2)["device"] == "ASIO: ASIO Studio Interface"
    # asio = false: WASAPI only, the USB device is chosen
    cpa._cache.clear()
    devs = capture.input_devices("portaudio", False)
    assert all(d["hostapi"] == "Windows WASAPI" for d in devs)
    assert capture.default_device(devs, 2)["device"] == "Windows WASAPI: Line (USB Audio CODEC)"
    # ASIO rates come from the driver (ASIOCanSampleRate through PortAudio's format check)
    asio = next(d for d in capture.input_devices("portaudio", True) if d["name"] == "ASIO Studio Interface")
    assert cpa.device_caps(asio) == {"rates": [44100, 48000], "current_rate": 48000, "formats": None}
    # A rate the driver refuses: the hint points to its control panel
    with pytest.raises(capture.CaptureError) as ei:
        capture.open_source("portaudio", "ASIO: ASIO Studio Interface", 96000, 2, "S24_3LE", asio="auto")
    assert ei.value.message["$p"]["hint"] == {"$t": "audioset.hint_rate_asio"}
    assert "control panel" in str(ei.value)


def test_wasapi_rates_and_formats_from_the_exclusive_format_check(monkeypatch):
    sd = make_sd(["Windows WASAPI"], [{"name": "Line (USB Audio CODEC)", "api": 0, "rate": 44100,
                                       "rates": (44100, 48000), "formats": ("int16",)}])
    monkeypatch.setitem(sys.modules, "sounddevice", sd)
    dev = capture.input_devices("portaudio")[0]
    assert cpa.device_caps(dev, exclusive=True) == {"rates": [44100, 48000], "current_rate": 44100,
                                                    "formats": ["S16_LE"]}
    assert cpa.device_caps(dev, exclusive=False)["rates"] == [44100]         # shared: the mixer's rate only


def test_asio_switch_on_windows(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    for available in (False, True):
        monkeypatch.setattr(cpa, "asio_available", lambda: available)
        aud = _aud(tmp_path / str(available), fake_source="x.wav")
        page = aud.page()
        assert page["windows"] and page["asio"]["available"] is available
    assert aud.system() == "wasapi"
    monkeypatch.setenv("SD_ENABLE_ASIO", "1")
    monkeypatch.setitem(sys.modules, "sounddevice", make_sd(["ASIO"], []))
    assert aud.system() == "wasapi_asio"
    aud.save({"asio": False})
    assert aud.system() == "wasapi" and aud.effective()["asio"] is False
    monkeypatch.setattr(sys, "platform", "darwin")
    assert not aud.page()["windows"] and aud.page()["asio"] is None


def test_asio_setting_in_the_configuration_is_checked(tmp_path):
    with pytest.raises(ValueError, match="asio"):
        _aud(tmp_path, asio="sometimes")


# ─── Precedence ─────────────────────────────────────────────────────────────

def test_defaults_follow_the_device_on_portaudio(studio, tmp_path):
    aud = _aud(tmp_path)
    e = aud.effective()
    assert e["device"] == CODEC and e["sources"]["device"] == "auto"
    assert (e["rate"], e["sample_format"]) == (44100, "S24_3LE")              # its current rate, 24 bit
    assert e["sources"] == {"device": "auto", "wasapi_exclusive": "default", "asio": "default", "rate": "device",
                                 "sample_format": "device", "inputs": "default"}
    e = aud.effective(IFACE)
    assert (e["rate"], e["sample_format"], e["inputs"], e["capture_channels"]) == (96000, "S24_3LE", [1, 2], 0)
    assert e["sources"]["device"] == "request"
    # A mono microphone keeps the built-in defaults
    e = aud.effective("Core Audio: USB Microphone")
    assert (e["rate"], e["sample_format"]) == (48000, "S16_LE")


def test_configuration_file_beats_the_device_and_the_interface_beats_both(studio, tmp_path):
    toml = tmp_path / "config.toml"
    toml.write_text('rate = 48000\nsample_format = "S16_LE"\ncapture_channels = 6\n')
    cfg = replace(load(str(toml)), data_dir=tmp_path / "data", state_dir=tmp_path / "state")
    assert {"rate", "sample_format", "capture_channels"} <= cfg.file_keys
    cfg.state_dir.mkdir(parents=True)
    aud = audio_settings.AudioSettings(cfg, DB(cfg.db_path), "portaudio")
    e = aud.effective(IFACE)
    assert (e["rate"], e["sample_format"]) == (48000, "S16_LE")                # set in the file, even = the default
    assert e["sources"]["rate"] == e["sources"]["sample_format"] == "config"
    aud.save(aud.merge({"rate": 88200, "sample_format": 32, "inputs": [3, 4], "device": IFACE}))
    e = aud.effective()
    assert (e["device"], e["rate"], e["sample_format"]) == (IFACE, 88200, "S32_LE")
    assert (e["inputs"], e["capture_channels"]) == ([3, 4], 6)        # the file's capture_channels still counts
    assert set(e["sources"].values()) >= {"ui"}
    assert aud.source_kwargs(IFACE) == {"capture_channels": 6, "input_channels": (3, 4), "wasapi_exclusive": True,
                                        "asio": "auto"}
    aud.save(aud.merge({"rate": None, "inputs": [19, 20]}))                    # null: back to the file
    e = aud.effective()
    assert (e["rate"], e["capture_channels"]) == (48000, 20)
    bad = tmp_path / "bad.toml"
    bad.write_text("file_keys = []\n")
    with pytest.raises(ValueError, match="file_keys"):                         # not a configuration key
        load(str(bad))


def test_raspberry_pi_keeps_its_configuration(tmp_path, monkeypatch):
    """arecord: no device defaults, the configured values apply as before; the page still reads the card's rates."""
    stream0 = ("USB Audio\n\nPlayback:\n  Status: Stop\n    Channels: 2\n    Rates: 44100\n\nCapture:\n"
               "  Status: Stop\n  Interface 2\n    Altset 1\n    Format: S16_LE\n    Channels: 2\n"
               "    Rates: 44100, 48000\n    Altset 2\n    Format: S24_3LE\n    Channels: 4\n    Rates: 8000 - 96000\n")
    card = {"card": 1, "id": "CODEC", "name": "USB Audio CODEC", "driver": "USB-Audio", "usb": True,
            "alsa": "hw:CARD=CODEC,DEV=0", "info": stream0}
    monkeypatch.setattr(capture, "list_devices", lambda: [card])
    aud = _aud(tmp_path, backend="alsa")
    e = aud.effective()
    assert (e["device"], e["rate"], e["sample_format"], e["capture_channels"]) == ("hw:CARD=CODEC,DEV=0", 48000,
                                                                                     "S16_LE", 0)
    assert e["sources"]["rate"] == e["sources"]["sample_format"] == "default"
    page = aud.page()
    assert page["system"] == "alsa" and page["options"]["rates"] == [44100, 48000, 88200, 96000]
    assert page["options"]["pairs"] == [[1, 2], [3, 4]] and page["devices"][0]["channels"] == 4
    assert page["options"]["native_formats"] == ["S16_LE", "S24_3LE"]


# ─── API ────────────────────────────────────────────────────────────────────

def test_license_page_names_the_asio_trademark(tmp_path):
    with TestClient(create_app(_cfg(tmp_path, fake_source="x.wav"))) as c:
        head = c.get("/licenses").text.split("=" * 100)[0]
        assert "ASIO is a trademark and software of Steinberg Media Technologies GmbH." in head
        head = c.get("/licenses", params={"lang": "de"}).text.split("=" * 100)[0]
        assert "ASIO ist eine Marke und Software der Steinberg Media Technologies GmbH." in head


def test_api_validates_and_stores(tmp_path):
    with TestClient(create_app(_cfg(tmp_path, fake_source="x.wav"))) as c:
        bad = c.put("/api/audio", json={"rate": 12345, "sample_format": "FLOAT", "inputs": [1, 1], "nope": 1,
                                        "wasapi_exclusive": "maybe", "device": 5})
        assert bad.status_code == 422
        fields = bad.json()["detail"]["fields"]
        assert set(fields) == {"rate", "sample_format", "inputs", "nope", "wasapi_exclusive", "device"}
        assert fields["rate"].startswith("Choose one of these sample rates: 44100, 48000")
        de = c.put("/api/audio", json={"inputs": [0, 2]}, params={"lang": "de"}).json()
        assert de["detail"]["fields"]["inputs"].startswith("2 verschiedene Eingänge")
        assert c.get("/api/audio").json()["stored"] == {}                        # nothing stored on errors
        ok = c.put("/api/audio", json={"rate": "96000", "sample_format": "S24_3LE", "inputs": [3, 4]}).json()
        assert ok["stored"] == {"rate": 96000, "sample_format": "S24_3LE", "inputs": [3, 4]}
        assert ok["effective"]["sources"]["rate"] == "ui"
        info = c.get("/api/settings").json()["info"]
        assert (info["rate"], info["format"]) == (96000, "S24_3LE") and info["audio_system"]
        assert c.delete("/api/audio").json()["stored"] == {}
        assert c.get("/api/settings").json()["info"]["rate"] == 48000


def test_changes_apply_at_the_next_recording_and_never_during_one(tmp_path):
    sr = 96000
    audio = _tone(5.0, sr)
    wav = tmp_path / "side96.wav"
    sf.write(wav, audio, sr, subtype="PCM_24")
    with TestClient(create_app(_cfg(tmp_path, fake_source=str(wav), fake_speed=50.0))) as c:
        # Configuration: 48 kHz, but the test file runs at 96 kHz → the level check is refused with the reason
        r = c.post("/api/monitor/start")
        assert r.status_code == 409 and "96000" in r.json()["detail"]
        assert c.put("/api/audio", json={"rate": 96000, "sample_format": 24}).status_code == 200
        st = c.post("/api/monitor/start").json()
        assert (st["rate"], st["format"]) == (96000, "S24_3LE")
        locked = c.put("/api/audio", json={"rate": 48000}, params={"lang": "de"})
        assert locked.status_code == 409 and "Pegelprüfung" in locked.json()["detail"]
        assert c.post("/api/audio/test").status_code == 409
        assert c.get("/api/audio").json()["locked"]
        c.post("/api/monitor/stop")
        rid = c.post("/api/records", json={"artist": "Test", "title": "Rate"}).json()["id"]
        rec = c.post("/api/recordings/start", json={"record_id": rid, "kind": "before"}).json()["recording_id"]
        deadline = time.time() + 30
        while c.get("/api/status").json()["recorder"]["active"] and time.time() < deadline:
            time.sleep(0.05)
        r = c.get(f"/api/recordings/{rec}").json()
        assert (r["samplerate"], r["format"]) == (96000, "S24_3LE")
        info = sf.info(r["path"])
        assert (info.samplerate, info.subtype) == (96000, "PCM_24")
        deadline += 60                                     # let the analysis finish before the folder goes
        while c.get(f"/api/recordings/{rec}").json()["status"] not in ("analyzed", "error") and time.time() < deadline:
            time.sleep(0.2)


def test_the_level_check_listens_on_the_profile_device(studio, tmp_path):
    _feeding(studio, [-20.0, -20.0])
    with TestClient(create_app(_cfg(tmp_path, audio_backend="portaudio"))) as c:
        c.put("/api/audio", json={"device": IFACE})
        st = c.post("/api/monitor/start").json()
        assert st["device"] == IFACE and st["rate"] == 96000
        c.post("/api/monitor/stop")
        p = c.post("/api/profiles", json={"name": "Deck", "device": CODEC}).json()
        c.post(f"/api/profiles/{p['id']}/activate")
        st = c.post("/api/monitor/start").json()
        assert st["device"] == CODEC and st["rate"] == 44100              # the profile's device, at its own rate
        c.post("/api/monitor/stop")
        page = c.get("/api/audio").json()
        assert page["profile"] == {"name": "Deck", "device": CODEC}


def test_page_lists_interfaces_first_and_offers_what_the_device_can(studio, tmp_path):
    with TestClient(create_app(_cfg(tmp_path, audio_backend="portaudio"))) as c:
        page = c.get("/api/audio", params={"device": IFACE}).json()
        assert page["system"] == "coreaudio" and page["selected"] == IFACE and page["auto"] == CODEC
        names = [d["device"] for d in page["devices"] if d["kind"] == "interface"]
        assert names == [IFACE, CODEC]
        iface = next(d for d in page["devices"] if d["device"] == IFACE)
        assert (iface["channels"], iface["current_rate"]) == (20, 96000)
        o = page["options"]
        assert o["rates"] == list(ALL_RATES) and o["rates_known"] and o["current_rate"] == 96000
        assert o["pairs"][0] == [1, 2] and o["pairs"][-1] == [19, 20] and len(o["pairs"]) == 10
        assert page["effective"]["rate"] == 96000 and page["stored"] == {}           # nothing saved by looking
        o = c.get("/api/audio", params={"device": CODEC}).json()["options"]
        assert o["rates"] == [44100, 48000] and o["pairs"] == [[1, 2]]


# ─── Test button ────────────────────────────────────────────────────────────

def test_test_button_with_portaudio(studio, tmp_path):
    _feeding(studio, [None, None, -6.0, -12.0])
    cfg = _cfg(tmp_path, audio_backend="portaudio")
    with TestClient(create_app(cfg)) as c:
        r = c.post("/api/audio/test", json={"device": IFACE, "rate": 48000, "inputs": [3, 4]}).json()
        assert r["ok"] and r["error"] is None, r
        assert (r["rate"], r["bits"], r["inputs"], r["capture_channels"]) == (48000, 24, [3, 4], 4)
        assert abs(r["peak_dbfs"][0] + 6.0) < 0.2 and abs(r["peak_dbfs"][1] + 12.0) < 0.2
        assert 0.9 <= r["seconds"] <= 1.2
        assert studio.streams[-1].kw["channels"] == 4 and studio.streams[-1].closed
        assert not c.get("/api/status").json()["recorder"]["active"]
        assert c.get("/api/audio").json()["stored"] == {}                       # testing saves nothing
        assert not list((tmp_path / "data").rglob("*.wav"))                     # and writes no file
        # Refusals come back as the source's message, in the reader's language
        r = c.post("/api/audio/test", json={"device": CODEC, "rate": 96000}).json()
        assert not r["ok"] and r["error"].startswith("“Core Audio: USB Audio CODEC” cannot record at 96000 Hz")
        r = c.post("/api/audio/test", json={"device": "Core Audio: Turntable"},
                   params={"lang": "de"}).json()
        assert not r["ok"] and r["error"].startswith("Aufnahmegerät „Core Audio: Turntable“ nicht gefunden")
        r = c.post("/api/audio/test", json={"device": CODEC, "inputs": [3, 4]}).json()
        assert not r["ok"] and "the device offers 2" in r["error"]
        assert c.post("/api/audio/test", json={"rate": 1}).status_code == 422


def test_test_button_with_the_test_file(tmp_path):
    audio = _tone(3.0, 48000)
    wav = tmp_path / "side.wav"
    sf.write(wav, audio, 48000, subtype="PCM_16")
    with TestClient(create_app(_cfg(tmp_path, fake_source=str(wav), fake_speed=20.0))) as c:
        r = c.post("/api/audio/test").json()
        assert r["ok"] and r["device"] == "file:side.wav" and len(r["peak_dbfs"]) == 2
        assert abs(max(r["peak_dbfs"]) + 20.0) < 0.5
        r = c.post("/api/audio/test", json={"rate": 44100}).json()
        assert not r["ok"] and "44100" in r["error"]


class _Arecord:
    """Stands in for the arecord process: raw samples on stdout, messages on stderr, then it ends."""
    calls: list = []

    def __init__(self, out: bytes, err: bytes):
        self.stdout, self.stderr = io.BytesIO(out), io.BytesIO(err)

    def poll(self):
        return 1

    def wait(self, timeout=None):
        return 1

    def terminate(self):
        pass

    kill = terminate


@pytest.mark.parametrize("out_s,err,key", [
    (0.0, b"arecord: set_params:1416: Sample format non available\nAvailable formats:\n- S16_LE\n",
     "audioset.alsa_format"),
    (0.0, b"arecord: set_params:1411: Channels count non available\n", "audioset.alsa_channels"),
    (0.5, b"Warning: rate is not accurate (requested = 96000Hz, got = 48000Hz)\n         please, try the plug plugin\n",
     "audioset.alsa_rate"),
    (0.0, b"arecord: main:850: audio open error: Device or resource busy\n", "audio.device_busy"),
    (1.2, b"Recording raw data 'stdin' : Signed 16 bit Little Endian, Rate 48000 Hz, Stereo\n", None),
])
def test_test_button_with_arecord(tmp_path, monkeypatch, out_s, err, key):
    card = {"alsa": "hw:CARD=CODEC,DEV=0", "name": "USB Audio CODEC", "usb": True, "info": ""}
    monkeypatch.setattr(capture, "list_devices", lambda: [card])
    monkeypatch.setattr(capture.shutil, "which", lambda name: "/usr/bin/arecord")
    calls = []
    frames = int(48000 * out_s)
    out = (np.full((frames, 4), 1000, "<i2")).tobytes()

    def popen(args, **kw):
        calls.append(args)
        return _Arecord(out, err)

    monkeypatch.setattr(capture.subprocess, "Popen", popen)
    with TestClient(create_app(_cfg(tmp_path, audio_backend="alsa"))) as c:
        r = c.post("/api/audio/test", json={"inputs": [3, 4], "rate": 48000}).json()
    assert calls[-1][calls[-1].index("-c") + 1] == "4" and calls[-1][calls[-1].index("-r") + 1] == "48000"
    if key is None:
        assert r["ok"] and r["seconds"] == 1.0 and abs(r["peak_dbfs"][0] - 20 * np.log10(1000 / 32768)) < 0.1
    else:
        assert not r["ok"]
        msg = audio_settings.arecord_message(err.decode().splitlines()[0], "hw:CARD=CODEC,DEV=0",
                                             {"sample_format": "S16_LE", "capture_channels": 4, "channels": 2,
                                              "rate": 48000})
        assert msg["$t"] == key and r["error"] == i18n.render(msg, "en")


def test_arecord_messages_are_kept_when_it_exits_at_once():
    """arecord's complaint arrives just before it exits; close() waits for it (the reason must not get lost)."""
    src = capture.ArecordSource.__new__(capture.ArecordSource)
    src.proc, src.errors, src.xruns = _Arecord(b"", b"arecord: busy\n"), [], 0
    slow = threading.Thread(target=lambda: (time.sleep(0.2), src._stderr()))
    src._err_reader = slow
    slow.start()
    src.close()
    assert src.errors == ["arecord: busy"]
