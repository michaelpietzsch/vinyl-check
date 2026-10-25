"""Engineer mode: measure a recording of a vinyl side against the digital master of that side.

Built for approving test pressings against the master. With the master as a reference, things can be measured that
otherwise can only be estimated:

1. Alignment (like compare.align, master = A, recording = B): offset and speed of the whole chain against the master.
   Subtracting the turntable speed measured during calibration leaves the error of cut and pressing. If no common
   music is found, the alignment is retried per channel (one channel may be inverted, which cancels the mid signal)
   and then against the masters of the other sides (wrong side played, labels swapped).
2. Wow per revolution: in three sections (start, middle, end of the side) the time offset against the master every
   50 ms (0.1 s windows, sub-sample position by parabolic interpolation), fitted with a sine at the rotation
   frequency. An off-center hole (eccentricity e) modulates the groove speed by ±e/r, so the variation grows toward
   the inside: e ≈ variation · radius. The radius is estimated (music from 146 to 65 mm at a constant pitch).
3. Channels: correlation of mid (L+R) and side (L−R) against the master, 200 Hz–3 kHz, using the fine offset from 2.
4. Frequency response vinyl/master for each third of the side in third-octave bands, relative to 1 kHz. The groove
   noise (quiet passages of the recording) is subtracted first. Mid and side are kept apart: if the side signal drops
   in the bass, the bass was summed to mono during cutting; if the treble drops toward the end, it is inner-groove loss.
5. Level per track relative to the master, referred to the median of all tracks.
6. Clicks: every event of the analysis is mapped into the master. If the master explains the peak (2 kHz high-pass,
   ±3 ms, scaled by the level ratio, at least half of the peak), it is music; otherwise it is a defect on the vinyl.
7. Gaps: where the master is silent, everything on the vinyl is noise or echo. Noise (also A-weighted) and its
   distance to the music; pre-echo = copy of the next entry one revolution early, post-echo = copy of the end one
   revolution late (normalized cross-correlation; level relative to the music).

The result is a plain JSON-able dict (stored in the database). User-facing texts in it (reason, warnings, verdicts,
section labels) are i18n messages, rendered in the reader's language when the result is served.
"""

from __future__ import annotations

import time

import numpy as np
from scipy import signal

from . import analysis, compare, i18n

VERSION = 1
HOP_S = 0.01                                      # hop of the level envelopes (s)
R_OUT_MM, R_IN_MM = 146.0, 65.0                   # radius at music start / end of a full LP side (estimated)
THIRD_OCT = (25, 31.5, 40, 50, 63, 80, 100, 125, 160, 200, 250, 315, 400, 500, 630, 800, 1000, 1250, 1600, 2000,
             2500, 3150, 4000, 5000, 6300, 8000, 10000, 12500, 16000)
SILENT_REL_DB = 45.0                              # master counts as silent this far below the music level (95th pct.)
ECHO_WARN_DB = -50.0                              # pre-echo from this level (relative to the music) is a warning
CLICK_SHARP = 12.0                                # residual peak this far above the residual floor: vinyl click
ECHO_MIN_Q, ECHO_LAG_TOL_S = 0.12, 0.004          # echo found from this correlation, one revolution away (±4 ms)



