"""Engineer mode: recording against the master. A clean synthetic master is turned into a "pressing" with known flaws
(speed, off-center hole, treble loss, a quieter track, noise, clicks, pre-echo, polarity, channels); master.check must
find exactly these flaws. The PDF reports are rendered from such a result in every language."""

import logging
import time

import numpy as np
import pytest
from scipy import signal

from vinylcheck import i18n, master, report_pdf, synth

SR = 48000
RPM = 100 / 3
LAY = synth.Layout(lead_in_s=2.0, tracks=3, gap_s=4.0, lead_out_s=2.0)


def make_master(seed=3, duration_s=90.0):
    audio, truth = synth.make_side(duration_s=duration_s, seed=seed, layout=LAY, noise_dbfs=-150.0,
                                   clicks_per_min=0, pops=0)
    return audio, truth


def _resample(x, pos):
    """Band-limited resampling at fractional positions (windowed sinc). Cubic interpolation was not enough: its treble
    error changes with the fractional position, so with a varying speed it becomes a time-variant filter that a real
    record does not have, and it artificially worsens the click classification (residual after subtracting the
    master)."""
    out = np.empty((len(pos), x.shape[1]))
    for a in range(0, len(pos), 200_000):                  # in blocks: in one go it would need 4–5 GB (32 taps)
        for c in range(x.shape[1]):
            out[a:a + 200_000, c] = master._sinc_sample(x[:, c], pos[a:a + 200_000], half=16)
    return out


def press(m, truth, *, speed=0.004, ecc_mm=0.25, gain_db=-6.0, shelf_db=-3.0, noise_dbfs=-62.0, lead_in_s=3.0,
          lead_out_s=3.0, track_gain_db=(0.0, -1.0, 0.0), pre_echo_db=-30.0, clicks=(), polarity=(1, 1), swap=False, seed=9):
    """Vinyl simulation. Returns (recording, mapping recording time → master time)."""
    rng = np.random.default_rng(seed)
    dm = len(m) / SR
    tv_len = lead_in_s + dm / (1 + speed) + lead_out_s
    n = int(tv_len * SR)
    tau = np.arange(n) / SR
    omega = 2 * np.pi * RPM / 60
    t_lin = (tau - lead_in_s) * (1 + speed)
    ms0, ms1 = truth.music[0][0], truth.music[-1][1]
    frac = np.clip((t_lin - ms0) / (ms1 - ms0), 0, 1)
    r = np.sqrt(master.R_OUT_MM ** 2 - (master.R_OUT_MM ** 2 - master.R_IN_MM ** 2) * frac)
    a = ecc_mm / r
    t_m = t_lin + a / omega * np.sin(omega * tau) * (1 + speed)
    pos = t_m * SR
    inside = (pos >= 0) & (pos < len(m) - 2)
    v = np.zeros((n, 2))
    v[inside] = _resample(m.astype(np.float64), pos[inside])
    hp = signal.butter(2, 6000, btype="high", fs=SR, output="sos")
    v = v + (10 ** (shelf_db / 20) - 1) * signal.sosfilt(hp, v, axis=0)
    v *= 10 ** (gain_db / 20)
    for (t0, t1), g in zip(truth.music, track_gain_db):
        sel = (t_m >= t0) & (t_m < t1)
        v[sel] *= 10 ** (g / 20)
    music_only = v.copy()
    rev = 60 / RPM
    inv = lambda t: float(np.interp(t, t_m, tau))
    for t0, _ in truth.music:                              # pre-echo: the following revolution, one revolution early
        i_on = int(inv(t0) * SR)
        ln = int(rev * SR)
        j = i_on - ln
        v[j:j + ln] += music_only[i_on:i_on + ln] * 10 ** (pre_echo_db / 20)
    v += synth._surface_noise(n, rng, noise_dbfs)
    for tc, amp in clicks:
        synth.add_click(v, SR, tc, amp, 3, rng)
    v *= np.array(polarity, dtype=float)
    if swap:
        v = v[:, ::-1]
    return np.clip(v, -1, 1).astype(np.float32), (tau, t_m)


def _keys(messages):
    return [m["$t"] for m in messages if isinstance(m, dict)]


@pytest.fixture(scope="module")
def side_a():
    return make_master(seed=3)


