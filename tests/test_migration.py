"""Migration of databases from versions before 1.0: German interface values become the English codes of 1.0."""

import json
import sqlite3

import pytest

from vinylcheck import i18n, legacy
from vinylcheck.db import DB


def _old_database(path):
    """A database with the data shapes of 0.9.x: same schema, but the German identifiers of that time, next to user
    text that happens to contain such words and must stay as it is."""
    DB(path).conn.close()
    con = sqlite3.connect(path)
    con.execute("PRAGMA user_version = 0")
    j = json.dumps
    con.executescript("""
        INSERT INTO records (id, artist, title, notes, created_at) VALUES (1, 'Artist', 'Title', 'musik und knackser', 1);
        INSERT INTO profiles (id, name, notes, target_map, active, created_at) VALUES (1, 'labor', 'pegel', 'labor', 1, 1);
        INSERT INTO setups (id, label, profile_id, created_at) VALUES (1, 'justage', 1, 1);
    """)
    con.execute("INSERT INTO recordings (id, record_id, side, kind, copy_label, findings, notes, status, created_at) "
                "VALUES (1, 1, 'A', 'before', 'musik', ?, 'knackser bei 1:20', 'analyzed', 1)", (j(["kratzer", "welle"]),))
    con.execute("INSERT INTO recordings (id, setup_id, side, kind, status, created_at) "
                "VALUES (2, 1, '2', 'alignment', 'analyzed', 2)")
    con.execute("INSERT INTO copy_findings VALUES (1, 'musik', 'A', ?, 1)", (j(["schmutz", "exzentrisch"]),))
    con.executemany("INSERT INTO events (recording_id, t, dur_ms, strength, amp_dbfs, ch, kind, region, label) "
                    "VALUES (1, ?, 1, 30, -30, 3, 'click', 'music', ?)", [(1.0, "musik"), (2.0, "knackser"), (3.0, None)])
    con.executemany("INSERT INTO event_labels VALUES (1, ?, ?, 1)", [(1.0, "musik"), (2.0, "knackser"), (4.0, "unsicher")])
    con.executemany("INSERT INTO skip_labels VALUES (1, ?, ?, 1)", [(40.0, "endrille"), (90.0, "kein_sprung")])
    floor = {"clicks_per_min": 5.0, "ticks_per_min": 9.0, "source": "stille Rille der Testplatte"}
    metrics = {"skips": [{"t": 40.0, "kind": "endrille", "kind_auto": "rueckwaerts", "label": "endrille"},
                         {"t": 60.0, "kind": "rueckwaerts", "kind_auto": "rueckwaerts", "label": None},
                         {"t": 90.0, "kind": "unsicher", "kind_auto": "unsicher", "label": "kein_sprung"}],
               "floor": floor, "profile": {"floor": floor, "name": "labor"},
               "tracks": [{"pos": "A1", "title": "Ende"}], "counts": {"click": 3}}
    grading = {"grade": "VG", "score": 60, "hinweis": i18n.msg("grading.uncalibrated"),
               "per_metric": {"clicks_music_per_min": {"label": i18n.msg("grading.metric_clicks_music_per_min")}}}
    con.execute("INSERT INTO analyses (recording_id, version, metrics, grading, warnings, overview, grade, score, "
                "analyzed_at) VALUES (1, '0.9.3', ?, ?, ?, ?, 'VG', 60, 1)",
                (j(metrics), j(grading), j(["Nadel 1× zurückgesprungen", i18n.msg("analysis.long_repeat")]),
                 j({"quiet": [[1.0, 2.0]], "groove": [0.5, 100.0]})))
    bands = [{"i": 1, "type": "ton", "mode": "links", "role": "xt_links", "label": "musik"},
             {"i": 2, "type": "sweep", "direction": "ab", "mode": "vertikal", "role": "resonanz"},
             {"i": 3, "type": "stille", "mode": "–", "role": "stille_rille",
              "m": {"lf_peaks": {"lateral": {"freq_hz": 3.0}, "vertikal": {"freq_hz": 11.0}}}},
             {"i": 4, "type": "rauschen", "mode": "gemischt"}]
    results = [{"key": "pegel", "label": "Pegel", "value": -18.0, "status": "ok", "soll": "−24 bis −16 dBFS"},
               {"key": "antiskating", "value": "zu stark", "status": "warn", "soll": "ganze Rampe"}]
    al_metrics = {"kind": "justage", "bands": bands, "results": results, "clip_in_bands": 0,
                  "summary": {"ref_level_dbfs": -18.0}}
    con.execute("INSERT INTO analyses (recording_id, version, metrics, grading, warnings, overview, analyzed_at) "
                "VALUES (2, '0.9.3', ?, ?, '[]', ?, 2)",
                (j(al_metrics), j({"grade": None, "hinweis": None}),
                 j({"bands": [[0.0, 5.0, 1, "xt_links", "Übersprechen"], [5.0, 9.0, 4, "", "–"]]})))
    con.execute("UPDATE profiles SET baseline = ? WHERE id = 1", (j({"setup_id": 1, "summary": {}, "results": results}),))
    master = {"ok": True, "polarity": "gedreht", "master_file": {"name": "Pause.wav"},
              "gaps": {"items": [{"kind": "Anfang"}, {"kind": "Pause"}, {"kind": "Ende"}]},
              "wow": {"segments": [{"label": "Anfang", "wow_pct": 0.1}, {"label": "Mitte"}, {"label": "Ende"}]},
              "freq": {"segments": [{"label": "Ende", "dev_db": 0.5}]},
              "grading": {"grade": "NM", "hinweis": None},
              "clicks": {"top": [{"t": 2.0, "kind": "click", "label": "knackser"}], "events": [[2.0, "v", 0.4]]}}
    con.execute("INSERT INTO master_checks (recording_id, status, result, created_at) VALUES (1, 'done', ?, 1)",
                (j(master),))
    comparison = {"copies": ["musik", ""], "kinds": ["before", "before"],
                  "skips": {"a": [{"t": 60.0, "kind": "rueckwaerts"}], "b": [],
                            "relative": [{"t_a": 10.0, "deutung": i18n.msg("skips.a_skipped_back")}]}}
    con.execute("INSERT INTO comparisons (id, a_id, b_id, status, result, created_at) VALUES (1, 1, 1, 'done', ?, 1)",
                (j(comparison),))
    maps = {"labor": {"name": "My lab map", "note": "", "targets": {"pegel": {"ok": [-24, -16], "warn": [-32, -6]}}},
            "pegel": {"name": "a map the user called pegel", "targets": {"balance_db": {"ok": 1, "warn": 2}}}}
    con.executemany("INSERT INTO settings VALUES (?, ?, 1)",
                    [("target_maps", j(maps)), ("target_map_active", "labor"), ("note", "labor")])
    con.commit()
    con.close()


