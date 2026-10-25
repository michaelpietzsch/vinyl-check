"""Recording from the turntable's audio interface (ALSA) with live levels.

Audio is captured with `arecord` straight from the `hw:` device, without a sound server and without
`plughw`, so there is no format conversion and no resampling. The raw samples pass through Python: they are
written to a WAV file while level, clipping and dropouts (arecord reports "overrun") are tracked. A recording
with dropouts is useless for comparisons and is flagged.

While monitoring or recording, the live meter (measure.LiveMeter) measures fundamental, level and distortion
per channel five times a second, for aligning the turntable while a test record plays. Listeners get a queue of
16-bit PCM blocks from `Recorder.tap()` (web.py streams it to the browser).

For development and tests `FileSource` replaces the turntable with a WAV file that is "played" in (optionally
accelerated) real time.

On macOS and Windows (and Linux without arecord) the samples come from PortAudio instead, with the same rules: no
resampling, the configured sample format, dropouts counted (capture_portaudio.SoundDeviceSource). Which backend is
used follows the `audio_backend` setting, see "Audio backends" at the end of this file.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

import numpy as np
import soundfile as sf

from . import i18n
from .measure import LiveMeter
from .skips import EndGrooveWatch

FORMATS = {
    # ALSA format: (bytes per sample, soundfile subtype, full scale)
    "S16_LE": (2, "PCM_16", 32768.0),
    "S24_3LE": (3, "PCM_24", 8388608.0),
    "S32_LE": (4, "PCM_32", 2147483648.0),
}
CHUNK_S = 0.1
log = logging.getLogger("vinylcheck.capture")


class CaptureError(i18n.MessageError, RuntimeError):
    """A recording problem the user should see (an i18n message, see i18n.MessageError)."""


def list_devices(proc_asound: Path = Path("/proc/asound")) -> list[dict]:
    """ALSA cards that can record, read from /proc/asound (no arecord call)."""
    cards = proc_asound / "cards"
    if not cards.exists():
        return []
    out = []
    pat = re.compile(r"^\s*(\d+)\s+\[(\S+)\s*\]:\s*(.+?)\s+-\s+(.+)$", re.M)
    for m in pat.finditer(cards.read_text(errors="replace")):
        idx, cid, driver, name = m.groups()
        cdir = proc_asound / f"card{idx}"
        cap = sorted(p.name for p in cdir.glob("pcm*c"))
        if not cap:
            continue
        dev = int(re.sub(r"\D", "", cap[0]) or 0)
        stream = cdir / "stream0"
        out.append({
            "card": int(idx), "id": cid, "name": name.strip(), "driver": driver.strip(),
            "usb": "USB" in driver, "alsa": f"hw:CARD={cid},DEV={dev}",
            "info": stream.read_text(errors="replace")[:2000] if stream.exists() else "",
        })
    return out


def _decode(raw: bytes, fmt: str, channels: int) -> np.ndarray:
    """Raw bytes → integer array (frames, channels) in the format's native value range."""
    if fmt == "S16_LE":
        a = np.frombuffer(raw, dtype="<i2").astype(np.int32)
    elif fmt == "S32_LE":
        a = np.frombuffer(raw, dtype="<i4").astype(np.int64)
    elif fmt == "S24_3LE":
        b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        a = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        a = np.where(a & 0x800000, a - 0x1000000, a)        # sign-extend 24 bit
    else:
        raise ValueError(fmt)
    return a.reshape(-1, channels)


def channel_pick(channels: int, capture_channels: int = 0,
                 input_channels: tuple[int, ...] = ()) -> tuple[int, list[int]]:
    """How many channels the device delivers and which of them (0-based) are left/right.

    Some interfaces only record at their full channel count on the `hw:` device (a Soundcraft Notepad-12FX, for
    example, need 4 and answer "Channels count non available" for 2). Then everything is captured and the pair is
    picked here; everything downstream (WAV, live meter, analysis) only sees `channels` channels.
    Raises CaptureError if `input_channels` does not fit.
    """
    cap = capture_channels or channels
    pick = tuple(input_channels) or tuple(range(1, channels + 1))
    if len(pick) != channels or not all(1 <= i <= cap for i in pick):
        raise CaptureError(i18n.msg("capture.channel_mismatch", pick=str(list(pick)), channels=channels, cap=cap))
    return cap, [i - 1 for i in pick]


