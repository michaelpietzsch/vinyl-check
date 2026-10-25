"""Track table: track boundaries from Discogs durations, snapped to the pauses, metrics per track; what-if and
skip labels."""


from vinylcheck import analysis, discogs, i18n, synth

SR = 48000


def test_tracks_by_side_and_master_fallback(monkeypatch):
    tl = [{"position": "A1", "title": "One", "duration": "4:10", "type_": "track"},
          {"position": "A2", "title": "Two", "duration": "", "type_": "track"},
          {"position": "B1", "title": "Three", "duration": "3:05", "type_": "track"},
          {"position": "", "title": "Side A", "type_": "heading"}]
    tr = discogs.tracks_by_side(tl)
    assert [x["pos"] for x in tr["A"]] == ["A1", "A2"] and tr["A"][0]["dur_s"] == 250.0 and tr["A"][1]["dur_s"] is None
    assert tr["B"] == [{"pos": "B1", "title": "Three", "dur_s": 185.0}]
    d = discogs.Discogs("", "", "/nonexistent/x.json", "test")
    calls = []

    def fake(url):
        calls.append(url)
        if "/releases/" in url:
            return {"tracklist": tl, "master_id": 99}
        return {"tracklist": [{"position": "A2", "title": "Two", "duration": "3:30", "type_": "track"}]}
    monkeypatch.setattr(d, "_public_or_auth", fake)
    tr = d.release_tracks(5)
    assert tr["A"][1]["dur_s"] == 210.0 and len(calls) == 2 and "/masters/99" in calls[1]


def test_track_bounds_snap_to_gaps_and_stats_sum_up():
    lay = synth.Layout(lead_in_s=5.0, tracks=3, gap_s=3.0, lead_out_s=5.0)
    audio, truth = synth.make_side(duration_s=120, seed=2, clicks_per_min=40, layout=lay)
    tracks = [{"pos": f"A{i + 1}", "title": f"Track {i + 1}", "dur_s": b - a} for i, (a, b) in enumerate(truth.music)]
    res = analysis.analyze(audio, SR, analysis.AnalysisConfig(second_opinion=False), tracks=tracks)
    tr = res["metrics"]["tracks"]
    assert len(tr) == 3 and all(x["snapped"] for x in tr)
    for x, (a, b) in zip(tr, truth.music):
        assert abs(x["t0"] - a) < 1.5 and abs(x["t1"] - b) < 1.5, (x, a, b)
    assert sum(x["clicks"] for x in tr) <= res["metrics"]["counts"]["music"]
    assert all(x["clicks_per_min"] >= 0 and x["ticks_per_min"] >= 0 for x in tr)
    # no table without complete durations; a recount keeps the table
    assert analysis.analyze(audio, SR, analysis.AnalysisConfig(second_opinion=False), tracks=[{"pos": "A1", "dur_s": None}])["metrics"]["tracks"] == []
    m2, _ = analysis.recount(res["metrics"], res["events"])
    assert [x["t0"] for x in m2["tracks"]] == [x["t0"] for x in tr] and m2["tracks"][0]["clicks"] == tr[0]["clicks"]
    assert analysis.track_bounds(tracks, None, 100.0, []) == [] and analysis.track_bounds([], 5.0, 100.0, []) == []


def test_what_if_ignores_skips_and_events_without_touching_inputs():
    audio, _ = synth.make_side(duration_s=40, seed=5, clicks_per_min=30)
    res = analysis.analyze(audio, SR, analysis.AnalysisConfig(second_opinion=False))
    sk = [{"t": 20.0, "kind": "backward", "position_pct": 50.0}]
    m = dict(res["metrics"]); m["skips"] = sk; m["skips_back"] = 1
    _, g0 = analysis.recount(m, res["events"], skip_labels={})
    assert g0["grade"] == "F"
    evs = res["events"]; before = [dict(e) for e in evs]
    m1, g1, hit = analysis.what_if(m, evs, {}, ignore_skips=[20.5])            # ±2 s around the skip
    assert hit == {"events": 0, "skips": 1} and m1["skips_back"] == 0 and g1["grade"] != "F"
    ts = [e["t"] for e in evs if e["region"] != "excluded" and e["kind"] in ("click", "pop")]
    assert ts
    m2, g2, hit2 = analysis.what_if(m, evs, {}, ignore_events=[t + 0.002 for t in ts], ignore_skips=[20.0])
    assert hit2 == {"events": len(ts), "skips": 1}
    assert m2["clicks_music_per_min"] + m2["clicks_quiet_per_min"] < m1["clicks_music_per_min"] + m1["clicks_quiet_per_min"]
    assert evs == before and m["skips_back"] == 1 and sk[0]["kind"] == "backward"   # inputs unchanged
    m3, _, hit3 = analysis.what_if(m, evs, {20.0: "run_out"}, ignore_skips=[99.0])     # no skip at 99 s
    assert hit3["skips"] == 0 and m3["skips_back"] == 0                                  # the stored label applies


def test_skip_labels_reclassify_and_recount():
    sk = [{"t": 100.0, "kind": "backward", "position_pct": 50.0}, {"t": 300.0, "kind": "uncertain", "position_pct": 80.0},
          {"t": 1197.2, "kind": "backward", "position_pct": 91.4}]
    out = analysis.apply_skip_labels(sk, {1196.0: "run_out", 300.5: "no_skip"})
    assert [e["kind"] for e in out] == ["backward", "uncertain", "run_out"]
    assert out[2]["label"] == "run_out" and out[2]["kind_auto"] == "backward" and out[0]["label"] is None
    back = analysis.apply_skip_labels(out, {})                       # label removed → skip back again
    assert [e["kind"] for e in back] == ["backward", "uncertain", "backward"]
    audio, _ = synth.make_side(duration_s=40, seed=5, clicks_per_min=30)
    res = analysis.analyze(audio, SR, analysis.AnalysisConfig(second_opinion=False))
    m = dict(res["metrics"]); m["skips"] = sk; m["skips_back"] = 2
    two = analysis.skip_warning([sk[0], sk[2]])
    assert two["$t"] == "analysis.skip_back" and two["$p"]["n"] == 2
    assert i18n.render(two, "en").startswith("Stylus skipped back 2 times (groove repeated): 1:40 (50% into the side), "
                                             "19:57 (91% into the side).")
    # stored by a version before 1.0: German text, replaced like the message
    warns = ["Nadel 2× zurückgesprungen (Rille wiederholt): 1:40 (50 % der Seite), 19:57 (91 % der Seite).", "other"]
    m2, g2 = analysis.recount(m, res["events"], skip_labels={1197.0: "run_out"}, warnings=warns)
    assert m2["skips_back"] == 1 and m2["skips"][2]["kind"] == "run_out"
    assert warns[0] == analysis.skip_warning([sk[0]]) and warns[0]["$p"]["n"] == 1 and warns[1] == "other"
    assert i18n.render(warns[0], "de").startswith("Nadel 1× zurückgesprungen")
    m3, g3 = analysis.recount(m, res["events"], skip_labels={100.0: "no_skip", 1197.0: "run_out"}, warnings=warns)
    assert m3["skips_back"] == 0 and warns == ["other"] and g3["grade"] != "F"
    assert "grading.skipped_back" in [p["$t"] for p in g2["explanation"]["$join"]]
