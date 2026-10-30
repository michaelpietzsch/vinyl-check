"""Guided first alignment: test-record catalogue, describing another record, the verdict, the tutorial's state."""

import time
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from vinylcheck import i18n, onboarding, testrecords
from vinylcheck.config import Config
from vinylcheck.db import DB
from vinylcheck.web import create_app


def _cfg(tmp_path):
    return replace(Config(), data_dir=tmp_path / "data", state_dir=tmp_path / "state", min_free_gb=0.0)


@pytest.fixture()
def client(tmp_path):
    with TestClient(create_app(_cfg(tmp_path))) as c:
        yield c


def _item(key, status="ok", value=1.0, unit="dB"):
    return {"key": key, "label": key, "value": value, "unit": unit, "status": status, "target": "", "text": ""}


AAPT = testrecords.get(None, "aapt1")
# A value within the standard targets for every finding (stored findings are judged again against the targets)
OK_VALUES = {"ref_level": (-20.0, "dBFS"), "balance_db": (0.2, "dB"), "crosstalk_lr_db": (-30.0, "dB"),
             "crosstalk_rl_db": (-29.0, "dB"), "azimuth_asym_db": (1.0, "dB"), "mono_cancel_db": (-25.0, "dB"),
             "speed_pct": (0.1, "%"), "wf_weighted_pct": (0.05, "%"), "antiskating": ("ok", ""),
             "tracking_reserve_db": (11.5, "dB"), "hf_10k_db": (0.5, "dB"), "sweep_hf_dev_db": (0.5, "dB"),
             "sweep_lf_dev_db": (0.5, "dB"), "lf_100_db": (0.3, "dB"), "resonance_hz": (10.0, "Hz"),
             "imd_pct": (1.0, "%"), "rumble_db": (-60.0, "dB"), "floor_clicks_per_min": (5.0, "/min")}
ALL_OK = [_item(k, "ok", *OK_VALUES[k]) for c in testrecords.CHECKS.values() for k in c]


# ─── Catalogue ──────────────────────────────────────────────────────────────
def test_catalogue_is_consistent():
    for rid, r in testrecords.BUILTIN.items():
        assert r["support"] in testrecords.SUPPORT, rid
        assert all(c in testrecords.CHECKS for side in r["layout"].values() for c in side), rid
        assert (r["support"] == "none") == (not r["layout"]), rid          # nothing to record ↔ not measurable
        assert i18n.t(r["note"]["$t"], "de") != r["note"]["$t"], rid      # the note is in the catalogs
    assert list(testrecords.BUILTIN)[0] == testrecords.DEFAULT == "aapt1"
    assert AAPT["sides"] == ["1", "2"] and AAPT["support"] == "verified" and AAPT["reference"]
    assert set(AAPT["checks"]) == set(testrecords.CHECKS)                   # the AAPT 1 has every kind of band
    assert testrecords.expected(AAPT) >= {"ref_level", "antiskating", "speed_pct", "rumble_db"}
    assert testrecords.expected(testrecords.get(None, "unitra_wt2")) == set()   # only verified records are expected
    assert not testrecords.get(None, "ortofon")["reference"] and not testrecords.usable(testrecords.get(None, "hifinews"))
    assert testrecords.get(None, None)["id"] == "aapt1" and testrecords.get(None, "nope") is None


