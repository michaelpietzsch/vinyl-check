"""SQLite storage: records, alignment runs, recordings, analyses, events, comparisons, masters, profiles.

One connection shared by all threads (WAL mode, guarded by a re-entrant lock). Older databases are migrated in
place on start; before a migration that rebuilds tables or rewrites stored values, a backup copy is written next to
the database file (`<name>.pre-<version>-<time>.sqlite`). `PRAGMA user_version` holds the data version (DATA_VERSION).

Error texts of jobs (columns `error`) may be i18n messages: they are stored as JSON and turned back into
message dicts when read, so the API can render them in the reader's language. Plain strings from older versions
pass through unchanged.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path

from . import grading, i18n, legacy

log = logging.getLogger("vinylcheck.db")
# Version of the stored values (PRAGMA user_version): 1 = interface values of 1.0 (English codes, see legacy.py)
DATA_VERSION = 1


def copy_key(label) -> str:
    """Key of a copy label, like copyKey in the web client: case and whitespace do not matter."""
    return " ".join((label or "").split()).lower()


def side_key(side) -> str:
    """Normalized side name ("a " → "A")."""
    return str(side or "").strip().upper()


def _load_error(row: dict | None) -> dict | None:
    if row and "error" in row:
        row["error"] = i18n.loads_text(row["error"])
    return row


def _store_error(fields: dict) -> dict:
    if "error" in fields:
        fields["error"] = i18n.dumps(fields["error"])
    return fields

SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    id INTEGER PRIMARY KEY,
    artist TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    discogs_release_id INTEGER,
    discogs_media_condition TEXT,
    notes TEXT NOT NULL DEFAULT '',
    sides TEXT,
    tracks TEXT,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS setups (
    id INTEGER PRIMARY KEY,
    label TEXT NOT NULL DEFAULT '',
    test_record TEXT NOT NULL DEFAULT 'AAPT 1',
    tracking_force_g REAL,
    bias TEXT NOT NULL DEFAULT '',
    vta TEXT NOT NULL DEFAULT '',
    needle_hours REAL,
    input_level TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS recordings (
    id INTEGER PRIMARY KEY,
    record_id INTEGER REFERENCES records(id) ON DELETE CASCADE,
    setup_id INTEGER REFERENCES setups(id) ON DELETE CASCADE,
    side TEXT NOT NULL DEFAULT 'A',
    kind TEXT NOT NULL CHECK (kind IN ('calibration', 'before', 'after', 'after_wet', 'after_dry', 'other', 'alignment')),
    rpm REAL,
    cleaning_method TEXT NOT NULL DEFAULT '',
    copy_label TEXT NOT NULL DEFAULT '',
    findings TEXT NOT NULL DEFAULT '[]',
    notes TEXT NOT NULL DEFAULT '',
    path TEXT,
    device TEXT, samplerate INTEGER, channels INTEGER, format TEXT,
    duration_s REAL, clip_samples INTEGER DEFAULT 0, xruns INTEGER DEFAULT 0,
    chain_db REAL,
    sensitivity REAL,
    sha256 TEXT,
    audio_deleted_at REAL,
    status TEXT NOT NULL DEFAULT 'recording',
    error TEXT,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS analyses (
    recording_id INTEGER PRIMARY KEY REFERENCES recordings(id) ON DELETE CASCADE,
    version TEXT NOT NULL,
    metrics TEXT NOT NULL,
    grading TEXT NOT NULL,
    warnings TEXT NOT NULL,
    overview TEXT NOT NULL,
    grade TEXT, score REAL,
    duration_s REAL,
    analyzed_at REAL NOT NULL,
    sg_meta TEXT,
    sg_img BLOB
);
CREATE TABLE IF NOT EXISTS events (
    recording_id INTEGER NOT NULL REFERENCES recordings(id) ON DELETE CASCADE,
    t REAL NOT NULL, dur_ms REAL, strength REAL, amp_dbfs REAL,
    ch INTEGER, kind TEXT, region TEXT
);
CREATE INDEX IF NOT EXISTS events_rec ON events(recording_id, t);
CREATE TABLE IF NOT EXISTS so_cache (
    sha256 TEXT NOT NULL,
    t REAL NOT NULL,
    p REAL NOT NULL,
    PRIMARY KEY (sha256, t)
);
CREATE TABLE IF NOT EXISTS copy_findings (
    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
    copy_key TEXT NOT NULL DEFAULT '',
    side TEXT NOT NULL,
    findings TEXT NOT NULL DEFAULT '[]',
    updated_at REAL NOT NULL,
    PRIMARY KEY (record_id, copy_key, side)
);
CREATE TABLE IF NOT EXISTS skip_labels (
    recording_id INTEGER NOT NULL REFERENCES recordings(id) ON DELETE CASCADE,
    t REAL NOT NULL,
    label TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (recording_id, t)
);
CREATE TABLE IF NOT EXISTS event_labels (
    recording_id INTEGER NOT NULL REFERENCES recordings(id) ON DELETE CASCADE,
    t REAL NOT NULL,
    label TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (recording_id, t)
);
CREATE TABLE IF NOT EXISTS profiles (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    cartridge TEXT NOT NULL DEFAULT '',
    device TEXT NOT NULL DEFAULT '',
    tracking_force_g REAL,
    antiskating TEXT NOT NULL DEFAULT '',
    weight TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 0,
    calibration_id INTEGER,
    baseline_setup_id INTEGER,
    wizard_setup_id INTEGER,
    target_map TEXT,
    ref_dbfs REAL, chain_db REAL, speed_pct REAL,
    floor_clicks_per_min REAL, floor_ticks_per_min REAL, sensitivity REAL,
    fixed_gain INTEGER NOT NULL DEFAULT 0,
    baseline TEXT,
    accepted_at REAL,
    vm_id TEXT,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY, value TEXT NOT NULL, changed_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS comparisons (
    id INTEGER PRIMARY KEY,
    a_id INTEGER NOT NULL REFERENCES recordings(id) ON DELETE CASCADE,
    b_id INTEGER NOT NULL REFERENCES recordings(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'queued',
    result TEXT, error TEXT,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS masters (
    id INTEGER PRIMARY KEY,
    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
    side TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    path TEXT NOT NULL,
    samplerate INTEGER, channels INTEGER, subtype TEXT, duration_s REAL, bytes INTEGER,
    created_at REAL NOT NULL,
    UNIQUE (record_id, side)
);
CREATE TABLE IF NOT EXISTS master_checks (
    recording_id INTEGER PRIMARY KEY REFERENCES recordings(id) ON DELETE CASCADE,
    master_id INTEGER REFERENCES masters(id) ON DELETE SET NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    result TEXT, error TEXT, version TEXT,
    created_at REAL NOT NULL
);
"""


