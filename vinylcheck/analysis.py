"""Full analysis of one recording: find the groove and pauses, measure noise, classify clicks, grade.

Steps:
 1. Level curve in 50 ms steps (20 Hz high-pass against rumble), smoothed with a 0.55 s median against
    single clicks.
 2. Groove: detect the silence before the stylus is lowered and after it is lifted (via the chain noise from
    the calibration, or automatically) and exclude it, together with a 0.5 s margin (needle drop/lift thump).
 3. Quiet passages (lead-in, pauses, run-out): level close to the quietest groove level, at least 1 s long.
    This is where noise and clicks are measured, as no music masks anything there.
 4. Detect clicks per channel (detect.py), merge the channels and classify:
      pop   – loud (residual ≥ pop_dbfs), audible even in the music
      click – distinct (strength ≥ click_strength)
      tick  – faint; in large numbers it is "crackle"
 5. Metrics and grade (grading.py).
 6. Groove skips (skips.py): if one revolution repeats, the stylus has skipped back.

User-facing warnings are i18n messages (see i18n.py); they are stored with the analysis and rendered in the
reader's language.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import numpy as np
from scipy import ndimage, signal

from . import detect, i18n, learning, repeats, skips, technical, second_opinion
from .grading import GradingConfig, grade
from .measure import runs as _runs

# review labels per event: real click / music (a false alarm, not counted) / unsure
LABELS = ("defect", "music", "unsure")
SKIP_LABELS = ("run_out", "no_skip")            # labels per skip: run-out groove, or rhythm (no skip back)


def apply_skip_labels(skip_events: list[dict], labels: dict | None, tol_s: float = 2.0) -> list[dict]:
    """Attach the user's labels to the detected skips (±tol_s).

    "run_out" turns a skip back into the run-out groove, "no_skip" turns it into an uncertain hit. The
    automatic classification is kept in `kind_auto`. Typical case: a "skip back" found where the music fades out
    quietly into the run-out groove."""
    out = []
    for e in skip_events:
        e = dict(e)
        e.setdefault("kind_auto", e["kind"])
        lab = None
        for t, l in (labels or {}).items():
            if abs(float(t) - e["t"]) <= tol_s:
                lab = l
        e["label"] = lab
        if lab == "run_out":
            e["kind"] = "run_out"
        elif lab == "no_skip":
            e["kind"] = "uncertain"
        else:
            e["kind"] = e["kind_auto"]
        out.append(e)
    return out


def _mmss(t: float) -> str:
    return f"{int(t // 60)}:{int(t % 60):02d}"


def skip_warning(back: list[dict]) -> dict | None:
    """Warning message for the skips back of a side (None if there are none)."""
    if not back:
        return None
    places = [i18n.msg("analysis.skip_place", time=_mmss(e["t"]), pct=e["position_pct"]) for e in back]
    return i18n.msg("analysis.skip_back", n=len(back), places=i18n.msg_join(places, ", "))


def _is_skip_warning(w) -> bool:
    """True for the skip warning, as a message or as German text stored by versions before 1.0."""
    if isinstance(w, dict):
        return w.get("$t") == "analysis.skip_back"
    return str(w).startswith("Nadel ")


HOP_S = 0.05
EDGE_MARGIN_S = 0.5
CLIP_GRACE_S = 5.0          # grace period after lowering and before lifting the stylus: needle drop, dust cover,
                            # a hand on the platter do not count
CLIP_MENTION = 200          # clipped samples in the music are mentioned from this many on (≈ 4 ms); no grade from 500
MIN_QUIET_S = 1.0
QUIET_BELOW_MUSIC_DB = 22.0   # pauses also count if they are this far below the music reference (see segment)
QUIET_SHRINK_S = 0.2


@dataclass(frozen=True)
class AnalysisConfig:
    detect: detect.DetectParams = detect.DetectParams()
    click_strength: float = 25.0
    pop_dbfs: float = -20.0
    no_groove_dbfs: float = -75.0
    second_opinion: bool = True       # ask the grooveclean network about the candidates (needs PyTorch)
    repeat_filter: bool = True        # count how often each click in the music repeats with the music (repeats.py)
    clip_grace_s: float = CLIP_GRACE_S   # clipping: grace period at the start and end of the groove (settings)
    clip_mention: int = CLIP_MENTION     # … mention from this many samples in the music on (settings)


def labels_for(times: np.ndarray, labels: dict | None, tol_s: float = 0.003) -> np.ndarray:
    """Review label per event time from {t: label}: the nearest label within tol_s, otherwise None."""
    out = np.array([None] * len(times), dtype=object)
    if not labels or len(times) == 0:
        return out
    lt = np.array(sorted(float(t) for t in labels))
    lv = [labels[t] for t in sorted(labels, key=float)]
    i = np.clip(np.searchsorted(lt, times), 1, len(lt) - 1)
    for j, t in enumerate(times):
        k = i[j] if abs(lt[i[j]] - t) < abs(lt[i[j] - 1] - t) else i[j] - 1
        if abs(lt[k] - t) <= tol_s:
            out[j] = lv[k]
    return out


def tally(kind: np.ndarray, region: np.ndarray, counted: np.ndarray, gc: np.ndarray | None,
          quiet_min: float, music_min: float, groove_min: float, floor: dict | None,
          rep: np.ndarray | None = None) -> dict:
    """Counts and per-minute metrics from the events; shared by analyze() and recount().

    `counted` is either a boolean mask (raw count) or a weight per event, 0…1 (estimated count from the
    learning model). `gc` holds the second opinion per event (NaN where there is none); `rep` the repeats with
    the music per event (repeats.py; NaN where there is no count)."""
    weighted = counted.dtype != bool
    wts = counted.astype(float)

    def count(reg=None, kinds=("pop", "click", "tick"), extra=None):
        m = np.isin(kind, kinds) & (region != "excluded")
        if reg:
            m &= region == reg
        if extra is not None:
            m &= extra
        s = float(np.sum(wts[m]))
        return round(s, 1) if weighted else int(round(s))

    enough_quiet = quiet_min >= 0.1   # at least 6 s of quiet groove
    m = {
        "counts": {
            "pop": count(kinds=("pop",)), "click": count(kinds=("click",)), "tick": count(kinds=("tick",)),
            "quiet": count("quiet"), "music": count("music"),
            "excluded": int(np.sum(region == "excluded")),
            "labeled_music": 0 if weighted else int(np.sum(~counted)),
        },
        "clicks_quiet_per_min": round(count("quiet", ("pop", "click")) / quiet_min, 2) if enough_quiet else None,
        "ticks_quiet_per_min": round(count("quiet", ("tick",)) / quiet_min, 2) if enough_quiet else None,
        "clicks_music_per_min": round(count("music", ("pop", "click")) / music_min, 2) if music_min > 0 else None,
        "pops_per_min": round(count("music", ("pop",)) / music_min, 3) if music_min > 0 else None,   # music only
    }
    so = {"available": gc is not None, "model": second_opinion.MODEL, "threshold": second_opinion.THRESHOLD}
    if gc is not None and len(gc):
        ok = np.nan_to_num(gc, nan=-1.0) >= second_opinion.THRESHOLD      # crackle has no network score (NaN)
        for reg in ("quiet", "music"):
            n = count(reg, ("pop", "click"))
            so[f"confirmed_{reg}"] = round(count(reg, ("pop", "click"), ok) / n, 3) if n else None
        so["clicks_music_confirmed_per_min"] = (round(count("music", ("pop", "click"), ok) / music_min, 2)
                                               if music_min > 0 else None)
    m["second_opinion"] = so
    # Repeats with the music (repeats.py): how many clicks in the music are sequenced hits, and the count without
    # them. A finding next to the count, not (yet) the figure the grade uses.
    rp = {"available": rep is not None, "min_repeats": repeats.MIN_REPEATS}
    if rep is not None and len(rep):
        rpt = repeats.repeating(rep)
        n = count("music", ("pop", "click"))
        rp["music_clicks"] = n
        rp["repeated_music"] = count("music", ("pop", "click"), rpt)
        rp["share_music"] = round(rp["repeated_music"] / n, 3) if n else None
        rp["clicks_music_unrepeated_per_min"] = (round(count("music", ("pop", "click"), ~rpt) / music_min, 2)
                                                if music_min > 0 else None)
    m["repeats"] = rp
    for key, fk in (("clicks_quiet_per_min", "clicks_per_min"), ("ticks_quiet_per_min", "ticks_per_min")):
        raw, fv = m.get(key), (floor or {}).get(fk)
        m[key.replace("_per_min", "_net_per_min")] = (None if raw is None or fv is None
                                                      else round(max(0.0, raw - float(fv)), 2))
    return m


def track_bounds(tracks: list[dict] | None, music_start: float | None, music_end: float | None,
                 quiet_runs: list[tuple[float, float]]) -> list[dict]:
    """Track boundaries on the side from the Discogs durations.

    Durations are laid out from the start of the music, scaled to the measured music length (if their sum
    deviates by at most 10 %) and snapped to the detected pauses (±8 s or ±3 % of the track length). Without
    complete durations there is no track table: no boundaries are better than wrong ones."""
    if not tracks or music_start is None or music_end is None or music_end <= music_start:
        return []
    durs = [t.get("dur_s") for t in tracks]
    if any(d is None or d <= 0 for d in durs):
        return []
    total, measured = float(sum(durs)), music_end - music_start
    scale = measured / total if 0.9 <= measured / total <= 1.1 else 1.0
    gaps = [(a, b) for a, b in quiet_runs if a > music_start + 1 and b < music_end - 1]
    out, t0 = [], music_start
    acc = music_start
    for i, (tr, d) in enumerate(zip(tracks, durs)):
        last = i == len(tracks) - 1
        if last:
            t1, nxt, snapped = music_end, None, True
        else:
            acc += d * scale
            tol = max(8.0, 0.03 * d)
            near = [g for g in gaps if abs(g[0] - acc) <= tol or abs(g[1] - acc) <= tol]
            if near:
                g = min(near, key=lambda g: min(abs(g[0] - acc), abs(g[1] - acc)))
                t1, nxt, snapped = g[0], g[1], True
            else:
                t1, nxt, snapped = acc, acc, False
        out.append({"pos": tr.get("pos") or f"{i + 1}", "title": tr.get("title") or "", "dur_discogs_s": round(d, 1),
                    "t0": round(t0, 2), "t1": round(max(t1, t0), 2), "snapped": snapped})
        t0 = nxt if nxt is not None else t1
    return out


def track_stats(bounds: list[dict], t: np.ndarray, kind: np.ndarray, region: np.ndarray, counted: np.ndarray,
                gc: np.ndarray | None, strength: np.ndarray, scratches: list[dict]) -> list[dict]:
    """Per track: clicks and crackle per minute, pops, scratches, share of clicks confirmed by the network and
    the strongest event."""
    out = []
    for b in bounds:
        m = (t >= b["t0"]) & (t < b["t1"]) & (region != "excluded") & counted
        cp = m & np.isin(kind, ["click", "pop"])
        minutes = max(b["t1"] - b["t0"], 1e-9) / 60
        row = dict(b)
        row.update({
            "clicks": int(cp.sum()), "clicks_per_min": round(float(cp.sum()) / minutes, 2),
            "ticks_per_min": round(float((m & (kind == "tick")).sum()) / minutes, 2),
            "pops": int((m & (kind == "pop")).sum()),
            "scratches": int(sum(1 for s in scratches if b["t0"] <= s["t"] < b["t1"])),
            "max_strength": round(float(strength[m].max()), 1) if m.any() else None,
            "confirmed_share": (round(float(np.mean(np.nan_to_num(gc[cp], nan=-1.0) >= second_opinion.THRESHOLD)), 3)
                                if gc is not None and cp.any() else None),
        })
        out.append(row)
    return out


def learned_metrics(m: dict, events: list[dict], kind: np.ndarray, region: np.ndarray, model: dict | None,
                    quiet_min: float, music_min: float, groove_min: float,
                    zero_point: dict | None = None) -> dict | None:
    """Estimated genuine clicks from the learning model (learning.estimate): the same metrics as the raw count,
    only weighted. None without a model. `zero_point`: the recording's turntable profile (learning.zero_point), used
    by models of feature version 2."""
    if not model:
        return None
    wts, _ = learning.estimate(events, model, m.get("music_ref_dbfs"), zero_point)
    est = tally(kind, region, wts, None, quiet_min, music_min, groove_min, m.get("floor"))
    usable = [k for k, v in (model.get("kinds") or {}).items() if v.get("usable")]
    return {"model_id": model.get("id"), "usable": usable, "counts": est["counts"],
            **{k: est.get(k) for k in ("clicks_music_per_min", "pops_per_min", "clicks_quiet_per_min", "ticks_quiet_per_min",
                                       "clicks_quiet_net_per_min", "ticks_quiet_net_per_min")}}


def graded_view(m: dict, learn_grade: bool) -> dict:
    """The metrics the grade is computed from: with `learn_grade` the estimated genuine counts instead of the
    reported ones (and then without the second-opinion confirmation, which the estimate replaces)."""
    lm = m.get("learned")
    if not learn_grade or not lm or not lm.get("usable"):
        return m
    g = dict(m)
    for k in ("clicks_music_per_min", "pops_per_min", "clicks_quiet_per_min", "ticks_quiet_per_min"):
        if lm.get(k) is not None:
            g[k] = lm[k]
    g["second_opinion"] = {**(m.get("second_opinion") or {}), "clicks_music_confirmed_per_min": None}
    return g


def recount(metrics: dict, events: list[dict], gcfg: GradingConfig = GradingConfig(),
            skip_labels: dict | None = None, warnings: list | None = None,
            learned: dict | None = None, learn_grade: bool = False,
            zero_point: dict | None = None) -> tuple[dict, dict]:
    """Recompute counts and grade of a stored analysis after labels have changed.

    Returns (metrics, grading) with the same keys as analyze(). With `skip_labels` the groove skips are
    reclassified as well; the `warnings` list (if given) is then updated in place with the new skip warning.
    `zero_point` is the recording's turntable profile for a learned model of feature version 2."""
    kind = np.array([e["kind"] for e in events], dtype=object)
    region = np.array([e["region"] for e in events], dtype=object)
    counted = np.array([e.get("label") != "music" for e in events], dtype=bool)
    gcs = [e.get("gc") for e in events]
    gc = np.array([(np.nan if v is None else float(v)) for v in gcs]) if any(v is not None for v in gcs) else None
    reps = [e.get("rep") for e in events]
    rep = np.array([(np.nan if v is None else float(v)) for v in reps]) if any(v is not None for v in reps) else None
    quiet_min = (metrics.get("quiet_s") or 0) / 60
    groove_min = (metrics.get("groove_s") or 0) / 60
    music_min = max(groove_min - quiet_min, 0.0)
    m = dict(metrics)
    m.update(tally(kind, region, counted, gc, quiet_min, music_min, groove_min, metrics.get("floor"), rep))
    m["learned"] = learned_metrics(m, events, kind, region, learned, quiet_min, music_min, groove_min, zero_point)
    if m["learned"] is not None:
        m["learned"]["graded"] = bool(learn_grade and m["learned"]["usable"])
    if skip_labels is not None:
        sk = apply_skip_labels(metrics.get("skips") or [], skip_labels)
        back = [e for e in sk if e["kind"] == "backward"]
        m["skips"], m["skips_back"], m["skips_weak"] = sk, len(back), sum(1 for e in sk if e["kind"] == "uncertain")
        if warnings is not None:
            warnings[:] = [w for w in warnings if not _is_skip_warning(w)]
            sw = skip_warning(back)
            if sw:
                warnings.insert(0, sw)
    if metrics.get("tracks"):
        t = np.array([e["t"] for e in events], dtype=float)
        strength = np.array([e.get("strength") or 0.0 for e in events], dtype=float)
        bounds = [{k: b[k] for k in ("pos", "title", "dur_discogs_s", "t0", "t1", "snapped")} for b in metrics["tracks"]]
        m["tracks"] = track_stats(bounds, t, kind, region, counted, gc, strength, metrics.get("scratches") or [])
    return _py(m), _py(grade(graded_view(m, learn_grade), gcfg, metrics.get("floor")))


