# Contributing to vinyl-check

Thank you for your interest in vinyl-check. Bug reports, measurements from your setup, translations, documentation
and code are all welcome.

## Ways to contribute

- **Report a bug.** Open an issue with the version (shown in *Settings → Device and service*), your platform, your
  turntable chain (turntable, cartridge, phono stage, interface, sample format) and the steps to reproduce. For
  analysis problems, the recording's technical data and a screenshot of the report help a lot.
- **Share measurements.** Records whose condition you know by ear, and alignment runs on turntables we have not seen
  yet, help calibrate the grade limits and the targets.
- **Translate.** See [docs/translating.md](docs/translating.md).
- **Write code or documentation.** For larger changes, please open an issue first so we can agree on the approach.

## Development setup

You need Python 3.11 or newer; 3.13 is the reference version used by CI for linting, packaging and the macOS/Windows tests.

```bash
git clone https://github.com/michaelpietzsch/vinyl-check.git
cd vinyl-check
python3 -m venv .venv
. .venv/bin/activate                 # Windows: .venv\Scripts\activate
pip install -e ".[dev]"              # add ",ml" for the second-opinion network: pip install -e ".[dev,ml]"
```

Keep the virtual environment out of synced folders (cloud drives): it contains thousands of files.

### Run a local instance without a turntable

```bash
python -c "import soundfile as sf; from vinylcheck import synth; a, _ = synth.make_side(120); sf.write('demo-side.wav', a, 48000, subtype='PCM_16')"
VINYLCHECK_FAKE_SOURCE=demo-side.wav VINYLCHECK_FAKE_SPEED=10 \
VINYLCHECK_DATA_DIR=data VINYLCHECK_STATE_DIR=state python -m vinylcheck
```

`vinylcheck/synth.py` generates test records with lead-in, tracks (chords and drums), pauses, run-out, groove noise
and logged clicks. It can also add groove skips (`skip_back`, `skip_forward`, `locked_groove`) and simulate the
AAPT 1 test record with adjustable errors: `synth.make_test_side(1 | 2, synth.Rig(...))`. One lesson from building
it: every hard-cut note is itself a click, so the generator fades everything smoothly.

If you start an instance from your own script, guard it with `if __name__ == "__main__":`. The analysis runs in a
spawned child process, which re-imports the script.

## Tests

```bash
pytest                         # full suite, takes a few minutes
pytest tests/test_i18n.py      # translation catalogs only
pytest -k compare              # a subset by name
node --check vinylcheck/static/app.js    # syntax of the web interface
```

- New endpoints and new calculations need tests. Signal-processing code is tested against synthetic signals with
  known ground truth (`vinylcheck/synth.py`). Assert on measured values with a tolerance, not on exact floats.
- Tests must pass on the oldest supported Python and on current library versions.
- Requests in tests get English by default. Prefer asserting on message keys, or on the English text, over
  asserting on German text.

## Building the desktop apps

The desktop apps are the same engine plus a native window (`vinylcheck/desktop.py`, pywebview), packed with
PyInstaller. Everything for the build is in `packaging/`; CI builds them in
[`.github/workflows/desktop.yml`](.github/workflows/desktop.yml) on version tags and on demand.

```bash
pip install ".[desktop]" pyinstaller
vinyl-check-desktop                          # run from source: engine on 127.0.0.1 plus window
vinyl-check-desktop --selftest               # headless check: API, WebSocket, analysis, PDF, PortAudio, toolkit
pyinstaller packaging/vinyl-check.spec --noconfirm
```

- **macOS** (on a Mac): `dist/vinyl-check.app`. Check it with
  `dist/vinyl-check.app/Contents/MacOS/vinyl-check --selftest`, then pack it with
  `packaging/macos/make-dmg.sh dist/vinyl-check.app dist/vinyl-check-<version>-macos-arm64.dmg`.
