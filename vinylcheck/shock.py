"""Mechanical shocks: the needle drop, the dust cover closing, a hand on the plinth.

Such a shock reaches the stylus through the arm and the platter and excites the tonearm/cartridge resonance
(around 10 Hz): after the impulse the signal rings for a few hundred milliseconds far below the music. A click
from the groove or a sequenced hit has no such tail. So, for a detection, the energy below LP_HZ in the window
AFTER_S after it is compared with the window BEFORE_S before it, on the lateral (L+R) and the vertical (L−R)
signal; the larger rise is the detection's shock gain. From RATIO_DB on, with the ringing at least MIN_DBFS loud,
the detection is a shock and does not count as a click. On nine real sides the needle drops rose by 28–53 dB,
every click in the music and every judged defect by 10 dB at most.
"""

from __future__ import annotations

import numpy as np
from scipy import signal

LP_HZ = 25.0            # the arm resonance sits around 10 Hz; music bass starts above 30 Hz
RATE = 200              # the low-pass signal is decimated to this rate before measuring
BEFORE_S = (0.5, 0.1)   # window before the detection: from −0.5 s to −0.1 s
AFTER_S = (0.02, 0.35)  # window after it: the ringing
RATIO_DB = 15.0         # from this rise of the sub-LP_HZ energy the detection is a shock
MIN_DBFS = -40.0        # … provided the ringing is at least this loud (otherwise it is noise on silence)


def measure(audio: np.ndarray, sr: int, times: np.ndarray) -> np.ndarray:
    """Shock gain in dB per detection time: the rise of the energy below LP_HZ after the detection against before
    it, the larger of lateral and vertical; 0 where the ringing stays below MIN_DBFS. Shape (len(times),)."""
    times = np.asarray(times, float)
    out = np.zeros(len(times))
    if not len(times) or len(audio) < sr:
        return out
    x = np.asarray(audio, np.float32)
    mid = x.mean(axis=1) if x.ndim == 2 else x
    side = (x[:, 0] - x[:, 1]) / 2 if x.ndim == 2 and x.shape[1] > 1 else None
    q = max(1, int(round(sr / RATE)))
    rate = sr / q
    sos = signal.butter(4, LP_HZ, "low", fs=rate, output="sos")
    gains = []
    for y in (mid, side):
        if y is None:
            continue
        lp = signal.sosfiltfilt(sos, signal.resample_poly(y.astype(np.float64), 1, q))
        p = lp ** 2
        cum = np.concatenate([[0.0], np.cumsum(p)])

        def mean_power(a, b):
            a, b = np.clip(a, 0, len(p)), np.clip(b, 0, len(p))
            return (cum[b] - cum[a]) / np.maximum(b - a, 1)

        i = np.round(times * rate).astype(int)
        before = mean_power(i - int(BEFORE_S[0] * rate), i - int(BEFORE_S[1] * rate))
        after = mean_power(i + int(AFTER_S[0] * rate), i + int(AFTER_S[1] * rate))
        level = 10 * np.log10(np.maximum(after, 1e-20))
        g = 10 * np.log10(np.maximum(after, 1e-20) / np.maximum(before, 1e-20))
        g[level < MIN_DBFS] = 0.0
        gains.append(g)
    return np.round(np.max(np.stack(gains), axis=0), 1)


def is_shock(gain_db) -> np.ndarray:
    """Mask: which detections are mechanical shocks (None or NaN = not measured)."""
    g = np.array([(np.nan if v is None else v) for v in gain_db], dtype=float)
    return np.nan_to_num(g, nan=-1.0) >= RATIO_DB