def what_if(metrics: dict, events: list[dict], skip_labels: dict | None, ignore_events=(), ignore_skips=(),
            gcfg: GradingConfig = GradingConfig(), learned: dict | None = None, learn_grade: bool = False,
            tol_ev_s: float = 0.003, tol_skip_s: float = 2.0,
            zero_point: dict | None = None) -> tuple[dict, dict, dict]:
    """What-if: count and grade as if the given detections were not there ("what would the grade be without
    this skip?").

    Ignored events drop out completely (also from the learning model's estimate), ignored skips back count as
    "no_skip". Only for this calculation: the inputs stay unchanged, nothing is stored, labels and training
    are untouched. Returns (metrics, grading, {"events": n, "skips": n} actually matched)."""
    ign = np.sort(np.asarray([float(x) for x in ignore_events or ()], float))
    kept, hit_ev = [], 0
    for e in events:
        if len(ign):
            i = int(np.searchsorted(ign, e["t"]))
            if any(0 <= j < len(ign) and abs(ign[j] - e["t"]) <= tol_ev_s for j in (i - 1, i)):
                hit_ev += 1
                continue
        kept.append(e)
    sl = {float(t): lab for t, lab in (skip_labels or {}).items()}
    found = metrics.get("skips") or []
    hit_sk = 0
    for t in ignore_skips or ():
        t = float(t)
        if not any(s.get("kind_auto", s["kind"]) == "backward" and abs(s["t"] - t) <= tol_skip_s for s in found):
            continue
        sl = {k: v for k, v in sl.items() if abs(k - t) > tol_skip_s}
        sl[t] = "no_skip"
        hit_sk += 1
    m, g = recount(metrics, kept, gcfg, skip_labels=sl, warnings=None, learned=learned, learn_grade=learn_grade,
                   zero_point=zero_point)
    return m, g, {"events": hit_ev, "skips": hit_sk}


