"""Trusted certificates for outgoing HTTPS (tls.py): where they come from, and that every HTTPS caller uses them."""
import ssl
import sys
import types
import urllib.error
import urllib.request

import pytest

from vinylcheck import discogs, tls, vinylmind


@pytest.fixture(autouse=True)
def fresh_context(monkeypatch):
    for k in ("SSL_CERT_FILE", "SSL_CERT_DIR"):
        monkeypatch.delenv(k, raising=False)
    tls._context.cache_clear()
    yield
    tls._context.cache_clear()


def test_always_verifies():
    ctx = tls.context()
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname
    assert tls.context() is ctx                                  # created once, shared


@pytest.mark.skipif(sys.platform not in ("darwin", "win32"), reason="truststore is installed on macOS and Windows")
def test_system_trust_store_on_macos_and_windows():
    assert tls.source() == "system"


def test_environment_wins(monkeypatch, tmp_path):
    before = tls.context()
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "cert.pem"))      # also when set after the first use
    assert tls.source() == "environment" and tls.context() is not before
    monkeypatch.delenv("SSL_CERT_FILE")
    assert tls.context() is before


def test_certifi_when_openssl_paths_are_missing(monkeypatch, tmp_path):
    """The case of a packaged app: OpenSSL's paths point to the build machine. Without truststore, certifi is used."""
    monkeypatch.setitem(sys.modules, "truststore", None)         # import fails
    missing = str(tmp_path / "nowhere")
    monkeypatch.setattr(ssl, "get_default_verify_paths",
                        lambda: types.SimpleNamespace(cafile=missing + "/cert.pem", capath=missing))
    bundle = tmp_path / "cacert.pem"
    used = {}
    real = ssl.create_default_context

    def fake_default(cafile=None, **kw):
        used["cafile"] = cafile
        return real()
    monkeypatch.setattr(ssl, "create_default_context", fake_default)
    monkeypatch.setitem(sys.modules, "certifi", types.SimpleNamespace(where=lambda: str(bundle)))
    assert tls.source() == "certifi" and used["cafile"] == str(bundle)


def test_openssl_defaults_when_their_paths_exist(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "truststore", None)
    (tmp_path / "cert.pem").write_text("")
    monkeypatch.setattr(ssl, "get_default_verify_paths",
                        lambda: types.SimpleNamespace(cafile=str(tmp_path / "cert.pem"), capath=None))
    assert tls.source() == "openssl"


def test_every_https_caller_uses_the_shared_context(monkeypatch, tmp_path):
    seen = []

    class Stop(Exception):
        pass

    def fake_urlopen(req, timeout=None, context=None):
        seen.append(context)
        raise urllib.error.URLError(Stop())
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    dc = discogs.Discogs("token", "user", tmp_path / "c.json", "test")
    for call in (lambda: dc.identity("token"), lambda: dc._oauth_call(discogs.API + "/oauth/request_token", "x"),
                 lambda: discogs.Discogs("", "", tmp_path / "c.json", "test")._public_or_auth(discogs.API + "/releases/1")):
        with pytest.raises(discogs.DiscogsError):
            call()
    assert len(seen) == 3 and all(c is tls.context() for c in seen)
    https = [h for h in vinylmind._opener().handlers if isinstance(h, urllib.request.HTTPSHandler)]
    assert https and https[0]._context is tls.context()
