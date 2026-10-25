"""Translations for everything the server tells people: API errors, analysis warnings, advice, PDF reports.

Code that produces user-facing text returns a *message* instead of a finished string::

    msg("grading.clipped", n=4000)   # -> {"$t": "grading.clipped", "$p": {"n": 4000}}

A message is plain JSON, so it can be stored in the database and rendered later in whatever language the
reader asks for (in text columns as JSON text: ``dumps``/``loads_text``). JSON responses of the web API are
localized on the way out (``localize``); the PDF reports and the WebSocket call ``t``/``localize`` themselves.
Errors a user should see are raised as ``MessageError`` (or a subclass) carrying a message.

Catalogs live in ``vinylcheck/locales/<lang>/<namespace>.json`` (server) and
``vinylcheck/static/locales/<lang>/<namespace>.json`` (web interface). Each file is a flat JSON object; the full
key is ``<namespace>.<key>``. Values are ``str.format`` templates with named fields (``{db:.1f}``), or an object
of plural forms (``{"one": "...", "other": "..."}``) chosen by the parameter ``n``. English is the reference
language and the fallback for missing keys. See docs/translating.md.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import string
import time
from functools import lru_cache
from pathlib import Path

log = logging.getLogger("vinylcheck.i18n")

DEFAULT_LANG = "en"
SERVER_LOCALES = Path(__file__).parent / "locales"
WEB_LOCALES = Path(__file__).parent / "static" / "locales"
COOKIE = "vc_lang"          # set by the web interface's language switch

_lang: contextvars.ContextVar[str] = contextvars.ContextVar("vc_lang", default=DEFAULT_LANG)
_missing_logged: set[tuple[str, str]] = set()


# ─── Catalogs ────────────────────────────────────────────────────────────────

def _load_dir(base: Path, lang: str) -> dict:
    """Merge all namespace files of one language into a flat {"ns.key": value} dict."""
    out: dict = {}
    d = base / lang
    if not d.is_dir():
        return out
    for f in sorted(d.glob("*.json")):
        data = json.loads(f.read_text(encoding="utf-8"))
        for k, v in data.items():
            out[f"{f.stem}.{k}"] = v
    return out


@lru_cache(maxsize=None)
def catalog(lang: str, web: bool = False) -> dict:
    """Flat catalog of one language, server side or web interface."""
    return _load_dir(WEB_LOCALES if web else SERVER_LOCALES, lang)


@lru_cache(maxsize=1)
def languages() -> dict[str, str]:
    """Available languages as {code: native name}, English first. A language counts once it has a web catalog."""
    langs = {}
    for d in sorted(WEB_LOCALES.iterdir()) if WEB_LOCALES.is_dir() else ():
        if d.is_dir():
            meta = catalog(d.name, web=True).get("meta.language_name")
            langs[d.name] = meta if isinstance(meta, str) else d.name
    return dict(sorted(langs.items(), key=lambda kv: (kv[0] != DEFAULT_LANG, kv[0])))


def supported(lang: str | None) -> str | None:
    """Normalize "de-AT" / "DE" to a supported language code, or None."""
    if not lang:
        return None
    base = lang.strip().lower().replace("_", "-").split("-")[0]
    return base if base in languages() else None


def negotiate(accept_language: str | None) -> str:
    """Pick the best supported language from an Accept-Language header."""
    best: list[tuple[float, int, str]] = []
    for i, part in enumerate((accept_language or "").split(",")):
        code, _, params = part.strip().partition(";")
        q = 1.0
        if params.strip().startswith("q="):
            try:
                q = float(params.strip()[2:])
            except ValueError:
                q = 0.0
        lang = supported(code)
        if lang and q > 0:
            best.append((-q, i, lang))
    return min(best)[2] if best else DEFAULT_LANG


def resolve(query_lang: str | None, cookie_lang: str | None, accept_language: str | None,
            default: str = "auto") -> str:
    """Language of a request: an explicit ?lang= wins, then the interface setting (cookie), then the configured
    default; only with default "auto" does the browser's Accept-Language decide."""
    chosen = supported(query_lang) or supported(cookie_lang)
    if chosen:
        return chosen
    if default != "auto":
        return supported(default) or DEFAULT_LANG
    return negotiate(accept_language)


# ─── Current language (per request) ──────────────────────────────────────────

def current_lang() -> str:
    return _lang.get()


@contextlib.contextmanager
def use_lang(lang: str):
    """Render everything inside the block in `lang` (background jobs, tests)."""
    token = _lang.set(supported(lang) or DEFAULT_LANG)
    try:
        yield
    finally:
        _lang.reset(token)


# ─── Formatting ──────────────────────────────────────────────────────────────

# Decimal and thousands separators per language; languages not listed use the English ones.
SEPARATORS = {"de": (",", ".")}