def a_weighting_sos(fs: int) -> np.ndarray:
    """A-weighting filter (IEC 61672) as second-order sections for sample rate fs (bilinear transform)."""
    f1, f2, f3, f4 = 20.598997, 107.65265, 737.86223, 12194.217
    z = [0, 0, 0, 0]
    p = [-2 * np.pi * f4] * 2 + [-2 * np.pi * f1] * 2 + [-2 * np.pi * f3, -2 * np.pi * f2]
    k = (2 * np.pi * f4) ** 2 * 10 ** (1.9997 / 20)        # +1.9997 dB: 0 dB gain at 1 kHz
    zd, pd, kd = signal.bilinear_zpk(z, p, k, fs)
    return signal.zpk2sos(zd, pd, kd)


def _db(power):
    return 10 * np.log10(np.maximum(power, 1e-20))


def find_scratches(times: np.ndarray, period_s: float, tol: float = 0.006, min_revs: int = 4) -> list[dict]:
    """Scratches across the grooves: a click that repeats every revolution (±tol), for at least min_revs
    revolutions.

    `times` in seconds, ascending. Returns per scratch: start, end, number of revolutions. Tolerance and minimum
    length are tight because dense clicks (2,500 per side) otherwise form random chains: ±1.5 % and 3 revolutions
    gave 163 "scratches" where chance alone predicts about 40."""
    t = np.sort(np.asarray(times, float))
    used = np.zeros(len(t), bool)
    out = []
    win = period_s * tol
    for i in range(len(t)):
        if used[i]:
            continue
        chain = [i]
        cur = t[i]
        while True:
            lo, hi = np.searchsorted(t, cur + period_s - win), np.searchsorted(t, cur + period_s + win, side="right")
            cand = [j for j in range(lo, hi) if not used[j]]
            if not cand:
                break
            j = min(cand, key=lambda j: abs(t[j] - cur - period_s))
            chain.append(j)
            cur = t[j]
        if len(chain) >= min_revs:
            used[chain] = True
            out.append({"t": round(float(t[chain[0]]), 2), "until": round(float(t[chain[-1]]), 2), "revs": len(chain)})
    return out