class DB:
    """The vinyl-check database. One SQLite connection, shared by all threads under `lock`."""

    def get_settings(self) -> dict[str, str]:
        with self.lock:
            return {r["key"]: r["value"] for r in self.conn.execute("SELECT key, value FROM settings")}

    def setting(self, key: str, default=None):
        r = self.one("SELECT value FROM settings WHERE key = ?", (key,))
        return r["value"] if r else default

    def set_setting(self, key: str, value: str) -> None:
        with self.lock:
            self.conn.execute("INSERT INTO settings (key, value, changed_at) VALUES (?, ?, strftime('%s','now')) "
                              "ON CONFLICT(key) DO UPDATE SET value = excluded.value, changed_at = excluded.changed_at",
                              (key, value))

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA foreign_keys=ON")
            self.conn.executescript(SCHEMA)
            # Migrate older databases (columns added in 0.2.0)
            cols = {r[1] for r in self.conn.execute("PRAGMA table_info(recordings)")}
            for col, typ in (("sha256", "TEXT"), ("audio_deleted_at", "REAL")):
                if col not in cols:
                    self.conn.execute(f"ALTER TABLE recordings ADD COLUMN {col} {typ}")
            self._migrate_0_3(path)
            # 0.3.1: overview spectrogram stored with the analysis
            acols = {r[1] for r in self.conn.execute("PRAGMA table_info(analyses)")}
            for col, typ in (("sg_meta", "TEXT"), ("sg_img", "BLOB")):
                if col not in acols:
                    self.conn.execute(f"ALTER TABLE analyses ADD COLUMN {col} {typ}")
            self._migrate_profiles(path)
            self._migrate_copy_findings()
            self._migrate_1_0(path)                    # last: rewrites what the steps above carried over

    def _backup(self, path: Path, tag: str) -> Path:
        """Copy of the database before the migration to version `tag`, next to it."""
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup, i = path.with_name(f"{path.stem}.pre-{tag}-{stamp}.sqlite"), 1
        while backup.exists():                         # two migrations within one second
            backup, i = path.with_name(f"{path.stem}.pre-{tag}-{stamp}-{i}.sqlite"), i + 1
        self.conn.execute("VACUUM INTO ?", (str(backup),))
        return backup

    def _migrate_1_0(self, path: Path) -> None:
        """Interface values of 1.0 (data version 1): the German identifiers stored by earlier versions become the
        English codes (tables and rules in legacy.py). Covers the judgement tables, the judgements on the events, the
        findings, the stored analyses, master checks and comparisons, the profiles' target maps and baselines and the
        target maps in the settings. Text typed by users stays as it is.

        Runs once, in one transaction; a database with data is backed up first."""
        c = self.conn
        if c.execute("PRAGMA user_version").fetchone()[0] >= DATA_VERSION:
            return
        has_data = any(c.execute(f"SELECT 1 FROM {t} LIMIT 1").fetchone()
                       for t in ("records", "recordings", "setups", "profiles", "settings"))
        backup = self._backup(path, "1.0.0") if has_data else None
        changed: dict[str, int] = {}
        try:
            c.execute("BEGIN")
            for table, values in (("event_labels", legacy.EVENT_LABELS), ("events", legacy.EVENT_LABELS),
                                  ("skip_labels", legacy.SKIP_LABELS)):
                changed[f"{table}.label"] = sum(
                    c.execute(f"UPDATE {table} SET label = ? WHERE label = ?", (new, old)).rowcount
                    for old, new in values.items())
            findings = lambda v: legacy.upgrade(v, "findings")       # noqa: E731
            columns = [("recordings", "findings", findings), ("copy_findings", "findings", findings)]
            columns += [(t, col, legacy.upgrade) for t, col in (
                ("analyses", "metrics"), ("analyses", "grading"), ("analyses", "warnings"), ("analyses", "overview"),
                ("profiles", "baseline"), ("master_checks", "result"), ("comparisons", "result"))]
            for table, col, fn in columns:
                changed[f"{table}.{col}"] = self._upgrade_json(table, col, fn)
            changed["settings.target_maps"] = self._upgrade_json("settings", "value", legacy.upgrade_target_maps,
                                                                 "key = 'target_maps'")
            ids = 0
            for old, new in legacy.MAP_IDS.items():
                ids += c.execute("UPDATE profiles SET target_map = ? WHERE target_map = ?", (new, old)).rowcount
                ids += c.execute("UPDATE settings SET value = ? WHERE key = 'target_map_active' AND value = ?",
                                 (new, old)).rowcount
            changed["target map ids"] = ids
            c.execute(f"PRAGMA user_version = {DATA_VERSION}")
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise
        if has_data:
            log.info("Database migrated to the values of 1.0 (backup: %s); rows changed: %s", backup,
                     {k: n for k, n in changed.items() if n} or "none")

    def _upgrade_json(self, table: str, col: str, fn, where: str = "1") -> int:
        """Rewrite the JSON values of one column with `fn`, row by row (analyses can be large); returns the number of
        rows changed. Values that are not JSON stay as they are."""
        n = 0
        for (rowid,) in self.conn.execute(f"SELECT rowid FROM {table} WHERE {where} AND {col} IS NOT NULL").fetchall():
            raw = self.conn.execute(f"SELECT {col} FROM {table} WHERE rowid = ?", (rowid,)).fetchone()[0]
            try:
                old = json.loads(raw)
            except (TypeError, ValueError):
                continue
            new = fn(old)
            if new != old:
                self.conn.execute(f"UPDATE {table} SET {col} = ? WHERE rowid = ?", (json.dumps(new), rowid))
                n += 1
        return n

    def _migrate_profiles(self, path: Path) -> None:
        """Turntable profiles (schema 0.4.0): runs and recordings get `profile_id`. Existing data is split into one
        profile per recording device (one device = one turntable or signal chain); the profile of the most recent
        recording becomes active. The database is backed up first. Also adds later profile/recording columns."""
        rcols = {r[1] for r in self.conn.execute("PRAGMA table_info(recordings)")}
        scols = {r[1] for r in self.conn.execute("PRAGMA table_info(setups)")}
        pcols = {r[1] for r in self.conn.execute("PRAGMA table_info(profiles)")}
        for col in ("wizard_setup_id INTEGER", "target_map TEXT",                     # 0.4.x: profile columns
                    "floor_clicks_per_min REAL", "floor_ticks_per_min REAL", "sensitivity REAL",    # 0.6.0: floor
                    "fixed_gain INTEGER NOT NULL DEFAULT 0",                                        # 0.7.0: fixed gain
                    "vm_id TEXT"):                                                                  # 1.1.0: Vinyl-Mind
            if col.split()[0] not in pcols:
                self.conn.execute(f"ALTER TABLE profiles ADD COLUMN {col}")
        if "sensitivity" not in rcols:                                                 # 0.6.0: per-recording override
            self.conn.execute("ALTER TABLE recordings ADD COLUMN sensitivity REAL")
            rcols = rcols | {"sensitivity"}
        if "copy_label" not in rcols:                                                  # 0.6.0: copy label
            self.conn.execute("ALTER TABLE recordings ADD COLUMN copy_label TEXT NOT NULL DEFAULT ''")
            rcols = rcols | {"copy_label"}
        if "findings" not in rcols:                                                    # 0.7.0: visual findings
            self.conn.execute("ALTER TABLE recordings ADD COLUMN findings TEXT NOT NULL DEFAULT '[]'")
            rcols = rcols | {"findings"}
        ecols = {r[1] for r in self.conn.execute("PRAGMA table_info(events)")}
        for col in ("gc REAL", "label TEXT"):                                          # 0.7.0: second opinion, verdict
            if col.split()[0] not in ecols:
                self.conn.execute(f"ALTER TABLE events ADD COLUMN {col}")
        rec_cols = {r[1] for r in self.conn.execute("PRAGMA table_info(records)")}
        if "sides" not in rec_cols:                                                    # 0.5.0: side lengths
            self.conn.execute("ALTER TABLE records ADD COLUMN sides TEXT")
        if "tracks" not in rec_cols:                                                   # 0.7.0: tracks per side
            self.conn.execute("ALTER TABLE records ADD COLUMN tracks TEXT")
        if "profile_id" in rcols and "profile_id" in scols:
            return
        has_data = self.conn.execute("SELECT COUNT(*) FROM recordings").fetchone()[0] > 0
        if has_data:
            self._backup(path, "0.4.0")
        for tbl, cols in (("recordings", rcols), ("setups", scols)):
            if "profile_id" not in cols:
                self.conn.execute(f"ALTER TABLE {tbl} ADD COLUMN profile_id INTEGER "
                                  "REFERENCES profiles(id) ON DELETE SET NULL")
        if not has_data or self.conn.execute("SELECT COUNT(*) FROM profiles").fetchone()[0]:
            return
        now = time.time()
        devs = [r[0] for r in self.conn.execute(
            "SELECT device FROM recordings WHERE device IS NOT NULL AND device != '' "
            "GROUP BY device ORDER BY MIN(created_at)")]
        for dev in devs:
            pid = self.conn.execute("INSERT INTO profiles (name, device, created_at) VALUES (?, ?, ?)",
                                    (f"Turntable on {dev}", dev, now)).lastrowid
            self.conn.execute("UPDATE recordings SET profile_id = ? WHERE device = ?", (pid, dev))
        # Runs: the profile of most of their recordings
        self.conn.execute("""UPDATE setups SET profile_id = (
            SELECT r.profile_id FROM recordings r WHERE r.setup_id = setups.id AND r.profile_id IS NOT NULL
             GROUP BY r.profile_id ORDER BY COUNT(*) DESC LIMIT 1)""")
        last = self.conn.execute("SELECT profile_id FROM recordings WHERE profile_id IS NOT NULL "
                                 "ORDER BY created_at DESC LIMIT 1").fetchone()
        if last:
            self.conn.execute("UPDATE profiles SET active = 1 WHERE id = ?", (last[0],))
        # Calibration per profile: the most recent valid one recorded with the profile's device
        self.conn.execute("""UPDATE profiles SET calibration_id = (
            SELECT r.id FROM recordings r WHERE r.kind = 'calibration' AND r.chain_db IS NOT NULL
               AND r.profile_id = profiles.id ORDER BY r.created_at DESC LIMIT 1),
            chain_db = (SELECT r.chain_db FROM recordings r WHERE r.kind = 'calibration' AND r.chain_db IS NOT NULL
               AND r.profile_id = profiles.id ORDER BY r.created_at DESC LIMIT 1)""")

    # ─── Turntable profiles ─────────────────────────────────────────────────
    PROFILE_FIELDS = ("name", "cartridge", "device", "tracking_force_g", "antiskating", "weight", "notes",
                      "target_map", "calibration_id", "baseline_setup_id", "wizard_setup_id", "ref_dbfs", "chain_db", "speed_pct", "baseline",
                      "floor_clicks_per_min", "floor_ticks_per_min", "sensitivity", "fixed_gain", "accepted_at")

    def profiles(self) -> list[dict]:
        return self.q("""SELECT p.*, (SELECT COUNT(*) FROM setups s WHERE s.profile_id = p.id) AS setups,
                                (SELECT COUNT(*) FROM recordings r WHERE r.profile_id = p.id) AS recordings
                           FROM profiles p ORDER BY p.active DESC, p.created_at DESC""")

    def profile(self, pid: int) -> dict | None:
        return self.one("SELECT * FROM profiles WHERE id = ?", (pid,))

    def active_profile(self) -> dict | None:
        return self.one("SELECT * FROM profiles WHERE active = 1 LIMIT 1")

    def add_profile(self, **f) -> int:
        cols = [c for c in self.PROFILE_FIELDS if f.get(c) is not None]
        return self.x(f"INSERT INTO profiles ({', '.join(cols + ['created_at'])}) "
                      f"VALUES ({', '.join('?' * (len(cols) + 1))})", (*[f[c] for c in cols], time.time()))

    def update_profile(self, pid: int, **fields) -> None:
        text = ("name", "cartridge", "device", "antiskating", "weight", "notes")
        sets = {k: ("" if v is None and k in text else v) for k, v in fields.items() if k in self.PROFILE_FIELDS}
        if sets.get("target_map") == "":
            sets["target_map"] = None
        if sets:
            self.x(f"UPDATE profiles SET {', '.join(f'{k} = ?' for k in sets)} WHERE id = ?", (*sets.values(), pid))

    def activate_profile(self, pid: int) -> None:
        with self.lock:
            self.conn.execute("UPDATE profiles SET active = CASE WHEN id = ? THEN 1 ELSE 0 END", (pid,))

    def delete_profile(self, pid: int) -> None:
        self.x("DELETE FROM profiles WHERE id = ?", (pid,))

    def profile_vm_id(self, pid: int) -> str | None:
        """The profile's id for Vinyl-Mind: 128 random bits, created on first use and never derived from names or
        hardware, so contributions cannot be traced to a turntable. None if the profile does not exist."""
        with self.lock:
            row = self.conn.execute("SELECT vm_id FROM profiles WHERE id = ?", (pid,)).fetchone()
            if row is None:
                return None
            if row[0]:
                return row[0]
            vid = secrets.token_hex(16)
            self.conn.execute("UPDATE profiles SET vm_id = ? WHERE id = ?", (vid, pid))
            return vid

    def _migrate_copy_findings(self) -> None:
        """Visual findings (scratch, warp …) belong to the record, per copy and side (`copy_findings`), not to a
        single recording: a scratch does not go away with the next recording. Merge the ticks of all recordings of a
        copy side once; the old column `recordings.findings` stays but is no longer read."""
        if self.conn.execute("SELECT COUNT(*) FROM copy_findings").fetchone()[0]:
            return
        merged: dict[tuple, set] = {}
        for rid, label, side, fj in self.conn.execute(
                "SELECT record_id, copy_label, side, findings FROM recordings "
                "WHERE record_id IS NOT NULL AND findings IS NOT NULL AND findings != '[]'").fetchall():
            try:
                f = json.loads(fj or "[]")
            except ValueError:
                f = []
            if f:
                merged.setdefault((rid, copy_key(label), side_key(side)), set()).update(f)
        for (rid, ck, sd), f in merged.items():
            self.conn.execute("INSERT OR REPLACE INTO copy_findings VALUES (?, ?, ?, ?, ?)",
                              (rid, ck, sd, json.dumps(sorted(f)), time.time()))

    def _migrate_0_3(self, path: Path) -> None:
        """Recording kind 'alignment' with columns setup_id and rpm (schema 0.3.0), and the kinds 'after_wet' (after
        wet cleaning) and 'after_dry' (after dry cleaning) (schema 0.7.0).

        SQLite cannot change a CHECK constraint, so `recordings` is recreated and copied over (procedure from the
        SQLite documentation, foreign keys off meanwhile). The database is first backed up to
        `<name>.pre-<version>-<time>.sqlite`.
        """
        sql = self.conn.execute("SELECT sql FROM sqlite_master WHERE name = 'recordings'").fetchone()[0]
        if "'alignment'" in sql and "'after_wet'" in sql:
            return
        tag = "0.7.0" if "'alignment'" in sql else "0.3.0"
        self._backup(path, tag)
        new = SCHEMA.split("CREATE TABLE IF NOT EXISTS recordings")[1].split(";")[0]
        old_cols = [r[1] for r in self.conn.execute("PRAGMA table_info(recordings)")]
        self.conn.execute("PRAGMA foreign_keys=OFF")
        try:
            self.conn.execute("BEGIN")
            self.conn.execute("CREATE TABLE recordings_new" + new)
            # Columns that later migrations appended with ALTER (profile_id, sensitivity, copy_label …) are missing
            # from the base schema: carry them over with type, default and foreign key. Without this the copy fails
            # ("recordings_new has no column named profile_id") and the service ends up in a restart loop.
            new_cols = {r[1] for r in self.conn.execute("PRAGMA table_info(recordings_new)")}
            fks = {r[3]: (r[2], r[4], r[6]) for r in self.conn.execute("PRAGMA foreign_key_list(recordings)")}
            for _, name, typ, notnull, dflt, _ in self.conn.execute("PRAGMA table_info(recordings)").fetchall():
                if name in new_cols:
                    continue
                decl = f"{name} {typ}"
                if dflt is not None:
                    decl += f" NOT NULL DEFAULT {dflt}" if notnull else f" DEFAULT {dflt}"
                if name in fks:
                    decl += f" REFERENCES {fks[name][0]}({fks[name][1]}) ON DELETE {fks[name][2]}"
                self.conn.execute(f"ALTER TABLE recordings_new ADD COLUMN {decl}")
            cols = ", ".join(old_cols)
            self.conn.execute(f"INSERT INTO recordings_new ({cols}) SELECT {cols} FROM recordings")
            self.conn.execute("DROP TABLE recordings")
            self.conn.execute("ALTER TABLE recordings_new RENAME TO recordings")
            bad = self.conn.execute("PRAGMA foreign_key_check").fetchall()
            if bad:
                raise RuntimeError(f"Migration 0.3.0: foreign key violations: {bad[:5]}")
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        finally:
            self.conn.execute("PRAGMA foreign_keys=ON")

    def q(self, sql: str, args=()) -> list[dict]:
        """All rows of a query as dicts."""
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def one(self, sql: str, args=()) -> dict | None:
        """First row of a query as dict, or None."""
        rows = self.q(sql, args)
        return rows[0] if rows else None

    def x(self, sql: str, args=()) -> int:
        """Execute a statement; returns lastrowid."""
        with self.lock:
            cur = self.conn.execute(sql, args)
            return cur.lastrowid

    # ─── Records ────────────────────────────────────────────────────────────
    def add_record(self, artist: str, title: str, discogs_release_id=None,
                   discogs_media_condition=None, notes: str = "") -> int:
        return self.x("INSERT INTO records (artist, title, discogs_release_id, discogs_media_condition,"
                      " notes, created_at) VALUES (?,?,?,?,?,?)",
                      (artist, title, discogs_release_id, discogs_media_condition, notes, time.time()))

    def copy_grades(self, rid: int | None = None) -> dict[int, list[dict]]:
        """Record grade per record and copy: per side the most recent graded recording, combined into the side
        average (grading.record_grade). Calibration and alignment recordings do not count. Copies are keyed like
        copyKey in the web client (case and whitespace do not matter); unnamed copy first, then natural order
        ("Copy 1" < "Copy 10")."""
        sql = """SELECT x.id, x.record_id, x.copy_label, x.side, a.grade, x.created_at
                   FROM recordings x JOIN analyses a ON a.recording_id = x.id
                  WHERE a.grade IS NOT NULL AND x.kind NOT IN ('alignment', 'calibration')"""
        rows = self.q(sql + (" AND x.record_id = ?" if rid is not None else "") + " ORDER BY x.created_at DESC",
                      (rid,) if rid is not None else ())
        by_rec: dict[int, dict[str, dict]] = {}
        for r in rows:
            key, side = copy_key(r["copy_label"]), side_key(r["side"])
            c = by_rec.setdefault(r["record_id"], {}).setdefault(key, {"copy": (r["copy_label"] or "").strip(), "sides": {},
                                                                       "at": r["created_at"]})
            if side not in c["sides"]:                               # most recent per side (sorted descending)
                c["sides"][side] = {"side": side, "grade": r["grade"], "id": r["id"], "at": r["created_at"]}
        natural = lambda s: [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]
        out = {}
        for rec_id, copies in by_rec.items():
            lst = []
            for c in copies.values():
                sides = sorted(c["sides"].values(), key=lambda s: s["side"])
                lst.append({"copy": c["copy"], "grade": grading.record_grade([s["grade"] for s in sides]),
                            "sides": sides, "at": c["at"]})
            out[rec_id] = sorted(lst, key=lambda c: (c["copy"] != "", natural(c["copy"])))
        return out

    def records(self) -> list[dict]:
        rows = [dict(r) for r in self.q("""
            SELECT r.*, COUNT(rc.id) AS recordings
              FROM records r LEFT JOIN recordings rc ON rc.record_id = r.id
             GROUP BY r.id ORDER BY r.created_at DESC""")]
        grades = self.copy_grades()
        for r in rows:                                               # grade per copy; "last" = newest copy
            copies = grades.get(r["id"], [])
            newest = max(copies, key=lambda c: c["at"], default=None)
            r["grades_by_copy"] = json.dumps(copies)
            r["last_grade"] = newest["grade"] if newest else None
            r["last_grade_at"] = newest["at"] if newest else None
        return rows

    def record(self, rid: int) -> dict | None:
        return self.one("SELECT * FROM records WHERE id = ?", (rid,))

    def update_record(self, rid: int, **fields) -> None:
        allowed = {"artist", "title", "discogs_release_id", "discogs_media_condition", "notes", "sides", "tracks"}
        sets = {k: v for k, v in fields.items() if k in allowed}
        if sets:
            self.x(f"UPDATE records SET {', '.join(f'{k} = ?' for k in sets)} WHERE id = ?",
                   (*sets.values(), rid))

    def delete_record(self, rid: int) -> None:
        self.x("DELETE FROM records WHERE id = ?", (rid,))

    # ─── Alignment runs ─────────────────────────────────────────────────────
    SETUP_FIELDS = ("profile_id", "label", "test_record", "tracking_force_g", "bias", "vta", "needle_hours",
                    "input_level", "notes")

    def add_setup(self, **f) -> int:
        cols = [c for c in self.SETUP_FIELDS if f.get(c) is not None]
        return self.x(f"INSERT INTO setups ({', '.join(cols + ['created_at'])}) "
                      f"VALUES ({', '.join('?' * (len(cols) + 1))})", (*[f[c] for c in cols], time.time()))

    def setups(self, profile_id: int | None = None) -> list[dict]:
        where, args = ("WHERE s.profile_id = ?", (profile_id,)) if profile_id is not None else ("", ())
        return self.q(f"""SELECT s.*, COUNT(r.id) AS recordings FROM setups s
                         LEFT JOIN recordings r ON r.setup_id = s.id {where} GROUP BY s.id ORDER BY s.created_at DESC""", args)

    def setup(self, sid: int) -> dict | None:
        return self.one("SELECT * FROM setups WHERE id = ?", (sid,))

    def update_setup(self, sid: int, **fields) -> None:
        """None sets a field to NULL (clears number fields); text fields become ''."""
        text = ("label", "test_record", "bias", "vta", "input_level", "notes")
        sets = {k: ("" if v is None and k in text else v) for k, v in fields.items() if k in self.SETUP_FIELDS}
        if sets:
            self.x(f"UPDATE setups SET {', '.join(f'{k} = ?' for k in sets)} WHERE id = ?", (*sets.values(), sid))

    def delete_setup(self, sid: int) -> list[str]:
        paths = [r["path"] for r in self.q("SELECT path FROM recordings WHERE setup_id = ?", (sid,)) if r["path"]]
        self.x("DELETE FROM setups WHERE id = ?", (sid,))
        return paths

    def setup_recordings(self, sid: int) -> list[dict]:
        """Recordings of a run, oldest first, with key figures (summary) and results. `is_alignment` marks
        recordings analyzed as a test record side, `clip_in_bands` counts clipped samples inside measurement bands.

        The large parts of the analysis (curves, wow & flutter trace) stay in SQLite: json_extract fetches only the
        small parts instead of loading and parsing the whole metrics blob per recording."""
        rows = self.q("""
            SELECT r.*, a.warnings,
                   json_extract(a.metrics, '$.kind') = 'alignment' AS is_alignment,
                   json_extract(a.metrics, '$.clip_in_bands') AS clip_in_bands,
                   json_extract(a.metrics, '$.summary') AS summary,
                   json_extract(a.metrics, '$.results') AS results,
                   json_array_length(a.metrics, '$.bands') AS bands,
                   (SELECT json_group_array(json_extract(b.value, '$.role'))
                      FROM json_each(a.metrics, '$.bands') b
                     WHERE json_extract(b.value, '$.role') IS NOT NULL) AS roles
              FROM recordings r LEFT JOIN analyses a ON a.recording_id = r.id
             WHERE r.setup_id = ? ORDER BY r.created_at""", (sid,))
        for row in rows:
            row["is_alignment"] = bool(row["is_alignment"])
            row["clip_in_bands"] = row["clip_in_bands"] or 0
            row["bands"] = row["bands"] or 0
            row["summary"] = json.loads(row["summary"]) if row.get("summary") else {}
            for k in ("results", "roles", "warnings"):
                row[k] = json.loads(row[k]) if row.get(k) else []
            _load_error(row)
        return rows

    # ─── Recordings ─────────────────────────────────────────────────────────
    def add_recording(self, **f) -> int:
        cols = ["record_id", "setup_id", "profile_id", "side", "kind", "rpm", "cleaning_method", "copy_label",
                "notes", "path", "device", "samplerate", "channels", "format", "chain_db", "status"]
        cols = [c for c in cols if f.get(c) is not None]          # otherwise the column default applies
        vals = [f[c] for c in cols]
        return self.x(f"INSERT INTO recordings ({', '.join(cols)}, created_at) "
                      f"VALUES ({', '.join('?' * len(cols))}, ?)", (*vals, time.time()))

    def update_recording(self, rec_id: int, **fields) -> None:
        if fields:
            _store_error(fields)
            self.x(f"UPDATE recordings SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
                   (*fields.values(), rec_id))

    @staticmethod
    def _findings(row: dict) -> dict:
        try:
            row["findings"] = json.loads(row.get("findings") or "[]")
        except (TypeError, ValueError):
            row["findings"] = []
        return row

    # ─── Visual findings per copy and side ──────────────────────────────────
    def copy_findings(self, record_id: int) -> list[dict]:
        rows = self.q("SELECT copy_key, side, findings, updated_at FROM copy_findings WHERE record_id = ? "
                      "ORDER BY copy_key, side", (record_id,))
        return [self._findings(r) for r in rows]

    def set_copy_findings(self, record_id: int, copy_label, side, findings_json: str) -> None:
        self.x("INSERT INTO copy_findings (record_id, copy_key, side, findings, updated_at) VALUES (?, ?, ?, ?, ?) "
               "ON CONFLICT(record_id, copy_key, side) DO UPDATE SET findings = excluded.findings, updated_at = excluded.updated_at",
               (record_id, copy_key(copy_label), side_key(side), findings_json, time.time()))

    def _attach_findings(self, rows: list[dict]) -> None:
        """Give every recording the findings of its copy side (recordings.findings is no longer used)."""
        rids = {r["record_id"] for r in rows if r.get("record_id") is not None}
        m = {}
        if rids:
            for x in self.q("SELECT record_id, copy_key, side, findings FROM copy_findings "
                            f"WHERE record_id IN ({','.join('?' * len(rids))})", tuple(rids)):
                m[(x["record_id"], x["copy_key"], x["side"])] = self._findings(x)["findings"]
        for r in rows:
            r["findings"] = m.get((r.get("record_id"), copy_key(r.get("copy_label")), side_key(r.get("side"))), [])

    def recording(self, rec_id: int) -> dict | None:
        r = _load_error(self.one("SELECT * FROM recordings WHERE id = ?", (rec_id,)))
        if r:
            self._attach_findings([r])
            a = self.one("SELECT grade, score, metrics, grading, warnings, analyzed_at, duration_s, version"
                         " FROM analyses WHERE recording_id = ?", (rec_id,))
            if a:
                for k in ("metrics", "grading", "warnings"):
                    a[k] = json.loads(a[k])
            r["analysis"] = a
        return r

    def recordings(self, record_id: int | None = None) -> list[dict]:
        where, args = ("WHERE r.record_id = ?", (record_id,)) if record_id is not None else ("", ())
        rows = self.q(f"""
            SELECT r.*, a.grade, a.score, a.metrics
              FROM recordings r LEFT JOIN analyses a ON a.recording_id = r.id
              {where} ORDER BY r.side, r.created_at""", args)
        self._attach_findings(rows)
        for row in rows:
            _load_error(row)
            m = json.loads(row.pop("metrics")) if row.get("metrics") else None
            row["summary"] = None if not m else {
                k: m.get(k) for k in ("clicks_quiet_per_min", "ticks_quiet_per_min", "pops_per_min",
                                      "noise_dbfs", "noise_rel_db", "quiet_s", "clip_samples", "clip_in_groove",
                                      "clip_first_s", "level_vs_profile_db", "skips_back", "music_len_s",
                                      "clicks_quiet_net_per_min", "ticks_quiet_net_per_min", "clicks_music_per_min",
                                      "scratch_count")} | {"skips": [{"t": e["t"], "kind": e["kind"]} for e in (m.get("skips") or [])
                                                                     if e.get("kind") == "backward"],
                                                           "clicks_music_confirmed_per_min": (m.get("second_opinion") or {}).get("clicks_music_confirmed_per_min"),
                                                           "learned": {k: (m.get("learned") or {}).get(k) for k in ("clicks_music_per_min", "pops_per_min", "clicks_quiet_per_min", "ticks_quiet_per_min", "graded")} if m.get("learned") else None}
        return rows

    def measured_side_length(self, record_id: int, side: str) -> float | None:
        """Music length of the most recent analyzed recording of this side (from music start to music end)."""
        rows = self.q("""SELECT json_extract(a.metrics, '$.music_len_s') AS v FROM recordings r
                           JOIN analyses a ON a.recording_id = r.id
                          WHERE r.record_id = ? AND r.side = ? AND r.kind != 'calibration'
                          ORDER BY r.created_at DESC""", (record_id, side))
        for r in rows:
            if r["v"] is not None:
                return float(r["v"])
        return None

    def retention_candidates(self, cutoff: float) -> list[dict]:
        """Recordings with audio, older than cutoff, not being processed and not part of a running comparison."""
        return self.q("""
            SELECT id, path FROM recordings
             WHERE audio_deleted_at IS NULL AND path IS NOT NULL AND created_at < ?
               AND status NOT IN ('recording', 'queued', 'analyzing')
               AND id NOT IN (SELECT a_id FROM comparisons WHERE status IN ('queued', 'running')
                              UNION SELECT b_id FROM comparisons WHERE status IN ('queued', 'running'))""",
                      (cutoff,))

    def latest_calibration(self, profile_id: int | None = None) -> dict | None:
        if profile_id is not None:
            return self.one("SELECT * FROM recordings WHERE kind = 'calibration' AND chain_db IS NOT NULL"
                            " AND profile_id = ? ORDER BY created_at DESC LIMIT 1", (profile_id,))
        return self.one("SELECT * FROM recordings WHERE kind = 'calibration' AND chain_db IS NOT NULL"
                        " ORDER BY created_at DESC LIMIT 1")

    def delete_recording(self, rec_id: int) -> str | None:
        r = self.one("SELECT path FROM recordings WHERE id = ?", (rec_id,))
        self.x("DELETE FROM recordings WHERE id = ?", (rec_id,))
        return r["path"] if r else None

    # ─── Analyses ───────────────────────────────────────────────────────────
    def save_analysis(self, rec_id: int, res: dict, version: str, duration_s: float) -> None:
        with self.lock:
            c = self.conn
            c.execute("BEGIN")
            try:
                c.execute("DELETE FROM analyses WHERE recording_id = ?", (rec_id,))
                c.execute("DELETE FROM events WHERE recording_id = ?", (rec_id,))
                c.execute("INSERT INTO analyses (recording_id, version, metrics, grading, warnings, overview,"
                          " grade, score, duration_s, analyzed_at) VALUES (?,?,?,?,?,?,?,?,?,?)", (
                    rec_id, version, json.dumps(res["metrics"]), json.dumps(res["grading"]),
                    json.dumps(res["warnings"]), json.dumps(res["overview"]),
                    res["grading"]["grade"], res["grading"]["score"], duration_s, time.time()))
                c.executemany("INSERT INTO events (recording_id, t, dur_ms, strength, amp_dbfs, ch, kind, region, gc, label)"
                              " VALUES (?,?,?,?,?,?,?,?,?,?)", [
                    (rec_id, e["t"], e["dur_ms"], e["strength"], e["amp_dbfs"], e["ch"],
                     e["kind"], e["region"], e.get("gc"), e.get("label")) for e in res["events"]])
                c.execute("COMMIT")
            except Exception:
                c.execute("ROLLBACK")
                raise

    def save_spectrogram(self, rec_id: int, meta: dict, img: bytes) -> None:
        self.x("UPDATE analyses SET sg_meta = ?, sg_img = ? WHERE recording_id = ?",
               (json.dumps(meta), img, rec_id))

    def spectrogram(self, rec_id: int) -> tuple[dict, bytes] | None:
        a = self.one("SELECT sg_meta, sg_img FROM analyses WHERE recording_id = ?", (rec_id,))
        if not a or not a["sg_img"]:
            return None
        return json.loads(a["sg_meta"]), a["sg_img"]

    def analysis_full(self, rec_id: int) -> dict | None:
        """The analysis as analysis.analyze() returns it (input for comparisons)."""
        a = self.one("SELECT * FROM analyses WHERE recording_id = ?", (rec_id,))
        if not a:
            return None
        return {"metrics": json.loads(a["metrics"]), "grading": json.loads(a["grading"]),
                "warnings": json.loads(a["warnings"]), "overview": json.loads(a["overview"]),
                "events": self.events(rec_id)}

    def overview(self, rec_id: int) -> dict | None:
        a = self.one("SELECT overview FROM analyses WHERE recording_id = ?", (rec_id,))
        return json.loads(a["overview"]) if a else None

    def events(self, rec_id: int, limit: int | None = None, order: str = "t") -> list[dict]:
        order_sql = "strength DESC" if order == "strength" else "t"
        sql = f"SELECT t, dur_ms, strength, amp_dbfs, ch, kind, region, gc, label FROM events WHERE recording_id = ? ORDER BY {order_sql}"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return self.q(sql, (rec_id,))

    # ─── Audit verdicts ─────────────────────────────────────────────────────
    def labels(self, rec_id: int) -> dict:
        return {r["t"]: r["label"] for r in self.q("SELECT t, label FROM event_labels WHERE recording_id = ?", (rec_id,))}

    def set_label(self, rec_id: int, t: float, label: str | None, tol_s: float = 0.003) -> int:
        """Set or clear (label None) the verdict for an event time; also attaches it to the event itself.
        Returns the number of events updated."""
        t = round(float(t), 5)
        with self.lock:
            self.conn.execute("DELETE FROM event_labels WHERE recording_id = ? AND ABS(t - ?) <= ?", (rec_id, t, tol_s))
            if label:
                self.conn.execute("INSERT INTO event_labels (recording_id, t, label, created_at) VALUES (?,?,?,?)",
                                  (rec_id, t, label, time.time()))
            cur = self.conn.execute("UPDATE events SET label = ? WHERE recording_id = ? AND ABS(t - ?) <= ?",
                                    (label, rec_id, t, tol_s))
            return cur.rowcount

    def labeled_events(self) -> list[dict]:
        """All judged events with features and context, for training."""
        return self.q("""SELECT e.recording_id, r.record_id, r.profile_id, r.side, r.kind AS recording_kind,
                                e.t, e.dur_ms, e.strength, e.amp_dbfs, e.ch, e.kind, e.region, e.gc, e.label
                           FROM events e JOIN recordings r ON r.id = e.recording_id
                          WHERE e.label IS NOT NULL ORDER BY e.recording_id, e.t""")

    def update_analysis(self, rec_id: int, metrics: dict, grading: dict, warnings: list | None = None) -> None:
        self.x("UPDATE analyses SET metrics = ?, grading = ?, grade = ?, score = ? WHERE recording_id = ?",
               (json.dumps(metrics), json.dumps(grading), grading.get("grade"), grading.get("score"), rec_id))
        if warnings is not None:
            self.x("UPDATE analyses SET warnings = ? WHERE recording_id = ?", (json.dumps(warnings), rec_id))

    # ─── Second opinion: network values per file (checksum) and time, so re-analysis need not ask again ───
    def so_cache(self, sha256: str | None) -> dict:
        if not sha256:
            return {}
        return {r["t"]: r["p"] for r in self.q("SELECT t, p FROM so_cache WHERE sha256 = ?", (sha256,))}

    def save_so_cache(self, sha256: str | None, values: dict) -> None:
        if not sha256 or not values:
            return
        with self.lock:
            self.conn.executemany("INSERT OR REPLACE INTO so_cache (sha256, t, p) VALUES (?,?,?)",
                                  [(sha256, float(t), float(p)) for t, p in values.items()])

    def skip_labels(self, rec_id: int) -> dict:
        return {r["t"]: r["label"] for r in self.q("SELECT t, label FROM skip_labels WHERE recording_id = ?", (rec_id,))}

    def set_skip_label(self, rec_id: int, t: float, label: str | None, tol_s: float = 2.0) -> None:
        t = round(float(t), 3)
        with self.lock:
            self.conn.execute("DELETE FROM skip_labels WHERE recording_id = ? AND ABS(t - ?) <= ?", (rec_id, t, tol_s))
            if label:
                self.conn.execute("INSERT INTO skip_labels (recording_id, t, label, created_at) VALUES (?,?,?,?)",
                                  (rec_id, t, label, time.time()))

    # ─── Comparisons ────────────────────────────────────────────────────────
    def add_comparison(self, a_id: int, b_id: int) -> int:
        return self.x("INSERT INTO comparisons (a_id, b_id, created_at) VALUES (?,?,?)",
                      (a_id, b_id, time.time()))

    # ─── Masters (engineer mode) ────────────────────────────────────────────
    def set_master(self, record_id: int, side: str, **f) -> int:
        """Add or replace the master of a side (one file per record and side). Returns the master id."""
        side = side_key(side)[:1]                           # side = first letter, as with the tracks
        cols = ("name", "path", "samplerate", "channels", "subtype", "duration_s", "bytes")
        vals = tuple(f.get(c) for c in cols)
        with self.lock:
            self.conn.execute(
                f"INSERT INTO masters (record_id, side, {', '.join(cols)}, created_at) VALUES (?, ?, {', '.join('?' * len(cols))}, ?) "
                f"ON CONFLICT(record_id, side) DO UPDATE SET {', '.join(f'{c} = excluded.{c}' for c in cols)}, created_at = excluded.created_at",
                (record_id, side, *vals, time.time()))
        return self.one("SELECT id FROM masters WHERE record_id = ? AND side = ?", (record_id, side))["id"]

    def masters(self, record_id: int) -> list[dict]:
        return self.q("SELECT * FROM masters WHERE record_id = ? ORDER BY side", (record_id,))

    def master(self, record_id: int, side: str) -> dict | None:
        return self.one("SELECT * FROM masters WHERE record_id = ? AND side = ?", (record_id, side_key(side)[:1]))

    def master_by_id(self, mid: int) -> dict | None:
        return self.one("SELECT * FROM masters WHERE id = ?", (mid,))

    def delete_master(self, mid: int) -> None:
        self.x("DELETE FROM masters WHERE id = ?", (mid,))

    def set_master_check(self, rec_id: int, **fields) -> None:
        if "result" in fields and fields["result"] is not None:
            fields["result"] = json.dumps(fields["result"])
        _store_error(fields)
        with self.lock:
            if not self.one("SELECT 1 AS x FROM master_checks WHERE recording_id = ?", (rec_id,)):
                self.conn.execute("INSERT INTO master_checks (recording_id, created_at) VALUES (?, ?)", (rec_id, time.time()))
            if fields:
                self.conn.execute(f"UPDATE master_checks SET {', '.join(f'{k} = ?' for k in fields)} WHERE recording_id = ?",
                                  (*fields.values(), rec_id))

    def master_check(self, rec_id: int) -> dict | None:
        c = _load_error(self.one("SELECT * FROM master_checks WHERE recording_id = ?", (rec_id,)))
        if c and c.get("result"):
            c["result"] = json.loads(c["result"])
        return c

    def master_check_states(self, record_id: int) -> dict[int, str]:
        """Status of the master comparison per recording of a record (for the recordings table)."""
        return {r["recording_id"]: r["status"] for r in self.q(
            "SELECT m.recording_id, m.status FROM master_checks m JOIN recordings r ON r.id = m.recording_id WHERE r.record_id = ?",
            (record_id,))}

    def update_comparison(self, cid: int, **fields) -> None:
        if "result" in fields and fields["result"] is not None:
            fields["result"] = json.dumps(fields["result"])
        _store_error(fields)
        self.x(f"UPDATE comparisons SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
               (*fields.values(), cid))

    def comparison(self, cid: int) -> dict | None:
        c = _load_error(self.one("SELECT * FROM comparisons WHERE id = ?", (cid,)))
        if c and c.get("result"):
            c["result"] = json.loads(c["result"])
        return c

    def comparisons(self, record_id: int | None = None) -> list[dict]:
        sql = """SELECT c.id, c.a_id, c.b_id, c.status, c.created_at, ra.kind AS a_kind, rb.kind AS b_kind,
                        ra.copy_label AS a_copy, rb.copy_label AS b_copy, ra.side, ra.record_id
                   FROM comparisons c JOIN recordings ra ON ra.id = c.a_id JOIN recordings rb ON rb.id = c.b_id"""
        if record_id is not None:
            return self.q(sql + " WHERE ra.record_id = ? ORDER BY c.created_at DESC", (record_id,))
        return self.q(sql + " ORDER BY c.created_at DESC")