# ─── Helpers ─────────────────────────────────────────────────────────────────
def load(path, sr: int) -> np.ndarray:
    """Read a master (WAV/FLAC/AIFF) as (n, 2) float32, resampled to the recording's sample rate `sr`."""
    import soundfile as sf
    x, msr = sf.read(str(path), dtype="float32", always_2d=True)
    if x.shape[1] == 1:
        x = np.repeat(x, 2, axis=1)
    x = x[:, :2]
    if msr != sr:
        g = int(np.gcd(int(msr), int(sr)))
        x = signal.resample_poly(x, sr // g, msr // g, axis=0).astype(np.float32)
    return np.ascontiguousarray(x)


# Filters run in float32 (coefficients and signal): otherwise sosfilt works on the whole side in float64 and needs twice
# the memory; a 20-minute side peaked at 5.8 GB, too much for a small machine that also runs the service.
def _bp(x: np.ndarray, sr: int, lo: float, hi: float) -> np.ndarray:
    sos = signal.butter(4, [lo, hi], btype="band", fs=sr, output="sos").astype(np.float32)
    return signal.sosfilt(sos, np.asarray(x, dtype=np.float32), axis=0)


def _hp(x: np.ndarray, sr: int, f: float, order: int = 4) -> np.ndarray:
    sos = signal.butter(order, f, btype="high", fs=sr, output="sos").astype(np.float32)
    return signal.sosfilt(sos, np.asarray(x, dtype=np.float32), axis=0)


def _meansq(x: np.ndarray, block: int = 1 << 20) -> float:
    """Mean power, accumulated block by block in float64 (no float64 copy of the whole side)."""
    if not x.size:
        return 0.0
    tot = 0.0
    for a in range(0, len(x), block):
        b = x[a:a + block].astype(np.float64)
        tot += float(np.sum(b * b))
    return tot / x.size


def _db(p: float) -> float | None:
    return None if p is None or p <= 0 or not np.isfinite(p) else float(10 * np.log10(p))


def _rms_db(x: np.ndarray) -> float | None:
    return _db(_meansq(x)) if x.size else None


def _envelope_db(x: np.ndarray, sr: int) -> np.ndarray:
    hop = int(sr * HOP_S)
    n = len(x) // hop
    xr = x[:n * hop].reshape(n, hop)
    p = np.einsum("ij,ij->i", xr, xr, dtype=np.float64) / hop       # without a squared copy of the whole side
    return 10 * np.log10(p + 1e-14)


def _ncc(seg_a: np.ndarray, seg_b: np.ndarray) -> np.ndarray:
    """Normalized cross-correlation of seg_a at every lag in seg_b (the sign is kept)."""
    c = signal.correlate(seg_b, seg_a, mode="valid", method="fft")
    csum = np.concatenate([[0.0], np.cumsum(seg_b.astype(np.float64) ** 2)])
    w = len(seg_a)
    norm_b = np.sqrt(np.maximum(csum[w:] - csum[:-w], 1e-20))
    return c / (norm_b * np.sqrt(np.sum(seg_a.astype(np.float64) ** 2)) + 1e-20)


def _parabola(y: np.ndarray, k: int) -> float:
    """Sub-sample offset of the peak at index k (parabola through the peak and its two neighbors)."""
    if k <= 0 or k >= len(y) - 1:
        return 0.0
    d = y[k - 1] - 2 * y[k] + y[k + 1]
    return 0.0 if d == 0 else float(0.5 * (y[k - 1] - y[k + 1]) / d)


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """(start, end) index pairs of the True runs in mask (end exclusive)."""
    m = np.concatenate([[False], mask, [False]]).astype(np.int8)
    d = np.diff(m)
    return list(zip(np.flatnonzero(d == 1), np.flatnonzero(d == -1)))


def _maps(al: dict):
    """Time mappings master → recording and recording → master from an alignment."""
    sl, ic = al["slope"], al["intercept"]
    return (compare._mapper(al["map_a"], al["map_b"], sl, ic),
            compare._mapper(al["map_b"], al["map_a"], 1 / sl, -ic / sl))


def _parts() -> list[dict]:
    """Labels of the three sections of a side (start, middle, end) as i18n messages."""
    return [i18n.msg("master.part_start"), i18n.msg("master.part_middle"), i18n.msg("master.part_end")]


def radius_mm(frac: float) -> float:
    """Groove radius at position frac (0 = music start, 1 = music end), assuming constant pitch (area ∝ time)."""
    frac = min(1.0, max(0.0, frac))
    return float(np.sqrt(R_OUT_MM ** 2 - (R_OUT_MM ** 2 - R_IN_MM ** 2) * frac))


# ─── 1. Alignment with polarity search ───────────────────────────────────────
# Own variant of compare.align: its fine anchors (1 s windows, 150 Hz–4 kHz) smear when the speed varies by ±0.4 %
# within a window (off-center hole, inner grooves); anchors then go missing and false matches appear (test: speed
# 0.372 instead of 0.400 %). Here: short windows (0.3 s, 100 Hz–1.5 kHz) every 2 s with parabolic sub-sample position,
# a robust line fit, and the mapping is smoothed over ±15 s. The variation per revolution averages out of the mapping
# and remains in the residual for the wow measurement.
MIN_ANCHORS, MIN_SHARE = 5, 0.3


def _anchors(fa, fb, sr, lev, loud, slope, icpt, win_s=0.3, search_s=0.25, spacing_s=2.0, min_q=0.4):
    """Anchor points (t_master, t_recording, quality, sign) from short loud master windows found in the recording."""
    w, s = int(win_s * sr), int(search_s * sr)
    out, tried = [], 0
    for ta in np.arange(1.0, len(fa) / sr - win_s - 1.0, spacing_s):
        li = int(ta / compare.ENV_HOP_S)
        if lev[li:li + int(win_s / compare.ENV_HOP_S)].mean() < loud:
            continue
        pos = (slope * (ta - search_s + np.arange(w + 2 * s) / sr) + icpt) * sr
        if pos[0] < 0 or pos[-1] >= len(fb) - 1:
            continue
        tried += 1
        ia = int(ta * sr)
        y = _ncc(fa[ia:ia + w], compare._sample(fb, pos))
        k = int(np.argmax(np.abs(y)))                      # magnitude: inverted recordings correlate negatively
        if abs(y[k]) < min_q:
            continue
        sgn = 1.0 if y[k] >= 0 else -1.0
        d = (k + _parabola(y * sgn, k) - s) / sr
        out.append((ta, slope * (ta + d) + icpt, float(abs(y[k])), sgn))
        if len(out) >= 3:                                  # keep tracking the drift
            arr = np.array(out)
            slope, icpt = np.polyfit(arr[:, 0], arr[:, 1], 1)
    return out, tried


def _align_master(mm: np.ndarray, vv: np.ndarray, sr: int) -> dict:
    """Align a mono master signal to a mono recording signal: coarse offset, anchors, outlier removal, smoothing."""
    offset = compare.coarse_offset(mm, vv, sr)
    sos = signal.butter(4, [100, 1500], btype="band", fs=sr, output="sos")
    fa, fb = signal.sosfilt(sos, mm).astype(np.float32), signal.sosfilt(sos, vv).astype(np.float32)
    lev = compare._envelope(fa, sr)
    loud = np.percentile(lev, 60) - 1.0
    anchors, tried = _anchors(fa, fb, sr, lev, loud, 1.0, offset)
    out = {"coarse_offset_s": round(offset, 4), "anchors": len(anchors), "tried": tried}
    if len(anchors) < MIN_ANCHORS:
        return out | {"ok": False, "anchors_used": len(anchors)}
    arr = np.array(anchors)
    keep = np.ones(len(arr), bool)
    for _ in range(5):                                     # drop outliers (repeated choruses, false matches)
        sl, ic = np.polyfit(arr[keep, 0], arr[keep, 1], 1)
        res = np.abs(arr[:, 1] - (sl * arr[:, 0] + ic))
        new = res <= max(0.003, 4 * np.median(res[keep]))
        if new.sum() < MIN_ANCHORS or np.array_equal(new, keep):
            break
        keep = new
    arr = arr[keep]
    used = len(arr)
    if used < max(MIN_ANCHORS, MIN_SHARE * tried):
        return out | {"ok": False, "anchors_used": used}
    sl, ic = np.polyfit(arr[:, 0], arr[:, 1], 1)
    smooth = np.empty(used)
    for i, t in enumerate(arr[:, 0]):                      # local line over ±15 s
        sel = np.abs(arr[:, 0] - t) <= 15.0
        if sel.sum() >= 4:
            a, b = np.polyfit(arr[sel, 0], arr[sel, 1], 1)
            smooth[i] = a * t + b
        else:
            smooth[i] = sl * t + ic
    return out | {"ok": True, "anchors_used": used, "quality": round(float(np.median(arr[:, 2])), 3),
                  "speed_ratio": round(float(1 / sl), 7), "offset_s": round(float(ic), 4), "slope": float(sl),
                  "intercept": float(ic), "map_a": arr[:, 0].tolist(), "map_b": smooth.tolist(),
                  "raw_b": arr[:, 1].tolist()}


def _channel_signs(master, vinyl, sr, al, win_s=0.3, n_max=40) -> tuple[int, int]:
    """Sign of the correlation per channel at the anchors. Uses the anchors' own unsmoothed positions: there the offset
    is right to a fraction of a sample, even with wow. 100 Hz–1.5 kHz, filtered locally per window."""
    sos = signal.butter(4, [100, 1500], btype="band", fs=sr, output="sos")
    w, mg = int(win_s * sr), int(0.1 * sr)
    ta, tb = np.array(al["map_a"]), np.array(al["raw_b"])
    idx = np.linspace(0, len(ta) - 1, min(n_max, len(ta))).astype(int)
    out = []
    for c in range(2):
        cs = []
        for i in idx:
            ia = int(ta[i] * sr)
            pos = (tb[i] + (np.arange(-mg, w + mg) / sr) * al["slope"]) * sr
            if ia - mg < 0 or ia + w + mg > len(master) or pos[0] < 0 or pos[-1] >= len(vinyl) - 1:
                continue
            a = signal.sosfilt(sos, master[ia - mg:ia + w + mg, c])[mg:mg + w]
            b = signal.sosfilt(sos, compare._sample(vinyl[:, c], pos))[mg:mg + w]
            if np.std(a) > 0 and np.std(b) > 0:
                cs.append(float(np.corrcoef(a, b)[0, 1]))
        med = float(np.median(cs)) if cs else 1.0
        out.append(-1 if med < -0.1 else 1)
    return out[0], out[1]


# Polarity per (sign left, sign right). The values are stored and compared elsewhere (web.py, app.js); "none"
# (no match) is returned when the recording does not fit the master at all.
POLARITY = {(1, 1): "ok", (-1, -1): "inverted", (1, -1): "right_inverted", (-1, 1): "left_inverted"}


def _align_any(master: np.ndarray, vinyl: np.ndarray, sr: int) -> tuple[dict, str, np.ndarray]:
    """Align master → recording, first via the mid signal (L+R), otherwise per channel (if one channel is inverted,
    only the side signal is left of the mid). Then the polarity per channel. Returns (alignment, polarity, corrected
    recording); polarity is one of the POLARITY values or "none" (does not match)."""
    mm = master.mean(axis=1)
    al = None
    for msig, vsig in ((mm, vinyl.mean(axis=1)), (master[:, 0], vinyl[:, 0]), (master[:, 1], vinyl[:, 1])):
        al = _align_master(msig, vsig, sr)
        if al["ok"]:
            break
    else:
        return al, "none", vinyl
    gl, gr = _channel_signs(master, vinyl, sr, al)
    pol = POLARITY[(gl, gr)]
    if pol == "ok":
        return al, pol, vinyl
    vinyl *= np.array([gl, gr], dtype=vinyl.dtype)        # in place: a second copy cost 460 MB
    again = _align_master(mm, vinyl.mean(axis=1), sr)     # with corrected polarity the mid signal carries again
    return (again if again["ok"] else al), pol, vinyl


# ─── 2. Wow per revolution ───────────────────────────────────────────────────
def _local_lags(fm: np.ndarray, fv: np.ndarray, sr: int, m2v, t0: float, t1: float, thr: float,
                step: float = 0.05, win: float = 0.1, search: float = 0.003):
    """Fine offset recording vs. master every `step` s between t0 and t1 (master time). Windows quieter than `thr`
    (RMS) or with a correlation below 0.6 are skipped. Returns (window centers, lags in s, qualities)."""
    w, s = int(win * sr), int(search * sr)
    ts, lags, qs = [], [], []
    for t in np.arange(t0, t1 - win, step):
        im = int(t * sr)
        seg_m = fm[im:im + w]
        if len(seg_m) < w or np.sqrt(np.mean(seg_m.astype(np.float64) ** 2)) < thr:
            continue
        pos = m2v(t - search + np.arange(w + 2 * s) / sr) * sr
        if pos[0] < 0 or pos[-1] >= len(fv) - 1:
            continue
        y = _ncc(seg_m, compare._sample(fv, pos))
        k = int(np.argmax(y))
        if y[k] < 0.6 or k == 0 or k == len(y) - 1:
            continue
        ts.append(t + win / 2)                              # the offset belongs to the window center
        lags.append((k + _parabola(y, k) - s) / sr)
        qs.append(float(y[k]))
    return np.array(ts), np.array(lags), np.array(qs)


def _fit_rev(ts: np.ndarray, lags: np.ndarray, omega0: float) -> dict | None:
    """Sine at the rotation frequency (searched within ±3 %) plus a quadratic trend, outliers dropped. The time-offset
    amplitude A (s) at angular frequency ω gives the relative speed variation A·ω."""
    if len(ts) < 60:
        return None
    tc = ts - ts.mean()
    keep = np.ones(len(ts), bool)
    best = None
    for _ in range(2):
        best = None
        for om in omega0 * np.linspace(0.97, 1.03, 61):
            X = np.column_stack([np.ones_like(tc), tc, tc ** 2, np.sin(om * ts), np.cos(om * ts)])
            coef, *_ = np.linalg.lstsq(X[keep], lags[keep], rcond=None)
            amp = float(np.hypot(coef[3], coef[4]))
            if best is None or amp > best[0]:
                best = (amp, om, coef, X)
        amp, om, coef, X = best
        res = lags - X @ coef
        mad = np.median(np.abs(res[keep])) + 1e-9
        new = np.abs(res) <= 5 * 1.4826 * mad
        if new.sum() < 60 or np.array_equal(new, keep):
            break
        keep = new
    amp, om, coef, X = best
    res = (lags - X @ coef)[keep]
    return {"wow_pct": round(100 * amp * om, 4), "omega": float(om), "amp_s": amp,
            "resid_us": round(float(np.std(res)) * 1e6, 1), "points": int(keep.sum())}


def _wow(ts_all: np.ndarray, lags_all: np.ndarray, m2v, slope: float, music: tuple[float, float],
         rpm: float) -> tuple[dict, list]:
    """Wow per revolution in three sections (start, middle, end) from the continuous fine offset. Returns the result
    and the master time ranges of the sections (reused for the channel measurement)."""
    ms0, ms1 = music
    span = ms1 - ms0
    L = float(min(24.0, max(6.0, span / 4)))
    starts = [ms0 + 3.0, ms0 + span / 2 - L / 2, ms1 - 3.0 - L]
    omega0 = 2 * np.pi * (rpm / 60) * slope            # revolutions per second of recording → per second of master
    segs, ranges = [], []
    for lab, a in zip(_parts(), starts):
        a = max(ms0, a)
        b = min(ms1, a + L)
        sel = (ts_all >= a) & (ts_all < b)
        ranges.append((a, b))
        fit = _fit_rev(ts_all[sel], lags_all[sel], omega0)
        r = radius_mm(((a + b) / 2 - ms0) / span if span > 0 else 0.0)
        seg = {"label": lab, "t_m": [round(a, 2), round(b, 2)], "t_v": [round(float(m2v(a)), 2), round(float(m2v(b)), 2)],
               "radius_mm": round(r, 1), "points": int(sel.sum())}
        if fit:
            seg.update({"wow_pct": fit["wow_pct"], "ecc_mm": round(fit["wow_pct"] / 100 * r, 3), "resid_us": fit["resid_us"]})
        segs.append(seg)
    ok = [s for s in segs if s.get("wow_pct") is not None]
    out = {"segments": segs, "rev_s": round(60 / rpm, 4)}
    if ok:
        out["wow_pct_max"] = max(s["wow_pct"] for s in ok)
        out["ecc_mm"] = round(float(np.median([s["ecc_mm"] for s in ok])), 3)
        first, last = ok[0], ok[-1]
        if len(ok) >= 2 and first["wow_pct"] > 0 and last is not first:
            # An off-center hole makes the variation grow with 1/r: compare the observed growth from the first to the
            # last section with the growth eccentricity would cause (ratio of the radii).
            obs = last["wow_pct"] / first["wow_pct"]
            ecc = first["radius_mm"] / last["radius_mm"]
            out["ratio_obs"], out["ratio_ecc"] = round(obs, 2), round(ecc, 2)
            if out["wow_pct_max"] < 0.05:
                out["verdict"] = i18n.msg("master.wow_unremarkable")
            elif obs >= 1 + 0.5 * (ecc - 1):
                out["verdict"] = i18n.msg("master.wow_eccentric")
            elif obs <= 1.15:
                out["verdict"] = i18n.msg("master.wow_constant")
            else:
                out["verdict"] = i18n.msg("master.wow_increases_slightly")
    return out, ranges


def _fine_mapper(m2v, slope: float, ts: np.ndarray, lags: np.ndarray):
    """Master → recording including the continuous fine offset (every 50 ms, outliers dropped). Linear between the
    points, plain m2v outside the music. This places even wow in the µs range correctly."""
    if len(ts) < 10:
        return m2v
    med = signal.medfilt(lags, 5)                          # only drop outliers, do not smooth: a median over 250 ms
    dev = np.abs(lags - med)                               # flattens the peaks of the variation by ~70 µs
    keep = dev <= max(30e-6, 5 * 1.4826 * float(np.median(dev)))
    ts, lags = ts[keep], lags[keep]

    def f(t):
        t = np.asarray(t, dtype=float)
        inside = (t >= ts[0]) & (t <= ts[-1])
        return m2v(t) + np.where(inside, np.interp(t, ts, lags), 0.0) * slope
    return f


def _inverse(fmap, t0: float, t1: float, step: float = 0.01):
    """Inverse of a monotonic mapping via a grid (10 ms; the variation per revolution is linear in between)."""
    grid = np.arange(t0, t1 + step, step)
    img = fmap(grid)

    def g(t):
        return np.interp(np.asarray(t, dtype=float), img, grid, left=np.nan, right=np.nan)
    return g


# ─── 3. Channels ─────────────────────────────────────────────────────────────
def _channels(master, vinyl, sr, fine, ranges) -> dict:
    """Per-channel and mid/side correlation with the master, L/R balance and a check for swapped channels."""
    mL, mR, vL, vR = [], [], [], []
    for ta, tb in ranges:
        a, b = int(ta * sr), int(tb * sr)
        if b - a < sr:
            continue
        grid = np.arange(a, b) / sr
        pos = fine(grid) * sr
        okp = (pos >= 0) & (pos < len(vinyl) - 1)
        grid, pos = grid[okp], pos[okp]
        mi = (grid * sr).astype(np.int64)
        mL.append(master[mi, 0]); mR.append(master[mi, 1])
        vL.append(compare._sample(vinyl[:, 0], pos)); vR.append(compare._sample(vinyl[:, 1], pos))
    if not mL:
        return {"verdict": None}
    mL, mR, vL, vR = (_bp(np.concatenate(x), sr, 200, 3000) for x in (mL, mR, vL, vR))
    cc = lambda a, b: float(np.corrcoef(a, b)[0, 1])
    mid_m, side_m, mid_v, side_v = mL + mR, mL - mR, vL + vR, vL - vR
    side_rel = _db(_meansq(side_m) / max(_meansq(mid_m), 1e-20))
    out = {"c_ll": round(cc(mL, vL), 3), "c_rr": round(cc(mR, vR), 3), "c_lr": round(cc(mL, vR), 3), "c_rl": round(cc(mR, vL), 3),
           "c_mid": round(cc(mid_m, mid_v), 3), "c_side": round(cc(side_m, side_v), 3) if side_rel and side_rel > -30 else None,
           "side_rel_db": round(side_rel, 1) if side_rel is not None else None}
    pl = _meansq
    out["balance_db"] = round(_db(pl(vL) / pl(vR)) - _db(pl(mL) / pl(mR)), 2) if min(pl(vL), pl(vR), pl(mL), pl(mR)) > 0 else None
    # Swapped channels turn the side signal upside down (L−R becomes R−L) and make the cross correlations stronger
    # than the straight ones. The side signal is only trusted when the master has enough stereo content.
    swapped = out["c_side"] is not None and out["c_side"] < -0.2 and (out["c_lr"] + out["c_rl"]) > (out["c_ll"] + out["c_rr"])
    out["swapped"] = bool(swapped)
    out["verdict"] = i18n.msg("master.channels_swapped") if swapped else "ok"
    return out


# ─── 4. Frequency response ───────────────────────────────────────────────────
def _band_sum(spec: np.ndarray, freqs: np.ndarray) -> np.ndarray:
    """Power spectrum summed into the third-octave bands of THIRD_OCT (NaN for bands without a bin)."""
    out = np.zeros(len(THIRD_OCT))
    for i, fc in enumerate(THIRD_OCT):
        lo, hi = fc / 2 ** (1 / 6), fc * 2 ** (1 / 6)
        sel = (freqs >= lo) & (freqs < hi)
        out[i] = spec[sel].sum() if sel.any() else np.nan
    return out


def _spectra(x: np.ndarray, sr: int, starts: np.ndarray, n: int, win: np.ndarray):
    """Mean power spectra of mid and side over frames starting at `starts` (samples). Returns (mid, side, frames)."""
    acc_m = np.zeros(n // 2 + 1)
    acc_s = np.zeros(n // 2 + 1)
    k = 0
    for i in starts:
        i = int(i)
        if i < 0 or i + n > len(x):
            continue
        fr = x[i:i + n].astype(np.float64)
        acc_m += np.abs(np.fft.rfft((fr[:, 0] + fr[:, 1]) * 0.5 * win)) ** 2
        acc_s += np.abs(np.fft.rfft((fr[:, 0] - fr[:, 1]) * 0.5 * win)) ** 2
        k += 1
    return (acc_m / k, acc_s / k, k) if k else (None, None, 0)


def _freq(master, vinyl, sr, m2v, music, quiet_v, speed_ratio: float = 1.0) -> dict:
    """Frequency response vinyl/master (mid and side, third-octave bands) for each third of the side, groove noise
    subtracted, each third referred to its own 1 kHz band. Also the treble loss toward the end and the bass in the
    side signal (mono bass from the cut)."""
    n = 8192
    win = np.hanning(n)
    freqs = np.fft.rfftfreq(n, 1 / sr)
    lev = _envelope_db(master.mean(axis=1), sr)
    ms0, ms1 = music
    i0, i1 = int(ms0 / HOP_S), int(ms1 / HOP_S)
    loud = np.percentile(lev[i0:i1], 95) if i1 > i0 else -60
    # Groove noise from the quiet passages of the recording
    nst = [s for a, b in quiet_v for s in np.arange(int(a * sr) + n, int(b * sr) - 2 * n, n)]
    noise_m, noise_s, nk = _spectra(vinyl, sr, np.array(nst[:400]), n, win)
    bands_ref = THIRD_OCT.index(1000)
    segs = []
    thirds = np.linspace(ms0, ms1, 4)
    for lab, a, b in zip(_parts(), thirds[:-1], thirds[1:]):
        st = np.arange(int(a * sr), int(b * sr) - n, n)
        lv = np.array([lev[min(len(lev) - 1, int(s / sr / HOP_S))] for s in st]) if len(st) else np.zeros(0)
        st = st[lv >= loud - 20] if len(st) else st
        pm_m, pm_s, k = _spectra(master, sr, st, n, win)
        pv_m, pv_s, _ = _spectra(vinyl, sr, np.round(m2v(st / sr) * sr), n, win)
        if not k or pv_m is None:
            continue
        # The recording runs faster by speed_ratio, so its frequencies are higher by that factor. Without correction
        # single tones slip across a band edge (test: 250 Hz off by 1.2 dB at 0.4 %).
        bm_m, bm_s = _band_sum(pm_m, freqs), _band_sum(pm_s, freqs)
        bv_m, bv_s = _band_sum(pv_m, freqs / speed_ratio), _band_sum(pv_s, freqs / speed_ratio)
        if nk:
            bn_m, bn_s = _band_sum(noise_m, freqs), _band_sum(noise_s, freqs)
        else:
            bn_m = bn_s = np.zeros(len(THIRD_OCT))

        def ratio(bv, bn, bm, floor_ref):
            # Only bands where the music clearly exceeds the groove noise (≥ 25 % of the power after subtraction) and
            # the master has content (within 60 dB of its loudest band) are measurable.
            sig = bv - bn
            ok = (sig > 0.25 * bv) & (bm > floor_ref * 1e-6)
            return np.where(ok, 10 * np.log10(np.maximum(sig, 1e-30) / np.maximum(bm, 1e-30)), np.nan)

        r_m = ratio(bv_m, bn_m, bm_m, np.nanmax(bm_m))
        r_s = ratio(bv_s, bn_s, bm_s, np.nanmax(bm_m))
        # Each third referred to its own 1 kHz: the shape counts here; the level is reported by _levels (a quieter
        # track is not a frequency response).
        ref_off = r_m[bands_ref] if np.isfinite(r_m[bands_ref]) else np.nanmedian(r_m)
        r_s = np.where(bm_s > 10 ** (-2.5) * bm_m, r_s, np.nan)          # master has hardly any stereo: not measurable
        segs.append({"label": lab, "t_m": [round(float(a), 1), round(float(b), 1)], "frames": int(k),
                     "mid_db": [None if not np.isfinite(v) else round(float(v - ref_off), 2) for v in r_m],
                     "side_db": [None if not np.isfinite(v) else round(float(v - ref_off), 2) for v in r_s]})
    out = {"bands": list(THIRD_OCT), "ref_hz": 1000, "segments": segs, "noise_frames": int(nk)}
    if len(segs) >= 2:
        i10 = THIRD_OCT.index(10000)
        a, z = segs[0]["mid_db"][i10], segs[-1]["mid_db"][i10]
        out["hf_loss_end_db"] = None if a is None or z is None else round(z - a, 2)
    lows = [THIRD_OCT.index(f) for f in (40, 50, 63, 80, 100)]
    vals = [s["side_db"][i] - s["mid_db"][i] for s in segs for i in lows if s["side_db"][i] is not None and s["mid_db"][i] is not None]
    out["bass_side_db"] = round(float(np.median(vals)), 2) if vals else None
    return out


# ─── 5. Level per track ──────────────────────────────────────────────────────
def _levels(master, vinyl, sr, m2v, v2m, music, tracks) -> dict:
    """Level of each track (or each third of the side without a track list) on the vinyl vs. the master. `rel_db` is
    referred to the median difference, so the overall gain of the turntable chain drops out."""
    hm, hv = _hp(master.mean(axis=1), sr, 20, 2), _hp(vinyl.mean(axis=1), sr, 20, 2)
    items = []
    if tracks:
        spans = [(float(v2m(t["t0"])), float(v2m(t["t1"])), t.get("pos") or "", t.get("title") or "") for t in tracks]
    else:
        th = np.linspace(music[0], music[1], 4)
        spans = [(th[i], th[i + 1], "", lab) for i, lab in enumerate(_parts())]
    for a, b, pos, title in spans:
        a, b = max(0.0, a), min(len(master) / sr, b)
        if b - a < 2:
            continue
        va, vb = float(m2v(a)), float(m2v(b))
        lm = _rms_db(hm[int(a * sr):int(b * sr)])
        lv = _rms_db(hv[max(0, int(va * sr)):int(vb * sr)])
        if lm is None or lv is None:
            continue
        items.append({"pos": pos, "title": title, "t_m": [round(a, 2), round(b, 2)], "t_v": [round(va, 2), round(vb, 2)],
                      "master_dbfs": round(lm, 2), "vinyl_dbfs": round(lv, 2), "diff_db": round(lv - lm, 2)})
    gain = float(np.median([x["diff_db"] for x in items])) if items else None
    for x in items:
        x["rel_db"] = round(x["diff_db"] - gain, 2)
    return {"gain_db": None if gain is None else round(gain, 2), "items": items,
            "spread_db": round(max(x["rel_db"] for x in items) - min(x["rel_db"] for x in items), 2) if items else None}


# ─── 6. Clicks: vinyl or music ───────────────────────────────────────────────
def _sinc_sample(x: np.ndarray, pos: np.ndarray, half: int = 8) -> np.ndarray:
    """Band-limited interpolation at fractional positions (windowed sinc, 16 taps). Linear interpolation attenuates
    the treble depending on the fractional position; the FIR in _residual_peak could not undo that."""
    i0 = np.floor(pos).astype(np.int64)
    fr = pos - i0
    k = np.arange(-half + 1, half + 1)
    d = fr[:, None] - k[None, :]
    wts = np.sinc(d) * (0.5 + 0.5 * np.cos(np.pi * d / half))
    idx = np.clip(i0[:, None] + k[None, :], 0, len(x) - 1)
    return np.sum(x[idx] * wts, axis=1)


def _residual_peak(hm: np.ndarray, hv: np.ndarray, sr: int, v2m, tv: float, taps: int = 16,
                   win_s: float = 0.015, search_s: float = 0.003, ev_s: tuple = (-0.0005, 0.0015)):
    """Is the peak at tv on the vinyl contained in the master?

    The (high-passed) master is resampled onto the recording's time axis (speed and wow: running 0.4 % fast already
    shifts the treble by 0.12 ms within 30 ms), tracked to the nearest sample (±3 ms), then re-measured to a fraction
    of a sample in the treble left and right of the spot, and the time axis inside the window is corrected linearly
    (the ±20 µs noise of the 50 ms fine offset would otherwise act like a small speed error). A short FIR then matches
    the master to the recording (fitted without the spot itself) and is subtracted.
    Returns (peak on the vinyl, peak of the residual, residual floor) or None near the edges. A real click stays in
    the residual, a transient of the music does not."""
    w, s, h = int(win_s * sr), int(search_s * sr), taps // 2
    iv = int(tv * sr)
    if iv - w < 0 or iv + w >= len(hv):
        return None
    # Map linearly within the window: position and local speed from the fine mapping, but without its kinks (support
    # points every 50 ms with ±15 µs noise; 15 µs are already 54° at 10 kHz and would leave a transient in the residual)
    t_c, dt = iv / sr, 0.025
    m_l, m_c, m_r = (float(x) for x in v2m(np.array([t_c - dt, t_c, t_c + dt])))
    if not all(np.isfinite([m_l, m_c, m_r])):
        return None
    loc = (m_r - m_l) / (2 * dt)
    pos = (m_c + loc * (np.arange(-(w + s + h), w + s + h) / sr)) * sr   # pos[n] ↔ vinyl sample iv - (w + s + h) + n
    if pos[0] < 8 or pos[-1] >= len(hm) - 9:
        return None
    mg = _sinc_sample(hm, pos)
    v = hv[iv - w:iv + w].astype(np.float64)               # v[i] ↔ vinyl sample iv - w + i
    y = _ncc(v, mg[h:len(mg) - h])
    k = int(np.argmax(y))                                  # the master for v[i] is mg[h + k + i]
    c = w
    i_l, i_r = c - int(0.002 * sr), c + int(0.003 * sr)

    def sub(i0, i1):
        yy = _ncc(v[i0:i1], mg[h + k + i0 - 3:h + k + i1 + 3])
        q = int(np.argmax(yy))
        return q + _parabola(yy, q) - 3, float(yy[q]), (i0 + i1) / 2

    d_l, q_l, c_l = sub(0, i_l)
    d_r, q_r, c_r = sub(i_r, 2 * w)
    ii = np.arange(-h, 2 * w + h)
    corr = d_l + (d_r - d_l) * (ii - c_l) / (c_r - c_l) if min(q_l, q_r) >= 0.5 else np.zeros(len(ii))
    mg2 = _sinc_sample(hm, pos[ii + h + k] + corr)          # mg2[i + h] ↔ v[i]
    X = np.lib.stride_tricks.sliding_window_view(mg2, taps)[:2 * w]
    if X.shape[0] != len(v):
        return None
    e0, e1 = c + int(ev_s[0] * sr), c + int(ev_s[1] * sr) + 1
    fit = np.ones(len(v), bool)
    fit[e0 - int(0.001 * sr):e1 + int(0.001 * sr)] = False
    coef, *_ = np.linalg.lstsq(X[fit], v[fit], rcond=None)
    r = v - X @ coef
    pv, pr = float(np.max(np.abs(v[e0:e1]))), float(np.max(np.abs(r[e0:e1])))
    return pv, pr, float(np.sqrt(np.mean(r[fit] ** 2)))


def _clicks(master, vinyl, sr, m2v, v2m, events, music, music_s: float) -> dict:
    """Sort every detected event into vinyl defect or music, using the residual after subtracting the fitted master.

    Vinyl defect if the residual peak stands out sharply (≥ 12× the residual floor in the ±15 ms window) or at least
    half of the peak remains (at ≥ 4× the floor); otherwise music. Calibrated on a real test pressing: on pure music
    transients without a detection, residual/floor has a median of 3.8 (90 % below 8.9), on reported clicks 24.
    Residual/peak alone does not separate on real vinyl (clicks often sit on loud music, median 0.39). Where the
    master is silent (gaps, lead-in, run-out), every event is a vinyl event.
    `music_s` is the music duration in s (for the rates per minute)."""
    hm, hv = _hp(master.mean(axis=1), sr, 2000), _hp(vinyl.mean(axis=1), sr, 2000)
    hf_music = float(np.sqrt(_meansq(hm[int(music[0] * sr):int(music[1] * sr)])))   # treble level of the music
    by = {"vinyl": {"pop": 0, "click": 0, "tick": 0}, "master": {"pop": 0, "click": 0, "tick": 0}}
    by_region: dict = {}
    items, every = [], []
    for e in events or []:
        if e.get("region") == "excluded":
            continue
        tv = float(e["t"])
        tm = float(v2m(tv))
        im = int(tm * sr)
        local = hm[max(0, im - int(0.015 * sr)):im + int(0.015 * sr)] if 0 <= im < len(hm) else np.zeros(0)
        lev = float(np.sqrt(np.mean(local.astype(np.float64) ** 2))) if local.size else 0.0
        src, ratio = "vinyl", None
        if lev > 0.01 * hf_music:                          # master has treble here (> −40 dB): does it explain it?
            rp = _residual_peak(hm, hv, sr, v2m, tv)
            if rp:
                pv, pr, floor = rp
                ratio = pr / pv if pv > 0 else 0.0
                sharp = pr / floor if floor > 0 else float("inf")
                if e.get("kind") == "pop":
                    # A pop is loud, i.e. as large as the music peaks themselves. If less than half of it remains after
                    # subtracting the master, it was an entry of the music (drums) that the detector counted as a pop.
                    # Sharpness alone is not enough here: at loud entries the tracing distortion of the vinyl leaves a
                    # sharp residual (on a real test pressing such "pops" kept 17–56 % residual).
                    vinyl_ev = ratio >= 0.5 and sharp >= 4
                else:
                    vinyl_ev = sharp >= CLICK_SHARP or (ratio >= 0.5 and sharp >= 4)
                if not vinyl_ev:
                    src = "master"
        kind, reg = e.get("kind", "click"), e.get("region", "")
        every.append([round(tv, 5), "v" if src == "vinyl" else "m", None if ratio is None else round(ratio, 3)])
        by[src][kind] = by[src].get(kind, 0) + 1
        by_region.setdefault(reg, {"vinyl": 0, "master": 0})[src] += 1
        if src == "vinyl":
            items.append({"t": round(tv, 4), "t_m": round(tm, 4), "kind": kind, "region": reg, "strength": e.get("strength"),
                          "amp_dbfs": e.get("amp_dbfs"), "label": e.get("label"),
                          "residual": None if ratio is None else round(ratio, 3)})
    items.sort(key=lambda x: -(x["strength"] or 0))
    vm = sum(1 for x in items if x["region"] == "music" and x["kind"] != "tick")
    music_kind = {k: sum(1 for x in items if x["region"] == "music" and x["kind"] == k) for k in ("pop", "click", "tick")}
    return {"by_source": by, "by_region": by_region, "vinyl_music_by_kind": music_kind,
            "vinyl_pops_music_per_min": round(music_kind["pop"] / (music_s / 60), 3) if music_s > 0 else None,
            "vinyl_music_per_min": round(vm / (music_s / 60), 2) if music_s > 0 else None, "music_s": round(music_s, 1),
            "top": items[:40], "events": every}               # every event: [t, v = vinyl | m = music, residual]


# ─── 7. Gaps: noise, pre-echo and post-echo ──────────────────────────────────
def _floor_db(seg: np.ndarray, sr: int, block_s: float = 0.1, share: float = 0.4) -> float | None:
    """Noise of a gap: mean of the quietest 40 % of the 0.1 s blocks. Pre-echo, post-echo and clicks sit in single
    blocks and do not bias it that way (test: a −30 dB pre-echo otherwise raised the gap by 4.6 dB)."""
    blk = int(block_s * sr)
    n = len(seg) // blk
    if n < 2:
        return _rms_db(seg)
    p = np.mean(seg[:n * blk].astype(np.float64).reshape(n, blk, -1) ** 2, axis=(1, 2))
    p = np.sort(p)[:max(1, int(np.ceil(share * n)))]
    return _db(float(p.mean()))


def _echo(bpv: np.ndarray, sr: int, gv0: float, gv1: float, rev_s: float, pre: bool, w: float = 0.5) -> dict | None:
    """Look for a copy of the music one revolution away inside the gap gv0..gv1 (recording time). pre: the entry after
    the gap, one revolution early; otherwise the end of the music before the gap, one revolution late."""
    m = 0.02
    if pre:
        lo, hi = max(gv1, gv0 + m + rev_s), gv1 - m + rev_s - w
    else:
        lo, hi = gv0 + m - rev_s, min(gv0 - w, gv1 - m - rev_s - w)
    lo = max(lo, 0.0)
    if hi < lo or (hi + w) * sr >= len(bpv):
        return None
    best_u, best_e = None, -1.0                           # loudest music window in the allowed range
    for u in np.arange(lo, hi + 1e-9, 0.05):
        seg = bpv[int(u * sr):int((u + w) * sr)]
        e = float(np.mean(seg.astype(np.float64) ** 2)) if len(seg) else 0.0
        if e > best_e:
            best_u, best_e = u, e
    if best_u is None or best_e <= 0:
        return None
    u = best_u
    tmpl = bpv[int(u * sr):int((u + w) * sr)]
    exp = u - rev_s if pre else u + rev_s
    a, b = max(gv0 + m, exp - 0.03), min(gv1 - m, exp + w + 0.03)
    seg = bpv[int(a * sr):int(b * sr)]
    if len(seg) <= len(tmpl) + 4:
        return None
    y = _ncc(tmpl, seg)
    k = int(np.argmax(np.abs(y)))
    mask = np.ones(len(y), bool)
    mask[max(0, k - int(0.002 * sr)):k + int(0.002 * sr) + 1] = False
    sd = float(np.std(y[mask])) if mask.sum() > 10 else float("nan")
    q = float(y[k])
    x = seg[k:k + len(tmpl)].astype(np.float64)
    amp = float(np.dot(x, tmpl) / max(np.dot(tmpl.astype(np.float64), tmpl), 1e-20))
    z = abs(q) / sd if sd and np.isfinite(sd) and sd > 0 else 0.0
    # No z-score as criterion: with tonal music the correlation has side maxima of the music itself at every lag, so
    # the spread "next to the hit" is no noise measure (test: a −30 dB pre-echo with q 0.47 and z 3.8 was rejected).
    # Pure noise stays at |q| around 0.05; in addition the hit must lie exactly one revolution from the music.
    lag_err = (a + k / sr) - exp
    found = abs(q) >= ECHO_MIN_Q and abs(lag_err) <= ECHO_LAG_TOL_S
    return {"db": round(20 * np.log10(max(abs(amp), 1e-9)), 1), "q": round(q, 3), "z": round(z, 1), "found": bool(found),
            "t_v": round(a + k / sr, 3), "music_t_v": round(float(u), 3), "lag_err_ms": round(float(lag_err) * 1000, 2)}


def _gaps(master, vinyl, sr, m2v, music, rev_s: float, music_ref_db: float | None) -> dict:
    """Noise and echoes wherever the master is silent: lead-in ("start"), gaps between tracks ("gap"), run-out
    ("end")."""
    lev = _envelope_db(_hp(master.mean(axis=1), sr, 20, 2), sr)
    i0, i1 = int(music[0] / HOP_S), int(music[1] / HOP_S)
    loud = float(np.percentile(lev[i0:i1], 95)) if i1 > i0 else float(lev.max())
    silent = lev < loud - SILENT_REL_DB
    a_sos = analysis.a_weighting_sos(sr)
    bpv = _bp(vinyl.mean(axis=1), sr, 300, 6000)
    dur_m, dur_v = len(master) / sr, len(vinyl) / sr
    items = []
    for a, b in _runs(silent):
        g0, g1 = a * HOP_S, min(b * HOP_S, dur_m)
        if g1 - g0 < 0.4:
            continue
        v0, v1 = float(m2v(g0)), float(m2v(g1))
        if v0 < 0 or v1 > dur_v:
            continue
        i0, i1, pre_s = int((v0 + 0.05) * sr), int((v1 - 0.05) * sr), int(0.5 * sr)
        if i1 - i0 < int(0.2 * sr):
            continue
        seg = _hp(vinyl[max(0, i0 - pre_s):i1], sr, 20, 2)[i0 - max(0, i0 - pre_s):]   # 0.5 s run-up: filter settles
        noise, noise_a = _floor_db(seg, sr), _floor_db(signal.sosfilt(a_sos, seg, axis=0), sr)
        # Kind of silence (stored enum values): "start" = before the music, "end" = after it, "gap" = between tracks
        kind = "start" if g0 <= 0.05 else ("end" if g1 >= dur_m - 0.05 else "gap")
        item = {"kind": kind, "t_m": [round(g0, 2), round(g1, 2)], "t_v": [round(v0, 2), round(v1, 2)], "len_s": round(v1 - v0, 2),
                "noise_dbfs": None if noise is None else round(noise, 1), "noise_a_dbfs": None if noise_a is None else round(noise_a, 1),
                "snr_db": None if noise is None or music_ref_db is None else round(music_ref_db - noise, 1)}
        if kind != "end":
            item["pre_echo"] = _echo(bpv, sr, v0, v1, rev_s, pre=True)
        if kind != "start":
            item["post_echo"] = _echo(bpv, sr, v0, v1, rev_s, pre=False)
        items.append(item)
    found = lambda key: [x[key] for x in items if x.get(key) and x[key]["found"]]
    pre, post = found("pre_echo"), found("post_echo")
    na = [x["noise_a_dbfs"] for x in items if x["noise_a_dbfs"] is not None]
    inner = sum(x["len_s"] for x in items if x["kind"] == "gap")
    return {"items": items, "pre_echo_max_db": max((e["db"] for e in pre), default=None), "pre_echo_count": len(pre),
            "post_echo_max_db": max((e["db"] for e in post), default=None), "post_echo_count": len(post),
            "noise_a_median_dbfs": round(float(np.median(na)), 1) if na else None,
            "snr_median_db": round(float(np.median([x["snr_db"] for x in items if x["snr_db"] is not None])), 1)
            if any(x["snr_db"] is not None for x in items) else None,
            "inner_gaps_s": round(inner, 2), "silent_rel_db": SILENT_REL_DB}


# ─── Putting it together ─────────────────────────────────────────────────────
def music_range(master: np.ndarray, sr: int) -> tuple[float, float]:
    """Start and end of the music in the master (s): first and last 10 ms block within 40 dB of the loud level."""
    lev = _envelope_db(master.mean(axis=1), sr)
    above = np.flatnonzero(lev > np.percentile(lev, 95) - 40)
    if not len(above):
        return 0.0, len(master) / sr
    return float(above[0] * HOP_S), float((above[-1] + 1) * HOP_S)


def check(vinyl: np.ndarray, master: np.ndarray, sr: int, res: dict | None, rpm: float = 100 / 3,
          turntable_speed_pct: float | None = None, others: dict | None = None) -> dict:
    """Measure a recording against the master of its side.

    vinyl, master: (n, 2) float32 at the same sample rate `sr`. res: analysis of the recording (events, quiet passages,
    track boundaries; may be missing). rpm: actual speed of the turntable (profile). turntable_speed_pct: speed error
    of the turntable from calibration (%), subtracted to get the error of cut and pressing. others: masters of the
    other sides {side: array or path}, only used when this side does not match.
    Returns a JSON-able dict; with "ok": False it carries a "reason" message and possibly "other_side".
    """
    t_start = time.time()
    res = res or {}
    strip = lambda al: {k: v for k, v in al.items() if k not in ("map_a", "map_b")}
    out: dict = {"version": VERSION, "master_s": round(len(master) / sr, 2), "vinyl_s": round(len(vinyl) / sr, 2)}
    al, pol, vinyl = _align_any(master, vinyl, sr)
    if pol == "none":
        other = None
        for side, om in (others or {}).items():
            om = load(om, sr) if isinstance(om, str) else om
            if _align_any(om, vinyl, sr)[1] != "none":
                other = side
                break
            del om
        reason = (i18n.msg("master.reason_other_side", side=other) if other else i18n.msg("master.reason_no_match"))
        return analysis._py(out | {"ok": False, "reason": reason, "other_side": other, "alignment": strip(al),
                                   "seconds": round(time.time() - t_start, 1)})
    m2v, v2m = _maps(al)
    slope = al["slope"]
    music = music_range(master, sr)
    warnings = []
    if pol == "inverted":
        warnings.append(i18n.msg("master.warn_polarity_inverted"))
    elif pol == "left_inverted":
        warnings.append(i18n.msg("master.warn_polarity_left"))
    elif pol == "right_inverted":
        warnings.append(i18n.msg("master.warn_polarity_right"))
    speed_pct = (al["speed_ratio"] - 1) * 100
    speed = {"vs_master_pct": round(speed_pct, 3), "turntable_pct": turntable_speed_pct,
             "cut_pct": None if turntable_speed_pct is None else round(speed_pct - turntable_speed_pct, 3)}

    fm, fv = _bp(master.mean(axis=1), sr, 150, 4000), _bp(vinyl.mean(axis=1), sr, 150, 4000)
    i0, i1 = int(music[0] * sr), int(music[1] * sr)
    thr = 0.2 * float(np.sqrt(_meansq(fm[i0:i1])))
    ts, lags, _q = _local_lags(fm, fv, sr, m2v, music[0], music[1], thr)      # every 50 ms across the whole music
    del fm, fv
    wow, ranges = _wow(ts, lags, m2v, slope, music, rpm)
    fine = _fine_mapper(m2v, slope, ts, lags)
    fine_inv = _inverse(fine, 0.0, len(master) / sr)
    v2m_fine = lambda t: np.where(np.isnan(fine_inv(t)), v2m(t), fine_inv(t))
    channels = _channels(master, vinyl, sr, fine, ranges)
    if channels.get("swapped"):
        warnings.append(i18n.msg("master.warn_channels_swapped"))
    if wow.get("ecc_mm") is not None and (wow.get("verdict") or {}).get("$t") == "master.wow_eccentric":
        warnings.append(i18n.msg("master.warn_eccentric", wow=wow["wow_pct_max"], ecc=wow["ecc_mm"]))

    v_music = (float(m2v(music[0])), float(m2v(music[1])))
    a, b = max(0, int(v_music[0] * sr)), min(len(vinyl), int(v_music[1] * sr))
    music_ref = None
    if b > a:                                              # high-pass per channel, never the whole stereo side at once
        music_ref = _db(float(np.mean([_meansq(_hp(vinyl[a:b, c], sr, 20, 2)) for c in range(vinyl.shape[1])])))
    rev_s = 60 / rpm
    gaps = _gaps(master, vinyl, sr, m2v, music, rev_s, music_ref)
    music_s = max(0.0, (min(v_music[1], len(vinyl) / sr) - max(0.0, v_music[0])) - gaps["inner_gaps_s"])
    clicks = _clicks(master, vinyl, sr, fine, v2m_fine, res.get("events") or [], music, music_s)
    quiet_v = [tuple(q) for q in ((res.get("overview") or {}).get("quiet") or [])]
    freq = _freq(master, vinyl, sr, m2v, music, quiet_v, al["speed_ratio"])
    levels = _levels(master, vinyl, sr, m2v, v2m, music, (res.get("metrics") or {}).get("tracks") or [])

    if v_music[1] > len(vinyl) / sr + 0.5:
        warnings.append(i18n.msg("master.warn_ends_early", s=round(v_music[1] - len(vinyl) / sr)))
    if v_music[0] < 0:
        warnings.append(i18n.msg("master.warn_starts_late"))
    if gaps["pre_echo_count"] and gaps["pre_echo_max_db"] >= ECHO_WARN_DB:   # weaker pre-echo stays below the noise
        warnings.append(i18n.msg("master.warn_pre_echo", n=gaps["pre_echo_count"], db=gaps["pre_echo_max_db"]))
    return analysis._py(out | {
        "ok": True, "reason": None, "polarity": pol,
        "alignment": strip(al), "map": {"m": al["map_a"], "v": al["map_b"], "slope": slope, "intercept": al["intercept"]},
        "music_m": [round(music[0], 2), round(music[1], 2)], "music_v": [round(v_music[0], 2), round(v_music[1], 2)],
        "music_ref_dbfs": None if music_ref is None else round(music_ref, 1), "rpm": round(rpm, 4),
        "speed": speed, "wow": wow, "channels": channels, "freq": freq, "levels": levels, "gaps": gaps, "clicks": clicks,
        "warnings": warnings, "seconds": round(time.time() - t_start, 1)})


def check_files(vinyl_path: str, master_path: str, res: dict | None, rpm: float = 100 / 3,
                turntable_speed_pct: float | None = None, others: dict | None = None) -> dict:
    """check() for a recording file and a master file (see check for the other arguments)."""
    import soundfile as sf
    vinyl, sr = sf.read(str(vinyl_path), dtype="float32", always_2d=True)
    if vinyl.shape[1] == 1:
        vinyl = np.repeat(vinyl, 2, axis=1)
    return check(np.ascontiguousarray(vinyl[:, :2]), load(master_path, sr), sr, res, rpm, turntable_speed_pct, others)
