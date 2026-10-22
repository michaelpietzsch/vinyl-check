"""Condition grade of a record side from the measurements (Goldmine scale, audible side only).

Single grades
-------------
Every metric has five upper limits (NM, VG+, VG, G+, G); above the last one it is F. Within its band a value
also gets a score (BANDS: NM 90–100, VG+ 75–90, VG 55–75, G+ 35–55, G 15–35, F 0–15), interpolated linearly
between the limits, so that "just inside VG+" and "almost NM" can be told apart. "Mint" is never given: it
cannot be judged without a visual inspection.

What counts: the music
----------------------
The grade reflects what happens while the music plays. By default only two metrics determine it:

* clicks per minute in the music (only those confirmed by the second opinion, when that is available), and
* pops per minute in the music.

They are combined as a weighted score, 0.9 × clicks + 0.1 × pops, and the grade is the band of that score.
Pops get the small weight because in loud, dense music most detected "pops" are drum transients that the second
opinion cannot tell apart from real pops: compared against the digital master of a rock record, about three
quarters of the pops found in the music were drum hits, while only about one in seven clicks in the music was
false. The downside is that many genuine pops barely lower the grade; when a master is available, the grade by
ear (audible genuine clicks per side, computed in the web layer) covers that case. Without weights the overall
grade is the worst single grade of the graded metrics.

Clicks and crackle in quiet passages (pauses between tracks, lead-in and run-out groove) are shown with their own
single grades but are information only: what matters to a listener is what happens while the music plays.
Optionally they can be enabled as *secondary* metrics, which may lower the grade by at most one step and only when
they are at least `secondary_gap` steps worse. Lead-in and run-out groove never count. The noise level also gets
an informational single grade only: its limits are not calibrated, and on some turntables the playback chain, not
the record, determines the value.

Limits
------
Clicks in the music: NM up to 8/min, VG+ 15, VG 30, G+ 50, G 100. Pops in the music: NM up to 2/min, VG+ 5,
VG 10, G+ 20, G 40. These accept dust: no handling keeps a record entirely free of it, and a clean, well-kept
record should still reach NM. They were checked against records whose condition was known by ear: clean test
pressings and well-kept records reach NM, a record judged VG by ear lands at VG, scratched copies stay at G+.
All limits are configurable ([grading] in the configuration); as long as `calibrated` is false, the result carries
a hint that the grade is a rough estimate. To calibrate, measure a few records whose condition you know by ear and
adjust the limits.

Special rules
-------------
* F is reserved for a stylus that skips back (Goldmine: "damaged, skips"). A side that plays through ends at G,
  however much it crackles.
* Clipping in the groove beyond `clip_samples_max` is a fault of the recording, not of the record: no grade.
* Turntable baseline (`floor`): clicks and crackle in quiet passages are reduced by what the same playback chain
  produces in the silent groove of the test record. Some turntable/cartridge combinations generate hundreds of
  detector events per minute even there; without the subtraction every record played on them would look bad.
* A record's grade is the average of its side grades (`record_grade`); a skipping side makes the record F.

VERSION numbers the grading scheme. When it changes, the service re-grades all stored analyses at startup from the
stored events, without processing the audio again.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import i18n

GRADES = ["NM", "VG+", "VG", "G+", "G", "F"]
# Grading scheme: 1 = worst single grade of all metrics, 2 = grade from the music (pauses cost at most one step),
# 3 = pauses information only and pops counted in the music only, 4 = weighted 90 % clicks / 10 % pops in the
# music, 5 = dust accepted: milder limits
VERSION = 5
# Score range per grade (top, bottom)
BANDS = [(100, 90), (90, 75), (75, 55), (55, 35), (35, 15), (15, 0)]

# Display name per metric (messages, rendered in the reader's language)
METRIC_LABELS = {
    "clicks_music_per_min": i18n.msg("grading.metric_clicks_music_per_min"),
    "clicks_quiet_per_min": i18n.msg("grading.metric_clicks_quiet_per_min"),
    "ticks_quiet_per_min": i18n.msg("grading.metric_ticks_quiet_per_min"),
    "pops_per_min": i18n.msg("grading.metric_pops_per_min"),
    "noise_rel_db": i18n.msg("grading.metric_noise_rel_db"),
}


@dataclass(frozen=True)
class GradingConfig:
    calibrated: bool = False
    # Anchors (confirmed clicks per minute in the music, one turntable): clean records 1.4–9 · a record judged VG
    # by ear 14–16 · two copies of a worn record 16–19 and 36–64 · a record judged problematic by ear 33–54
    clicks_music_per_min: tuple = (8, 15, 30, 50, 100)
    clicks_quiet_per_min: tuple = (3, 10, 30, 80, 200)
    ticks_quiet_per_min: tuple = (20, 60, 150, 400, 1000)
    pops_per_min: tuple = (2.0, 5.0, 10.0, 20.0, 40.0)
    noise_rel_db: tuple = (-55, -48, -42, -36, -30)
    metrics: tuple = field(default=("clicks_music_per_min", "clicks_quiet_per_min", "ticks_quiet_per_min",
                                    "pops_per_min", "noise_rel_db"))
    # Only these determine the overall grade. Noise gets a single grade but does not count: its limits are not
    # calibrated, and the playback chain can dominate the value (silent groove of the test record −58 dBFS on one
    # turntable, −64 on another, pauses of a record −49). Configurable via [grading] graded; the click detector
    # is the measuring instrument.
    graded: tuple = field(default=("clicks_music_per_min", "pops_per_min"))
    # Pauses (above the turntable baseline): single grade that lowers the overall grade by at most one step, and
    # only when it is ≥ secondary_gap steps worse. Empty by default: pauses are information only (can be enabled
    # via [grading] secondary).
    secondary: tuple = field(default=())
    secondary_gap: int = 2
    # Weights of the grade (weighted mean of the scores); empty = worst single grade
    weights: tuple = field(default=(("clicks_music_per_min", 0.9), ("pops_per_min", 0.1)))
    # From this many clipped samples in the groove there is no grade. A turntable with fixed gain clips a few
    # dozen samples on loud records (11–43 observed); that does not change the count.
    clip_samples_max: int = 500


def record_grade(side_grades) -> str | None:
    """Grade of a whole record from its side grades.

    The average of the sides' grade steps, half steps rounded toward the worse grade (NM + VG+ → VG+); if one side
    skips (F), the record is F. None without a graded side. Per side, the latest graded recording should be passed.
    """
    g = [x for x in side_grades if x in GRADES]
    if not g:
        return None
    if "F" in g:
        return "F"
    return GRADES[int(sum(GRADES.index(x) for x in g) / len(g) + 0.5)]


def _grade_one(value: float, limits: tuple) -> tuple[str, float]:
    """Grade and score (0–100) of one value against its five upper limits.

    The bands below the first and above the last limit are extended by the width of the neighbouring band, so
    that NM and F also have a finite range for interpolating the score.
    """
    t = list(map(float, limits))
    edges = [t[0] - (t[1] - t[0])] + t + [t[-1] + (t[-1] - t[-2])]
    for i, (hi_s, lo_s) in enumerate(BANDS):
        lo, hi = edges[i], edges[i + 1]
        if value <= hi or i == len(BANDS) - 1:
            frac = min(1.0, max(0.0, (value - lo) / (hi - lo)))
            return GRADES[i], round(hi_s - frac * (hi_s - lo_s), 1)
    raise AssertionError("unreachable")


FLOOR_KEYS = {"clicks_quiet_per_min": "clicks_per_min", "ticks_quiet_per_min": "ticks_per_min"}


def _label(parts: list) -> dict:
    """One label message from a metric name and optional qualifiers ("(confirmed)", "above baseline", …)."""
    return dict(parts[0]) if len(parts) == 1 else i18n.msg_join(parts)


def _floor_value(v):
    """A baseline value for the hint text: rounded number, or a dash when the profile has none."""
    return "–" if v is None else round(float(v), 1)


def grade(metrics: dict, cfg: GradingConfig = GradingConfig(), floor: dict | None = None) -> dict:
    """Grade one side from its metrics (as produced by analysis.tally).

    floor: turntable baseline {"clicks_per_min", "ticks_per_min"} from the turntable profile; the two quiet-passage
    metrics are then graded as the difference (at least 0), with raw value and baseline kept alongside.
    Returns {"grade", "score", "per_metric", "calibrated", "hinweis"}; "hinweis" is an i18n message explaining the
    result (or None), the labels in "per_metric" are messages as well.
    """
    per = {}
    core = {}                                  # label without the role qualifier, for the hints
    used_floor = False
    for name in cfg.metrics:
        v = metrics.get(name)
        fv = (floor or {}).get(FLOOR_KEYS.get(name, ""))
        parts, raw = [METRIC_LABELS[name]], None
        if name == "clicks_music_per_min" and v is not None:            # second opinion: only confirmed clicks count
            conf = (metrics.get("second_opinion") or {}).get("clicks_music_confirmed_per_min")
            if conf is not None:
                raw, v = round(float(v), 2), float(conf)
                parts.append(i18n.msg("grading.label_confirmed"))
        if v is None:
            core[name] = parts
            per[name] = {"label": _label(parts), "value": None, "grade": None, "score": None, "raw": None, "floor": fv}
            continue
        if fv is not None:
            raw, v = round(float(v), 2), max(0.0, float(v) - float(fv))
            parts.append(i18n.msg("grading.label_above_floor"))
            used_floor = True
        g, s = _grade_one(float(v), getattr(cfg, name))
        core[name] = parts
        per[name] = {"label": _label(parts), "value": round(float(v), 2), "grade": g, "score": s, "raw": raw,
                     "floor": fv}
    for name, p in per.items():
        p["in_grade"] = name in cfg.graded
        p["secondary"] = name in cfg.secondary and name not in cfg.graded
        if p["secondary"]:
            p["label"] = _label(core[name] + [i18n.msg("grading.label_secondary")])
        elif not p["in_grade"]:
            p["label"] = _label(core[name] + [i18n.msg("grading.label_info_only")])
    rated = [p for n, p in per.items() if p["grade"] is not None and n in cfg.graded]
    if not rated:
        return {"grade": None, "score": None, "per_metric": per, "calibrated": cfg.calibrated,
                "hinweis": i18n.msg("grading.no_metrics")}
    if (metrics.get("clip_in_groove") or 0) > cfg.clip_samples_max:
        return {"grade": None, "score": None, "per_metric": per, "calibrated": cfg.calibrated,
                "hinweis": i18n.msg("grading.clipped", n=metrics["clip_in_groove"])}
    worst = max(rated, key=lambda p: GRADES.index(p["grade"]))
    overall, score = worst["grade"], min(p["score"] for p in rated)
    hints = []
    wts = {n: float(w) for n, w in (cfg.weights or ()) if n in per and per[n]["score"] is not None}
    if wts and sum(wts.values()) > 0:                        # weighted score instead of the worst single grade
        score = round(sum(per[n]["score"] * w for n, w in wts.items()) / sum(wts.values()), 1)
        overall = next(GRADES[i] for i, (hi, lo) in enumerate(BANDS) if score >= lo or i == len(BANDS) - 1)
        for n, w in wts.items():
            per[n]["weight"] = round(w / sum(wts.values()), 3)
        if overall != worst["grade"]:
            items = [i18n.msg("grading.weight_item", label=METRIC_LABELS[n], pct=round(100 * w / sum(wts.values())))
                     for n, w in wts.items()]
            hints.append(i18n.msg("grading.weighted", weights=i18n.msg_join(items, ", "), worst=worst["grade"]))
    if overall == "F":
        overall, score = "G", max(score, 15.0)
        hints.append(i18n.msg("grading.f_without_skip", label=worst["label"]))
    sec = [n for n, p in per.items() if p["grade"] is not None and p.get("secondary")]
    if sec:                                                   # pauses: at most one step lower, never below G
        ws = max(sec, key=lambda n: GRADES.index(per[n]["grade"]))
        gap = GRADES.index(per[ws]["grade"]) - GRADES.index(overall)
        if gap >= cfg.secondary_gap and GRADES.index(overall) < 4:
            overall = GRADES[GRADES.index(overall) + 1]
            score = min(score, float(BANDS[GRADES.index(overall)][0]))
            hints.append(i18n.msg("grading.secondary_penalty", label=_label(core[ws]), grade=per[ws]["grade"]))
    if metrics.get("skips_back"):
        overall, score = "F", 0.0
        hints.append(i18n.msg("grading.skipped_back", n=metrics["skips_back"]))
    if not cfg.calibrated:
        hints.append(i18n.msg("grading.uncalibrated"))
    if used_floor:
        hints.append(i18n.msg("grading.floor_applied", clicks=_floor_value(floor.get("clicks_per_min")),
                              ticks=_floor_value(floor.get("ticks_per_min"))))
    left_out = [METRIC_LABELS[n] for n in cfg.metrics if n not in cfg.graded and n not in cfg.secondary and per.get(n, {}).get("grade")]
    if left_out:
        hints.append(i18n.msg("grading.not_in_grade", metrics=i18n.msg_join(left_out, ", ")))
    return {
        "grade": overall,
        "score": score,
        "per_metric": per,
        "calibrated": cfg.calibrated,
        "hinweis": i18n.msg_join(hints) if hints else None,
    }
