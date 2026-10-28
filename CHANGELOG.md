# Changelog

All notable changes to vinyl-check are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.1.3] – 2026-10-28

This release also contains the changes of 1.1.2 below, which was not published on its own.

### Fixed

- The macOS app could not open any HTTPS connection (“CERTIFICATE_VERIFY_FAILED … unable to get local issuer
  certificate”): testing a Discogs token, syncing the collection, cover images and signing in to Vinyl-Mind all
  failed. The OpenSSL bundled with the app looked for the trusted certificates in a directory of the build machine.
  All HTTPS connections now share one context with the operating system's trusted certificates (macOS keychain,
  Windows certificate store; on Linux and the Raspberry Pi the system's OpenSSL as before). `SSL_CERT_FILE` still
  takes precedence when set.
- The self-test of the packaged apps checks HTTPS with the system's certificates and one real connection, so CI
  catches this kind of failure: the build machines had the directory, the users' computers did not.

### Added

- A support address: questions and problems to info@vinylcheck.net (README, setup guide).

## [1.1.2] – 2026-10-27

### Fixed

- When the station did not answer, the interface showed the browser's own message (“Failed to fetch”). Requests now
  go through one place: reads are tried again after 1, 2 and 4 seconds, which covers a restart of the service, also
  behind a reverse proxy that answers 502–504 meanwhile. Writes are not repeated, since they may have arrived; the
  message says so in the interface language.
- After a restart of the service, a page could keep showing the state from before (for example the hint that the
  grade limits are not calibrated, after they were marked as calibrated). The status now carries the start time of
  the service, and the interface reloads the current view by itself after a restart, unless something is being typed
  or a report page is open.

### Changed

- The restart when idle (after an update or a changed configuration) also waits until nothing has been changed in
  the interface for a minute, so that it does not cut into someone working there. The notice says so and no longer
  asks to reload the page.

### Removed

- The GitHub Pages workflow and its site builder (`site/build.py`): the project's website is vinylcheck.net.

## [1.1.1] – 2026-10-26

A security release after an independent review of the station, the hub and the account service.

### Added

- The Windows installer (x64, recording through ASIO or WASAPI) is attached to this release: the ASIO SDK
  licensing agreement is in place. It is not signed yet, see [code signing](docs/code-signing.md).

### Security

- The `Host` header check (DNS rebinding) and the `Origin` check on requests that change something and on the
  WebSocket (cross-site request forgery, cross-site WebSocket hijacking) now apply to a service as well, not only to
  the desktop app. A service behind a reverse proxy lists the names it is served under in the new configuration key
  `trusted_hosts`; IP addresses, `localhost`, `.local` names and the host name are always accepted.
- A service that listens on all interfaces without an `access_code` logs a warning at start; the setup guide has a
  new section, *Running as a service in a network*, and recommends a code for every station other devices can reach.
- The generated API documentation (`/docs`, `/redoc`, `/openapi.json`) is switched off.
- Master uploads are accepted only in engineer mode, which is the only place the interface offers them.
- Cover images and the clicks-only track are sent as whole files without range requests: older Starlette releases,
  as shipped by Debian 13, had a weakness in range handling. Installations with `pip` require Starlette 1.10 or
  newer; the setup guide describes the state for distribution packages.
- Messages from the hub or the account service lose control characters before they reach the log or the interface.
- Vinyl-Mind: a published model is used only when it belongs to the group that was asked for (detector version,
  chain class), and never when it is older than the model adopted before for that group (version or creation time).
  After the hub withdraws a newer model, the station keeps its local model rather than an older global one; a
  rollback on the hub cannot put an old model back into use. Candidates were never used as the prior; a test now
  covers it.
- `systemd/vinyl-check.service`: `/opt` is read-only for the service (`ReadOnlyPaths`).
- Build and release: GitHub Actions are pinned to commits, the release workflow builds with read-only permissions
  and publishes in a separate job, and every release carries `SHA256SUMS` and a build provenance attestation
  (`gh attestation verify`, see [code signing](docs/code-signing.md)).

### Changed

