"""Before/after comparison of two recordings of the same record side.

1. Coarse alignment: the loudness curves (10 ms grid) of both recordings are cross-correlated → offset of the
   start points.
2. Fine alignment: every ~10 s a 1 s music excerpt from A is searched in B (normalized cross-correlation,
   sample-accurate). The anchor points give a time map A → B that also compensates slightly different speed
   (motor warm/cold) and wow & flutter.
3. Matching: every event from A is mapped to B and searched there within ±tol_ms.
     persistent → in both: physical damage (scratch), cleaning does not help
     removed    → only before: it was dirt, cleaning worked
     new        → only after: cleaning residue / dust from handling
   Events outside the groove range common to both recordings do not count.

Comparing two "before" recordings measures the repeatability of the measurement.

If the alignment finds no common music (no or too few anchors), the recordings are not of the same side. Then
there is no matching and no differences, only `comparable: False` with a reason. Without this check, side A
compared against side B of the same record ended up as tens of thousands of "clicks removed".

4. Groove skips (skips.py): if one of the recordings skips (forward or back), the matching shifts by one
   revolution. This is reported; clicks after the skip are then matched with less certainty. Skips at the same
   place in both recordings point to the record itself.
"""

from __future__ import annotations

import numpy as np
from scipy import signal

from . import i18n, skips

ENV_HOP_S = 0.01


def _mono(audio: np.ndarray) -> np.ndarray:
    return audio.mean(axis=1) if audio.ndim == 2 else audio


def _envelope(x: np.ndarray, sr: int) -> np.ndarray:
    """Log RMS envelope on a grid of ENV_HOP_S seconds."""
    hop = int(sr * ENV_HOP_S)
    n = len(x) // hop
    e = np.sqrt(np.mean(x[:n * hop].astype(np.float64).reshape(n, hop) ** 2, axis=1))
    return np.log10(e + 1e-6)


def coarse_offset(a: np.ndarray, b: np.ndarray, sr: int) -> float:
    """Offset in s such that t_B ≈ t_A + offset."""
    ea, eb = _envelope(a, sr), _envelope(b, sr)
    ea = (ea - ea.mean()) / (ea.std() + 1e-12)
    eb = (eb - eb.mean()) / (eb.std() + 1e-12)
    c = signal.correlate(eb, ea, mode="full", method="fft")
    lags = signal.correlation_lags(len(eb), len(ea), mode="full")
    return float(lags[np.argmax(c)] * ENV_HOP_S)


def _ncc_peak(seg_a: np.ndarray, seg_b: np.ndarray) -> tuple[int, float]:
    """Best position of seg_a in seg_b (normalized cross-correlation). → (index in seg_b, quality)."""
    c = signal.correlate(seg_b, seg_a, mode="valid", method="fft")
    csum = np.concatenate([[0.0], np.cumsum(seg_b.astype(np.float64) ** 2)])
    w = len(seg_a)
    norm_b = np.sqrt(np.maximum(csum[w:] - csum[:-w], 1e-20))
    ncc = c / (norm_b * np.sqrt(np.sum(seg_a.astype(np.float64) ** 2)) + 1e-20)
    k = int(np.argmax(ncc))
    return k, float(ncc[k])


def _sample(x: np.ndarray, pos: np.ndarray) -> np.ndarray:
    """Linear interpolation at fractional sample positions (without a huge array of support points)."""
    i0 = np.floor(pos).astype(np.int64)
    fr = (pos - i0).astype(np.float32)
    return x[i0] * (1 - fr) + x[i0 + 1] * fr


def _anchor_pass(fa, fb, lev, loud, sr, slope, icpt, win_s, search_s, spacing_s,
                 min_quality, track_drift):
    """One pass: search A windows in B. B is resampled onto A's time axis for this (t_B = slope · t_A + icpt), so
    that a speed difference does not smear the window."""
    w, s = int(win_s * sr), int(search_s * sr)
    anchors = []
    for ta in np.arange(1.0, len(fa) / sr - win_s - 1.0, spacing_s):
        li = int(ta / ENV_HOP_S)
        if lev[li:li + int(win_s / ENV_HOP_S)].mean() < loud:
            continue   # too quiet → noise does not correlate
        grid = ta - search_s + np.arange(w + 2 * s) / sr
        pos = (slope * grid + icpt) * sr
        if pos[0] < 0 or pos[-1] >= len(fb) - 1:
            continue
        ia = int(ta * sr)
        k, q = _ncc_peak(fa[ia:ia + w], _sample(fb, pos))
        if q < min_quality:
            continue
        d = (k - s) / sr
        anchors.append((ta, slope * (ta + d) + icpt, q))
        if track_drift and len(anchors) >= 3:   # follow the drift as we go
            arr = np.array(anchors)
            slope, icpt = np.polyfit(arr[:, 0], arr[:, 1], 1)
    return anchors, slope, icpt