def _dump(path) -> list[str]:
    con = sqlite3.connect(path)
    try:
        return list(con.iterdump())
    finally:
        con.close()


def test_old_database_is_migrated_once_with_backup(tmp_path):
    path = tmp_path / "vinyl-check.sqlite"
    _old_database(path)
    db = DB(path)
    q = lambda sql: db.conn.execute(sql).fetchall()                                   # noqa: E731
    assert len(list(tmp_path.glob("vinyl-check.pre-1.0.0-*.sqlite"))) == 1
    assert q("PRAGMA user_version")[0][0] == 1

    # Judgements in their own tables and on the events
    assert dict(q("SELECT t, label FROM event_labels")) == {1.0: "music", 2.0: "defect", 4.0: "unsure"}
    assert dict(q("SELECT t, label FROM events")) == {1.0: "music", 2.0: "defect", 3.0: None}
    assert dict(q("SELECT t, label FROM skip_labels")) == {40.0: "run_out", 90.0: "no_skip"}
    # Findings per recording (old column) and per copy and side
    assert db.recording(1)["findings"] == ["dirt", "off_center"]
    assert json.loads(q("SELECT findings FROM recordings WHERE id = 1")[0][0]) == ["scratch", "warp"]

    a = db.recording(1)["analysis"]
    m = a["metrics"]
    assert [(s["kind"], s["kind_auto"], s["label"]) for s in m["skips"]] == [
        ("run_out", "backward", "run_out"), ("backward", "backward", None), ("uncertain", "uncertain", "no_skip")]
    assert m["floor"]["source"] == m["profile"]["floor"]["source"] == "test_record_silent_groove"
    assert a["grading"]["explanation"] == i18n.msg("grading.uncalibrated") and "hinweis" not in a["grading"]
    assert a["warnings"][0] == "Nadel 1× zurückgesprungen"                            # stored text stays as it is

    al = db.recording(2)["analysis"]["metrics"]
    assert al["kind"] == "alignment"
    assert [(b.get("type"), b.get("mode"), b.get("role"), b.get("direction")) for b in al["bands"]] == [
        ("tone", "left", "xt_left", None), ("sweep", "vertical", "resonance", "down"),
        ("silence", "–", "silent_groove", None), ("noise", "mixed", None, None)]
    assert al["bands"][0]["label"] == "musik"                                         # a label is text, not a value
    assert al["bands"][2]["m"]["lf_peaks"] == {"lateral": {"freq_hz": 3.0}, "vertical": {"freq_hz": 11.0}}
    res = {r["key"]: r for r in al["results"]}
    assert set(res) == {"ref_level", "antiskating"} and res["antiskating"]["value"] == "too_strong"
    assert res["ref_level"]["target"] == "−24 bis −16 dBFS" and "soll" not in res["ref_level"]
    assert json.loads(q("SELECT overview FROM analyses WHERE recording_id = 2")[0][0])["bands"] == [
        [0.0, 5.0, 1, "xt_left", "Übersprechen"], [5.0, 9.0, 4, "", "–"]]
    run = db.setup_recordings(1)
    assert run[0]["is_alignment"] and run[0]["clip_in_bands"] == 0

    mc = db.master_check(1)["result"]
    assert mc["polarity"] == "inverted" and [g["kind"] for g in mc["gaps"]["items"]] == ["start", "gap", "end"]
    assert mc["clicks"]["top"][0]["label"] == "defect" and mc["clicks"]["events"] == [[2.0, "v", 0.4]]
    assert mc["master_file"]["name"] == "Pause.wav" and "explanation" in mc["grading"]
    assert [s["label"] for s in mc["wow"]["segments"]] == [i18n.msg("master.part_start"), i18n.msg("master.part_middle"),
                                                           i18n.msg("master.part_end")]
    assert mc["freq"]["segments"] == [{"label": i18n.msg("master.part_end"), "dev_db": 0.5}]
    cmp = db.comparison(1)["result"]
    assert cmp["skips"]["a"][0]["kind"] == "backward" and cmp["copies"] == ["musik", ""]
    assert cmp["skips"]["relative"][0]["interpretation"] == i18n.msg("skips.a_skipped_back")

    p = db.profile(1)
    assert p["target_map"] == "lab" and p["name"] == "labor" and p["notes"] == "pegel"
    assert json.loads(p["baseline"])["results"][0]["key"] == "ref_level"
    st = db.get_settings()
    assert st["target_map_active"] == "lab" and st["note"] == "labor"
    maps = json.loads(st["target_maps"])
    assert set(maps) == {"lab", "pegel"} and set(maps["lab"]["targets"]) == {"ref_level"}
    assert maps["pegel"]["name"] == "a map the user called pegel"                    # a user's map id stays

    # User text everywhere stays as it is
    assert db.record(1)["notes"] == "musik und knackser" and db.setup(1)["label"] == "justage"
    r = db.recording(1)
    assert r["copy_label"] == "musik" and r["notes"] == "knackser bei 1:20"
    assert m["tracks"][0]["title"] == "Ende" and m["profile"]["name"] == "labor"

    # Opening again does nothing (no second backup); running the step again changes nothing
    db.conn.close()
    before = _dump(path)
    db = DB(path)
    assert len(list(tmp_path.glob("vinyl-check.pre-1.0.0-*.sqlite"))) == 1
    db.conn.execute("PRAGMA user_version = 0")
    db._migrate_1_0(path)
    db.conn.close()
    assert _dump(path) == before


