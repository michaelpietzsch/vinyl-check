"""Synthetic "records" with known clicks, for the tests and the development mode (fake audio source).

Layout of a side: (optional) silence before the stylus is set down → lead-in groove → tracks with pauses in
between → run-out groove. Groove noise everywhere. The music deliberately contains drum transients (hi-hat/
snare noise bursts, bass drum), so that the tests also measure false alarms in music.

All clicks are logged with time, level and channel (ground truth).

The second half simulates the test record used for turntable alignment (AAPT 1, see make_test_side) on a
turntable with adjustable faults (Rig).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy import signal


@dataclass
class Layout:
    """Durations (s) of the parts of a synthetic record side."""
    no_groove_s: float = 0.0     # recording already running, stylus still up
    lead_in_s: float = 5.0
    tracks: int = 3
    gap_s: float = 3.0
    lead_out_s: float = 5.0


@dataclass
class Truth:
    """Ground truth of a synthetic side: where the quiet parts, music and clicks are."""
    sr: int
    quiet: list[tuple[float, float]] = field(default_factory=list)   # (from, to) in s
    music: list[tuple[float, float]] = field(default_factory=list)
    groove: tuple[float, float] = (0.0, 0.0)
    clicks: list[dict] = field(default_factory=list)                  # t, amp_dbfs, ch, kind


def _db(x: float) -> float:
    """dB → linear amplitude factor."""
    return 10 ** (x / 20)


def _surface_noise(n: int, rng, level_dbfs: float) -> np.ndarray:
    """Pink-ish groove noise, left/right partly correlated: second-order low-pass from 3 kHz plus a small white
    component. Real vinyl measured on two turntables has its treble (above 6 kHz, 2.7 ms window) about 25 dB
    below the overall level (−83 to −86 dB at −58 to −67 dBFS); this noise matches that (−76 dB at −60 dBFS),
    so the stripe check treats synthetic clicks like real ones. A whiter noise (treble only 5 dB below the
    overall level) would hide them."""
    common = rng.standard_normal(n)
    out = np.empty((n, 2))
    b, a = signal.butter(2, 3000, fs=48000, btype="low")
    for c in range(2):
        w = 0.6 * common + 0.8 * rng.standard_normal(n)
        w = signal.lfilter(b, a, w) + 0.06 * w
        out[:, c] = w
    out *= _db(level_dbfs) / np.sqrt(np.mean(out ** 2))
    return out


def _fade(m: int, sr: int, ms: float = 8.0) -> np.ndarray:
    """Soft ending: without it, every cut-off signal would produce a click of its own."""
    f = np.ones(m)
    k = min(m, int(sr * ms / 1000))
    f[m - k:] = 0.5 * (1 + np.cos(np.linspace(0, np.pi, k)))
    return f


def _music(n: int, sr: int, rng, level_dbfs: float) -> np.ndarray:
    """Synthetic music: decaying chords with harmonics plus a drum pattern (noise bursts and a 60 Hz kick)."""
    t = np.arange(n) / sr
    out = np.zeros((n, 2))
    pos = 0
    while pos < n:  # chords
        ln = int(sr * rng.uniform(0.3, 0.6))
        seg = slice(pos, min(n, pos + ln))
        m = seg.stop - seg.start
        env = np.minimum(1, np.arange(m) / (0.01 * sr)) * np.exp(-np.arange(m) / (0.35 * sr)) * _fade(m, sr)
        for _ in range(3):
            f0 = rng.uniform(110, 440)
            pan = rng.uniform(0.3, 0.7)
            for h in range(1, 5):
                tone = np.sin(2 * np.pi * f0 * h * t[seg] + rng.uniform(0, 6.3)) / h
                out[seg, 0] += tone * env * pan
                out[seg, 1] += tone * env * (1 - pan)
        pos += ln
    beat = int(sr * 0.5)  # drums
    for p in range(0, n, beat):
        ln = min(int(0.08 * sr), n - p)
        burst = rng.standard_normal((ln, 2)) * (np.exp(-np.arange(ln) / (0.015 * sr)) * _fade(ln, sr))[:, None]
        out[p:p + ln] += burst * 0.8
        ln = min(int(0.12 * sr), n - p)
        kick = np.sin(2 * np.pi * 60 * np.arange(ln) / sr) * np.exp(-np.arange(ln) / (0.04 * sr)) * _fade(ln, sr)
        out[p:p + ln] += kick[:, None] * 1.5
    out *= _db(level_dbfs) / np.sqrt(np.mean(out ** 2))
    return out


def click_shape(rng, sr: int, amp: float, kind: str = "click") -> np.ndarray:
    """Waveform of a click (3–20 samples, decaying, alternating) or a pop (4 ms bump with a sharp edge)."""
    if kind == "pop":
        # Loud pop: a 4 ms bump with a sharp edge at the start. A soft bump alone leaves no stripe in the
        # spectrogram; a real pop (scratch, stylus jump) has a hard edge.
        ln = int(0.004 * sr)
        k = np.arange(ln)
        sign = rng.choice([-1, 1])
        edge = 0.6 * np.exp(-k / max(1.0, 0.0001 * sr)) * (1 - 0.6 * (k % 2))
        return amp * sign * (np.sin(np.pi * k / ln) * np.exp(-k / (0.001 * sr)) + edge)
    ln = int(rng.integers(3, 20))
    k = np.arange(ln)
    return amp * np.exp(-k / max(1.0, ln / 4)) * rng.choice([-1, 1]) * (1 - 0.6 * (k % 2))


def add_click(audio: np.ndarray, sr: int, t: float, amp_dbfs: float, ch: int, rng,
              kind: str = "click") -> None:
    """Add a click at time `t` (s) with peak level `amp_dbfs` to the channels in bit mask `ch` (1 = L, 2 = R)."""
    shape = click_shape(rng, sr, _db(amp_dbfs), kind)
    i = int(t * sr)
    j = min(len(audio), i + len(shape))
    for c in range(2):
        if ch & (1 << c):
            audio[i:j, c] += shape[:j - i]


def make_side(duration_s: float = 90.0, sr: int = 48000, seed: int = 0,
              layout: Layout | None = None, noise_dbfs: float = -60.0,
              music_dbfs: float = -21.0, clicks_per_min: float = 20.0,
              pops: int = 2, amp_range=(-45.0, -12.0),
              needle_drop: bool = False) -> tuple[np.ndarray, Truth]:
    """Generate a record side (float32, stereo) plus its ground truth."""
    rng = np.random.default_rng(seed)
    lay = layout or Layout()
    n = int(duration_s * sr)
    truth = Truth(sr=sr)

    g0 = lay.no_groove_s
    audio = np.zeros((n, 2))
    audio[int(g0 * sr):] = _surface_noise(n - int(g0 * sr), rng, noise_dbfs)
    audio[:int(g0 * sr)] = rng.standard_normal((int(g0 * sr), 2)) * _db(-92)  # chain noise
    truth.groove = (g0, duration_s)

    music_total = duration_s - g0 - lay.lead_in_s - lay.lead_out_s - lay.gap_s * (lay.tracks - 1)
    track_len = music_total / lay.tracks
    t = g0
    truth.quiet.append((t, t + lay.lead_in_s))
    t += lay.lead_in_s
    for k in range(lay.tracks):
        a, b = int(t * sr), int((t + track_len) * sr)
        audio[a:b] += _music(b - a, sr, rng, music_dbfs)
        truth.music.append((t, t + track_len))
        t += track_len
        if k < lay.tracks - 1:
            truth.quiet.append((t, t + lay.gap_s))
            t += lay.gap_s
    truth.quiet.append((t, duration_s))

    if needle_drop and g0 > 0:
        add_click(audio, sr, g0, -6.0, 3, rng, "pop")

    n_clicks = int(clicks_per_min * (duration_s - g0) / 60)
    for _ in range(n_clicks):
        tc = rng.uniform(g0 + 0.6, duration_s - 0.6)
        amp = rng.uniform(*amp_range)
        ch = int(rng.choice([1, 2, 3]))
        add_click(audio, sr, tc, amp, ch, rng)
        truth.clicks.append({"t": tc, "amp_dbfs": amp, "ch": ch, "kind": "click"})
    for _ in range(pops):
        tc = rng.uniform(g0 + 0.6, duration_s - 0.6)
        add_click(audio, sr, tc, -8.0, 3, rng, "pop")
        truth.clicks.append({"t": tc, "amp_dbfs": -8.0, "ch": 3, "kind": "pop"})
    truth.clicks.sort(key=lambda c: c["t"])
    return np.clip(audio, -1, 1).astype(np.float32), truth


def region_of(truth: Truth, t: float) -> str:
    """"quiet" or "music": the part of a synthetic side at time t."""
    for a, b in truth.quiet:
        if a <= t < b:
            return "quiet"
    return "music"


def skip_back(audio: np.ndarray, sr: int, t: float, rpm: float = 100 / 3, grooves: int = 1,
              speed: float = 1.0) -> np.ndarray:
    """Stylus jumps back by `grooves` grooves at `t`: the last revolution(s) repeat."""
    i = int(t * sr)
    j = i - int(round(grooves * 60 / rpm / speed * sr))
    return np.concatenate([audio[:i], audio[j:]])


def skip_forward(audio: np.ndarray, sr: int, t: float, rpm: float = 100 / 3, grooves: int = 1,
                 speed: float = 1.0) -> np.ndarray:
    """Stylus jumps inwards by `grooves` grooves at `t`: a revolution is missing."""
    i = int(t * sr)
    j = i + int(round(grooves * 60 / rpm / speed * sr))
    return np.concatenate([audio[:i], audio[j:]])


def locked_groove(audio: np.ndarray, sr: int, repeats: int = 4, rpm: float = 100 / 3) -> np.ndarray:
    """Locked groove: the last revolution plays `repeats` times (run-out until the stylus is lifted)."""
    n = int(round(60 / rpm * sr))
    return np.concatenate([audio] + [audio[-n:]] * repeats)


# ─── Test record (alignment) ───────────────────────────────────────────────────
# Model of the Analogue Productions "The Ultimate Analogue Test LP" (AAPT 1) with adjustable turntable
# faults. Band sequence as published by the manufacturer (Acoustic Sounds); the real bands are longer,
# short ones are enough for the tests. Levels relative to the 1 kHz reference tone (0 dB, 7 cm/s).

@dataclass
class Rig:
    """Simulated turntable: settings and faults that the alignment analysis should find again."""
    ref_dbfs: float = -18.0          # level of the 1 kHz reference tone at the interface
    balance_db: float = 0.0          # left louder than right by …
    xt_lr_db: float = -27.0          # crosstalk left → right
    xt_rl_db: float = -27.0          # crosstalk right → left
    speed_pct: float = 0.0
    ecc_pct: float = 0.0             # speed variation once per revolution (peak)
    flutter_pct: float = 0.0         # drive variation (peak) at flutter_hz
    flutter_hz: float = 4.0
    clip_l_db: float | None = None   # from this level (rel. reference) the stylus loses contact on the left
    clip_r_db: float | None = None
    tracing_pct: float = 0.5         # tracing distortion (k2) at 0 dB, grows with the level
    resonance_hz: float = 11.0
    resonance_q: float = 2.0
    hf_db: float = 0.0               # deviation at 10 kHz (above 1 kHz linear in log f)
    lf_db: float = 0.0               # deviation at 20 Hz (below 1 kHz linear in log f, without resonance)
    imd_pct: float = 1.0             # intermodulation 60 Hz/4 kHz (SMPTE)
    # drive rumble rel. reference (20–100 Hz); record warps at 4–18 Hz with a vertical resonance bump, same level
    rumble_db: float = -60.0
    warp_db: float | None = None     # own level of the record warps (no record weight: ≈ 13 dB above the rumble)
    noise_dbfs: float = -80.0        # groove noise (≈ −62 dB below the reference)
    rpm: float = 100 / 3


@dataclass
class BandTruth:
    """Ground truth of one test band: its role (as alignment.assign_roles names it) and position (s)."""
    role: str
    t0: float
    t1: float


def _hp2(f: np.ndarray, f0: float, q: float) -> np.ndarray:
    """Arm/cartridge resonance: second-order high-pass (flat above, peak set by Q at f0)."""
    u = (f / f0) ** 2
    return u / np.sqrt((1 - u) ** 2 + (np.sqrt(u) / q) ** 2)


def _pink(n: int, rng) -> np.ndarray:
    """Pink noise (1/f power) with unit RMS."""
    spec = np.fft.rfft(rng.standard_normal(n))
    fr = np.arange(len(spec), dtype=float)
    fr[0] = 1
    y = np.fft.irfft(spec / np.sqrt(fr), n=n)
    return y / np.sqrt(np.mean(y ** 2))


class _Side:
    """Builds a test record side part by part, logging where each band lies."""

    def __init__(self, rig: Rig, sr: int, seed: int):
        self.rig, self.sr = rig, sr
        self.rng = np.random.default_rng(seed)
        self.parts: list[np.ndarray] = []
        self.t = 0.0
        self.bands: list[BandTruth] = []
        self.a_ref = _db(rig.ref_dbfs)
        self.g_r = _db(-rig.balance_db)

    def _speed(self, tt: np.ndarray) -> np.ndarray:
        r = self.rig
        f_rev = r.rpm / 60 * (1 + r.speed_pct / 100)
        return (1 + r.speed_pct / 100) * (1 + r.ecc_pct / 100 * np.sin(2 * np.pi * f_rev * tt)
                                          + r.flutter_pct / 100 * np.sin(2 * np.pi * r.flutter_hz * tt))

    def _times(self, dur: float) -> np.ndarray:
        return self.t + np.arange(int(dur * self.sr)) / self.sr

    def _phase(self, f_inst: np.ndarray) -> np.ndarray:
        return 2 * np.pi * np.cumsum(f_inst) / self.sr + self.rng.uniform(0, 6.3)

    def _resp(self, f: np.ndarray) -> np.ndarray:
        r = self.rig
        lg = np.log10(np.maximum(f, 1.0) / 1000)
        dev = np.where(lg > 0, r.hf_db * lg, r.lf_db * (-lg) / np.log10(50))
        return _db(dev) * _hp2(f, r.resonance_hz, r.resonance_q)

    def _add(self, stereo: np.ndarray, role: str | None) -> None:
        if role:
            self.bands.append(BandTruth(role, self.t, self.t + len(stereo) / self.sr))
        self.parts.append(stereo)
        self.t += len(stereo) / self.sr

    def gap(self, dur: float) -> None:
        self._add(np.zeros((int(dur * self.sr), 2)), None)

    def needle_up(self, dur: float) -> None:
        self._add(np.full((int(dur * self.sr), 2), np.nan), None)   # replaced by chain noise in render()

    def _place(self, s: np.ndarray, mode: str, role: str) -> None:
        r = self.rig
        if mode == "lateral":
            out = np.column_stack([s, s * self.g_r])
        elif mode == "vertical":
            out = np.column_stack([s, -s * self.g_r])
        elif mode == "left":
            out = np.column_stack([s, s * _db(r.xt_lr_db)])
        else:
            out = np.column_stack([s * _db(r.xt_rl_db), s * self.g_r])
        n = len(out)
        fade = np.minimum(1, np.minimum(np.arange(n), np.arange(n)[::-1]) / (0.02 * self.sr))
        self._add(out * fade[:, None], role)

    def tone(self, f: float, level_db: float, dur: float, mode: str, role: str) -> None:
        tt = self._times(dur)
        fi = f * self._speed(tt)
        a = self.a_ref * _db(level_db) * self._resp(np.array([f * (1 + self.rig.speed_pct / 100)]))[0]
        self._place(a * np.sin(self._phase(fi)), mode, role)

    def sweep(self, f1: float, f2: float, level_db: float, dur: float, mode: str, role: str) -> None:
        tt = self._times(dur)
        f = f1 * (f2 / f1) ** ((tt - tt[0]) / dur)
        fi = f * self._speed(tt)
        a = self.a_ref * _db(level_db) * self._resp(fi)
        self._place(a * np.sin(self._phase(fi)), mode, role)

    def ramp(self, f: float, lo_db: float, hi_db: float, dur: float, role: str) -> None:
        r = self.rig
        tt = self._times(dur)
        lev = lo_db + (hi_db - lo_db) * (tt - tt[0]) / dur
        amp = self.a_ref * _db(lev)
        ph = self._phase(f * self._speed(tt))
        s = amp * np.sin(ph) + r.tracing_pct / 100 * amp ** 2 / self.a_ref * np.sin(2 * ph + 0.7)
        out = np.column_stack([s, s * self.g_r])
        for c, clip in enumerate((r.clip_l_db, r.clip_r_db)):
            if clip is not None:
                lim = self.a_ref * _db(clip) * (1 if c == 0 else self.g_r)
                out[:, c] = np.clip(out[:, c], -lim, lim)
        n = len(out)
        fade = np.minimum(1, np.minimum(np.arange(n), np.arange(n)[::-1]) / (0.02 * self.sr))
        self._add(out * fade[:, None], role)

    def imd(self, dur: float, role: str) -> None:
        r = self.rig
        tt = self._times(dur)
        sp = self._speed(tt)
        m = r.imd_pct / 100 * np.sqrt(2)
        lo = 0.8 * self.a_ref * np.sin(self._phase(60 * sp))
        hi = 0.2 * self.a_ref * (1 + m * np.sin(2 * np.pi * 60 * tt)) * np.sin(self._phase(4000 * sp))
        self._place(lo + hi, "lateral", role)

    def pink(self, level_db: float, dur: float, mode: str, role: str) -> None:
        n = int(dur * self.sr)
        self._place(self.a_ref * _db(level_db) / np.sqrt(2) * _pink(n, self.rng), mode, role)

    def silent(self, dur: float, role: str) -> None:
        r = self.rig
        n = int(dur * self.sr)
        # lateral: drive rumble (20–100 Hz); vertical: record warps that excite the arm resonance
        b, a = signal.butter(2, [20, 100], btype="band", fs=self.sr)
        sw = signal.butter(4, [4, 18], btype="band", fs=self.sr, output="sos")   # b/a would be unstable here
        bv, av = signal.iirpeak(r.resonance_hz, max(r.resonance_q, 0.5), fs=self.sr)
        lat = signal.lfilter(b, a, self.rng.standard_normal(n))
        ver = signal.lfilter(bv, av, signal.sosfilt(sw, self.rng.standard_normal(n)))
        # normalize to the power within 20–100 Hz (Parseval), which is what the analysis measures
        X = np.fft.rfft(lat)
        fr = np.fft.rfftfreq(n, 1 / self.sr)
        ms_in = 2 * np.sum(np.abs(X[(fr >= 20) & (fr <= 100)]) ** 2) / n ** 2
        lat *= self.a_ref * _db(r.rumble_db) / np.sqrt(ms_in + 1e-30) / np.sqrt(2)
        warp = r.rumble_db if r.warp_db is None else r.warp_db
        ver *= self.a_ref * _db(warp) / np.sqrt(np.mean(ver ** 2) + 1e-30) / np.sqrt(2)
        self._add(np.column_stack([lat + ver, lat - ver]), role)

    def render(self) -> np.ndarray:
        audio = np.concatenate(self.parts)
        up = np.isnan(audio[:, 0])
        audio[up] = 0.0
        groove = np.flatnonzero(~up)
        noise = _surface_noise(len(groove), self.rng, self.rig.noise_dbfs) if groove.size else np.zeros((0, 2))
        audio[groove] += noise
        audio[up] = self.rng.standard_normal((int(up.sum()), 2)) * _db(-92)
        return np.clip(audio, -1, 1).astype(np.float32)


def make_test_side(side: int, rig: Rig | None = None, sr: int = 48000, seed: int = 0,
                   band_s: float = 6.0, sweep_s: float = 16.0, wf_s: float = 20.0,
                   ramp_s: float = 24.0, silent_s: float = 40.0, gap_s: float = 2.0,
                   needle_up_s: float = 1.0) -> tuple[np.ndarray, list[BandTruth]]:
    """Side 1 or 2 of the AAPT 1 played on `rig`. Returns audio and the bands (ground truth)."""
    rig = rig or Rig()
    s = _Side(rig, sr, seed)
    s.needle_up(needle_up_s)
    s.gap(3.0)                                                          # lead-in groove
    if side == 1:
        steps = [lambda: s.tone(1000, 0, band_s, "lateral", "ref_1k"),
                 lambda: s.tone(1000, 0, band_s, "left", "xt_left"),
                 lambda: s.tone(1000, 0, band_s, "right", "xt_right"),
                 lambda: s.tone(1000, -20, band_s, "lateral", "ref_1k_m20"),
                 lambda: s.tone(10000, -20, band_s, "lateral", "hf_10k"),
                 lambda: s.sweep(1000, 20000, -20, sweep_s, "lateral", "sweep_high"),
                 lambda: s.sweep(1000, 20, 0, sweep_s, "lateral", "sweep_low"),
                 lambda: s.tone(100, 0, band_s, "lateral", "lf_100"),
                 lambda: s.imd(band_s, "imd"),
                 lambda: s.tone(3150, -6, wf_s, "lateral", "wow_flutter")]
    else:
        steps = [lambda: s.ramp(315, 0, 12, ramp_s, "antiskating"),
                 lambda: s.pink(-10, band_s, "lateral", "pink_lateral"),
                 lambda: s.pink(-10, band_s, "vertical", "pink_vertical"),
                 lambda: s.tone(1000, 0, band_s, "vertical", "vert_1k"),
                 lambda: s.sweep(1000, 10, -20, sweep_s, "vertical", "resonance")]
    for k, step in enumerate(steps):
        if k:
            s.gap(gap_s)
        step()
    s.gap(gap_s)
    if side == 2:
        s.silent(silent_s, "silent_groove")
    s.gap(4.0)                                                          # run-out
    s.needle_up(needle_up_s)
    return s.render(), s.bands


def time_warp(audio: np.ndarray, sr: int, speed: float, offset_s: float) -> np.ndarray:
    """Play `audio` at a different speed and a later start: out(t) = in((t - offset) · speed)."""
    n_out = int((len(audio) / speed) + offset_s * sr)
    src = (np.arange(n_out) / sr - offset_s) * speed * sr
    out = np.zeros((n_out, audio.shape[1]), dtype=np.float32)
    ok = (src >= 0) & (src <= len(audio) - 1)
    for c in range(audio.shape[1]):
        out[ok, c] = np.interp(src[ok], np.arange(len(audio)), audio[:, c])
    return out
