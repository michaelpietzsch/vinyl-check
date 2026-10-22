"""Alignment analysis against the simulated test record (AAPT 1) with known turntable faults.

Each test builds a turntable with deliberate faults (synth.Rig) and checks that the analysis finds them again:
bands recognized from the signal, measured values within tolerance, advice pointing in the right direction.
"""

import numpy as np
import pytest

from vinylcheck import alignment, i18n, measure, synth

SR = 48000


def _res(side, rig, **kw):
    audio, truth = synth.make_test_side(side, rig, seed=side, **kw)
    res = alignment.analyze(audio, SR)
    by = {b["role"]: b for b in res["metrics"]["bands"] if b.get("role")}
    items = {it["key"]: it for it in res["metrics"]["results"]}
    return res, truth, by, items


def en(m):
    return i18n.render(m, "en")


def test_tone_and_crosstalk_levels():
    t = np.arange(SR * 3) / SR
    a = 10 ** (-14 / 20)
    x = np.column_stack([a * np.sin(2 * np.pi * 1000 * t) + 0.01 * a * np.sin(2 * np.pi * 2000 * t),
                         10 ** (-25 / 20) * a * np.sin(2 * np.pi * 1000 * t)])
    r = measure.tone(x, SR)
    assert r["freq_hz"] == pytest.approx(1000, abs=0.5)
    assert r["level_db"][0] == pytest.approx(-14, abs=0.05)
    assert r["level_db"][1] - r["level_db"][0] == pytest.approx(-25, abs=0.1)
    assert r["thd_pct"][0] == pytest.approx(1.0, abs=0.02)


def test_flutter_speed_eccentricity_drive():
    n = SR * 40
    tt = np.arange(n) / SR
    f_rev = 100 / 3 / 60 * 1.008
    fi_true = 3150 * 1.008 * (1 + 0.002 * np.sin(2 * np.pi * f_rev * tt) + 0.001 * np.sin(2 * np.pi * 4 * tt))
    y = 0.2 * np.sin(2 * np.pi * np.cumsum(fi_true) / SR)
    fi, rate = measure.inst_freq(y, SR, 3150 * 1.008)
    fi = fi[int(rate):-int(rate)]
    w = measure.flutter(fi, rate, 3150.0, 100 / 3)
    assert w["speed_pct"] == pytest.approx(0.8, abs=0.01)
    assert w["eccentricity_pct"] == pytest.approx(0.2, abs=0.005)
    assert w["drive_weighted_2s_pct"] == pytest.approx(0.1, abs=0.005)   # 4 Hz: weighting 0 dB
    assert measure.weighting_db(np.array([4.0]))[0] == pytest.approx(0.0)


def test_side1_finds_all_bands_and_errors():
    rig = synth.Rig(balance_db=0.6, xt_lr_db=-22, xt_rl_db=-29, speed_pct=0.8, ecc_pct=0.15,
                    flutter_pct=0.05, hf_db=1.5, imd_pct=1.5)
    res, truth, by, items = _res(1, rig)
    assert {b.role for b in truth} <= set(by), "not all bands recognized"
    for b in truth:                                  # position correct to ±0.5 s
        assert by[b.role]["t0"] == pytest.approx(b.t0, abs=0.5)
    s = res["metrics"]["summary"]
    assert s["balance_db"] == pytest.approx(0.6, abs=0.1)
    assert s["crosstalk_lr_db"] == pytest.approx(-22, abs=0.5)
    assert s["crosstalk_rl_db"] == pytest.approx(-28.4, abs=0.5)        # −29 dB, right channel 0.6 dB quieter
    assert items["azimuth_asym_db"]["status"] == "warn"
    assert s["speed_pct"] == pytest.approx(0.8, abs=0.02)
    assert s["eccentricity_pct"] == pytest.approx(0.15, abs=0.01)
    assert s["wf_drive_pct"] == pytest.approx(0.05, abs=0.006)
    assert s["imd_pct"] == pytest.approx(1.5, abs=0.2)
    assert s["hf_10k_db"] == pytest.approx(1.5, abs=0.3)
    assert s["ref_level_dbfs"] == pytest.approx(-18.3, abs=0.3)
    assert not res["warnings"]


