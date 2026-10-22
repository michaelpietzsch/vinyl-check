<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="vinylcheck/static/brand/vinyl-check-logo-invers.svg">
    <source media="(prefers-color-scheme: light)" srcset="vinylcheck/static/brand/vinyl-check-logo.svg">
    <img alt="vinyl-check" src="vinylcheck/static/brand/vinyl-check-logo.svg" width="360">
  </picture>
</p>

<p align="center"><b>Measure the condition of your records, and the alignment of the turntable that plays them.</b></p>

<p align="center">
  <a href="https://github.com/michaelpietzsch/vinyl-check/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/michaelpietzsch/vinyl-check/actions/workflows/ci.yml/badge.svg"></a>
  <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-blue.svg"></a>
  <img alt="Python 3.11+" src="https://img.shields.io/badge/python-3.11%2B-blue.svg">
</p>

---

vinyl-check records a side of a record through your turntable, finds clicks, pops and crackle, measures groove
noise and suggests a condition grade on the Goldmine scale. It shows what a record cleaning really achieved, checks
the alignment of your turntable with a test record, compares test pressings against the digital master and writes
PDF reports. It is a small web application written in Python. It runs on your own hardware, either on a Raspberry Pi
next to the turntable or on your computer, and works without a cloud service.

> **Your turntable is part of the measurement.** vinyl-check works with an inexpensive USB turntable as well as
> with a high-end turntable, phono stage and studio audio interface. The price of the chain matters less than its
> setup: a properly aligned budget deck gives repeatable, meaningful results, and a badly aligned expensive one does
> not. The [setup guide](docs/setup-guide.md) explains each step, and vinyl-check's alignment mode checks the result
> with a test record.

## Features

**Record condition and grading**
- Click, pop and crackle detection with a prediction-error (AR model) detector and a spectral "stripe" check that
  separates real defects from drum hits.
- Condition grade (NM, VG+, VG, G+, G, F) computed from what happens while the music plays. F is only given when
  the stylus actually skips. The grade is shown together with the criteria that produced it.
- Groove skip detection, recurring-scratch chains, groove noise, clipping and technical data (true peak, loudness
  per ITU-R BS.1770, stereo correlation, rumble, hum).
- Interactive overview, level and spectrogram charts: click any point to listen, drag to zoom, and play back only
  the detected clicks to check them by ear.

**Cleaning before and after**
- Record a side before and after cleaning. vinyl-check aligns both recordings sample-accurately and labels every
  click as *removed* (dirt), *persistent* (damage) or *new*.
- Two uncleaned recordings show how repeatable the measurement is, so only real improvements count.
- Compare several copies of the same record and follow each side's history over time.

**Turntable alignment with a test record**
- Guided turntable profiles: noise calibration, both sides of the test record, review, accept.
- Measures anti-skating and tracking reserve, channel balance, crosstalk and azimuth, arm/cartridge resonance,
  speed and wow & flutter, intermodulation (VTA), frequency response and rumble, each with a target value and advice.
- Live meter for adjustments: distortion per channel while you turn the anti-skating dial.
- Editable target presets (standard, DIN 45500, strict lab values, manufacturer data).

**Master comparison and test pressing approval**
- Optional engineer mode: upload the digital master of each side and measure a pressing against it, including
  speed of the cut, eccentricity, polarity, channel swap, frequency response, level per track, pre-echo and which
  clicks are really on the vinyl.
- Test pressing approval across several copies: stamper defects common to all copies, checkpoints and a recommendation.

**PDF reports**
- A report per recording and an approval report per record, with charts, spectrograms and excerpts, ready to send
  to a pressing plant or a client.

**Discogs collection**
- Search your Discogs collection, take over the release, tracklist and cover, and write the measured grade back as
  media condition once you confirm it.

**Learning from your judgements**
- Mark detections as *click*, *false alarm* or *unsure*. Together with master comparisons, vinyl-check learns to
  estimate how many detections are real. Each estimate is validated by cross-validation per recording.

**Multilingual interface**
- English and German, chosen from the browser language or in the settings. Most languages can be added without
  code changes ([translating guide](docs/translating.md)).

## Screenshots

| Record overview | Recording a side | Turntable alignment |
|---|---|---|
| ![Record overview with grade, copies and recordings](docs/images/overview.png) | ![Live levels and analyzer while recording](docs/images/recording.png) | ![Alignment findings from a test record](docs/images/alignment.png) |

## Quick start

### Requirements