- The setup guide no longer refers to a `vinyl-check` package on PyPI, where none is published yet: install from a
  source checkout or from the release wheel. CI fails when the documentation reintroduces the command.

## [1.1.0] – 2026-10-26

### Added

- Vinyl-Mind, the station side of learning together with other stations, built to the hub's specification, protocol
  version 1 (`spec/` and `docs/api.md` of the Vinyl-Mind repository); [Vinyl-Mind on the station](docs/vinyl-mind.md)
  says what the station does and what it sends. Never audio.
- *Settings → Vinyl-Mind*: the switches *Contribute to Vinyl-Mind* and *Use the global model* (both off by default),
  the server (the project's hub `https://api.vinylcheck.net` is preset; configuration key `vinyl_mind_url` presets
  another) with *Check server* (name, key fingerprint, clock and version warnings), status, *Send now*, *Reconnect*,
  *Disconnect*, *What is sent* with the exact document per turntable profile, and export and import of package files.
- Connecting as a sign-in, when the hub has an account service (`docs/connect.md` of the hub): *Connect with
  Vinyl-Mind* opens the account page in the browser on this computer (the desktop app uses the system browser) and
  comes back to the engine's loopback address by itself (OAuth with PKCE, `/vinyl-mind/callback`); *Connect with a
  code* shows a short code and a QR code to confirm on a phone (device flow), for a Raspberry Pi used from a phone or
  when the browser does not come back. The station joins with the invitation and the binding it receives, after
  checking that they name the configured hub (its canonical address, compared as a string) and its key. The secrets
  of the sign-in stay in memory only, never in the database, the logs or the interface. *Other ways to join*: an
  invitation code from the operator of a hub without accounts, or the operator's approval, after confirming the
  server key's fingerprint; *Where do I get a code?* explains it, with a link when the configuration names one
  (`vinyl_mind_invite_url`).
- The server address is kept in the hub's canonical form (https, lower case, no `:443`, no path, no trailing slash);
  an address with a path is refused in the settings.
- Keys: one Ed25519 key pair per hub, a file in the state directory (mode 600, never in the database, the logs or an
  API answer), deleted on disconnecting; *Reconnect* makes a new one. Every request is signed as the specification
  says, names the client (`Vinyl-Mind-Client: vinyl-check/1.1.0`) and sends its body with `Content-Length`; plain
  HTTP is refused except to the computer itself; redirects are not followed. When the hub's operator revokes the
  station (or the hub no longer knows it), the key and the state for that hub are deleted on the station as well,
  also when leaving is refused, and the settings say so; connecting again makes a new key.
- Contributions per turntable profile: the learning model trained on the recordings made with that profile, without
  any prior, as coefficients in the hub's shared scaling with example counts and the cross-check (estimate error, raw
  count error, AUC; errors above the schema's maximum sent as the maximum), and the profile's chain class. Features
  that never varied in a profile's examples (on a station without the second opinion `gc` and `gc_missing`) are
  marked as not informative, with the prior's weight and the intercept shifted so that the predictions stay the
  same. Each profile gets a random ID (16 bytes, base64url). Nothing is sent while a recording runs or while both
  switches are off; contributions only when they changed, at most hourly per profile. A hub that no longer accepts
  this version gets nothing more until vinyl-check is updated.
- Global models are verified as a JWS against the accepted server key before anything in them is read, validated,
  checked on the station's own master recordings and reported per profile; a published one becomes the starting
  point (prior) of the training per kind only where the estimate does not get worse. Withdrawn models are dropped.
- Turntable profiles have two fields for Vinyl-Mind, *Connection* (USB turntable, turntable with audio interface)
  and *Cartridge type* (MM, MC, unknown), preset to a guess from the profile's device and cartridge.
