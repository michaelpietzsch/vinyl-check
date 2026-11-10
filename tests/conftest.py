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


# A run has to end when its tests have ended. At exit the interpreter waits for threads that are no daemons, joins
# the feeder threads of multiprocessing queues and the manager threads of process pools; when one of them never
# returns, a green run hangs until the CI's time limit (macOS, 2026-11-01; Linux with Python 3.11 and 3.12,
# 2026-11-10: "416 passed", then 35 minutes of nothing). Two guards:
#  - after the summary, threads and child processes that are still alive get a moment; what is left then is named,
#    child processes are terminated, and the process ends with the result of the tests;
#  - a watchdog: if the process is still there a good minute after its tests, it writes the stack of every thread
#    (which shows what the exit is waiting for) and ends the process with the result of the tests. Should even that
#    thread be unable to run, faulthandler ends the process a minute later (then with status 1).
_result: dict = {"status": None}
EXIT_GRACE_S = 10.0
EXIT_WATCH_S = 75.0


def _watch_the_exit(status: int) -> None:
    import faulthandler
    import multiprocessing
    import os
    import threading
    import time

    def run():
        time.sleep(EXIT_WATCH_S)
        err = sys.__stderr__
        try:
            err.write(f"\nThe test process is still alive {EXIT_WATCH_S:.0f} s after its tests ended (result {status}). "
                      "It is ended now; the threads were:\n")
            err.flush()
            faulthandler.dump_traceback(file=err, all_threads=True)
            kids = [f"{p.name} (pid {p.pid}, alive {p.is_alive()})" for p in multiprocessing.active_children()]
            err.write(f"Child processes: {', '.join(kids) or 'none'}\n")
            err.flush()
        finally:
            os._exit(status)
    threading.Thread(target=run, daemon=True, name="tests-exit-watchdog").start()
    faulthandler.dump_traceback_later(EXIT_WATCH_S + 60, exit=True, file=sys.__stderr__)


def pytest_sessionfinish(session, exitstatus):
    _result["status"] = int(exitstatus)
    _watch_the_exit(int(exitstatus))


@pytest.hookimpl(trylast=True)
def pytest_unconfigure(config):
    import multiprocessing
    import os
    import threading
    import time

    if _result["status"] is None:
        return

    def left():
        threads = [t for t in threading.enumerate()
                   if t is not threading.main_thread() and t.is_alive() and not t.daemon]
        return threads, multiprocessing.active_children()

    deadline = time.monotonic() + EXIT_GRACE_S
    threads, children = left()
    while (threads or children) and time.monotonic() < deadline:
        time.sleep(0.2)
        threads, children = left()
    if not threads and not children:
        return
    names = [f"thread {t.name}" for t in threads] + [f"process {p.name} (pid {p.pid})" for p in children]
    sys.stderr.write(f"\nStill alive {EXIT_GRACE_S:.0f} s after the tests: {', '.join(names)}. Ending the run with the "
                     f"tests' result ({_result['status']}) instead of waiting for them.\n")
    for p in children:
        p.terminate()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(_result["status"])