@pytest.fixture(scope="module")
def pressed(side_a):
    m, truth = side_a
    rng = np.random.default_rng(1)
    clicks = [(float(t), -24.0) for t in np.sort(rng.uniform(10, 85, 25))]
    v, (tau, t_m) = press(m, truth, clicks=clicks)
    inv = lambda t: float(np.interp(t, t_m, tau))
    beats = [t0 + k * 0.5 for t0, t1 in truth.music for k in range(int((t1 - t0) / 0.5)) if t0 + k * 0.5 + 0.1 < t1]
    drums = [inv(t) for t in beats[::7]]                   # drum hits: false alarms that the master explains
    events = ([{"t": t, "kind": "click", "region": "music", "strength": 10.0} for t, _ in clicks]
              + [{"t": t, "kind": "click", "region": "music", "strength": 5.0} for t in drums])
    tracks = [{"pos": f"A{i + 1}", "title": f"Track {i + 1}", "t0": inv(a), "t1": inv(b)} for i, (a, b) in enumerate(truth.music)]
    quiet = [[0.2, 2.8]]                                   # lead-in groove of the recording
    res = {"events": events, "metrics": {"tracks": tracks}, "overview": {"quiet": quiet}}
    out = master.check(v, m, SR, res, rpm=RPM, turntable_speed_pct=0.1)
    return out, clicks, drums, truth


def test_speed_wow_and_eccentricity(pressed):
    out, *_ = pressed
    assert out["ok"] and out["polarity"] == "ok", out.get("reason")
    assert abs(out["speed"]["vs_master_pct"] - 0.4) < 0.02
    assert abs(out["speed"]["cut_pct"] - 0.3) < 0.02
    segs = [s for s in out["wow"]["segments"] if s.get("wow_pct") is not None]
    assert len(segs) == 3
    assert [s["label"]["$t"] for s in segs] == ["master.part_start", "master.part_middle", "master.part_end"]
    for s in segs:                                         # variation = e / r at every position
        assert abs(s["wow_pct"] - 100 * 0.25 / s["radius_mm"]) < 0.03, s
    assert abs(out["wow"]["ecc_mm"] - 0.25) < 0.04
    assert out["wow"]["verdict"]["$t"] == "master.wow_eccentric"
    assert segs[-1]["wow_pct"] > segs[0]["wow_pct"] * 1.6
    assert "master.warn_eccentric" in _keys(out["warnings"])
    ecc = next(w for w in out["warnings"] if w["$t"] == "master.warn_eccentric")
    assert "mm off-center" in i18n.render(ecc, "en") and "mm außermittig" in i18n.render(ecc, "de")
    assert f'{out["wow"]["ecc_mm"]:.2f}'.replace(".", ",") in i18n.render(ecc, "de")    # German decimal comma


def test_channels_frequency_and_levels(pressed):
    out, *_ = pressed
    ch = out["channels"]
    assert ch["verdict"] == "ok" and not ch["swapped"] and ch["c_mid"] > 0.8
    assert abs(ch["balance_db"]) < 0.3
    f = out["freq"]
    bands = f["bands"]
    for seg in f["segments"]:
        mid = dict(zip(bands, seg["mid_db"]))
        assert abs(mid[250]) < 0.7 and abs(mid[1000]) < 0.01
        assert -3.9 < mid[12500] < -2.0, (seg["label"], mid[12500])
    lv = {x["pos"]: x for x in out["levels"]["items"]}
    assert abs(out["levels"]["gain_db"] + 6.0) < 0.5
    assert abs(lv["A2"]["rel_db"] + 1.0) < 0.25 and abs(lv["A1"]["rel_db"]) < 0.25


def test_clicks_vinyl_or_music(pressed):
    out, clicks, drums, _ = pressed
    top = {round(x["t"], 3) for x in out["clicks"]["top"]}
    hits = sum(1 for t, _ in clicks if round(t, 3) in top)
    assert hits >= 0.9 * len(clicks), (hits, len(clicks))
    assert out["clicks"]["by_source"]["master"]["click"] >= 0.9 * len(drums)
    assert out["clicks"]["vinyl_music_per_min"] > 0


