"""Mechanical shocks (needle drop, dust cover): the arm resonance rings after the impulse, a click has no tail."""

import numpy as np

from vinylcheck import analysis, detect, shock, synth

SR = 48000


def _ring(audio, sr, t, amp_dbfs=-22.0, f_hz=10.0, decay_s=0.12, vertical=False):
    """A shock at t: a short impulse followed by a damped 10 Hz oscillation (the tonearm resonance)."""
    i = int(t * sr)
    n = int(0.5 * sr)
    tt = np.arange(n) / sr
    ring = synth._db(amp_dbfs) * np.exp(-tt / decay_s) * np.sin(2 * np.pi * f_hz * tt)
    sign = np.array([1.0, -1.0]) if vertical else np.array([1.0, 1.0])
    audio[i:i + n, :] += ring[:, None] * sign[None, :]
    synth.add_click(audio, sr, t, amp_dbfs + 4, 3, np.random.default_rng(int(t * 100)))   # the impulse itself


def test_measure_separates_a_shock_from_a_click():
    audio, truth = synth.make_side(duration_s=60, seed=11, clicks_per_min=0, pops=0)
    a, b = truth.music[0]
    t_shock, t_click, t_vert = a + 5.0, a + 15.0, a + 25.0
    # the synthetic music carries more energy below 25 Hz than a record (cut with a rumble filter), so the lateral
    # shock has to be loud; a real shock through the plinth moves the arm mostly vertically, where music is quiet
    _ring(audio, SR, t_shock, amp_dbfs=-12.0)
    _ring(audio, SR, t_vert, vertical=True)
    rng = np.random.default_rng(1)
    synth.add_click(audio, SR, t_click, -20.0, 3, rng)
    g = shock.measure(audio, SR, np.array([t_shock, t_click, t_vert]))
    assert g[0] >= shock.RATIO_DB and g[2] >= shock.RATIO_DB, g       # lateral and vertical ringing both count
    assert g[1] < shock.RATIO_DB / 2, g                                # a click has no low-frequency tail
    assert list(shock.is_shock([g[0], g[1], None, float("nan")])) == [True, False, False, False]
    assert shock.measure(audio, SR, np.array([])).size == 0


def test_a_shock_is_not_counted_and_a_verdict_click_counts_it_again():
    base, truth = synth.make_side(duration_s=90, seed=12, clicks_per_min=6, pops=0)
    a, b = truth.music[0]
    # a spot where the detector sees the impulse (the synthetic music masks a click now and then); the shock through
    # the plinth moves the arm vertically, as it does on a real turntable
    for off in np.arange(5.0, 20.0, 0.37):
        audio, t_shock = base.copy(), a + float(off)
        _ring(audio, SR, t_shock, vertical=True)
        ev0 = detect.merge_channels([detect.detect_channel(audio[:, c], SR) for c in range(2)], 1.5)
        if any(abs(e["t"] - t_shock) < 0.004 and e["strength"] >= 30 for e in ev0):
            break
    else:
        raise AssertionError("no spot where the impulse is detected")
    res = analysis.analyze(audio, SR, analysis.AnalysisConfig(second_opinion=False))
    ev = res["events"]
    hit = [e for e in ev if abs(e["t"] - t_shock) < 0.01 and e["kind"] in ("click", "pop")]
    assert hit and hit[0]["shock"] is not None and hit[0]["shock"] >= shock.RATIO_DB, hit
    assert res["metrics"]["counts"]["shocks"] >= 1
    plain = [e for e in ev if e["kind"] in ("click", "pop") and e["region"] == "music" and abs(e["t"] - t_shock) > 1.0]
    assert plain and all((e["shock"] or 0) < shock.RATIO_DB for e in plain), [e["shock"] for e in plain][:10]
    ea = analysis.event_arrays(ev)
    counted, raw, why, info = analysis.count_mask(ea["kind"], ea["region"], ea["label"], ea["rep"], ea["t"],
                                                  ea["strength"], analysis.AnalysisConfig(), shk=ea["shock"])
    i = ev.index(hit[0])
    assert not counted[i] and why[i] == "shock" and info["shocks"] >= 1
    judged = [dict(e) for e in ev]
    judged[i]["label"] = "defect"
    m, _ = analysis.recount(res["metrics"], judged)
    assert m["counts"]["shocks"] == res["metrics"]["counts"]["shocks"] - 1
    off = analysis.analyze(audio, SR, analysis.AnalysisConfig(second_opinion=False, shock_filter=False))
    assert all(e["shock"] is None for e in off["events"]) and off["metrics"]["counts"]["shocks"] == 0
