"""Checks before the heavy imports: a clear line instead of a bare crash.

NumPy 2.4 raised its default x86-64 baseline to x86-64-v2 (SSE4.2, POPCNT: processors from about 2009). On an
older CPU `import numpy` dies with "Illegal instruction" before vinyl-check has printed anything, and the fix is
an older NumPy in the same environment. This module imports nothing heavy so that it can run first.
"""

from __future__ import annotations

import platform
import sys

X86_V2 = ("sse4_2", "popcnt")


def cpu_hint(cpuinfo: str = "/proc/cpuinfo", system: str | None = None, machine: str | None = None) -> str | None:
    """A hint when this x86 Linux processor lacks the x86-64-v2 features NumPy ≥ 2.4 is built for, else None."""
    system = system or platform.system()
    machine = machine or platform.machine()
    if system != "Linux" or machine not in ("x86_64", "AMD64", "i686", "i386"):
        return None
    try:
        with open(cpuinfo, encoding="utf-8", errors="replace") as f:
            flags = next((line.split(":", 1)[1].split() for line in f if line.startswith("flags")), None)
    except OSError:
        return None
    if flags is None:
        return None
    missing = [f for f in X86_V2 if f not in flags]
    if not missing:
        return None
    return (f"this processor lacks {', '.join(m.upper() for m in missing)} (x86-64-v2). NumPy 2.4 and newer stop "
            "here with 'Illegal instruction'. If vinyl-check ends right after this line, install an older NumPy "
            "into the same environment: pip install \"numpy<2.4\"")


def run() -> None:
    """Print the hints to stderr; the start goes on either way."""
    hint = cpu_hint()
    if hint:
        print(f"vinyl-check: {hint}", file=sys.stderr, flush=True)