def test_list_and_describe_another_record(client):
    c = client
    tr = c.get("/api/testrecords").json()
    assert tr["default"] == "aapt1" and tr["records"][0]["id"] == "aapt1" and tr["checks"] == list(testrecords.CHECKS)
    assert all(not r["custom"] for r in tr["records"])
    body = {"name": "  My   test LP ", "maker": "Shelf", "rpm": 33.3, "sides": 1,
            "checks": ["speed", "reference", "azimuth"], "note": "from the flea market"}
    r = c.post("/api/testrecords", json=body).json()
    assert r["id"] == "custom-1" and r["name"] == "My test LP" and r["custom"] and r["sides"] == ["1"]
    assert r["checks"] == ["reference", "azimuth", "speed"] and r["support"] == "partial" and r["layout"] is None
    assert r["reference"] and "tracking" in r["missing_checks"]
    assert c.post("/api/testrecords", json={**body, "name": "Second"}).json()["id"] == "custom-2"
    for bad in ({"name": ""}, {"name": "x" * 81}, {"name": "x", "rpm": 78}, {"name": "x", "sides": 3},
                {"name": "x", "checks": ["magic"]}, {"name": "x", "checks": "speed"}):
        assert c.post("/api/testrecords", json=bad).status_code == 422, bad
    assert c.put("/api/testrecords/aapt1", json=body).status_code == 409                # built-in records are fixed
    assert c.put("/api/testrecords/custom-9", json=body).status_code == 404
    up = c.put("/api/testrecords/custom-1", json={**body, "checks": [], "rpm": 45}).json()
    assert up["support"] == "none" and up["rpm"] == 45
    names = [x["name"] for x in c.get("/api/testrecords").json()["records"] if x["custom"]]
    assert names == ["My test LP", "Second"]
    # a record in use by a profile cannot be deleted
    p = c.post("/api/profiles", json={"name": "Deck", "test_record": "custom-2"}).json()
    assert p["test_record"] == "custom-2" and p["test_record_info"]["name"] == "Second"
    assert c.delete("/api/testrecords/custom-2").status_code == 409
    assert c.delete("/api/testrecords/aapt1").status_code == 409
    assert c.delete("/api/testrecords/custom-1").status_code == 200
    assert c.delete("/api/testrecords/custom-1").status_code == 404


def test_profile_takes_the_sides_of_its_test_record(client):
    c = client
    p = c.post("/api/profiles", json={"name": "Deck"}).json()
    assert p["test_record"] is None and p["test_record_info"]["id"] == "aapt1"          # empty = the AAPT 1
    assert p["progress"]["sides"] == ["1", "2"] and p["progress"]["next"] == "calibration"
    q = c.post("/api/profiles", json={"name": "Second deck", "test_record": "unitra_wt2"}).json()
    assert q["progress"]["sides"] == ["C", "D", "B"] and "sideC" in q["progress"] and "side1" not in q["progress"]
    assert c.post("/api/profiles", json={"name": "x", "test_record": "nope"}).status_code == 422
    assert c.post("/api/profiles", json={"name": "x", "test_record": "hifinews"}).status_code == 422   # not measurable
    assert c.patch(f"/api/profiles/{q['id']}", json={"test_record": "clearaudio_smart"}).json()["progress"]["sides"] == ["A"]
    assert c.patch(f"/api/profiles/{q['id']}", json={"test_record": ""}).json()["test_record"] is None
    run = c.post(f"/api/profiles/{q['id']}/run").json()["id"]
    assert c.get(f"/api/setups/{run}").json()["test_record"] == "AAPT 1"


