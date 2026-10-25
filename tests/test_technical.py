"""Technical data: loudness, true peak, DC, correlation, hum."""

import numpy as np
import pytest

from vinylcheck import analysis, i18n, synth, technical

SR = 48000


def _sine(f, dbfs, s=12.0, phase=0.0):
    t = np.arange(int(s * SR)) / SR
    return (10 ** (dbfs / 20)) * np.sin(2 * np.pi * f * t + phase)


def test_loudness_of_stereo_sine_matches_bs1770():
    x = np.column_stack([_sine(997, -20), _sine(997, -20)]).astype(np.float32)
    assert technical.loudness_lufs(x, SR) == pytest.approx(-20.0, abs=0.1)        # standard: 997 Hz, 0 dBFS in one channel = −3.01 LKFS
    mono = np.column_stack([_sine(997, -20), np.zeros(int(12 * SR))]).astype(np.float32)
    assert technical.loudness_lufs(mono, SR) == pytest.approx(-23.01, abs=0.1)
    sos = technical.k_weighting_sos(SR)
    assert np.allclose(sos[0], [1.53512485958697, -2.69169618940638, 1.19839281085285, 1, -1.69065929318241, 0.73248077421585], atol=1e-4)
    assert technical.loudness_lufs(np.zeros((SR, 2), np.float32), SR) is None   # everything below the gate


def test_true_peak_dc_correlation_and_hum():
    n = int(20 * SR)
    rng = np.random.default_rng(1)
    noise = (rng.standard_normal((n, 2)) * 10 ** (-60 / 20)).astype(np.float32)
    x = noise.copy()
    x[:, 0] += _sine(3000, -6, 20); x[:, 1] += _sine(3000, -6, 20)
    x[:, 0] += 0.01                                                               # 1 % DC on the left
    x[: SR * 6, :] += _sine(50, -40, 6)[:, None]                                  # hum in the first 6 s (quiet)
    frames = int(20 / analysis.HOP_S)
    quiet = np.zeros(frames, bool); quiet[: int(6 / analysis.HOP_S)] = True
    music = ~quiet
    t = technical.measure(x, SR, (0.0, 20.0), music, quiet, analysis.HOP_S, -6.0)
    assert t["ok"] and t["true_peak_dbtp"][1] >= t["peak_dbfs"][1] - 0.05
    assert t["true_peak_dbtp"][1] == pytest.approx(-6.0, abs=0.3)
    assert t["dc_pct"][0] == pytest.approx(1.0, abs=0.1) and abs(t["dc_pct"][1]) < 0.05
    assert t["correlation"] == pytest.approx(1.0, abs=0.01) and t["stereo_width_db"] < -40
    assert t["hum_prominence_db"]["50"] > 10 and t["hum_dbfs"] == pytest.approx(-43, abs=1.5)   # −40 dB amplitude = −43 dB power
    assert t["balance_db"]["presence"] > t["balance_db"]["bass"]
    inv = x.copy(); inv[:, 1] = -inv[:, 1]
    assert technical.measure(inv, SR, (0.0, 20.0), music, quiet, analysis.HOP_S, -6.0)["correlation"] == pytest.approx(-1.0, abs=0.01)


def test_short_groove_is_not_measured():
    x = np.zeros((5 * SR, 2), np.float32)
    frames = np.zeros(int(5 / analysis.HOP_S), bool)
    t = technical.measure(x, SR, (0.0, 5.0), frames, frames, analysis.HOP_S, None)
    assert not t["ok"] and i18n.render(t["reason"], "en") == "Groove shorter than 10 s"
    assert i18n.render(t["reason"], "de") == "Rille kürzer als 10 s"


def test_analyze_includes_technical_block():
    audio, _ = synth.make_side(duration_s=40, seed=4, clicks_per_min=10)
    m = analysis.analyze(audio, SR)["metrics"]
    t = m["technical"]
    assert t["ok"] and t["loudness_lufs"] is not None and t["music_blocks_s"] > 5
    assert -1 <= t["correlation"] <= 1 and set(t["balance_db"]) == {"bass", "low_mid", "mid", "presence", "treble"}
