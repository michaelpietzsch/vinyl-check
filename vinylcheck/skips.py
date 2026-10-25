"""Skip detector: find groove skips in recordings of music.

When the stylus jumps back one groove, it plays the last revolution again. From the skip on, the signal then
equals the signal exactly one revolution earlier (33⅓ rpm: 1.8 s, 45 rpm: 1.33 s), sample by sample including
groove noise and crackle, because it is the same groove. Music that repeats itself (chorus, loop) is only
similar in sound, not in its noise.

The measure is GCC-PHAT: a cross-correlation of two excerpts in which every frequency counts equally (phase
only). For a true repetition the phases match across all frequencies → a sharp peak close to 1. For music with
long sustained notes only the few bins of those notes match → hardly any peak. This makes the detector
insensitive to pedal points and pads.

Forward skips (one revolution missing) leave no trace in a single recording that could be told apart reliably
from a normal transition. Only the comparison with a second recording of the same side reveals them
(`relative_jumps`).

The run-out (locked) groove is excluded: it repeats on purpose until the stylus is lifted.
"""

from __future__ import annotations

from math import gcd

import numpy as np
from scipy import fft as sfft
from scipy import signal

from . import i18n
from .measure import runs

RATE = 16000              # analysis sample rate (music + groove noise up to 7 kHz is enough)
BAND = (300.0, 7000.0)    # band used for the correlation: above rumble/hum, below the resampling limit
WIN_S = 0.5
HOP_S = 0.25
SEARCH = 0.02             # ±2 % speed around the nominal revolution
THRESH = 0.25             # GCC-PHAT peak from which two excerpts count as the same groove
STRONG = 0.40             # … and from which a skip back in the music counts as certain (true repetition: ≥ 0.5)
PERIOD_TOL = 0.006        # ±0.6 % around the revolution time: a true skip back lies exactly there, rhythm does not
LP_OUTER_MM, LP_INNER_MM = 146.0, 65.0   # estimated groove radius of a 12" LP: lead-in → end of the music


def revolution_s(rpm: float) -> float:
    """Duration of one revolution in seconds."""
    return 60.0 / rpm


