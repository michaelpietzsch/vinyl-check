# Changelog

All notable changes to vinyl-check are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.0.0] – unreleased

First public release, under the MIT license.

### Added

- Multilingual interface in English and German. The web interface, API messages and errors, analysis findings and
  PDF reports come from translation catalogs (`vinylcheck/locales/`, `vinylcheck/static/locales/`). The language
  follows the browser, can be chosen in the settings and falls back to English. Numbers and dates follow the chosen
  language. Messages are stored language-neutral and rendered in the reader's language.
- Tests that keep all catalogs complete, with identical keys and placeholders in every language.
- Continuous integration on GitHub Actions.
- Packaging with `pyproject.toml`: `pip install .`, the console command `vinyl-check`, and the optional extra
  `[ml]` for the second-opinion network (PyTorch).
- Documentation: setup guide (from USB turntable to high end), user guide, measurement methods, architecture of
  stations and a central hub, design system, translating guide, contributing guide.

### Changed

- English code base: modules, identifiers, comments, docstrings, log messages and tests are in English. Modules
  renamed, for example `alignment`, `learning` and `second_opinion`.
- Installation-specific values (hosts, addresses) are no longer part of the repository. `deploy.sh` takes its
  target from `VC_TARGET`.

## Pre-release history

Versions before 1.0.0 were developed and used privately and were not published. Their history is summarised below
by minor version.

### 0.9.x

- Learning from master comparisons and manual judgements: an estimate of how many detections are real, validated
  per recording by cross-validation. Computing the grade from the estimate is optional.
- Test pressing approval report with charts: timeline with stamper defects and real clicks per copy, spectrogram
  with markers, excerpts around each audible defect, frequency response, wow and level per track.
- Approval report as a PDF generated on the server (ReportLab, IBM Plex) instead of browser printing.
- PDF report per recording.
- Judged detections are hidden from the event list, with a toggle to show them again.
- What-if: "don't count" per detection or skip, with the grade recalculated live and nothing saved.
- Descriptive regions in the event list: lead-in, run-out, lead-in groove, end groove, track, pause between tracks.

### 0.8.x

- Engineer mode: measure a recording against the digital master of the side. Covers alignment, polarity, wrong
  side, speed of the cut, wow per revolution and centre-hole eccentricity, channels, frequency response, level per
  track, pauses and pre-echo, which clicks are really on the vinyl, and a grade against the master.
- Copies compared on the master's time axis: defects common to several copies point to the stamper or the cut.
- Judging detections directly in the clicks chart. False alarms are shown in grey.
- Grading schemes 3 to 5: grade from the music only, weighted 90 % clicks and 10 % pops, and milder limits that
  accept the dust no cleaning removes. Stored analyses are regraded automatically when the scheme changes.
- One grade everywhere, with the grade against the master next to it. An interim grade "by ear" was dropped.
- Record grade over all copies, with the best and worst side left out from four sides on.
- Separate copy column in the recordings table.
- Comparing more than two recordings against a reference recording.
- Test pressing approval with checkpoints and a recommendation. Calibrated on real test pressings: up to three
  audible defects per side common to all copies are a note to the pressing plant, not a reason to reject.

### 0.7.x

- Heavy computations run in a separate low-priority process. A crash there no longer takes down the service.
- Second opinion from the GrooveClean click network, queried only at the detector's candidates and cached per file
  checksum.
- Manual judgements of detections (click, false alarm, unsure) and of skips (run-out groove, no skip), with an
  immediate recount and an export for training.
- Recording conditions: uncleaned, after wet cleaning, after dry cleaning, other.
- System page with running and queued jobs and machine load.
- Per-track table from the Discogs tracklist, with track borders snapped to the pauses.
- Preview listening without recording, hover-to-listen, zoom in the clicks and level charts, synchronised time
  cursor, play from any position, loop in zoom, mini player, report as its own page.
- Sleeve sticker suggestion per grade.
- Deploy waits for idle: new code is staged and the service restarts itself once nothing is recording or analysing.
- Findings (scratch, warp, dirt and more) per copy and side.
- Grade from the music, with pauses counting at most one step.
- Quiet passages also detected relative to the music level.
- Side, copy and condition editable in the recordings table. The table is sortable.
- Grading scale and criteria as a pop-up on the grade tiles.
- Own cover upload and tracklist for records that are not on Discogs.
- In-app documentation page and clipping thresholds in the settings.

### 0.6.x

- Stripe check: a click must be a thin vertical line in the spectrogram, which separates crackle from drum and
  cymbal hits. Pops need click strength, and clipped peaks never count.
- Turntable click floor, measured in the silent groove of the test record and stored in the profile.
- "Listen to the clicks only": a difference track of all counted clicks.
- One sensitivity control per turntable profile and per recording.
- F only when the stylus skips back. No grade when the groove is clipped. Noise no longer counts in the grade.
- Copy label per recording, comparison of copies, history per side with changes in percent, and the record grade
  as the average of its sides.

### 0.5.x

- Predicting the end of a side from earlier recordings or the Discogs tracklist, with browser notifications.
- Clipping reported with position, sample count and time.
- Back skips only with high similarity and an exact revolution period.
- Groove silence measured from the quietest full second, recurring-scratch chains, clicks and level charts.
- Comparisons only between recordings of the same side. Without a common alignment the result is "not comparable".

### 0.4.x

- Turntable profiles with a five-step alignment assistant. The active profile adapts every analysis (capture
  device, chain noise, reference level, speed).
- Target presets for alignment findings (standard, DIN 45500, strict lab values, manufacturer data), editable in
  the settings.
- Direct Discogs integration: read the collection, write the measured condition back, OAuth login.
- Technical data per recording: true peak, loudness per ITU-R BS.1770-4, correlation, tonal balance, rumble, hum.
- Recording level checked against the alignment reference.
- The run-out groove after the music is no longer reported as a skip, and clipping only counts inside the groove.

### 0.3.x

- Alignment mode with a test record: bands recognised by their signal, measurements with targets and advice, live
  meter, comparison of runs.
- Skip detector for groove skips.
- Spectrograms, with the overview stored in the database so it survives audio deletion.
- Channel selection for interfaces that only record with all channels open.
- Live charts for frequency response, frequency and level, plus a 31-band analyzer with ballistics.
- Covers from Discogs.
- Automatic stop in the run-out groove.
- Listening in the browser, with rewind during a recording.
- Design system in the app: logo, dark anthracite palette, icon set, menu and settings page.
- Fixes from the first measurements with a real test record.

### 0.2.x

- Data integrity: recordings synced to disk, SHA-256 checksum per recording, verified before every re-analysis and
  comparison.
- Retention: original audio is deleted after a configurable number of days, and measurements are kept.
- The service starts even when the data drive fails, and refuses to record without it.

### 0.1.x

- Click, pop and crackle detection from the prediction error of an autoregressive model.
- Analysis of quiet passages and groove noise, and a suggested condition grade.
- Before/after comparison with sample-accurate alignment.
- Web interface with live level, recording via ALSA, systemd service on a Raspberry Pi.

[Unreleased]: https://github.com/michaelpietzsch/vinyl-check/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/michaelpietzsch/vinyl-check/releases/tag/v1.0.0
