"""Two different sides must not end up as "clicks removed" (side A compared against side B)."""

import time
from dataclasses import replace

import pytest
import soundfile as sf
from fastapi.testclient import TestClient

from vinylcheck import analysis, compare, i18n, synth
from vinylcheck.config import Config
from vinylcheck.web import create_app

SR = 48000
DE = {"lang": "de"}


@pytest.fixture()
def client(tmp_path):
    audio, _ = synth.make_side(duration_s=40, seed=5, clicks_per_min=30)
    wav = tmp_path / "record.wav"
    sf.write(wav, audio, SR, subtype="PCM_16")
    cfg = replace(Config(), data_dir=tmp_path / "data", state_dir=tmp_path / "state",
                  fake_source=str(wav), fake_speed=200.0, min_free_gb=0.0)
    with TestClient(create_app(cfg)) as c:
        yield c


def _record(client, record_id, kind, side="A"):
    r = client.post("/api/recordings/start", json={"record_id": record_id, "side": side, "kind": kind})
    assert r.status_code == 200, r.text
    rec_id = r.json()["recording_id"]
    deadline = time.time() + 30
    while client.get("/api/status").json()["recorder"]["active"] and time.time() < deadline:
        time.sleep(0.1)
    t0 = time.time()
    while time.time() - t0 < 90:
        rec = client.get(f"/api/recordings/{rec_id}").json()
        if rec["status"] in ("analyzed", "error"):
            return rec
        time.sleep(0.2)
    raise AssertionError(f"Timeout, status {rec['status']}")


def test_unrelated_sides_are_not_comparable():
    a, _ = synth.make_side(duration_s=40, seed=5, clicks_per_min=30)
    b, _ = synth.make_side(duration_s=40, seed=11, clicks_per_min=30)
    res_a, res_b = analysis.analyze(a, SR), analysis.analyze(b, SR)
    r = compare.compare(a, b, SR, res_a, res_b)
    assert r["comparable"] is False and r["reason"] == i18n.msg("compare.no_anchors") and r["match"] is None
    assert "cannot be aligned" in i18n.render(r["reason"], "en")
    assert all(v is None for v in r["delta"].values())
    same = compare.compare(a, a, SR, res_a, res_a)
    assert same["comparable"] is True and same["match"]["counts"]["removed"] == 0


def test_not_comparable_reason():
    assert compare.not_comparable_reason({"ok": True, "anchors": 9, "anchors_used": 5}) is None
    few = compare.not_comparable_reason({"ok": True, "anchors": 4, "anchors_used": 2})
    assert few == i18n.msg("compare.too_few_anchors", used=2, needed=compare.MIN_ANCHORS)
    assert i18n.render(few, "en").startswith("Only 2 of at least 3 required anchors")


def test_api_refuses_different_sides_and_records(client):
    rid = client.post("/api/records", json={"artist": "Test Artist", "title": "Test Album"}).json()["id"]
    a = _record(client, rid, "before", side="A")
    b = _record(client, rid, "before", side="B")
    # German texts: the wording of these API errors is defined in web.py
    r = client.post("/api/comparisons", json={"a_id": a["id"], "b_id": b["id"]}, params=DE)
    assert r.status_code == 409 and "Seite A" in r.json()["detail"] and "Seite B" in r.json()["detail"]
    rid2 = client.post("/api/records", json={"artist": "Test Artist", "title": "Other Album"}).json()["id"]
    c = _record(client, rid2, "before", side="A")
    r = client.post("/api/comparisons", json={"a_id": a["id"], "b_id": c["id"]}, params=DE)
    assert r.status_code == 409 and "verschiedenen Platten" in r.json()["detail"]
