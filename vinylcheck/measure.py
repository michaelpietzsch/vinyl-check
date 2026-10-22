"""Measurement building blocks for test tones: level, distortion (THD, THD+N), frequency, wow & flutter, live meter.

Shared by the turntable alignment analysis (alignment.py) and the live measurement during monitoring and
recording (capture.py).

Levels are sine amplitudes relative to full scale: 0 dB = full-scale sine (as in AES17).
Spectra use a 4-term Blackman-Harris window (side lobes −92 dB), so the measuring range for distortion
reaches down to ≈ 0.003 %. Spectra are scaled so that the sum over the main lobe of a sine gives its mean
power A²/2 and the sum over all bins gives the total power.
"""

from __future__ import annotations

import numpy as np
from scipy import fft as sfft
from scipy import signal

BH_LOBE = 4                # half main-lobe width of the Blackman-Harris window in bins
AUDIO_BAND = (20.0, 20000.0)

# Weighting curve for wow & flutter per IEC 60386 / DIN 45507 (table values, Hz → dB); peaks at 4 Hz,
# where speed fluctuations are most audible
FLUTTER_WEIGHTING = [(0.1, -48.0), (0.2, -30.6), (0.315, -19.7), (0.4, -15.0), (0.63, -8.4),
                     (0.8, -6.0), (1.0, -4.2), (1.6, -1.8), (2.0, -0.9), (4.0, 0.0), (6.3, -0.9),
                     (10.0, -2.1), (20.0, -5.9), (40.0, -10.4), (63.0, -14.2), (100.0, -17.3),
                     (200.0, -23.0)]


def db(power):
    """Power → dB (floored at −200 dB)."""
    return 10 * np.log10(np.maximum(power, 1e-20))


def runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """Contiguous True runs of a mask as (start, end_exclusive)."""
    d = np.diff(np.concatenate([[0], np.asarray(mask).astype(np.int8), [0]]))
    return list(zip(np.flatnonzero(d == 1), np.flatnonzero(d == -1)))


def spectrum(x: np.ndarray, sr: int, frame_s: float | None = 0.5) -> tuple[np.ndarray, np.ndarray]:
    """Averaged power spectrum (Welch, Blackman-Harris, 50 % overlap) in power per bin.

    x: (n,) or (n, k); frame_s: segment length (None or 0: one segment over all of x). Returns f, P (bins, k)."""
    x = np.asarray(x, dtype=np.float64)
    if x.ndim == 1:
        x = x[:, None]
    n = len(x)
    nper = n if not frame_s else min(n, max(64, int(frame_s * sr)))
    f, p = signal.welch(x, sr, window="blackmanharris", nperseg=nper, noverlap=nper // 2,
                        scaling="density", axis=0, detrend="constant")
    return f, p * (f[1] - f[0])


def _refine(p: np.ndarray, k: int) -> float:
    """Parabolic interpolation of the peak bin (on dB values); returns the fractional bin."""
    if k <= 0 or k >= len(p) - 1:
        return float(k)
    a, b, c = db(p[k - 1]), db(p[k]), db(p[k + 1])
    den = a - 2 * b + c
    return float(k + (0.5 * (a - c) / den if den < 0 else 0.0))


def _lobe(p: np.ndarray, k: int, half: int) -> np.ndarray:
    return p[max(0, k - half):k + half + 1].sum(axis=0)