- Python 3.11 or newer.
- For recording: Linux with ALSA (`arecord` from the `alsa-utils` package) and a USB turntable or USB audio
  interface. On macOS and Windows the interface, the analysis and the demo mode run today. Native recording on
  these systems is on the [roadmap](#roadmap).
- About 1–2 GB of free RAM while an LP side is analysed, and disk space for recordings (about 12 MB per minute at
  16 bit / 48 kHz, 17 MB at 24 bit / 48 kHz).

### Install from source

```bash
git clone https://github.com/michaelpietzsch/vinyl-check.git
cd vinyl-check
python3 -m venv .venv
. .venv/bin/activate              # Windows: .venv\Scripts\activate
pip install .                     # or: pip install ".[ml]" to add the second-opinion network (PyTorch)
```

### Run

vinyl-check reads its configuration from the TOML file named in `VINYLCHECK_CONFIG`. For a local instance, a few
lines are enough:

```toml
# config.toml
data_dir = "data"            # recordings (large)
state_dir = "state"          # database, caches (small)
host = "127.0.0.1"           # "0.0.0.0" to open the interface to other devices in your network
port = 8080
device = ""                  # empty = first USB capture device, or e.g. "hw:CARD=CODEC,DEV=0"
sample_format = "S24_3LE"    # 24 bit if your interface supports it (arecord --dump-hw-params)
```

```bash
VINYLCHECK_CONFIG=config.toml vinyl-check       # same as: python -m vinylcheck
```

Then open <http://localhost:8080>. The interface walks you through the first steps: add your turntable, calibrate
the chain, align it with a test record, then check your first record ([user guide](docs/user-guide.md)).

### Try it without a turntable

The demo mode plays a WAV file instead of a turntable. vinyl-check can synthesize a test side with music, pauses,
groove noise and logged clicks:

```bash
python -c "import soundfile as sf; from vinylcheck import synth; a, _ = synth.make_side(120); sf.write('demo-side.wav', a, 48000, subtype='PCM_16')"
VINYLCHECK_FAKE_SOURCE=demo-side.wav VINYLCHECK_FAKE_SPEED=10 \
VINYLCHECK_DATA_DIR=data VINYLCHECK_STATE_DIR=state vinyl-check
```

`VINYLCHECK_FAKE_SPEED=10` plays the file ten times faster than real time. The examples use POSIX shell syntax.
In PowerShell, set each variable first, for example `$env:VINYLCHECK_FAKE_SOURCE = "demo-side.wav"`.

### Raspberry Pi as a headless station

A Raspberry Pi 5 (8 GB recommended) next to the turntable makes a quiet, always-on measuring station. Phones,
tablets and computers in the same network use it through the browser. `deploy.sh` sets it up from your computer
over SSH (key login and password-less `sudo` on the Pi):

```bash
VC_TARGET=user@raspberrypi.local ./deploy.sh
```

The script installs the Debian packages, creates the service user `vinylcheck`, copies the code to
`/opt/vinyl-check`, installs the sandboxed systemd unit (`systemd/vinyl-check.service`), runs the test suite on the
Pi and restarts the service once no recording or analysis is running. The configuration in
`/etc/vinyl-check/config.toml` is created from [`config.example.toml`](config.example.toml) on the first run and
never overwritten. See the header of `deploy.sh` for all options.

```bash
systemctl status vinyl-check
journalctl -u vinyl-check -f
```

### Desktop apps

Native apps for Windows, macOS and Linux, with recording through the system's audio layer, are on the
[roadmap](#roadmap).

## Recommended setups

| | Chain | What to expect |
|---|---|---|
| **Entry level** | USB turntable with built-in phono stage | Good for cleaning comparisons and rough grades. Fixed gain and a higher noise floor; the turntable profile records the chain's own reference level and click floor so results stay comparable. |
| **Recommended** | Turntable, phono preamp, audio interface at 24 bit / 48 kHz | Adjustable levels and lower noise. This is the reference setup for grading. |
| **High end** | Well-aligned turntable, low-noise phono stage, studio interface | Lowest noise floor, reliable alignment measurements, test pressing approval against the master. |

Every setup needs the same basics: correct tracking force, anti-skating, VTA, azimuth and overhang, a level
calibration and a clean stylus. The [setup guide](docs/setup-guide.md) covers each step and has a checklist for
every session.

## How it works

1. **Capture**: the side is recorded bit-exact from the audio device (no resampling) with live level, clipping and
   dropout monitoring. The recording stops by itself when the stylus reaches the run-out groove.
2. **Detection**: an autoregressive model predicts every sample. Clicks show up as peaks in the prediction error.
   A high-frequency stripe test, a second-opinion network and your own judgements remove false alarms.
3. **Segmentation**: lead-in, music, pauses and run-out groove are found from the level curve. The track borders
   come from the Discogs tracklist.
4. **Grading**: clicks and pops per minute of music, weighted 90 / 10, mapped to the Goldmine scale. A skip
   means F. The record grade is the average of its sides.
5. **Comparison**: recordings are aligned by cross-correlation, including speed differences, and events are
   matched within ±4 ms.
6. **Alignment**: the bands of the test record are recognised by their signal, then measured with
   Blackman-Harris spectra, THD and THD+N, sweeps and a resonance model.

Details, thresholds and validation results: [docs/measurement.md](docs/measurement.md).

## Configuration

All settings have defaults. [`config.example.toml`](config.example.toml) lists the keys with comments. The most
important ones:

| Key | Default | Meaning |
|---|---|---|
| `data_dir`, `state_dir` | `/mnt/data/vinyl-check`, `/var/lib/vinyl-check` | recordings and database |
| `device` | `""` | ALSA capture device, empty = first USB device |
| `rate`, `channels`, `sample_format` | `48000`, `2`, `"S16_LE"` | capture format (`S16_LE`, `S24_3LE`, `S32_LE`) |
| `capture_channels`, `input_channels` | — | for interfaces that only record with all their channels open |
| `require_mount` | `""` | refuse to record unless this mount point is present (protects an SD card) |
| `retention_days` | `30` | delete original audio after this many days; measurements are kept |
| `discogs_token`, `discogs_username` | — | Discogs access via personal token |
| `discogs_consumer_key`, `discogs_consumer_secret` | — | Discogs access via OAuth app ("Connect with Discogs" in the settings) |

Further sections of `config.example.toml` hold the detector, analysis, grading and alignment parameters. Many values
can also be changed in the interface under *Settings*. Environment
variables override the file: `VINYLCHECK_DATA_DIR`, `VINYLCHECK_STATE_DIR`, `VINYLCHECK_PORT`,
`VINYLCHECK_FAKE_SOURCE`, `VINYLCHECK_FAKE_SPEED`, `VINYLCHECK_REQUIRE_MOUNT`, `VINYLCHECK_RETENTION_DAYS`,
`VINYLCHECK_DISCOGS_TOKEN`, `VINYLCHECK_DISCOGS_USERNAME`, `VINYLCHECK_DISCOGS_CONSUMER_KEY`,
`VINYLCHECK_DISCOGS_CONSUMER_SECRET`, `VINYLCHECK_DISCOGS_SYNC_MINUTES`, `VINYLCHECK_FONT_DIR` (IBM Plex fonts for
the PDF reports).

The REST API is documented at `/docs` on a running instance.

## Languages

The interface, API messages and PDF reports are available in **English** and **German**. vinyl-check follows the
browser language and falls back to English, and the language can be changed in the settings. Numbers and dates use
the conventions of the chosen language. To add a language, see [docs/translating.md](docs/translating.md).

## Documentation

| Document | Content |
|---|---|
| [Setup guide](docs/setup-guide.md) | turntable chains from USB to high end, alignment basics, level calibration, session checklist |
| [User guide](docs/user-guide.md) | the workflow in the interface |
| [Measurement](docs/measurement.md) | how detection, grading, comparison and alignment work |
| [Architecture](docs/architecture.md) | design of stations and a central knowledge hub (roadmap) |
| [Design system](docs/design-system.md) | interface principles, tokens, components ([preview](docs/design-preview.html)) |
| [Translating](docs/translating.md) | adding a language |
| [Changelog](CHANGELOG.md) | release history |

## Roadmap

- **Platform independence**: native apps for Windows, macOS and Linux. Recording moves behind a capture interface
  with ALSA and PortAudio backends (ASIO on Windows), data and configuration live in the user's profile, and LAN
  access from phones and tablets is optional.
- **Stations and a central hub**: several vinyl-check stations pool their judgements, and only features and
  labels leave a station, never audio. A hub trains better models and hands them back after they have been
  checked on each station's own data ([architecture](docs/architecture.md)).
- **More test records**: band layouts beyond the Analogue Productions AAPT 1, 45 rpm alignment tones.
- **Grade calibration** against a larger set of records with known condition.
- **WAV import** of recordings made elsewhere.

## Contributing

Contributions are welcome: bug reports, measurements from your setup, translations and code. Please read
[CONTRIBUTING.md](CONTRIBUTING.md) first.

## License

vinyl-check is released under the [MIT License](LICENSE).

The click-detector weights in `vinylcheck/weights/` come from GrooveClean and are licensed under MIT as well
([license](vinylcheck/weights/LICENSE-grooveclean.txt)).

## Acknowledgements

- [GrooveClean](https://github.com/Booyaka101/grooveclean) by Chris Bosch (MIT) for the click-detector network used
  as a second opinion, and for ideas such as the single sensitivity control and the clipped-peak check.
- The grading scale follows the Goldmine standard as used by Discogs.
- This application uses Discogs' API but is not affiliated with, sponsored or endorsed by Discogs. "Discogs" is a
  trademark of Zink Media, LLC.
- vinyl-check is not affiliated with Analogue Productions. The AAPT 1 test record is referenced for its documented
  band layout.
