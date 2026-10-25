"""Website builder: release assets are classified by file name and listed with size and checksum."""

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if not (ROOT / "site" / "build.py").is_file():                # deployments copy only vinylcheck/ and tests/
    pytest.skip("site/ not present", allow_module_level=True)
spec = importlib.util.spec_from_file_location("site_build", ROOT / "site" / "build.py")
site = importlib.util.module_from_spec(spec)
spec.loader.exec_module(site)


def _asset(name, size=1000, digest="sha256:" + "ab" * 32):
    return {"name": name, "size": size, "digest": digest, "browser_download_url": f"https://example.org/{name}"}


RELEASES = [
    {"tag_name": "v1.0.0", "published_at": "2026-10-23T07:34:00Z", "html_url": "https://example.org/v1.0.0",
     "assets": [_asset("vinyl_check-1.0.0-py3-none-any.whl"), _asset("vinyl_check-1.0.0.tar.gz", digest=None)]},
    {"tag_name": "v1.0.1", "published_at": "2026-10-24T10:00:00Z", "html_url": "https://example.org/v1.0.1",
     "assets": [_asset("vinyl-check-1.0.1-windows-x64-portable.zip"), _asset("vinyl-check-1.0.1-windows-x64-setup.exe"),
                _asset("vinyl-check-1.0.1-macos-arm64.dmg", 168_000_000)]},
    {"tag_name": "v1.1.0", "draft": True, "assets": []},
]


def test_classify():
    assert site.classify("vinyl-check-1.0.1-macos-arm64.dmg") == ("macos", "arm64")
    assert site.classify("vinyl-check-1.0.1-windows-x64-setup.exe") == ("windows", "x64")
    assert site.classify("vinyl_check-1.0.0-py3-none-any.whl") == ("any", "any")
    assert site.classify("vinyl_check-1.0.0.tar.gz") == ("source", "any")


def test_model_newest_first_installer_first_drafts_hidden():
    model = site.releases_model(RELEASES)
    assert [r["version"] for r in model] == ["1.0.1", "1.0.0"] and model[0]["latest"] and not model[1]["latest"]
    assert [f["name"] for f in model[0]["files"]] == [
        "vinyl-check-1.0.1-macos-arm64.dmg", "vinyl-check-1.0.1-windows-x64-setup.exe",
        "vinyl-check-1.0.1-windows-x64-portable.zip"]
    assert model[0]["files"][0]["sha256"] == "ab" * 32 and model[1]["files"][1]["sha256"] is None


def test_build_writes_pages_and_json(tmp_path):
    out = tmp_path / "_site"
    site.build(RELEASES, "owner/vinyl-check", out)
    page = (out / "download" / "index.html").read_text(encoding="utf-8")
    assert "LATEST" in page and "160.22 MiB" in page and "Code signing policy" in page
    assert json.loads((out / "download.json").read_text())[0]["version"] == "1.0.1"
    assert (out / "assets" / "vinyl-check-logo-inverse.svg").exists() and (out / ".nojekyll").exists()

