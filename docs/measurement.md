# How vinyl-check measures

This document describes the signal processing behind vinyl-check: how a recording is captured, how clicks are
detected and classified, how the grade is computed, how recordings are compared with each other and with a master,
and how the turntable is evaluated with a test record. Thresholds are given with their defaults. Most of them can be
changed in the configuration file (see `config.example.toml`).

Validation figures marked *synthetic* come from the test suite, which builds records with known defects
(`vinylcheck/synth.py`). Figures marked *real* come from recordings of real records and test pressings.

## Contents

1. [Capture](#capture)
2. [Click detection](#click-detection)
3. [Segmentation and metrics](#segmentation-and-metrics)
4. [Second opinion](#second-opinion)
5. [Grading](#grading)
6. [Groove skips](#groove-skips)
7. [Comparing two recordings](#comparing-two-recordings)
8. [Measuring against the master](#measuring-against-the-master)
9. [Learning](#learning)
10. [Technical data, spectrogram, analyzer](#technical-data-spectrogram-analyzer)
11. [Turntable alignment with a test record](#turntable-alignment-with-a-test-record)
12. [Known limitations](#known-limitations)

## Capture

Code: `capture.py`

- **Bit-exact.** Recording uses `arecord` directly on the ALSA `hw:` device: no sound server, no `plughw`, no
  format conversion, no resampling. Supported formats are 16, 24 (3-byte) and 32 bit. Interfaces that only open
  with all channels are recorded in full, and the configured inputs are picked out.
- **Integrity.** A recording counts as complete only after the file and its directory have been synced to disk.
  At the first analysis, the SHA-256 of the WAV file is stored and checked again before every re-analysis and
  comparison.
- **Monitoring.** Level (RMS, peak), clipping and dropouts (`arecord` overruns) are tracked live. A recording with
  dropouts is marked, because it is useless for comparisons.
- **End-groove stop.** When the signal repeats once per revolution (the run-out groove) for three revolutions, at
  least 30 dB below the loudest passage and no earlier than 60 s into the recording, the recording stops by itself.
- **Live measurement.** Five times per second the live meter estimates the fundamental, level and THD+N per
  channel. This drives the alignment live view.

## Click detection

Code: `detect.py`

### Prediction error

Music and groove noise are predictable over a few milliseconds, but a click is not. For every block of 2048 samples
(about 43 ms at 48 kHz) an autoregressive model of order 32 is estimated (autocorrelation method and
Levinson-Durbin). The prediction error, or residual, is small and nearly white for music, and a click appears in it
as a peak. At the first sample, the residual of a click has roughly the click's own amplitude, which gives a usable
estimate of its loudness.

A sample is a candidate when the residual exceeds **8 σ**, where σ is estimated robustly
(σ ≈ 1.4826 × median |e|). This makes the threshold independent of recording level and loudness. The noise level is
estimated three ways, and the highest estimate counts:

| Estimate | Window | Purpose |
|---|---|---|
| per block | ~43 ms, median over 3 blocks | base level |
| centred | ~12 ms | a longer noise burst (hi-hat, snare) raises its own threshold |
| forward | the ~12 ms *after* the event | a click is over after 1–2 ms, while an instrument hit rings on. Without this window, drum hits were counted as clicks. |

Candidates closer than 1.5 ms are merged into one event.

### Stripe check

In a spectrogram, a click is a thin vertical line across the whole band. A cymbal or snare hit is wide and decays,
whereas crackle is thin. Every candidate must therefore:

- rise at least **12 dB** above the median of its ±60 ms surroundings in the high band (above 6 kHz, 2.7 ms
  windows), and
- be at most **5 ms** wide at half height.

On a dense real record, this reduced the detections from about 21,500 to about 2,000 per side and the clicks in the
music from 80 to 36 per minute, while the visible stripes in the spectrogram remained.

**Clipped peaks** (flat runs at the signal's own maximum) are never counted as clicks.

### Classification

| Kind | Rule | Meaning |
|---|---|---|
| **tick** (crackle) | strength below 25 | barely audible on its own, the crackle of a pause in large numbers |
| **click** | strength ≥ 25 (strength = peak / local σ) | a single audible click |
| **pop** | click strength and residual ≥ the pop level | loud enough to be heard through the music |

The pop level is −20 dBFS by default. With an aligned turntable profile it is set relative to the profile's measured
1 kHz reference level, so loud and quiet chains are graded alike.

### Sensitivity

A single control from 0 to 1 replaces the individual thresholds. 0.5 is the calibrated point (8 σ, 12 dB). The
threshold scales with e^(0.5 − s) and the stripe level with 8 dB × (0.5 − s): 0 finds only the obvious
(about 13 σ, 16 dB), and 1 is aggressive (about 4.9 σ, 8 dB). Order of precedence: recording, turntable profile,
default. The setting used is stored with the analysis (`metrics.detect`).

### Validation

| Test (synthetic) | Result |
|---|---|
| quiet passages, clicks from −35 dBFS | 100 % found |
| music with drums, clicks from −25 dBFS | about 90 % found; the rest are hidden in drum hits |
| false alarms on a click-free side | at most one per minute (test guard) |
| pink noise and pure test tones | at most one hit per minute |

## Segmentation and metrics

Code: `analysis.py`

1. **Level curve** in 50 ms steps (20 Hz high-pass against rumble), smoothed with a 0.55 s median against single
   clicks.
2. **Groove.** Anything quieter than the chain noise from calibration + 6 dB (or −75 dBFS without calibration)
   at the start or end counts as "stylus up" and is excluded together with a 0.5 s margin for the touch-down and
   lift-off.
3. **Quiet passages** (lead-in, pauses, run-out): the groove-silence floor is the quietest full second of the groove
   (the minimum of a 1 s sliding maximum). Quiet means at most 8 dB above that floor, or more than 22 dB below the
   music reference, and at least 1 s long. The second rule keeps real pauses when a very quiet lead-in groove sets
   the floor, and quiet music stays music.
4. **Regions** of every event: *lead-in* and *run-out* (outside the evaluated groove, never counted),
   *lead-in groove* and *end groove* (in the groove before the first or after the last note), *music* with the
   track name, and *pause* between two tracks.
5. **Tracks.** With track durations from Discogs (or a tracklist entered via the API), the track borders are laid
   out from the start of the music, scaled to the measured music length (if the sum differs by at most 10 %), and
   each border is moved to the nearest pause (within ±8 s or ±3 % of the track length). Without complete durations
   there is no track table, because no borders are better than wrong ones.

**Metrics**

| Metric | Definition |
|---|---|
| clicks per minute, quiet | clicks and pops in quiet passages, per minute of quiet passage (needs at least 6 s) |
| crackle per minute, quiet | ticks in quiet passages |
| clicks per minute, music | clicks and pops in the music, per minute of music |
| pops per minute | pops in the music, per minute of music |
| noise | level of the quiet passages in dBFS, A-weighted, relative to the music and relative to the chain |
| scratches | clicks that repeat at the revolution period (±0.6 %) for at least 4 revolutions. A scratch across several grooves shows up as a chain, and dust does not. |
| clipping | clipped samples in the groove, excluding 5 s after touch-down and before lift-off and everything after the end of the music. Mentioned from 200 samples, no grade from 500. |

**Click floor.** During alignment, the detector counts clicks and crackle in the silent groove of the test record.
This is what the chain itself contributes (stylus, cartridge, static, dust on the test record). On real hardware,
an entry-level USB chain produced several hundred hits per minute there, and a high-end chain a few dozen. Quiet-passage
figures are also reported above this floor (raw minus floor, never negative), so that two turntables become
comparable.

## Second opinion

Code: `second_opinion.py`, weights in `vinylcheck/weights/` (MIT, from [GrooveClean](https://github.com/Booyaka101/grooveclean))

A small 1-D convolutional network (32 channels, 8 dilated blocks, receptive field 2,041 samples at 44.1 kHz),
trained on synthetic clicks in clean music, gives a click probability per sample. As the only detector it does not
work for vinyl-check: on a real record it reported over a thousand positions per minute, and 10–14 per minute in
pink noise. As a second opinion on vinyl-check's own candidates, it is useful:

- It only runs on windows of ±150 ms around clicks and pops (crackle is not sent), merged where they overlap,
  resampled to 44.1 kHz, blockwise.
- Per candidate, the maximum within ±2.7 ms over both channels counts. From **0.55** a click is *confirmed*.
- Results are cached by file checksum, so re-analysing the same recording only evaluates new candidates.
- It needs PyTorch (`pip install ".[ml]"`). Without it, the second opinion is silently unavailable, and
  `[analysis] second_opinion = false` switches it off.

The network is a hint, not a judge: the grade uses confirmed clicks when available, but the network also confirms
drum hits with a click-like attack.

## Grading

Code: `grading.py`

Each metric has five upper limits (NM, VG+, VG, G+, G), and above the last one it is F. Within a grade, a score is
interpolated linearly:

| Grade | NM | VG+ | VG | G+ | G | F |
|---|---|---|---|---|---|---|
| Score | 100–90 | 90–75 | 75–55 | 55–35 | 35–15 | 15–0 |

**What counts** (default, grading scheme 5):

| Metric | NM | VG+ | VG | G+ | G | Weight |
|---|---|---|---|---|---|---|
| clicks per minute in the music (confirmed by the second opinion if available) | 8 | 15 | 30 | 50 | 100 | 90 % |
| pops per minute in the music | 2 | 5 | 10 | 20 | 40 | 10 % |

The side's score is the weighted mean of both scores, and the grade follows from the score bands. Rules:

- **Music only.** Clicks and crackle in pauses and noise relative to the music get their own grades for information
  but do not enter the grade. What matters is what you hear while the music plays. Noise depends heavily on the chain:
  the same silent groove measured −58 dBFS on one chain and −64 dBFS on another.
- **Weighting 90 / 10.** In loud music, most detected pops are instrument transients. Against the master, 77 % of
  the pops in the music of a loud rock record were music, but only 14 % of the clicks. Equal weighting would let
  drum hits dominate the grade.
- **F only for skips.** Goldmine F means "damaged, skips". A side that plays through ends at G at worst. When the
  stylus jumps back, the side is F with score 0.
- **No grade** when more than 500 samples in the groove are clipped: that is an error of the recording.
- **Mint** is never given, because it needs a visual inspection.
- The **record grade** of a copy is the mean of the grade steps of its sides (latest graded recording per side),
  with half steps rounded towards the worse grade (NM + VG = VG+, NM + VG+ = VG+). One skipping side makes the record F.

**Calibration.** The limits are starting values (`calibrated = false`). They were adjusted so that records judged by
ear landed in the expected grade, and so that dust that no cleaning removes completely does not push a good record
below NM. To calibrate for your chain, measure a few records whose condition you know by ear, adjust `[grading]` in
the configuration and set `calibrated = true`. `weights`, `graded` and `secondary` select which metrics count and
how. When the grading scheme changes, the service regrades all stored analyses at startup from the stored events,
without touching the audio.

## Groove skips

Code: `skips.py`

When the stylus jumps back one groove, exactly one revolution repeats (1.80 s at 33⅓ rpm, 1.33 s at 45 rpm), and it
repeats sample-exact including groove noise, because it is the same groove. Music that repeats (a chorus or a loop)
only sounds the same. Its noise differs.

- **GCC-PHAT** (cross-correlation in which every frequency counts equally, phase only): 0.5 s windows every 0.25 s,
  each compared with the window one and two revolutions earlier (±2 % speed), band 300 Hz–7 kHz, at 16 kHz. A real
  repetition gives a sharp peak near 1. Sustained tones match in only a few bins, so pedal tones and pads do not
  trigger it.
- A **back skip** needs a similarity of at least 0.40 and a period of exactly one revolution (±0.6 %). Weaker hits
  are reported as *unsure* and usually come from rhythm.
- A repetition that lasts until the stylus is lifted is the **end groove**, which is normal. The repetition of the
  run-out groove after the end of the music is never a skip.
- **Forward skips** leave no reliable trace in a single recording and are only found by comparing two recordings of
  the same side.
- Each skip carries its time, position on the side and track, the estimated radius (12" LP, 146 → 65 mm, even
  groove pitch) and the measured speed.

Validation (synthetic): skips found within ±0.4 s, also at +0.8 % speed, with no false alarms on music and pedal
tones. Limitation: a sample-exact loop in the music that is exactly one revolution long (a bar at about 133 BPM)
looks like a skip. With many repetitions, the app asks you to listen.

## Comparing two recordings

Code: `compare.py`

1. **Coarse alignment** by cross-correlating the level curves (10 ms grid), which gives the start offset.
2. **Fine alignment** in two passes: first 0.3 s windows in a low band give speed and offset. Then the second
   recording is resampled onto the time axis of the first and anchored sample-accurately with 1 s windows. Without
   this resampling, even a 0.2 % speed difference smears the window, and the alignment was 50 ms off in the first
   attempt.
3. **Matching.** Every event of the first recording is mapped into the second and searched within ±4 ms:
   *persistent* (in both), *removed* (only before), *new* (only after). Events outside the common groove range do
   not count.
4. **Skips.** If one recording skips, the mapping shifts by one revolution after that point. This is reported, and
   matches after it are marked uncertain. Skips at the same place in both recordings point to the record itself.

Only recordings of the same side of the same record are compared. If fewer than three anchors are found, the result
is *not comparable* with a reason, and no differences are reported.

Validation (synthetic: second recording 0.2 % faster and started 1.3 s later): offset and speed found exactly,
84 % of scratches classified *persistent* and 77 % of dirt *removed*. The rest is hidden in drum hits, where the
click is not found in both recordings.

## Measuring against the master

Code: `master.py` (engineer mode)

With the digital master of a side, vinyl-check measures what is otherwise only estimated.

| Measurement | Method |
|---|---|
| Alignment | a variant of the comparison alignment: 0.3 s windows every 2 s (100 Hz–1.5 kHz), parabolic peak refinement, robust line fit, mapping smoothed over ±15 s. The 1 s windows of the plain comparison smear at ±0.4 % wow. |
| Polarity | after aligning on magnitude, the sign of the correlation per channel at the anchors; per channel if the mid signal does not match (one inverted channel cancels the mid) |
| Wrong side | if no polarity case matches, the recording is checked against the masters of the other sides |
| Speed | slope of the alignment, minus the turntable's own speed from its alignment = error of cut and pressing |
| Wow per revolution, eccentricity | time offset every 50 ms over the music (0.1 s windows, 150 Hz–4 kHz, outliers rejected). In three 24 s sections, a sinusoid at the revolution frequency (±3 %) plus a quadratic trend is fitted. An off-centre hole changes the groove speed by ±e/r, so the wow grows towards the inside: e ≈ wow × radius (radius estimated from the position on the side). |
| Channels | correlation of mid (L+R) and side (L−R) against the master, 200 Hz–3 kHz; balance against the master |
| Frequency response | third-octave bands per third of the side from loud frames (8192 points), groove noise from quiet passages subtracted, frequency axis corrected for speed, referenced to 1 kHz; side signal separately (a falling side signal in the bass means the bass was cut in mono, falling highs at the end are inner-groove loss) |
| Level per track | 20 Hz high-pass, RMS per track, difference to the master relative to the median |
| Clicks only on vinyl | for each event, the master is resampled (sinc) onto the recording's time axis around the event, a 16-tap FIR is fitted around it (excluding the event) and subtracted. A click or tick is on the vinyl if the remainder is ≥ 12 × the residual floor or ≥ 50 % of the peak remains. A **pop** only counts if ≥ 50 % remains, because at loud transients the tracking distortion leaves a sharp residue. |
| Grade against the master | the same grading, counting only clicks and pops in the music that the master does not explain |
| Pauses | master silent = 45 dB below its 95th percentile for at least 0.4 s. Noise is the mean of the quietest 40 % of 0.1 s blocks, also A-weighted. Pre- and post-echo: the loudest 0.5 s music window next to the pause, against the pause one revolution away (300 Hz–6 kHz), found from \|correlation\| 0.12 at exactly one revolution (±4 ms). |

Calibration (real): on a test pressing, pure music transients had a residual-to-floor ratio of 3.8 (median, 90 %
below 8.9), and reported clicks had 24.

What the master showed about false alarms (real, 1,158 detections on two sides of a loudly mastered rock record):

| Kind (in the music) | Detections | Explained by the master |
|---|---|---|
| pop | 108 | 83 (77 %) |
| click | 113 | 16 (14 %) |
| crackle | 568 | 198 (35 %) |

- The second opinion does not separate these. Even at a network value of 95 % or more, 42 % of pops and clicks were
  music.
- For crackle, the high-frequency peak above its surroundings (2 kHz high-pass, ±30 ms) separates well. From
  20 dB, 4 % were false alarms, at 10–15 dB 48 %, and below 10 dB 75 %.
- Validation (synthetic, `tests/test_master.py`): a pressing with +0.4 % speed, 0.25 mm eccentricity, −3 dB highs,
  one track −1 dB, noise at −62 dBFS, clicks, −30 dB pre-echo, inverted polarity, swapped channels and a wrong side.

Limits: the frequency response against the master includes the turntable (cartridge, phono stage). The reliable
parts are the change across the side and the comparison of copies on the same turntable. The radius used for
eccentricity is estimated.

## Learning

Code: `learning.py`

Every master comparison classifies every detection as *on the vinyl* (real) or *in the master* (music, false alarm).
Manual judgements add to this: *click* is real, *false alarm* is music, and *unsure* is left out. Manual judgements
weigh five times as much as master classifications and take precedence.

- **Model.** One logistic regression per kind (click, crackle, pop) on features the analysis already stores:
  strength, level relative to the recording's music reference, duration, network value, region, both channels and
  the density of detections within ±0.5 s, plus two curvature terms. Pure numpy, trained in fractions of a second.
- **Estimate, don't filter.** Individual detections are not separated reliably enough to drop them. For crackle,
  at best about 80 % of what would be dropped is really music. The probabilities are well calibrated, though, and
  their sum estimates the number of real clicks. Manual judgements count as fixed values (click and unsure 1,
  false alarm 0).
- **Cross-validation per recording.** Each recording with a master is left out once and predicted from the rest. A
  kind is *usable* when its estimate is off by at most 30 % and better than the raw count. That needs at least two
  recordings with a master and at least ten examples per class.

| Kind (music, real: ten recordings of one record with master) | Raw count off by | Estimate off by |
|---|---|---|
| clicks | +15 % | ±4 % |
| crackle | +52 % | ±5 % |
| pops | +208 % | ±23 % |

The estimate appears next to the raw figures (*≈ estimated*, *real?* per detection). The grade uses the raw count by
default. Computing it from the estimate is a setting, best enabled only once records of different genres and
pressings with a master are included. A background thread checks every 30 s for new judgements or master comparisons,
retrains, and recounts analyses made with an older model.

## Technical data, spectrogram, analyzer

**Technical data** (`technical.py`), computed only over the groove and blockwise so that a 30-minute side fits in
little memory: peak per channel, true peak (4× oversampled), RMS, crest factor, loudness per ITU-R BS.1770-4
(K-weighting, 400 ms blocks, absolute and relative gate; a −20 dBFS sine on both channels reads −20.7 LUFS), DC
offset, correlation and stereo width in the music, tonal balance in five bands and spectral centroid, rumble and
mains hum (50/100 Hz prominence) in quiet passages, noise, chain noise, clipping with position, speed from the
profile, and recording format.

**Spectrogram** (`spectro.py`): time against log frequency (20 Hz–16 kHz) as an 8-bit image, computed directly at
the display size and read blockwise. The overview of the whole side (1600 × 160) is computed after the analysis and
stored in the database, so it stays available after the audio has been deleted. Images are computed with a 100 dB
range and displayed with 80 dB. Because the image is linear in dB, a stored image can be rescaled to another
recording's colour scale exactly. Comparisons use this to show both recordings with the same scale.

**Analyzer:** 31 third-octave bands per ISO 266, band power summed, full scale = 0 dB, with ballistics (instant
attack, 15 dB/s release, 6 dB/s peak hold). On the pink-noise band of the test record it reads flat within ±1 dB
from 250 Hz to 8 kHz.

## Turntable alignment with a test record

Code: `alignment.py`, `measure.py`

### Finding the bands

The test record is recorded side by side like a normal record. The analysis splits the recording into bands **by
the signal**, because band lengths are printed on no sleeve:

1. A 0.1 s grid with power of L, R, mid and side, peak frequency and tonality.
2. Active means a level (from 30 Hz up) at most 40 dB below the loudest band. Warp signals at 10–17 Hz do not count.
   Falling sweeps (bass, resonance down to 10 Hz) are followed below 30 Hz along their frequency trajectory.
3. Active sections are further split at changes of the channel image and at frequency jumps (not within sweeps).
   Noise bands are not split by channel image, because warps tilt it at times.
4. A pause of at least 30 s that is not an end groove is the silent groove. "Stylus up" at the start or end counts
   only if it is at least 8 dB quieter than the pauses between bands.
5. **Classification:** tone, sweep (Spearman correlation > 0.9 over at least half an octave), level ramp (more than
   4 dB rise), two-tone, noise. Channel image: lateral (L = R), vertical (L = −R), left or right only (more than
   12 dB difference).
6. **Assignment** by these features, not by position, following the band layout of the Analogue Productions
   AAPT 1. Spoken announcements between bands end up as *not assigned*.

### Measurements

| Side | Band | Measurement | Default target |
|---|---|---|---|
| 1 | 1 kHz lateral, 0 dB | channel balance L − R, reference level for level calibration | ±1 dB |
| 1 | 1 kHz left only / right only | crosstalk in each direction, their difference = azimuth | ≤ −25 dB; difference ≤ 3 dB |
| 1 | 1 kHz and 10 kHz at −20 dB | high frequencies, 10 kHz against 1 kHz | ±2 dB |
| 1 | sweeps 1 → 20 kHz (−20 dB) and 1 kHz → 20 Hz (0 dB) | frequency response per channel, window matched to the instantaneous frequency. Highs are evaluated up to 16 kHz: the 18 kHz step was 8–10 dB lower on the pressing measured, on every turntable. | ±2 dB |
| 1 | 100 Hz, 0 dB | bass against 1 kHz (RIAA accuracy) | ±1 dB |
| 1 | 60 Hz + 4 kHz (4:1) | intermodulation per SMPTE, the median over 2 s blocks → VTA, lowest value wins | ≤ 3 % |
| 1 | 3150 Hz | speed, weighted wow & flutter (IEC 60386, 2σ), eccentricity (once per revolution), drive without eccentricity | ±0.5 %; ≤ 0.1 % |
| 2 | 315 Hz ramp, 0 → +12 dB | distortion per channel over level → **anti-skating** and **tracking reserve** | both clean up to +12 dB, or both break up together |
| 2 | pink noise, lateral and vertical | broadband balance only | — |
| 2 | 1 kHz vertical | mono cancellation (L+R against L−R) | ≤ −20 dB |
| 2 | sweep 1 kHz → 10 Hz vertical (−20 dB) | **arm/cartridge resonance**: second-order high-pass model, grid search over f0 and Q | 8–12 Hz |
| 2 | silent groove | rumble 20–100 Hz (unweighted, relative to the reference, mains hum removed and reported separately), low-frequency peak (warps), noise (A-weighted), discrete lines, click floor | ≤ −55 dB |

**Anti-skating.** The ramp is measured from the start of the tone, excluding the touch-down. For each channel, the
break-up point is the first level at which the distortion of the harmonics (THD, not THD+N, so hum and noise do not
count) stays above 3 % for three grid steps, or rises 6 dB above the trend of the lower half of the ramp (and above
1 %). Tracking distortion grows with amplitude, so the trend must rise by at least 1 dB per dB. Left first means too
much anti-skating (the stylus leans on the outer, right wall). Right first means too little. Both within 1 dB means
the anti-skating is balanced, and if the reserve is then still limited, the tracking force is the next suspect.
Levels are relative to the start of the ramp in each channel, so gain and balance do not matter.

**Building blocks** (`measure.py`): 4-term Blackman-Harris spectra (side lobes −92 dB, distortion measurable down to
about 0.003 %), levels as sine amplitude relative to full scale (0 dB = full-scale sine, as in AES17), THD
(harmonics 2–9) and THD+N (everything except the fundamental, 20 Hz–20 kHz), instantaneous frequency from the
analytic signal.

**Targets** are evaluated when a finding is displayed, against the chosen target preset, not at analysis time. The
anti-skating judgement itself (break-up per channel) uses the analysis parameter `thd_limit_pct`.

### Validation

- **Synthetic** (a simulated AAPT 1 with built-in errors, `synth.make_test_side`): balance ±0.1 dB, crosstalk
  ±0.5 dB, speed ±0.02 %, eccentricity and drive variation ±0.01 %, IMD ±0.2 %, resonance ±0.5 Hz and Q ±0.3 at 7,
  11 and 14 Hz, anti-skating direction correct in all four cases, break-up 0–2 dB above the built-in limit.
- **Real:** all 16 bands of the AAPT 1 recognised on different turntables. Speed, high-frequency sweep, balance, IMD
  and bass repeat within ±0.1 dB or ±0.001 % between runs. An azimuth correction between two runs showed up as the
  expected change in crosstalk asymmetry (3.3 → 1.2 dB).

**Rumble** is measured unweighted from 20 to 100 Hz relative to the reference tone. Lab figures are usually
DIN-B weighted and cannot be compared directly. The default target of −55 dB is strict for this method, so use a
target preset that suits your drive.

## Known limitations

- **Grade limits are starting values**, set from a limited number of records judged by ear. Calibrate them for your
  chain.
- **Loud music.** About 10 % of clicks in loud music are missed, and some loud instrument transients are reported as
  pops. The master comparison, your judgements and the learned estimate address this.
- **Detector against the ear.** In one calibration on a test pressing, an audible click reported by the mastering
  engineer was found only weakly and only on one of five copies. Such cases are calibration points for the detector.
- **Live albums.** Quiet passages contain audience and hall, so noise relative to the music is not meaningful. This
  is one reason noise is not part of the grade.
- **Skips** in sample-exact loops one revolution long can be false alarms.
- **Radius estimates** assume a 12" LP with even groove pitch.
- **Alignment** currently covers the AAPT 1 band layout at 33⅓ rpm. Tracking distortion over the radius and lateral
  resonance need further test records.
