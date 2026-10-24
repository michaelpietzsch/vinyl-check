# Design system

The rules behind vinyl-check's interface: principles, information architecture, layout, tokens, icons,
components, key screens, accessibility and a measurable design QA. The approach follows Otl Aicher's systematic
design: a set of rules rather than a collection of attractive pages.

- Visual preview of icons, colours, type, components and screen sketches: [design-preview.html](design-preview.html)
  (open it in a browser; it follows the system's light or dark setting).
- Icon sprite: `vinylcheck/static/icons.svg`. Logo files: `vinylcheck/static/brand/`.
- Tokens in code: the custom properties at the top of `vinylcheck/static/style.css`.

Parts of this document describe the target state. Items not yet built are marked *proposal*, and the
[implementation status](#implementation-status) lists what is done.

## Inputs

| | |
|---|---|
| Product | **vinyl-check**, a web application that records a record side, counts clicks, grades the condition, compares before and after cleaning, measures the turntable's alignment with a test record, and compares test pressings with the master. Live: levels, a 31-band analyzer, listening with rewind. |
| Users | **Audio-aware people at the turntable and at the desk**: collectors who care about condition, audio and mastering engineers, studios and pressing plants checking test pressings. Two contexts of use: **at the turntable** (standing, hands on stylus and record, looking at a tablet or laptop from 1–1.5 m) and **at the desk** (reading results, comparing, documenting). Occasionally others in the same network read along. There are no accounts and no roles. |
| Main tasks | record a side (before), clean, record again (after), compare, grade; alignment run with the test record and live measurement at the anti-skating dial; create records (Discogs); calibration. Rarely: configuration, retention, integrity. |
| Platform | a responsive web app served by the station on the local network. Desktop browsers, tablets and phones at the turntable. Works offline: no CDNs, no web fonts. |
| Brand | **black and anthracite** as main colours, dark as the main mood and light as the variant. Own pictograms and wordmark. A red accent stands for the record label. |
| Technology | plain JavaScript without libraries or a build step, FastAPI, status over WebSocket, charts on `<canvas>`. Everything this system defines must be expressible with CSS custom properties, a small set of classes and one SVG sprite. |
| Language | multilingual (English and German, more via catalogs). Numbers and dates follow the reader's locale. Units are separated by a space (`−20 dBFS`, `3.01 g`), and negative values use a true minus sign. |

## A. Product understanding

**What it is:** a measuring station with a protocol. Two procedures share the same recording chain: the *record
check* (condition of a record, effect of a cleaning) and the *turntable check* (condition of the turntable). Both
produce **recordings** (WAV plus measurements) and **findings** (value, target, verdict, text).

| Context | Goal | Must be grasped quickly | Time-critical |
|---|---|---|---|
| At the turntable | start a recording safely, lower the stylus, let the side play, turn the anti-skating dial | Is it running? Since when? Clipping? Dropouts? Which side and condition? How loud? | start/stop, the "GO" for the stylus, reacting to clipping |
| At the desk | read findings, compare before and after, settle the grade, decide on alignment | grade, clicks per minute, change against the previous recording, status per figure | nothing |

**Tasks by frequency**

| Level | Tasks |
|---|---|
| Frequent (per record) | choose record · record a side (before) · after · listen · read the result · compare · accept the grade |
| Regular | alignment run (after changes, a new stylus, as a check) · live measurement · calibration (stylus up) |
| Administrative | create a record or take it from Discogs · device and channels in the configuration · retention · integrity check · deployment |
| Special cases | clipping, dropouts, platter stop, network loss, mono recording, live album, 45 rpm, records with four or more sides |

**Decisions the interface must support:** Is the recording usable (clipping, dropouts)? Did the cleaning help,
beyond the repeatability? Which grade? Which alignment setting is better? Do I need to adjust the turntable, and
what first?

**Where errors are expensive:** a lost recording (play the side again, 25 minutes), wrong alignment advice
(physical adjustment of stylus and arm), a wrong grade written to Discogs. Consequences: never show a finding
without its target and explanation, always show why something stopped, and show the state of the recording chain
before every start.

## B. Design principles

1. **The station is a transport, not a form.** The *deck* bar is always on top: state, time, level and **one**
   primary button per state (ready → *Record side*, running → *Stop*). Everything else ranks below it, because the
   time-critical actions happen with the hands on the record.
2. **A number is not a finding.** Every figure appears as a quartet of *value · target · verdict · explanation*.
   Raw values without context only appear in expandable details.
3. **Time is the common axis.** Overview, spectrogram, events, skips and the listening position share one time axis
   and one interaction pattern: *click = listen, drag = zoom, double-click = everything, keyboard = type a time +
   ▶*. No two charts behave differently.
4. **Two channels, two colours, everywhere.** Left is blue and right is violet, in meters, the analyzer, curves and
   tables. Status colours (green, yellow, red) are never used for channels, so red is never ambiguous.
5. **Status speaks in words.** `ok · note · problem` is always shown as icon, word and colour. The recording state
   is a word plus time, and grades are letters with a score. Colour alone carries nothing.
6. **Readable from 1.5 metres.** While recording, state and time grow to 40 px, meters are wide bars, targets are at
   least 44 px, and the dark mode suits the listening corner. Nothing important is available on hover only.
7. **Nothing disappears silently.** Automatic stop, retention, dropouts, deleted audio, lost connection: each state
   has a sentence with the reason and the next action.
8. **Raw data stays reachable, but second.** Band times, WAV path, SHA-256, device and format are there (monospace,
   under *Details*), but never before the finding.
9. **The system lives in tokens, not pages.** No library, no web fonts, no build: everything defined here maps to a
   `--token` and a class in `style.css`. Whatever cannot be expressed that way is designed wrongly.

## C. Information architecture

**Main navigation** (order = frequency):

| Area | Icon | Content |
|---|---|---|
| **Overview** | tonearm | chain state, recent recordings, *next up* |
| **Record check** | disc | list and search → record → recording → comparison |
| **Turntable check** | sliders | turntable profiles → alignment runs → run → sides and bands → run comparison; live measurement in the deck |
| **System** | clock | running and queued jobs, machine load |
| **Settings** | nut | recording, retention, Discogs, clipping, engineer mode, learning, display and language, targets, device and service (read-only) |

The deck belongs to no area. It stays in place when you switch areas, because a recording keeps running wherever
you are reading. The mental model is "one device with a transport, plus an archive".

**Hierarchy and addresses** (*proposal* beyond the top-level areas, which already have `#` addresses): `#station` ·
`#records/12` · `#records/12/recording/16` · `#compare/15/16` · `#turntable/5` · `#turntable/compare/4/5`. Every
detail page has a breadcrumb (`Records › Test Artist – Test Album › Recording 16`). At most three levels deep.

**Terms (binding):** record · side (A, B, C …) · recording (condition: uncleaned, after wet cleaning, after dry
cleaning, other; plus calibration and alignment) · comparison · run (alignment) · band (of the test record) ·
finding (one measured result) · grade (condition) · deck (transport) · chain (turntable → phono stage → interface →
station) · copy (one physical copy of a record). Use one term per concept and no synonyms: not "setup" for run,
not "session" or "take" for recording, not "monitor" for level check.

**Search and filters.** The global search is the record search (local records and the Discogs collection in one
field, results in two groups). Local filters: recordings by condition and side; events by kind (pop, click,
crackle), region and minimum strength; alignment findings by status. Active filters appear as chips above the
table with *reset all* next to them. There are no saved views: *Recent* on the overview covers the last record and
the last run.

**Roles:** none. *Proposal* for readers on other devices: identical navigation, transport buttons disabled with the
note "operate at the station".

## D. Screen inventory

| # | Screen | Type | Purpose |
|---|---|---|---|
| 1 | Deck | persistent | state, time, level, primary action; expandable: analyzer, listening, live measurement |
| 2 | Overview | dashboard | chain state (calibration, storage, device), recent recordings, *next up* |
| 3 | Records | list and search | find or create a record, cover, latest grade, number of recordings |
| 4 | Record | detail and table | master data, cover, recordings per side, comparisons, copies, history |
| 5 | Recording report | detail and analysis | grade header, time axis, spectrogram, events, skips, details |
| 6 | Comparison | analysis | two or more recordings: deltas, common time axis, removed / new / persistent clicks, repeatability |
| 7 | Turntable profiles and runs | list | profiles, runs with their settings (tracking force, anti-skating, VTA, hours) |
| 8 | Run | detail and findings | findings of both sides, bands, deviations |
| 9 | Run comparison | analysis | two runs side by side, difference per figure, better or worse |
| 10 | Live measurement | deck, expanded | distortion L/R, test tone, level, frequency trend, analyzer |
| 11 | Settings | settings | editable settings, read-only device and service values with the path of the configuration file |
| 12 | System | status | jobs, queue, history, load |
| 13 | Dialogs | form / critical | record a side (*proposal*: as a dialog), create record or run, delete with consequences |
| 14 | Banner | system status | connection lost, storage full, not calibrated, demo mode, analysis running, new version waiting |

Layout types: dashboard (2), list (3, 7), detail (4, 8), analysis (5, 6, 9, 10), settings (11), dialog (13). There is
one assistant, the five-step turntable profile, because alignment has a fixed order. The record check is linear
enough for the deck and the record page to lead it, with a *next up* line instead of a wizard.

## E. Core workflows

**W1 – Check a record (most frequent)**
1. Overview: the deck shows "ready · chain calibrated · 2 GB free". The primary action *Record side* stays disabled,
   with the reason, until a record is chosen.
2. Records: search (one field), take a hit from Discogs or create a record. It is now the chosen record.
3. Record side with prefilled values (side A, uncleaned, 33⅓, last cleaning method) → *Start recording*.
4. The deck switches to recording (large: `● Recording · uncleaned · Side A · 0:00`) with the hint "Lower the
   stylus, include the lead-in groove". Listening is possible.
5. The side plays. Clipping and dropouts appear immediately as a line in the deck (word and number, not just colour).
6. End groove → automatic stop with the line "End groove detected, stopped automatically" → "Analysing" with
   progress.
7. The result replaces the progress line: grade, clicks per minute, noise, plus *Open recording*, *Next: side B*
   and *Record after cleaning*.
8. After cleaning: step 3 with the cleaning condition, method prefilled. The record then offers the comparison in one
   click.

**W2 – Alignment run:** create a run (settings as number fields with units) → record side 1 → side 2 → findings
sorted problem / note / ok → "what to do" per row → compare with the previous run.

**W3 – Live at the anti-skating dial:** deck *Live measurement* → play the band → large distortion figures L/R with
the distance to the threshold and a 60 s curve → adjust → see the reaction → stop. Nothing needs to be saved.

**W4 – Decide after a comparison:** the comparison shows the change per figure **against the repeatability** (two
uncleaned recordings): "−18 clicks/min, scatter ±3 → real improvement". Accept the grade.

**W5 – First use and calibration:** the overview shows "not calibrated" as a banner with a button → instructions
(stylus up, 20–30 s) → result with value and verdict.

**W6 – Disruption** (*proposal*): clipping, dropouts or a platter stop → the deck offers *Cancel and restart* (one
action: stop, mark the recording as discarded, start a new one with the same details).

## F. Layout system

- **Spacing:** 8 px system, scale `4 · 8 · 12 · 16 · 24 · 32 · 48` (`--s-1 … --s-7`). Card padding 16, heading to
  content 12, groups 24, sections 32.
- **Breakpoints:** S < 600 (phone), M 600–1023 (tablet, at the turntable), L 1024–1439 (laptop), XL ≥ 1440
  (content limited to 1440 and centred). Page margin 16 / 24 / 32 / 32.
- **Grid:** 12 columns from L, 8 on M, 4 on S, gutters 16 / 24.
- **Page structure:** header (title, navigation, system state) → deck (sticky, 64 px when ready, up to 200 px when
  active) → content. List and detail as two columns, `320 px | rest`, from L. On M and S they follow each other with
  a back arrow.
- **Content widths:** running text and finding texts at most 72 characters. Tables and charts use the full width,
  and charts have fixed heights (overview 160, spectrogram 160 per row, analyzer 180).
- **Cards only for independent units:** record, recording, run, deck. Inside them, spacing and headings structure
  the content: no nested cards, no shadows.

## G. Visual system

### Typography

System fonts, no web fonts: `system-ui, -apple-system, "Segoe UI", Roboto, sans-serif`. Monospace:
`ui-monospace, "SF Mono", Menlo, Consolas, monospace`. Five sizes, two weights:

| Role | Size / line | Weight | Example |
|---|---|---|---|
| Page title | 24/32 | 600 | "Test Artist – Test Album" |
| Section heading | 18/24 | 600 | "Recordings, side A" |
| Body, tables, labels | 15/22 | 400 (labels 600) | finding texts |
| Meta, help, chips | 13/18 | 400 | "recorded 2026-01-15 14:52 · Turntable A · 24 bit / 48 kHz" |
| Micro (axes, legend) | 12/16 | 400 | "third-octave bands 20 Hz – 20 kHz" |
| Key figure (deck, grade header) | 40/44 | 600, `tabular-nums` | "23:41", "VG+", "12.4 /min" |

All numbers use `font-variant-numeric: tabular-nums`. Technical values (hash, path, device `hw:CARD=CODEC,DEV=0`,
versions) are monospace 13. Units are separated by a space, and negative values use `−`.

### Colours

Tokens for dark (main mood) and light:

| Token | Dark | Light | Role |
|---|---|---|---|
| `--bg` | `#141516` | `#eef0f1` | page background (black / light grey) |
| `--card` | `#202224` | `#ffffff` | cards, dialogs (anthracite / white) |
| `--ink` | `#e9eaeb` | `#1a1b1c` | text |
| `--muted` | `#9da1a5` | `#5f6468` | meta and help text (AA on bg and card) |
| `--line` | `#34373a` | `#d5d8db` | borders, dividers, chart grid |
| `--accent` | `#e05a48` | `#b7382c` | **primary action and recording state**, nothing else |
| `--focus` | `#7fb3ff` | `#1a5fb4` | focus ring, 2 px with 2 px offset |
| `--ok` / `--warn` / `--bad` | `#58c08a` / `#e3b45c` / `#e05a48` | `#2a7a4c` / `#8f6210` / `#b7382c` | verdicts ok / note / problem |
| `--info` | `#7fb3ff` | `#1a5fb4` | notes without a verdict |
| `--left` / `--right` | `#6fa9e8` / `#bf8de8` | `#2255a4` / `#7640b0` | channel left / right |
| `--quiet` | `rgba(88,192,138,.12)` | `rgba(42,122,76,.10)` | quiet passages on the time axis |
| `--pop` / `--click` / `--tick` | `#e05a48` / `#e3b45c` / `#7d8185` | `#b7382c` / `#8f6210` / `#8a8e92` | event kinds (also by shape: pop = diamond, click = line, crackle = dot) |
| `--removed` / `--persistent` / `--new` | `#58c08a` / `#e05a48` / `#7fb3ff` | `#2a7a4c` / `#b7382c` / `#1a5fb4` | comparison results |
| `--wave` | `#86a8d4` | `#3d5a80` | waveforms and level curves |

Neutrals have no colour cast. The right channel is violet, not red, because red already means error and accent.
`--warn` is dark enough for 4.5 : 1 on white. Contrast: text ≥ 4.5 : 1, bars and icons ≥ 3 : 1 against their
background, checked in the design QA.

### Brand

The wordmark "vinyl-check" and the signet (a record with a tonearm, where the red dot is the label) live in
`vinylcheck/static/brand/` in anthracite and inverted versions. They are drawn with the same tool as the
pictograms: monoline, stroke 3 on 24 units, square ends, only lines, circles and arcs. Clear space is the height of
the signet. Minimum height is 20 px for the logo and 16 px for the signet (favicon). See the
[brand README](../vinylcheck/static/brand/README.md).

### Icons

File `icons.svg`, used as `<svg class="i"><use href="icons.svg#i-rec"/></svg>`. 24 px box, 2 px stroke, square ends
and corners (`square` / `miter`), 2 px inner margin, only circle, line, rectangle and 45° diagonal. Transport icons
(record, stop, play) are **filled**, and all others are **outlined**: two recognisable families with the same
geometry. Sizes are 16 (in text), 20 (buttons, tables) and 24 (navigation). Every icon has exactly one meaning:

| Icon | Meaning | Icon | Meaning |
|---|---|---|---|
| `disc` | record, record check | `tonearm` | station, overview, lower the stylus |
| `rec` ● | recording, start recording | `stop` ■ | stop |
| `play` ▶ | listen (to a position) | `listen` | listen live |
| `rew` `prev` / `next` `fwd` | 30 s or 5 s back / forward (always with the time) | `live` | back to live |
| `back` | rewind (with a time label) | `level` | level, level check |
| `analyzer` | 31-band analyzer | `spectro` | spectrogram |
| `click` | clicks, events | `skip` | groove skip |
| `compare` | comparison | `clean` | cleaning |
| `calibrate` | calibration (stylus up) | `sliders` | alignment, turntable check |
| `testrecord` | test record, band | `nut` | settings |
| `search` | search | `cover` | cover, Discogs |
| `ok` `warn` `bad` `info` | verdicts and notes | `clock` | retention, time, jobs |
| `storage` | storage | `wifi` / `offline` | connection / connection lost |
| `sweep` | sweep | `wave` | hum and rumble, master |
| `trash` | delete | `export` | export (WAV, CSV) |
| `close` | close | `chevron` | next, expand |
| `hub` | Vinyl-Mind (stations learning together) | | |

Rules: important actions always have icon **and** word ("■ Stop"). Icon-only buttons are only for close, expand and
play, with `aria-label` and a tooltip. Never two icons for the same function.

### Shapes, lines, radii, shadows, motion

Radius 4 (buttons, fields, chips), 8 (cards, dialogs), 0 (tables, charts). Lines are 1 px `--line`, and focus is 2 px
`--focus`. Shadows only for dialogs and toasts (`0 8px 24px rgba(0,0,0,.18)`). Motion: 120 ms in and out, 200 ms for
layer changes. Meters and the analyzer have ballistics for technical reasons, not decoration.
`prefers-reduced-motion` removes transitions and the meters' decay.

### Data display

One time-axis component for overview, spectrogram and events (same scale, same interaction, same legend: pop ◆ red,
click | yellow, crackle · grey, quiet = `--quiet` area). Bars for comparisons (before and after per figure), lines
for trends (live distortion, frequency response), tables for findings. No pie charts. Grades appear as a chip
`VG+ · 78`, always next to their criterion ("12.4 clicks/min in the music"). Every figure carries its period or
reference ("relative to the 1 kHz reference", "side A, 23:41").

## H. Components

Shared states for all interactive elements: *default · hover (background −4 %) · focus-visible (ring) · active
(background −8 %) · selected (border `--ink`) · disabled (opacity 45 %, with the reason in a tooltip or next to it) ·
loading (16 px spinner on the left, text stays)*. Keyboard: tab order = reading order, Enter and Space activate, Esc
closes, arrows move in tabs, menus and the time axis (±1 s, Shift ±10 s).

| Component | Variants / sizes | Notes |
|---|---|---|
| Button | primary (`--accent`, one per area), secondary (outline), danger (delete and discard only: red text, filled on hover), text link; 36 px / 44 px (at the turntable) | icon and word; long labels do not wrap, the button grows; disabled with a reason |
| Icon button | 36 / 44 px | close, expand, play only; `aria-label` required |
| Text field, number field with unit, text area | 36 / 44 px, unit inside on the right (`g`, `dB`, `h`) | label above, help below, error below with an icon; placeholders only as examples |
| Select / combobox | side, condition, speed; record search (local and Discogs, grouped) | arrows and Enter; results with a 32 px cover |
| Checkbox / radio / toggle | 20 px box; toggle only for instant switches (listening, dark mode) | selection in tables = checkbox column |
| Chip (filter, grade, status) | status (icon and word), filter (removable ×), grade | never colour only |
| Badge | on tabs and buttons ("Compare (2)") | |
| Status line (deck) | ready / level check / recording / analysing / error | word, time and icon; recording in `--accent` |
| Level meter | L/R, RMS, peak and hold, scale −60 … 0, clip mark | `role="meter"`, number next to it |
| Analyzer | 31 bands, L/R, ballistics | text alternative: "loudest band 125 Hz −32 dB" |
| Finding row | status chip · figure · value with unit · target · text · ▶ band | sortable by status, expandable for raw values |
| Time axis | 160 px high; zoom, click to listen; markers | one component, three data sources |
| Table | sorting, fixed first column, row actions on the right | on S: cards with three key values |
| Tooltip | 13 px, 300 ms, also on focus | never the only source of information |
| Dropdown menu | row actions ("…") | arrows, Esc |
| Tabs | areas at the top; sub-tabs in a recording (time axis / spectrogram / events / details) | `role="tablist"` |
| Accordion | *Details* (raw values, paths, hash) | closed by default |
| Pagination | event table | "Showing 1–50 of 412" |
| Breadcrumb | from depth 2 | |
| Toast | success and error 4 s; errors with *Details* stay | top right, at most one |
| Banner | connection, storage, calibration, demo mode, analysis running, new version | below the header, with an action |
| Dialog | 480 px, full screen on S; focus moves in and back | primary on the right, cancel on the left |
| Drawer | recording details on M and S instead of two columns | |
| Card | record, recording, run, deck | no shadows |
| Empty state | 24 px icon, sentence and action | see K |
| Skeleton | tables and charts loading longer than 300 ms | |
| Progress | analysis (estimate, progress where known), upload | with cancel where possible |

Long text: titles wrap to at most two lines, then "…" with the full text in a tooltip and the detail view; table
cells with titles do the same; numbers never wrap. Small screens: full-width buttons in dialogs, tables become cards,
time axes stay full width with zoom, and the spectrogram is shown from M up (on S a note and a link).

## I. Key screens

**1 · Deck (transport, persistent).** Purpose: the state of the station at a glance and one action. Hierarchy:
state and time (large) → level → warning line → buttons. Ready: `ready · Turntable A · calibrated (−91 dBFS) ·
2.1 GB free`, primary *Record side* (naming the chosen record, or "choose a record"), secondary *Check level*,
*Live measurement*, *Calibration*. Recording: `● Recording · uncleaned · Side A · 12:34` (40 px), wide meters, a
warning line ("0 clipped · 0 dropouts"), primary *■ Stop*, secondary *Listen* (with −30 s, −5 s, Live) and
*Analyzer* (open/close). Error: a red line with the reason and *Cancel and restart*. Never empty. On S: two rows
(state and time above the meters), buttons as a bar at the bottom within thumb reach.

**2 · Overview.** Purpose: "Is everything ready, what happened last, what now?" Cards: *Chain* (device, format,
calibration with date, storage, retention, each line with a verdict) · *Recent* (last five recordings: record, side,
condition, grade, date, open) · *Next up* (context-dependent: "Record side B of …", "View comparison 15 ↔ 16",
"Run 5 is missing side 2"). Primary: the first *next up* action. Empty state on first use: "No recording yet.
1 · Calibrate 2 · Add a record 3 · Record a side" as a list with buttons.

**4 · Record.** Header: 96 px cover, artist – title (24 px), condition in the collection and measured grade per copy,
primary *Record side*. Table *Recordings*: side · copy · condition · date · duration · grade · clicks/min · status ·
actions (open, compare, …). Selecting two rows enables *Compare (2)*. Filters: side and condition as chips. Status:
"analysed", "waiting (2nd)", "error: recording shorter than 3 s". Empty: "No recording. Record side A uncleaned."
On S: one card per recording with side, condition, grade and date.

**5 · Recording report.** Grade header: grade `VG+ · 78` with its criterion · clicks per minute (music and quiet
passages) · noise · skips · warnings (clipping, dropouts, mono, live album). Then the time axis (overview with
events, quiet areas, skip markers), spectrogram, events (time, kind, strength, level, channel, region, ▶; filters by
kind, region and minimum strength) and details (device, format, duration, SHA-256, path, audio deleted on …,
analysis version). Primary: *Compare with …* (*proposal*: preselect the last other recording of the same side).
Secondary: listen, export, delete (danger, in the menu). Failed analysis: the header shows the reason and *Analyse
again*. Audio deleted after retention: ▶ disabled with the date, measurements stay.

**6 · Comparison.** Header: A vs. B (condition, date, cleaning) · delta table (figure, A, B, Δ, repeatability, verdict
"real / within scatter") · common time axis (removed green, new blue, persistent red; Shift+click plays B) · skips.
Empty: only one recording → "A second recording of the same side is needed."

**8 · Alignment run.** Header: settings as chips (`3.01 g · anti-skating 3 · no weight · Turntable A`), sides with
status (side 1 ✓, side 2 missing → primary *Record side 2*). Findings sorted problem → note → ok, columns status ·
figure · value · target · explanation · ▶ band. Below: *bands* (time, type, channel image, role, level; unassigned
ones grey with the reason). Comparison with the previous run as an optional Δ column. Empty: "No side recorded yet."

**10 · Live measurement.** Expanded deck: on the left, large distortion figures L / R (40 px, with the distance to
the 3 % threshold as text), the test tone (`315 Hz · −18.2 dBFS`) and the channel image; on the right, 60 s curves
(distortion, level, frequency) and the analyzer. "No test tone" only after 2 s. Responsive: figures first, curves
below.

**11 · Settings.** Two columns, label · value with explanation and default. Values that live in the configuration
file (device, format, channels, paths) are shown read-only, with the path of the file and what to do after changing
it, because the sandboxed service cannot write there.

## J. Accessibility checklist (WCAG 2.2 AA)

- [ ] Every action by keyboard: deck buttons, table actions, the time axis (arrows ±1 s / ±10 s, Enter = listen),
      dialogs (Esc, focus trap, focus return).
- [ ] Focus ring 2 px `--focus` with 2 px offset on everything interactive, never `outline: none` without a
      replacement.
- [ ] Contrast ≥ 4.5 : 1 for text (including `--muted` and `--warn`), ≥ 3 : 1 for icons, bars, borders and focus,
      checked in both themes.
- [ ] Status never by colour alone: chips with icon and word, channels labelled "L" and "R", event kinds with shapes.
- [ ] Meters and the analyzer with `role="meter"` / `aria-valuenow` or a text alternative; time axes with a summary
      text and the event table as an equivalent view.
- [ ] Every form field with a `<label>`, errors via `aria-describedby`, required fields marked in the label.
- [ ] Live changes (recording state, automatic stop, analysis finished) in an `aria-live="polite"` region, errors
      `assertive`.
- [ ] Targets at least 24 × 24 px everywhere, at least 44 px in the deck and on M and S.
- [ ] 200 % zoom without horizontal scrolling (except tables in a scroll container), text sizes in `rem`.
- [ ] `prefers-reduced-motion`: no transitions, meters without decay, analyzer in 1 s steps.
- [ ] The `lang` attribute matches the interface language. Abbreviations explained on first use (THD, IMD, dBFS in
      a tooltip or glossary).
- [ ] Icon-only buttons with `aria-label`; tooltips also on focus.

## K. Edge cases

| Case | Behaviour |
|---|---|
| First use | the overview leads: calibrate → record → side; the deck's primary action is disabled with the reason |
| No calibration | banner with a button; recording allowed, noise verdict "without reference" |
| No Discogs match / Discogs unreachable | local matches first; "Discogs not reachable" as a note; manual creation still possible |
| Clipping or dropouts while recording | warning line in the deck at once, with a number; the result carries the warning |
| Platter stop, stylus skips, wrong side | as above; *proposal*: mark the recording as discarded (stays visible, greyed out) |
| Automatic stop fails or stops too early | the reason line shows what was detected; manual stop always possible; switch in the settings |
| Connection lost | banner "Connection to the station lost · for 12 s · recording continues", deck frozen with a timestamp, listening stops with a note |
| Low storage / data drive missing | banner with the number, start blocked with the reason ("1.2 GB free, minimum 2 GB") |
| Mono recording, wrong format | warning in the findings; figures that cannot be measured say "not measurable: …" instead of staying empty |
| Live album, pedal tones | a note instead of a misleading figure: "quiet passages contain audience" |
| 45 rpm, 10" / 7", box sets | speed in the form; free side names (C1, D2); radius estimate marked as an estimate |
| Very long titles or artists | two lines, then "…", tooltip and detail view; flexible title column |
| Audio deleted after retention | ▶ disabled with the date, spectrogram from the database stays, listening not available |
| Analysis error / queue | status "waiting (2nd)", "error: …" with *retry* |
| Two browsers at once | both see the same state; start and stop from either |
| Invalid address | "Record 999 does not exist (deleted?)" and back to the list |
| Demo mode (file instead of turntable) | a permanent yellow banner |

## L. Design QA (measurable)

1. Every view has exactly **one** primary button (`.primary`), counted by a DOM test.
2. Every status display consists of icon and word (no `.pill` without text, no coloured dot without a label).
3. Contrast of all text tokens ≥ 4.5 : 1 and of all icons and bars ≥ 3 : 1 in both themes (script over the token
   table).
4. All numbers in tables and the deck use `tabular-nums`; technical values are monospace (sample check).
5. Only values of the spacing scale in `margin`, `padding` and `gap` (lint: multiples of 4).
6. At most five font sizes plus the key-figure size in the CSS; no inline `font-size`.
7. No function by hover only; every row action is reachable by keyboard (tab through).
8. Targets: deck ≥ 44 px, otherwise ≥ 24 px (measured in the DOM).
9. Every table has an empty state with reason and action; every list a skeleton from 300 ms.
10. Every figure in the grade header has its unit and reference in the same element.
11. Icons only from `icons.svg`, and every `<use>` points to a defined ID (lint).
12. `prefers-reduced-motion` disables all transitions (CSS test).
13. Lighthouse accessibility ≥ 95 on overview, record, recording report and run.
14. First render without any network request except to the station (network log).
15. No user-visible text outside the translation catalogs (test: `tests/test_i18n.py`), and layouts checked with
    the longest language.

## Risks

1. **Canvas charts stay pictures.** Without text alternatives and keyboard access, half of the information is
   mouse-only. Countermeasure: build the time axis as one component with an `aria` summary, arrow keys and the
   event table as an equivalent view before adding further charts.
2. **The deck gets overloaded** (analyzer, listening, live measurement, warnings). Countermeasure, a hard rule: the
   bar only holds state, time, level, the warning line and buttons. Everything else is expandable and closed by
   default on M and S.
3. **Translations break layouts.** German words are long, and other languages may be longer. Countermeasure:
   buttons grow instead of truncating, labels wrap, and every new screen is checked in the longest language.

## Implementation status

Done:

- Tokens and the icon sprite in `style.css` and `icons.svg`; channel right in violet; focus ring;
  `prefers-reduced-motion`; tabular numbers.
- Logo and signet in the header and as favicon.
- Deck with meters, recording state, warnings and transport buttons; listening with rewind; analyzer and live
  measurement.
- Overview with chain, recent recordings and *next up*; System and Settings as separate areas.
- The recording report as its own page with a synchronised time cursor across clicks, level and spectrogram charts.
- Multilingual interface with catalogs.

Open (*proposals*), in order of benefit:

1. Status chips with icon and word everywhere; banners instead of pills for states that need an action.
2. URL state for detail pages and breadcrumbs.
3. Tabs in the recording report; the time axis as one keyboard-operable component.
4. A *Record side* dialog with prefilled values; *Cancel and restart*; discarded recordings.