# ─── Verdict ────────────────────────────────────────────────────────────────
def test_readiness_verdicts():
    r = onboarding.readiness(ALL_OK, AAPT, calibrated=True)
    assert r["verdict"] == "ready" and r["items"] == [] and r["missing"] == [] and r["not_covered"] == []
    # only notes (frequency response, rumble): still ready, listed as notes
    r = onboarding.readiness([*ALL_OK[:-1], _item("rumble_db", "bad")], AAPT, calibrated=True)
    assert r["verdict"] == "ready" and r["items"][0]["level"] == "info" and r["items"][0]["guide"] == "rumble"
    # a should figure off its target: good enough, with notes
    items = [it if it["key"] != "azimuth_asym_db" else _item("azimuth_asym_db", "warn") for it in ALL_OK]
    r = onboarding.readiness(items, AAPT, calibrated=True)
    assert r["verdict"] == "ready_notes" and r["items"][0]["guide"] == "azimuth" and r["items"][0]["side"] == "1"
    # a must figure bad: adjust first; sorted must → should → info
    items = [_item("speed_pct", "bad", 1.6, "%") if it["key"] == "speed_pct" else
             _item("balance_db", "warn") if it["key"] == "balance_db" else
             _item("lf_100_db", "bad") if it["key"] == "lf_100_db" else it for it in ALL_OK]
    r = onboarding.readiness(items, AAPT, calibrated=True)
    assert r["verdict"] == "adjust" and [x["key"] for x in r["items"]] == ["speed_pct", "balance_db", "lf_100_db"]
    assert r["guides"] == ["speed", "balance", "phono_eq"]
    # both channels breaking up together points to the tracking force, not the anti-skating
    items = [_item("antiskating", "warn", "balanced", "") if it["key"] == "antiskating" else it for it in ALL_OK]
    assert onboarding.readiness(items, AAPT, calibrated=True)["items"][0]["guide"] == "tracking_force"
    # a USB turntable's level cannot be changed: a level off target is advice, not a must
    items = [_item("ref_level", "bad", -30.0, "dBFS") if it["key"] == "ref_level" else it for it in ALL_OK]
    assert onboarding.readiness(items, AAPT, calibrated=True)["verdict"] == "adjust"
    r = onboarding.readiness(items, AAPT, calibrated=True, fixed_gain=True)
    assert r["verdict"] == "ready" and r["items"][0]["level"] == "info"


def test_readiness_incomplete_missing_and_not_covered():
    assert onboarding.readiness(ALL_OK, AAPT, calibrated=False)["verdict"] == "incomplete"
    r = onboarding.readiness([it for it in ALL_OK if it["key"] != "ref_level"], AAPT, calibrated=True)
    assert r["verdict"] == "incomplete" and not r["has_reference"]
    # a verified record names what it should have measured
    r = onboarding.readiness([it for it in ALL_OK if it["key"] not in ("antiskating", "tracking_reserve_db")], AAPT,
                             calibrated=True)
    assert [m["key"] for m in r["missing"]] == ["antiskating", "tracking_reserve_db"] and r["missing"][0]["side"] == "2"
    assert r["verdict"] == "remeasure"                                    # a must figure is missing: record side 2 again
    r = onboarding.readiness([it for it in ALL_OK if it["key"] != "resonance_hz"], AAPT, calibrated=True)
    assert r["verdict"] == "ready_notes" and r["missing"][0]["key"] == "resonance_hz"
    r = onboarding.readiness([it for it in ALL_OK if it["key"] != "rumble_db"], AAPT, calibrated=True)
    assert r["verdict"] == "ready" and r["missing"][0]["key"] == "rumble_db"
    # a record without a reference tone cannot be the reference; what it cannot measure has a guide
    ortofon = testrecords.get(None, "ortofon")
    r = onboarding.readiness(ALL_OK, ortofon, calibrated=True)
    assert r["verdict"] == "incomplete" and r["record_has_reference"] is False and r["missing"] == []
    covered = {x["check"]: x["guide"] for x in r["not_covered"]}
    assert covered["tracking"] == "antiskating" and covered["speed"] == "speed" and "azimuth" not in covered


def test_every_guide_has_its_texts():
    ids = set(onboarding.GUIDES)
    assert set(onboarding.GUIDE_FOR.values()) <= ids and set(onboarding.GUIDE_FOR_CHECK.values()) <= ids
    assert set(onboarding.BASICS) <= ids
    for lang in i18n.languages():
        cat = i18n.catalog(lang, web=True)
        for g in ids:
            for part in ("title", "why", "s1", "check"):
                assert f"guides.{g}.{part}" in cat, (lang, g, part)
            steps = [k for k in cat if k.startswith(f"guides.{g}.s")]
            assert steps == [f"guides.{g}.s{i}" for i in range(1, len(steps) + 1)], (lang, g)   # s1 … sN, no gaps
        for c in testrecords.CHECKS:
            assert f"testrecords.check_{c}" in cat and f"testrecords.band_{c}" in cat
        for v in ("ready", "ready_notes", "adjust", "remeasure", "incomplete"):
            assert f"tour.verdict_{v}" in cat and f"tour.verdict_{v}_text" in cat and f"tour.done_{v}_text" in cat


