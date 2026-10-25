"""End to end against the real Vinyl-Mind hub: joining with a code and by signing in (both flows), contributing, a
global model built, signed and published by the hub, checked and reported by the station, leaving.

Runs only when VINYL_MIND_BIN names a built hub binary (from the Vinyl-Mind repository:
`go build -o /some/dir/vinyl-mind ./cmd/vinyl-mind`), so the default test run and CI need no Go. VINYL_CHECK_SKIP_HUB=1
skips it even then. The hub runs on 127.0.0.1 with a data directory of its own; the account service is the fake of
test_vinylmind.py, served over HTTPS (the hub accepts only an https connect_url) with a throwaway certificate that
only this test trusts (SSL_CERT_FILE). Invitations come from the hub's command line, as an account service's pool
would get them."""

import datetime
import json
import os
import socket
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from fastapi.testclient import TestClient

from test_vinylmind import LOCAL, FakeAccount, _app, _seed, _set_server
from vinylcheck import vinylmind

BIN = os.environ.get("VINYL_MIND_BIN", "")
pytestmark = pytest.mark.skipif(not BIN or not os.access(BIN, os.X_OK) or os.environ.get("VINYL_CHECK_SKIP_HUB") == "1",
                                reason="VINYL_MIND_BIN does not name a Vinyl-Mind hub binary")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _certificates(d: Path) -> tuple[Path, Path, Path]:
    """A throwaway CA and a server certificate for 127.0.0.1 signed by it: (CA file, certificate, key)."""
    now = datetime.datetime.now(datetime.timezone.utc)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "vinyl-check test CA")])
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name).public_key(ca_key.public_key())
          .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(minutes=5))
          .not_valid_after(now + datetime.timedelta(days=1))
          .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
          .add_extension(x509.KeyUsage(digital_signature=False, content_commitment=False, key_encipherment=False,
                                       data_encipherment=False, key_agreement=False, key_cert_sign=True,
                                       crl_sign=True, encipher_only=False, decipher_only=False), critical=True)
          .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
          .sign(ca_key, hashes.SHA256()))
    key = ec.generate_private_key(ec.SECP256R1())
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])).issuer_name(ca_name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5)).not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(__import__("ipaddress").ip_address("127.0.0.1"))]),
                           critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256()))
    files = (d / "ca.pem", d / "cert.pem", d / "key.pem")
    files[0].write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    files[1].write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    files[2].write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    return files


class _TLSServer:
    """The fake account service on https://127.0.0.1."""

    def __init__(self, app, cert: Path, key: Path):
        import uvicorn
        self.port = _free_port()
        config = uvicorn.Config(app, host="127.0.0.1", port=self.port, log_config=None, access_log=False,
                                ssl_certfile=str(cert), ssl_keyfile=str(key), lifespan="off")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True, name="fake-account-tls")

    def start(self):
        self.thread.start()
        deadline = time.monotonic() + 20
        while not self.server.started:
            assert self.thread.is_alive() and time.monotonic() < deadline, "the account service did not start"
            time.sleep(0.05)

    def stop(self):
        self.server.should_exit = True
        self.thread.join(10)