- Learning: coefficients in a fixed shared scaling per feature version (`FIXED_SCALING`, for feature version 1 the
  values of the hub's specification, stable there, with `gc_missing` centred at 1), `train(prior=...)` fits in that
  scaling and pulls every coefficient, the
  intercept included, towards a prior with strength 100 instead of towards zero (without a prior nothing changes),
  the cross-check also reports the AUC, training data can be limited to one turntable profile, and the detector has
  a version.
- Dependency `cryptography` (Debian: `python3-cryptography`, installed by `deploy.sh`); for the tests `jsonschema`.
- Learning: feature version 2 (experimental, configuration key `feature_version = "2"`), with the turntable profile
  as the zero point: the density of detections net of the profile's click floor, the level relative to the profile's
  reference tone in addition to the music reference, and how far a detection rises above the chain noise. Recordings
  without a measured and accepted profile fall back to values without profile information and are flagged; they
  never go into a contribution. Version 1 stays the default and is unchanged. Version 2 is not part of the hub's
  specification yet and is sent only to a hub that lists it.
- The feature version is used everywhere: the model is trained and applied in it (a stored model of another version
  is retrained), Vinyl-Mind contributions, model requests and reports carry it, and models, package files and priors
  of another version are refused. A profile without a measured zero point has nothing to contribute in version 2.
- `tools/eval_features.py` compares the feature versions: on synthetic chains (a budget USB turntable, a mid-range
  deck, an aligned high-end chain playing the same records) and on a copy of a station database (read-only), by the
  local estimate error, the transfer of weights between chains, federated averaging, and the similarity of weights.
- Tests against a fake hub and a fake account service that implement the specification on their own, and an
  end-to-end test against a real hub binary (`VINYL_MIND_BIN`, skipped without it).

### Changed

- *Settings → Network access* (desktop app) now appears above *Device and service*, followed by *Vinyl-Mind*.
- `deploy.sh` installs `python3-cryptography`, `python3-segno` (the QR code of *Connect with a code* on a Raspberry
  Pi) and `python3-jsonschema` (the tests).

## [1.0.1] – 2026-10-26

### Added

- The version is shown next to the logo; a click opens *Device and service* in the settings.
- Recording on macOS and Windows through PortAudio (`sounddevice`): Core Audio, WASAPI (exclusive mode by default,
  shared mode as a fallback) and ASIO (where a driver is installed). The same rules as with `arecord` apply: the
  configured rate and sample format, no resampling (a device that cannot record at the configured rate is refused
  with a message that says what to change), dropouts counted, inputs picked with `capture_channels` and
  `input_channels`. Samples that the host API delivers as floating point are converted back to the converter's
  integer values exactly.
- Configuration keys `audio_backend` (`auto`, `alsa`, `portaudio`; environment variable `VINYLCHECK_AUDIO_BACKEND`),
  `wasapi_exclusive` and `asio`. `auto` keeps `arecord` on Linux, so existing installations need no change.
- The device selection of the turntable profile lists PortAudio devices with audio system, inputs and default rate.
  Their names (`<audio system>: <device name>`) stay valid across restarts and re-plugging, and `device` in the
  configuration also accepts a part of the name. The settings show which audio system records.
- The recording status shows a note when WASAPI exclusive mode is not available, and when a device delivers only
  digital silence (on macOS usually missing microphone access). A device that stops delivering audio ends the
  recording with a message.
- Setup guide: recording on macOS and Windows (Core Audio, WASAPI, ASIO, bit depth, exclusive mode, enhancements,
  system sounds).
- Desktop apps for macOS (Apple Silicon, disk image) and Windows (x64, installer with uninstaller), built with
  PyInstaller from the same code base. The engine runs on 127.0.0.1 and the interface opens in a native window
  (pywebview); closing the window stops the engine, and the app asks first while a recording or an analysis runs.
  The computer is kept awake during a recording. PyTorch (second opinion) is not part of the apps. From source:
  `pip install ".[desktop]"` and `vinyl-check-desktop`. This release ships the macOS disk image; the Windows
  installer follows once the licensing for its ASIO support is in place.
- Access from other devices in the desktop app: the setting *Allow access from other devices on the network* (off
  by default) opens the interface to the local network and shows the address, a QR code and an access code. Other
  devices enter the code once (or scan the QR code, which carries it) and stay signed in with a cookie; the
  computer itself never needs it, and a new code signs all devices out. A service can require a code too, with the
  new configuration key `access_code`; without it, a station stays open to its network as before.
- `vinyl-check-desktop --selftest` checks an installation without a window: API, interface, WebSocket, an analysis
  in the worker process, a PDF report, the QR code, PortAudio and the window toolkit. CI runs it on the built apps
  (`.github/workflows/desktop.yml`), which are attached to each release.
- The license page `/licenses` with vinyl-check's license and, in the desktop apps, the notices of all bundled
  third-party software (`THIRD-PARTY-NOTICES.txt`, generated at build time), linked from the settings.
- [Code signing](docs/code-signing.md): status of the apps (not signed yet), first start on macOS and Windows, and
  how signing can be switched on. Windows signing is prepared in the workflow and stays off until configured.
- *Settings → Audio interface*: recording device, sample rate, bit depth, the pair of inputs and, on Windows, WASAPI
  exclusive mode and ASIO can be set in the interface (`/api/audio`), so the desktop apps need no configuration
  file. The device list shows audio interfaces first with their inputs and current rate; microphones, phones and
  virtual devices are listed separately and never chosen automatically. Only the rates the device reports are
  offered, and the page says when the interface would be switched to another rate. *Test* opens the device with
  the form's values for one second and shows the peak level per input or the reason it was refused (arecord's
  messages included). Values set there take precedence over the configuration file and apply from the next
  recording or level check without a restart; changes are refused while one runs.
