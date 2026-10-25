"""CPU-heavy jobs that the worker (jobs.py) runs in a child process with low priority.

Running the analysis as a thread inside the service process would share cores and the interpreter with the web
interface and the recorder: with four threads for the second opinion the interface became sluggish and the
recording survived only thanks to arecord's buffer. So everything heavy runs in one process at `nice 10`; if it
dies (out of memory), the service keeps running. The functions take only paths and plain data (pickled across the
process boundary) and return dicts.
"""

from __future__ import annotations

import os
import sys

import soundfile as sf

from . import analysis, compare, alignment, i18n, master, spectro

NICE = 10


def _lower_priority() -> None:
    """`nice 10` on Linux and macOS, "below normal" on Windows (which has no os.nice)."""
    try:
        if hasattr(os, "nice"):
            os.nice(NICE)
        elif sys.platform == "win32":
            import ctypes
            below_normal = 0x4000                                      # BELOW_NORMAL_PRIORITY_CLASS
            ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), below_normal)
    except OSError:
        pass                                                           # running at normal priority is fine


def init_worker() -> None:
    """Initializer of the child process: lower its priority and limit PyTorch's threads.

    It must never raise: an exception here kills the pool before the first job runs."""
    _lower_priority()
    try:
        import torch
        torch.set_num_threads(max(1, min(4, os.cpu_count() or 1)))
    except Exception:  # noqa: BLE001 — without PyTorch there is no second opinion, nothing else changes
        pass


def analyze_job(path: str, acfg, gcfg, chain_db, rpm, ref_dbfs, floor, labels, tracks, skip_labels, fixed_gain,
                so_cache: dict | None) -> dict:
    """Record check of one side (analysis.analyze_file)."""
    return analysis.analyze_file(path, acfg, gcfg, chain_db, rpm=rpm, ref_dbfs=ref_dbfs, floor=floor, labels=labels,
                                 tracks=tracks, skip_labels=skip_labels, fixed_gain=fixed_gain, so_cache=so_cache)


def alignment_job(path: str, jcfg, chain_db, detect_params) -> dict:
    """Turntable alignment with a test record (alignment.analyze_file)."""
    return alignment.analyze_file(path, jcfg, chain_db, detect_params=detect_params)


def compare_job(path_a: str, path_b: str, res_a: dict, res_b: dict, rpm: float) -> dict:
    """Before/after comparison of two recordings of the same side (compare.compare)."""
    a, sr_a = sf.read(path_a, dtype="float32", always_2d=True)
    b, sr_b = sf.read(path_b, dtype="float32", always_2d=True)
    if sr_a != sr_b:
        raise RuntimeError(i18n.msg("jobs.sample_rate_mismatch", a=sr_a, b=sr_b))
    return compare.compare(a, b, sr_a, res_a, res_b, rpm=rpm)


def master_job(path: str, master_path: str, res: dict | None, rpm: float, turntable_speed_pct: float | None,
               others: dict) -> dict:
    """Recording measured against the digital master of its side (master.check_files)."""
    return master.check_files(path, master_path, res, rpm=rpm, turntable_speed_pct=turntable_speed_pct, others=others)


def spectro_job(path: str) -> tuple[dict, bytes]:
    """Overview spectrogram: metadata and the uint8 image as bytes."""
    sg = spectro.compute(path)
    return {k: sg[k] for k in ("rows", "cols", "t0", "t1", "fmin", "fmax", "top_db", "floor_db")}, sg["img"].tobytes()