def tone(x: np.ndarray, sr: int, f_hint: float | None = None, search: float = 0.1,
         nharm: int = 9, band: tuple = AUDIO_BAND, frame_s: float | None = 0.5,
         fmin: float | None = None) -> dict:
    """Fundamental and distortion per channel. The fundamental frequency is shared by all channels (loudest
    peak of the channel sum, searched within ±`search` of `f_hint` if given), so crosstalk in an undriven
    channel is measured at exactly the same frequency.

    Returns freq_hz, level_db (sine amplitude, 0 = full scale), thd_pct (harmonics 2 … nharm),
    thdn_pct (everything in `band` except the fundamental), share (fundamental's share of the band power).
    """
    f, p = spectrum(x, sr, frame_s)
    df = f[1] - f[0]
    lo, hi = band[0] if fmin is None else fmin, min(band[1], 0.45 * sr)
    tot = p.sum(axis=1)
    if f_hint:
        rng = (f >= f_hint * (1 - search)) & (f <= f_hint * (1 + search))
    else:
        rng = (f >= lo) & (f <= hi)
    if not rng.any():
        rng = (f >= lo) & (f <= hi)
    k0 = int(np.flatnonzero(rng)[np.argmax(tot[rng])])
    f0 = _refine(tot, k0) * df
    half = BH_LOBE + 1 + int(np.ceil(f0 * 0.004 / df))      # wow & flutter smears the tone a little
    fund = _lobe(p, k0, half)
    inband = (f >= lo) & (f <= hi)
    total = p[inband].sum(axis=0)
    harm = np.zeros(p.shape[1])
    for h in range(2, nharm + 1):
        fh = h * f0
        if fh > hi:
            break
        kh = int(round(fh / df))
        lo_k, hi_k = max(0, kh - 2), min(len(p) - 1, kh + 2)
        kh = lo_k + int(np.argmax(tot[lo_k:hi_k + 1]))
        harm += _lobe(p, kh, half)
    fund_safe = np.maximum(fund, 1e-30)
    return {
        "freq_hz": float(f0),
        "level_db": [float(v) for v in db(2 * fund)],
        "thd_pct": [float(v) for v in 100 * np.sqrt(harm / fund_safe)],
        "thdn_pct": [float(v) for v in 100 * np.sqrt(np.maximum(total - fund, 0) / fund_safe)],
        "share": [float(v) for v in fund / np.maximum(total, 1e-30)],
    }


def level_at(x: np.ndarray, sr: int, freq: float, frame_s: float | None = 0.5) -> list[float]:
    """Level (dB) of the component at `freq` per channel, without a peak search across channels."""
    f, p = spectrum(x, sr, frame_s)
    df = f[1] - f[0]
    k = int(round(freq / df))
    k = max(1, min(len(p) - 2, k))
    tot = p.sum(axis=1)
    k = k - 2 + int(np.argmax(tot[max(0, k - 2):k + 3]))
    half = BH_LOBE + 1 + int(np.ceil(freq * 0.004 / df))
    return [float(v) for v in db(2 * _lobe(p, k, half))]


def band_power(x: np.ndarray, sr: int, lo: float, hi: float, frame_s: float = 1.0) -> np.ndarray:
    """Mean power per channel in the frequency band [lo, hi] Hz."""
    f, p = spectrum(x, sr, frame_s)
    m = (f >= lo) & (f <= hi)
    return p[m].sum(axis=0)