@pytest.mark.parametrize("clip_l, clip_r, verdict", [
    (7.0, None, "zu stark"),          # left distorts first → bias too strong
    (None, 5.0, "zu schwach"),        # right first → bias too weak
    (8.0, 8.3, "ausgeglichen"),       # both together → bias right, headroom limited
    (None, None, "ok"),               # clean up to +12 dB
])
def test_side2_antiskating_direction(clip_l, clip_r, verdict):
    res, truth, by, items = _res(2, synth.Rig(clip_l_db=clip_l, clip_r_db=clip_r))
    assert items["antiskating"]["value"] == verdict
    bl, br = by["antiskating"]["m"]["break_db"]
    for got, want in ((bl, clip_l), (br, clip_r)):
        if want is None:
            assert got is None
        else:
            assert want - 0.5 <= got <= want + 2.0
    reserve = res["metrics"]["summary"]["tracking_reserve_db"]
    assert reserve == pytest.approx(min(v for v in (clip_l, clip_r, 11.5) if v is not None), abs=2.0)
    text = en(items["antiskating"]["text"])
    assert {"zu stark": "too strong", "zu schwach": "too weak", "ausgeglichen": "bias is right",
            "ok": "Leave the settings"}[verdict] in text


@pytest.mark.parametrize("f0, q", [(11.0, 2.0), (7.0, 2.5), (14.0, 1.5)])
def test_resonance(f0, q):
    res, truth, by, items = _res(2, synth.Rig(resonance_hz=f0, resonance_q=q))
    fit = by["resonanz"]["m"]["fit"]
    assert fit["f0_hz"] == pytest.approx(f0, abs=0.5)
    assert fit["q"] == pytest.approx(q, abs=0.3)
    assert items["resonance_hz"]["status"] == ("ok" if 8 <= f0 <= 12 else "warn")


def test_side2_silent_groove_and_vertical():
    res, truth, by, items = _res(2, synth.Rig(rumble_db=-58))
    sil = by["stille_rille"]["m"]
    assert sil["rumble_db"] == pytest.approx(-58, abs=1.5)            # RMS against the RMS of the reference
    assert sil["lf_peaks"]["vertikal"]["freq_hz"] == pytest.approx(11, abs=1.5)
    assert by["vert_1k"]["m"]["mono_cancel_db"] < -25


def test_strong_record_warps_do_not_split_silent_groove():
    """Record warps at 10–17 Hz (light platter without record weight) can be so loud that the silent groove fell
    apart into many "bands" and the rumble was measured after the stylus lift (−76.5 instead of −40.4 dB)."""
    res, truth, by, items = _res(2, synth.Rig(rumble_db=-58, warp_db=-22))
    t = next(b for b in truth if b.role == "stille_rille")
    sil = by["stille_rille"]
    assert sil["t0"] == pytest.approx(t.t0, abs=2.5)
    assert sil["t1"] >= t.t1 - 2.5
    assert len(res["metrics"]["bands"]) <= 7
    assert -50 < sil["m"]["rumble_db"] < -36                          # the resonance slope above 20 Hz counts too
    assert sil["m"]["lf_peaks"]["vertikal"]["freq_hz"] == pytest.approx(11, abs=1.5)


def test_overdriven_input_is_reported():
    res, *_ = _res(2, synth.Rig(ref_dbfs=-9))                         # the +12 dB ramp clips
    w = next(w for w in res["warnings"] if w["$t"] == "alignment.warn.clipped_in_bands")
    assert res["metrics"]["clip_in_bands"] == w["$p"]["n"] > 0
    assert "clipped samples in measurement bands" in en(w)
    assert {it["key"]: it for it in res["metrics"]["results"]}["pegel"]["status"] == "warn"


def test_compare_summaries_direction():
    rows = {r["key"]: r for r in alignment.compare_summaries(
        {"tracking_reserve_db": 7.0, "imd_pct": 3.0, "resonance_hz": 7.5, "balance_db": -1.0},
        {"tracking_reserve_db": 11.5, "imd_pct": 2.0, "resonance_hz": 10.5, "balance_db": 0.4})}
    assert all(r["better"] == "b" for r in rows.values())
    assert rows["tracking_reserve_db"]["delta"] == pytest.approx(4.5)
    assert en(rows["imd_pct"]["label"]) == "Intermodulation (VTA)"


