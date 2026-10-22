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

You need Python 3.11 or newer.

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

- **Server code** returns messages, not text: `i18n.msg("analysis.too_quiet", db=x)`. Messages are JSON, can be
  stored in the database and are rendered in the reader's language when a response is sent. Code that needs a
  final string right away (PDF reports) uses `i18n.t(key, **params)`, `i18n.num(value, digits)` and
  `i18n.date(ts)`. HTTP errors: `raise HTTPException(404, i18n.msg("api.record_not_found"))`.
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

By contributing, you agree that your contributions are licensed under the [MIT License](LICENSE).