# ─── Tutorial state ─────────────────────────────────────────────────────────
def test_onboarding_state(client):
    c = client
    o = c.get("/api/onboarding").json()
    assert o["state"] == "open" and o["offer"] and o["pending"] is None and o["guides"] == list(onboarding.GUIDES)
    p = c.post("/api/profiles", json={"name": "Deck"}).json()
    assert c.get("/api/onboarding").json()["pending"] == {"id": p["id"], "name": "Deck"}
    assert c.post("/api/onboarding", json={"state": "maybe"}).status_code == 422
    o = c.post("/api/onboarding", json={"state": "skipped"}).json()
    assert o["state"] == "skipped" and not o["offer"]
    assert c.post("/api/onboarding", json={"state": "open"}).json()["offer"]


def _seed_run(cfg, results, *, test_record=None, ref=-20.0):
    """A profile whose wizard run has both sides analysed with `results` (no audio), and a chain calibration."""
    db = DB(cfg.db_path)
    pid = db.add_profile(name="Deck", test_record=test_record)
    db.add_recording(kind="calibration", side="-", profile_id=pid, chain_db=-90.0, status="analyzed")
    sid = db.add_setup(profile_id=pid, label="run", test_record="AAPT 1")
    db.update_profile(pid, wizard_setup_id=sid)
    for side in testrecords.of_profile(db, db.profile(pid))["sides"]:
        rid = db.add_recording(kind="alignment", setup_id=sid, side=side, profile_id=pid, status="analyzed")
        metrics = {"kind": "alignment", "bands": [], "results": results,
                   "summary": {"ref_level_dbfs": ref, "speed_pct": 0.1}}
        db.save_analysis(rid, {"metrics": metrics, "grading": {"grade": None, "score": None}, "warnings": [],
                               "overview": {}, "events": []}, "test", 10.0)
        time.sleep(0.01)                                           # recordings are ordered by their time
    db.conn.close()
    return pid


def test_accepting_the_first_turntable_ends_the_tour(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.state_dir.mkdir(parents=True)
    results = [it if it["key"] != "speed_pct" else _item("speed_pct", "bad", 1.6, "%") for it in ALL_OK]
    pid = _seed_run(cfg, results)
    with TestClient(create_app(cfg)) as c:
        p = c.get(f"/api/profiles/{pid}").json()
        assert p["progress"]["next"] == "accept"
        rd = p["readiness"]
        assert rd["verdict"] == "adjust" and rd["items"][0]["key"] == "speed_pct" and rd["items"][0]["side"] == "1"
        assert c.get("/api/onboarding").json()["offer"]
        done = c.post(f"/api/profiles/{pid}/accept", json={}).json()
        assert done["progress"]["next"] == "done" and done["readiness"]["verdict"] == "adjust"   # from the reference
        o = c.get("/api/onboarding").json()
        assert o["state"] == "done" and not o["offer"] and o["accepted"]


def test_a_record_without_reference_cannot_be_accepted(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.state_dir.mkdir(parents=True)
    pid = _seed_run(cfg, ALL_OK, test_record="ortofon")
    with TestClient(create_app(cfg)) as c:
        p = c.get(f"/api/profiles/{pid}").json()
        assert p["progress"]["sides"] == ["A"] and p["readiness"]["verdict"] == "incomplete"
        r = c.post(f"/api/profiles/{pid}/accept", json={})
        assert r.status_code == 409 and "reference" in r.json()["detail"]
        assert c.get("/api/onboarding").json()["state"] == "open"
