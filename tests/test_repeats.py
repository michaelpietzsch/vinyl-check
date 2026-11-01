"""Repeats with the music: sequenced hits repeat, defects do not."""

import numpy as np
from scipy import ndimage

from vinylcheck import analysis, detect, repeats, synth

SR = 48000
LOOP_LEVEL_DBFS = -21.0


def _sequenced_side(seed=5, duration_s=150.0, period_s=0.53, hits_per_loop=4, clicks_per_min=12.0):
    """A synthetic side whose music is a loop (as a sequencer plays it) with one identical hard transient every
    `period_s`, `hits_per_loop` of them per loop, plus random clicks (the defects). Returns audio, the hit times
    and the defect times.

    The hits sit beside the synthetic music's own kicks (every 0.5 s): a transient on a ringing attack is not a
    click to the detector, by design."""
    audio, truth = synth.make_side(duration_s=duration_s, seed=seed, clicks_per_min=0, pops=0, music_dbfs=-120.0)
    rng = np.random.default_rng(seed + 1)
    loop_s = period_s * hits_per_loop
    loop = synth._music(int(round(loop_s * SR)), SR, rng, LOOP_LEVEL_DBFS)
    hit = synth._db(-6.0) * np.array([1.0, -0.7, 0.45, -0.3, 0.18, -0.1, 0.05])
    # Defects go where the loop is quiet in the treble (±30 ms): a defect within a few ms of a transient the
    # music itself repeats (a drum burst, a chord attack, a hit) cannot be told apart from it by any method that
    # looks for the spike at the repeat, so the test does not pretend it can
    env = detect.hf_envelope(loop.mean(axis=1), SR)[0]
    bin_s = detect.STRIPE_WIN // 2 / SR
    busy = ndimage.maximum_filter1d(env, size=int(0.06 / bin_s))
    quiet_bins = np.flatnonzero(busy <= np.percentile(busy, 30))
    hits, defects = [], []
    for a, b in truth.music:
        ia, ib = int(a * SR), int(b * SR)
        reps = -(-(ib - ia) // len(loop))
        audio[ia:ib] += np.tile(loop, (reps, 1))[:ib - ia]
        t = a + 0.137
        while t < b - 0.5:
            i = int(t * SR)
            audio[i:i + len(hit), :] += hit[:, None]
            hits.append(t)
            t += period_s
        for _ in range(int(clicks_per_min * (b - a) / 60)):
            for _try in range(50):
                tc = a + rng.integers(0, int((b - a) / loop_s)) * loop_s + rng.choice(quiet_bins) * bin_s
                if a + 0.5 < tc < b - 0.5 and not _near(np.array(hits), tc, 0.05):
                    break
            synth.add_click(audio, SR, tc, rng.uniform(-35.0, -15.0), int(rng.choice([1, 2, 3])), rng)
            defects.append(tc)
    return np.clip(audio, -1, 1).astype(np.float32), np.array(hits), np.array(sorted(defects))


def _near(times, t, tol=0.004):
    return bool(len(times)) and bool(np.min(np.abs(times - t)) <= tol)


def test_sequenced_hits_repeat_and_defects_do_not():
    audio, hits, defects = _sequenced_side()
    res = analysis.analyze(audio, SR, analysis.AnalysisConfig(second_opinion=False))
    ev = [e for e in res["events"] if e["region"] == "music" and e["kind"] in ("click", "pop")]
    on_hit = [e for e in ev if _near(hits, e["t"])]
    on_defect = [e for e in ev if _near(defects, e["t"]) and not _near(hits, e["t"])]
    assert len(on_hit) >= 0.7 * len(hits) and len(on_defect) >= 8, (len(on_hit), len(hits), len(on_defect))
    rep_hit = np.array([e["rep"] or 0 for e in on_hit])
    rep_def = np.array([e["rep"] or 0 for e in on_defect])
    assert np.mean(rep_hit >= repeats.MIN_REPEATS) >= 0.8, rep_hit            # the sequenced hits repeat …
    assert np.mean(rep_def >= repeats.MIN_REPEATS) <= 0.15, rep_def           # … the defects do not: their
    # surroundings come back with the loop, but the spike is not there (the loop repeats exactly, so a defect
    # near one of its transients can get a single chance match; real sides: 0 of 25 and 1 of 15 judged defects)
    m = res["metrics"]["repeats"]
    assert m["available"] and m["repeated_music"] >= 0.8 * len(on_hit)
    # the rule (sequenced music from 20 % repeating clicks on) takes the repeating hits out of the count
    assert m["rule"]["active"] and m["rule"]["mode"] == "auto" and m["share_music"] >= 0.5
    assert m["rule"]["removed"] == res["metrics"]["counts"]["rule_music"] >= 0.8 * len(on_hit)
    assert res["metrics"]["clicks_music_per_min"] == m["clicks_music_unrepeated_per_min"]
    # ticks and clicks outside the music carry no count
    assert all(e["rep"] is None for e in res["events"] if e["kind"] == "tick" or e["region"] != "music")
    # recount from the stored events keeps the figures
    m2, _ = analysis.recount(res["metrics"], res["events"])
    assert m2["repeats"]["repeated_music"] == m["repeated_music"]
    assert m2["repeats"]["clicks_music_unrepeated_per_min"] == m["clicks_music_unrepeated_per_min"]
    assert m2["clicks_music_per_min"] == res["metrics"]["clicks_music_per_min"]


def test_repeat_rule_in_the_count_and_verdicts_override():
    """With the rule off every click counts; a verdict "click" on a repeating hit counts it again, a verdict
    "false alarm" on a defect takes it out; the record's own setting replaces the configured rule."""
    audio, hits, defects = _sequenced_side()
    res = analysis.analyze(audio, SR, analysis.AnalysisConfig(second_opinion=False))
    events, metrics = res["events"], res["metrics"]
    on = metrics["clicks_music_per_min"]
    m_off, _ = analysis.recount(metrics, events, acfg=analysis.AnalysisConfig(repeat_rule="off"))
    assert not m_off["repeats"]["rule"]["active"] and m_off["counts"]["rule_music"] == 0
    assert m_off["clicks_music_per_min"] > on * 2                              # the hits are back in the count
    m_on, _ = analysis.recount(metrics, events, acfg=analysis.AnalysisConfig(repeat_rule="on", repeat_rule_share=0.99))
    assert m_on["repeats"]["rule"]["active"] and m_on["clicks_music_per_min"] == on
    m_auto, _ = analysis.recount(metrics, events, acfg=analysis.AnalysisConfig(repeat_rule_share=0.99))
    assert not m_auto["repeats"]["rule"]["active"]                            # below the share: auto stays off
    # verdicts: "defect" on a repeating hit counts it again, "music" on a plain click takes it out
    hit = next(e for e in events if e["region"] == "music" and (e["rep"] or 0) >= repeats.MIN_REPEATS)
    plain = next(e for e in events if e["region"] == "music" and e["kind"] == "click" and (e["rep"] or 0) < repeats.MIN_REPEATS)
    ea = analysis.event_arrays(events)
    counted, raw, why, info = analysis.count_mask(ea["kind"], ea["region"], ea["label"], ea["rep"], ea["t"], ea["strength"],
                                                  analysis.AnalysisConfig())
    i_hit, i_plain = events.index(hit), events.index(plain)
    assert why[i_hit] == "rule" and not counted[i_hit] and counted[i_plain] and why[i_plain] is None
    judged = [dict(e) for e in events]
    judged[i_hit]["label"], judged[i_plain]["label"] = "defect", "music"
    m_j, _ = analysis.recount(metrics, judged)
    assert m_j["counts"]["rule_music"] == metrics["counts"]["rule_music"] - 1
    assert m_j["counts"]["labeled_music"] == 1
    ea = analysis.event_arrays(judged)
    counted, raw, why, info = analysis.count_mask(ea["kind"], ea["region"], ea["label"], ea["rep"], ea["t"], ea["strength"],
                                                  analysis.AnalysisConfig())
    assert counted[i_hit] and why[i_hit] is None and not counted[i_plain] and why[i_plain] == "label"


def test_audible_clicks_group_impulses_within_20_ms():
    """A particle makes the stylus bounce: impulses 2 ms and 10 ms apart are one audible click; the strongest one
    stands for the group, crackle is not grouped, and a verdict on any member speaks for the group."""
    t = np.array([1.000, 1.010, 1.012, 1.025, 1.060, 2.000, 2.015, 3.000, 3.005])
    region = np.array(["music"] * 7 + ["quiet", "quiet"], dtype=object)
    kind = np.array(["click", "click", "click", "click", "click", "tick", "tick", "click", "pop"], dtype=object)
    strength = np.array([30.0, 80.0, 40.0, 35.0, 50.0, 10.0, 12.0, 60.0, 90.0])
    rep_of = analysis.group_impulses(t, region, kind, strength, 20.0)
    assert list(rep_of[:4]) == [1, 1, 1, 1]                 # chained within 20 ms, the strongest (80) represents
    assert rep_of[4] == 4                                   # 35 ms after the last member: on its own
    assert list(rep_of[5:7]) == [5, 6]                      # crackle is never grouped
    assert list(rep_of[7:]) == [8, 8]                       # the pop (90) stands for the click 5 ms before it
    # a group spans at most 2.5 × 20 ms from its first impulse
    tt = np.arange(0, 0.2, 0.015)
    r2 = analysis.group_impulses(tt, np.array(["music"] * len(tt), dtype=object),
                                 np.array(["click"] * len(tt), dtype=object), np.ones(len(tt)), 20.0)
    assert len(set(r2)) >= 3
    label = np.array([None] * 9, dtype=object)
    cfg = analysis.AnalysisConfig(repeat_rule="off")
    counted, raw, why, info = analysis.count_mask(kind, region, label, None, t, strength, cfg)
    assert list(counted) == [False, True, False, False, True, True, True, False, True]
    assert info["grouped"] == 3 and info["grouped_all"] == 4 and list(why[[0, 2, 3, 7]]) == ["group"] * 4
    label[0] = "music"                                      # a verdict on a member applies to the group
    counted, raw, why, info = analysis.count_mask(kind, region, label, None, t, strength, cfg)
    assert not counted[1] and why[1] == "label" and info["labeled_music"] == 1
    label[0], label[2] = None, "defect"
    counted, raw, why, info = analysis.count_mask(kind, region, label, None, t, strength, cfg)
    assert counted[1] and why[1] is None
    counted, raw, why, info = analysis.count_mask(kind, region, label, None, t, strength,
                                                  analysis.AnalysisConfig(repeat_rule="off", click_group_ms=0))
    assert counted.sum() == 9 and info["grouped"] == 0      # grouping off: every impulse counts


def test_filter_can_be_switched_off_and_a_plain_side_has_few_repeats():
    audio, truth = synth.make_side(duration_s=120, seed=7, clicks_per_min=20)
    res = analysis.analyze(audio, SR, analysis.AnalysisConfig(second_opinion=False))
    ev = [e for e in res["events"] if e["region"] == "music" and e["kind"] in ("click", "pop")]
    share = np.mean([(e["rep"] or 0) >= repeats.MIN_REPEATS for e in ev]) if ev else 0.0
    assert share <= 0.1, share                                                 # random clicks in music that does not loop
    off = analysis.analyze(audio, SR, analysis.AnalysisConfig(second_opinion=False, repeat_filter=False))
    assert off["metrics"]["repeats"] == {"available": False, "min_repeats": repeats.MIN_REPEATS,
                                         "rule": {"mode": "auto", "active": False, "share": None, "threshold": 0.2,
                                                  "removed": 0}}
    assert all(e["rep"] is None for e in off["events"])


def test_count_direct_and_revolution_guard():
    audio, hits, _ = _sequenced_side(seed=9, duration_s=90.0, period_s=0.6, clicks_per_min=0)
    rep = repeats.count(audio, SR, hits, np.full(len(hits), 3), rpm=100 / 3)
    assert np.mean(rep >= repeats.MIN_REPEATS) >= 0.8, rep
    # a loop of exactly one revolution with one hit: the repeats sit at multiples of the revolution, which is what
    # a scratch across the grooves looks like, so they do not count
    audio2, hits2, _ = _sequenced_side(seed=9, duration_s=90.0, period_s=1.8, hits_per_loop=1, clicks_per_min=0)
    rep2 = repeats.count(audio2, SR, hits2, np.full(len(hits2), 3), rpm=100 / 3)
    assert np.mean(rep2 >= repeats.MIN_REPEATS) <= 0.3, rep2        # chance matches of the synthetic music remain
    rep3 = repeats.count(audio2, SR, hits2, np.full(len(hits2), 3), rpm=None)      # without the guard they count
    assert np.mean(rep3 >= repeats.MIN_REPEATS) >= 0.8, rep3
    assert repeats.count(audio, SR, np.array([]), np.array([])).size == 0


def test_clicks_outside_the_music_span_count_as_quiet():
    """A click in the lead-in or run-out groove is not part of the music the grade is made of: it moves to the
    quiet passages, for new analyses and for stored ones alike (recount)."""
    t = np.array([2.0, 10.0, 50.0, 95.0, 99.0])
    region = np.array(["music", "music", "music", "music", "quiet"], dtype=object)
    out = analysis.music_region(region, t, (5.0, 90.0))
    assert list(out) == ["quiet", "music", "music", "quiet", "quiet"]
    assert list(analysis.music_region(region, t, None)) == list(region)
    assert list(analysis.music_region(region, t, (None, 90.0))) == ["music", "music", "music", "quiet", "quiet"]
    events = [{"t": tt, "kind": "click", "region": r, "strength": 50.0, "amp_dbfs": -30.0, "ch": 3, "gc": None,
               "label": None, "rep": None} for tt, r in zip(t, region)]
    metrics = {"groove_s": 100.0, "quiet_s": 10.0, "music_start_s": 5.0, "music_end_s": 90.0}
    m, _ = analysis.recount(metrics, events, acfg=analysis.AnalysisConfig(repeat_rule="off", click_group_ms=0))
    assert m["counts"]["music"] == 2 and m["counts"]["quiet"] == 3
    m2, _ = analysis.recount({"groove_s": 100.0, "quiet_s": 10.0}, events,
                             acfg=analysis.AnalysisConfig(repeat_rule="off", click_group_ms=0))
    assert m2["counts"]["music"] == 4 and m2["counts"]["quiet"] == 1
