"""License texts: /licenses from source and in a built app, and the third-party notices written at build time."""

import importlib.util
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from vinylcheck.config import Config
from vinylcheck.web import create_app

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def client(tmp_path):
    cfg = replace(Config(), data_dir=tmp_path / "data", state_dir=tmp_path / "state", min_free_gb=0.0)
    with TestClient(create_app(cfg)) as c:
        yield c


def test_licenses_from_source(client):
    r = client.get("/licenses")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    text = r.text
    assert "non-commercial" in text and "License of vinyl-check" in text
    if (ROOT / "LICENSE").is_file():
        assert (ROOT / "LICENSE").read_text(encoding="utf-8").strip()[:60] in text
    else:                                       # deploy.sh copies only the package and the tests to a Pi
        assert "license file is missing" in text
    assert "SIL Open Font License" in text and "GrooveClean" in text          # what the package itself ships
    de = client.get("/licenses", params={"lang": "de"}).text
    assert "nicht-kommerzielle" in de and "Software von Dritten" in de


def test_licenses_in_built_app(client, tmp_path, monkeypatch):
    (tmp_path / "LICENSE.txt").write_text("Bundled license text", encoding="utf-8")
    (tmp_path / "THIRD-PARTY-NOTICES.txt").write_text("Bundled notices: numpy 2", encoding="utf-8")
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    text = client.get("/licenses").text
    assert "Bundled license text" in text and "Bundled notices: numpy 2" in text


def _notices_module():
    path = ROOT / "packaging" / "third_party_notices.py"
    if not path.exists():                       # deploy.sh copies only the package and the tests to a Pi
        pytest.skip("packaging/ not present")
    spec = importlib.util.spec_from_file_location("third_party_notices", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_third_party_notices(tmp_path):
    tpn = _notices_module()
    toc = [("numpy/_core/_multiarray_umath.so", "x", "EXTENSION"), ("soundfile", "x", "PYMODULE"),
           ("_soundfile_data/libsndfile.dylib", "x", "BINARY"), ("fastapi.routing", "x", "PYMODULE"),
           ("_cffi_backend.cpython-313-darwin.so", "x", "EXTENSION")]
    modules = tpn.top_levels(toc)
    assert modules == {"numpy", "soundfile", "_soundfile_data", "fastapi", "_cffi_backend"}
    out = tpn.write(tmp_path / "THIRD-PARTY-NOTICES.txt", modules, binaries={"libportaudio64bit-asio.dll"})
    text = out.read_text(encoding="utf-8")
    assert text.startswith("vinyl-check ") and "third-party notices" in text
    for needle in ("Python Software Foundation", "PyInstaller bootloader", "libsndfile (in soundfile)", "LGPL-2.1",
                   "can be replaced", "Steinberg", "IBM Plex fonts", "OFL-1.1", "GrooveClean", "SIL Open Font License"):
        assert needle in text, needle
    names = {line.split(". ", 1)[1].split(" ")[0].lower() for line in text.splitlines()
             if line[:6].strip().rstrip(".").isdigit() and ". " in line}
    assert {"numpy", "soundfile", "fastapi", "cffi"} <= names
    assert "pip" not in names and "setuptools" not in names and "vinyl-check" not in names
    assert "numpy" in text and "BSD" in text                              # numpy's license texts are quoted