def prepare(audio: np.ndarray, sr: int) -> np.ndarray:
    """Mono, resampled to 16 kHz."""
    x = audio.mean(axis=1) if audio.ndim == 2 else audio
    g = gcd(RATE, sr)
    return signal.resample_poly(x.astype(np.float32), RATE // g, sr // g).astype(np.float32)


def _phat(a: np.ndarray, b: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Row-wise GCC-PHAT: where does a (m, w) occur in b (m, w + 2d)? → (peak, position in b)."""
    w, lb = a.shape[1], b.shape[1]
    nfft = sfft.next_fast_len(w + lb)
    fa = sfft.rfft(a, nfft, axis=1)
    fb = sfft.rfft(b, nfft, axis=1)
    fr = np.fft.rfftfreq(nfft, 1 / RATE)
    keep = (fr >= BAND[0]) & (fr <= BAND[1])
    x = fb * np.conj(fa)
    x = np.where(keep, x / np.maximum(np.abs(x), 1e-20), 0)      # phase transform: unit magnitude per bin
    c = sfft.irfft(x, nfft, axis=1)[:, :lb - w + 1]
    c *= nfft / (2 * keep.sum())                    # identical excerpts → 1
    k = np.argmax(c, axis=1)
    return c[np.arange(len(k)), k], k


def _scan(y: np.ndarray, lag: int, d: int, starts: np.ndarray, w: int) -> tuple[np.ndarray, np.ndarray]:
    """GCC-PHAT score and position for every window start against the excerpt `lag` ± d samples earlier."""
    scores = np.zeros(len(starts))
    pos = np.zeros(len(starts), int)
    for s in range(0, len(starts), 256):
        st = starts[s:s + 256]
        a = np.stack([y[i:i + w] for i in st])
        b = np.stack([y[i - lag - d:i - lag + d + w] for i in st])
        sc, k = _phat(a, b)
        scores[s:s + len(st)], pos[s:s + len(st)] = sc, k
    return scores, pos


def position(t: float, groove: tuple[float, float], music_end: float | None, rpm: float) -> dict:
    """Place on the record: share of the side and (at 33⅓) the estimated radius, assuming even groove pitch."""
    g0 = groove[0]
    g1 = music_end if music_end and music_end > g0 else groove[1]
    frac = float(np.clip((t - g0) / max(g1 - g0, 1e-6), 0, 1))
    out = {"position_pct": round(100 * frac, 1)}
    if abs(rpm - 100 / 3) < 1:
        out["radius_mm"] = round(LP_OUTER_MM - (LP_OUTER_MM - LP_INNER_MM) * frac)
    return out


def find_repeats(audio: np.ndarray, sr: int, rpm: float = 100 / 3, groove: tuple[float, float] | None = None,
                 music_end: float | None = None, y: np.ndarray | None = None) -> list[dict]:
    """Skips back (a groove repeats) and the run-out groove. Times in seconds of the recording.

    Each event has kind "backward" (skip back), "uncertain" (probably rhythm) or "run_out" (run-out groove),
    plus how many grooves it jumped, repeats, measured period/rpm, score and position."""
    y = prepare(audio, sr) if y is None else y
    dur = len(y) / RATE
    groove = groove or (0.0, dur)
    T = revolution_s(rpm)
    w, hop = int(WIN_S * RATE), int(HOP_S * RATE)
    d = int(SEARCH * T * RATE)
    lag1 = int(round(T * RATE))
    starts = np.arange(max(lag1 + d, int(groove[0] * RATE)), min(len(y) - w, int(groove[1] * RATE) - w), hop)
    if len(starts) == 0:
        return []
    best = {"starts": starts, "score": np.zeros(len(starts)), "period": np.full(len(starts), T),
            "k": np.ones(len(starts), int)}
    for k in (1, 2):                                  # one or two grooves back
        lag = int(round(k * T * RATE))
        ok = starts >= lag + d
        if not ok.any():
            continue
        sc, pos = _scan(y, lag, d, starts[ok], w)
        period = (lag + d - pos) / RATE / k
        idx = np.flatnonzero(ok)
        better = sc > best["score"][idx]
        best["score"][idx[better]] = sc[better]
        best["period"][idx[better]] = period[better]
        best["k"][idx[better]] = k
    hit = best["score"] >= THRESH
    # bridge single gaps (one window with a click or similar)
    gaps = np.flatnonzero(~hit[1:-1] & hit[:-2] & hit[2:]) + 1
    hit[gaps] = True
    events = []
    for a, b in runs(hit):
        t0 = best["starts"][a] / RATE
        t1 = (best["starts"][b - 1] + w) / RATE
        k = int(np.bincount(best["k"][a:b]).argmax())
        per = float(np.median(best["period"][a:b]))
        if t1 - t0 < 0.5 * T * k:
            continue
        # Run-out groove: runs until the stylus is lifted, or lies behind the end of the music. Clicks often break
        # the repetition of the run-out groove into several sections; without the second condition the earlier
        # ones were reported as skips back (observed: 3 false alarms at 100 % of the side).
        ends = t1 >= min(groove[1], dur) - 1.5 or (music_end is not None and t0 >= music_end - 1.0)
        kind = "run_out" if ends else "backward"
        # Rhythmic music (a riff in strict time produced 20 "skips back" with scores of 0.25–0.46 and periods at
        # the edges of the search window) counts as uncertain: only hits with high similarity AND a period right
        # at the revolution time count as a skip back. The (quiet) run-out groove stays as it is.
        score = float(np.median(best["score"][a:b]))
        if (kind == "backward" and (score < STRONG or abs(per / T - 1) > PERIOD_TOL)
                and b - a < 2 * max(1, int(T / HOP_S))):
            kind = "uncertain"
        ev = {"t": round(float(t0), 2), "until": round(float(t1), 2), "kind": kind, "grooves": k,
              "repeats": max(1, int(round((t1 - t0) / (per * k)))), "period_s": round(per, 4),
              "rpm_measured": round(60 / per, 3), "score": round(float(np.median(best["score"][a:b])), 3)}
        ev |= position(t0, groove, music_end, rpm)
        events.append(ev)
    return events


def _resample_at(y: np.ndarray, pos: np.ndarray) -> np.ndarray:
    """Linear interpolation of y at fractional sample positions."""
    i0 = np.floor(pos).astype(np.int64)
    fr = (pos - i0).astype(np.float32)
    return y[i0] * (1 - fr) + y[i0 + 1] * fr


def relative_jumps(a: np.ndarray, b: np.ndarray, sr: int, slope: float, icpt: float, rpm: float,
                   groove_a: tuple[float, float], skips_a: list | None = None, skips_b: list | None = None,
                   step_s: float = 2.0, ya: np.ndarray | None = None, yb: np.ndarray | None = None) -> list[dict]:
    """Skips between two recordings of the same side.

    Where does an excerpt of A land in B, measured in whole revolutions relative to the time map
    t_B = slope · t_A + icpt? If that changes, one of the two recordings has skipped. Each result carries an
    interpretation ("interpretation", an i18n message): which recording skipped, and in which direction."""
    ya = prepare(a, sr) if ya is None else ya
    yb = prepare(b, sr) if yb is None else yb
    T = revolution_s(rpm)
    w = int(WIN_S * RATE)
    d = int(0.03 * RATE)
    ks = np.arange(-2, 3)
    rows = []
    for ta in np.arange(groove_a[0] + 1.0, groove_a[1] - 1.0 - WIN_S, step_s):
        ia = int(ta * RATE)
        if ia + w > len(ya):
            break
        seg_a = ya[ia:ia + w]
        segs, valid = [], []
        grid = ta - d / RATE + np.arange(w + 2 * d) / RATE
        for k in ks:
            pos = (slope * grid + icpt + k * T * slope) * RATE
            if pos[0] < 0 or pos[-1] >= len(yb) - 1:
                valid.append(False)
                segs.append(np.zeros(w + 2 * d, np.float32))
                continue
            valid.append(True)
            segs.append(_resample_at(yb, pos))
        sc, _ = _phat(np.stack([seg_a] * len(ks)), np.stack(segs))
        sc = np.where(valid, sc, 0.0)
        j = int(np.argmax(sc))
        if sc[j] >= 0.15:
            rows.append((ta, int(ks[j])))
    if len(rows) < 3:
        return []
    t_arr = np.array([r[0] for r in rows])
    k_arr = np.array([r[1] for r in rows])
    # runs of the same revolution offset; a single outlier does not count
    runs = []
    s = 0
    for i in range(1, len(k_arr) + 1):
        if i == len(k_arr) or k_arr[i] != k_arr[s]:
            if i - s >= 2:
                runs.append((s, i, int(k_arr[s])))
            s = i
    out = []
    for (s1, e1, k1), (s2, e2, k2) in zip(runs[:-1], runs[1:]):
        if k1 == k2:
            continue
        t = float((t_arr[e1 - 1] + t_arr[s2]) / 2)
        dk = k2 - k1
        tb = slope * t + icpt + k2 * T * slope
        near_a = any(abs(e["t"] - t) < step_s + 1.0 for e in (skips_a or []) if e["kind"] == "backward")
        near_b = any(abs(e["t"] - tb) < step_s + 1.0 for e in (skips_b or []) if e["kind"] == "backward")
        # A rising offset means A's content now appears later in B: either B played a revolution twice (a skip
        # back, if B has one there) or A lost one (a skip forward). A falling offset is the mirror case.
        if dk > 0:
            who = i18n.msg("skips.b_skipped_back") if near_b else i18n.msg("skips.a_skipped_forward")
        else:
            who = i18n.msg("skips.a_skipped_back") if near_a else i18n.msg("skips.b_skipped_forward")
        out.append({"t_a": round(t, 1), "t_b": round(float(tb), 1), "grooves": abs(dk), "delta": dk,
                    "interpretation": who, "window_s": step_s})
    return out


def same_place(skips_a: list, skips_b: list, slope: float, icpt: float, tol_s: float = 2.0) -> list[dict]:
    """Skips back that lie at the same place on the record in both recordings → more likely damage to the record
    (a scratch) than a matter of the turntable setup."""
    out = []
    for ea in skips_a or []:
        if ea["kind"] != "backward":
            continue
        tb = slope * ea["t"] + icpt
        for eb in skips_b or []:
            if eb["kind"] == "backward" and abs(eb["t"] - tb) <= tol_s:
                out.append({"t_a": ea["t"], "t_b": eb["t"]})
                break
    return out


class EndGrooveWatch:
    """Live during a recording: detects the run-out groove so that the recording stops by itself.

    Run-out groove = the last `revs` revolutions repeat (the same GCC-PHAT search as `find_repeats`) **and** it
    is quiet: at least `quiet_db` below the loudest section so far. The level condition is needed because a
    continuous tone on a test record repeats every revolution as well. A spiral groove (including the silent
    groove) does not repeat. Meant for turntables without an automatic stop at the end of the side.
    """

    def __init__(self, sr: int, rpm: float = 100 / 3, revs: int = 3, quiet_db: float = 30.0,
                 min_s: float = 60.0, every_s: float = 2.0):
        self.sr, self.rpm, self.revs = sr, rpm, revs
        self.T = revolution_s(rpm)
        self.keep = int((revs + 1.5) * self.T * sr)
        self.quiet_db, self.min_s = quiet_db, min_s
        self.every = int(every_s * sr)
        self.buf = np.zeros(0, np.float32)
        self.loudest_db = -200.0
        self.since = 0
        self.frames = 0

    def feed(self, x: np.ndarray) -> bool:
        """x: raw samples (frames, channels) as float. True = run-out groove detected."""
        m = (x.mean(axis=1) if x.ndim == 2 else x).astype(np.float32)
        self.frames += len(m)
        self.since += len(m)
        lvl = 10 * np.log10(float(np.mean(m.astype(np.float64) ** 2)) + 1e-20)
        self.loudest_db = max(self.loudest_db, lvl)
        self.buf = np.concatenate([self.buf, m])[-self.keep:]
        if self.since < self.every or self.frames < self.min_s * self.sr or len(self.buf) < self.keep:
            return False
        self.since = 0
        span = int(self.revs * self.T * self.sr)
        recent = 10 * np.log10(float(np.mean(self.buf[-span:].astype(np.float64) ** 2)) + 1e-20)
        if recent > self.loudest_db - self.quiet_db:
            return False
        y = prepare(self.buf, self.sr)
        dur = len(y) / RATE
        for ev in find_repeats(self.buf[:, None], self.sr, self.rpm, y=y):
            if ev["until"] >= dur - 1.0 and ev["until"] - ev["t"] >= (self.revs - 1) * self.T:
                return True
        return False
