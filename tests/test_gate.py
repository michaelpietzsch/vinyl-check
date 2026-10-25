"""Stripe check of the detector: false alarms on material without clicks, sensitivity, clipped peaks (as in
grooveclean), turntable baseline in the grade and in the alignment summary."""

from dataclasses import replace

import numpy as np
import pytest

from vinylcheck import analysis, detect, grading, alignment, i18n, synth

SR = 48000


def _events_per_min(x: np.ndarray, p: detect.DetectParams = detect.DetectParams()) -> float:
    ev = detect.detect_channel(x.astype(np.float32), SR, p)
    return len(ev) / (len(x) / SR / 60)


def _en(m) -> str:
    return i18n.render(m, "en") or ""


def _hint_keys(g: dict) -> list[str]:
    """Message keys of the grading explanation."""
    h = g.get("explanation")
    if not h:
        return []
    return [p["$t"] for p in h["$join"]] if "$join" in h else [h["$t"]]


def test_pink_noise_and_tone_bands_give_almost_no_clicks():
    """Pink noise and pure test tones of the test record: every hit is a false alarm (as in grooveclean: at most
    one false alarm per minute on clean material)."""
    rng = np.random.default_rng(7)
    n = 60 * SR
    pink = synth._pink(n, rng) * 10 ** (-20 / 20)
    t = np.arange(n) / SR
    noise = rng.standard_normal(n) * 10 ** (-60 / 20)
    tone = np.sin(2 * np.pi * 1000 * t) * 10 ** (-12 / 20) + noise
    tone10k = np.sin(2 * np.pi * 10000 * t) * 10 ** (-32 / 20) + noise
    assert _events_per_min(pink) <= 1.0
    assert _events_per_min(tone) <= 1.0
    assert _events_per_min(tone10k) <= 1.0


def test_stripe_check_keeps_synthetic_clicks():
    """The stripe check must not lower the hit rate on synthetic clicks: what the AR detector finds without the
    check it also finds with it (the four clicks missed with this seed lie in loud music and are missing in both
    cases)."""
    audio, truth = synth.make_side(duration_s=60, seed=3, clicks_per_min=40, amp_range=(-40.0, -15.0))

    def recall(p):
        ev = detect.merge_channels([detect.detect_channel(audio[:, c], SR, p) for c in range(2)])
        found = np.sort(ev["t"])
        hit = {c["t"]: np.min(np.abs(found - c["t"])) <= 0.003 for c in truth.clicks}
        quiet_loud = [hit[c["t"]] for c in truth.clicks if synth.region_of(truth, c["t"]) == "quiet" and c["amp_dbfs"] >= -35]
        assert all(quiet_loud), "every click from −35 dBFS in a quiet groove must be found"
        assert all(hit[c["t"]] for c in truth.clicks if c["kind"] == "pop"), "pops must be found"
        return np.mean([hit[c["t"]] for c in truth.clicks if c["kind"] == "click"]), len(ev)

    on, n_on = recall(detect.DetectParams())
    off, n_off = recall(detect.DetectParams(stripe_db=0.0))
    assert on >= 0.8 and on >= off - 0.1, (on, off)      # losses only for clicks deep below the music (−35 … −40 dBFS)
    assert n_on <= n_off                                                            # the check only removes


def test_clipped_runs_are_not_clicks():
    rng = np.random.default_rng(1)
    x = rng.standard_normal(20 * SR) * 10 ** (-50 / 20)
    sq = np.sin(2 * np.pi * 3 * np.arange(len(x)) / SR) * 4.0                  # overdriven sine: flat runs at the peak
    x[5 * SR:15 * SR] = np.clip(x[5 * SR:15 * SR] + sq[5 * SR:15 * SR], -0.999, 0.999)
    assert detect.clipped(x[5 * SR:15 * SR]).any()
    assert _events_per_min(x) <= 1.0


def test_params_for_sensitivity_is_monotonic():
    lo, mid, hi = (detect.params_for(s) for s in (0.0, 0.5, 1.0))
    assert mid == detect.DetectParams()
    assert lo.threshold > mid.threshold > hi.threshold and lo.stripe_db > mid.stripe_db > hi.stripe_db
    assert (lo.stripe_db, mid.stripe_db, hi.stripe_db) == (16.0, 12.0, 8.0)
    assert detect.params_for(None) == mid and detect.params_for(7.0).threshold == hi.threshold
    assert detect.params_for(0.0, detect.DetectParams(stripe_db=0.0)).stripe_db == 0.0        # off stays off


def test_stripe_mask_thin_click_yes_broad_burst_no():
    """A click is a thin treble stripe; a 25 ms noise burst (cymbal, snare) is wide and fails the check."""
    rng = np.random.default_rng(2)
    x = rng.standard_normal(4 * SR) * 10 ** (-60 / 20)
    x[SR] += 0.3                                                     # click
    burst = slice(3 * SR, 3 * SR + int(0.025 * SR))
    x[burst] += rng.standard_normal(int(0.025 * SR)) * 10 ** (-20 / 20)  # wide burst, +40 dB
    env, hop = detect.hf_envelope(x, SR)
    idx = np.array([SR, 2 * SR, 3 * SR + int(0.012 * SR)])
    m = detect.stripe_mask(env, hop, SR, idx, 12.0, 5.0)
    assert m.tolist() == [True, False, False]
    assert detect.stripe_mask(env, hop, SR, np.zeros(0, dtype=np.int64), 12.0, 5.0).size == 0