def _music_end(levels_db: np.ndarray, hop_s: float, ge: int, music_ref_db: float, quiet: np.ndarray,
                        drop_db: float = 8.0, min_music_s: float = 3.0) -> float | None:
    """End of the music, in seconds (for skip detection: what follows is the run-out groove, not a skip back).

    The run-out may contain handling noise, or the run-out groove may be louder than "quiet" (observed: music
    until 25:10, 10 s of silence, then the run-out groove at −31…−37 dB with the music reference at −20 dB).
    The end is therefore the start of the **first** quiet passage after which no music follows any more (nothing
    louder than reference − drop_db for min_music_s at a stretch). Not the last one: the run-out groove has quiet
    stretches too, and they would lie behind a skip-back candidate."""
    if ge <= 0:
        return None
    runs = _runs(quiet[:ge])
    if not runs:
        return None
    sm = ndimage.median_filter(levels_db[:ge], size=11, mode="nearest")
    loud = sm >= music_ref_db - drop_db
    need = int(round(min_music_s / hop_s))
    # for every frame: length of the longest "loud" run still to come (built from the back)
    longest_after = np.zeros(ge + 1, int)
    run_len = 0
    for i in range(ge - 1, -1, -1):
        run_len = run_len + 1 if loud[i] else 0
        longest_after[i] = max(longest_after[i + 1], run_len)
    for s, e in runs:
        if longest_after[e] < need:
            return float(s * hop_s)
    return None