def test_gaps_noise_and_pre_echo(pressed):
    out, *_ = pressed
    g = out["gaps"]
    kinds = [x["kind"] for x in g["items"]]
    assert kinds.count("Pause") == 2 and kinds[0] == "Anfang"
    for x in g["items"]:
        if x["kind"] == "Anfang":                          # 2 s lead-in, 1.8 s of it pre-echo: no pure noise left
            assert x["noise_dbfs"] > -62.5
        else:
            assert abs(x["noise_dbfs"] + 62) < 1.0, x
        if x.get("pre_echo"):
            assert x["pre_echo"]["found"] and abs(x["pre_echo"]["db"] + 30) < 3, x["pre_echo"]
        if x.get("post_echo"):
            assert not x["post_echo"]["found"], x["post_echo"]  # no post-echo built in: no false alarm
    assert g["pre_echo_count"] == 3 and g["post_echo_count"] == 0
    pre = next(w for w in out["warnings"] if w["$t"] == "master.warn_pre_echo")
    assert i18n.render(pre, "en").startswith("Pre-echo in 3 gaps,")
    assert i18n.render(pre, "de").startswith("Vorecho in 3 Pausen,")


@pytest.mark.parametrize("pol,swap,expect,warning", [((-1, -1), False, "gedreht", "master.warn_polarity_inverted"),
                                                     ((1, -1), False, "rechts verpolt", "master.warn_polarity_right"),
                                                     ((1, 1), True, "vertauscht", "master.warn_channels_swapped")])
def test_polarity_and_swapped_channels(side_a, pol, swap, expect, warning):
    m, truth = side_a
    v, _ = press(m, truth, ecc_mm=0.0, polarity=pol, swap=swap, pre_echo_db=-80)
    out = master.check(v, m, SR, None, rpm=RPM)
    assert out["ok"], out.get("reason")
    if expect == "vertauscht":
        assert out["polarity"] == "ok" and out["channels"]["swapped"]
        assert out["channels"]["verdict"]["$t"] == "master.channels_swapped"
    else:
        assert out["polarity"] == expect and not out["channels"]["swapped"]
    assert warning in _keys(out["warnings"])
    assert abs(out["speed"]["vs_master_pct"] - 0.4) < 0.02


def test_wrong_side_points_to_other_master(side_a):
    m_a, truth_a = side_a
    m_b, _ = make_master(seed=11)
    v, _ = press(m_a, truth_a, ecc_mm=0.0)
    out = master.check(v, m_b, SR, None, rpm=RPM, others={"A": m_a})
    assert not out["ok"] and out["other_side"] == "A"
    assert out["reason"] == {"$t": "master.reason_other_side", "$p": {"side": "A"}}
    assert "side A" in i18n.render(out["reason"], "en") and "Seite A" in i18n.render(out["reason"], "de")


