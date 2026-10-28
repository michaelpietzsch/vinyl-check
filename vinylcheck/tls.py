"""Trusted certificates for the HTTPS connections of the station (Discogs, the Vinyl-Mind hub, the account service).

Python's ssl module asks the OpenSSL it runs with where the trusted root certificates are. A packaged desktop app
(PyInstaller) brings its own OpenSSL, and that one points to a directory of the machine it was built on, which does
not exist on the user's computer: every HTTPS connection then fails with CERTIFICATE_VERIFY_FAILED. So the context
comes, in this order, from

1. the variable SSL_CERT_FILE or SSL_CERT_DIR, if set (OpenSSL reads them itself: a user's own choice wins),
2. the operating system's trust store, through `truststore` (macOS keychain, Windows certificate store; installed
   on these two systems), which also knows certificates an administrator added,
3. the certificate bundle of `certifi`, if OpenSSL's own paths do not exist and certifi is installed,
4. OpenSSL's defaults (Linux and the Raspberry Pi: the system's OpenSSL knows where its certificates are).

Certificates are always verified and the host name is always checked; there is no switch to turn that off.
"""

from __future__ import annotations

import os
import ssl
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=4)
def _context(cert_file: str | None, cert_dir: str | None) -> tuple[ssl.SSLContext, str]:
    """Created once per value of the two variables, so that setting one later (a test, a user) takes effect."""
    if cert_file or cert_dir:
        return ssl.create_default_context(), "environment"
    try:
        import truststore
    except ImportError:
        pass
    else:
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT), "system"
    paths = ssl.get_default_verify_paths()
    if not any(p and Path(p).exists() for p in (paths.cafile, paths.capath)):
        try:
            import certifi
        except ImportError:
            pass
        else:
            return ssl.create_default_context(cafile=certifi.where()), "certifi"
    return ssl.create_default_context(), "openssl"


def _env() -> tuple[str | None, str | None]:
    return os.environ.get("SSL_CERT_FILE") or None, os.environ.get("SSL_CERT_DIR") or None


def context() -> ssl.SSLContext:
    """The SSL context for every outgoing HTTPS request (shared, created once)."""
    return _context(*_env())[0]


def source() -> str:
    """Where the trusted certificates come from: "environment", "system", "certifi" or "openssl"."""
    return _context(*_env())[1]
