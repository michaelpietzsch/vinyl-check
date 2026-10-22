"""Signal processing tests against synthetic records with known clicks.

The limits here are minimum requirements, measured with detection threshold 8:
quiet passages 100 % from −35 dBFS, music ~90 % from −25 dBFS, 0–3 false alarms in 2 min of music.
"""

import numpy as np
import pytest
from scipy.linalg import solve_toeplitz

from vinylcheck import analysis, compare, detect, grading, synth

SR = 48000


def test_levinson_matches_toeplitz_solver():
    rng = np.random.default_rng(0)
    x = rng.standard_normal(4096)
    order = 16
    r = np.array([np.dot(x[: len(x) - k], x[k:]) for k in range(order + 1)])
    a = detect.levinson(r[None, :], order)[0]
    ref = solve_toeplitz(r[:order], -r[1:order + 1])
    np.testing.assert_allclose(a[1:], ref, rtol=1e-8, atol=1e-10)


@pytest.fixture(scope="module")
def side():
    return synth.make_side(duration_s=120, seed=2, clicks_per_min=40)


def _hits(ev, truth, tol=0.003):
    tt = np.array([c["t"] for c in truth.clicks])
    hit = np.array([np.any(np.abs(ev["t"] - t) < tol) for t in tt])
    fp = [t for t in ev["t"] if not np.any(np.abs(tt - t) < 0.005)]
    return tt, hit, fp


def test_detection_recall_and_false_alarms(side):
    audio, truth = side
    ev = detect.merge_channels([detect.detect_channel(audio[:, c], SR) for c in range(2)])
    tt, hit, fp = _hits(ev, truth)
    amps = np.array([c["amp_dbfs"] for c in truth.clicks])
    reg = np.array([synth.region_of(truth, t) for t in tt])
    quiet_loud = (reg == "quiet") & (amps >= -35)
    music_loud = (reg == "music") & (amps >= -25)
    assert hit[quiet_loud].mean() == 1.0
    assert hit[music_loud].mean() >= 0.8
    assert len(fp) <= 6, f"{len(fp)} false alarms"
    assert not any(synth.region_of(truth, t) == "quiet" for t in fp)


def test_no_clicks_on_clean_music():
    audio, truth = synth.make_side(duration_s=60, seed=3, clicks_per_min=0, pops=0)
    ev = detect.merge_channels([detect.detect_channel(audio[:, c], SR) for c in range(2)])
    assert len(ev) <= 5


def test_analysis_segments_and_noise(side):
    audio, truth = side
    res = analysis.analyze(audio, SR)
    m = res["metrics"]
    assert m["separable"]
    assert m["quiet_s"] > 8                        # lead-in + pauses + run-out found
    assert abs(m["noise_dbfs"] - (-60)) < 2.0      # noise level of the generator
    assert m["clicks_quiet_per_min"] is not None
    assert res["grading"]["grade"] in grading.GRADES
    assert len(res["overview"]["min"]) > 100


def test_needle_up_is_excluded():
    lay = synth.Layout(no_groove_s=2.0)
    audio, truth = synth.make_side(duration_s=60, seed=4, layout=lay, needle_drop=True,
                                   clicks_per_min=0, pops=0)
    res = analysis.analyze(audio, SR)
    g0, _ = res["overview"]["groove"]
    assert 2.0 <= g0 <= 3.0                         # groove starts after the needle drop (+ margin)
    assert all(e["region"] == "excluded" for e in res["events"] if e["t"] < 2.4)   # needle-drop thump


def test_grading_monotonic():
    good = grading.grade({"clicks_music_per_min": 1, "clicks_quiet_per_min": 1, "ticks_quiet_per_min": 5, "pops_per_min": 0,
                          "noise_rel_db": -60})
    bad = grading.grade({"clicks_music_per_min": 120, "clicks_quiet_per_min": 1, "ticks_quiet_per_min": 5, "pops_per_min": 0,
                         "noise_rel_db": -60})
    assert good["grade"] == "NM" and bad["grade"] == "G"
    assert good["score"] > bad["score"]


