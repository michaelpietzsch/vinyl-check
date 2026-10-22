"""Second opinion (GrooveClean network at our candidates) and audit verdicts with recount."""

import numpy as np
import pytest

from vinylcheck import analysis, grading, synth, second_opinion

SR = 48000
needs_torch = pytest.mark.skipif(not second_opinion.available(), reason="PyTorch or weights missing")


def test_segments_merge_overlapping_windows():
    segs = second_opinion.segments(np.array([10.0, 10.1, 10.2, 30.0]), margin_s=0.15)
    assert segs == [(9.85, 10.35), (29.85, 30.15)]
    assert second_opinion.segments(np.zeros(0)) == []
    assert second_opinion.info()["model"].startswith("grooveclean")


@needs_torch
def test_net_says_click_for_clicks_and_not_for_noise():
    audio, truth = synth.make_side(duration_s=40, seed=9, clicks_per_min=30, amp_range=(-30.0, -12.0))
    clicks = np.array([c["t"] for c in truth.clicks if c["kind"] == "click"])
    rng = np.random.default_rng(1)
    quiet = [(a + 0.3, b - 0.3) for a, b in truth.quiet if b - a > 1]
    noise_t = np.array([rng.uniform(a, b) for a, b in quiet for _ in range(4)])
    p_click = second_opinion.opinion(audio, SR, clicks)
    p_noise = second_opinion.opinion(audio, SR, noise_t)
    assert np.mean(p_click >= second_opinion.THRESHOLD) >= 0.8, p_click
    assert np.mean(p_noise < 0.2) >= 0.8, p_noise
    assert second_opinion.opinion(audio, SR, np.zeros(0)).size == 0


@needs_torch
def test_analysis_carries_second_opinion_and_cache():
    audio, _ = synth.make_side(duration_s=40, seed=5, clicks_per_min=30)
    res = analysis.analyze(audio, SR)
    so = res["metrics"]["second_opinion"]
    assert so["available"] and so["confirmed_quiet"] is not None and so["seconds"] >= 0
    strong = [e for e in res["events"] if e["kind"] != "tick"]
    assert strong and all(e["gc"] is not None for e in strong)                  # only clicks and pops go to the network
    assert all(e["gc"] is None for e in res["events"] if e["kind"] == "tick")
    assert so["cached"] == 0 and so["computed"] == len(strong) == len(res["so_new"])
    res2 = analysis.analyze(audio, SR, so_cache=res["so_new"])                   # second run: everything from the cache
    so2 = res2["metrics"]["second_opinion"]
    assert so2["cached"] == len(strong) and so2["computed"] == 0 and res2["so_new"] == {}
    assert [e["gc"] for e in res2["events"]] == [e["gc"] for e in res["events"]]


def test_labels_change_counts_and_survive_reanalysis():
    audio, _ = synth.make_side(duration_s=40, seed=5, clicks_per_min=30)
    cfg = analysis.AnalysisConfig(second_opinion=False)
    res = analysis.analyze(audio, SR, cfg)
    strong = sorted((e for e in res["events"] if e["region"] == "quiet" and e["kind"] != "tick"), key=lambda e: -e["strength"])
    assert len(strong) >= 3
    t1, t2 = strong[0]["t"], strong[1]["t"]
    for e in res["events"]:
        if e["t"] == t1:
            e["label"] = "musik"
    m2, g2 = analysis.recount(res["metrics"], res["events"])
    assert m2["counts"]["labeled_music"] == 1
    assert m2["counts"]["quiet"] == res["metrics"]["counts"]["quiet"] - 1
    assert m2["clicks_quiet_per_min"] < res["metrics"]["clicks_quiet_per_min"]
    assert g2["grade"] in grading.GRADES
    # Re-analysis with stored verdicts: "musik" does not count, "knackser" is attached to the event
    res2 = analysis.analyze(audio, SR, cfg, labels={t1: "musik", t2 + 0.002: "knackser"})
    by_t = {e["t"]: e for e in res2["events"]}
    assert by_t[t1]["label"] == "musik" and by_t[t2]["label"] == "knackser"
    assert res2["metrics"]["counts"]["labeled_music"] == 1
    assert res2["metrics"]["counts"]["quiet"] == res["metrics"]["counts"]["quiet"] - 1
    assert analysis.labels_for(np.array([1.0, 2.0]), {1.0021: "musik"}).tolist() == ["musik", None]
    assert analysis.labels_for(np.array([1.0]), None).tolist() == [None]
