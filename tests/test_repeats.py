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
    assert np.mean(rep_def >= repeats.MIN_REPEATS) <= 0.1, rep_def            # … the defects do not: their
    # surroundings come back with the loop, but the spike is not there
    m = res["metrics"]["repeats"]
    assert m["available"] and m["repeated_music"] >= 0.8 * len(on_hit)
    assert m["clicks_music_unrepeated_per_min"] < res["metrics"]["clicks_music_per_min"]
    assert res["metrics"]["clicks_music_per_min"] > 20                         # the grade still counts them all
    # ticks and clicks outside the music carry no count
    assert all(e["rep"] is None for e in res["events"] if e["kind"] == "tick" or e["region"] != "music")
    # recount from the stored events keeps the figures
    m2, _ = analysis.recount(res["metrics"], res["events"])
    assert m2["repeats"]["repeated_music"] == m["repeated_music"]
    assert m2["repeats"]["clicks_music_unrepeated_per_min"] == m["clicks_music_unrepeated_per_min"]


def test_filter_can_be_switched_off_and_a_plain_side_has_few_repeats():
    audio, truth = synth.make_side(duration_s=120, seed=7, clicks_per_min=20)
    res = analysis.analyze(audio, SR, analysis.AnalysisConfig(second_opinion=False))
    ev = [e for e in res["events"] if e["region"] == "music" and e["kind"] in ("click", "pop")]
    share = np.mean([(e["rep"] or 0) >= repeats.MIN_REPEATS for e in ev]) if ev else 0.0
    assert share <= 0.1, share                                                 # random clicks in music that does not loop
    off = analysis.analyze(audio, SR, analysis.AnalysisConfig(second_opinion=False, repeat_filter=False))
    assert off["metrics"]["repeats"] == {"available": False, "min_repeats": repeats.MIN_REPEATS}
    assert all(e["rep"] is None for e in off["events"])


def test_count_direct_and_revolution_guard():
    audio, hits, _ = _sequenced_side(seed=9, duration_s=90.0, period_s=0.6, clicks_per_min=0)
    rep = repeats.count(audio, SR, hits, np.full(len(hits), 3), rpm=100 / 3)
    assert np.mean(rep >= repeats.MIN_REPEATS) >= 0.8, rep
    # a loop of exactly one revolution with one hit: the repeats sit at multiples of the revolution, which is what
    # a scratch across the grooves looks like, so they do not count
    audio2, hits2, _ = _sequenced_side(seed=9, duration_s=90.0, period_s=1.8, hits_per_loop=1, clicks_per_min=0)
    rep2 = repeats.count(audio2, SR, hits2, np.full(len(hits2), 3), rpm=100 / 3)
    assert np.mean(rep2 >= repeats.MIN_REPEATS) <= 0.2, rep2
    rep3 = repeats.count(audio2, SR, hits2, np.full(len(hits2), 3), rpm=None)      # without the guard they count
    assert np.mean(rep3 >= repeats.MIN_REPEATS) >= 0.8, rep3
    assert repeats.count(audio, SR, np.array([]), np.array([])).size == 0
