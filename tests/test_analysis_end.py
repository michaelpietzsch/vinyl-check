"""End of the music as a fallback for skip detection, and clipping at needle drop."""

import numpy as np

from vinylcheck import analysis, i18n, synth

SR = 48000
GAIN_KEYS = {"analysis.level_too_low", "analysis.level_too_high", "analysis.clip_reduce_gain"}


def _keys(msgs) -> list[str]:
    """Message keys of a list of warnings, including the parts of joined messages."""
    out = []
    for w in msgs:
        out += _keys(w["$join"]) if "$join" in w else [w["$t"]]
    return out


def test_music_end_finds_the_fade_out():
    hop = analysis.HOP_S
    n = int(100 / hop)
    lv = np.full(n, -25.0)
    lv[int(60 / hop):int(70 / hop)] = np.linspace(-25, -45, int(10 / hop))   # fade-out
    lv[int(70 / hop):int(80 / hop)] = -46.0                                   # silence
    lv[int(80 / hop):] = -35.0                                                # run-out with noise, louder than "quiet"
    quiet = lv <= -44
    end = analysis._music_end(lv, hop, n, -25.0, quiet)
    assert 69 <= end <= 70.5                                                  # where the fade-out ends (70 s)
    lv2 = lv.copy(); lv2[int(85 / hop):int(95 / hop)] = -25.0                 # another track follows
    assert analysis._music_end(lv2, hop, n, -25.0, lv2 <= -44) is None
    lv3 = lv.copy(); lv3[int(90 / hop):int(95 / hop)] = -46.0                 # quiet stretch in the run-out groove
    assert 69 <= analysis._music_end(lv3, hop, n, -25.0, lv3 <= -44) <= 70.5   # still the first pause
    assert analysis._music_end(np.full(n, -25.0), hop, n, -25.0, np.zeros(n, bool)) is None
    assert analysis._music_end(lv, hop, 0, -25.0, quiet) is None


def test_clips_at_needle_drop_do_not_spoil_the_recording():
    audio, _ = synth.make_side(duration_s=40, seed=3, clicks_per_min=10)
    g0 = analysis.analyze(audio, SR)["overview"]["groove"][0]
    a = audio.copy()
    i = int((g0 + 0.3) * SR)
    a[i:i + 8, 0] = 1.0                                                       # needle drop
    res = analysis.analyze(a, SR)
    m = res["metrics"]
    assert m["clip_samples"] == 8 and m["clip_in_groove"] == 0 and m["clip_first_s"] is None
    keys = _keys(res["warnings"])
    assert "analysis.clip_at_needle_drop" in keys and "analysis.clip_unusable" not in keys
    b = audio.copy()
    j = int((g0 + 10) * SR)
    b[j:j + 3, 1] = -1.0                                                      # in the middle of the groove, but tiny
    res = analysis.analyze(b, SR)
    m = res["metrics"]
    assert m["clip_in_groove"] == 3 and abs(m["clip_first_s"] - (g0 + 10)) < 0.01
    assert not any(k.startswith("analysis.clip_") for k in _keys(res["warnings"]))   # 3 samples: not worth a mention
    assert res["grading"]["grade"] is not None
    c = audio.copy()
    c[j:j + 700, 1] = -1.0                                                    # 700 samples: unusable, no grade
    res = analysis.analyze(c, SR)
    assert res["metrics"]["clip_in_groove"] == 700 and res["grading"]["grade"] is None
    assert any({"analysis.clip_in_music", "analysis.clip_unusable"} <= set(_keys([w])) for w in res["warnings"])
    text = " ".join(i18n.render(w, "en") for w in res["warnings"])
    assert "700 clipped samples in the music" in text and "not suitable for comparison" in text
    d = audio.copy()
    d[int((g0 + 2.0) * SR):int((g0 + 2.0) * SR) + 700, 0] = 1.0              # within the grace period after needle drop
    assert analysis.analyze(d, SR)["metrics"]["clip_in_groove"] == 0
    short = analysis.AnalysisConfig(second_opinion=False, clip_grace_s=1.0, clip_mention=10)   # settings apply
    res = analysis.analyze(d, SR, short)
    assert res["metrics"]["clip_in_groove"] == 700 and "analysis.clip_in_music" in _keys(res["warnings"])


def test_fixed_gain_suppresses_gain_hints():
    audio, _ = synth.make_side(duration_s=40, seed=5, clicks_per_min=30, music_dbfs=-45.0, needle_drop=True)
    cfg = analysis.AnalysisConfig(second_opinion=False)
    loud = _keys(analysis.analyze(audio, SR, cfg, ref_dbfs=-13.0)["warnings"])
    assert GAIN_KEYS & set(loud)
    quiet = _keys(analysis.analyze(audio, SR, cfg, ref_dbfs=-13.0, fixed_gain=True)["warnings"])
    assert not (GAIN_KEYS | {"analysis.clip_at_needle_drop"}) & set(quiet)


def test_level_far_below_profile_reference_is_flagged():
    audio, _ = synth.make_side(duration_s=40, seed=6, clicks_per_min=10)
    ok = analysis.analyze(audio, SR, ref_dbfs=-18.0)
    assert ok["metrics"]["level_vs_profile_db"] is not None and "analysis.level_too_low" not in _keys(ok["warnings"])
    quiet = (audio * 10 ** (-20 / 20)).astype(np.float32)                        # recorded 20 dB too quietly
    res = analysis.analyze(quiet, SR, ref_dbfs=-18.0)
    assert res["metrics"]["level_vs_profile_db"] < -14
    low = [w for w in res["warnings"] if w.get("$t") == "analysis.level_too_low"]
    assert low and "not comparable" in i18n.render(low[0], "en")
    with i18n.use_lang("de"):
        assert "leiser als beim Einmessen" in i18n.render(low[0])                # German text, decimal comma
        assert "," in i18n.render(low[0]).split("Musik ")[1].split(" dBFS")[0]
    assert analysis.analyze(audio, SR)["metrics"]["level_vs_profile_db"] is None  # no statement without a profile
