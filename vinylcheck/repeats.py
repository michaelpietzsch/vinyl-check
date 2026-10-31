"""Does a detected click repeat with the music? A defect does not; a sequenced drum hit does.

Electronic music with a drum machine or a sequencer plays the same hard transient bar after bar. To the click
detector such a hit looks like a click: a thin stripe in the treble, strong in the prediction residual. On a
record of this kind the detector reported 75 clicks per minute in the music and graded it G, while the record
was fine. Timing alone (is the detection on the beat grid?) did not separate them: the tempo drifts over a side
and the grid smears. What does separate them is the music around the detection: a defect sits in music that
occurs once, a sequenced hit sits in music that comes back.

For every click or pop in the music the treble envelope (detect.hf_envelope, 2.7 ms hop) around it, ±CTX_S, is
compared with the envelope within ±SEARCH_S: where the surroundings match (normalized cross-correlation at least
CTX_NCC, at least MIN_LAG_S away), the spike itself must be there too, within ±SPIKE_TOL_BINS of the same spot and
within SPIKE_DB_TOL of the same jump above its surroundings. Each such place is one repeat. From MIN_REPEATS repeats
the detection counts as repeating with the music. Lags at multiples of one revolution do not count: a scratch
across the grooves repeats there too, and the music under a sustained chord might match.

On recordings of a sequencer record about half of the music clicks repeated (the rest are one-off accents, intros
and fills); on live rock, jazz and a studio rock record 3–9 % did, by coincidence; of 25 clicks a listener had
judged defects, none repeated. The count is stored per event (`rep`) and summarised in the metrics (`repeats`); it
does not change the grade yet. The counts are a finding for the listener and a feature for the learning model to
come; which figure the grade should use is decided once the counts have been checked against judgements by ear
and master comparisons.
"""

from __future__ import annotations

import numpy as np

from . import detect

CTX_S = 0.20            # half width of the surroundings compared
SEARCH_S = 8.0          # how far before and after to look for a repeat
MIN_LAG_S = 0.25        # closer is the event itself or its own decay
CTX_NCC = 0.80          # the surroundings must match this well …
SPIKE_TOL_BINS = 2      # … and the spike must sit within ±2 bins (±5.3 ms at 48 kHz) of the same spot
SPIKE_DB_TOL = 8.0      # with a jump above its surroundings within this many dB of the candidate's …
SPIKE_REL = 0.6         # … and at least this share of it …
SPIKE_MIN_DB = 6.0      # … and at least this much: dense music has small jumps everywhere
MIN_REPEATS = 2         # from this many repeats a detection repeats with the music
REV_TOL_S = 0.03        # lags this close to a multiple of one revolution do not count
BG_HALF_MS = 60.0       # surroundings whose median is the reference of a spike's jump (as in detect.stripe_mask)
BG_GUARD_MS = 2.7


def _jump(env: np.ndarray, c: int, half: int, guard: int) -> float:
    """Rise of the envelope at bin `c` above the median of its ±half bins (without ±guard)."""
    lo, hi = max(0, c - half), min(len(env), c + half + 1)
    if hi - lo < 2 * guard + 3:
        return 0.0
    bg = np.median(np.concatenate([env[lo:max(lo, c - guard)], env[min(hi, c + guard + 1):hi]]))
    return float(env[max(0, c - guard):c + guard + 1].max() - bg)


def _repeats_at(env: np.ndarray, c: int, w: int, search: int, half: int, guard: int, jump0: float,
                min_lag: int, rev_bins: float | None, rev_tol: float) -> int:
    """Number of places within ±search bins where the surroundings of bin `c` match and the spike repeats."""
    lo, hi = c - w, c + w + 1
    if lo < 0 or hi > len(env):
        return 0
    ctx = env[lo:hi]
    ctx = (ctx - ctx.mean()) / (ctx.std() + 1e-9)
    s0, s1 = max(0, c - search - w), min(len(env), c + search + w + 1)
    seg = env[s0:s1]
    n = len(ctx)
    if len(seg) < n + 2:
        return 0
    dot = np.correlate(seg, ctx, mode="valid")
    cs = np.cumsum(np.concatenate([[0.0], seg]))
    cs2 = np.cumsum(np.concatenate([[0.0], seg ** 2]))
    sums, sums2 = cs[n:] - cs[:-n], cs2[n:] - cs2[:-n]
    std = np.sqrt(np.maximum(sums2 / n - (sums / n) ** 2, 1e-12))
    ncc = dot / (n * std)
    centres = np.arange(len(ncc)) + s0 + w
    lag = centres - c
    ok = (ncc >= CTX_NCC) & (np.abs(lag) >= min_lag)
    if rev_bins:                                                  # multiples of one revolution: a scratch, not music
        k = np.rint(np.abs(lag) / rev_bins)
        ok &= ~((k >= 1) & (np.abs(np.abs(lag) - k * rev_bins) <= rev_tol))
    idx = np.flatnonzero(ok)
    count = 0
    for i in idx:
        if (i > 0 and ncc[i - 1] > ncc[i]) or (i < len(ncc) - 1 and ncc[i + 1] > ncc[i]):
            continue                                              # local maxima of the match only
        c2 = int(centres[i])
        best = max(_jump(env, c2 + d, half, guard) for d in range(-SPIKE_TOL_BINS, SPIKE_TOL_BINS + 1))
        if best >= max(jump0 - SPIKE_DB_TOL, SPIKE_REL * jump0, SPIKE_MIN_DB):
            count += 1
    return count


def count(audio: np.ndarray, sr: int, times: np.ndarray, channels: np.ndarray, rpm: float | None = None,
          envs: list[np.ndarray] | None = None) -> np.ndarray:
    """Repeats with the music per event. `channels` is the detector's channel bit mask per event (1 = L, 2 = R,
    3 = both). `envs`: the treble envelopes per channel if already computed (detect.hf_envelope), else they are
    computed here. Returns an int array, one count per event (0 where the event is too close to an edge)."""
    out = np.zeros(len(times), dtype=int)
    if not len(times):
        return out
    x = np.asarray(audio)
    if x.ndim == 1:
        x = x[:, None]
    if envs is None:
        envs = [detect.hf_envelope(x[:, c], sr)[0] for c in range(x.shape[1])]
    hop = detect.STRIPE_WIN // 2
    bin_s = hop / sr
    w, search = int(round(CTX_S / bin_s)), int(round(SEARCH_S / bin_s))
    half, guard = int(round(BG_HALF_MS / 1000 / bin_s)), max(1, int(round(BG_GUARD_MS / 1000 / bin_s)))
    min_lag = int(round(MIN_LAG_S / bin_s))
    rev_bins = (60.0 / rpm) / bin_s if rpm else None
    rev_tol = REV_TOL_S / bin_s
    mixed = (envs[0] + envs[1]) / 2 if len(envs) > 1 else envs[0]
    for i, (t, ch) in enumerate(zip(times, channels)):
        env = envs[0] if ch == 1 or len(envs) == 1 else envs[1] if ch == 2 else mixed
        c = int(round(float(t) / bin_s))
        if c < 0 or c >= len(env):
            continue
        out[i] = _repeats_at(env, c, w, search, half, guard, _jump(env, c, half, guard), min_lag, rev_bins, rev_tol)
    return out


def repeating(rep) -> np.ndarray:
    """Mask: which events repeat with the music (None or NaN = no count)."""
    r = np.array([(-1 if v is None else v) for v in rep], dtype=float)
    return np.nan_to_num(r, nan=-1.0) >= MIN_REPEATS