def segment(levels_db: np.ndarray, hop_s: float, chain_db: float | None,
            no_groove_dbfs: float = -75.0) -> dict:
    """Groove, quiet passages and reference levels from the level curve (dB per hop).

    Returns {"groove": (first, end frame), "quiet": mask per frame, "music_ref_db", "noise_ref_db", "separable"}.
    """
    nfr = len(levels_db)
    sm = ndimage.median_filter(levels_db, size=11, mode="nearest")
    per_s = int(round(1 / hop_s))

    # Groove: cut off "stylus up" at the start and end. Chain noise (USB converter, stylus up) is at
    # ≤ −80 dBFS, groove noise practically always above −75 dBFS. With a calibration (recording with the
    # stylus up) the measured value + 6 dB applies. Only at the start and end, never in the middle.
    nog = sm < (chain_db + 6 if chain_db is not None else no_groove_dbfs)
    groove = ~nog
    gs, ge = 0, nfr
    for s, e in _runs(groove):
        if e - s >= per_s:
            gs = s
            break
    for s, e in reversed(_runs(groove)):
        if e - s >= per_s:
            ge = e
            break
    margin = int(EDGE_MARGIN_S / hop_s)
    has_lead = gs > 0
    has_tail = ge < nfr
    gs2 = gs + (margin if has_lead else 0)
    ge2 = ge - (margin if has_tail else 0)
    if ge2 <= gs2:
        gs2, ge2 = gs, ge

    g = sm[gs2:ge2]
    music_ref = float(np.percentile(g, 95))
    # Floor = quietest full second of the groove (moving maximum over 1 s, then its minimum): the true groove
    # silence between tracks, even if it adds up to only 5 s per side. A low percentile is not enough: on dense
    # records the 5th percentile was quiet music ("noise" at −41 instead of −70 dBFS, hundreds of clicks per
    # minute in "quiet" passages, grade F), while the same file has 4.5 s of real silence.
    noise_ref = float(np.min(ndimage.maximum_filter1d(g, size=per_s, mode="nearest"))) if len(g) >= per_s else float(np.min(g))
    quiet = np.zeros(nfr, bool)
    separable = music_ref - noise_ref >= 15
    if separable:
        # Floor + 8 dB alone fails as soon as the lead-in groove is part of the recording: freshly washed it lies
        # at −70 dBFS, while pauses between tracks lie at −46 (reverb, room) and would not count as pauses. Pauses
        # lie 20–35 dB below the music level, so everything below music reference − QUIET_BELOW_MUSIC_DB counts as
        # well; quiet music (quietest 2 s of a dense record at −32 dBFS) stays above it.
        thr = max(noise_ref + min(8.0, (music_ref - noise_ref) / 3), music_ref - QUIET_BELOW_MUSIC_DB)
        cand = np.zeros(nfr, bool)
        cand[gs2:ge2] = sm[gs2:ge2] < thr
        shrink = int(QUIET_SHRINK_S / hop_s)
        for s, e in _runs(cand):
            if e - s >= MIN_QUIET_S / hop_s:
                quiet[s + shrink:e - shrink] = True
    return {"groove": (gs2, ge2), "quiet": quiet, "music_ref_db": music_ref,
            "noise_ref_db": noise_ref, "separable": separable}