- *Settings → Discogs*: the personal access token and the Discogs user name can be entered in the interface
  (`/api/discogs/account`), so the desktop apps need no configuration file for Discogs either. An empty user name
  is taken from the token. *Test connection* (`/api/discogs/test`) asks Discogs whom the token belongs to without
  saving it and warns when that is not the user name entered. The values apply at once without a restart, take
  precedence over `discogs_token` and `discogs_username` in the configuration file, and *Remove access token*
  deletes them. The token is kept in the database; the API only ever returns its last four characters and it is
  never logged. Switching to another account drops the previous account's cached collection. Connecting through a
  Discogs app (OAuth) works as before.

### Changed

- The interface starts in English for everyone. The new configuration key `language` sets another default (for
  example `"de"` on a station used in German), `"auto"` follows the browser as before; a language chosen in the
  settings always wins.
- License: vinyl-check is now licensed under the PolyForm Noncommercial License 1.0.0. Personal and other
  non-commercial use stays free; commercial use requires a commercial license from the author.
- `sounddevice` is a dependency on macOS and Windows. On Linux it is optional (extra `[portaudio]`) and only
  imported when PortAudio records.
- Entries of `/api/devices` carry the field `device` (the value to store in a profile); ALSA entries keep `alsa`.
- Without a service configuration (no `VINYLCHECK_CONFIG`, no `/etc/vinyl-check/config.toml`), configuration,
  database and recordings live in the user's folders: `~/Library/Application Support/vinyl-check` on macOS,
  `%LOCALAPPDATA%\vinyl-check` on Windows and the XDG folders on Linux. Service installations keep their paths.
- The PDF reports use the IBM Plex fonts that now ship with the package (SIL Open Font License); the system's fonts
  remain the fallback.
- The idle restart after a deployment only applies to the service, not to the desktop app.
- Windows: ASIO is used by default where an interface has an ASIO driver (`asio = "auto"`, the new default; `true`
  and `false` still work, `false` = WASAPI only). PortAudio's ASIO build is loaded, which carries WASAPI as well;
  ASIO drivers are listed first and preferred by the automatic choice, WASAPI exclusive mode serves devices without
  one. A rate the ASIO driver refuses points to its control panel.
- Without a configured device, vinyl-check picks an audio interface with enough inputs (an ASIO driver first, then
  USB) and never a microphone, phone or virtual device; without one it asks to choose a device. On macOS and
  Windows the rate follows the interface's current rate and the bit depth is 24 bit for interfaces, unless set in
  the settings or the configuration file. A station (arecord) keeps its configured values.
- The level check listens on the active turntable profile's device, like the recording.
- `/licenses` names ASIO as a trademark of Steinberg Media Technologies GmbH; the third-party notices of the
  Windows app state the Steinberg ASIO SDK Licensing Agreement.
- When Discogs is not set up, the interface and the API point to *Settings → Discogs*; only a service also names
  the configuration file, as the alternative.
