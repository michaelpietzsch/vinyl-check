"""The interface in a real browser: the pages render without script errors, the main flows work from click to
database, and text that came from a user or from Discogs is shown as text, never run as markup.

The station under test is the real app on a loopback port, with a synthetic side as its turntable (played at 200
times real time), so recording, analysis and the live updates of the page are the real ones.

Needs Playwright and its Chromium:

    pip install -e ".[dev,browser]" && python -m playwright install chromium

Without them the module is skipped, unless VINYLCHECK_BROWSER_TESTS=1 demands the tests (the CI job that exists for
them), where a missing browser is an error.
"""

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import replace

import pytest
import soundfile as sf

from vinylcheck import network, synth
from vinylcheck.config import Config
from vinylcheck.web import create_app

REQUIRED = os.environ.get("VINYLCHECK_BROWSER_TESTS") == "1"
try:
    from playwright.sync_api import expect, sync_playwright
except ImportError:
    if REQUIRED:
        raise
    pytest.skip('Playwright is not installed (pip install -e ".[browser]")', allow_module_level=True)

SR = 48000
# Text as a user or a Discogs entry may bring it. Every piece breaks out of both places it can end up in: element
# content (a tag) and an attribute value (the quote first). If any of it were taken as markup, window.__xss would
# be set, an image "x" requested, or an element appear that the page never writes (<u>).
HOSTILE_RECORD = {"artist": """<img src=x onerror="window.__xss='artist'">""",
                  "title": """"><script>window.__xss='title'</script>""",
                  "notes": """<svg onload="window.__xss='record notes'">"""}
HOSTILE_RECORDING = {"side": '"><u>A', "kind": "before",                       # a side has eight characters
                     "copy_label": """"><img src=x onerror=window.__xss=1>""",
                     "cleaning_method": """"><img src=x onerror=window.__xss=2>""",
                     "notes": "</td><script>window.__xss=3</script>"}


class Station:
    """The running app and a few calls to its API, for preparing a test and for checking what the page stored."""

    def __init__(self, base: str):
        self.base = base

    def api(self, method: str, path: str, body=None):
        req = urllib.request.Request(self.base + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read() or b"null")

    def status(self, path: str) -> int:
        try:
            with urllib.request.urlopen(self.base + path, timeout=30) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    def analyzed(self, rec_id: int, timeout: float = 120) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            r = self.api("GET", f"/api/recordings/{rec_id}")
            if r["status"] in ("analyzed", "error"):
                assert r["status"] == "analyzed", r.get("error")
                return r
            time.sleep(0.2)
        raise AssertionError(f"recording {rec_id} was not analysed in time")

    def record_side(self, record_id: int, **meta) -> dict:
        """Record the test side for a record (it ends by itself) and wait for its analysis."""
        rec_id = self.api("POST", "/api/recordings/start", {"record_id": record_id, **meta})["recording_id"]
        return self.analyzed(rec_id)

    def recordings(self, record_id: int) -> list[dict]:
        return self.api("GET", f"/api/records/{record_id}")["recordings"]