def test_live_meter_crosstalk_and_thd():
    t = np.arange(SR) / SR
    s = 0.1 * np.sin(2 * np.pi * 315 * t)
    x = np.column_stack([np.clip(s, -0.08, 0.08), s * 10 ** (-24 / 20)]).astype(np.float32)
    lm = measure.LiveMeter(SR, 2)
    for i in range(0, len(x), 4800):
        r = lm.feed(x[i:i + 4800])
    assert r["tonal"] and r["freq_hz"] == pytest.approx(315, abs=1)
    assert r["share"] >= 0.7                                             # share of the fundamental, for the response curve
    assert r["mode"] == "links" and r["crosstalk_db"] < -20
    assert r["thdn_pct"][0] > 3                                          # left channel clipped


# ─── Regression tests ────────────────────────────────────────────────────────
def test_failed_band_does_not_break_analysis(monkeypatch):
    """A band whose measurement fails drops out of the findings; the rest is still evaluated."""
    orig = alignment._tone_band

    def boom(audio, sr, b, comps=("L", "R")):
        if b.get("role") == "ref_1k":
            raise ValueError("broken")
        return orig(audio, sr, b, comps)
    monkeypatch.setattr(alignment, "_tone_band", boom)
    audio, _ = synth.make_test_side(1, synth.Rig(speed_pct=0.5), seed=1)
    res = alignment.analyze(audio, SR)
    keys = {it["key"] for it in res["metrics"]["results"]}
    assert "balance_db" not in keys and {"speed_pct", "crosstalk_lr_db", "imd_pct"} <= keys
    assert any(en(w).endswith("(1 kHz reference (lateral)) could not be measured: broken") for w in res["warnings"])


def test_break_in_last_half_db_counts_as_clean():
    res, truth, by, items = _res(2, synth.Rig(clip_l_db=11.5, clip_r_db=11.7))
    assert items["antiskating"]["value"] == "ok"
    assert items["tracking_reserve_db"]["status"] == "ok"


def test_mono_recording_is_handled():
    audio, _ = synth.make_test_side(1, synth.Rig(), seed=1, band_s=3, sweep_s=6, wf_s=6)
    res = alignment.analyze(audio[:, :1], SR)
    assert i18n.msg("alignment.warn.mono") in res["warnings"]
    assert {it["key"] for it in res["metrics"]["results"]} >= {"speed_pct", "hf_10k_db"}


def test_traj_fit_rejects_parabola_with_vertex_inside():
    t = np.linspace(0, 10, 200)
    fit = alignment._traj_fit(t, -(t - 5) ** 2 + 0.03 * np.sin(40 * t))   # goes up and down again
    assert len(fit) == 2
    fit = alignment._traj_fit(t, 2 + 0.5 * t + 0.02 * t ** 2)             # vertex at t = −12.5: parabola stays
    assert len(fit) == 3


def test_hum_does_not_fake_antiskating_break():
    """50 Hz hum (stronger on the right) raised THD+N at the start of the ramp above 3 % → "right distorts from
    +0 dB, bias too weak", although the distortion from harmonics rose cleanly in both channels."""
    audio, _ = synth.make_test_side(2, synth.Rig(), seed=2)
    t = np.arange(len(audio)) / SR
    hum = 10 ** ((-18 - 26) / 20) * np.sin(2 * np.pi * 50 * t)          # 26 dB below the reference tone
    audio = audio.copy()
    audio[:, 1] += hum.astype(audio.dtype)
    res = alignment.analyze(audio, SR)
    by = {b["role"]: b for b in res["metrics"]["bands"] if b.get("role")}
    items = {it["key"]: it for it in res["metrics"]["results"]}
    assert items["antiskating"]["value"] == "ok"
    assert by["antiskating"]["m"]["break_db"] == [None, None]
    assert by["antiskating"]["m"]["top_thdn_pct"][1] > by["antiskating"]["m"]["top_thd_pct"][1]   # hum only in THD+N


