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

updates.FIRST_AFTER_S = 10 ** 9
