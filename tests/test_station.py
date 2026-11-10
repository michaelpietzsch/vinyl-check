"""The Station without the web interface around it: changing a stored analysis in one place, so that two changes
never overwrite each other and nobody reads half of one."""

import json
import threading
from dataclasses import replace

import numpy as np
import pytest
import soundfile as sf

from vinylcheck import analysis, synth
from vinylcheck.config import Config
from vinylcheck.station import Station

SR = 48000
SKIP = {"t": 10.0, "until": 12.0, "kind": "backward", "kind_auto": "backward", "grooves": 1, "repeats": 1,
        "period_s": 1.8, "rpm_measured": 33.333, "score": 0.9, "position_pct": 20.0, "label": None}


@pytest.fixture(scope="module")
def side():
    audio, _ = synth.make_side(duration_s=40, seed=5, clicks_per_min=30)
    return audio, analysis.analyze(audio, SR)


@pytest.fixture()
def station(tmp_path, side):
    """A station with one analysed recording (`station.rid`); nothing of it is started (no worker thread, no app)."""
    cfg = replace(Config(), data_dir=tmp_path / "data", state_dir=tmp_path / "state", min_free_gb=0.0)
    st = Station(cfg)
    wav = tmp_path / "side.wav"
    sf.write(wav, side[0], SR, subtype="PCM_16")
    st.rid = st.db.add_recording(kind="before", side="A", path=str(wav), status="analyzed")
    st.db.save_analysis(st.rid, json.loads(json.dumps(side[1])), "test", 1.0)
    return st


def stored(st) -> dict:
    return st.db.recording(st.rid)["analysis"]


def test_recount_stores_a_change_and_its_count_in_one_write(station, monkeypatch):
    """New hits and the count that follows from them arrive together: while the count is computed, the stored
    analysis is still the old one."""
    assert stored(station)["metrics"]["skips_back"] == 0 and stored(station)["grading"]["grade"] != "F"
    real, seen, writes = analysis.recount, [], []

    def recount(metrics, *args, **kw):
        seen.append(stored(station)["metrics"]["skips"])           # what a reader would get at this moment
        return real(metrics, *args, **kw)
    monkeypatch.setattr(analysis, "recount", recount)
    monkeypatch.setattr(station.db, "update_analysis",
                        lambda *a, _w=station.db.update_analysis, **k: (writes.append(1), _w(*a, **k))[1])
    m, g = station.recount(station.rid, change=lambda cur: cur.update(skips=[dict(SKIP)]))
    assert seen == [[]] and writes == [1]
    a = stored(station)
    assert a["metrics"]["skips_back"] == 1 == m["skips_back"] and a["grading"]["grade"] == "F" == g["grade"]
    assert any("skip" in json.dumps(w) for w in a["warnings"])     # the warning came in the same write


def test_recount_without_counting_and_without_change(station):
    before = stored(station)
    assert station.recount(station.rid, change=lambda cur: False) is None            # the change declined
    assert stored(station) == before
    m, g = station.recount(station.rid, change=lambda cur: cur.update(mark=1), count=False)
    after = stored(station)
    assert after["metrics"]["mark"] == 1 and after["grading"] == before["grading"] == g
    assert {k: v for k, v in after["metrics"].items() if k != "mark"} == before["metrics"]
    assert station.recount(station.rid + 1) is None                                  # no such recording
    station.db.x("UPDATE analyses SET metrics = ? WHERE recording_id = ?", (json.dumps({"old": True}), station.rid))
    assert station.recount(station.rid) is None                                      # nothing in it to count
    assert stored(station)["metrics"] == {"old": True}


def test_a_recount_does_not_write_over_a_fresh_analysis(station, side, monkeypatch):
    """A verdict is counted while the worker finishes analysing the same recording again: the fresh analysis must
    be what stays. Without the lock the recount, computed from what it read before, would be written over it."""
    st = station
    real, entered, release = analysis.recount, threading.Event(), threading.Event()

    def slow(metrics, *args, **kw):
        entered.set()
        assert release.wait(20)
        return real(metrics, *args, **kw)
    monkeypatch.setattr(analysis, "recount", slow)
    fresh = json.loads(json.dumps(side[1]))
    fresh["metrics"]["fresh"] = True
    st.worker.run_heavy = lambda *a, **k: json.loads(json.dumps(fresh))           # the analysis itself, done at once
    counting = threading.Thread(target=st.recount, args=(st.rid,))
    counting.start()
    assert entered.wait(20)
    analysing = threading.Thread(target=st.worker._analyze, args=(st.rid,))
    analysing.start()
    analysing.join(1.0)
    assert analysing.is_alive() and "fresh" not in stored(st)["metrics"]          # it waits for the recount
    release.set()
    counting.join(20)
    analysing.join(20)
    assert not counting.is_alive() and not analysing.is_alive()
    assert stored(st)["metrics"].get("fresh") is True and st.db.recording(st.rid)["status"] == "analyzed"


def test_regrade_goes_through_the_same_place(station, monkeypatch):
    calls = []
    monkeypatch.setattr(station, "recount", lambda rec_id, *a, _r=station.recount, **k: (calls.append(rec_id), _r(rec_id, *a, **k))[1])
    grade = stored(station)["grading"]["grade"]
    assert station.regrade_all(force=True) == 1 and calls == [station.rid]
    assert stored(station)["grading"]["grade"] == grade
    assert isinstance(np.asarray(stored(station)["metrics"]["counts"]["click"]).item(), int)