def wav_data_offset(path) -> int | None:
    """Byte offset of the samples in a WAV file, also while it is still being written (the length fields in the
    header are only correct after closing; the file size is what counts)."""
    try:
        with open(path, "rb") as f:
            head = f.read(12)
            if len(head) < 12 or head[:4] != b"RIFF" or head[8:12] != b"WAVE":
                return None
            pos = 12
            while True:
                f.seek(pos)
                h = f.read(8)
                if len(h) < 8:
                    return None
                cid, size = h[:4], int.from_bytes(h[4:8], "little")
                if cid == b"data":
                    return pos + 8
                pos += 8 + size + (size & 1)
    except OSError:
        return None


def pcm16(raw: bytes, fmt: str, channels: int) -> bytes:
    """Raw data of a capture format → 16-bit little-endian (for listening in)."""
    if fmt == "S16_LE":
        return raw
    return (_decode(raw, fmt, channels) >> (8 * FORMATS[fmt][0] - 16)).astype("<i2").tobytes()


def _fsync(path) -> None:
    """Make sure a recording is really on disk (file and directory entry) before it counts as finished."""
    try:
        fd = os.open(str(path), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        dfd = os.open(str(Path(path).parent), os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass


class ArecordSource:
    """Samples from an ALSA device via an `arecord` subprocess (raw PCM on stdout, messages on stderr)."""

    def __init__(self, device: str, rate: int, channels: int, fmt: str,
                 capture_channels: int = 0, input_channels: tuple[int, ...] = ()):
        if not shutil.which("arecord"):
            raise CaptureError(i18n.msg("capture.arecord_missing"))
        self.fmt, self.channels = fmt, channels
        self.cap, self.pick = channel_pick(channels, capture_channels, input_channels)
        self.bytes_per_frame = FORMATS[fmt][0] * self.cap
        self.xruns = 0
        self.errors: list[str] = []
        self.proc = subprocess.Popen(
            ["arecord", "-D", device, "-f", fmt, "-r", str(rate), "-c", str(self.cap),
             "-t", "raw", "--buffer-time=500000", "--period-time=100000"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        self._err_reader = threading.Thread(target=self._stderr, daemon=True)
        self._err_reader.start()

    def _stderr(self):
        for line in iter(self.proc.stderr.readline, b""):
            txt = line.decode(errors="replace").strip()
            if "overrun" in txt.lower() or "xrun" in txt.lower():
                self.xruns += 1
            elif txt and not txt.startswith("Recording"):
                self.errors.append(txt)

    def read(self, frames: int) -> np.ndarray | None:
        """Up to `frames` frames of the picked channels, or None at the end of the stream."""
        want = frames * self.bytes_per_frame
        buf = bytearray()
        while len(buf) < want:
            chunk = self.proc.stdout.read(want - len(buf))
            if not chunk:
                break
            buf += chunk
        usable = len(buf) - len(buf) % self.bytes_per_frame
        if usable == 0:
            return None
        a = _decode(bytes(buf[:usable]), self.fmt, self.cap)
        return a if self.pick == list(range(self.cap)) else a[:, self.pick]

    def close(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        # arecord's last words (why it refused the device or the format) arrive just before it exits: collect them
        # before the caller reads `errors`
        self._err_reader.join(1.0)


class FileSource:
    """Plays a WAV file as if it were the turntable (development and tests)."""

    def __init__(self, path: str, rate: int, channels: int, fmt: str, speed: float = 1.0,
                 loop: bool = False):
        self.f = sf.SoundFile(path)
        if self.f.samplerate != rate or self.f.channels != channels:
            raise CaptureError(i18n.msg("capture.test_file_mismatch", file_rate=self.f.samplerate,
                                        file_channels=self.f.channels, rate=rate, channels=channels))
        self.scale = FORMATS[fmt][2]
        self.speed, self.loop, self.rate = speed, loop, rate
        self.xruns = 0
        self.errors: list[str] = []
        self.t0 = time.monotonic()
        self.pos = 0

    def read(self, frames: int) -> np.ndarray | None:
        data = self.f.read(frames, dtype="float64", always_2d=True)
        if len(data) == 0 and self.loop:
            self.f.seek(0)
            data = self.f.read(frames, dtype="float64", always_2d=True)
        if len(data) == 0:
            return None
        self.pos += len(data)
        wait = self.t0 + self.pos / self.rate / self.speed - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        return np.clip(np.round(data * self.scale), -self.scale, self.scale - 1).astype(np.int64)

    def close(self):
        self.f.close()


class Recorder:
    """Exactly one recording (or level check) at a time, run in a background thread.

    `state` holds the live status (levels, clipping, dropouts, expected end of the side); `status()` returns a
    snapshot of it for the API and the WebSocket.
    """

    def __init__(self, source_factory):
        self._factory = source_factory
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.state: dict = {"active": False}
        self.levels: deque = deque(maxlen=50)
        self._taps: list[queue.Queue] = []

    def tap(self, max_chunks: int = 50) -> queue.Queue:
        """Listen in: a queue of 16-bit PCM blocks (bytes, channels interleaved) of the running recording, `None` at
        the end. If the consumer cannot keep up, the oldest blocks are dropped; the recording itself never waits."""
        q: queue.Queue = queue.Queue(maxsize=max_chunks)
        with self._lock:
            self._taps.append(q)
        return q

    def untap(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._taps:
                self._taps.remove(q)

    def _offer(self, item) -> None:
        for q in list(self._taps):
            if q.full():
                try:
                    q.get_nowait()
                except queue.Empty:
                    pass
            try:
                q.put_nowait(item)
            except queue.Full:
                pass

    @property
    def active(self) -> bool:
        return bool(self.state.get("active"))

    def start(self, *, device: str, rate: int, channels: int, fmt: str, path: Path | None,
              max_s: float, meta: dict | None = None, on_done=None, endstop_rpm: float | None = None,
              music_thr_dbfs: float | None = None, side_length_s: float | None = None, on_event=None) -> dict:
        """Start recording to `path` (None = level check only) for at most `max_s` seconds.

        endstop_rpm: stop by itself once the stylus runs in the run-out groove (recordings only).
        music_thr_dbfs: from this level (held for 1 s) the music counts as started; side_length_s: expected length
        of the side → "expected end" and a `side_end` event via on_event(name, state).
        on_done(state) is called from the recording thread when it ends. Raises CaptureError if one is running.
        """
        with self._lock:
            if self.active:
                raise CaptureError(i18n.msg("capture.busy"))
            src = self._factory(device, rate, channels, fmt)
            self._stop.clear()
            self.levels.clear()
            self.state = {"active": True, "mode": "record" if path else "monitor", "device": device,
                          "rate": rate, "channels": channels, "format": fmt,
                          "path": str(path) if path else None, "started": time.time(),
                          "frames": 0, "clip_samples": 0, "xruns": 0, "meta": meta or {},
                          "peak_hold_dbfs": [-120.0] * channels,
                          "clip_bursts": 0, "last_clip_s": None,
                          "music_thr_dbfs": music_thr_dbfs, "music_start_s": None,
                          "side_length_s": side_length_s, "expected_end_s": None, "side_end_notified": False,
                          "source_notes": list(getattr(src, "notes", ()))}
            self._thread = threading.Thread(target=self._run, args=(src, path, max_s, on_done, endstop_rpm, on_event),
                                            daemon=True)
            self._thread.start()
            return self.status()

    def stop(self, timeout: float = 10.0) -> dict:
        self._stop.set()
        t = self._thread
        if t:
            t.join(timeout)
        return self.status()

    def status(self) -> dict:
        s = dict(self.state)
        if s.get("active"):
            s["elapsed_s"] = round(s["frames"] / s["rate"], 2)
        s["level"] = self.levels[-1] if self.levels else None
        return s

    def _run(self, src, path, max_s, on_done, endstop_rpm=None, on_event=None):
        bytes_, subtype, full = FORMATS[self.state["format"]]
        rate, ch = self.state["rate"], self.state["channels"]
        frames = int(rate * CHUNK_S)
        live = LiveMeter(rate, ch)
        # Detect the run-out groove live and stop by itself (recordings only, not level checks or calibration)
        watch = EndGrooveWatch(rate, endstop_rpm) if (path and endstop_rpm) else None
        writer = sf.SoundFile(str(path), "w", samplerate=rate, channels=ch, subtype=subtype,
                              format="WAV") if path else None
        err = None
        thr = self.state.get("music_thr_dbfs")
        above = 0                                            # consecutive blocks above the music threshold
        try:
            while not self._stop.is_set():
                data = src.read(frames)
                if data is None:
                    break
                if writer:
                    # soundfile expects int32 at full 32-bit scale
                    writer.write((data.astype(np.int64) << (32 - 8 * bytes_)).astype(np.int32))
                if self._taps:
                    self._offer((data.astype(np.int64) >> (8 * bytes_ - 16)).astype("<i2").tobytes())
                x = data.astype(np.float64) / full
                peak = np.max(np.abs(x), axis=0)
                rms = np.sqrt(np.mean(x ** 2, axis=0))
                clip = int(np.sum(np.abs(data) >= full - 1))
                st = self.state
                st["frames"] += len(data)
                st["clip_samples"] += clip
                if clip:
                    st["clip_bursts"] += 1
                    st["last_clip_s"] = round(st["frames"] / rate, 1)
                st["xruns"] = src.xruns
                # PortAudio notes (exclusive mode unavailable, only digital silence) show up live in the status
                if len(getattr(src, "notes", ())) != len(st["source_notes"]):
                    st["source_notes"] = list(src.notes)
                pk_db = [round(float(20 * np.log10(max(p, 1e-9))), 1) for p in peak]
                st["peak_hold_dbfs"] = [max(a, b) for a, b in zip(st["peak_hold_dbfs"], pk_db)]
                self.levels.append({"t": round(st["frames"] / rate, 2), "peak_dbfs": pk_db,
                                    "rms_dbfs": [round(float(20 * np.log10(max(r, 1e-9))), 1) for r in rms],
                                    "clip": clip})
                try:
                    st["live"] = live.feed(x)
                except Exception as e:  # noqa: BLE001 — the live meter must never disturb the recording
                    st["live"] = {"error": str(e)}
                if thr is not None and st["music_start_s"] is None:
                    above = above + 1 if float(20 * np.log10(max(float(np.mean(rms)), 1e-9))) >= thr else 0
                    if above >= int(round(1.0 / CHUNK_S)):
                        st["music_start_s"] = round(st["frames"] / rate - 1.0, 2)
                        if st.get("side_length_s"):
                            st["expected_end_s"] = round(st["music_start_s"] + float(st["side_length_s"]), 1)
                if (st.get("expected_end_s") and not st["side_end_notified"] and st["frames"] / rate >= st["expected_end_s"]):
                    st["side_end_notified"] = True
                    if on_event:
                        try:
                            on_event("side_end", dict(st))
                        except Exception:  # noqa: BLE001
                            pass
                if watch is not None and watch.feed(x):
                    st["stopped_reason"] = i18n.msg("capture.stopped_end_groove")
                    if on_event:
                        try:
                            on_event("end_groove", dict(st))
                        except Exception:  # noqa: BLE001
                            pass
                    break
                if st["frames"] >= max_s * rate:
                    st["stopped_reason"] = i18n.msg("capture.stopped_max_duration")
                    break
        except Exception as e:  # noqa: BLE001 — the error ends up in the status and in the database
            err = str(e)
        finally:
            src.close()
            if writer:
                writer.close()
                _fsync(path)
            self._offer(None)
            st = self.state
            st.update({"active": False, "ended": time.time(), "error": err,
                       "source_errors": src.errors[-5:], "xruns": src.xruns,
                       "source_notes": list(getattr(src, "notes", ())),
                       "duration_s": round(st["frames"] / rate, 2)})
            if on_done:
                on_done(dict(st))


# ─── Audio backends ─────────────────────────────────────────────────────────
# "alsa": arecord on the hw: device (the Raspberry Pi, see ArecordSource). "portaudio": the sounddevice package with
# Core Audio (macOS), WASAPI and ASIO (Windows), see capture_portaudio. Both deliver the same integer samples, so
# everything after the source (WAV, live meter, analysis) is the same.

BACKENDS = ("auto", "alsa", "portaudio")


def resolve_backend(setting: str = "auto") -> str:
    """The backend for the `audio_backend` setting. auto = alsa on Linux when arecord is installed (the Raspberry Pi
    keeps working without any change), portaudio elsewhere. A Linux without arecord and without sounddevice stays on
    alsa, so that the message names the missing alsa-utils package."""
    if setting not in BACKENDS:
        raise ValueError(f"audio_backend must be one of {', '.join(BACKENDS)}, not {setting!r}")
    if setting != "auto":
        return setting
    if sys.platform.startswith("linux"):
        if shutil.which("arecord") or importlib.util.find_spec("sounddevice") is None:
            return "alsa"
    return "portaudio"


def input_devices(backend: str, asio="auto") -> list[dict]:
    """Capture devices for the device selection. Every entry has `device` (the value for the configuration and the
    turntable profile), `name` and `usb`; ALSA entries keep `alsa`, PortAudio entries add `hostapi`, `channels`,
    `default_rate` and `default`. An unusable backend gives an empty list (the reason: backend_problem)."""
    if backend == "alsa":
        return [dict(d, device=d["alsa"], backend="alsa") for d in list_devices()]
    from . import capture_portaudio
    try:
        return capture_portaudio.devices(asio)
    except Exception as e:  # noqa: BLE001 — pages that only show devices must work without PortAudio
        log.warning("PortAudio device list: %s", e)
        return []


def backend_problem(backend: str, asio="auto"):
    """Why the backend cannot record at all (an i18n message), or None."""
    if backend == "alsa":
        return None if shutil.which("arecord") else i18n.msg("capture.arecord_missing")
    from . import capture_portaudio
    try:
        capture_portaudio.load_sounddevice(asio)
    except CaptureError as e:
        return e.message
    return None


def default_device(devices: list[dict], channels: int) -> dict | None:
    """The device to use when none is configured: an audio interface with enough inputs, an ASIO driver first (Windows:
    the most direct path to a studio interface's converter), then a USB device (the turntable or its interface), then
    the system's default input, then the first. Microphones, phones and virtual devices (PortAudio entries with
    another `kind`, see capture_portaudio.device_kind) are never taken: without a suitable device the user is asked to
    choose one. For ALSA (no channel information) this is the first USB card, else the first card, as before."""
    fit = [d for d in devices if d.get("kind", "interface") == "interface"
           and (d.get("channels") is None or d["channels"] >= channels)]
    return (next((d for d in fit if d.get("hostapi") == "ASIO"), None) or next((d for d in fit if d.get("usb")), None)
            or next((d for d in fit if d.get("default")), None) or (fit[0] if fit else None))


def open_source(backend: str, device: str, rate: int, channels: int, fmt: str, capture_channels: int = 0,
                input_channels: tuple[int, ...] = (), *, wasapi_exclusive: bool = True, asio="auto"):
    """The audio source of the backend (raises CaptureError if the device cannot be opened as configured)."""
    if backend == "alsa":
        return ArecordSource(device, rate, channels, fmt, capture_channels, input_channels)
    from .capture_portaudio import SoundDeviceSource
    return SoundDeviceSource(device, rate, channels, fmt, capture_channels, input_channels,
                             wasapi_exclusive=wasapi_exclusive, asio=asio)