# Limits up to grading scheme 4: the structural tests (pauses information only, weighting) stay valid with them
OLD = grading.GradingConfig(clicks_music_per_min=(3, 10, 20, 40, 80), pops_per_min=(0.2, 0.6, 2.0, 5.0, 12.0))


def test_grade_comes_from_music_pauses_only_info():
    """Grading scheme 3: only the music determines the grade, pauses are information (scheme 2, at most one step
    lower, can be enabled in the configuration). Turntable baseline as before."""
    m = {"clicks_music_per_min": 2.0, "clicks_quiet_per_min": 700.0, "ticks_quiet_per_min": 60.0, "pops_per_min": 0.05,
         "noise_rel_db": -50.0}
    raw = grading.grade(m, OLD)
    assert raw["per_metric"]["clicks_music_per_min"]["grade"] == "NM" and raw["per_metric"]["clicks_quiet_per_min"]["grade"] == "F"
    assert raw["grade"] == "NM" and "grading.secondary_penalty" not in _hint_keys(raw)      # F in the pauses: ignored
    q = raw["per_metric"]["clicks_quiet_per_min"]
    assert not q["secondary"] and not q["in_grade"] and "(info only" in _en(q["label"])
    old = grading.grade(m, replace(OLD, secondary=("clicks_quiet_per_min", "ticks_quiet_per_min")))
    assert old["grade"] == "VG+" and "grading.secondary_penalty" in _hint_keys(old)          # scheme 2 can be enabled
    assert "which costs one step" in _en(old["explanation"])
    net = grading.grade(m, OLD, floor={"clicks_per_min": 690.0, "ticks_per_min": 50.0})
    assert net["grade"] == "NM"                     # above baseline 10/min = VG+, only one step worse: no penalty
    assert grading.grade(m | {"skips_back": 2}, OLD)["grade"] == "F"                            # F only for a skip back
    assert grading.grade(m | {"clip_in_groove": 4000}, OLD)["grade"] is None                    # clipped: no grade
    assert grading.grade(m | {"clip_in_groove": 43}, OLD)["grade"] == "NM"                       # a few samples: fine
    so = grading.grade(m | {"clicks_music_per_min": 30.0, "second_opinion": {"clicks_music_confirmed_per_min": 25.0}}, OLD)
    p = so["per_metric"]["clicks_music_per_min"]
    assert p["value"] == 25.0 and p["raw"] == 30.0 and p["grade"] == "G+"                  # the confirmed value counts
    assert "(confirmed)" in _en(p["label"])
    assert so["grade"] == "G+"                                                              # pauses F: no penalty
    assert grading.grade(m | {"clicks_music_per_min": 500.0}, OLD)["grade"] == "G"              # F without a skip → G
    noisy = grading.grade(m | {"noise_rel_db": -20.0}, OLD, floor={"clicks_per_min": 690.0, "ticks_per_min": 50.0})
    assert noisy["grade"] == "NM" and noisy["per_metric"]["noise_rel_db"]["grade"] == "F"        # noise is info only
    assert not noisy["per_metric"]["noise_rel_db"]["in_grade"] and noisy["per_metric"]["clicks_music_per_min"]["in_grade"]
    assert "grading.not_in_grade" in _hint_keys(noisy) and "Not part of the grade: " in _en(noisy["explanation"])
    strict = grading.grade(m | {"noise_rel_db": -20.0}, replace(OLD, graded=grading.GradingConfig().metrics, weights=()),
                           floor={"clicks_per_min": 690.0, "ticks_per_min": 50.0})
    assert strict["grade"] == "G" and strict["per_metric"]["noise_rel_db"]["grade"] == "F"       # all graded: worst
    pm = net["per_metric"]["clicks_quiet_per_min"]
    assert pm["value"] == 10.0 and pm["raw"] == 700.0 and pm["floor"] == 690.0
    assert "above turntable baseline" in _en(pm["label"])
    assert "grading.floor_applied" in _hint_keys(net) and raw["per_metric"]["clicks_quiet_per_min"]["raw"] is None
    assert "(690.0 clicks, 50.0 crackle per minute" in _en(net["explanation"])
    assert "(690,0 Clicks, 50,0 Knistern je Minute" in i18n.render(net["explanation"], "de")          # German decimal comma
    below = grading.grade(m, OLD, floor={"clicks_per_min": 900.0, "ticks_per_min": 90.0})
    assert below["per_metric"]["clicks_quiet_per_min"]["value"] == 0.0                # never negative