def analyze(audio: np.ndarray, sr: int, cfg: AnalysisConfig = AnalysisConfig(),
            gcfg: GradingConfig = GradingConfig(), chain_db: float | None = None,
            overview_points: int = 3000, rpm: float = 100 / 3, ref_dbfs: float | None = None,
            floor: dict | None = None, labels: dict | None = None, tracks: list[dict] | None = None,
            skip_labels: dict | None = None, fixed_gain: bool = False, so_cache: dict | None = None) -> dict:
    """Analyze one recorded side.

    audio: (n, channels) float32 in the range ±1, sr: sample rate in Hz. Returns a dict with metrics, grading,
    warnings (i18n messages), events, overview and so_new.
    so_cache: second opinion from earlier runs on the same file ({time (5 decimals): p}); only missing candidates
    go to the network, the new values are returned in `so_new` (the worker keeps them per file checksum).
    fixed_gain: the level cannot be adjusted on the device (e.g. USB turntables): no advice to check the gain, and
    the harmless clipping at needle drop is not reported as a warning.
    skip_labels: labels of groove skips from earlier runs ({t: "run_out"|"no_skip"}), ±2 s.
    tracks: tracks of this side from Discogs ([{pos, title, dur_s}]) for the track table (metrics["tracks"]).
    ref_dbfs: reference level of the turntable profile (1 kHz, 5 cm/s at calibration) for the level check.
    floor: turntable baseline of the profile ({"clicks_per_min", "ticks_per_min"} in the silent groove of the
    test record): the quiet-passage metrics are additionally reported as differences (`…_net_per_min`) and graded
    that way.
    labels: review labels {t: "defect"|"music"|"unsure"} from earlier runs; events labeled "music" are not
    counted, the labels are re-attached to the new events (±3 ms)."""
    if audio.ndim == 1:
        audio = audio[:, None]
    n, nch = audio.shape
    hop = int(round(sr * HOP_S))
    nfr = n // hop

    clip_mask = (np.abs(audio) >= 0.99995).any(axis=1)
    clip = int(np.sum(np.abs(audio) >= 0.99995))
    clip_t = np.flatnonzero(clip_mask) / sr
    peak_dbfs = [float(20 * np.log10(max(np.max(np.abs(audio[:, c])), 1e-12))) for c in range(nch)]

    hp = signal.butter(2, 20, btype="high", fs=sr, output="sos").astype(np.float32)
    power = np.zeros(nfr)
    hpch = []
    for c in range(nch):
        y = signal.sosfilt(hp, audio[:, c]).astype(np.float32)
        power += np.mean(y[:nfr * hop].reshape(nfr, hop).astype(np.float64) ** 2, axis=1)
        hpch.append(y)
    power /= nch
    levels_db = _db(power)

    seg = segment(levels_db, HOP_S, chain_db, cfg.no_groove_dbfs)
    gs, ge = seg["groove"]
    quiet = seg["quiet"]
    # Clipping only counts in the music: grace period CLIP_GRACE_S after lowering and before lifting the stylus
    # (needle drop, closing the dust cover, a hand on the platter), and nothing after the end of the music (run-out
    # groove). A needle drop alone produced 8 samples at 0 dBFS and made a side "unsuitable for comparison"; a few
    # dozen clipped samples in loud music are inaudible, so clipping is only mentioned from CLIP_MENTION on.
    g0, g1 = gs * HOP_S + cfg.clip_grace_s, ge * HOP_S - cfg.clip_grace_s
    clip_in_t = clip_t[(clip_t >= g0) & (clip_t <= g1)]
    clip_in = int(clip_in_t.size)

    # Noise in quiet passages (mean over the power of both channels)
    qpow = power[quiet]
    noise_dbfs = float(_db(np.mean(qpow))) if qpow.size else None
    noise_a_dbfs = None
    if qpow.size:
        aw = a_weighting_sos(sr)
        mono = np.mean(np.stack(hpch, axis=1), axis=1)
        acc, cnt = 0.0, 0
        for s, e in _runs(quiet):
            yq = signal.sosfilt(aw, mono[s * hop:e * hop])
            skip = min(len(yq) // 4, int(0.05 * sr))       # filter settling time
            acc += float(np.sum(yq[skip:] ** 2))
            cnt += len(yq) - skip
        noise_a_dbfs = float(_db(acc / max(cnt, 1)) + 10 * np.log10(nch)) if cnt else None
    del hpch

    # Technical data (levels, true peak, loudness, correlation, tonal balance, rumble/hum)
    frame_music = np.zeros(nfr, bool)
    frame_music[gs:ge] = ~quiet[gs:ge]
    try:
        tech = technical.measure(audio, sr, (gs * HOP_S, ge * HOP_S), frame_music, quiet, HOP_S, seg["music_ref_db"])
    except Exception as e:  # noqa: BLE001 — additional data must never stop the analysis
        tech = {"ok": False, "reason": str(e)}

    # Clicks
    per_ch = [detect.detect_channel(audio[:, c], sr, cfg.detect) for c in range(nch)]
    ev = detect.merge_channels(per_ch, cfg.detect.merge_ms)
    fr = np.minimum((ev["t"] * sr / hop).astype(int), nfr - 1)
    region = np.where((fr < gs) | (fr >= ge), "excluded", np.where(quiet[fr], "quiet", "music"))
    # Pop = loud click, and it must also be strong: by residual amplitude alone, drum transients in loud music
    # (peaks at 0 dBFS, strength 8–18) came out as "pops", 9 per minute, grade F.
    strong = ev["strength"] >= cfg.click_strength
    kind = np.where(strong & (ev["amp_dbfs"] >= cfg.pop_dbfs), "pop", np.where(strong, "click", "tick"))
    # Second opinion (grooveclean network) only for clicks and pops, not for crackle (halves the time), taken
    # from the cache of the same file where available; plus the review labels of earlier runs
    gc, so_seconds, so_new, so_cached = None, 0.0, {}, 0
    strong_idx = np.flatnonzero(strong)
    if cfg.second_opinion and second_opinion.available() and strong_idx.size:
        t_so = time.time()
        ts = ev["t"][strong_idx]
        keys = [round(float(t), 5) for t in ts]
        cached = so_cache or {}
        p = np.full(len(ts), np.nan)
        need = []
        for i, k in enumerate(keys):
            if k in cached:
                p[i] = float(cached[k])
            else:
                need.append(i)
        so_cached = len(keys) - len(need)
        if need:
            pn = second_opinion.opinion(audio, sr, ts[need])
            for j, i in enumerate(need):
                p[i] = float(pn[j])
                so_new[keys[i]] = float(pn[j])
        gc = np.full(len(ev), np.nan)
        gc[strong_idx] = p
        so_seconds = round(time.time() - t_so, 1)
    label = labels_for(ev["t"], labels)
    counted = np.array([lb != "music" for lb in label], dtype=bool)
    # Repeats with the music: clicks and pops in the music only (repeats.py)
    rep = None
    if cfg.repeat_filter:
        sel = np.flatnonzero(strong & (region == "music"))
        rep = np.full(len(ev), np.nan)
        if sel.size:
            rep[sel] = repeats.count(audio, sr, ev["t"][sel], ev["ch"][sel], rpm=rpm)

    groove_min = (ge - gs) * HOP_S / 60
    quiet_min = float(np.sum(quiet)) * HOP_S / 60
    music_min = max(groove_min - quiet_min, 0.0)
    enough_quiet = quiet_min >= 0.1   # at least 6 s of quiet groove
    metrics = {
        "duration_s": round(n / sr, 2),
        "groove_s": round(groove_min * 60, 2),
        "groove_end_s": round(ge * HOP_S, 2),           # where the groove ends: after it, the stylus was up
        "quiet_s": round(quiet_min * 60, 2),
        "samplerate": sr,
        "channels": nch,
        "peak_dbfs": [round(p, 2) for p in peak_dbfs],
        "clip_samples": clip,
        "level_vs_profile_db": None if ref_dbfs is None else round(seg["music_ref_db"] - ref_dbfs, 2),
        "technical": tech,
        "clip_in_groove": clip_in,
        "clip_first_s": None if not clip_in else round(float(clip_in_t[0]), 2),
        "music_ref_dbfs": round(seg["music_ref_db"], 2),
        "noise_dbfs": None if noise_dbfs is None else round(noise_dbfs, 2),
        "noise_a_dbfs": None if noise_a_dbfs is None else round(noise_a_dbfs, 2),
        "noise_rel_db": None if noise_dbfs is None else round(noise_dbfs - seg["music_ref_db"], 2),
        "chain_db": chain_db,
        "noise_above_chain_db": None if (noise_dbfs is None or chain_db is None)
        else round(noise_dbfs - chain_db, 2),
        "separable": seg["separable"],
    }
    metrics.update(tally(kind, region, counted, gc, quiet_min, music_min, groove_min, floor, rep))
    metrics["second_opinion"].update({"seconds": so_seconds, "cached": so_cached, "computed": len(so_new)})
    metrics["floor"] = floor
    metrics["detect"] = {"threshold": cfg.detect.threshold, "stripe_db": cfg.detect.stripe_db, "stripe_ms": cfg.detect.stripe_ms}
    # Groove skips
    groove_s = (gs * HOP_S, ge * HOP_S)
    # End of the music = first quiet passage after which no more music follows. Taking the start of the last quiet
    # passage that reaches the end of the groove failed when a click split the run-out groove into two passages:
    # the run-out groove then lay before the "end of the music" and counted as a skip back.
    music_end = _music_end(levels_db, HOP_S, ge, seg["music_ref_db"], quiet)
    # Start of the music: first frame in the groove from which the smoothed level stays above the groove
    # silence for 1 s
    sm_lv = ndimage.median_filter(levels_db, size=11, mode="nearest")
    thr = seg["music_ref_db"] - 25.0
    music_start = None
    need = int(round(1.0 / HOP_S))
    for s0, e0 in _runs(sm_lv[gs:ge] >= thr):
        if e0 - s0 >= need:
            music_start = (gs + s0) * HOP_S
            break
    metrics_music = {"music_start_s": None if music_start is None else round(music_start, 2),
                     "music_end_s": None if music_end is None else round(music_end, 2),
                     "music_len_s": None if (music_start is None or music_end is None) else round(music_end - music_start, 1)}
    skip_events = apply_skip_labels(skips.find_repeats(audio, sr, rpm, groove_s, music_end), skip_labels)
    back = [e for e in skip_events if e["kind"] == "backward"]
    weak = [e for e in skip_events if e["kind"] == "uncertain"]
    metrics["rpm"] = round(rpm, 3)
    metrics.update(metrics_music)
    if music_end is not None and clip_in:                        # the run-out groove is not clipping in the music
        clip_in_t = clip_in_t[clip_in_t <= music_end]
        clip_in = int(clip_in_t.size)
        metrics["clip_in_groove"] = clip_in
        metrics["clip_first_s"] = None if not clip_in else round(float(clip_in_t[0]), 2)
    # Recurring scratches from the clicks/pops in the groove (crackle is too dense to form chains)
    strong_t = np.sort(ev["t"][(region != "excluded") & (kind != "tick") & counted])
    scratches = find_scratches(strong_t, skips.revolution_s(rpm))
    metrics["scratches"] = scratches
    metrics["scratch_count"] = len(scratches)
    metrics["scratch_clicks"] = int(sum(s["revs"] for s in scratches))
    metrics["skips"] = skip_events
    metrics["skips_back"] = len(back)
    metrics["skips_weak"] = len(weak)

    warnings = []
    if back:
        warnings.append(skip_warning(back))
        if any(e["repeats"] >= 4 for e in back):
            warnings.append(i18n.msg("analysis.long_repeat"))
    # Level against the turntable calibration: music normally lies 0–10 dB below the 1 kHz reference tone
    # (5 cm/s). Far below means the interface gain was changed: the detector then misses small clicks, and the
    # numbers are not comparable with other recordings (observed: music 20 dB below the reference gave 36–69
    # clicks/min, the same side at normal level 211/min).
    if ref_dbfs is not None and not fixed_gain:
        dlev = seg["music_ref_db"] - ref_dbfs
        if dlev < -14:
            warnings.append(i18n.msg("analysis.level_too_low", diff=abs(dlev), music=seg["music_ref_db"], ref=ref_dbfs))
        elif dlev > 4:
            warnings.append(i18n.msg("analysis.level_too_high", diff=dlev))
    if clip_in >= cfg.clip_mention:
        unusable = clip_in > gcfg.clip_samples_max
        parts = [i18n.msg("analysis.clip_in_music", n=clip_in, time=_mmss(float(clip_in_t[0]))),
                 i18n.msg("analysis.clip_unusable") if unusable else i18n.msg("analysis.clip_tight")]
        if not fixed_gain:
            parts.append(i18n.msg("analysis.clip_reduce_gain"))
        warnings.append(i18n.msg_join(parts))
    elif clip and clip_in < cfg.clip_mention and not fixed_gain and clip - clip_in >= 8:
        warnings.append(i18n.msg("analysis.clip_at_needle_drop", n=clip - clip_in))
    if not seg["separable"]:
        warnings.append(i18n.msg("analysis.not_separable"))
    elif not enough_quiet:
        warnings.append(i18n.msg("analysis.too_little_quiet"))

    # Overview for the web interface
    pts = min(overview_points, nfr)
    b = nfr // pts * hop if pts else hop
    m = n // b
    mono = np.mean(audio[:m * b], axis=1).reshape(m, b)
    quiet_s = [(s * HOP_S, e * HOP_S) for s, e in _runs(quiet)]
    bounds = track_bounds(tracks, music_start, music_end, quiet_s)
    metrics["tracks"] = track_stats(bounds, ev["t"], kind, region, counted, gc, ev["strength"], scratches) if bounds else []
    overview = {
        "bucket_s": b / sr,
        "min": np.round(mono.min(axis=1), 4).tolist(),
        "max": np.round(mono.max(axis=1), 4).tolist(),
        "level_db": np.round(_db(np.mean(mono.astype(np.float64) ** 2, axis=1)), 1).tolist(),   # RMS per bucket
        "quiet": [[round(a, 2), round(z, 2)] for a, z in quiet_s],
        "groove": [round(gs * HOP_S, 2), round(ge * HOP_S, 2)],
    }

    events = [
        {"t": round(float(e["t"]), 5), "dur_ms": round(float(e["dur_ms"]), 3),
         "strength": round(float(e["strength"]), 2), "amp_dbfs": round(float(e["amp_dbfs"]), 2),
         "ch": int(e["ch"]), "kind": str(k), "region": str(r),
         "gc": None if gc is None or np.isnan(g) else round(float(g), 3), "label": lb,
         "rep": None if rep is None or np.isnan(rp) else int(rp)}
        for e, k, r, g, lb, rp in zip(ev, kind, region, gc if gc is not None else [np.nan] * len(ev), label,
                                      rep if rep is not None else [np.nan] * len(ev))
    ]
    return _py({"metrics": metrics, "grading": grade(metrics, gcfg, floor), "warnings": warnings,
                "events": events, "overview": overview, "so_new": so_new})


def _py(o):
    """numpy scalars → Python types (for JSON), recursively through dicts and lists."""
    if isinstance(o, dict):
        return {k: _py(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_py(v) for v in o]
    if isinstance(o, np.generic):
        return o.item()
    return o


def analyze_file(path, cfg: AnalysisConfig = AnalysisConfig(), gcfg: GradingConfig = GradingConfig(),
                 chain_db: float | None = None, rpm: float = 100 / 3, ref_dbfs: float | None = None,
                 floor: dict | None = None, labels: dict | None = None, tracks: list[dict] | None = None,
                 skip_labels: dict | None = None, fixed_gain: bool = False, so_cache: dict | None = None) -> dict:
    """analyze() for an audio file (any format soundfile can read)."""
    import soundfile as sf
    audio, sr = sf.read(str(path), dtype="float32", always_2d=True)
    return analyze(audio, sr, cfg, gcfg, chain_db, rpm=rpm, ref_dbfs=ref_dbfs, floor=floor, labels=labels, tracks=tracks,
                   skip_labels=skip_labels, fixed_gain=fixed_gain, so_cache=so_cache)


def chain_noise_db(path) -> float:
    """Chain noise from a calibration recording (stylus up): mean power in dBFS, 20 Hz high-pass."""
    import soundfile as sf
    audio, sr = sf.read(str(path), dtype="float32", always_2d=True)
    return chain_noise_dbfs(audio, sr)


def chain_noise_dbfs(audio: np.ndarray, sr: int) -> float:
    """chain_noise_db on samples ((n, channels) float in ±1) instead of a file (tools/eval_features.py)."""
    hp = signal.butter(2, 20, btype="high", fs=sr, output="sos")
    skip = min(len(audio) // 4, int(0.2 * sr))
    p = np.mean([np.mean(signal.sosfilt(hp, audio[:, c])[skip:] ** 2) for c in range(audio.shape[1])])
    return round(float(_db(p)), 2)