def _swap_separators(s: str, lang: str) -> str:
    dec, group = SEPARATORS.get(lang, (".", ","))
    if (dec, group) == (".", ","):
        return s
    return s.translate(str.maketrans({".": dec, ",": group}))


class _Formatter(string.Formatter):
    def __init__(self, lang: str):
        super().__init__()
        self.lang = lang

    def format_field(self, value, format_spec):
        if isinstance(value, dict) and ("$t" in value or "$join" in value):
            value = render(value, self.lang)
        out = super().format_field(value, format_spec)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            out = _swap_separators(out, self.lang)
        return out


def num(value, digits: int = 1, lang: str | None = None, dash: str = "–") -> str:
    """A number for display: fixed decimals, local decimal separator, `dash` for missing values."""
    if value is None:
        return dash
    try:
        return _swap_separators(f"{float(value):.{digits}f}", lang or current_lang())
    except (TypeError, ValueError):
        return str(value)


def date(ts: float, lang: str | None = None, with_time: bool = True) -> str:
    """A Unix timestamp as local date (and time) in the reader's convention."""
    fmt = t("common.datetime_format" if with_time else "common.date_format", lang)
    return time.strftime(fmt, time.localtime(ts))


# ─── Messages ────────────────────────────────────────────────────────────────

def msg(key: str, **params) -> dict:
    """A translatable message. Parameters may be numbers, strings or other messages."""
    return {"$t": key, "$p": params} if params else {"$t": key}


def msg_join(parts, sep: str = " ") -> dict:
    """Several messages (or strings) rendered one after another."""
    return {"$join": [p for p in parts if p], "$sep": sep}


def is_msg(value) -> bool:
    return isinstance(value, dict) and ("$t" in value or "$join" in value)


def _plural(lang: str, n) -> str:
    # English and German only need "one"/"other"; extend here for languages with more forms.
    try:
        return "one" if abs(float(n)) == 1 else "other"
    except (TypeError, ValueError):
        return "other"


def t(key: str, lang: str | None = None, **params) -> str:
    """Translate `key` into `lang` (default: the current request's language)."""
    lang = lang or current_lang()
    template = catalog(lang).get(key)
    if template is None:
        template = catalog(DEFAULT_LANG).get(key)
        if (lang, key) not in _missing_logged:
            _missing_logged.add((lang, key))
            log.warning("missing translation %s for %s", key, lang)
        if template is None:
            return key
    if isinstance(template, dict):
        template = template.get(_plural(lang, params.get("n", 0))) or template.get("other", key)
    try:
        return _Formatter(lang).format(template, **params)
    except (KeyError, IndexError, ValueError) as e:
        log.warning("bad translation %s (%s): %s", key, lang, e)
        return template


def render(value, lang: str | None = None):
    """Text of a message in `lang`; strings and None pass through unchanged (e.g. texts stored before 1.0)."""
    if isinstance(value, dict):
        if "$join" in value:
            return value.get("$sep", " ").join(str(render(p, lang)) for p in value["$join"] if p)
        if "$t" in value:
            return t(value["$t"], lang, **value.get("$p", {}))
    return value


def dumps(value):
    """A value for a text column of the database: a message as JSON text, anything else unchanged."""
    return json.dumps(value, ensure_ascii=False) if is_msg(value) else value


def loads_text(value):
    """Inverse of dumps: the JSON text of a message back to the message; other values (texts stored by versions
    before 1.0 included) unchanged."""
    if isinstance(value, str) and value.startswith(('{"$t"', '{"$join"')):
        try:
            m = json.loads(value)
        except ValueError:
            return value
        return m if is_msg(m) else value
    return value


class MessageError(Exception):
    """An error a user should see. Carries an i18n message (`message`); str() renders it in the current language,
    so code that turns it into an HTTP error or a log line with str(e) answers in the reader's language."""

    def __init__(self, message):
        super().__init__(message)
        self.message = message

    def __str__(self) -> str:
        return str(render(self.message))


def error_message(e: BaseException):
    """What to show or store for an exception: the message it carries (MessageError, or a message as its only
    argument), otherwise its text."""
    for v in (getattr(e, "message", None), e.args[0] if len(e.args) == 1 else None):
        if is_msg(v):
            return v
    return str(e)


def localize(obj, lang: str | None = None):
    """Replace every message inside a JSON-like structure by its text."""
    if isinstance(obj, dict):
        if is_msg(obj):
            return render(obj, lang)
        return {k: localize(v, lang) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [localize(v, lang) for v in obj]
    return obj


def web_catalog_js(lang: str) -> str:
    """Script with the web interface's catalog for `lang` plus the English fallback (served as /i18n/catalog.js)."""
    data = {"lang": lang, "languages": languages(), "catalog": catalog(lang, web=True),
            "fallback": catalog(DEFAULT_LANG, web=True) if lang != DEFAULT_LANG else {}}
    return "window.VC_I18N=" + json.dumps(data, ensure_ascii=False, separators=(",", ":")) + ";\n"