def test_live_meter_third_octave_bands():
    """31-band display: a sine lands with its level in its third-octave band, the neighbours far lower."""
    t = np.arange(SR) / SR
    x = np.column_stack([10 ** (-20 / 20) * np.sin(2 * np.pi * 1000 * t), 10 ** (-40 / 20) * np.sin(2 * np.pi * 100 * t)])
    lm = measure.LiveMeter(SR, 2)
    for i in range(0, len(x), 4800):
        r = lm.feed(x[i:i + 4800].astype(np.float32))
    bands = r["rta_db"]
    assert len(bands) == 2 and len(bands[0]) == len(measure.RTA_CENTERS_HZ) == 31
    k1k, k100 = measure.RTA_CENTERS_HZ.index(1000), measure.RTA_CENTERS_HZ.index(100)
    assert bands[0][k1k] == pytest.approx(-20, abs=0.5)                # sine reference: amplitude in dBFS
    assert bands[1][k100] == pytest.approx(-40, abs=0.5)
    assert bands[0][k1k - 2] < -55 and bands[0][k1k + 2] < -55


# ─── Target maps ────────────────────────────────────────────────────────────
def test_judge_rules_and_target_text():
    j = alignment.judge
    assert j("balance_db", 0.9) == "ok" and j("balance_db", -1.5) == "warn" and j("balance_db", 2.5) == "bad"
    assert j("crosstalk_lr_db", -30) == "ok" and j("crosstalk_lr_db", -22) == "warn" and j("crosstalk_lr_db", -10) == "bad"
    assert j("tracking_reserve_db", 12) == "ok" and j("tracking_reserve_db", 8) == "warn" and j("tracking_reserve_db", 3) == "bad"
    assert j("resonance_hz", 10) == "ok" and j("resonance_hz", 13.5) == "warn" and j("resonance_hz", 21.7) == "bad"
    assert j("resonance_hz", None) is None and j("unknown", 1) is None and j("speed_pct", "ok") is None
    din = alignment.TARGET_MAPS["din45500"]["targets"]
    assert j("crosstalk_lr_db", -18, din) == "ok" and j("speed_pct", 1.2, din) == "ok"
    tt = alignment.target_text
    assert en(tt("balance_db")) == "±1 dB" and en(tt("resonance_hz")) == "8 to 12 Hz"
    assert en(tt("crosstalk_lr_db")) == "≤ −25 dB" and en(tt("wf_weighted_pct")) == "≤ 0.1 %"
    assert en(tt("tracking_reserve_db")) == "≥ 11 dB" and en(tt("pegel")) == "−24 to −16 dBFS"
    assert i18n.render(tt("wf_weighted_pct"), "de") == "≤ 0,1 %" and i18n.render(tt("resonance_hz"), "de") == "8 bis 12 Hz"
    assert en(tt("wf_weighted_pct", alignment.TARGET_MAPS["at3600l"]["targets"])) == "≤ 0.15 %"
    assert tt("balance_db", {"speed_pct": {"ok": 1, "warn": 2}}) == ""             # no rule for this metric
    for mid, m in alignment.TARGET_MAPS.items():                        # every map complete and valid
        assert set(m["targets"]) == set(alignment.TARGET_FIELDS), mid
        alignment.validate_targets(m["targets"])
        assert en(m["name"]) and en(m["note"]) and not en(m["name"]).startswith("alignment.")


def test_validate_targets_rejects_inconsistent_rules():
    with pytest.raises(ValueError):
        alignment.validate_targets({"balance_db": {"ok": 2, "warn": 1}})          # "check" narrower than "ok"
    with pytest.raises(ValueError):
        alignment.validate_targets({"resonance_hz": {"ok": [8, 12], "warn": [9, 15]}})
    with pytest.raises(ValueError, match="Unknown metric nope"):
        alignment.validate_targets({"nope": {"ok": 1, "warn": 2}})
    ok =alignment.validate_targets({"speed_pct": {"ok": "0.5", "warn": 1}})
    assert ok == {"speed_pct": {"ok": 0.5, "warn": 1.0}}
    with pytest.raises(alignment.TargetMapError) as e:
        alignment.validate_targets({"balance_db": {"ok": 2, "warn": 1}})
    assert e.value.msg["$t"] == "alignment.error.warn_narrower_than_ok"
    assert str(e.value).startswith("Channel balance L − R: ")                  # str() in the current language
    with i18n.use_lang("de"):
        assert str(e.value).startswith("Kanalbalance L − R: „prüfen“")