class RealHub:
    """A running `vinyl-mind serve`, its command line, and what FakeAccount needs of a hub."""

    def __init__(self, d: Path, connect_url: str, binding_key: str):
        self.dir, self.port = d, _free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        (d / "data").mkdir(parents=True)
        (d / "config.toml").write_text(f"""
listen = "127.0.0.1:{self.port}"
join_mode = "invitation"
joins_per_hour = 100
joins_per_day = 100
station_requests_per_min = 600
address_requests_per_min = 1200

[aggregation]
min_profiles = 1
min_stations = 1
min_class_profiles = 1

[publication]
auto = false
combine_interval_s = 0
min_reports = 1
min_report_stations = 1
min_vote_age_days = 0

[clients]
allowed = ["vinyl-check/1.1.*"]
detector_versions = ["1"]

[accounts]
connect_url = "{connect_url}"
binding_keys = ["{binding_key}"]
""")
        self.env = dict(os.environ, VINYL_MIND_DATA_DIR=str(d / "data"), VINYL_MIND_CONFIG=str(d / "config.toml"))
        self.proc = subprocess.Popen([BIN, "serve"], env=self.env, stdout=subprocess.DEVNULL,
                                     stderr=open(d / "hub.log", "wb"))
        deadline = time.monotonic() + 30
        while True:
            try:
                with urllib.request.urlopen(self.url + "/healthz", timeout=1) as r:
                    if r.status == 200:
                        break
            except OSError:
                pass
            assert self.proc.poll() is None and time.monotonic() < deadline, (d / "hub.log").read_text()
            time.sleep(0.1)
        self.fp = self.cli("fingerprint").strip()

    def cli(self, *args) -> str:
        r = subprocess.run([BIN, *args], env=self.env, capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr
        return r.stdout

    def invite(self, bound: bool = False) -> tuple[int, str]:
        line = json.loads(self.cli("invite", "-json", "-uses", "1", "-days", "1", *(["-bound"] if bound else [])))
        assert line["bound"] is bound
        return line["id"], line["code"]

    def stations(self) -> list[dict]:
        return [json.loads(x) for x in self.cli("stations", "-json").splitlines() if x.strip()]

    def stop(self):
        self.proc.terminate()
        try:
            self.proc.wait(10)
        except subprocess.TimeoutExpired:
            self.proc.kill()


@pytest.fixture()
def real(tmp_path, monkeypatch):
    ca, cert, key = _certificates(tmp_path)
    monkeypatch.setenv("SSL_CERT_FILE", str(ca))                 # trusted in this test only
    account = FakeAccount(type("NotYet", (), {"url": "", "fp": ""})(), invite=lambda bound=False: (0, ""))
    tls = _TLSServer(account.app, cert, key)
    account.url = f"https://127.0.0.1:{tls.port}"
    tls.start()
    hub = RealHub(tmp_path / "hub", account.connect_url, account.public_b64u)
    account.hub, account.invite = hub, hub.invite
    yield hub, account
    hub.stop()
    tls.stop()


def _station(tmp_path, name: str, seed: int):
    app = _app(tmp_path, name)
    _seed(app.state.db, f"Deck {name}", "hw:CARD=Interface,DEV=0", "MC low output", seed=seed)
    _seed(app.state.db, f"USB {name}", "Core Audio: USB Audio CODEC", "Moving magnet", seed=seed + 1)
    return app


def test_station_against_the_real_hub(tmp_path, real):
    hub, account = real

    # flow A: sign in in the browser, come back to the station's loopback address, join with the binding
    app = _station(tmp_path, "a", 1)
    with TestClient(app, **LOCAL) as c:
        info = _set_server(c, hub)
        assert info["fingerprint"] == hub.fp and info["connect"] == {"site": "127.0.0.1"} and info["client_accepted"]
        r = c.post("/api/vinylmind/connect", json={"flow": "browser"}).json()
        page = c.get(account.browse(r["url"]))
        assert page.status_code == 200 and "Connected" in page.text, page.text
        st = c.get("/api/vinylmind").json()
        assert st["state"] == "active" and st["joined"]["via"] == "account"
        station_a = st["station_fingerprint"]
        by_fp = {s["fingerprint"]: s for s in hub.stations()}
        assert by_fp[station_a]["account"] == account.account and by_fp[station_a]["status"] == "active"

        # contribute; the hub combines, signs a candidate; the station verifies, checks and reports it
        c.put("/api/vinylmind", json={"contribute": True, "use_model": True})
        r = c.post("/api/vinylmind/sync")
        assert r.status_code == 200, r.text
        assert r.json()["result"]["contributions"]["sent"] == 2
        out = hub.cli("combine")
        assert "new candidate" in out, out
        r = c.post("/api/vinylmind/sync").json()
        assert r["result"]["model"] == "new" and r["status"]["model"]["status"] == "candidate"
        assert r["status"]["prior"] is None                                       # a candidate is not adopted
        version = r["status"]["model"]["version"]
        assert str(version) in hub.cli("models")
        # the operator publishes it; the station checks it again and adopts it where it is not worse
        hub.cli("publish", str(version))
        r = c.post("/api/vinylmind/sync").json()
        assert r["result"]["model"] == "new" and r["status"]["model"]["status"] == "published"
        if r["status"]["model"]["adopted"]:
            assert r["status"]["prior"]["source"] == "server" and r["status"]["prior"]["ref"] == version
        # a withdrawn model is dropped
        hub.cli("withdraw", str(version), "-reason", "end-to-end test")
        r = c.post("/api/vinylmind/sync").json()
        assert r["status"]["prior"] is None

    # flow B: a device code, confirmed on another device
    app_b = _station(tmp_path, "b", 5)
    with TestClient(app_b) as remote:
        _set_server(remote, hub)
        r = remote.post("/api/vinylmind/connect", json={"flow": "device"})
        assert r.status_code == 200, r.text
        assert r.json()["status"]["connect"]["user_code"]
        deadline = time.time() + 20
        while time.time() < deadline and remote.get("/api/vinylmind").json()["state"] != "active":
            time.sleep(0.3)
        st = remote.get("/api/vinylmind").json()
        assert st["state"] == "active", st
        assert {s["fingerprint"]: s for s in hub.stations()}[st["station_fingerprint"]]["account"] == account.account

    # an invitation code of a hub operator, without an account
    app_c = _station(tmp_path, "c", 9)
    with TestClient(app_c, **LOCAL) as c:
        info = _set_server(c, hub)
        r = c.post("/api/vinylmind/join", json={"invitation": hub.invite()[1], "fingerprint": info["fingerprint"]})
        assert r.status_code == 200 and r.json()["state"] == "active", r.text
        st = r.json()
        assert {s["fingerprint"]: s for s in hub.stations()}[st["station_fingerprint"]]["account"] in (None, "")
        # leaving deletes the station on the hub
        assert c.post("/api/vinylmind/leave").json()["state"] == "not_joined"
        assert st["station_fingerprint"] not in {s["fingerprint"] for s in hub.stations()}


@pytest.mark.parametrize("case", ["no_binding", "other_station", "fingerprint"])
def test_sign_in_refused_by_the_real_hub(tmp_path, real, case):
    hub, account = real
    before = {s["fingerprint"] for s in hub.stations()}
    app = _station(tmp_path, case, 3)
    if case == "no_binding":
        account.omit_binding = True
    elif case == "other_station":
        account.bind_station = vinylmind.StationKey.generate().fingerprint
    else:
        account.server_fp = "SHA256:" + "A" * 43
    with TestClient(app, **LOCAL) as c:
        _set_server(c, hub)
        r = c.post("/api/vinylmind/connect", json={"flow": "browser"}).json()
        page = c.get(account.browse(r["url"]))
        assert page.status_code == 400
        expect = {"no_binding": "needs the confirmation", "other_station": "does not match",
                  "fingerprint": "another server"}[case]
        assert expect in page.text, page.text
        assert c.get("/api/vinylmind").json()["state"] == "not_joined"
    assert {s["fingerprint"] for s in hub.stations()} == before
    if case == "other_station":
        # the refused join did not use up the code: the station it is bound to can still join with it
        grant = account.issued[-1]
        key = vinylmind.StationKey.generate()
        good = account.binding(grant["invitation_id"], key.fingerprint)
        joined = vinylmind.Client(hub.url, key, hub.fp).join(grant["invitation"], good)
        assert joined["status"] == "active"
