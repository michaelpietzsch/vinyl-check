"""Skip detector: skips back, run-out groove, forward skips in the comparison, no false alarms."""

import numpy as np
import pytest

from vinylcheck import analysis, compare, i18n, skips, synth

SR = 48000
RPM = 100 / 3


@pytest.fixture(scope="module")
def base():
    audio, _ = synth.make_side(duration_s=150, seed=11, clicks_per_min=20)
    return audio


def _noise(x, seed):
    return x + (np.random.default_rng(seed).standard_normal(x.shape) * 1e-4).astype(np.float32)


def test_no_false_alarms(base):
    assert skips.find_repeats(base, SR, RPM) == []
    t = np.arange(len(base)) / SR                                     # long sustained notes (pedal point)
    organ = base + 0.05 * np.column_stack([np.sin(2 * np.pi * 220 * t), np.sin(2 * np.pi * 330 * t)]).astype(np.float32)
    # Sustained notes resemble themselves one revolution later: at most "unsicher" (with realistically quiet
    # treble noise, two weak hits at the start), never a certain skip back
    assert [e for e in skips.find_repeats(organ, SR, RPM) if e["kind"] == "rueckwaerts"] == []


def test_back_skips_and_locked_groove(base):
    x = synth.skip_back(base, SR, 50.0)
    x = synth.skip_back(x, SR, 101.3, grooves=2)
    x = _noise(synth.locked_groove(x, SR, repeats=5), 1)
    ev = skips.find_repeats(x, SR, RPM)
    back = [e for e in ev if e["kind"] == "rueckwaerts"]
    assert [e["grooves"] for e in back] == [1, 2]
    assert back[0]["t"] == pytest.approx(50.0, abs=0.4)
    assert back[1]["t"] == pytest.approx(101.3, abs=0.4)
    assert back[0]["period_s"] == pytest.approx(1.8, abs=0.005)
    assert [e["kind"] for e in ev][-1] == "endrille"


def test_back_skip_with_speed_offset(base):
    y = synth.skip_back(synth.time_warp(base, SR, 1.008, 0.0), SR, 80.0, speed=1.008)
    ev = skips.find_repeats(y, SR, RPM)
    assert len(ev) == 1 and ev[0]["rpm_measured"] == pytest.approx(33.6, abs=0.03)


def test_relative_jumps_forward_and_back(base):
    a = _noise(synth.skip_back(base, SR, 110.0), 2)                     # A skips back
    b = synth.time_warp(base, SR, 1.002, 1.3)
    b = _noise(synth.skip_forward(b, SR, 61.3, speed=1.002), 3)          # B skips forward
    sa, sb = skips.find_repeats(a, SR, RPM), skips.find_repeats(b, SR, RPM)
    al = compare.align(a.mean(1), b.mean(1), SR)
    rel = skips.relative_jumps(a, b, SR, al["slope"], al["intercept"], RPM, (0, len(a) / SR), sa, sb)
    assert [j["deutung"] for j in rel] == [i18n.msg("skips.b_skipped_forward"), i18n.msg("skips.a_skipped_back")]
    assert [i18n.render(j["deutung"], "en") for j in rel] == ["B skipped forward", "A skipped back"]
    assert rel[0]["t_a"] == pytest.approx(61, abs=2.5)
    assert rel[1]["t_a"] == pytest.approx(110, abs=2.5)


def test_analysis_reports_skips(base):
    res = analysis.analyze(synth.skip_back(base, SR, 70.0), SR)
    m = res["metrics"]
    assert m["skips_back"] == 1 and 40 < m["skips"][0]["position_pct"] < 55
    warn = [w for w in res["warnings"] if w.get("$t") == "analysis.skip_back"]
    assert warn and i18n.render(warn[0], "en").startswith("Stylus skipped back once (groove repeated): 1:")
    assert analysis.analyze(base, SR)["metrics"]["skips_back"] == 0


def test_run_out_split_by_noise_is_end_groove_not_skip(base):
    """Clicks broke the run-out groove into several sections, and the earlier ones were reported as skips
    back. Behind the end of the music every repetition is the run-out groove."""
    music_end = len(base) / SR
    x = synth.locked_groove(base, SR, repeats=6)
    i = int((music_end + 3 * 1.8) * SR)                                 # 0.9 s of disturbance in the run-out
    x[i:i + int(0.9 * SR)] += (np.random.default_rng(4).standard_normal((int(0.9 * SR), 2)) * 0.3).astype(np.float32)
    ev = skips.find_repeats(_noise(x, 5), SR, RPM, music_end=music_end)
    assert ev and all(e["kind"] == "endrille" for e in ev)


# ─── Live detection of the run-out groove ─────────────────────────────────────
def _run_watch(audio, min_s=20.0, chunk_s=0.1):
    w = skips.EndGrooveWatch(SR, RPM, min_s=min_s)
    n = int(chunk_s * SR)
    for i in range(0, len(audio), n):
        if w.feed(audio[i:i + n].astype(np.float64)):
            return (i + n) / SR
    return None


def test_end_groove_watch_stops_in_run_out_only(base):
    music_end = len(base) / SR
    x = _noise(synth.locked_groove(base, SR, repeats=10), 6)
    t = _run_watch(x)
    assert t is not None and music_end + 2 * 1.8 - 0.5 <= t <= music_end + 6 * 1.8   # 3 equal revolutions, promptly
    assert _run_watch(_noise(base, 7)) is None                                   # no run-out groove, no stop


def test_end_groove_watch_ignores_test_tones_and_silent_groove():
    for side in (1, 2):                                   # continuous tones repeat every revolution, but are loud
        audio, _ = synth.make_test_side(side, synth.Rig(), seed=side)
        assert _run_watch(audio, min_s=5.0) is None
