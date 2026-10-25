# Setup guide

vinyl-check measures through your turntable. Every click it counts and every grade it suggests passes through the
stylus, the cartridge, the phono stage and the converter first. A record measured on a well-aligned budget USB
turntable gives repeatable, meaningful results. The same record on an expensive but misaligned deck does not:
mistracking and inner-groove distortion look like crackle, a tilted cartridge looks like a channel problem, and a
slipping belt looks like a bad pressing.

So the setup comes first, and following it matters more than the price of the equipment. This guide suggests three
chains, from entry level to high end, explains the alignment basics, shows how vinyl-check's alignment mode checks
them with a test record, and ends with a checklist for every session.

Product names below are examples, not recommendations or requirements.

## Contents

1. [The measurement chain](#the-measurement-chain)
2. [Entry level: USB turntable](#1-entry-level-usb-turntable)
3. [Recommended: turntable, phono preamp and audio interface](#2-recommended-turntable-phono-preamp-and-audio-interface)
4. [High end](#3-high-end)
5. [Example setups](#example-setups)
6. [Recording on macOS and Windows](#recording-on-macos-and-windows)
7. [Turntable alignment basics](#turntable-alignment-basics)
8. [Alignment with vinyl-check and a test record](#alignment-with-vinyl-check-and-a-test-record)
9. [Level calibration](#level-calibration)
10. [Your first recording](#your-first-recording)
11. [Checklist before every session](#checklist-before-every-session)

## The measurement chain

```
record → stylus and cartridge → tonearm → phono stage (RIAA) → A/D converter → vinyl-check
```

| Link | What goes wrong | How it shows up in vinyl-check |
|---|---|---|
| Stylus | dirt, wear | crackle everywhere, distortion on loud passages |
| Cartridge and arm | tracking force, anti-skating, VTA, azimuth, overhang | one channel distorts first, crackle on loud music, poor crosstalk |
| Drive | speed, belt slip, motor noise | speed and wow & flutter findings, rumble, lines in the silent groove |
| Phono stage | gain, loading, hum, subsonic filter | level warnings, mains hum, a distorted resonance curve |
| Converter | clipping, 16-bit noise, dropouts | clipping warnings, a high chain noise floor, a "dropout" mark on the recording |

vinyl-check records bit-exact from the audio device: no resampling, and on Linux no sound server. What the converter
delivers is what gets measured. On macOS and Windows the same rules apply through PortAudio, see
[recording on macOS and Windows](#recording-on-macos-and-windows).

## 1. Entry level: USB turntable

A USB turntable has the phono stage and the converter built in and appears as a USB audio device. Examples are the
USB versions of common entry-level decks from Audio-Technica, Sony, Denon or Teac.

**What to check**

- **It is recognised.** On Linux, `arecord -l` lists it (often as "USB Audio CODEC", device
  `hw:CARD=CODEC,DEV=0`). On macOS it appears as "USB Audio CODEC", on Windows as "Microphone (USB Audio CODEC)" or
  "Line (USB Audio CODEC)". In the turntable profile, vinyl-check lists the capture devices it finds.
- **Sample format.** Most USB turntables deliver 16 bit at 44.1 or 48 kHz. Choose 48 kHz if it is offered, under
  *Settings → Audio interface* (which only offers the rates the device reports) or with `rate = 48000` in the
  configuration; on Linux `arecord --dump-hw-params -D hw:CARD=CODEC,DEV=0` lists what the device can do.
  16 bit / 44.1 kHz is always enough to detect clicks and pops; stylus and alignment matter far more than the
  converter.
- **Gain.** Usually fixed. Tick *Level not adjustable on the device* in the turntable profile. vinyl-check then
  skips the advice to change the gain and the warning about the clip when the stylus touches down. Clipping in the
  groove is still reported. Some fixed-gain decks clip a few samples on loud records, and the defaults are tolerant
  of that: clipping in the first and last five seconds is ignored, below 200 samples it only appears in the
  technical data, and only above 500 clipped samples in the groove is there no grade, because then the recording
  itself is faulty.
- **Tracking force and anti-skating.** Use whatever your deck lets you adjust (counterweight scale, anti-skating
  dial). A digital stylus force gauge is cheap and more precise than the counterweight scale.
- **Stylus.** Replace a worn stylus. A conical stylus is fine; a worn one is not.
- **Placement.** A solid, level surface, away from speakers and footfall.

**Limitations**

- A higher noise floor and more detector hits in the silent groove. On real hardware, an entry-level USB chain
  produced several hundred detector hits per minute in the silent groove of the test record, and a high-end chain
  a few dozen. That is the click floor of the chain, not of the record. vinyl-check measures it during alignment and
  stores it in the turntable profile.
- Cartridges in this class often have about 18 dB channel separation. The standard alignment targets (≤ −25 dB
  crosstalk) are too strict for them, so choose a target preset that matches the cartridge's data sheet
  (*Settings → Targets*).
- Grades are most meaningful when you compare recordings made on the same turntable, for example before and after
  cleaning.

## 2. Recommended: turntable, phono preamp and audio interface

This is the reference setup. A separate phono stage and an audio interface with line inputs give adjustable levels,
24-bit resolution and a lower noise floor.

**Phono stage**

- Match it to the cartridge: MM at 47 kΩ with the recommended capacitance, MC with the recommended load.
- Prefer a phono stage without a subsonic filter, or with a switchable one. The resonance measurement sweeps
  down to 10 Hz. A filter that cuts below 20 Hz distorts the result. A chain that is flat to about 20 Hz and −3 dB
  at 5–6 Hz has proven fine.

**Audio interface**

- Use the **line inputs**, not microphone or instrument inputs.
- Record at **24 bit / 48 kHz** (*Settings → Audio interface*, or `sample_format = "S24_3LE"` or `"S32_LE"` and
  `rate = 48000` in the configuration). 48 kHz is the rate
  the detector was tuned at, because its analysis windows are defined in samples. For click and pop detection alone,
  16 bit / 44.1 kHz is always enough; 24 bit adds headroom for setting levels and a lower noise floor for the
  alignment measurements (crosstalk, silent groove). On macOS and Windows, also set the device itself to 24 bit
  ([recording on macOS and Windows](#recording-on-macos-and-windows)).
- 96 kHz works, but files double in size (about 35 MB per minute) and analysis takes longer. Grades are best
  compared at the same rate.
- Choose the pair of inputs the phono stage is connected to under *Settings → Audio interface → Inputs* (for
  example *Inputs 3 / 4*). Some interfaces only record with all their channels open; then also set
  `capture_channels` to their channel count in the configuration, for example `capture_channels = 4` (with
  `input_channels = [1, 2]` for the pair, if you configure everything in the file).

**Levels**

- Set the interface gain so that the 1 kHz reference tone of the test record reads about **−20 dBFS** (see
  [level calibration](#level-calibration)). Loud music then peaks well below 0 dBFS.
- As an example, a cartridge with 6.5 mV output into a phono stage with 38 dB gain delivers about 0.5 V (−3.5 dBu)
  on the reference tone and about 2 V (+8.5 dBu) on the loudest test band.
- After calibration, do not touch the gain again. Note the setting in the alignment run (field *Input level*).

**Grounding and hum**

- Connect the turntable's ground wire to the ground terminal of the phono stage.
- Use balanced cables from the phono stage to the interface where possible, and keep audio cables away from power
  supplies.
- Ground loops (computer, interface and amplifier on different outlets) show up as mains hum at 50/60 Hz and
  100/120 Hz in the technical data and in the silent groove of the test record. A quick check: run the computer on
  battery and see whether the hum goes away.
- On a Raspberry Pi, use the official power supply. Give the interface its own power if the Pi's USB port cannot
  supply it.

## 3. High end

Everything from the recommended setup applies. In addition:

- **Low-noise phono stage and interface.** A good chain calibrates below about −85 dBFS with the stylus raised, and
  an entry-level USB chain calibrates around −70 dBFS. The lower the floor, the more quiet passages can be measured.
- **Fine stylus profiles** (fine line, micro ridge) reward precise azimuth and VTA, and expose small errors.
- **Protractor for overhang and zenith** that matches your arm's geometry (Baerwald, Löfgren or Stevenson).
- **Record weight or clamp.** Without one, warps can put strong subsonic content (10–17 Hz, around −46 dBFS in one
  test) on the recording. vinyl-check ignores it when it looks for test bands, but the stylus still has to follow
  it.
- **Stricter targets.** The *Lab strict* preset uses what measurement labs expect from very good drives. Presets
  with manufacturer data cover individual cartridges.
- **Test pressing approval.** In engineer mode, measure pressings against the digital master of each side
  (WAV, FLAC or AIFF). See the [user guide](user-guide.md#master-comparison-and-test-pressing-approval).

## Example setups

Two common ways in, with complete turntables you can buy today. Each one becomes a good measuring chain once it is
aligned as described below; the "worthwhile upgrade" column names the step that improves the results most for the
money. For detecting clicks and pops, 16 bit / 44.1 kHz is always enough, so the converter specification is not the
deciding factor.

Prices are approximate street prices in Germany (October 2026), specifications are taken from the manufacturers.
Both change; check current data before buying. MicroLine (ML) is a fine-line stylus profile.

### Turntables with a built-in USB audio interface

The simplest chain ([setup 1](#1-entry-level-usb-turntable)): one USB cable to the computer or Raspberry Pi.

| Turntable | Price (approx.) | Cartridge as supplied | USB recording | Worthwhile upgrade | Assessment |
|---|---|---|---|---|---|
| Pro-Ject Debut Carbon RecordMaster HiRes | €651 | Ortofon 2M Red | PCM up to 24 bit / 192 kHz, DSD256; adjustable recording level | 2M Blue stylus | Best built-in digitisation: carbon arm, acrylic platter and by far the best A/D converter of this group. |
| Audio-Technica AT-LP5X | €368–390 | AT-VM95E, elliptical | 16 bit / 44.1 or 48 kHz | VMN95ML stylus, simply plugs on | Best value: solid 7.3 kg direct-drive deck and a very easy path to a really good MicroLine stylus. |
| Audio-Technica AT-LP120XUSB | €268–290 | AT-VM95E, elliptical | 16 bit / 44.1 or 48 kHz | VMN95ML stylus, simply plugs on | Cheapest serious option. Mechanically a little simpler than the LP5X, but with the ML stylus very strong at digitising used records. |
| Pro-Ject Debut RecordMaster II | €298–449 | Ortofon OM5e, elliptical | 16 bit / 48 kHz | OM20 or OM30 stylus | Good belt drive with a heavy metal platter and three speeds; the supplied OM5e is entry level only. |
| Thorens TD 202 | from €444 | Audio-Technica AT95E, elliptical | USB-B; no convincing hi-res specification | complete AT-VM95ML or another ½-inch cartridge | Decent classic hi-fi deck, but compared with the cheaper LP5X no better value, technically or in its cartridge. |
| Dual DTJ 301.1 USB (minimum) | from €229 | AT-3600, conical | USB; no hi-res specification | complete AT-VM95ML | Usable starting point with direct drive and an adjustable S-shaped tonearm. The AT-3600 is the weakest link; the VMN95ML stylus alone does not fit its body. |

### Turntables with a built-in phono preamp, into an audio interface

A compact variant of [setup 2](#2-recommended-turntable-phono-preamp-and-audio-interface): the turntable's line
output goes into the line inputs of an audio interface (for example from MOTU or RME). The interface gives you
adjustable levels and a lower noise floor; the phono stage is built into the turntable.

| Turntable | Price (approx.) | Cartridge as supplied | Drive and tonearm | Connection to the interface | Assessment |
|---|---|---|---|---|---|
| Pro-Ject Primary E Phono | €250–279 | Ortofon OM, spherical entry-level stylus | belt drive, 8.6″ aluminium arm | switch to LINE; RCA → two line inputs | Good simple mechanical basis, but not yet a high-quality cartridge. For serious digitisation, upgrade at least the stylus or the cartridge. |
| Audio-Technica AT-LPW50PB | €310–350 | AT-VM95E, elliptical | belt drive, sensor-controlled motor, carbon tonearm | line out → interface line in | Insider tip. With a VMN95ML stylus it becomes a very strong digitising combination for about €470. |
| Pro-Ject T1 EVO Phono | from €399 | Ortofon OM10, elliptical | belt drive, one-piece aluminium arm, 8 mm glass platter | line out → interface line in | Clearly better equipped than the Primary E; the OM10 is already decent and easy to improve with a better OM stylus. |
| Pro-Ject T2 Super Phono | €569–649 | Sumiko Rainier | 9″ aluminium arm, 1.7 kg glass platter, adjustable anti-skating | high-quality built-in phono preamp → interface line in | Best mid-range all-in-one of this table: drive, cartridge and especially the more elaborate internal phono stage are clearly above the cheaper models. |
| Technics SL-1500C | €1,199 | Ortofon 2M Red | ΔΣ direct drive, 2 kg aluminium platter, height-adjustable S-shaped arm, auto lift | line out → interface line in; phono stage can be bypassed completely | Mechanically the best choice: extremely stable speed, a very good tonearm, open to almost any MM cartridge in the long run. A 2M Blue stylus is the obvious upgrade. |
| Cambridge Audio Alva TT V2 | from €1,499 | Cambridge high-output MC, elliptical | direct drive, POM platter, removable headshell | switchable line out → interface line in | Highest-quality all-in-one out of the box: a good MC cartridge and a very low-noise internal phono stage. For digitising alone clearly more expensive than the T2 Super Phono or the LPW50PB with a MicroLine stylus. |

## Recording on macOS and Windows

On the Raspberry Pi and other Linux systems vinyl-check records with `arecord` straight from the ALSA `hw:` device.
On macOS and Windows it records through PortAudio (the Python package `sounddevice`, installed together with
vinyl-check on these systems), with the same rules: the chosen rate and sample format, no resampling, dropouts
counted and marked. If the device cannot record at the chosen rate without a converter in between, the recording
does not start, and the message says what to change.

**Settings → Audio interface.** This is where the desktop app is set up; no configuration file is needed.

- *Recording device* lists the audio interfaces first, each with its number of inputs and the rate it runs at now,
  for example *Studio Interface · 20 inputs · 96 kHz now*. Microphones (built-in, USB, headsets), phones used as
  microphones and virtual devices (video calls, routing software, Stereo Mix) are listed under *Other inputs*.
  *Automatic* takes an audio interface with at least two inputs (on Windows an ASIO driver first, then a USB
  device) and never one of the other inputs. The device of the active turntable profile, if it has one, takes
  precedence for recordings and level checks.
- *Sample rate* offers the rates the device reports (44.1 to 96 kHz, and 176.4 or 192 kHz where supported). Until
  you save, vinyl-check uses the rate the interface runs at now, so it does not switch the interface under other
  programs. If you choose another rate, the page says that the interface will be switched while recording.
- *Bit depth*: 24 bit for interfaces, 16 bit for devices that only offer 16 bit (most USB turntables).
- *Inputs* lists the pairs of the device (*Inputs 1 / 2* … *Inputs 19 / 20*): choose the pair the phono stage is
  connected to.
- On Windows also *WASAPI exclusive mode* and *Use ASIO drivers* (see below).
- *Test* opens the device with the values in the form for one second, without saving or recording, and shows the
  peak level per input or why the device refused (rate, bit depth, inputs, device busy). *Save* keeps all values
  shown, so later recordings stay in the same format even if another program changes the interface's rate. *Back
  to the configuration file* removes them again.

Changes apply from the next recording or level check, without a restart, and are refused while one runs. The
section works the same on a station; there the configuration file stays the base, and values saved in the
interface take precedence over it.

**In the configuration file.** The device is stored as `<audio system>: <name>`, for example
`Core Audio: USB Audio CODEC` or `ASIO: Studio Interface`; this name stays valid across restarts and re-plugging.
`device` in the configuration takes the same form, or just a part of the name (`device = "Scarlett"`). The format
keys are the same as on Linux: `rate = 48000` and `sample_format = "S24_3LE"` for 24 bit (or `"S16_LE"`).
`channels`, `capture_channels` and `input_channels` work as described above; PortAudio can open just the first
channels of a device, so `capture_channels` is only needed when the inputs you use are not the first ones (for
example `capture_channels = 4` and `input_channels = [3, 4]`).

**Settings at a glance**

| Key | Default | Meaning |
|---|---|---|
| `audio_backend` | `"auto"` | `"alsa"` (arecord), `"portaudio"`, or `"auto"`: arecord on Linux when it is installed, PortAudio elsewhere |
| `wasapi_exclusive` | `true` | Windows: open the device exclusively, past the Windows mixer; shared mode if that is not possible |
| `asio` | `"auto"` | Windows: use ASIO drivers where installed (listed first, chosen automatically); `false` = WASAPI only |

### macOS (Core Audio)

- **Sample rate.** When a recording or level check starts, vinyl-check switches the interface to the chosen
  rate, as *Audio MIDI Setup* would. Other apps using the same interface follow. If the interface does not support
  the rate, the recording does not start. Core Audio never resamples for vinyl-check.
- **Bit depth.** In *Audio MIDI Setup*, choose the input format with 24 bit (for example *2 ch 24-bit Integer
  48.0 kHz*) if the interface offers it. Core Audio hands the samples over as 32-bit floating point; vinyl-check turns
  them back into the converter's integer values exactly.
- **Microphone access.** macOS asks once whether vinyl-check, or the terminal app it was started from, may use the
  microphone. This covers every audio input, USB interfaces included. Without permission macOS delivers digital
  silence instead of an error, and vinyl-check shows a note after a few seconds of level check. Allow it under
  *System Settings → Privacy & Security → Microphone* and restart vinyl-check.
- **Input volume.** If *Audio MIDI Setup* shows an input volume for the device, set it once (usually to the
  maximum) and set the level on the interface or the phono stage. Do not touch either after calibration.

### Windows (WASAPI and ASIO)

- **Which entries.** Windows offers every device through several audio systems. vinyl-check lists the **ASIO**
  drivers first and then the **WASAPI** entries, each marked with its audio system (*ASIO: …*, *WASAPI: …*). An
  interface with an ASIO driver is recorded through ASIO; WASAPI serves devices without one, such as most USB
  turntables. MME and DirectSound are not offered and are refused, because they always pass through the Windows
  mixer, which resamples.
- **ASIO** (default where a driver is installed). Studio interfaces come with an ASIO driver, which gives direct
  access to the converter. vinyl-check asks the driver for the chosen rate when the recording starts. Many drivers
  keep the rate set in their control panel or dictated by an external clock; then the recording does not start,
  and the message says to set the rate there or to choose the driver's rate under *Settings → Audio interface*.
  ASIO drivers often offer all inputs of the interface: choose the pair under *Inputs*. With many drivers only one
  program can use the interface at a time. If a driver misbehaves, switch off *Use ASIO drivers* (or set
  `asio = false`): vinyl-check then uses WASAPI only. Switching ASIO back on takes effect after a restart.
- **WASAPI exclusive mode** (default, `wasapi_exclusive = true`; the fallback for devices without an ASIO driver).
  vinyl-check opens the device exclusively: the samples come straight from the converter at the chosen rate and bit
  depth, past the Windows mixer and its effects. The
  device must allow it: *Sound settings → More sound settings → Recording → the device → Properties → Advanced →
  Allow applications to take exclusive control of this device*. While vinyl-check records, other programs cannot use
  this input. In exclusive mode the device must offer the chosen bit depth; if it only offers 16 bit, choose
  16 bit under *Settings → Audio interface* (the page marks bit depths the device does not offer).
- **Shared mode.** If exclusive mode is not possible (the device is in use, exclusive control is not allowed, or it
  does not offer the bit depth), vinyl-check records in shared mode and says so in the recording status. Then the
  Windows mixer sets the format: choose *Default Format* on the *Advanced* tab with the same rate as in
  vinyl-check, and 24 bit (for example *2 channel, 24 bit, 48000 Hz (Studio Quality)*). A different rate is not
  converted; the recording does not start.
- **Turn off enhancements.** Turn off *Audio enhancements* for the device (*Settings → System → Sound → the
  device*, and the *Enhancements* or *Advanced* tab in the classic dialog), including noise suppression, automatic
  gain control and voice effects. In shared mode they change the signal.
- **Recording level.** On the *Levels* tab, set the level once (usually 100) and leave it alone after calibration.
  Set the actual gain on the interface or the phono stage.

### System sounds and other programs

- Turn off system sounds (macOS: *System Settings → Sound → Sound Effects*; Windows: sound scheme *No Sounds*) or
  send them to another output. Interfaces with a loopback or a mix of the computer's playback on their inputs would
  otherwise put them into the recording.
- Close video calls and other recording programs that use the same interface. They can change its rate or take it
  over.
- Keep the computer awake while recording, and a laptop on its charger. When the device delivers no audio for five
  seconds (sleep, a pulled cable), vinyl-check ends the recording with a message.

### Linux with PortAudio

Linux records with `arecord` by default. To record through PortAudio instead, for example on a desktop with
PipeWire or JACK, install the system library (`libportaudio2` on Debian and Ubuntu) and
`pip install "vinyl-check[portaudio]"`, then set `audio_backend = "portaudio"`. Prefer the device entries with
`(hw:…)` in their name: `default`, `pulse` and `sysdefault` go through the sound server, which may resample.

## Turntable alignment basics

Work in this order. Each step depends on the ones before it.

| Step | What it is | Tool | What vinyl-check measures |
|---|---|---|---|
| 1. Level | platter horizontal in both directions | spirit level on the platter | indirectly: crosstalk asymmetry |
| 2. Overhang and zenith | stylus position along the arc, cartridge angle in the headshell | protractor for your arm geometry | not directly; tracking distortion across the side |
| 3. Tracking force | downward force on the stylus, as specified by the cartridge maker (upper half of the range is a good start) | stylus force gauge at record height | tracking reserve on the +12 dB ramp |
| 4. Anti-skating | counters the inward pull of the groove | anti-skating dial | which channel distorts first on the 315 Hz ramp |
| 5. VTA / SRA | arm height, which sets the stylus rake angle | arm height adjustment | intermodulation distortion (SMPTE 60 Hz + 4 kHz): lowest value wins |
| 6. Azimuth | cartridge tilt seen from the front | headshell or arm adjustment | crosstalk left → right versus right → left |
| 7. Check | everything together | test record | all findings with targets |

**What the findings mean**

- **Anti-skating.** In a stereo groove, the inner wall carries the left channel and the outer wall the right.
  Too much anti-skating pushes the stylus against the outer wall, so the left channel distorts first. Too little
  anti-skating means the right channel distorts first. When both channels break up within 1 dB of each other, the
  anti-skating is balanced. If both break up early, check the tracking force or the stylus.
- **Azimuth.** Crosstalk should be low (≤ −25 dB for a good MM cartridge) and equal in both directions
  (difference ≤ 3 dB). A larger difference means the cartridge is tilted.
- **VTA.** Change the arm height in small steps, make one run per setting and keep the one with the lowest
  intermodulation (target ≤ 3 %).
- **Arm/cartridge resonance.** Effective arm mass and cartridge compliance set the resonance frequency. The target
  is 8–12 Hz: below 7 Hz, warps excite it, and above 15 Hz it reaches into the music.
- **Speed and wow & flutter.** Measured on the 3150 Hz band. Targets: speed within ±0.5 %, weighted wow & flutter
  ≤ 0.1 % (2σ). Too slow often means a slipping belt. Too fast points to the motor control (for a synchronous
  motor, its supply frequency).
- **Balance and response.** Channel balance within ±1 dB, frequency response within ±2 dB, and 100 Hz against
  1 kHz within ±1 dB. The last one checks the RIAA accuracy of the phono stage.

## Alignment with vinyl-check and a test record

### Test record

vinyl-check is built around the band layout of the **Analogue Productions AAPT 1** test record (two sides: a 1 kHz
reference, left-only and right-only tones, sweeps, a 100 Hz tone, an SMPTE two-tone, 3150 Hz for speed, a 315 Hz
anti-skating ramp, pink noise, a vertical resonance sweep and a silent groove).

The analysis finds the bands **by their signal**, not by their position or by time marks. Pauses, spoken
announcements and the order of the bands do not matter. Test records with comparable bands (for example a 1 kHz
reference, 1 kHz on one channel only, or 3150 Hz) may be recognised in part. Bands that cannot be assigned are
listed as such. Only the AAPT 1 has been verified, and support for further test records is on the roadmap.

### Turntable profile (first alignment)

Open *Turntable check* and choose *Add turntable*. The assistant has five steps, and you can leave and resume it at
any time:

1. **Create the profile.** Enter a name, the cartridge, the capture device, tracking force, anti-skating, record
   weight, whether the level is fixed, and the target preset.
2. **Calibrate the chain.** Switch the turntable on, let the platter spin and **keep the stylus raised**.
   vinyl-check records 25 seconds and stops by itself. This measures the noise of the electronics.
3. **Test record, side 1.** Start the recording and lower the stylus at the beginning of the side when *GO*
   appears. The recording stops in the run-out groove and the analysis runs automatically (about 1–2 minutes).
   Side 1 holds the reference tone, crosstalk (azimuth), frequency response, VTA and wow & flutter.
4. **Test record, side 2.** Side 2 holds the anti-skating ramp, pink noise, the resonance sweep and the silent
   groove.
5. **Review and accept.** If something is flagged (anti-skating, azimuth, speed), fix it first and record the
   affected side again. Then accept.

From then on, every recording made with this profile uses its values:

| Profile value | Effect |
|---|---|
| Capture device | a recording only starts if exactly this device is connected |
| Chain noise | tells "stylus up" from "stylus in the groove" |
| Reference level | pops are counted relative to the measured 1 kHz level instead of a fixed −20 dBFS, so loud and quiet chains are graded alike |
| Speed | skip detection and comparisons look for the real revolution time |
| Click floor | clicks and crackle the detector finds in the silent groove; quiet-passage figures are shown above this floor |
| Sensitivity | detector setting for this turntable (0.5 is the calibrated point) |

*Re-align* starts a new alignment, and the previous values stay valid until you accept the new result. Re-align
after changing the stylus or cartridge, after moving the turntable, and after changing the interface.

### Runs: adjust and compare

An alignment run is one setting of the turntable. Create a run with tracking force, anti-skating, VTA, stylus
hours and input level (the fields start with the values of the last run, so change only what you adjusted).
Record side 1 and side 2.

To try a change, create a new run, record only the side that measures it (side 2 for anti-skating), select both
runs and compare them. ▲ marks the better value. Differences below 0.3 dB or 10 % are within measurement scatter.

A good order at the turntable: measure the tracking force with a gauge first, then set anti-skating on the ramp,
and finally confirm with the resonance band.

### Live measurement

The live measurement works without recording. Five times per second it shows the test tone, level and distortion
(THD+N) per channel, the channel image and balance or crosstalk, plus the distortion trend of the last 60 seconds.
It is made for the anti-skating ramp (side 2, band 1): change the anti-skating, play the band again and watch which
channel crosses 3 % first.

### Targets

Targets are data, not constants. A target preset defines, for each figure, what counts as *ok*, *check* and
*problem*. Built-in presets: *vinyl-check standard*, *DIN 45500* (literature values of the withdrawn standard),
*Lab strict* (values measurement labs expect from very good drives) and presets with manufacturer data for
individual cartridges. You can copy and edit presets under *Settings → Targets* and choose one per turntable
profile. Findings are evaluated against the preset when they are displayed, so switching presets takes effect
immediately and no new analysis is needed.

## Level calibration

**Chain noise (stylus up).** The turntable profile does this in step 2. Outside the assistant: platter spinning,
stylus raised, *Calibration (stylus up)*, 20–30 seconds, *Stop*. A value louder than −65 dBFS is rejected, which
usually means the stylus was down.

**Recording level with a test record.** The 1 kHz reference tone (side 1, band 1) should read about **−20 dBFS
(±4 dB)**. The loudest band, the anti-skating ramp at +12 dB, then still has about 8 dB of headroom, and groove
noise stays well above the converter's own noise at 24 bit. The live measurement shows *ok / too loud / too quiet*
for the reference tone together with the value of the last run. The run comparison warns when the reference tones
of two runs differ by more than 1 dB.

**Recording level without a test record.** Use *Check level* and play the loudest passage of a record. The meter
must not turn red. Leave about 6 dB of headroom. A clipped recording is useless for comparisons.

**Fixed-gain USB turntables.** There is nothing to set. Mark the profile as fixed level.

After calibration, leave the gain alone. Both modes, record checks and alignment, use the same setting.

## Your first recording

1. **Create the record.** Under *Record check*, search your Discogs collection or enter artist and title by hand.
2. **Choose side, condition and speed.** Condition is *uncleaned*, *after wet cleaning*, *after dry cleaning* or
   *other*. Add the cleaning method if you like, and a copy label (*Copy 1*, *Copy 2*) if you own the record more
   than once. Set 33⅓ or 45 rpm (the skip detector depends on it).
3. **Check the level** and start the recording. Lower the stylus and record the **whole side, including lead-in
   and run-out groove**. vinyl-check stops by itself once the stylus has circled three times in the run-out groove
   (at least 30 dB below the loudest music, after 60 seconds at the earliest). The analysis starts automatically.
4. **Make two uncleaned recordings** before cleaning. Comparing them shows how much the measurement scatters on
   its own, and only improvements beyond that are real.
5. **Clean, record again, compare.** See the [user guide](user-guide.md).

## Checklist before every session

- [ ] Stylus clean: brush from back to front, no fluff on the tip.
- [ ] Record clean and discharged: dust and static are the most common source of crackle.
- [ ] Turntable level and warmed up for a few minutes, so that the speed is stable.
- [ ] Speed set on the turntable and in the recording form (33⅓ or 45).
- [ ] The right turntable profile is active (shown in the header) and its capture device is connected.
- [ ] Gain unchanged since calibration (compare with the input level noted in the last run).
- [ ] *Check level* on a loud passage: no red.
- [ ] Enough free disk space. The overview shows storage and running jobs.
- [ ] Dust cover and surroundings as during alignment: no hands on the plinth, no footfall, no speaker right next to
      the turntable.
- [ ] Record the whole side, lead-in to run-out.

**Every few weeks, or after any change to the turntable:** play the test record again (a new run) and compare it
with the last one. Re-align the profile after changing the stylus, cartridge, interface or location.
