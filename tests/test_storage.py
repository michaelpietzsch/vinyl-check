"""The user-data folder (storage.py): checking a target, moving with verified copies, gathering files left outside,
relinking, cancelling, and the guards while a move runs."""
import json
import os
import shutil
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from vinylcheck import i18n, storage
from vinylcheck.config import Config
from vinylcheck.db import DB
from vinylcheck.web import create_app


def keys(msgs):
    return [m["$t"] for m in msgs]


def wait(mover, timeout=20.0):
    end = time.monotonic() + timeout
    while mover.active and time.monotonic() < end:
        time.sleep(0.02)
    assert not mover.active


@pytest.fixture()
def station(tmp_path):
    """A user-data folder with two recordings and a master, and the database rows that point at them."""
    cfg = replace(Config(), data_dir=tmp_path / "old", state_dir=tmp_path / "state", min_free_gb=0.0)
    cfg.state_dir.mkdir()
    db = DB(cfg.db_path)
    rid = db.add_record("Artist", "Title")
    paths = {}
    for name, size in (("00001-A-before.wav", 3000), ("00002-B-before.wav", 5000)):
        p = cfg.rec_dir / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(bytes(range(256)) * (size // 256) + b"x" * (size % 256))
        paths[name] = db.add_recording(record_id=rid, side=name[6], kind="before", path=str(p))
    m = cfg.data_dir / "masters" / "1" / "A.flac"
    m.parent.mkdir(parents=True)
    m.write_bytes(b"master" * 100)
    db.set_master(rid, "A", name="A.flac", path=str(m), bytes=600)
    return cfg, db, rid


def test_check_refuses_what_cannot_work(station, tmp_path):
    cfg, db, _ = station
    assert keys(storage.check(cfg, db, "relative/path")["problems"]) == ["storage.not_absolute"]
    assert keys(storage.check(cfg, db, str(cfg.data_dir))["problems"]) == ["storage.same"]
    assert keys(storage.check(cfg, db, str(cfg.data_dir / "inner"))["problems"]) == ["storage.nested"]
    assert keys(storage.check(cfg, db, str(tmp_path))["problems"]) == ["storage.nested"]
    (tmp_path / "file").write_text("x")
    assert keys(storage.check(cfg, db, str(tmp_path / "file"))["problems"]) == ["storage.not_a_folder"]
    clash = tmp_path / "new" / "recordings" / "00001-A-before.wav"
    clash.parent.mkdir(parents=True)
    clash.write_bytes(b"other")
    assert keys(storage.check(cfg, db, str(tmp_path / "new"))["problems"]) == ["storage.conflicts"]
    big = replace(cfg, min_free_gb=1e9)
    assert "storage.no_space" in keys(storage.check(big, db, str(tmp_path / "other"))["problems"])
    assert keys(storage.check(cfg, db, str(tmp_path / "other"), relink=True)["problems"]) == ["storage.relink_missing"]


def test_check_counts_what_will_be_copied(station, tmp_path):
    cfg, db, _ = station
    r = storage.check(cfg, db, str(tmp_path / "new"))
    assert r["ok"] and r["files"] == 3 and r["bytes"] == 3000 + 5000 + 600 and r["already"] == 0
    assert r["path"] == str(tmp_path / "new") and not r["gather"]


def test_move_copies_verifies_switches_and_deletes_the_originals(station, tmp_path):
    cfg, db, _ = station
    old, new = cfg.data_dir, tmp_path / "new"
    before = {p.name: p.read_bytes() for p in old.rglob("*") if p.is_file()}
    mover = storage.Mover(cfg, db)
    st = mover.start(str(new))
    assert st["kind"] == "move" and st["files_total"] == 3
    wait(mover)
    st = mover.status()
    assert st["phase"] == "done" and st["pct"] == 100.0 and st["files_done"] == 3 and st["kept"] == 0
    assert st["bytes_done"] == st["bytes_total"] == 8600
    after = {p.name: p.read_bytes() for p in new.rglob("*") if p.is_file()}
    assert after == before                                         # same files, same content, no .part left
    assert not any(p.is_file() for p in old.rglob("*"))            # originals deleted, empty folders removed
    assert not (old / "recordings").exists() and not (old / "masters").exists()
    assert cfg.data_dir == new and cfg.rec_dir == new / "recordings"
    assert all(p.startswith(str(new) + os.sep) for p in storage.referenced(db))
    assert db.setting(storage.S_DATA_DIR) == str(new)
    # a restart: the choice in the database wins over the configuration file
    fresh = replace(Config(), data_dir=old, state_dir=cfg.state_dir)
    storage.apply_saved(fresh, db)
    assert fresh.data_dir == new
    info = storage.info(fresh, db, storage.Mover(fresh, db))
    assert info["data"]["source"] == "settings" and info["data"]["usage"]["recordings"]["files"] == 2
    assert info["move"]["phase"] == "done"


def test_move_can_keep_the_originals(station, tmp_path):
    cfg, db, _ = station
    old = cfg.data_dir
    mover = storage.Mover(cfg, db)
    mover.start(str(tmp_path / "new"), delete_originals=False)
    wait(mover)
    assert mover.status()["phase"] == "done"
    assert len(storage.files(old)) == 3 and len(storage.files(tmp_path / "new")) == 3


def test_resumed_move_checks_instead_of_copying_again(station, tmp_path, monkeypatch):
    cfg, db, _ = station
    new = tmp_path / "new"
    (new / "recordings").mkdir(parents=True)
    (new / "recordings" / "00001-A-before.wav").write_bytes((cfg.rec_dir / "00001-A-before.wav").read_bytes())
    assert storage.check(cfg, db, str(new))["already"] == 1
    copied = []
    real = storage.copy_verified
    monkeypatch.setattr(storage, "copy_verified", lambda s, d, *a: (copied.append(d.name), real(s, d, *a)))
    mover = storage.Mover(cfg, db)
    mover.start(str(new))
    wait(mover)
    assert mover.status()["phase"] == "done" and sorted(copied) == ["00002-B-before.wav", "A.flac"]


def test_files_outside_are_gathered(station, tmp_path):
    """The folder was changed in the configuration file: the old recordings stay referenced where they are, and
    moving to the folder in use brings them in."""
    cfg, db, _ = station
    old, new = cfg.data_dir, tmp_path / "ssd"
    object.__setattr__(cfg, "data_dir", new)
    info = storage.info(cfg, db, storage.Mover(cfg, db))
    assert info["data"]["outside"] | {"folders": None} == {"files": 3, "bytes": 8600, "missing": 0, "folders": None,
                                                           "recordings": 2, "masters": 1}
    assert sorted(info["data"]["outside"]["folders"]) == [str(old / "masters" / "1"), str(old / "recordings")]
    r = storage.check(cfg, db, str(new))
    assert r["ok"] and r["gather"] and r["files"] == r["outside_files"] == 3
    mover = storage.Mover(cfg, db)
    assert mover.start(str(new))["kind"] == "gather"
    wait(mover)
    assert mover.status()["phase"] == "done"
    assert sorted(storage.referenced(db)) == sorted(str(new / rel) for rel, _ in storage.files(new))
    assert {rel for rel, _ in storage.files(new)} == {"recordings/00001-A-before.wav", "recordings/00002-B-before.wav",
                                                      "masters/1/A.flac"}
    assert not (old / "recordings").exists() and not (old / "masters").exists() and old.is_dir()
    assert db.setting(storage.S_DATA_DIR) is None                  # nothing to remember: the folder did not change
    assert keys(storage.check(cfg, db, str(new))["problems"]) == ["storage.same"]   # nothing left to gather


def test_a_move_takes_the_files_from_outside_along(station, tmp_path):
    cfg, db, _ = station
    object.__setattr__(cfg, "data_dir", tmp_path / "second")
    (cfg.rec_dir).mkdir(parents=True)
    (cfg.rec_dir / "00003-A-after.wav").write_bytes(b"y" * 700)
    db.add_recording(record_id=1, side="A", kind="after_wet", path=str(cfg.rec_dir / "00003-A-after.wav"))
    db.add_recording(record_id=1, side="B", kind="after_wet", path=str(tmp_path / "missing" / "x.wav"))
    r = storage.check(cfg, db, str(tmp_path / "third"))
    assert r["ok"] and r["files"] == 4 and r["outside_files"] == 3
    # on a machine whose /tmp is its own mount (a tmpfs on a Raspberry Pi) the target also warns that its drive
    # must be mounted; that is right, and not what this test is about
    assert "storage.outside_missing" in keys(r["warnings"])
    assert set(keys(r["warnings"])) <= {"storage.outside_missing", "storage.drive_required"}
    mover = storage.Mover(cfg, db)
    mover.start(str(tmp_path / "third"))
    wait(mover)
    paths = storage.referenced(db)
    assert sum(p.startswith(str(tmp_path / "third") + os.sep) for p in paths) == 4
    assert str(tmp_path / "missing" / "x.wav") in paths              # cannot be found: stays as it is


def test_cancel_leaves_the_station_as_it_was(station, tmp_path, monkeypatch):
    cfg, db, _ = station
    old = cfg.data_dir
    mover = storage.Mover(cfg, db)
    real = storage.copy_verified

    def copy_then_cancel(s, d, step, cancelled):
        mover.cancel()
        return real(s, d, step, cancelled)
    monkeypatch.setattr(storage, "copy_verified", copy_then_cancel)
    mover.start(str(tmp_path / "new"))
    wait(mover)
    assert mover.status()["phase"] == "cancelled"
    assert cfg.data_dir == old and all(p.startswith(str(old)) for p in storage.referenced(db))
    assert len(storage.files(old)) == 3
    assert not [p for p in (tmp_path / "new").rglob("*") if p.is_file()]   # the unfinished copy is removed


def test_a_copy_that_differs_fails_the_move(station, tmp_path, monkeypatch):
    cfg, db, _ = station
    old = cfg.data_dir
    monkeypatch.setattr(storage, "_sha256", lambda *a, **k: b"different")
    mover = storage.Mover(cfg, db)
    mover.start(str(tmp_path / "new"))
    wait(mover)
    st = mover.status()
    assert st["phase"] == "failed" and "differs" in st["error"]
    assert cfg.data_dir == old and len(storage.files(old)) == 3


def test_relink_points_at_data_that_is_there(station, tmp_path):
    cfg, db, _ = station
    old, new = cfg.data_dir, tmp_path / "mounted-elsewhere"
    shutil.copytree(old, new)
    mover = storage.Mover(cfg, db)
    mover.relink(str(new))
    assert mover.status()["kind"] == "relink" and mover.status()["phase"] == "done"
    assert cfg.data_dir == new and all(p.startswith(str(new) + os.sep) for p in storage.referenced(db))
    assert len(storage.files(old)) == 3                            # relinking copies and deletes nothing


def test_a_restart_during_a_move_is_reported(station):
    cfg, db, _ = station
    db.set_setting(storage.S_MOVE, json.dumps({"kind": "move", "phase": "copying", "src": "/a", "dst": "/b",
                                               "bytes_total": 10, "units_done": 4}))
    st = storage.Mover(cfg, db).status()
    assert st["phase"] == "interrupted" and not st["active"]
    db.set_setting(storage.S_MOVE, json.dumps({"kind": "move", "phase": "cleaning", "src": "/a", "dst": "/b"}))
    st = storage.Mover(cfg, db).status()
    assert st["phase"] == "done" and st["cleanup_incomplete"]


def test_copy_verified_leaves_no_partial_file(tmp_path):
    src, dst = tmp_path / "a.wav", tmp_path / "out" / "a.wav"
    src.write_bytes(b"z" * 10_000)
    with pytest.raises(storage._Cancelled):
        storage.copy_verified(src, dst, cancelled=lambda: True)
    assert not dst.exists() and not list((tmp_path / "out").iterdir())
    n = []
    storage.copy_verified(src, dst, step=n.append)
    assert dst.read_bytes() == src.read_bytes() and sum(n) == 20_000   # read and written, then read back


def test_api_and_the_guards_while_moving(tmp_path, monkeypatch):
    cfg = replace(Config(), data_dir=tmp_path / "data", state_dir=tmp_path / "state", min_free_gb=0.0,
                  fake_source=str(tmp_path / "none.wav"))
    (cfg.data_dir / "recordings").mkdir(parents=True)
    (cfg.data_dir / "recordings" / "00001-A-before.wav").write_bytes(b"w" * 4000)
    release = threading.Event()
    real = storage.copy_verified

    def slow_copy(*a):
        release.wait(10)
        return real(*a)
    monkeypatch.setattr(storage, "copy_verified", slow_copy)
    with TestClient(create_app(cfg)) as c:
        info = c.get("/api/storage").json()
        assert info["data"]["path"] == str(cfg.data_dir) and info["data"]["source"] == "config"
        assert info["app"]["path"] == str(cfg.state_dir) and info["move"] is None
        assert info["targets"][0]["current"] and info["data"]["drive"]["mount"]
        r = c.post("/api/storage/check", json={"path": "nope"}).json()
        assert not r["ok"] and r["problems"] == [i18n.render(i18n.msg("storage.not_absolute"), "en")]
        target = str(tmp_path / "ext" / "vinyl-check")
        assert c.post("/api/storage/check", json={"path": target}).json()["ok"]
        st = c.post("/api/storage/move", json={"path": target, "delete_originals": True}).json()
        assert st["active"] and st["phase"] == "copying"
        assert c.post("/api/storage/move", json={"path": target}).status_code == 409          # one at a time
        rid = c.post("/api/records", json={"artist": "A", "title": "T"}).json()["id"]
        r = c.post("/api/recordings/start", json={"record_id": rid, "side": "A", "kind": "before"})
        assert r.status_code == 409 and "moved" in r.json()["detail"]
        assert c.get("/api/status").json()["storage_move"]["active"]
        release.set()
        end = time.monotonic() + 10
        while c.get("/api/status").json()["storage_move"]["active"] and time.monotonic() < end:
            time.sleep(0.05)
        info = c.get("/api/storage").json()
        assert info["data"]["path"] == target and info["data"]["source"] == "settings"
        assert info["move"]["phase"] == "done" and info["data"]["usage"]["recordings"]["files"] == 1
        assert c.post("/api/storage/move/dismiss").json()["move"] is None


def test_targets_offer_drives_earlier_folders_and_the_system_drive(station, tmp_path):
    """The drop-down in the settings: the folder in use first (not selectable), then what was found."""
    cfg, db, _ = station
    old = cfg.data_dir
    object.__setattr__(cfg, "data_dir", tmp_path / "ssd")              # changed in the configuration file
    t = storage.targets(cfg, db, service=True, config_dir=tmp_path / "from-config")
    kinds = {x["kind"]: x for x in t}
    assert t[0]["kind"] == "current" and t[0]["current"] and t[0]["path"] == str(tmp_path / "ssd")
    assert kinds["previous"]["path"] == str(old)                       # where the old recordings still are
    assert kinds["config"]["path"] == str(tmp_path / "from-config")
    assert kinds["system"]["path"] == str(cfg.state_dir / "data")      # a service keeps its data apart
    desktop = {x["kind"]: x for x in storage.targets(cfg, db, service=False)}
    assert desktop["system"]["path"] == str(cfg.state_dir)            # an app keeps its data in the user folder
    assert all({"free_gb", "total_gb", "drive", "available"} <= x.keys() for x in t)
    assert len({x["path"] for x in t}) == len(t)


def test_drive_of_a_folder(tmp_path):
    d = storage.drive(tmp_path)
    assert d["mount"] and isinstance(d["system"], bool) and "model" in d
    assert storage._linux_drive("not-a-device") == {}


def test_recording_names_are_readable_and_safe():
    rec = {"id": 43, "side": "B", "kind": "after_wet", "copy_label": "Exemplar 2"}
    assert storage.recording_name(rec, {"artist": "Die Ärzte", "title": "Jazz ist anders (Ökö-Edition)"}) \
        == "00043_Die-Aerzte_Jazz-ist-anders-Oekoe-Edition_Exemplar-2_B_after_wet.wav"
    assert storage.recording_name({"id": 2, "side": "1", "kind": "alignment", "setup_id": 1}, None,
                                  {"name": "Dual DTJ 301.1 USB"}) == "00002_Dual-DTJ-301-1-USB_J1_1_alignment.wav"
    assert storage.recording_name({"id": 6, "side": "A/", "kind": "before"}, {"artist": "", "title": "../.."}) \
        == "00006_A_before.wav"
    assert storage.slug("Björk – Homogénic / ß") == "Bjoerk-Homogenic-ss" and len(storage.slug("x" * 99)) == 40


def test_rename_brings_old_files_to_the_scheme(station, tmp_path):
    cfg, db, rid = station
    (cfg.rec_dir / "00009-taken.wav").write_bytes(b"x")
    mover = storage.Mover(cfg, db)
    todo = storage.renames(cfg, db)
    assert [new.name for _, _, new in todo] == ["00001_Artist_Title_A_before.wav", "00002_Artist_Title_B_before.wav"]
    blocker = cfg.rec_dir / "00002_Artist_Title_B_before.wav"                  # the new name is taken
    blocker.write_bytes(b"other")
    assert mover.rename() == {"renamed": 1, "kept": 1}
    paths = sorted(storage.referenced(db))
    assert str(cfg.rec_dir / "00001_Artist_Title_A_before.wav") in paths
    assert str(cfg.rec_dir / "00002-B-before.wav") in paths                    # kept its name, still found
    assert (cfg.rec_dir / "00001_Artist_Title_A_before.wav").stat().st_size == 3000
    assert storage.info(cfg, db, mover)["names"]["pending"] == 1
    busy = storage.Mover(cfg, db, analysing=lambda: True)
    with pytest.raises(storage.MoveRefused):
        busy.rename()


def test_rename_undoes_the_renames_when_the_database_fails(station, monkeypatch):
    cfg, db, _ = station
    before = sorted(p.name for p in cfg.rec_dir.iterdir())

    class Broken:
        def __init__(self, conn):
            self.conn = conn

        def execute(self, sql, *a):
            if sql.startswith("UPDATE recordings"):
                raise RuntimeError("disk full")
            return self.conn.execute(sql, *a)
    monkeypatch.setattr(db, "conn", Broken(db.conn))
    with pytest.raises(RuntimeError):
        storage.Mover(cfg, db).rename()
    assert sorted(p.name for p in cfg.rec_dir.iterdir()) == before


def test_new_recordings_get_the_readable_name(tmp_path):
    import numpy as np
    import soundfile as sf
    wav = tmp_path / "side.wav"
    sf.write(wav, np.zeros((48000 * 2, 2), dtype="float32"), 48000, subtype="PCM_16")
    cfg = replace(Config(), data_dir=tmp_path / "data", state_dir=tmp_path / "state", min_free_gb=0.0,
                  fake_source=str(wav), fake_speed=200.0)
    with TestClient(create_app(cfg)) as c:
        rid = c.post("/api/records", json={"artist": "Pink Floyd", "title": "The Wall"}).json()["id"]
        r = c.post("/api/recordings/start", json={"record_id": rid, "side": "A", "kind": "before"})
        assert r.status_code == 200, r.text
        rec_id = r.json()["recording_id"]
        meta = c.get("/api/status").json()["recorder"]["meta"]       # every window can follow it to its record
        assert meta["record_id"] == rid and meta["kind"] == "before"
        c.post("/api/recordings/stop")
        path = c.app.state.db.recording(rec_id)["path"]
        assert Path(path).name == f"{rec_id:05d}_Pink-Floyd_The-Wall_A_before.wav"