- In test mode (a test file instead of a turntable), *Settings → Audio interface* shows a note instead of the
  choice of inputs, which the test file ignores.
- Stopping the service or quitting the desktop app ends a running analysis at once instead of waiting for it to
  finish; it keeps its state and runs again at the next start.

### Fixed

- Quitting the desktop app shuts the analysis process down cleanly, within a few seconds and with a hard exit as
  the fallback when something hangs, so Python's resource tracker no longer warns about leaked semaphores.
- Covers are requested with the credentials in use; with a Discogs app (OAuth) the request carried an empty token.

## [1.0.0] – 2026-10-23

First release.

### Added

- Multilingual interface in English and German. The web interface, API messages and errors, analysis findings and
  PDF reports come from translation catalogs (`vinylcheck/locales/`, `vinylcheck/static/locales/`). The language
  follows the browser, can be chosen in the settings and falls back to English. Numbers and dates follow the chosen
  language. Messages are stored language-neutral and rendered in the reader's language.
- Tests that keep all catalogs complete, with identical keys and placeholders in every language.
- Continuous integration on GitHub Actions: lint, tests on Linux (Python 3.11 to 3.14, with and without PyTorch),
  macOS and Windows, package build; releases are built from version tags.
- Packaging with `pyproject.toml`: `pip install .`, the console command `vinyl-check`, and the optional extra
  `[ml]` for the second-opinion network (PyTorch).
- Documentation: setup guide (from USB turntable to high end), user guide, measurement methods, architecture of
  stations and a central hub, design system, translating guide, contributing guide.

### Changed

- English code base: modules, identifiers, comments, docstrings, log messages and tests are in English. Modules
  renamed, for example `alignment`, `learning` and `second_opinion`.
- Installation-specific values (hosts, addresses) are no longer part of the repository. `deploy.sh` takes its
  target from `VC_TARGET` or `--target=user@host`; `--now` restarts at once.
- Stored and exchanged values are English codes: judgements of detections and skips, skip kinds, visual findings,
  master polarity and gaps, approval verdicts, side length sources, alignment band types, roles and channel modes,
  anti-skating verdicts and target map ids. API result keys follow (`explanation`, `interpretation`, `target`,
  `previous`/`current`). The interface shows them through the catalogs as before.
- **Existing databases are migrated automatically** on the first start, and a backup is kept next to the database
  (`<name>.pre-1.0.0-<time>.sqlite`). Notes, titles and other text you typed stay as they are. Texts that earlier
  versions stored with a result (warnings, alignment findings and advice) are kept in German; analyse a recording
  again to get them in the selected language.
- The configuration section `[justage]` is now `[alignment]`; the old name is still read, with a warning in the log.
- Interface addresses are `#record`, `#alignment` and `#docs`; old bookmarks keep working.
- The idle-restart marker of a deployment is `restart-when-idle`, and database backups are named `….pre-….sqlite`.
- Brand files renamed to `vinyl-check-wordmark.svg` and `…-inverse.svg`.
- The in-app documentation describes the current grading: weighted clicks and pops in the music, with the limits of
  grading scheme 5.

### Fixed

- Analyses failed on Windows because the worker process could not lower its priority (no `os.nice`).
- Alignment advice was appended twice when a target map was applied again, and was not removed when the map judged
  the value as ok.

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

[Unreleased]: https://github.com/michaelpietzsch/vinyl-check/compare/v1.1.3...HEAD
[1.1.3]: https://github.com/michaelpietzsch/vinyl-check/compare/v1.1.1...v1.1.3
[1.1.2]: https://github.com/michaelpietzsch/vinyl-check/compare/v1.1.1...v1.1.3
[1.1.1]: https://github.com/michaelpietzsch/vinyl-check/compare/v1.1.0...v1.1.1
[1.1.0]: https://github.com/michaelpietzsch/vinyl-check/compare/v1.0.1...v1.1.0
[1.0.1]: https://github.com/michaelpietzsch/vinyl-check/compare/v1.0.0...v1.0.1
[1.0.0]: https://github.com/michaelpietzsch/vinyl-check/releases/tag/v1.0.0