def align(a: np.ndarray, b: np.ndarray, sr: int, spacing_s: float = 10.0,
          min_quality: float = 0.5) -> dict:
    """Time map A → B from anchor points. a, b: mono signals.

    Pass 1: short windows (0.3 s), low band → robust against speed differences, yields speed + offset.
    Pass 2: B resampled onto A's time axis, 1 s windows, wide band → sample-accurate anchors.
    """
    offset = coarse_offset(a, b, sr)
    sos1 = signal.butter(4, [100, 1500], btype="band", fs=sr, output="sos")
    fa = signal.sosfilt(sos1, a).astype(np.float32)
    fb = signal.sosfilt(sos1, b).astype(np.float32)
    lev = _envelope(fa, sr)
    loud = np.percentile(lev, 60) - 1.0
    anchors, slope, icpt = _anchor_pass(fa, fb, lev, loud, sr, 1.0, offset, 0.3, 0.25,
                                        spacing_s, min_quality, track_drift=True)
    if len(anchors) >= 2:
        arr = np.array(anchors)
        slope, icpt = np.polyfit(arr[:, 0], arr[:, 1], 1)
        sos2 = signal.butter(4, [150, 4000], btype="band", fs=sr, output="sos")
        fa = signal.sosfilt(sos2, a).astype(np.float32)
        fb = signal.sosfilt(sos2, b).astype(np.float32)
        fine, _, _ = _anchor_pass(fa, fb, lev, loud, sr, slope, icpt, 1.0, 0.03,
                                  spacing_s, min_quality, track_drift=False)
        if len(fine) >= 2:
            anchors = fine

    result = {"coarse_offset_s": round(offset, 4), "anchors": len(anchors)}
    if len(anchors) < 2:
        result.update({"ok": False, "speed_ratio": 1.0, "offset_s": offset, "quality": None,
                       "map_a": [0.0, 1.0], "map_b": [offset, 1.0 + offset]})
        return result

    arr = np.array(anchors)
    keep = np.ones(len(arr), bool)
    for _ in range(3):   # discard outliers (wrong hits, e.g. on a repeated chorus)
        sl, ic = np.polyfit(arr[keep, 0], arr[keep, 1], 1)
        res = np.abs(arr[:, 1] - (sl * arr[:, 0] + ic))
        lim = max(0.002, 4 * np.median(res[keep]))
        new = res <= lim
        if new.sum() < 2 or np.array_equal(new, keep):
            break
        keep = new
    arr = arr[keep]
    sl, ic = np.polyfit(arr[:, 0], arr[:, 1], 1)
    result.update({
        "ok": True,
        "speed_ratio": round(float(1 / sl), 6),     # > 1: B runs faster
        "offset_s": round(float(ic), 4),
        "quality": round(float(np.median(arr[:, 2])), 3),
        "anchors_used": int(len(arr)),
        "map_a": arr[:, 0].tolist(),
        "map_b": arr[:, 1].tolist(),
        "slope": float(sl), "intercept": float(ic),
    })
    return result


def _mapper(ma, mb, slope, icpt):
    """Piecewise linear time map through the anchors (ma → mb), extrapolated with `slope` outside them."""
    ma, mb = np.asarray(ma), np.asarray(mb)

    def f(t):
        t = np.asarray(t, dtype=float)
        out = np.interp(t, ma, mb)
        out = np.where(t < ma[0], mb[0] + (t - ma[0]) * slope, out)
        return np.where(t > ma[-1], mb[-1] + (t - ma[-1]) * slope, out)
    return f