@pytest.fixture(scope="module")
def station(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("browser")
    audio, _ = synth.make_side(duration_s=40, seed=5, clicks_per_min=30)
    wav = tmp / "side.wav"
    sf.write(wav, audio, SR, subtype="PCM_16")
    cfg = replace(Config(), data_dir=tmp / "data", state_dir=tmp / "state", fake_source=str(wav), fake_speed=200.0,
                  min_free_gb=0.0)
    server = network.ServerThread(create_app(cfg), network.bind_socket("127.0.0.1", 0))
    server.start()
    st = Station(f"http://127.0.0.1:{server.port}")
    try:
        st.api("POST", "/api/onboarding", {"state": "skipped"})        # the welcome has a test of its own
        st.album = st.api("POST", "/api/records", {"artist": "Test Artist", "title": "First Album"})["id"]
        st.side_a = st.record_side(st.album, side="A", kind="before")["id"]
        st.side_b = st.record_side(st.album, side="B", kind="before", copy_label="Copy 2")["id"]
        st.hostile = st.api("POST", "/api/records", HOSTILE_RECORD)["id"]
        st.hostile_side = st.record_side(st.hostile, **HOSTILE_RECORDING)["id"]
        yield st
    finally:
        server.stop()


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as e:  # noqa: BLE001 — no browser installed for this Playwright
            if REQUIRED:
                raise
            pytest.skip(f"no browser for Playwright (python -m playwright install chromium): {e}")
        yield b
        b.close()


@pytest.fixture()
def page(browser, station):
    """A fresh browser profile on the station's start page. After the test, nothing may have gone wrong in the page
    that the user would not have seen: a script error, an error on the console (which includes every failed
    request), or one of the browser's own dialogs, which the interface does not use."""
    context = browser.new_context(viewport={"width": 1400, "height": 900}, locale="en-US")
    context.set_default_timeout(15000)
    page = context.new_page()
    problems: list[str] = []
    page.on("pageerror", lambda e: problems.append(f"script error: {e}"))
    page.on("console", lambda m: problems.append(f"console: {m.text}") if m.type == "error" else None)

    def on_dialog(d):
        problems.append(f"browser dialog: {d.message}")
        d.dismiss()
    page.on("dialog", on_dialog)
    page.goto(station.base + "/")
    page.wait_for_selector("#pills .pill")                         # the first status has arrived and is rendered
    yield page
    context.close()
    assert not problems, problems


def open_record(page, record_id: int) -> None:
    page.click('#tabs button[data-mode="record"]')
    page.click(f'#records li[data-id="{record_id}"]')
    page.wait_for_selector("#btn-rec")


def cell(page, rec_id: int, field: str):
    return page.locator(f'td[data-edit="{field}"][data-rec="{rec_id}"]')


def by_id(rows: list[dict], rec_id: int) -> dict:
    return next(x for x in rows if x["id"] == rec_id)


def test_every_page_renders(page, station):
    for mode in ("station", "record", "alignment", "jobs", "system"):
        page.click(f'#tabs button[data-mode="{mode}"]')
        expect(page.locator(f'#tabs button[data-mode="{mode}"]')).to_have_class("active" if mode != "system"
                                                                                 else "tab-settings active")
        expect(page.locator(f"#main-{mode}")).to_be_visible()
        page.wait_for_function("id => document.getElementById(id).innerText.trim().length > 40", arg=f"main-{mode}")
    page.wait_for_load_state("networkidle")                        # the pages' own requests are answered


def test_recording_a_side_from_the_page(page, station):
    """Start a recording with the form of the record page: the page follows it by itself until the report is there."""
    open_record(page, station.album)
    before = {x["id"] for x in station.recordings(station.album)}
    page.fill("#r-side", "C")
    page.select_option("#r-kind", "after_wet")
    page.fill("#r-clean", "ultrasonic")
    page.click("#btn-rec")
    page.wait_for_function("n => document.querySelectorAll('td[data-edit=side]').length > n", arg=len(before))
    new = next(x for x in station.recordings(station.album) if x["id"] not in before)
    assert (new["side"], new["kind"], new["cleaning_method"]) == ("C", "after_wet", "ultrasonic")
    # no reload: the status over the WebSocket brings the row from "recording" to its grade
    page.wait_for_selector(f'[data-view="{new["id"]}"]', timeout=120000)
    assert station.analyzed(new["id"])["analysis"]["grading"]["grade"]
    page.click(f'[data-view="{new["id"]}"]')
    expect(page.locator("#view .big-grade")).to_be_visible()
    expect(page.locator("#view")).to_contain_text("after wet cleaning")


def test_editing_a_recording_in_the_page(page, station):
    open_record(page, station.album)
    rec = station.side_b
    cell(page, rec, "kind").dblclick()
    box = page.locator("#rec-edit")
    expect(box).to_have_attribute("open", "")
    assert page.evaluate("document.activeElement.name") == "kind"       # the clicked field is the focused one
    box.locator('select[name="kind"]').select_option("after_dry")
    box.locator('button[type="submit"]').click()
    expect(box).not_to_have_attribute("open", "")
    expect(cell(page, rec, "kind")).to_contain_text("after dry cleaning")
    assert by_id(station.recordings(station.album), rec)["kind"] == "after_dry"
    # Esc keeps everything as it was
    cell(page, rec, "copy_label").dblclick()
    expect(box).to_have_attribute("open", "")
    box.locator('input[name="copy_label"]').fill("Copy 9")
    page.keyboard.press("Escape")
    expect(box).not_to_have_attribute("open", "")
    assert by_id(station.recordings(station.album), rec)["copy_label"] == "Copy 2"
    # an empty side is refused in the box, with a word why
    cell(page, rec, "side").dblclick()
    box.locator('input[name="side"]').fill(" ")
    box.locator('button[type="submit"]').click()
    expect(page.locator("#toast")).to_contain_text("side")
    expect(box).to_have_attribute("open", "")
    page.keyboard.press("Escape")
    assert by_id(station.recordings(station.album), rec)["side"] == "B"


def test_deleting_asks_in_the_page(page, station):
    rec = station.record_side(station.album, side="D", kind="other")["id"]
    open_record(page, station.album)
    ask = page.locator("#ask")
    page.click(f'[data-del="{rec}"]')
    expect(ask).to_have_attribute("open", "")
    assert page.evaluate("document.activeElement.textContent") == "Cancel"     # Enter alone never deletes
    page.keyboard.press("Enter")
    expect(ask).not_to_have_attribute("open", "")
    assert station.status(f"/api/recordings/{rec}") == 200
    page.click(f'[data-del="{rec}"]')
    ask.locator('button[value="1"]').click()
    expect(cell(page, rec, "side")).to_have_count(0)
    assert station.status(f"/api/recordings/{rec}") == 404


def test_a_verdict_takes_the_click_off_the_list(page, station):
    """The event list is the work list: a judged click leaves it, and the verdict is stored."""
    open_record(page, station.album)
    page.click(f'[data-view="{station.side_a}"]')
    first = page.locator('#ev-wrap button[data-label="music"]').first
    t = first.get_attribute("data-t")
    rows = page.locator("#ev-wrap tr").count()
    first.click()
    expect(page.locator(f'#ev-wrap button[data-label="music"][data-t="{t}"]')).to_have_count(0)
    assert page.locator("#ev-wrap tr").count() <= rows
    events = station.api("GET", f"/api/recordings/{station.side_a}/events?limit=20000")
    assert [e["label"] for e in events if str(e["t"]) == t] == ["music"]
    expect(page.locator("#view .big-grade")).to_be_visible()       # the report is still whole after the redraw


def test_text_from_users_and_discogs_stays_text(page, station):
    """Names, notes and labels with markup in them are shown as they were typed, wherever they appear."""
    def nothing_ran(where: str) -> None:
        page.wait_for_load_state("networkidle")
        found = page.evaluate("""() => [window.__xss === undefined ? null : 'window.__xss = ' + window.__xss,
            ...[...document.querySelectorAll('img[src="x"], svg[onload], u')].map((e) => e.outerHTML.slice(0, 120)),
            ...[...document.scripts].filter((x) => x.textContent.includes('__xss')).map(() => 'an injected script'),
          ].filter(Boolean)""")
        assert not found, (where, found)

    expect(page.locator(f'#records li[data-id="{station.hostile}"]')).to_contain_text("<img src=x onerror=")
    nothing_ran("the start page")
    open_record(page, station.hostile)
    expect(page.locator("#detail h2")).to_contain_text("<script>window.__xss='title'</script>")
    expect(cell(page, station.hostile_side, "side")).to_have_text('"><u>A')
    expect(cell(page, station.hostile_side, "copy_label")).to_have_text('"><img src=x onerror=window.__xss=1>')
    expect(cell(page, station.hostile_side, "kind")).to_contain_text('"><img src=x onerror=window.__xss=2>')
    expect(cell(page, station.hostile_side, "kind")).to_contain_text("</td><script>window.__xss=3</script>")
    nothing_ran("the record page")
    cell(page, station.hostile_side, "copy_label").dblclick()       # the edit box puts the texts into attributes
    expect(page.locator('#rec-edit input[name="copy_label"]')).to_have_value('"><img src=x onerror=window.__xss=1>')
    expect(page.locator('#rec-edit input[name="side"]')).to_have_value('"><u>A')
    nothing_ran("the edit box")
    page.keyboard.press("Escape")
    page.click(f'[data-view="{station.hostile_side}"]')
    expect(page.locator("#view h2")).to_contain_text('"><u>A')
    expect(page.locator("#view [data-rec-clean]")).to_have_value('"><img src=x onerror=window.__xss=2>')
    nothing_ran("the report")
    page.click("#close-view")
    for mode in ("station", "jobs", "system"):                      # latest recordings, finished jobs, the rest
        page.click(f'#tabs button[data-mode="{mode}"]')
        nothing_ran(f"the {mode} page")


def test_the_welcome_is_offered_and_can_wait(page, station):
    station.api("POST", "/api/onboarding", {"state": "open"})
    try:
        page.reload()
        welcome = page.locator("#welcome")
        expect(welcome).to_have_attribute("open", "")
        welcome.locator('[data-welcome="later"]').click()
        expect(welcome).not_to_have_attribute("open", "")
        page.reload()                                              # "later" holds for this browser session
        page.wait_for_selector("#pills .pill")
        expect(welcome).not_to_have_attribute("open", "")
    finally:
        station.api("POST", "/api/onboarding", {"state": "skipped"})
