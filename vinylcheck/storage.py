"""The user-data folder: where recordings and masters live, and moving it to another place.

vinyl-check keeps two kinds of data apart:

- the **application folder** (`state_dir`): the database with every measurement, judgment and setting, the Vinyl-Mind
  key, caches and own cover images. It belongs to the installation and stays where it is.
- the **user-data folder** (`data_dir`): the recordings (`recordings/`) and the masters (`masters/`), large files that
  may belong on another disk. Settings → Storage shows both and moves the user-data folder.

Moving copies every file below `recordings/` and `masters/` to the new folder and checks each copy against the original
(SHA-256 of what was read and of what was written and read back), then switches in one step: the stored paths of the
recordings and masters are rewritten from the old folder to the new one in one transaction, and the folder in use
changes while the station runs. Only then are the originals deleted, if that was chosen, and only once no analysis
is running. Until the switch nothing refers to the copies, so a cancelled or failed move leaves the station as it was;
started again, it skips what is already at the target and identical.

While a move runs, no recording can start and no master can be uploaded (both write to the user-data folder), and the
retention cleanup waits. The progress (bytes copied and checked, files, speed, time left) is part of the status the
interface reads every second.

Files the database refers to outside the user-data folder (an earlier folder, left behind when `data_dir` was changed
in the configuration file) move along: they land in `recordings/` or `masters/` of the target and their rows are
rewritten one by one. Moving to the folder in use gathers only those.

Relinking points the station at a folder that already holds the data (a disk mounted under another name, a copy made
by hand): every file the database refers to must be there with the same size; then it switches the same way, without
copying.

The choice is kept in the database (settings `storage_data_dir`, `storage_require_mount`) and wins over `data_dir` in
the configuration file. A folder on another drive also sets `require_mount` to that drive's mount point, so that a
missing drive stops recordings instead of letting them land on the system disk.

Server catalog: ``storage``. The settings section is in the web catalog ``storage``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import sys
import threading
import time
from pathlib import Path

from . import i18n

log = logging.getLogger("vinylcheck.storage")

SUBDIRS = ("recordings", "masters")
S_DATA_DIR, S_REQUIRE_MOUNT, S_MOVE = "storage_data_dir", "storage_require_mount", "storage_move"
CHUNK = 4 * 1024 * 1024
WAIT_IDLE_S = 30 * 60                   # the originals are deleted once no analysis runs; give up waiting after this
SAVE_EVERY_S = 2.0                      # how often the progress is written to the database


class MoveRefused(i18n.MessageError):
    """A move or relink that cannot start (HTTP 409); `message` says why."""


class _Cancelled(Exception):
    pass


# ─── Folders and drives ──────────────────────────────────────────────────────────────────────────────────────────

def apply_saved(cfg, db) -> Path:
    """At startup: the user-data folder chosen in the interface wins over the configuration file. Returns the folder
    of the configuration file (offered in the settings to go back to)."""
    config_dir = cfg.data_dir
    saved = db.setting(S_DATA_DIR)
    if saved:
        object.__setattr__(cfg, "data_dir", Path(saved))                 # Config is frozen; one shared instance
        object.__setattr__(cfg, "require_mount", db.setting(S_REQUIRE_MOUNT) or "")
    return config_dir


def _abs(path) -> Path:
    return Path(os.path.abspath(os.path.expanduser(str(path))))


def _existing(path: Path) -> Path:
    """The path itself or its nearest existing parent."""
    p = path
    while not p.exists() and p != p.parent:
        p = p.parent
    return p


def _inside(a: Path, b: Path) -> bool:
    """Is `a` inside `b` (or the same)?"""
    return a == b or b in a.parents


def mount_point(path: Path) -> str:
    """The mount point of the drive `path` is (or would be) on."""
    p = _existing(_abs(path))
    while not os.path.ismount(p) and p != p.parent:
        p = p.parent
    return str(p)


def drive_for_mount(path: Path) -> str:
    """The value for `require_mount`: the mount point of a separate drive, or "" on the system drive (nothing to wait
    for there)."""
    m = mount_point(path)
    system = {mount_point(Path(sys.prefix)), mount_point(Path.home()), os.path.abspath(os.sep)}
    return "" if m in system else m


def disk(path: Path) -> dict:
    p = _existing(_abs(path))
    try:
        u = shutil.disk_usage(p)
    except OSError:
        return {"total_gb": None, "free_gb": None}
    return {"total_gb": round(u.total / 1e9, 1), "free_gb": round(u.free / 1e9, 1)}


def files(root: Path) -> list[tuple[str, int]]:
    """(relative path, size) of every file below recordings/ and masters/; hidden files (uploads and copies in
    progress) and links are left out."""
    out = []
    for sub in SUBDIRS:
        base = root / sub
        if not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for name in filenames:
                p = Path(dirpath) / name
                if name.startswith(".") or p.is_symlink() or not p.is_file():
                    continue
                out.append((p.relative_to(root).as_posix(), p.stat().st_size))
    return sorted(out)


def usage(root: Path) -> dict:
    out = {sub: {"files": 0, "bytes": 0} for sub in SUBDIRS}
    for rel, size in files(root):
        sub = rel.split("/", 1)[0]
        out[sub]["files"] += 1
        out[sub]["bytes"] += size
    return out


def referenced(db) -> list[str]:
    """Paths of the files the database refers to: recordings that still have their audio, and masters."""
    return [path for _, path in _referenced(db)]


def _referenced(db) -> list[tuple[str, str]]:
    rows = [("recordings", r["path"]) for r in
            db.q("SELECT path FROM recordings WHERE path IS NOT NULL AND audio_deleted_at IS NULL")]
    rows += [("masters", r["path"]) for r in db.q("SELECT path FROM masters WHERE path IS NOT NULL")]
    return [(t, path) for t, path in rows if path]


def outside(cfg, db) -> list[dict]:
    """The files the database refers to outside the user-data folder: {table, src, rel, size} (size None when the
    file cannot be found). `rel` is where it belongs in a user-data folder: below recordings/ or masters/, keeping
    what follows that folder in its path."""
    root, out, seen = _abs(cfg.data_dir), [], set()
    for table, path in _referenced(db):
        src = _abs(path)
        if _inside(src, root) or src in seen:
            continue
        seen.add(src)
        parts = src.parts
        idx = max((i for i, part in enumerate(parts[:-1]) if part == table), default=None)
        rel = "/".join(parts[idx:]) if idx is not None else f"{table}/{src.name}"
        size = src.stat().st_size if src.is_file() else None
        folder = Path(*parts[:idx]) if idx else None             # the earlier user-data folder, where recognisable
        out.append({"table": table, "src": src, "rel": rel, "size": size, "root": folder})
    return out


def plan(cfg, db, dst: Path) -> tuple[list[dict], int]:
    """What a move to `dst` copies: {src, rel, size, table} for every file of the user-data folder (table None: their
    rows are rewritten by prefix) unless `dst` is that folder, and for every file outside it that can be found
    (rewritten one by one). Also the number of outside files that cannot be found; they stay as they are."""
    src = _abs(cfg.data_dir)
    items = [] if dst == src else [{"src": src / rel, "rel": rel, "size": size, "table": None}
                                   for rel, size in files(src)]
    taken = {i["rel"] for i in items}
    missing = 0
    for o in outside(cfg, db):
        if o["size"] is None:
            missing += 1
            continue
        rel = o["rel"]
        if rel in taken:                          # two folders with a file of the same name: keep both
            stem, dot, ext = rel.rpartition(".")
            n = 2
            while (rel := f"{stem}-{n}.{ext}" if dot else f"{o['rel']}-{n}") in taken:
                n += 1
        taken.add(rel)
        items.append(o | {"rel": rel})
    return items, missing


def _mounts() -> list[tuple[str, str]]:
    """(device, mount point) of every mounted file system (Linux)."""
    try:
        lines = Path("/proc/mounts").read_text().splitlines()
    except OSError:
        return []
    return [(f[0], f[1].replace("\\040", " ")) for f in (line.split() for line in lines) if len(f) >= 2]


def _linux_drive(device: str) -> dict:
    """Kind ("sd", "nvme", "usb", "disk") and model of the drive a device node belongs to, from /sys."""
    blk = Path("/sys/class/block") / Path(os.path.realpath(device)).name
    if not device.startswith("/dev/") or not blk.exists():
        return {}
    blk = Path(os.path.realpath(blk))
    if (blk / "partition").exists():
        blk = blk.parent
    model = ""
    for f in ("device/model", "device/name"):
        try:
            model = (blk / f).read_text().strip()
        except OSError:
            continue
        if model:
            break
    kind = ("sd" if blk.name.startswith("mmcblk") else "nvme" if blk.name.startswith("nvme")
            else "usb" if "/usb" in str(blk) else "disk")
    return {"kind": kind, "model": model}


def drive(path: Path) -> dict:
    """The drive a folder is on: mount point, whether it is the system drive, kind and model (as far as known)."""
    m = mount_point(path)
    out = {"mount": m, "system": drive_for_mount(path) == "", "kind": None, "model": ""}
    if sys.platform.startswith("linux"):
        dev = next((d for d, mp in reversed(_mounts()) if mp == m), "")
        out |= _linux_drive(dev)
    elif sys.platform == "darwin" and m.startswith("/Volumes/"):
        out |= {"kind": "volume", "model": Path(m).name}
    elif sys.platform == "win32":
        out |= {"kind": "disk", "model": m.rstrip("\\")}
    return out


def _fstab_unmounted(mounted: set[str]) -> list[str]:
    """Mount points of extra drives listed in /etc/fstab that are not mounted now."""
    try:
        lines = Path("/etc/fstab").read_text().splitlines()
    except OSError:
        return []
    out = []
    for f in (line.split() for line in lines):
        if len(f) >= 3 and not f[0].startswith("#") and f[1].startswith(("/mnt/", "/media/", "/srv/")) \
                and f[1] not in mounted:
            out.append(f[1].replace("\\040", " "))
    return out


def targets(cfg, db, *, service: bool = True, config_dir: Path | None = None) -> list[dict]:
    """The folders the settings offer in the drop-down, the one in use first: vinyl-check on each extra drive (Linux
    /mnt, /media, /srv; macOS /Volumes; Windows D:–Z:), earlier folders the database still refers to, the folder of
    the configuration file, one on the system drive, and drives in /etc/fstab that are not mounted (not available)."""
    current = _abs(cfg.data_dir)
    cands: list[tuple[Path, str]] = [(current, "current")]
    unmounted: list[str] = []
    if sys.platform == "darwin":
        vols = Path("/Volumes")
        # the start volume is a link; hidden and com.apple.* volumes are Time Machine's
        roots = sorted(p for p in vols.iterdir() if os.path.ismount(p) and not p.is_symlink()
                       and not p.name.startswith((".", "com.apple."))) if vols.is_dir() else []
    elif sys.platform == "win32":
        roots = [Path(f"{c}:\\") for c in "DEFGHIJKLMNOPQRSTUVWXYZ" if os.path.exists(f"{c}:\\")]
    else:
        mounts = [mp for _, mp in _mounts()]
        roots = [Path(mp) for mp in mounts if mp.startswith(("/mnt/", "/media/", "/srv/"))]
        unmounted = _fstab_unmounted(set(mounts))
    cands += [(r / "vinyl-check", "drive") for r in roots]
    cands += [(o["root"], "previous") for o in outside(cfg, db) if o["root"] and o["size"] is not None]
    if config_dir is not None:
        cands.append((_abs(config_dir), "config"))
    cands.append((_abs(cfg.state_dir) / "data" if service else _abs(cfg.state_dir), "system"))
    out, seen = [], set()
    for path, kind in cands:
        if path in seen:
            continue
        seen.add(path)
        out.append({"path": str(path), "kind": kind, "current": path == current, "available": True,
                    "exists": path.is_dir(), **disk(path), "drive": drive(path)})
    for mp in unmounted:
        path = Path(mp) / "vinyl-check"
        if path not in seen:
            out.append({"path": str(path), "kind": "unmounted", "current": False, "available": False,
                        "exists": False, "total_gb": None, "free_gb": None,
                        "drive": {"mount": mp, "system": False, "kind": None, "model": ""}})
    return out[:16]


# ─── Checking a target ───────────────────────────────────────────────────────────────────────────────────────────

def check(cfg, db, target: str, *, relink: bool = False, service: bool = True) -> dict:
    """Can the user-data folder move to (or, with `relink`, be pointed at) `target`? Lists the problems (anything that
    stops it) and warnings, and what would be copied."""
    problems, warnings = [], []
    raw = (target or "").strip()
    out = {"ok": False, "path": raw, "files": 0, "bytes": 0, "free_bytes": None, "already": 0, "mount": "",
           "problems": problems, "warnings": warnings}
    if not raw or not Path(os.path.expanduser(raw)).is_absolute():
        problems.append(i18n.msg("storage.not_absolute"))
        return out
    dst, src = _abs(raw), _abs(cfg.data_dir)
    out["path"] = str(dst)
    out["gather"] = gather = dst == src and not relink
    if dst == src and (relink or not any(o["size"] is not None for o in outside(cfg, db))):
        problems.append(i18n.msg("storage.same"))
        return out
    if not gather and (_inside(dst, src) or _inside(src, dst)):
        problems.append(i18n.msg("storage.nested"))
        return out
    base = _existing(dst)
    if dst.exists() and not dst.is_dir():
        problems.append(i18n.msg("storage.not_a_folder"))
        return out
    if not os.access(base, os.W_OK | os.X_OK):
        problems.append(i18n.msg("storage.not_writable_service" if service else "storage.not_writable",
                                 path=str(dst)))
    out["mount"] = cfg.require_mount if gather else drive_for_mount(dst)
    todo, missing_outside = plan(cfg, db, dst)
    out["files"], out["bytes"] = len(todo), sum(i["size"] for i in todo)
    out["outside_files"] = sum(1 for i in todo if i["table"])
    out["outside_bytes"] = sum(i["size"] for i in todo if i["table"])
    try:
        out["free_bytes"] = shutil.disk_usage(base).free
    except OSError:
        out["free_bytes"] = None
    conflicts = []
    for item in todo:
        t = dst / item["rel"]
        if t.exists():
            if t.is_file() and t.stat().st_size == item["size"]:
                out["already"] += 1
            else:
                conflicts.append(item["rel"])
    if relink:
        missing = []
        for p in referenced(db):
            pp = _abs(p)
            if not _inside(pp, src):
                continue
            t = dst / pp.relative_to(src)
            # there, and as large as the original where the original can still be read (a drive mounted elsewhere
            # usually is not)
            if not t.is_file() or (pp.is_file() and t.stat().st_size != pp.stat().st_size):
                missing.append(pp.relative_to(src).as_posix())
        if missing:
            problems.append(i18n.msg("storage.relink_missing", n=len(missing), first=missing[0]))
    else:
        if conflicts:
            problems.append(i18n.msg("storage.conflicts", n=len(conflicts), first=conflicts[0]))
        need = out["bytes"] + cfg.min_free_gb * 1e9
        if out["free_bytes"] is not None and out["free_bytes"] < need:
            problems.append(i18n.msg("storage.no_space", need_gb=need / 1e9, free_gb=out["free_bytes"] / 1e9))
    if relink and outside(cfg, db):
        warnings.append(i18n.msg("storage.outside", n=len(outside(cfg, db))))
    if missing_outside and not relink:
        warnings.append(i18n.msg("storage.outside_missing", n=missing_outside))
    if out["mount"] and not gather:
        warnings.append(i18n.msg("storage.drive_required", mount=out["mount"]))
    out["ok"] = not problems
    return out


# ─── Moving ──────────────────────────────────────────────────────────────────────────────────────────────────────

def _sha256(path: Path, step=None, cancelled=lambda: False) -> bytes:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(CHUNK):
            if cancelled():
                raise _Cancelled
            h.update(chunk)
            if step:
                step(len(chunk))
    return h.digest()


def _prune(folder: Path) -> None:
    """Remove `folder` and its parents while they are empty, up to the recordings/ or masters/ folder they belong to
    (only below such a folder)."""
    if not any(part in SUBDIRS for part in folder.parts):
        return
    while True:
        try:
            folder.rmdir()
        except OSError:
            return
        if folder.name in SUBDIRS:
            return
        folder = folder.parent


def copy_verified(src: Path, dst: Path, step=lambda n: None, cancelled=lambda: False) -> None:
    """Copy `src` to `dst` through a hidden temporary file, then read the copy back and compare SHA-256 with what was
    read from the original; only an identical copy gets the final name. `step(n)` counts the bytes read and written
    (twice the size in all)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    part = dst.with_name(f".{dst.name}.part")
    try:
        h = hashlib.sha256()
        with open(src, "rb") as f, open(part, "wb") as g:
            while chunk := f.read(CHUNK):
                if cancelled():
                    raise _Cancelled
                h.update(chunk)
                g.write(chunk)
                step(len(chunk))
            g.flush()
            os.fsync(g.fileno())
        if _sha256(part, step, cancelled) != h.digest():
            raise OSError(i18n.render(i18n.msg("storage.copy_differs", name=src.name), "en"))
        try:
            shutil.copystat(src, part)
        except OSError:
            pass                                   # times and modes are a courtesy; some file systems refuse them
        os.replace(part, dst)
    except BaseException:
        part.unlink(missing_ok=True)
        raise


