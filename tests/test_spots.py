"""Where to look on the record: scratches and clicks get a place (share of the side, estimated radius), and a mono
record is analysed as L+R so that vertical noise and damage do not count."""

import numpy as np

from vinylcheck import analysis, skips, synth

SR = 48000


def test_radius_runs_from_the_edge_to_the_end_of_the_music():
    groove, music_end = (10.0, 1300.0), 1200.0
    r = skips.radius_mm([10.0, 605.0, 1200.0, 1290.0], groove, music_end, 100 / 3)
    assert r[0] == skips.LP_OUTER_MM and r[2] == skips.LP_INNER_MM == r[3]       # clipped at the end of the music
    assert r[1] == round((skips.LP_OUTER_MM + skips.LP_INNER_MM) / 2)
    assert skips.radius_mm([10.0, 100.0], groove, music_end, 45.0) == [None, None]   # 7" or 12": unknown at 45
    assert skips.position(605.0, groove, music_end, 100 / 3) == {"position_pct": 50.0, "radius_mm": r[1]}


def test_scratches_carry_their_place():
    audio, truth = synth.make_side(duration_s=90, seed=21, clicks_per_min=0, pops=0)
    a, b = truth.music[0]
    rng = np.random.default_rng(3)
    period = skips.revolution_s(100 / 3)
    for k in range(6):                                                           # a scratch across six grooves
        synth.add_click(audio, SR, a + 20 + k * period, -18.0, 3, rng)
    res = analysis.analyze(audio, SR, analysis.AnalysisConfig(second_opinion=False))
    sc = res["metrics"]["scratches"]
    assert sc and sc[0]["revs"] >= 5, sc
    s = sc[0]
    assert 0 <= s["position_pct"] <= 100 and skips.LP_INNER_MM <= s["radius_mm"] <= skips.LP_OUTER_MM
    assert s["radius_end_mm"] is not None and s["radius_end_mm"] <= s["radius_mm"]   # it runs inwards


def _vertical_click(audio, sr, t, amp_dbfs, rng):
    """A click in the vertical direction only: opposite polarity on the two channels (L−R), nothing in L+R."""
    shape = synth.click_shape(rng, sr, synth._db(amp_dbfs), "click")
    i = int(t * sr)
    audio[i:i + len(shape), 0] += shape
    audio[i:i + len(shape), 1] -= shape


def test_mono_analysis_drops_vertical_clicks_and_keeps_lateral_ones():
    # clicks in the pauses, where the detector sees every one of them (the synthetic music masks clicks now and then)
    audio, truth = synth.make_side(duration_s=60, seed=22, clicks_per_min=0, pops=0)
    pauses = [(a, b) for a, b in truth.quiet if b - a >= 2.5 and a > 1 and b < 59]
    assert len(pauses) >= 2, truth.quiet
    rng = np.random.default_rng(4)
    vertical = [a + 0.5 + 1.2 * k for a, b in pauses for k in range(2)]
    lateral = [a + 1.1 + 1.2 * k for a, b in pauses for k in range(2)]
    for t in vertical:
        _vertical_click(audio, SR, t, -14.0, rng)
    for t in lateral:
        synth.add_click(audio, SR, t, -20.0, 3, rng)
    stereo = analysis.analyze(audio, SR, analysis.AnalysisConfig(second_opinion=False))
    mono = analysis.analyze(audio, SR, analysis.AnalysisConfig(second_opinion=False, mono=True))
    near = lambda res, ts: sum(any(abs(e["t"] - t) < 0.005 for e in res["events"] if e["kind"] != "tick") for t in ts)
    assert near(stereo, vertical) == len(vertical) and near(stereo, lateral) == len(lateral)
    assert near(mono, vertical) == 0, [e["t"] for e in mono["events"]]           # L+R cancels the vertical click
    assert near(mono, lateral) == len(lateral)
    assert all(e["ch"] == 3 for e in mono["events"] if e["kind"] != "tick")        # both channels carry the sum
    assert mono["metrics"]["mono"] is True and stereo["metrics"]["mono"] is False
    assert mono["metrics"]["clicks_quiet_per_min"] < stereo["metrics"]["clicks_quiet_per_min"]