def test_new_database_needs_no_backup(tmp_path):
    path = tmp_path / "vinyl-check.sqlite"
    db = DB(path)
    assert db.conn.execute("PRAGMA user_version").fetchone()[0] == 1
    assert not list(tmp_path.glob("*.pre-*.sqlite"))


def test_upgrade_is_idempotent_and_by_context():
    old = {"label": "musik", "kind": "unsicher", "notes": "musik", "findings": ["kratzer", "own text"],
           "skips": [{"kind": "unsicher", "label": "kein_sprung"}], "msg": i18n.msg("x.y", kind="Pause"),
           "items": [{"key": "antiskating", "value": "ausgeglichen"}, {"key": "speed_pct", "value": "zu stark"}]}
    new = legacy.upgrade(old)
    assert new == {"label": "music", "kind": "uncertain", "notes": "musik", "findings": ["scratch", "own text"],
                   "skips": [{"kind": "uncertain", "label": "no_skip"}], "msg": i18n.msg("x.y", kind="Pause"),
                   "items": [{"key": "antiskating", "value": "balanced"}, {"key": "speed_pct", "value": "zu stark"}]}
    assert legacy.upgrade(new) == new
    new_values = {v for table in legacy.VALUES.values() for v in table.values()}
    old_values = {k for table in legacy.VALUES.values() for k in table}
    assert not new_values & old_values                                               # nothing is renamed twice


def test_old_config_section_is_still_read(tmp_path, caplog):
    from vinylcheck import config
    f = tmp_path / "config.toml"
    f.write_text("[justage]\nthd_limit_pct = 2.5\n")
    with caplog.at_level("WARNING", logger="vinylcheck.config"):
        assert config.load(str(f)).alignment.thd_limit_pct == 2.5
    assert "[alignment]" in caplog.text
    f.write_text("[alignment]\nthd_limit_pct = 4.0\n[justage]\nthd_limit_pct = 2.5\n")
    assert config.load(str(f)).alignment.thd_limit_pct == 4.0                       # the new section wins


def test_example_config_loads(tmp_path):
    """config.example.toml with every documented key switched on is a valid configuration."""
    import re
    from pathlib import Path

    from vinylcheck import config
    example = Path(__file__).resolve().parents[1] / "config.example.toml"
    if not example.is_file():                                   # deployments copy only vinylcheck/ and tests/
        pytest.skip("config.example.toml not present")
    text = example.read_text(encoding="utf-8")
    on = "\n".join(re.sub(r"^# ", "", ln) if re.match(r"^# (\[[a-z]+\]|[a-z_]+ = )", ln) else ln
                   for ln in text.splitlines())
    f = tmp_path / "config.toml"
    f.write_text(on, encoding="utf-8")
    cfg = config.load(str(f))
    assert cfg.grading.graded == ("clicks_music_per_min", "pops_per_min") and cfg.alignment.thd_limit_pct == 3.0