class Mover:
    """One move (or relink) of the user-data folder at a time, in a background thread. `recording()` and `analysing()`
    tell whether the recorder and the analysis worker are busy."""

    def __init__(self, cfg, db, *, recording=lambda: False, analysing=lambda: False, service: bool = True,
                 config_dir: Path | None = None):
        self.cfg, self.db, self.config_dir = cfg, db, config_dir
        self.recording, self.analysing, self.service = recording, analysing, service
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()
        self._saved_at = 0.0
        self.state: dict | None = self._load()

    # ─── State ───────────────────────────────────────────────────────────────────────────────────────────────────
    def _load(self) -> dict | None:
        try:
            st = json.loads(self.db.setting(S_MOVE) or "null")
        except ValueError:
            return None
        if isinstance(st, dict) and st.get("phase") in ("copying", "switching"):
            st["phase"] = "interrupted"                # the process ended during the move: nothing was switched
            self._write(st)
        elif isinstance(st, dict) and st.get("phase") == "cleaning":
            st["phase"], st["cleanup_incomplete"] = "done", True
            self._write(st)
        return st if isinstance(st, dict) else None

    def _write(self, st: dict | None) -> None:
        self.db.set_setting(S_MOVE, json.dumps(st))

    def _save(self, force: bool = False) -> None:
        if force or time.monotonic() - self._saved_at >= SAVE_EVERY_S:
            self._write(self.state)
            self._saved_at = time.monotonic()

    @property
    def active(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def status(self) -> dict | None:
        """What the interface shows: phase, percent, files, bytes, speed and time left (None without a move)."""
        st = self.state
        if not st:
            return None
        out = {k: st.get(k) for k in ("kind", "phase", "src", "dst", "delete_originals", "files_total", "files_done",
                                      "bytes_total", "bytes_done", "speed_mb_s", "eta_s", "current", "error",
                                      "started_at", "finished_at", "kept", "cleanup_incomplete")}
        total = 2 * (st.get("bytes_total") or 0)
        out["pct"] = 100.0 if st.get("phase") in ("switching", "cleaning", "done") else (
            round(100.0 * (st.get("units_done") or 0) / total, 1) if total else 0.0)
        out["active"] = self.active
        return out

    # ─── Commands ────────────────────────────────────────────────────────────────────────────────────────────────
    def start(self, target: str, delete_originals: bool = True) -> dict:
        with self._lock:
            if self.active:
                raise MoveRefused(i18n.msg("storage.busy"))
            if self.recording():
                raise MoveRefused(i18n.msg("storage.recording"))
            chk = check(self.cfg, self.db, target, service=self.service)
            if not chk["ok"]:
                raise MoveRefused(chk["problems"][0])
            src, dst = _abs(self.cfg.data_dir), Path(chk["path"])
            self.state = {"kind": "gather" if chk["gather"] else "move", "phase": "copying", "src": str(src),
                          "dst": str(dst),
                          "mount": chk["mount"], "delete_originals": bool(delete_originals),
                          "files_total": chk["files"], "files_done": 0, "bytes_total": chk["bytes"], "bytes_done": 0,
                          "units_done": 0, "speed_mb_s": None, "eta_s": None, "current": None, "error": None,
                          "started_at": time.time(), "finished_at": None, "kept": 0}
            self._save(force=True)
            self._cancel.clear()
            self._thread = threading.Thread(target=self._run, args=(src, dst), daemon=True,
                                            name="vinylcheck-storage")
            self._thread.start()
            log.info("Moving the user-data folder from %s to %s (%d files, %.1f GB)", src, dst, chk["files"],
                     chk["bytes"] / 1e9)
            return self.status()

    def cancel(self) -> dict | None:
        if self.active:
            self._cancel.set()
        return self.status()

    def dismiss(self) -> None:
        """Forget a finished, failed or cancelled move (the notice in the settings)."""
        with self._lock:
            if not self.active:
                self.state = None
                self._write(None)

    def relink(self, target: str) -> dict:
        with self._lock:
            if self.active:
                raise MoveRefused(i18n.msg("storage.busy"))
            if self.recording():
                raise MoveRefused(i18n.msg("storage.recording"))
            chk = check(self.cfg, self.db, target, relink=True, service=self.service)
            if not chk["ok"]:
                raise MoveRefused(chk["problems"][0])
            src, dst = _abs(self.cfg.data_dir), Path(chk["path"])
            self._switch(src, dst, chk["mount"])
            self.state = {"kind": "relink", "phase": "done", "src": str(src), "dst": str(dst),
                          "files_total": chk["files"], "files_done": chk["files"], "bytes_total": 0, "bytes_done": 0,
                          "started_at": time.time(), "finished_at": time.time(), "kept": 0}
            self._save(force=True)
            log.info("User-data folder relinked from %s to %s", src, dst)
            return self.status()

    # ─── The work ────────────────────────────────────────────────────────────────────────────────────────────────
    def _switch(self, src: Path, dst: Path, mount: str, moved: list[dict] = ()) -> None:
        """Rewrite the stored paths from `src` to `dst` (and of the files from outside in `moved`) and keep the choice,
        in one transaction; then use `dst`."""
        s = str(src)
        with self.db.lock:
            c = self.db.conn
            c.execute("BEGIN")
            try:
                for item in moved:
                    if item["table"]:
                        c.execute(f"UPDATE {item['table']} SET path = ? WHERE path = ?",
                                  (str(dst / item["rel"]), str(item["src"])))
                if dst == src:                        # gathered into the folder in use: nothing else changes
                    c.execute("COMMIT")
                    return
                for table in ("recordings", "masters"):
                    c.execute(f"UPDATE {table} SET path = ? || substr(path, ?) WHERE substr(path, 1, ?) = ?",
                              (str(dst), len(s) + 1, len(s) + 1, s + os.sep))
                for key, value in ((S_DATA_DIR, str(dst)), (S_REQUIRE_MOUNT, mount)):
                    c.execute("INSERT INTO settings (key, value, changed_at) VALUES (?, ?, strftime('%s','now')) "
                              "ON CONFLICT(key) DO UPDATE SET value = excluded.value, changed_at = excluded.changed_at",
                              (key, value))
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise
        object.__setattr__(self.cfg, "data_dir", dst)
        object.__setattr__(self.cfg, "require_mount", mount)

    def _run(self, src: Path, dst: Path) -> None:
        st = self.state
        t0 = time.monotonic()
        cancelled = self._cancel.is_set

        def step(n: int) -> None:
            st["units_done"] += n
            st["bytes_done"] = st["units_done"] // 2
            elapsed = time.monotonic() - t0
            if elapsed > 1:
                speed = st["units_done"] / elapsed                     # bytes read and written per second
                st["speed_mb_s"] = round(speed / 2 / 1e6, 1)
                st["eta_s"] = round((2 * st["bytes_total"] - st["units_done"]) / speed) if speed else None
            self._save()
        copied: list[dict] = []
        try:
            todo, _ = plan(self.cfg, self.db, dst)
            st["files_total"], st["bytes_total"] = len(todo), sum(i["size"] for i in todo)
            for item in todo:
                if cancelled():
                    raise _Cancelled
                s, d, size = item["src"], dst / item["rel"], item["size"]
                st["current"] = item["rel"]
                if s == d:
                    step(2 * size)                                     # an outside file that is in place already
                    copied.append(item | {"in_place": True})
                    st["files_done"] += 1
                    continue
                if d.is_file() and d.stat().st_size == size and _sha256(d, None, cancelled) == _sha256(s, None, cancelled):
                    step(2 * size)                                     # already there and identical (a resumed move)
                else:
                    copy_verified(s, d, step, cancelled)
                copied.append(item)
                st["files_done"] += 1
                self._save()
            st["current"], st["phase"] = None, "switching"
            self._save(force=True)
            while self.recording():                                    # cannot start during a move; just in case
                time.sleep(1)
            self._switch(src, dst, st.get("mount", ""), copied)
            log.info("User-data folder switched to %s", dst)
            if st.get("delete_originals"):
                st["phase"] = "cleaning"
                self._save(force=True)
                deadline = time.monotonic() + WAIT_IDLE_S
                while self.analysing() and time.monotonic() < deadline:   # an analysis may still read an original
                    time.sleep(2)
                kept = 0
                for item in copied:
                    if item.get("in_place"):
                        continue
                    try:
                        item["src"].unlink()
                    except FileNotFoundError:
                        pass
                    except OSError as e:
                        kept += 1
                        log.warning("Original %s kept: %s", item["src"], e)
                        continue
                    if item["table"]:
                        _prune(item["src"].parent)
                for sub in SUBDIRS if dst != src else ():              # empty folders left behind, deepest first
                    base = src / sub
                    if base.is_dir():
                        for dirpath, _, _ in sorted(os.walk(base), key=lambda w: -len(w[0])):
                            try:
                                Path(dirpath).rmdir()
                            except OSError:
                                pass
                st["kept"] = kept
            st["phase"], st["finished_at"] = "done", time.time()
            log.info("Move of the user-data folder done: %d files, %.1f GB", st["files_done"], st["bytes_total"] / 1e9)
        except _Cancelled:
            st["phase"], st["finished_at"] = "cancelled", time.time()
            log.info("Move of the user-data folder cancelled after %d of %d files", st["files_done"], st["files_total"])
        except Exception as e:  # noqa: BLE001 — reported in the settings; the station keeps the old folder
            st["phase"], st["finished_at"] = "failed", time.time()
            st["error"] = e.message if isinstance(e, i18n.MessageError) else str(e)
            log.exception("Move of the user-data folder failed")
        finally:
            st["current"] = None
            self._save(force=True)


# ─── Routes ──────────────────────────────────────────────────────────────────────────────────────────────────────

def info(cfg, db, mover: Mover) -> dict:
    data = _abs(cfg.data_dir)
    saved = db.setting(S_DATA_DIR)
    out = outside(cfg, db)
    found = [o for o in out if o["size"] is not None]
    folders = sorted({str(o["src"].parent) for o in found})
    return {"app": {"path": str(_abs(cfg.state_dir)), **disk(cfg.state_dir), "mount": mount_point(cfg.state_dir),
                    "drive": drive(cfg.state_dir)},
            "data": {"path": str(data), "source": "settings" if saved else "config", "exists": data.is_dir(),
                     "require_mount": cfg.require_mount or None, "mount": mount_point(data), "drive": drive(data),
                     "mounted": os.path.ismount(cfg.require_mount) if cfg.require_mount else None,
                     **disk(data), "usage": usage(data),
                     "outside": {"files": len(found), "bytes": sum(o["size"] for o in found),
                                 "missing": len(out) - len(found), "folders": folders[:5],
                                 "recordings": sum(1 for o in found if o["table"] == "recordings"),
                                 "masters": sum(1 for o in found if o["table"] == "masters")}},
            "targets": targets(cfg, db, service=mover.service, config_dir=mover.config_dir),
            "service": mover.service, "move": mover.status()}


def install(app, cfg, db, mover: Mover) -> None:
    """Routes: GET /api/storage, POST /api/storage/check, /move, /move/cancel, /move/dismiss, /relink."""
    from fastapi import HTTPException

    def path_of(body: dict | None) -> str:
        p = (body or {}).get("path")
        if not isinstance(p, str):
            raise HTTPException(422, i18n.msg("storage.not_absolute"))
        return p

    @app.get("/api/storage")
    def storage_info():
        return info(cfg, db, mover)

    @app.post("/api/storage/check")
    def storage_check(body: dict | None = None):
        return check(cfg, db, path_of(body), relink=bool((body or {}).get("relink")), service=mover.service)

    @app.post("/api/storage/move")
    def storage_move(body: dict | None = None):
        try:
            return mover.start(path_of(body), delete_originals=bool((body or {}).get("delete_originals", True)))
        except MoveRefused as e:
            raise HTTPException(409, e.message) from e

    @app.post("/api/storage/move/cancel")
    def storage_move_cancel():
        return {"move": mover.cancel()}

    @app.post("/api/storage/move/dismiss")
    def storage_move_dismiss():
        mover.dismiss()
        return info(cfg, db, mover)

    @app.post("/api/storage/relink")
    def storage_relink(body: dict | None = None):
        try:
            mover.relink(path_of(body))
        except MoveRefused as e:
            raise HTTPException(409, e.message) from e
        return info(cfg, db, mover)