def test_apply_targets_rejudges_and_toggles_advice():
    items = [alignment._item("crosstalk_lr_db", "Crosstalk", -22.0, "dB", "warn", "Separation 22.0 dB.",
                             advice="Check azimuth."),
             alignment._item("antiskating", "Anti-skating", "ok", "", "ok", "Both channels clean.", soll="whole ramp"),
             alignment._item("resonance_hz", "Resonance", None, "Hz", "info", "Sweep not low enough.", soll="")]
    din_t = alignment.TARGET_MAPS["din45500"]["targets"]
    std = alignment.apply_targets(items, alignment.STANDARD_TARGETS)
    xt = next(i for i in std if i["key"] == "crosstalk_lr_db")
    assert xt["status"] == "warn" and xt["text"] == "Separation 22.0 dB. Check azimuth." and en(xt["soll"]) == "≤ −25 dB"
    din = alignment.apply_targets(items, din_t)
    xt = next(i for i in din if i["key"] == "crosstalk_lr_db")
    assert xt["status"] == "ok" and xt["text"] == "Separation 22.0 dB." and en(xt["soll"]) == "≤ −15 dB"
    assert next(i for i in din if i["key"] == "antiskating")["soll"] == "whole ramp"          # no rule: unchanged
    assert next(i for i in din if i["key"] == "resonance_hz")["status"] == "info"
    # stored findings already carry their advice: re-applying adds it once, and a map that says "ok" removes it
    again = next(i for i in alignment.apply_targets(std, alignment.STANDARD_TARGETS) if i["key"] == "crosstalk_lr_db")
    assert again["text"] == "Separation 22.0 dB. Check azimuth."
    assert next(i for i in alignment.apply_targets(std, din_t) if i["key"] == "crosstalk_lr_db")["text"] == "Separation 22.0 dB."
    assert alignment.apply_targets(alignment.apply_targets(std, din_t), alignment.STANDARD_TARGETS) == std


def test_side1_items_carry_neutral_text_and_advice():
    res, truth, by, items = _res(1, synth.Rig(xt_lr_db=-21, xt_rl_db=-27, balance_db=1.6))
    advice = en(items["crosstalk_lr_db"]["advice"])
    assert items["crosstalk_lr_db"]["status"] == "warn" and en(items["crosstalk_lr_db"]["text"]).endswith(advice)
    assert items["crosstalk_rl_db"]["status"] == "ok" and advice not in en(items["crosstalk_rl_db"]["text"])
    assert items["balance_db"]["status"] == "warn" and items["balance_db"]["advice"]
    bal = en(items["balance_db"]["text"])
    assert bal.startswith("Left is 1.") and bal.endswith(" dB louder. " + en(items["balance_db"]["advice"]))
    assert en(items["pegel"]["soll"]) == "−24 to −16 dBFS"
    # findings as stored and re-judged by the web interface: the advice appears exactly once
    rejudged = {it["key"]: it for it in alignment.apply_targets(res["metrics"]["results"], alignment.STANDARD_TARGETS)}
    assert en(rejudged["crosstalk_lr_db"]["text"]).count(advice) == 1


@pytest.mark.parametrize("side, rig", [
    (1, synth.Rig(xt_lr_db=-21, balance_db=1.6, speed_pct=0.8, hf_db=3.0)),
    (2, synth.Rig(clip_l_db=7.0, rumble_db=-50, ref_dbfs=-9)),
])
def test_all_texts_render_in_both_languages(side, rig):
    """Every label, finding, target and warning renders completely in English and German (no raw keys,
    no unfilled placeholders), with German decimal commas."""
    res, *_ = _res(side, rig)
    m = res["metrics"]
    texts = [it[k] for it in m["results"] for k in ("label", "text", "soll", "advice") if it.get(k)]
    texts += [b["label"] for b in m["bands"]] + res["warnings"] + [row[4] for row in res["overview"]["bands"]]
    assert texts and all(i18n.is_msg(x) or x == "–" for x in texts)
    for lang in ("en", "de"):
        for x in texts:
            s = i18n.render(x, lang)
            assert isinstance(s, str) and "{" not in s and "alignment." not in s, (lang, x, s)
    de = " ".join(i18n.render(x, "de") for x in texts)
    assert "Kanaltrennung" in de or "Antiskating" in de
    assert not any(c.isdigit() and n == "." and d.isdigit() for c, n, d in zip(de, de[1:], de[2:]))
