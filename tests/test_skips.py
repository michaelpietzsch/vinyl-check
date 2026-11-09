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
    # Sustained notes resemble themselves one revolution later: at most "uncertain" (with realistically quiet
    # treble noise, two weak hits at the start), never a certain skip back
    assert [e for e in skips.find_repeats(organ, SR, RPM) if e["kind"] == "backward"] == []


def test_back_skips_and_locked_groove(base):
    x = synth.skip_back(base, SR, 50.0)
    x = synth.skip_back(x, SR, 101.3, grooves=2)
    x = _noise(synth.locked_groove(x, SR, repeats=5), 1)
    ev = skips.find_repeats(x, SR, RPM)
    back = [e for e in ev if e["kind"] == "backward"]
    assert [e["grooves"] for e in back] == [1, 2]
    assert back[0]["t"] == pytest.approx(50.0, abs=0.4)
    assert back[1]["t"] == pytest.approx(101.3, abs=0.4)
    assert back[0]["period_s"] == pytest.approx(1.8, abs=0.005)
    assert [e["kind"] for e in ev][-1] == "run_out"


def _scratch(x, t0, revs, offsets=(0.0, 0.12, 0.3), amp=0.8):
    """A scratch across the grooves: the same clicks once per revolution, here three hairlines side by side."""
    out = x.copy()
    k = np.arange(int(0.0015 * SR)) - int(0.0003 * SR)
    pulse = (-k / (0.00008 * SR) * np.exp(-0.5 * (k / (0.00008 * SR)) ** 2)).astype(np.float32)
    for r in range(revs):
        for o in offsets:
            i = int(round((t0 + r * 60 / RPM + o) * SR))
            out[i:i + len(pulse)] += amp * pulse[:, None]
    return out


def test_a_scratch_is_not_a_skip(base):
    """A scratch clicks once per revolution, at exactly the period of a skip back, and its clicks match each other
    in every frequency: the phase measure alone reports a skip. The waveform between the clicks is other music."""
    x = _scratch(base, 60.0, 8)
    assert "backward" in [e["kind"] for e in skips.find_repeats(x, SR, RPM, waveform=False)]
    assert skips.find_repeats(x, SR, RPM) == []
    # a skip back within the scratched passage is still one, and only one
    back = [e for e in skips.find_repeats(synth.skip_back(x, SR, 66.0), SR, RPM) if e["kind"] == "backward"]
    assert len(back) == 1 and back[0]["same"] >= skips.MIN_SAME
    assert back[0]["t"] <= 66.5 and back[0]["until"] >= 67.0


def test_an_echo_of_the_neighbouring_groove_is_not_a_skip(base):
    """A quiet passage after a loud one carries the revolution before as an echo, far below its level."""
    n = int(round(60 / RPM * SR))
    x = base.copy()
    i = int(70 * SR)
    x[i:i + 3 * n] *= 0.003                                             # three quiet revolutions …
    x[i:i + n] += 0.03 * base[i - n:i]                                  # … the first with the echo, 30 dB down
    assert "backward" in [e["kind"] for e in skips.find_repeats(x, SR, RPM, waveform=False)]
    assert skips.find_repeats(x, SR, RPM) == []


def test_stored_hits_are_judged_again(base, tmp_path):
    """Hits found before the waveform check: the excerpt around each decides whether it stays."""
    import soundfile as sf
    x = synth.skip_back(_scratch(base, 90.0, 8), SR, 50.0)             # a real skip at 50 s, a scratch from 91.8 s
    old = skips.find_repeats(x, SR, RPM, waveform=False)
    assert [e["kind"] for e in old].count("backward") >= 2
    a = int((50.0 - 4.5) * SR)
    j = skips.rejudge(x[a:a + 8 * SR], SR, 4.5, 6.5)
    assert j["kind"] == "backward" and j["same"] >= skips.MIN_SAME and j["period_s"] == pytest.approx(1.8, abs=0.005)
    wav = tmp_path / "side.wav"
    sf.write(wav, x, SR, subtype="PCM_16")
    m = {"rpm": RPM, "skips": [dict(e, kind_auto=e["kind"], label=None) for e in old]
         + [{"t": 148.0, "until": 151.0, "kind": "run_out", "grooves": 1}]}
    assert analysis.recheck_skips(wav, m) is True and m["skips_v"] == skips.VERSION
    assert [(e["kind_auto"], round(e["t"])) for e in m["skips"] if e["kind"] != "run_out"] == [("backward", 50)]
    assert m["skips"][-1]["kind"] == "run_out"                          # the run-out groove is left alone
    assert analysis.recheck_skips(wav, m) is False                      # nothing left to change


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
    assert [j["interpretation"] for j in rel] == [i18n.msg("skips.b_skipped_forward"), i18n.msg("skips.a_skipped_back")]
    assert [i18n.render(j["interpretation"], "en") for j in rel] == ["B skipped forward", "A skipped back"]
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
    assert ev and all(e["kind"] == "run_out" for e in ev)


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


def test_old_analyses_are_rechecked_at_startup(base, tmp_path):
    """An analysis stored before the waveform check lost its grade to a scratch: the next start judges its hits
    again on the audio, and the side gets the grade of its clicks."""
    import json
    import time
    from dataclasses import replace

    import soundfile as sf
    from fastapi.testclient import TestClient

    from vinylcheck.config import Config
    from vinylcheck.db import DB
    from vinylcheck.web import create_app

    cfg = replace(Config(), data_dir=tmp_path / "data", state_dir=tmp_path / "state", min_free_gb=0.0)
    cfg.state_dir.mkdir(parents=True)
    x = _scratch(base, 60.0, 8)
    wav = tmp_path / "side.wav"
    sf.write(wav, x, SR, subtype="PCM_16")
    res = analysis.analyze(x, SR)
    assert res["metrics"]["skips_back"] == 0 and res["metrics"]["skips_v"] == skips.VERSION
    old = [dict(e, kind_auto=e["kind"], label=None) for e in skips.find_repeats(x, SR, RPM, waveform=False)]
    m = dict(res["metrics"], skips=old, skips_back=sum(e["kind"] == "backward" for e in old))
    del m["skips_v"]
    db = DB(cfg.db_path)
    rec = db.add_record("Artist", "Title")
    rid = db.add_recording(record_id=rec, kind="before", side="A", path=str(wav), status="analyzed")
    db.save_analysis(rid, {**res, "metrics": m, "grading": {**res["grading"], "grade": "F"}}, "1.5.1", 1.0)
    gone = db.add_recording(record_id=rec, kind="before", side="B", path=str(tmp_path / "gone.wav"), status="analyzed")
    db.save_analysis(gone, {**res, "metrics": m, "grading": {**res["grading"], "grade": "F"}}, "1.5.1", 1.0)
    db.conn.close()
    with TestClient(create_app(cfg)) as c:
        deadline = time.time() + 30
        while time.time() < deadline:
            a = c.get(f"/api/recordings/{rid}").json()["analysis"]
            if a["metrics"].get("skips_v") == skips.VERSION:
                break
            time.sleep(0.1)
        assert a["metrics"]["skips_v"] == skips.VERSION and a["metrics"]["skips_back"] == 0 and a["metrics"]["skips"] == []
        assert a["grading"]["grade"] == res["grading"]["grade"] != "F"
        assert not [w for w in a["warnings"] if json.dumps(w).count("skip_back")]
        b = c.get(f"/api/recordings/{gone}").json()["analysis"]          # audio gone: its hits stay as they were
        assert "skips_v" not in b["metrics"] and b["metrics"]["skips_back"] >= 1