- **Windows** (on Windows): `dist\vinyl-check\vinyl-check.exe`. It is a windowed program without a console, so the
  self-test writes its result to a file: `vinyl-check.exe --selftest --output selftest.txt`. The installer needs
  [Inno Setup 6](https://jrsoftware.org/isinfo.php): `iscc /DAppVersion=<version> packaging\windows\vinyl-check.iss`.

The build draws the app icons from the brand signet (`packaging/make_icons.py`; no generated icons are kept in the
repository) and writes `THIRD-PARTY-NOTICES.txt` with the licenses of every bundled package
(`packaging/third_party_notices.py`). If you add a dependency, check that its license allows bundling and that it
shows up in that file. PyTorch is deliberately left out of the apps. Keep build and work folders outside synced
folders (cloud drives): `--distpath` and `--workpath` move them.

Code signing is not set up yet; [docs/code-signing.md](docs/code-signing.md) describes the status and how to switch
it on.

## Code style

- **English** for everything in the repository: identifiers, comments, docstrings, log messages, tests, commit
  messages and documentation.
- **Comments explain why**, not what. Every module starts with a docstring that says what it does and how it fits
  in. Public functions get a concise docstring (what, inputs with units, return value). Non-obvious DSP, maths and
  thresholds get a short comment with the reason or the measurement behind them.
- **Line length 120.** Linting with [ruff](https://docs.astral.sh/ruff/) (configured in `pyproject.toml`):

  ```bash
  ruff check .
  ```

  Match the style of the surrounding code, and do not reformat code you do not otherwise change.
- **No new platform assumptions.** vinyl-check is moving towards Linux, macOS and Windows. Do not add new
  hard-coded paths, Linux-only calls or shell dependencies outside the capture layer and the deployment scripts.
- **The web interface uses no external libraries and no build step.** Plain JavaScript, CSS custom properties from
  the [design system](docs/design-system.md) and the SVG icon sprite. It must work without internet access.
- Never commit recordings, databases or configuration files with credentials (see `.gitignore`).

## Translations (i18n)

Every user-visible string goes through the translation catalogs. Nothing user-facing is hard-coded.

- **Server code** returns messages, not text: `i18n.msg("analysis.level_too_high", diff=x)`. Messages are JSON,
  can be stored in the database and are rendered in the reader's language when a response is sent. Code that needs
  a final string right away (PDF reports) uses `i18n.t(key, **params)`, `i18n.num(value, digits)` and
  `i18n.date(ts)`. HTTP errors: `raise HTTPException(404, i18n.msg("api.record_not_found"))`; other errors a user
  should see: `raise i18n.MessageError(i18n.msg(...))` or a subclass.
- **Stored values are English codes** (kinds, judgements, roles, verdicts) and are never translated; the interface
  shows them through catalog labels. Renaming one needs a database migration (see `vinylcheck/legacy.py`).
- **Web code** uses `T("records.title")`, `T("records.count", { n })`, `I18N.num(v, 1)` and `I18N.date(ts)`. Static
  HTML uses `data-i18n`, `data-i18n-title`, `data-i18n-placeholder`, `data-i18n-aria-label` or `data-i18n-html`.
  Never format numbers with a fixed locale.
- **Add every new key to English and German.** English is the reference language. `tests/test_i18n.py` fails if a
  key is missing in any language, if placeholders differ, or if code uses a key that is not in the English catalog.
- Log messages are plain English and are not translated. User content (titles, notes) and API JSON keys are never
  translated.

Details: [docs/translating.md](docs/translating.md).

## Commit messages

- In English, in the imperative mood, with a concise subject line ("Add 45 rpm tone to the alignment bands").
- Explain *why* in the body when the reason is not obvious, for example the measurement that motivated a threshold.
- One logical change per commit.

## Pull requests

1. Fork the repository and create a branch from `main`.
2. Make your change with tests and, where it affects users, documentation. Add an entry under *Unreleased* in
   [CHANGELOG.md](CHANGELOG.md).
3. Run `pytest`, `ruff check .` and `node --check vinylcheck/static/app.js` locally. CI runs the same checks.
4. Open the pull request with a description of what changes and why, and how you tested it. For changes to the
   analysis, include before/after figures on real or synthetic recordings.
5. A maintainer reviews the change. Please keep the discussion focused, and push follow-up commits to the same
   branch.

vinyl-check is free for non-commercial use under the [PolyForm Noncommercial License 1.0.0](LICENSE) and is
licensed separately for commercial use. So that both remain possible, by contributing you agree that your
contribution is published under the same license, and you grant the maintainer a perpetual, worldwide, royalty-free
right to use, change and license your contribution under any terms, including commercial licenses. You confirm
that you wrote the contribution yourself or have the right to submit it under these terms.
