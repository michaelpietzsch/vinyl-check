"""Click and pop detection via the prediction error of an autoregressive model (LPC residual).

How it works
------------
Music and groove noise are well predictable over a few milliseconds; a click is not. For each block of
`frame` samples an AR model of order `order` is estimated (autocorrelation method + Levinson-Durbin). Filtering
the block with its own prediction-error filter yields the *residual*: for music it is small and nearly white,
while a click shows up in it as a sharp spike far above its surroundings.

The threshold is relative to a robust estimate of the residual level around each sample
(σ ≈ 1.4826 × median |e|, the MAD estimate of a Gaussian's standard deviation). This makes detection independent
of the recording level and of the loudness of the music, and a click hardly raises "its own" median.
σ is the larger of two estimates: one per LPC block (smoothed over neighbouring blocks) and a local one over
~12 ms, which lets a sustained noise burst (hi-hat, snare) raise the threshold for itself.

At its first sample the residual of a click has roughly the amplitude of the click itself, so `amp_dbfs` is a
usable estimate of how loud the click is.

Stripe check
------------
In a spectrogram a click is a thin vertical stripe across the whole band. Every candidate of the AR detector
must therefore rise in the treble (above 6 kHz), within ±2.7 ms, by at least `stripe_db` above the median of its
±60 ms surroundings, and be at most `stripe_ms` wide at half height. A cymbal or snare hit is wide and decays;
crackle is thin. Without this check the AR detector alone reported about nine times as many events in dense
music as the grooveclean network, of which only a small fraction (about 4 %) was visible in the spectrogram.
grooveclean's own verification step (second derivative against a clipped local mean) did not separate them on
real records: a gate of 3 removed only 2 % of the invisible events, a gate of 20 already lost 25 % of the
visible ones.

Clipped peaks (flat runs at the signal's maximum, as in grooveclean) never count as clicks.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view
from scipy import ndimage, signal

MAD_TO_SIGMA = 1.4826          # median absolute deviation → standard deviation for Gaussian data
# Version of what the detector reports (which events, their strength, duration, level and kind). The learning model's
# weights depend on it, so Vinyl-Mind never combines weights from different detector versions: raise it whenever a
# change alters the events or their values on the same audio (not for pure speed-ups or refactoring). A string, as the
# hub's specification sends it ("1", "2", …).
VERSION = "1"

EVENT_DTYPE = np.dtype([
    ("t", "f8"),          # time of the peak (s)
    ("dur_ms", "f4"),     # time above the threshold (ms)
    ("strength", "f4"),   # peak / local σ of the residual
    ("amp_dbfs", "f4"),   # peak amplitude of the residual (dBFS)
    ("ch", "u1"),         # channels as a bit mask: 1 = left, 2 = right, 3 = both
])


@dataclass(frozen=True)
class DetectParams:
    order: int = 32               # AR model order
    frame: int = 2048             # block for the LPC estimate (~43 ms at 48 kHz)
    threshold: float = 8.0        # |residual| > threshold × σ → candidate
    merge_ms: float = 1.5         # hits closer together than this → one event
    sigma_floor_dbfs: float = -110.0  # prevents false alarms in digital silence
    local_block: int = 64         # sub-block for the local σ (~1.3 ms)
    local_blocks: int = 9         # window width in sub-blocks (~12 ms)
    chunk_frames: int = 512       # blocks per processing step (bounds memory use)
    stripe_db: float = 12.0       # stripe check: treble jump (≥ 6 kHz) above the ±60 ms surroundings; 0 = off
    stripe_ms: float = 5.0        # … and at most this wide at half height


def params_for(sensitivity: float | None, base: DetectParams = DetectParams()) -> DetectParams:
    """Detector parameters from a single 0…1 sensitivity control instead of several thresholds (as in grooveclean).

    0.5 is the calibrated default, 0 finds only the obvious, 1 is aggressive. Threshold and stripe check move
    together: the threshold is scaled by e^(0.5 − s), the required treble jump shifts by up to ±4 dB.
    """
    s = 0.5 if sensitivity is None else min(max(float(sensitivity), 0.0), 1.0)
    return replace(base, threshold=round(base.threshold * math.exp(1.0 * (0.5 - s)), 3),
                   stripe_db=round(base.stripe_db + 8.0 * (0.5 - s), 2) if base.stripe_db > 0 else 0.0)


# ─── Stripe check ─────────────────────────────────────────────────────────────────────────────────
STRIPE_HP_HZ = 6000.0       # treble band: a click always reaches it, music is rarely dense up there
STRIPE_WIN = 128            # window for the treble energy (2.7 ms at 48 kHz), hop is half of it
STRIPE_HALF_MS = 60.0       # surroundings on each side whose median is the reference level
STRIPE_GUARD_MS = 2.7       # left out of the reference: the click itself (±2 bins)
STRIPE_CHUNK = 1 << 22      # samples per processing block for the energy (bounds memory use)


def hf_envelope(x: np.ndarray, sr: int) -> tuple[np.ndarray, int]:
    """Treble energy (dB) in windows of STRIPE_WIN samples with hop STRIPE_WIN/2. Returns (env_db, hop).

    The filtered signal is squared block by block so that no float64 copy of a whole side is created."""
    hop = STRIPE_WIN // 2
    sos = signal.butter(4, STRIPE_HP_HZ, btype="high", fs=sr, output="sos")
    y = signal.sosfilt(sos, np.asarray(x, dtype=np.float32)).astype(np.float32)
    nblk = len(y) // hop
    half = np.empty(nblk)
    for b0 in range(0, nblk, STRIPE_CHUNK // hop):
        b1 = min(nblk, b0 + STRIPE_CHUNK // hop)
        seg = y[b0 * hop:b1 * hop].astype(np.float64)
        half[b0:b1] = np.mean(seg.reshape(b1 - b0, hop) ** 2, axis=1)
    env = (half[:-1] + half[1:]) / 2 if nblk > 1 else half      # 50 % overlapping windows from half-windows
    return 10 * np.log10(env + 1e-20), hop


def stripe_mask(env_db: np.ndarray, hop: int, sr: int, idx: np.ndarray, jump_db: float, max_ms: float) -> np.ndarray:
    """Which sample indices `idx` are a thin stripe in the spectrogram.

    A stripe rises at least `jump_db` above the median of its surroundings (±STRIPE_HALF_MS without the click
    itself) and is at most `max_ms` wide at half height (half the jump, in dB). Returns a boolean mask per index.
    """
    if idx.size == 0:
        return np.zeros(0, dtype=bool)
    bin_ms = hop * 1000.0 / sr
    half = max(3, int(round(STRIPE_HALF_MS / bin_ms)))
    guard = max(1, int(round(STRIPE_GUARD_MS / bin_ms)))
    wmax = max(1, int(round(max_ms / bin_ms)))
    k = np.clip(idx // hop, half, len(env_db) - half - 1)
    out = np.zeros(len(idx), dtype=bool)
    for i, c in enumerate(k):
        seg = env_db[c - half:c + half + 1]
        bg = np.median(np.concatenate([seg[:half - guard], seg[half + guard + 1:]]))
        j = env_db[c - guard:c + guard + 1].max() - bg
        if j < jump_db:
            continue
        # width: bins above half height within ±2 × max width; a decaying hit stays up for longer
        out[i] = np.sum(env_db[c - 2 * wmax:c + 2 * wmax + 1] > bg + j / 2) <= wmax
    return out


def clipped(x: np.ndarray, min_run: int = 3, dilate: int = 8) -> np.ndarray:
    """Mask of flat runs at the signal's own maximum: such an edge is impulsive, but not a click.

    Runs of at least `min_run` samples at the peak are marked and widened by `dilate` samples on each side
    (grooveclean uses 2): the residual peak of the AR detector sits 1–6 samples next to the run, because noise
    frays the entry into the limiting (tested with an overdriven sine)."""
    x = np.asarray(x)
    n = len(x)
    peak = float(np.max(np.abs(x))) if n else 0.0
    if peak <= 0.0 or n < min_run:
        return np.zeros(n, dtype=bool)
    at_peak = np.abs(x) >= peak * (1.0 - 1e-9)
    run = at_peak[:n - min_run + 1].copy()
    for k in range(1, min_run):
        run &= at_peak[k:n - min_run + 1 + k]
    mask = np.zeros(n, dtype=bool)
    for k in range(min_run):
        mask[k:n - min_run + 1 + k] |= run
    grown = mask.copy()
    for k in range(1, dilate + 1):
        grown[k:] |= mask[:-k]
        grown[:-k] |= mask[k:]
    return grown


def levinson(r: np.ndarray, order: int) -> np.ndarray:
    """Levinson-Durbin recursion, vectorized over many blocks.

    r: (F, order+1) autocorrelation per block → a: (F, order+1) prediction-error filter with a[:, 0] = 1,
    i.e. residual e[n] = Σ_k a[k] · x[n-k].
    """
    nf = r.shape[0]
    a = np.zeros((nf, order + 1))
    a[:, 0] = 1.0
    err = r[:, 0].copy()
    tiny = np.finfo(float).tiny
    for i in range(1, order + 1):
        acc = r[:, i] + np.einsum("fj,fj->f", a[:, 1:i], r[:, i - 1:0:-1])
        k = -acc / err                                   # reflection coefficient
        prev = a[:, 1:i].copy()
        a[:, 1:i] = prev + k[:, None] * prev[:, ::-1]
        a[:, i] = k
        err = np.maximum(err * (1.0 - k * k), tiny)      # never zero: silent blocks would divide by 0
    return a


def _residual_chunks(x: np.ndarray, p: DetectParams):
    """Yield (start index, residual, σ per sample) chunk by chunk over the whole signal."""
    order, frame = p.order, p.frame
    win = np.hanning(frame)
    nfft = 1 << int(np.ceil(np.log2(2 * frame)))         # zero padding: linear, not circular autocorrelation
    step = frame * p.chunk_frames
    hist = np.zeros(order)
    floor = 10 ** (p.sigma_floor_dbfs / 20)
    for start in range(0, len(x), step):
        seg = np.asarray(x[start:start + step], dtype=np.float64)
        n = len(seg)
        nf = -(-n // frame)
        segp = np.pad(seg, (0, nf * frame - n))
        blocks = segp.reshape(nf, frame)

        # autocorrelation of the windowed blocks via FFT (Wiener-Khinchin)
        spec = np.fft.rfft(blocks * win, n=nfft, axis=1)
        r = np.fft.irfft(spec.real ** 2 + spec.imag ** 2, n=nfft, axis=1)[:, :order + 1]
        r[:, 0] = r[:, 0] * (1 + 1e-9) + 1e-20   # regularization (silence, rounding)
        a = levinson(r, order)

        # residual with each block's own coefficients; the samples before a block come from the signal itself
        xe = np.concatenate([hist, segp])
        ext = sliding_window_view(xe, frame + order)[::frame][:nf]
        e = a[:, :1] * ext[:, order:order + frame]
        for k in range(1, order + 1):
            e += a[:, k:k + 1] * ext[:, order - k:order - k + frame]

        sigma = MAD_TO_SIGMA * np.median(np.abs(e), axis=1)
        # include the neighbouring blocks: a block full of clicks must not raise its own σ
        padded = np.pad(sigma, 1, mode="edge")
        sigma = np.median(np.stack([padded[:-2], padded[1:-1], padded[2:]]), axis=0)
        sigma = np.maximum(sigma, floor)
        sigma = np.repeat(sigma, frame)

        # Local σ over ~12 ms: a click (≤ 2 ms) occupies at most 2 of 9 sub-blocks and does not raise the
        # median. A longer noise burst (hi-hat, snare) occupies the majority and raises the threshold for
        # itself → no false alarm.
        e = e.reshape(-1)
        sub = p.local_block
        nsub = len(e) // sub
        blk = MAD_TO_SIGMA * np.median(np.abs(e[:nsub * sub]).reshape(nsub, sub), axis=1)
        centered = ndimage.median_filter(blk, size=p.local_blocks, mode="nearest")
        # Forward window (this and the next 8 sub-blocks ≈ 12 ms): a click is over after 1–2 ms, an attack
        # (drum, plucked string) keeps ringing. Without this window an attack that falls unluckily between
        # sub-blocks would be counted.
        fwd = np.median(sliding_window_view(np.pad(blk, (0, p.local_blocks - 1), mode="edge"),
                                            p.local_blocks), axis=1)
        local = np.repeat(np.maximum(centered, fwd), sub)
        local = np.concatenate([local, np.full(len(e) - len(local), local[-1] if len(local) else 0.0)])
        sigma = np.maximum(sigma, local)

        hist = np.concatenate([hist, seg])[-order:]
        yield start, e[:n], sigma[:n]


def detect_channel(x: np.ndarray, sr: int, p: DetectParams = DetectParams()) -> np.ndarray:
    """Find clicks in one channel.

    x: mono samples (float, ±1), sr: sample rate in Hz. Returns an array of EVENT_DTYPE (ch = 0), one row per
    click: hits closer than `merge_ms` are merged into one event at their strongest sample.
    """
    idx_parts, abs_parts, str_parts = [], [], []
    for start, e, sigma in _residual_chunks(x, p):
        ae = np.abs(e)
        hit = np.flatnonzero(ae > p.threshold * sigma)
        if hit.size and p.stripe_db > 0:
            # clipped peaks (flat runs at the maximum, as in grooveclean) are edges, not clicks
            cl = clipped(x[start:start + len(e)])
            hit = hit[~cl[hit]]
        if hit.size:
            idx_parts.append(hit + start)
            abs_parts.append(ae[hit])
            str_parts.append(ae[hit] / sigma[hit])
    if not idx_parts:
        return np.zeros(0, EVENT_DTYPE)

    idx = np.concatenate(idx_parts)
    absval = np.concatenate(abs_parts)
    strength = np.concatenate(str_parts)

    merge = max(1, int(round(p.merge_ms * sr / 1000)))
    group = np.concatenate([[0], np.cumsum(np.diff(idx) > merge)])
    starts = np.flatnonzero(np.diff(np.concatenate([[-1], group])))
    ends = np.concatenate([starts[1:], [len(idx)]]) - 1
    # strongest hit per group
    order = np.lexsort((-strength, group))
    peak = order[np.flatnonzero(np.diff(np.concatenate([[-1], group[order]])))]

    ev = np.zeros(len(starts), EVENT_DTYPE)
    ev["t"] = idx[peak] / sr
    ev["dur_ms"] = (idx[ends] - idx[starts] + 1) * 1000.0 / sr
    ev["strength"] = strength[peak]
    ev["amp_dbfs"] = 20 * np.log10(np.maximum(absval[peak], 1e-12))
    if p.stripe_db > 0 and len(ev):
        env, hop = hf_envelope(x, sr)
        ev = ev[stripe_mask(env, hop, sr, idx[peak], p.stripe_db, p.stripe_ms)]
    return ev


def merge_channels(per_channel: list[np.ndarray], merge_ms: float = 1.5) -> np.ndarray:
    """Merge the events of several channels: events within `merge_ms` become one event.

    The merged event keeps the values of its strongest channel, the longest duration and the channel bit mask.
    """
    parts = []
    for c, ev in enumerate(per_channel):
        ev = ev.copy()
        ev["ch"] = 1 << c
        parts.append(ev)
    if not parts:
        return np.zeros(0, EVENT_DTYPE)
    allev = np.sort(np.concatenate(parts), order="t")
    if allev.size == 0:
        return allev
    gap = merge_ms / 1000.0
    cut = np.flatnonzero(np.diff(allev["t"]) > gap) + 1
    bounds = np.concatenate([[0], cut, [len(allev)]])
    out = np.zeros(len(bounds) - 1, EVENT_DTYPE)
    for i, (s, e) in enumerate(zip(bounds[:-1], bounds[1:])):
        m = allev[s:e]
        out[i] = m[np.argmax(m["strength"])]
        out[i]["ch"] = np.bitwise_or.reduce(m["ch"])
        out[i]["dur_ms"] = m["dur_ms"].max()
    return out