def test_compare_before_after():
    rng = np.random.default_rng(7)
    base, _ = synth.make_side(duration_s=150, seed=3, clicks_per_min=0, pops=0)
    scratches = sorted(rng.uniform(1, 149, 25))
    dirt = sorted(rng.uniform(1, 149, 60))
    for t in scratches:
        synth.add_click(base, SR, t, rng.uniform(-35, -15), 3, rng)
    a = base.copy()
    for t in dirt:
        synth.add_click(a, SR, t, rng.uniform(-35, -15), int(rng.choice([1, 2, 3])), rng)
    b = synth.time_warp(base, SR, 1.002, 1.3)
    b += (rng.standard_normal(b.shape) * 10 ** (-72 / 20)).astype(np.float32)
    for t in sorted(rng.uniform(3, 148, 15)):
        synth.add_click(b, SR, t, rng.uniform(-35, -15), 3, rng)

    ra, rb = analysis.analyze(a, SR), analysis.analyze(b, SR)
    res = compare.compare(a, b, SR, ra, rb)
    al, cnt = res["alignment"], res["match"]["counts"]
    assert abs(al["offset_s"] - 1.3) < 0.005
    assert abs(al["speed_ratio"] - 1.002) < 0.0002
    assert 18 <= cnt["persistent"] <= 27
    assert 40 <= cnt["removed"] <= 62
    assert 10 <= cnt["new"] <= 25


def test_spectrogram_tone_and_click(tmp_path):
    """Spectrogram: a tone lands in the right frequency row, a click shows as a bright column."""
    import soundfile as sf

    from vinylcheck import spectro
    t = np.arange(SR * 20) / SR
    rng = np.random.default_rng(0)
    x = np.column_stack([0.1 * np.sin(2 * np.pi * 1000 * t)] * 2).astype(np.float32)
    x += (rng.standard_normal(x.shape) * 10 ** (-75 / 20)).astype(np.float32)   # noise floor like vinyl
    x[SR * 10:SR * 10 + 30] += 0.5
    wav = tmp_path / "sg.wav"
    sf.write(wav, x, SR, subtype="PCM_16")
    r = spectro.compute(wav, cols=200, rows=80)
    assert r["img"].shape == (80, 200) and r["img"].dtype == np.uint8
    edges = np.geomspace(20, 16000, 81)
    assert int(np.argmax(r["img"].mean(axis=1))) == np.searchsorted(edges, 1000) - 1
    assert int(np.argmax(r["img"].mean(axis=0))) == 100
    z = spectro.compute(wav, cols=100, rows=40, t0=9.0, t1=11.0, top_db=r["top_db"])   # zoom, same scale
    assert z["img"].shape == (40, 92) and z["top_db"] == r["top_db"] and z["t0"] == 9.0
    # rescaling without audio = recomputing with another top level (up to rounding), also to the display range
    r6 = spectro.compute(wav, cols=200, rows=80, top_db=r["top_db"] - 6, db_range=spectro.DISPLAY_RANGE)
    d = spectro.rescale(r["img"], r["top_db"], r["top_db"] - 6).astype(int) - r6["img"].astype(int)
    assert np.abs(d).max() <= 2                     # rounding of the 100 dB grid → 80 dB grid
    # energy above fmax must not leak into the top row
    hi = np.column_stack([0.3 * np.sin(2 * np.pi * 19000 * t)] * 2).astype(np.float32)
    sf.write(wav, hi, SR, subtype="PCM_16")
    h = spectro.compute(wav, cols=50, rows=80, top_db=r["top_db"])
    assert h["img"][-1].mean() < 40                 # only the 16-bit noise floor (≈ 5 dB above the bottom), no tone