def _report_data(out: dict) -> tuple[dict, dict]:
    """Approval and recording report data shaped like web.approval_report_data() / recording_report_data(), built
    from a master.check result (its labels, verdicts and warnings are i18n messages)."""
    k, sp, wo, fr, ga = out["clicks"], out["speed"], out["wow"], out["freq"], out["gaps"]
    img = (np.arange(40 * 300) % 256).astype(np.uint8).reshape(40, 300)
    sg = {"img": img, "t0": 0.0, "t1": 96.0, "fmin": 20.0, "fmax": 20000.0}
    names = ["Test pressing 1", "Test pressing 2"]
    copies = [{"copy": n, "recording_id": i + 1, "grade": "VG+", "grade_master": "NM",
               "clicks_vinyl": k["vinyl_music_per_min"], "pops_vinyl": k["vinyl_pops_music_per_min"], "audible": 1,
               "speed_pct": sp["vs_master_pct"], "cut_pct": sp["cut_pct"], "ecc_mm": wo["ecc_mm"],
               "hf_loss_end_db": fr.get("hf_loss_end_db"), "pre_echo_db": ga["pre_echo_max_db"], "polarity": pol}
              for i, (n, pol) in enumerate(zip(names, (out["polarity"], "gedreht")))]
    common = [{"tm": 20.0, "n": 2, "max_strength": 120.0, "kind": "click", "copies": {n: {"t": 21.0} for n in names}},
              {"tm": 50.0, "n": 2, "max_strength": 20.0, "kind": "tick", "copies": {n: {"t": 51.0} for n in names}}]
    vinyl = [{"tm": x["t_m"], "kind": x["kind"], "strength": x["strength"] or 1.0} for x in k["top"]]
    detail = [{"copy": n, "vinyl": vinyl, "freq": fr, "wow": wo, "levels": out["levels"]} for n in names]
    checks = [{"name": "Stamper / cut", "status": "warn", "text": "1 audible defect on all copies.", "detail": "2 total."},
              {"name": i18n.msg("master.channels_swapped"), "status": "bad",
               "text": i18n.msg("master.warn_channels_swapped")}]
    markers = [{"t": 21.0, "status": "bad", "label": report_pdf.ft(20.0)}]
    zooms = [{"tm": 20.0, "kind": "click", "n": 2, "max_strength": 120.0,
              "cells": [sg | {"copy": n, "t": 21.0, "t0": 20.75, "t1": 21.25} for n in names]}]
    side = {"side": "A", "master": {"name": "Side A.wav", "duration_s": out["master_s"], "samplerate": SR},
            "copies": copies, "checks": checks, "verdict": "Freigabe mit Hinweisen", "loud_common": common[:1],
            "common": common, "copies_detail": detail, "spectrogram": sg | {"copy": names[0], "markers": markers},
            "zooms": zooms}
    criteria = {"cut_ok": 0.3, "cut_warn": 0.6, "ecc_ok": 0.2, "ecc_warn": 0.35, "pre_echo_warn": -45.0,
                "hf_warn": -6.0, "stamper_hint_max": 3}
    approval = {"record": {"id": 1, "artist": "Test Artist", "title": "Test Album"}, "sides": [side],
                "verdict": "Freigabe mit Hinweisen", "criteria": criteria, "audible": 80.0, "created_at": time.time(),
                "version": "test"}
    metrics = {"clicks_music_per_min": 3.2, "clicks_quiet_per_min": 5.0, "ticks_quiet_per_min": 12.0,
               "pops_per_min": 0.1, "clicks_quiet_net_per_min": 4.0, "ticks_quiet_net_per_min": 10.0,
               "floor": {"clicks_per_min": 1.0, "ticks_per_min": 2.0}, "noise_rel_db": -55.0, "noise_dbfs": -62.0,
               "music_ref_dbfs": -18.0, "counts": {"pop": 1}, "second_opinion": {"clicks_music_confirmed_per_min": 2.5},
               "learned": {"clicks_music_per_min": 2.0},
               "tracks": [{"pos": "A1", "title": "Track 1", "t0": 3.0, "t1": 30.0, "clicks_per_min": 2.0,
                           "ticks_per_min": 5.0, "pops": 0}],
               "skips": [{"kind": "rueckwaerts", "t": 40.0, "grooves": 1, "repeats": 2, "position_pct": 45.0,
                          "score": 0.9, "label": None}]}
    grading = {"grade": "VG+", "score": 81, "hinweis": i18n.msg("master.warn_starts_late"),
               "per_metric": {"clicks_music_per_min": {"label": "Clicks/min in the music (info only)", "value": 3.2,
                                                       "grade": "VG+", "in_grade": True}}}
    overview = {"events": [[10.0, "click", "music", 12.0, None, 0.5], [1.0, "tick", "excluded", 3.0, None, None],
                           [50.0, "pop", "quiet", 30.0, "musik", None]],
                "level_db": np.linspace(-60, -20, 96).tolist(), "bucket_s": 1.0, "quiet": [[0.2, 2.8]],
                "groove": [1.0, 95.0]}
    top = [{"t": 10.0, "kind": "click", "region": "music", "strength": 12.0, "amp_dbfs": -30.0, "ch": 1, "gc": 0.8,
            "lp": 0.6, "label": "knackser"}]
    recording = {"record": {"artist": "Test Artist", "title": "Test Album"}, "metrics": metrics, "grading": grading,
                 "recording": {"id": 7, "side": "A", "kind": "before", "created_at": time.time(),
                               "copy_label": names[0], "notes": "a & b"},
                 "warnings": out["warnings"] + ["A warning stored as plain text."], "kind_label": "before cleaning",
                 "profile": "Turntable A", "findings": "visible scratch", "overview": overview,
                 "spectrogram": sg | {"markers": [{"t": 40.0, "status": "bad", "label": "skip"}]}, "top_events": top,
                 "tech": [("Peak (sample)", "L −1.0 dBFS · R −1.2 dBFS")],
                 "master": {"grade": "NM", "clicks": 1.2, "pops": 0.0, "cut_pct": 0.3, "ecc_mm": 0.25,
                            "polarity": "wie Master", "name": "Side A.wav"}, "version": "test"}
    return approval, recording


SUBJECTS = {"en": (b"Test pressing approval against the digital master", b"Condition of a record side"),
            "de": (b"Testpressungs-Abnahme gegen den digitalen Master", b"Zustand einer Schallplattenseite")}