def test_grade_without_metrics_or_with_clipping_explains_why():
    none = grading.grade({})
    assert none["grade"] is None and none["explanation"] == i18n.msg("grading.no_metrics")
    clipped = grading.grade({"clicks_music_per_min": 2.0, "pops_per_min": 0.0, "clip_in_groove": 4000})
    assert clipped["explanation"] == i18n.msg("grading.clipped", n=4000)
    assert _en(clipped["explanation"]).startswith("No grade: 4000 clipped samples")


def test_analyze_reports_net_values_and_floor():
    audio, _ = synth.make_side(duration_s=40, seed=5, clicks_per_min=30)
    res = analysis.analyze(audio, SR, floor={"clicks_per_min": 5.0, "ticks_per_min": 5.0})
    m = res["metrics"]
    assert m["floor"]["clicks_per_min"] == 5.0 and m["detect"]["stripe_db"] == 12.0
    assert m["clicks_quiet_net_per_min"] == pytest.approx(max(0.0, m["clicks_quiet_per_min"] - 5.0), abs=0.01)
    assert "above turntable baseline" in _en(res["grading"]["per_metric"]["clicks_quiet_per_min"]["label"])
    plain = analysis.analyze(audio, SR)["metrics"]
    assert plain["floor"] is None and plain["clicks_quiet_net_per_min"] is None


def test_silent_groove_click_floor_in_alignment_summary():
    audio, _ = synth.make_test_side(2, seed=4)
    res = alignment.analyze(audio, SR)
    s = res["metrics"]["summary"]
    assert s.get("floor_clicks_per_min") is not None and s["floor_clicks_per_min"] >= 0 and s["floor_ticks_per_min"] >= 0
    it = next(i for i in res["metrics"]["results"] if i["key"] == "floor_clicks_per_min")
    # German wording of alignment.py (rendered if it is a message, passed through if it is still a string)
    assert it["status"] == "info" and "stillen Rille" in i18n.render(it["text"], "de")
    sil = next(b for b in res["metrics"]["bands"] if b.get("role") == "silent_groove")
    assert sil["m"]["clicks"]["ok"] and sil["m"]["clicks"]["stripe_db"] == 12.0


def test_grade_weighted_90_clicks_10_pops():
    """Grading scheme 4: 90 % clicks in the music, 10 % pops. A real test pressing side with clicks at VG+ and pops
    at G+ grades VG+ (the worst single grade would be G+); without weights as up to scheme 3."""
    m = {"clicks_music_per_min": 6.91, "pops_per_min": 3.93, "clicks_quiet_per_min": 0.0, "ticks_quiet_per_min": 0.0}
    w = grading.grade(m, OLD)
    assert w["per_metric"]["clicks_music_per_min"]["grade"] == "VG+" and w["per_metric"]["pops_per_min"]["grade"] == "G+"
    hint = _en(w["explanation"])
    assert w["grade"] == "VG+" and "grading.weighted" in _hint_keys(w)
    assert "Weighted: Clicks/min in the music 90%, Pops/min in the music 10% (the worst single grade would be G+)" \
        in hint
    assert "Gewichtet: Clicks/min in der Musik 90 %, Pops/min in der Musik 10 % (schlechteste Einzelnote wäre G+)" \
        in i18n.render(w["explanation"], "de")
    assert w["per_metric"]["clicks_music_per_min"]["weight"] == 0.9 and w["per_metric"]["pops_per_min"]["weight"] == 0.1
    s_c, s_p = w["per_metric"]["clicks_music_per_min"]["score"], w["per_metric"]["pops_per_min"]["score"]
    assert abs(w["score"] - (0.9 * s_c + 0.1 * s_p)) < 0.06
    assert grading.grade(m, replace(OLD, weights=()))["grade"] == "G+"            # worst single grade
    assert grading.grade(m | {"skips_back": 1}, OLD)["grade"] == "F"                            # a skip stays F
    bad = grading.grade({"clicks_music_per_min": 500.0, "pops_per_min": 50.0}, OLD)
    assert bad["grade"] == "G"                                                              # never F without a skip
    assert "grading.f_without_skip" in _hint_keys(bad)


def test_schema_5_dust_accepted_anchors():
    """Grading scheme 5 (dust accepted, a clean record should reach NM): anchors from real measurements."""
    g = lambda c, p: grading.grade({"clicks_music_per_min": c, "pops_per_min": p})["grade"]
    for c, p in ((3.0, 1.23), (6.67, 3.92), (3.5, 1.59), (5.8, 3.24), (2.67, 0.4)):   # test pressings, fine by ear
        assert g(c, p) == "NM", (c, p)
    assert g(7.4, 4.37) == "VG+"                   # one test pressing side: 89.6 points, just below NM
    assert g(5.7, 0.0) == "NM"                     # a clean record
    assert g(34.81, 0.11) == "G+" and g(24.9, 0.06) == "VG"   # a worn record after washing, sides A and B
    assert g(36.87, 0.28) == "G+"                  # a scratched copy
    assert grading.VERSION >= 5
