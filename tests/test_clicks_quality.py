"""False alarms of the click detector on a clean side, and recurring scratches."""

import numpy as np

from vinylcheck import analysis, synth

SR = 48000


def test_clean_side_has_few_false_clicks():
    """As in grooveclean: at most one false alarm per minute on click-free material."""
    audio, _ = synth.make_side(duration_s=120, seed=11, clicks_per_min=0)
    m = analysis.analyze(audio, SR)["metrics"]
    minutes = m["groove_s"] / 60
    assert (m["counts"]["click"] + m["counts"]["pop"]) / minutes <= 1.0, m["counts"]


def test_find_scratches_chains_every_revolution():
    T = 1.8
    times = [10.0, 11.8, 13.6, 15.405, 17.2, 30.0, 31.82, 33.6, 50.0, 50.9, 60.0, 61.8]   # chain 10–17.2, rest random
    sc = analysis.find_scratches(np.array(times), T)
    assert len(sc) == 1 and sc[0]["revs"] == 5 and sc[0]["t"] == 10.0 and sc[0]["until"] == 17.2
    assert analysis.find_scratches(np.array([1.0, 2.0, 3.0]), T) == []
    rng = np.random.default_rng(3)                                             # 2,500 random clicks over 20 min
    rand = np.sort(rng.uniform(0, 1200, 2500))
    assert len(analysis.find_scratches(rand, T)) <= 2