# ─── Wow & flutter ─────────────────────────────────────────────────────────────
def inst_freq(x: np.ndarray, sr: int, f_c: float, rel_bw: float = 0.2,
              out_rate: float = 1000.0) -> tuple[np.ndarray, float]:
    """Instantaneous frequency of a tone around f_c: band-pass (±rel_bw), analytic signal, phase advance per block.

    The phase advance over a block is exactly the mean frequency within the block, so averaging over blocks
    does not amplify noise. Returns the frequency (Hz) at rate `out_rate` and the actual rate.
    """
    sos = signal.butter(4, [f_c * (1 - rel_bw), f_c * (1 + rel_bw)], btype="band", fs=sr, output="sos")
    y = signal.sosfiltfilt(sos, np.asarray(x, dtype=np.float64))
    n = len(y)
    z = signal.hilbert(y, N=sfft.next_fast_len(n))[:n]
    ph = np.unwrap(np.angle(z))
    m = max(1, int(sr // out_rate))
    nb = (n - 1) // m
    blocks = ph[:nb * m + 1:m]
    fi = np.diff(blocks) * sr / (2 * np.pi * m)
    return fi, sr / m


def weighting_db(f: np.ndarray) -> np.ndarray:
    """IEC 60386 weighting (dB) for frequencies f > 0; outside the table extrapolated with the edge slope."""
    tf = np.log10([a for a, _ in FLUTTER_WEIGHTING])
    td = np.array([b for _, b in FLUTTER_WEIGHTING])
    lf = np.log10(np.maximum(f, 1e-6))
    out = np.interp(lf, tf, td)
    lo_slope = (td[1] - td[0]) / (tf[1] - tf[0])
    hi_slope = (td[-1] - td[-2]) / (tf[-1] - tf[-2])
    out = np.where(lf < tf[0], td[0] + (lf - tf[0]) * lo_slope, out)
    return np.where(lf > tf[-1], td[-1] + (lf - tf[-1]) * hi_slope, out)


def _weighted(dev: np.ndarray, rate: float) -> np.ndarray:
    """Apply the flutter weighting to a deviation signal (sampled at `rate`) in the frequency domain."""
    spec = np.fft.rfft(dev - dev.mean())
    fr = np.fft.rfftfreq(len(dev), 1 / rate)
    g = np.zeros_like(fr)
    g[1:] = 10 ** (weighting_db(fr[1:]) / 20)
    return np.fft.irfft(spec * g, n=len(dev))


def flutter(fi: np.ndarray, rate: float, nominal_hz: float, rpm: float) -> dict:
    """Speed and wow & flutter from the instantaneous frequency of a test tone (3150 Hz at 33⅓).

    - Speed: mean frequency relative to the nominal one.
    - Wow & flutter "2σ": the value the deviation does not exceed 95 % of the time (peak method per AES6;
      an IEC meter with quasi-peak detection reads similarly).
    - Eccentricity: the component that fluctuates exactly once per revolution (centre hole of record or
      platter off-centre), fitted as a sine at the revolution frequency. "Drive" = weighted without this
      component (belt, motor, bearing).
    """
    mean = float(np.mean(fi))
    dev = fi / mean - 1.0
    t = np.arange(len(dev)) / rate
    f_rev = rpm / 60 * mean / nominal_hz
    basis = np.column_stack([np.cos(2 * np.pi * f_rev * t), np.sin(2 * np.pi * f_rev * t), np.ones_like(t)])
    coef, *_ = np.linalg.lstsq(basis, dev, rcond=None)
    ecc = float(np.hypot(coef[0], coef[1]))
    drive = dev - basis[:, :2] @ coef[:2]
    dw = _weighted(dev, rate)
    drive_w = _weighted(drive, rate)
    return {
        "freq_hz": round(mean, 3),
        "nominal_hz": nominal_hz,
        "speed_pct": round((mean / nominal_hz - 1) * 100, 3),
        "cents": round(float(1200 * np.log2(mean / nominal_hz)), 1),
        "wf_unweighted_2s_pct": round(float(np.percentile(np.abs(dev - dev.mean()), 95)) * 100, 4),
        "wf_weighted_2s_pct": round(float(np.percentile(np.abs(dw), 95)) * 100, 4),
        "wf_weighted_rms_pct": round(float(np.sqrt(np.mean(dw ** 2))) * 100, 4),
        "eccentricity_pct": round(ecc * 100, 4),
        "drive_weighted_2s_pct": round(float(np.percentile(np.abs(drive_w), 95)) * 100, 4),
        "rev_hz": round(f_rev, 4),
        "duration_s": round(len(dev) / rate, 1),
    }


# ─── Live meter ────────────────────────────────────────────────────────────────
def channel_mode(p_l: float, p_r: float, corr: float) -> str:
    """Channel image of a signal from channel powers and L/R correlation.

    Returns stored values: "lateral" (in phase), "vertikal" (vertical, out of phase), "links"/"rechts" (left/right
    only: the other channel ≥ 12 dB lower) or "gemischt" (mixed)."""
    r = db(p_l) - db(p_r)
    if r > 12:
        return "links"
    if r < -12:
        return "rechts"
    if corr > 0.6:
        return "lateral"
    if corr < -0.6:
        return "vertikal"
    return "gemischt"


# Third-octave centre frequencies per ISO 266, 20 Hz to 20 kHz (31 bands)
RTA_CENTERS_HZ = (20, 25, 31.5, 40, 50, 63, 80, 100, 125, 160, 200, 250, 315, 400, 500, 630, 800,
                  1000, 1250, 1600, 2000, 2500, 3150, 4000, 5000, 6300, 8000, 10000, 12500, 16000, 20000)


class LiveMeter:
    """Live measurement during monitoring and recording: fundamental, level, distortion and channel image.

    Keeps the last `window_s` seconds and measures again every `every_s` seconds. Meant for the moment someone
    is adjusting the anti-skating or the counterweight while a test band is playing.
    """

    def __init__(self, rate: int, channels: int, window_s: float = 0.5, every_s: float = 0.2):
        self.rate, self.channels = rate, channels
        self.n = int(rate * window_s)
        self.every = int(rate * every_s)
        self.buf = np.zeros((self.n, channels), np.float32)
        self.fill = 0
        self.since = 0
        self.frames = 0
        self.seq = 0
        self.result: dict | None = None
        # 31-band third-octave analysis (ISO 20 Hz … 20 kHz) for the display: compute the bin edges once
        self.win = np.hanning(self.n)
        self.win_pow = float(np.sum(self.win ** 2))
        fr = np.fft.rfftfreq(self.n, 1 / rate)
        self.rta_edges = []
        for fc in RTA_CENTERS_HZ:
            lo, hi = np.searchsorted(fr, fc * 2 ** (-1 / 6)), np.searchsorted(fr, fc * 2 ** (1 / 6))
            self.rta_edges.append((int(lo), int(max(hi, lo + 1))))

    def rta(self, x: np.ndarray) -> list[list[float]]:
        """Level per third-octave band and channel in dBFS (sine reference: full scale = 0 dB)."""
        X = np.abs(np.fft.rfft(x * self.win[:, None], axis=0)) ** 2
        out = []
        for c in range(x.shape[1]):
            ms = [2 * float(X[lo:hi, c].sum()) / (self.n * self.win_pow) for lo, hi in self.rta_edges]
            out.append([round(max(10 * np.log10(v + 1e-20) + 3.01, -120.0), 1) for v in ms])
        return out

    def feed(self, x: np.ndarray) -> dict | None:
        """Add a block of samples (n, channels); returns the latest measurement (None until the window is full)."""
        x = np.asarray(x, dtype=np.float32).reshape(-1, self.channels)
        k = len(x)
        if k >= self.n:
            self.buf[:] = x[-self.n:]
        else:
            self.buf = np.concatenate([self.buf[k:], x])
        self.fill = min(self.n, self.fill + k)
        self.since += k
        self.frames += k
        if self.fill >= self.n and self.since >= self.every:
            self.since = 0
            self.result = self.measure()
        return self.result

    def measure(self) -> dict:
        """Measure the current window: third-octave levels always, tone values only above −80 dBFS."""
        x = self.buf.astype(np.float64)
        self.seq += 1
        out = {"seq": self.seq, "t": round(self.frames / self.rate, 2), "rta_db": self.rta(x)}
        rms = np.sqrt(np.mean(x ** 2, axis=0))
        if float(np.max(rms)) < 10 ** (-80 / 20):
            return out | {"tonal": False}
        r = tone(x, self.rate, frame_s=None)
        loud = int(np.argmax(r["level_db"]))
        tonal = r["share"][loud] >= 0.7
        out |= {"tonal": bool(tonal), "freq_hz": round(r["freq_hz"], 1), "share": round(r["share"][loud], 2),
                "level_db": [round(v, 1) for v in r["level_db"]],
                "thd_pct": [round(v, 3) for v in r["thd_pct"]],
                "thdn_pct": [round(v, 3) for v in r["thdn_pct"]]}
        if self.channels >= 2:
            l, rr = x[:, 0] - x[:, 0].mean(), x[:, 1] - x[:, 1].mean()
            pl, pr = float(np.mean(l ** 2)), float(np.mean(rr ** 2))
            corr = float(np.mean(l * rr) / np.sqrt(max(pl * pr, 1e-30)))
            mode = channel_mode(pl, pr, corr)
            out |= {"balance_db": round(r["level_db"][0] - r["level_db"][1], 2), "corr": round(corr, 3),
                    "mode": mode}
            if mode in ("links", "rechts"):
                drv, oth = (0, 1) if mode == "links" else (1, 0)
                out["crosstalk_db"] = round(r["level_db"][oth] - r["level_db"][drv], 1)
            if mode == "vertikal":
                m = (x[:, 0] + x[:, 1]) / 2
                s = (x[:, 0] - x[:, 1]) / 2
                lv = level_at(np.column_stack([m, s]), self.rate, r["freq_hz"], frame_s=None)
                out["mono_cancel_db"] = round(lv[0] - lv[1], 1)
        return out
