"""Translations: catalogs complete and consistent, messages rendered per request language."""

import json
import re
import string
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from vinylcheck import i18n
from vinylcheck.config import Config
from vinylcheck.web import create_app

ROOT = Path(__file__).resolve().parents[1]


def _fields(template) -> set[str]:
    texts = template.values() if isinstance(template, dict) else [template]
    out = set()
    for s in texts:
        out |= {f for _, f, _, _ in string.Formatter().parse(s) if f}
    return out


@pytest.mark.parametrize("web", [False, True], ids=["server", "web"])
def test_catalogs_match_english(web):
    """Every language has exactly the English keys, with the same placeholders."""
    en = i18n.catalog("en", web=web)
    for lang in i18n.languages():
        cat = i18n.catalog(lang, web=web)
        assert set(cat) == set(en), (lang, sorted(set(en) ^ set(cat))[:20])
        for k, v in en.items():
            assert _fields(cat[k]) == _fields(v), (lang, k)


def test_catalog_files_are_flat_json():
    for f in list((ROOT / "vinylcheck/locales").rglob("*.json")) + list((ROOT / "vinylcheck/static/locales").rglob("*.json")):
        data = json.loads(f.read_text(encoding="utf-8"))
        for k, v in data.items():
            assert re.fullmatch(r"[a-z0-9_.]+", k), (f, k)
            assert isinstance(v, str) or (isinstance(v, dict) and "other" in v), (f, k)


def test_keys_used_in_code_exist():
    """Literal keys in msg()/t()/T() calls must be in the English catalogs (dynamic keys are not checked)."""
    server, web = i18n.catalog("en"), i18n.catalog("en", web=True)
    for f in (ROOT / "vinylcheck").glob("*.py"):
        if f.name == "i18n.py":                                      # examples in the docstring
            continue
        for k in re.findall(r"""\b(?:msg|i18n\.t|t)\(\s*["']([a-z0-9_]+\.[a-z0-9_.]+)["']""", f.read_text(encoding="utf-8")):
            assert k in server, (f.name, k)
    for f in (ROOT / "vinylcheck/static").glob("*.js"):
        if f.name == "i18n.js":                                      # examples in the comments
            continue
        for k in re.findall(r"""\bT\(\s*["'`]([a-z0-9_]+\.[a-z0-9_.]+)["'`]""", f.read_text(encoding="utf-8")):
            assert k in web, (f.name, k)
    html = (ROOT / "vinylcheck/static/index.html").read_text(encoding="utf-8")
    for k in re.findall(r'data-i18n(?:-[a-z-]+)?="([^"]+)"', html):
        assert k in web, ("index.html", k)


def test_render_and_numbers():
    m = i18n.msg("common.datetime_format")
    assert i18n.render(m, "de") == "%d.%m.%Y %H:%M"
    assert i18n.render("plain text", "de") == "plain text"          # texts stored before 1.0 pass through
    assert i18n.num(-12.345, 1, "de") == "-12,3" and i18n.num(-12.345, 1, "en") == "-12.3"
    assert i18n.num(None) == "–"
    nested = {"a": [i18n.msg("common.date_format")], "b": i18n.msg_join([i18n.msg("common.date_format"), "x"])}
    assert i18n.localize(nested, "en") == {"a": ["%Y-%m-%d"], "b": "%Y-%m-%d x"}


def test_stored_messages_and_errors():
    m = i18n.msg("grading.clipped", n=4000)
    text = i18n.dumps(m)
    assert isinstance(text, str) and i18n.loads_text(text) == m
    assert i18n.dumps("plain") == "plain" and i18n.loads_text("plain") == "plain"     # texts from before 1.0
    assert i18n.loads_text('{"$t": broken') == '{"$t": broken'
    e = i18n.MessageError(m)
    assert e.message == m and i18n.error_message(e) == m and i18n.error_message(ValueError(m)) == m
    assert i18n.error_message(RuntimeError("boom")) == "boom"
    with i18n.use_lang("en"):
        assert str(e) == i18n.t("grading.clipped", "en", n=4000)
    with i18n.use_lang("de"):
        assert str(e) == i18n.t("grading.clipped", "de", n=4000) != str(i18n.t("grading.clipped", "en", n=4000))


def test_negotiation():
    assert i18n.negotiate("de-DE,de;q=0.9,en;q=0.8") == "de"
    assert i18n.negotiate("fr-FR,fr;q=0.9") == "en"
    assert i18n.negotiate("fr;q=0.9,de;q=0.5") == "de"
    assert i18n.resolve("en", "de", "de") == "en" and i18n.resolve(None, "de", "en") == "de"
    assert i18n.resolve(None, None, "de", "en") == "en" and i18n.resolve(None, None, "en", "de") == "de"
    assert i18n.resolve(None, None, "de", "auto") == "de" and i18n.resolve(None, "en", "de", "de") == "en"


def test_request_language(tmp_path):
    cfg = Config(data_dir=tmp_path / "data", state_dir=tmp_path / "state", min_free_gb=0.0)
    with TestClient(create_app(cfg)) as c:
        assert 'lang="en"' in c.get("/", headers={"Accept-Language": "de"}).text     # English by default
        c.cookies.set("vc_lang", "de")                              # the interface's language switch
        js = c.get("/i18n/catalog.js").text
        assert js.startswith("window.VC_I18N=") and '"lang":"de"' in js


@pytest.mark.parametrize("language, header, expected", [("en", "de", "en"), ("de", "en", "de"), ("auto", "de", "de"),
                                                        ("auto", "fr", "en")])
def test_configured_default_language(tmp_path, language, header, expected):
    """The configured language applies until a user picks one; only "auto" follows the browser."""
    cfg = Config(data_dir=tmp_path / "data", state_dir=tmp_path / "state", min_free_gb=0.0, language=language)
    with TestClient(create_app(cfg)) as c:
        assert f'lang="{expected}"' in c.get("/", headers={"Accept-Language": header}).text
        c.cookies.set("vc_lang", "en" if expected == "de" else "de")         # the user's choice wins
        assert f'lang="{"en" if expected == "de" else "de"}"' in c.get("/").text


def test_unknown_language_stops_the_start(tmp_path):
    cfg = Config(data_dir=tmp_path / "data", state_dir=tmp_path / "state", min_free_gb=0.0, language="xx")
    with pytest.raises(ValueError):
        create_app(cfg)
