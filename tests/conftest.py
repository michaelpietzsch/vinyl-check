import sys
from pathlib import Path

import starlette.testclient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# The interface accepts only Host headers that name the station (an address, localhost, a .local name, its host
# name, a configured proxy name): the test client's default "testserver" is none of these. Tests therefore address
# the app as the computer itself unless they say otherwise (tests of the Host check set their own base_url).
_test_client_init = starlette.testclient.TestClient.__init__


def _local_test_client_init(self, app, base_url="http://127.0.0.1", **kwargs):
    _test_client_init(self, app, base_url=base_url, **kwargs)


starlette.testclient.TestClient.__init__ = _local_test_client_init

# Looking for a new version asks GitHub a minute after the start; tests never wait that long on purpose, but a slow
# machine might: they ask only where they say so (tests/test_updates.py fakes the answer)
from vinylcheck import updates  # noqa: E402

updates.FIRST_AFTER_S = 10 ** 6          # eleven days; Windows refuses to wait longer than seven weeks

# A test that builds an app without the lifespan (TestClient without `with`) leaves its worker and, after a heavy
# job, the analysis child process behind; at exit the interpreter then waits for that child for good (seen on
# macOS in CI and locally: a green run that never ended). Every worker is closed when the session ends.
import pytest  # noqa: E402

from vinylcheck import jobs  # noqa: E402

_workers: list = []
_worker_init = jobs.Worker.__init__


def _tracked_worker_init(self, *args, **kwargs):
    _worker_init(self, *args, **kwargs)
    _workers.append(self)


jobs.Worker.__init__ = _tracked_worker_init


@pytest.fixture(autouse=True, scope="session")
def _close_workers_at_the_end():
    yield
    for w in _workers:
        try:
            w.close(timeout=5)
        except Exception:  # noqa: BLE001 — teardown must not fail the run
            pass