def match_events(ev_a: list[dict], ev_b: list[dict], alignment: dict,
                 groove_a: tuple[float, float], groove_b: tuple[float, float],
                 tol_ms: float = 4.0) -> dict:
    """Match the events of A and B via the alignment (±tol_ms) into persistent, removed and new.

    Strong events are matched first, each event of B at most once. Returns counts, counts per kind, the share
    of removed events and the event lists (at most 5000 each).
    """
    sl = alignment.get("slope", 1.0)
    ic = alignment.get("intercept", alignment.get("offset_s", 0.0))
    a2b = _mapper(alignment["map_a"], alignment["map_b"], sl, ic)
    b2a = _mapper(alignment["map_b"], alignment["map_a"], 1 / sl, -ic / sl)

    ea = [e for e in ev_a if e["region"] != "excluded"]
    eb = [e for e in ev_b if e["region"] != "excluded"]
    ta = np.array([e["t"] for e in ea]) if ea else np.zeros(0)
    tb = np.array([e["t"] for e in eb]) if eb else np.zeros(0)
    ta_in_b = a2b(ta) if ta.size else ta
    tb_in_a = b2a(tb) if tb.size else tb
    # common range only
    ina = (ta_in_b >= groove_b[0]) & (ta_in_b <= groove_b[1])
    inb = (tb_in_a >= groove_a[0]) & (tb_in_a <= groove_a[1])

    tol = tol_ms / 1000
    order_b = np.argsort(tb)
    tb_sorted = tb[order_b]
    used = np.zeros(len(tb), bool)
    pair = -np.ones(len(ta), int)
    for i in np.argsort([-e["strength"] for e in ea]) if ea else []:
        if not ina[i]:
            continue
        lo = np.searchsorted(tb_sorted, ta_in_b[i] - tol)
        hi = np.searchsorted(tb_sorted, ta_in_b[i] + tol, side="right")
        best, bestd = -1, tol + 1
        for j in order_b[lo:hi]:
            d = abs(tb[j] - ta_in_b[i])
            if not used[j] and d < bestd:
                best, bestd = j, d
        if best >= 0:
            used[best] = True
            pair[i] = best

    persistent, removed, new = [], [], []
    for i, e in enumerate(ea):
        if not ina[i]:
            continue
        item = {"t_a": e["t"], "kind": e["kind"], "strength": e["strength"], "region": e["region"]}
        if pair[i] >= 0:
            item["t_b"] = eb[pair[i]]["t"]
            persistent.append(item)
        else:
            removed.append(item)
    for j, e in enumerate(eb):
        if inb[j] and not used[j]:
            new.append({"t_a": round(float(tb_in_a[j]), 5), "t_b": e["t"], "kind": e["kind"],
                        "strength": e["strength"], "region": e["region"]})

    def by_kind(lst):
        return {k: sum(1 for x in lst if x["kind"] == k) for k in ("pop", "click", "tick")}

    total_a = len(persistent) + len(removed)
    return {
        "tol_ms": tol_ms,
        "counts": {"persistent": len(persistent), "removed": len(removed), "new": len(new)},
        "by_kind": {"persistent": by_kind(persistent), "removed": by_kind(removed), "new": by_kind(new)},
        "removed_share": round(len(removed) / total_a, 3) if total_a else None,
        "events": {"persistent": persistent[:5000], "removed": removed[:5000], "new": new[:5000]},
    }


MIN_ANCHORS = 3


def not_comparable_reason(al: dict) -> dict | None:
    """Why two recordings do not line up (an i18n message); None if the alignment holds."""
    used = al.get("anchors_used", 0) if al.get("ok") else 0
    if used >= MIN_ANCHORS:
        return None
    if not al.get("anchors"):
        return i18n.msg("compare.no_anchors")
    return i18n.msg("compare.too_few_anchors", used=used, needed=MIN_ANCHORS)


def compare(audio_a: np.ndarray, audio_b: np.ndarray, sr: int, res_a: dict, res_b: dict,
            tol_ms: float = 4.0, rpm: float = 100 / 3) -> dict:
    """Compare two recordings of the same side. audio_a/audio_b: (n, channels) float, res_a/res_b: results of
    analysis.analyze() for them. Returns alignment, matching, metrics of both, their deltas and the skips."""
    al = align(_mono(audio_a), _mono(audio_b), sr)
    ma, mb = res_a["metrics"], res_b["metrics"]
    keys = ("clicks_quiet_per_min", "ticks_quiet_per_min", "clicks_music_per_min", "pops_per_min",
            "noise_dbfs", "noise_a_dbfs", "noise_rel_db")
    reason = not_comparable_reason(al)
    if reason:
        return {
            "alignment": {k: v for k, v in al.items() if k not in ("map_a", "map_b")},
            "comparable": False, "reason": reason, "match": None,
            "metrics_a": {k: ma.get(k) for k in keys}, "metrics_b": {k: mb.get(k) for k in keys},
            "delta": {k: None for k in keys},
            "grade_a": res_a["grading"]["grade"], "grade_b": res_b["grading"]["grade"],
            "skips": {"a": ma.get("skips") or [], "b": mb.get("skips") or [], "relative": [], "same_place": []},
        }
    ga, gb = tuple(res_a["overview"]["groove"]), tuple(res_b["overview"]["groove"])
    m = match_events(res_a["events"], res_b["events"], al, ga, gb, tol_ms)
    sa, sb = res_a["metrics"].get("skips") or [], res_b["metrics"].get("skips") or []
    sl = al.get("slope", 1.0)
    ic = al.get("intercept", al.get("offset_s", 0.0))
    jumps = skips.relative_jumps(audio_a, audio_b, sr, sl, ic, rpm, ga, sa, sb) if al.get("ok") else []
    skip_info = {"a": sa, "b": sb, "relative": jumps, "same_place": skips.same_place(sa, sb, sl, ic)}
    delta = {k: (None if ma.get(k) is None or mb.get(k) is None else round(mb[k] - ma[k], 2)) for k in keys}
    return {
        "alignment": {k: v for k, v in al.items() if k not in ("map_a", "map_b")},
        "comparable": True, "reason": None,
        "match": m,
        "metrics_a": {k: ma.get(k) for k in keys},
        "metrics_b": {k: mb.get(k) for k in keys},
        "delta": delta,
        "grade_a": res_a["grading"]["grade"],
        "grade_b": res_b["grading"]["grade"],
        "skips": skip_info,
    }
