# User guide

This guide follows the workflow in the vinyl-check interface: from a record to a side, a recording, the analysis
and the grade, then cleaning comparisons, master comparison and approval, turntable alignment and the settings.
Before your first session, set up and align your turntable as described in the [setup guide](setup-guide.md).

Labels in *italics* refer to buttons and headings in the interface. The in-app documentation (*Settings → Docs*)
explains every figure, colour and button.

The principle behind every workflow is the same: vinyl-check records a side as a WAV file and analyses it. The
grade, the cleaning comparison, the alignment findings and the master comparison are all derived from such
recordings, so a clean recording (aligned turntable, calibrated level, the whole side including the run-out groove)
is the most important step.

## Contents

1. [Layout](#layout)
2. [Records](#records)
3. [Recording a side](#recording-a-side)
4. [The recording report](#the-recording-report)
5. [Grades](#grades)
6. [Judging detections](#judging-detections)
7. [Cleaning comparison](#cleaning-comparison)
8. [Copies and history](#copies-and-history)
9. [Master comparison and test pressing approval](#master-comparison-and-test-pressing-approval)
10. [PDF reports](#pdf-reports)
11. [Turntable profiles and alignment runs](#turntable-profiles-and-alignment-runs)
12. [Settings](#settings)
13. [System and jobs](#system-and-jobs)

## Layout

| Area | Purpose |
|---|---|
| **Header** | navigation (*Overview*, *Record check*, *Turntable check*, *System*, *Settings*) and status pills: active turntable profile, running analysis, new version waiting |
| **Deck** | always visible at the top, whatever page you are on: level meters (L/R, RMS, peak and hold), recording state and time, warnings, and the transport buttons *Check level*, *Calibration (stylus up)*, *Listen* and *Stop*. A recording keeps running when you switch pages. |
| **Overview** | the state of the chain (device, calibration, storage, retention), the most recent recordings, and *Next up* with the next sensible step |
| **Record check** | your records, each record's recordings, reports and comparisons |
| **Turntable check** | turntable profiles, the alignment assistant and alignment runs |

**Listening.** *Listen* plays the turntable in the browser with about 0.5 s delay. Without a recording it starts a
level check by itself (preview). During a recording you can rewind (−30 s, −5 s), skip forward (+5 s, +30 s, up to
live) and return to *Live*. Rewound audio comes from the file being written. The recording never waits for the
listener: if the browser falls behind, it drops blocks.

**Analyzer.** While the level check or a recording runs, a 31-band analyzer (third-octave bands per ISO 266, left
and right, with peak hold) shows the spectrum.

**Notifications.** On the first recording, the browser asks for permission to show notifications. vinyl-check uses
them for *side probably finished*, *stopped in the run-out groove* and *recording analysed*. No external service is
involved.

## Records

**Create a record** in the left panel of *Record check*:

- Type into the search field to search your Discogs collection (if Discogs is connected, see
  [settings](#settings)). A hit fills in artist, title, release and the condition stored in your collection.
  vinyl-check also takes over the tracklist and the cover.
- Or enter artist and title by hand.

**The record page** shows the cover, title, link to the release, the condition block, the known side lengths and
the tracklist. Below follow the recording form, the table of recordings, the copies, the history per side and the
list of comparisons.

- **Condition block.** *In collection* is the condition stored at Discogs, and *Measured* is vinyl-check's grade
  per copy. Hover over or tap either tile to see the grading scale and the criteria. When the two differ,
  vinyl-check offers to write the measured grade to your Discogs collection (media condition only, sleeve
  condition and notes stay untouched). Nothing is written without your confirmation.
- **Cover.** Click the cover to upload your own image (JPEG, PNG or WebP). The browser scales it to 1200 px.
  This helps with test pressings and records that are not on Discogs. *Remove own cover* restores the Discogs
  cover.
- **Side lengths and tracks.** Side lengths come from earlier recordings of the same side, otherwise from the
  Discogs tracklist. They drive the *side probably finished* notice during recording and the per-track table in
  the report. Records without Discogs data can get a tracklist through the API (`PATCH /api/records/{id}` with
  `tracks`). There is no editor in the interface yet.

## Recording a side

1. Clean the stylus and *Check level* (no red on loud passages).
2. In the form *Record*, choose:
   - **Side**: `A`, `B`, … or a free label such as `C1` (up to 8 characters).
   - **Condition**: *uncleaned*, *after wet cleaning*, *after dry cleaning* or *other*.
   - **Speed**: 33⅓ or 45 rpm. The skip detector needs it.
   - **Cleaning** (optional): the method, for example *brush* or *ultrasonic*.
   - **Copy** (optional): *Copy 1*, *Copy 2*, … if you own the record more than once. Case and spaces are
     ignored, and earlier labels are suggested.
   - **Remark** (optional).
3. *Start recording*, then lower the stylus. Record the whole side **including lead-in and run-out groove**.
4. vinyl-check stops by itself after three revolutions in the run-out groove. You can stop by hand at any time.
5. The analysis starts automatically. A pill in the header shows it, and the recording table updates when it is
   done.

While recording, the deck shows clipping (where, how many samples, when the last one happened), dropouts, the time
since the music started and the expected remaining time of the side.

**Recording table.** Click a column heading to sort, and click again to reverse the order. *Default* restores the
order by copy and date. Double-click (or double-tap) the side, copy or condition cell to change it after a
confirmation. A changed side re-analyses the recording, because the tracklist differs per side. Per row: *Report*,
re-analyse (↻) and delete (✕).

## The recording report

*Report* opens the analysis of one recording as a full page (*Back to record* returns).

- **Grade tile** with score, sticker and the note explaining it, plus key figures: clicks per minute in the music
  (raw, confirmed by the second opinion, estimated real), clicks and crackle in pauses, pops per minute and noise.
- **Clicks chart**: detections per 2 seconds (pops red, clicks yellow, crackle grey). Lead-in and run-out are
  drawn below the axis and are not counted.
- **Level chart**: level over time with lines for groove silence, the music reference and the chain noise. Zones
  with the stylus up are grey.
- **Spectrogram**: time against frequency (20 Hz–16 kHz, logarithmic), in colour or greyscale. Clicks are
  vertical lines, groove noise is a haze, rumble is a band at the bottom, and hum is a horizontal line.
- **Single grades** per measured figure, with their role in the grade.
- **Tracks**: clicks, crackle, pops and scratches per track, with play buttons. The worst track is red. The table
  only appears when the durations of all tracks of the side are known.
- **Events**: the strongest detections and all skips in one list (see [judging](#judging-detections)).
- **Technical data**: peak and true peak per channel, RMS, crest factor, loudness (ITU-R BS.1770-4), DC offset,
  correlation and stereo width, tonal balance, rumble and mains hum, noise, clipping and recording format.
  Hover over a row for an explanation.

**Charts.** All charts share one time axis and behave the same way. Hovering shows a time cursor in all three,
clicking plays from that point (30 s), dragging zooms (a zoomed range plays as a loop), and double-clicking shows
the whole side. With *Play on hover* switched on, the nearest click under the pointer plays briefly.

**Listen to the clicks only.** This plays a difference track that contains nothing but the counted clicks, with
silence in between. If you hear music in it, those detections are false alarms.

**Sensitivity.** The detector has a single control from 0 to 1 (0.5 is the calibrated point). It can be set per
turntable profile and overridden per recording. Changing it re-analyses the recording.

## Grades

vinyl-check grades on the Goldmine scale: **NM, VG+, VG, G+, G, F**. *Mint* is never given, because it needs a
visual inspection.

- The grade of a side comes from the **music**: clicks per minute while music plays (confirmed by the second
  opinion when available) and pops per minute of music, weighted 90 % and 10 %. Clicks and crackle in pauses are
  shown for information only.
- **F only for skips.** A side that plays through ends at G at worst. When the stylus jumps back, the side is F.
- **No grade** when more than 500 samples in the groove are clipped. That is a recording error: lower the gain and
  record again.
- The **record grade** per copy is the average of its sides, using the latest graded recording of each side. Half
  steps round towards the worse grade, and if one side skips, the record is F.
- **Stickers.** Next to each measured grade, vinyl-check shows which of six smiley stickers (*Excellent* to
  *Very Bad*) matches it, for marking the sleeve.

The default limits are starting values. How the grade is computed and how to adjust it is described in
[measurement](measurement.md#grading).

**What-if.** Every row in the event list has a *don't count* button. The grade is recalculated at once, and a bar
above the list shows the result, for example *without 1 skip: side VG instead of F*. Nothing is saved, and the
selection is reset when you leave the page or choose *count everything again*.

## Judging detections

Your judgements make vinyl-check better. In the event list (or in the pop-up when you click a detection in the
zoomed clicks chart), listen with ▶ and decide:

| Judgement | Meaning | Effect |
|---|---|---|
| *Click* | a real defect | confirmed, counts |
| *False alarm* | the music itself (a drum hit, a pluck) | removed from counts, grade and scratch search |
| *Unsure* | can't tell | stays as detected |

For skips, the choices are *run-out groove* (no damage) and *no skip* (rhythm or a loop). Judgements are applied
immediately, survive re-analysis and are the training data for the [learning](measurement.md#learning) model.

Judged rows disappear from the list so that you can work through the rest. *Show judged* brings them back for
corrections and shows how many are hidden. The charts always show every detection, with false alarms in grey.

## Cleaning comparison

1. Record the side **uncleaned**, ideally twice.
2. Clean the record and record it again with the condition *after wet cleaning* or *after dry cleaning*, with the
   same settings.
3. Tick two recordings in the table and choose *Compare selected*.

vinyl-check aligns both recordings, including small speed differences, and labels every click:

| Label | Meaning |
|---|---|
| *removed* | only before cleaning: it was dirt |
| *persistent* | in both: physical damage, cleaning does not help |
| *new* | only after cleaning: residue or dust from handling |

Comparing two uncleaned recordings shows the **repeatability**: how much the measurement scatters on its own. Only
improvements clearly beyond that are real. After a wash, loose dust moves around, so many clicks change place while
the fixed defects stay.

The comparison shows both recordings' spectrograms on the same time axis and colour scale (click to listen,
Shift+click for the second recording), the figures side by side with the change in percent, and skips. It only
compares recordings of the **same side** of the same record. If no common music is found, it says so instead of
reporting nonsense.

**More than two recordings.** Tick three or more and choose *Compare selected*. The first ticked recording is the
reference, and each further recording is compared against it. The summary lists grade, clicks and pops per
recording, what persisted or disappeared, and the defects found at the same place on all recordings.

## Copies and history

- **Copies.** With copy labels, the recording table groups by copy, the record list shows a grade per copy, and
  the record page states which copy is better (latest recording of each side, compared by grade, then clicks in
  pauses, then clicks in the music).
- **History per side.** Each analysed recording of a side and copy appears in chronological order, with the change
  against the previous one in percent. *n/a* means the turntable or level differ, so the numbers are not comparable.
- **Findings per copy and side.** In the report of a recording you can tick visible findings (visible scratch,
  hairline scratch, warp, dirt, fingerprints, off-centre hole) and add a remark. Findings belong to the copy and
  side, not to a single recording, so all recordings of that copy and side show them.

## Master comparison and test pressing approval

Switch on *Settings → Engineer → Engineer mode*. The record page then shows a **Master** section.

**Upload a master** per side (WAV, FLAC or AIFF, up to 3 GB). Masters are kept outside the audio retention period.
Every analysed recording of that side is then measured against the master automatically, and uploading a master
also measures the recordings that already exist.

**Against the master** (in the recording report):

| Measurement | What it tells you |
|---|---|
| Alignment and polarity | offset, speed and polarity of the whole chain; a recording that matches the master of another side is flagged as wrong side |
| Speed of the cut | speed against the master minus the turntable's own speed from alignment = error of cutting and pressing |
| Wow per revolution, eccentricity | wow at the start, middle and end of the side; eccentricity of the centre hole in mm |
| Channels and balance | channels swapped or not, balance against the master |
| Frequency response | per third of the side, against the master; high-frequency loss towards the inner grooves |
| Level per track | deviation of each track from the master |
| Clicks only on vinyl | which detections are in the master (music, false alarm) and which are real defects |
| Grade against the master | the same grading, counting only clicks and pops the master does not explain |
| Pauses | noise and pre-echo where the master is silent |

Each finding has ▶ *Vinyl* and ▶ *Master* for A/B listening at the same position.

**Copies against each other.** With at least two copies of a side measured against the master, vinyl-check lists
defects at the same place (±3 ms) on several copies. Those are in the stamper or the cut, and the rest belongs to
the individual copy (dust, static, pressing faults).

**Test pressing approval.** *Test pressing approval* in the master section evaluates every side with a master
across all copies:

| Checkpoint | ok | note | not ok |
|---|---|---|---|
| Stamper or cut: audible defects at the same place on several copies | none | on several copies, or up to 3 on all (report to the plant) | more than 3 on all |
| Speed of the cut against the master (turntable removed) | ±0.3 % | ±0.6 % | more |
| Centre hole eccentricity (median) | ≤ 0.2 mm | ≤ 0.35 mm | more |
| Copies: grade against the master (median) | NM / VG+ | VG | worse |
| Pre-echo (strongest) | below −45 dB | from −45 dB | |
| High frequencies at the end of the side (10 kHz) | info | from −6 dB | |
| Polarity and channels | info | channels swapped | |

The result is *approval recommended*, *approval with notes* or *do not approve*. The thresholds are suggestions,
not a standard. A single bad copy does not speak against the run, and defects common to all copies are reported to
the plant and only count against approval in larger numbers. The thresholds were calibrated on real test pressings:
two audible clicks per side on all copies were judged acceptable by the mastering engineer.

## PDF reports

- **Recording report**: *PDF report* at the top of a recording report. It contains the grade with score and
  sticker, key figures, warnings, findings, the clicks and level charts, the spectrogram, tracks, single grades,
  skips, the strongest detections with judgements, the master comparison in engineer mode, technical data and the
  grading scale.
- **Approval report**: *Download PDF report* in the test pressing approval. It has a title page with the
  recommendation and an overview, then one page per record side with checkpoints, copies, common defects, a
  timeline, the spectrogram with markers, excerpts around each audible defect on every copy, frequency response,
  wow and level per track, and an appendix with method and thresholds.

Reports are generated on the server (ReportLab) in the language of the interface. With the IBM Plex fonts installed
they use IBM Plex Sans, otherwise Helvetica.

## Turntable profiles and alignment runs

*Turntable check* holds your turntable profiles and the alignment runs with the test record. The full procedure,
including what each finding means, is in the [setup guide](setup-guide.md#alignment-with-vinyl-check-and-a-test-record).
In short:

- **Add turntable** starts a five-step assistant: create the profile, calibrate the chain with the stylus raised,
  record side 1 and side 2 of the test record, and review and accept. You can resume it at any time.
- The **active profile** (shown in the header) applies to every new recording: capture device, chain noise,
  reference level, speed, click floor and sensitivity. Recordings from different turntables carry a note, because
  their clicks are only comparable within the same chain.
- **Alignment runs** record one turntable setting each (tracking force, anti-skating, VTA, stylus hours, input
  level). Each run shows its findings per figure with value, target, status and advice. *View* shows the
  recognised bands and the curves (anti-skating ramp, resonance, frequency response, wow & flutter).
- **Compare two selected runs** shows what an adjustment changed. ▲ marks the better value.
- **Live measurement** in the deck shows distortion per channel five times per second while you adjust.
- **Targets** decide what counts as ok. Choose a preset per profile or globally under *Settings → Targets*.

## Settings

| Section | Contents |
|---|---|
| *Audio interface* | recording device, sample rate, bit depth, input pair; on Windows WASAPI exclusive mode and ASIO. *Test*, *Save*, *Back to the configuration file*. |
| *Recording* | stop automatically in the run-out groove, longest recording, minimum free disk space |
| *Retention* | how long original audio is kept (default 30 days; measurements, grades, comparisons and the overview spectrogram stay). *Apply retention now*. |
| *Discogs* | sync interval of the collection (default 15 minutes) |
| *Clipping* | grace period at start and end (5 s), mention from (200 samples), no grade from (500 samples). Applies to new analyses. |
| *Engineer* | engineer mode (master comparison, test pressing approval) |
| *Learning* | learn from master comparisons and judgements (default on), compute the grade from the estimate (default off), the state of the model per kind with its cross-validation, *Relearn now* |
| *Display* | colour scheme (light, dark or system) and interface language; both are stored in this browser |
| *Discogs connection* | personal access token and user name (save, test, remove), connect or disconnect a Discogs app, sync the collection now |
| *Targets* | target presets for the turntable check: edit, copy, reset, set active |
| *Vinyl-Mind* | learning together with other stations: *Contribute* and *Use the global model* (both off), server, *Join*, status, *What is sent*, export and import of package files |
| *Device and service* | version, the capture device and format in use, the audio system (Core Audio, ASIO and WASAPI, ALSA), paths. Read-only: everything except the audio interface and the Discogs access lives in the configuration file; restart the service after changing it. |

**Audio interface.** Choose which input records the turntable and in which format. The device list shows the audio
interfaces first, each with its inputs and the rate it runs at now; microphones, phones and virtual devices (video
calls, routing software) are listed under *Other inputs* and are never chosen by *Automatic*. *Sample rate* offers
what the device supports and says when the interface would be switched to another rate (other programs using it
notice). *Inputs* lists the pairs of the device: choose the one the phono stage is connected to. Under each field
a note says where the value comes from: set here, from the configuration file, from the device, or the default.

*Test* opens the device with the values in the form for one second (nothing is saved or recorded) and shows the
peak level of the left and right input, or why it did not work. Play a record during the test to see the level.
*Save* stores all values shown; they apply from the next recording or level check. While a recording or level
check runs, the section cannot be changed. The device of the active turntable profile takes precedence over the
device chosen here. *Back to the configuration file* removes the values saved here.

**Language.** The interface starts in English, or in the language set with `language` in the configuration file
(`"auto"` follows the browser). Choosing another language under *Settings → Display* stores it in a cookie, so API
messages and PDF reports follow it as well.

**Discogs.** The simplest way to connect is a personal access token:

1. On Discogs, open *Settings → Developers* ([discogs.com/settings/developers](https://www.discogs.com/settings/developers))
   and choose *Generate new token*.
2. In vinyl-check, paste the token under *Settings → Discogs*, enter your Discogs user name (or leave it empty to
   take it from the token) and choose *Save*. It applies at once, without a restart, and the collection is synced.
   *Test connection* asks Discogs whom the token belongs to without saving it, and warns if that is not the user
   name you entered. vinyl-check keeps the token in its database and only ever shows its last four characters
   again; *Remove access token* deletes it.

On a service, the token can also be set in the configuration file (`discogs_token`, `discogs_username`; restart the
service afterwards). A token saved in the settings takes precedence over the file. A Discogs application
(`discogs_consumer_key`, `discogs_consumer_secret` in the configuration file, created under Discogs → Settings →
Developers) is the alternative to a token: choose *Connect with Discogs* in the settings and confirm the access at
Discogs once. That access token is stored in vinyl-check's database too, and *Disconnect* removes it.

Without access, Discogs features are off and everything else works. The collection is cached locally. If a search
finds nothing and the cache is older than a minute, vinyl-check syncs immediately and searches again.

**Vinyl-Mind.** Several stations can pool what their learning models learn (see
[architecture](architecture.md#federated-learning-with-vinyl-mind)). Only weights and counts per turntable profile
leave the station, never audio, record titles or names; the [protocol](vinyl-mind-protocol.md#what-leaves-a-station)
lists everything that is sent.

- *Contribute to Vinyl-Mind* sends, after each retraining of the learning model, the contribution of every turntable
  profile whose data changed (at most once an hour per profile). *Use the global model* fetches the shared model
  every few hours, checks it on your own master recordings and uses it as the **starting point** of the training
  only for the kinds where the estimate does not get worse. Both are off until you turn them on; with both off,
  nothing is sent at all, and nothing is sent while a recording runs.
- *Server* is the address of the Vinyl-Mind server, `https://` only. There is no default server yet; a configuration
  file can preset one (`vinyl_mind_url`), and *Reset to default* restores it.
- *Join* (with an invitation code, if the operator gave you one) first shows the fingerprint of the server's key:
  compare it with the one the operator publishes. After joining, the station waits for approval unless the code
  admitted it directly, and the fingerprint stays visible under *Server key*. Every model is checked against that key
  before it is used. *Send now* sends changed contributions and asks for a newer model at once. *Leave* deletes the
  station's contributions on the server and its key on the station; if the server is gone, the station can forget it
  locally.
- *What is sent* shows the exact data of the next contribution per profile, with a random profile ID (never the
  name) and the coarse chain class: USB turntable or turntable and interface, MM or MC. The class is guessed from the
  profile's device and cartridge; correct it there if the guess is wrong.
- *Package files* work without a server: *Export* saves a profile's contribution as a file, *Import* reads a
  contribution or model file from another station, says what it is and, after you confirm, uses it as the starting
  point of the training. *Starting point → Remove* trains from zero again.

## System and jobs

The *System* page lists the running job (with a rough progress estimate), the queue and the last 30 finished jobs
with their duration, plus CPU (total and per core), load, memory, the service's own memory, CPU temperature,
uptime and storage. The analysis pill in the header opens the same list.

Heavy work (analysis, alignment evaluation, comparisons, spectrograms) runs one job at a time in a separate
low-priority process, so the interface and an ongoing recording stay responsive. If that process runs out of memory,
the job is reported as failed and the service keeps running.

**Data integrity.** Every recording is synced to disk before it counts as complete. Its SHA-256 checksum is stored
at the first analysis and checked again before every re-analysis and comparison. A mismatch stops the job with an
integrity error instead of computing with corrupted data. After the retention period, listening, re-analysis and
new comparisons are no longer possible for that recording (it is marked *audio deleted*), but all results remain.