@pytest.mark.parametrize("lang", ["en", "de"])
def test_reports_render_in_each_language(pressed, caplog, lang):
    """Both PDF reports render in the request language, with messages and plain strings from other modules."""
    approval, recording = _report_data(pressed[0])
    with caplog.at_level(logging.WARNING, logger="vinylcheck.i18n"), i18n.use_lang(lang):
        pdfs = report_pdf.build(approval), report_pdf.build_recording(recording)
        assert report_pdf.de(-1.25, 2, sign=True, unit="dB") == ("−1,25 dB" if lang == "de" else "−1.25 dB")
        assert report_pdf.ft(83.5) == ("1:23,50" if lang == "de" else "1:23.50")
        verdict = report_pdf._verdict("Freigabe mit Hinweisen")                   # web.py's verdict text → own label
        assert verdict == (("Freigabe mit Hinweisen" if lang == "de" else "Approval with notes"), "warn")
    for pdf, subject in zip(pdfs, SUBJECTS[lang]):
        assert pdf[:5] == b"%PDF-" and b"/Count" in pdf and len(pdf) > 10000
        assert subject in pdf                              # document info (not compressed) is in the request language
    assert not [r for r in caplog.records if "translation" in r.getMessage()]


def test_api_flow_engineer_mode(tmp_path):
    """Through the API: engineer mode on, upload a master, recording → analysis → master comparison, A/B snippets,
    second copy (shared stamper defects), test pressing approval and both PDF reports."""
    import io
    from dataclasses import replace
    import soundfile as sf
    from fastapi.testclient import TestClient
    from vinylcheck.config import Config
    from vinylcheck.web import create_app

    m, truth = make_master(seed=5, duration_s=60.0)
    v, _ = press(m, truth, ecc_mm=0.15, clicks=[(t, -14.0) for t in (8.5, 17.3, 26.1, 38.7, 47.9)])
    wav = tmp_path / "pressing.wav"
    sf.write(wav, v, SR, subtype="PCM_16")
    mbuf = io.BytesIO()
    sf.write(mbuf, m, SR, format="WAV", subtype="PCM_24")
    cfg = replace(Config(), data_dir=tmp_path / "data", state_dir=tmp_path / "state", fake_source=str(wav),
                  fake_speed=200.0, min_free_gb=0.0, autostop_end_groove=False)
    with TestClient(create_app(cfg)) as c:
        assert c.put("/api/settings", json={"engineer_mode": True}).json()["values"]["engineer_mode"] is True
        assert c.get("/api/status").json()["engineer_mode"] is True
        rid = c.post("/api/records", json={"artist": "Test Artist", "title": "Test Pressing"}).json()["id"]
        assert c.put(f"/api/records/{rid}/masters/A", content=b"no audio").status_code == 415
        short = io.BytesIO()
        sf.write(short, m[:SR * 5], SR, format="WAV", subtype="PCM_16")
        assert c.put(f"/api/records/{rid}/masters/A", content=short.getvalue()).status_code == 422
        up = c.put(f"/api/records/{rid}/masters/A", params={"name": "SIDE A.wav"}, content=mbuf.getvalue()).json()
        assert up["side"] == "A" and up["name"] == "SIDE A.wav" and up["samplerate"] == SR and abs(up["duration_s"] - 60) < 0.1
        assert [x["side"] for x in c.get(f"/api/records/{rid}").json()["masters"]] == ["A"]

        r = c.post("/api/recordings/start", json={"record_id": rid, "side": "A", "kind": "before", "copy_label": "Test pressing 1"})
        assert r.status_code == 200, r.text
        rec_id = r.json()["recording_id"]
        deadline = time.time() + 60
        chk = None
        while time.time() < deadline:
            chk = c.get(f"/api/recordings/{rec_id}/master").json()["check"]
            if chk and chk["status"] in ("done", "error"):
                break
            time.sleep(0.3)
        assert chk and chk["status"] == "done", chk
        res = chk["result"]
        assert res["ok"] and abs(res["speed"]["vs_master_pct"] - 0.4) < 0.03 and res["master_file"]["name"] == "SIDE A.wav"
        assert all(isinstance(w, str) for w in res["warnings"])                   # messages arrive rendered
        assert [s["label"] for s in res["wow"]["segments"]] == ["Start", "Middle", "End"]
        de_res = c.get(f"/api/recordings/{rec_id}/master", params={"lang": "de"}).json()["check"]["result"]
        assert [s["label"] for s in de_res["wow"]["segments"]] == ["Anfang", "Mitte", "Ende"]
        snip = c.get(f"/api/recordings/{rec_id}/master/snippet", params={"t": 20.0, "before": 0.5, "after": 1.0})
        assert snip.status_code == 200 and snip.content[:4] == b"RIFF"
        assert c.post(f"/api/recordings/{rec_id}/master").json()["queued"]
        assert c.get(f"/api/records/{rid}").json()["master_states"][str(rec_id)] in ("queued", "running", "done")
        deadline = time.time() + 60
        while c.get(f"/api/recordings/{rec_id}/master").json()["check"]["status"] != "done" and time.time() < deadline:
            time.sleep(0.3)
        assert c.get(f"/api/recordings/{rec_id}/master").json()["check"]["status"] == "done"
        dm = c.get(f"/api/recordings/{rec_id}/master").json()
        gm, loud = dm["grading_master"], dm["loud"]
        assert gm and gm["grade"] in ("NM", "VG+", "VG", "G+", "G") and loud["audible"] == len(loud["top"])
        # second copy: the same file → all real defects at the same spots (stamper)
        r2 = c.post("/api/recordings/start", json={"record_id": rid, "side": "A", "kind": "before", "copy_label": "Test pressing 2"})
        rec2 = r2.json()["recording_id"]
        deadline = time.time() + 90
        while time.time() < deadline:
            ch2 = c.get(f"/api/recordings/{rec2}/master").json()["check"]
            if ch2 and ch2["status"] in ("done", "error"):
                break
            time.sleep(0.3)
        assert ch2["status"] == "done", ch2
        com = c.get(f"/api/records/{rid}/master/common", params={"side": "A"}).json()
        assert [x["copy"] for x in com["copies"]] == ["Test pressing 1", "Test pressing 2"]
        n_real = com["copies"][0]["events"]
        assert n_real > 0 and com["copies"][1]["events"] == n_real and com["copies"][0]["shared"] == n_real
        assert com["common_count"] == n_real and all(x["n"] == 2 for x in com["common"]) and com["on_all"] == n_real
        assert com["copies"][0]["grade_master"] == gm["grade"] and com["copies"][0]["grade"]
        rec_page = c.get(f"/api/records/{rid}").json()
        assert not any("grade_ear" in sd for cp in rec_page["copies"] for sd in cp["sides"])   # no second grade any more
        # Test pressing approval. Its verdicts and check names are display texts of web.py; they are compared in German,
        # whose wording stays the same whether web.py returns plain text or i18n messages.
        ap = c.get(f"/api/records/{rid}/approval", params={"lang": "de"}).json()
        assert ap["verdict"] in ("Freigabe empfohlen", "Freigabe mit Hinweisen", "nicht freigeben") and len(ap["sides"]) == 1
        assert ap["criteria"]["stamper_hint_max"] == 3                             # up to 3 stamper defects: a note
        names = [x["name"] for x in ap["sides"][0]["checks"]]
        assert {"Stamper / Schnitt", "Drehzahl des Schnitts", "Exemplare gegen den Master", "Vorecho"} <= set(names), names
        speed = next(x for x in ap["sides"][0]["checks"] if x["name"] == "Drehzahl des Schnitts")
        assert speed["status"] == "warn" and "+0,4" in speed["text"]               # 0.4 % fast, no calibration
        assert len(ap["sides"][0]["copies"]) == 2 and all(x["ecc_mm"] is not None for x in ap["sides"][0]["copies"])
        for lang in ("en", "de"):                                                    # PDF reports in both languages
            pdf = c.get(f"/api/records/{rid}/approval.pdf", params={"lang": lang})
            assert pdf.status_code == 200 and pdf.headers["content-type"] == "application/pdf", pdf.text[:300]
            assert pdf.content[:5] == b"%PDF-" and b"/Count" in pdf.content and len(pdf.content) > 20000
            assert "attachment" in pdf.headers["content-disposition"] and ".pdf" in pdf.headers["content-disposition"]
            rpdf = c.get(f"/api/recordings/{rec_id}/report.pdf", params={"lang": lang})
            assert rpdf.status_code == 200 and rpdf.content[:5] == b"%PDF-", rpdf.text[:300]
        assert c.delete(f"/api/records/{rid}/masters/A").json()["ok"]
        assert c.get(f"/api/records/{rid}/approval.pdf").status_code == 409                 # no report without a master
        assert not list((tmp_path / "data" / "masters").glob("*.wav"))
        assert c.post(f"/api/recordings/{rec_id}/master").status_code == 409
