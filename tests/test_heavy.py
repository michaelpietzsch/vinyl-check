"""The worker initializer must start on every platform, otherwise every analysis fails."""

import os

from vinylcheck import heavy


def test_init_worker_without_os_nice(monkeypatch):
    monkeypatch.delattr(os, "nice", raising=False)                   # Windows has no os.nice
    heavy.init_worker()
