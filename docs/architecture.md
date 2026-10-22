# Architecture: stations and a central hub

> **Status: design document for the roadmap.** Nothing described here beyond the current single station is
> implemented yet. The document settles **what belongs where** before code is written. Interfaces and names may
> still change.

## Today: one station

```
Browser ──HTTP/WebSocket──► FastAPI (web.py)
                              ├── capture.py      arecord on the ALSA hw: device → WAV + live level
                              │     └── measure.LiveMeter   fundamental, THD+N L/R five times per second
                              ├── jobs.py         one worker: analysis, alignment, comparison, master check
                              │     └── heavy.py  child process with low priority
                              │           ├── analysis.py ─► detect.py, grading.py, skips.py, second_opinion.py
                              │           ├── alignment.py ─► measure.py
                              │           ├── compare.py, master.py
                              │           └── spectro.py, technical.py
                              ├── learning.py     model from judgements and master comparisons
                              ├── discogs.py      collection, release data, condition write-back
                              ├── report_pdf.py   PDF reports
                              └── db.py           SQLite (WAL): records, recordings, analyses, profiles, runs
```

| Decision | Reason |
|---|---|
| Python with numpy and scipy | the core work is signal processing, and everything needed exists |
| capture straight from the `hw:` device | no sound server and no resampling make recordings bit-exact and reproducible |
| one worker, heavy work in a child process | an LP side needs 1–2 GB of RAM, and parallel analyses would only compete. The interface and an ongoing recording keep priority, and an out-of-memory crash does not take the service down. |
| database and audio in separate places | small, valuable measurements survive the loss of the drive holding the large WAV files |
| a systemd service with a sandbox, not a container | USB audio hot-plug without workarounds and updates via the system package manager |
| no external JavaScript libraries | the interface works without internet access |

## The idea: several stations that learn together

vinyl-check gets better with every judgement: whenever someone marks a detection as *click* or *false alarm*, or a
master comparison classifies it, the learning model has one more example (see [measurement](measurement.md#learning)).
A single station only learns from one turntable, one listener and the records in one collection. Several stations
in different places (a private listening room, a mastering studio, a pressing plant) could pool what they learn, so
that detection improves with every station.

## Principles

1. **A station stays self-contained.** Recording, analysis, grading, reports and approval run on the station, as
   today, also without internet. The hub is never in the measurement path. An LP side needs 1–2 GB of working
   memory and 600 MB of audio, which belongs next to the turntable, and a studio should not depend on someone
   else's cloud.
2. **Only knowledge travels, never audio.** A station sends judgements with precomputed features and the context of
   its chain. Whole sides never leave the station. Short snippets around an event come later, if at all, and only
   with the station's explicit consent.
3. **The station calls, never the other way round.** Stations sit behind other people's routers. They open the
   connection themselves (outgoing HTTPS), so the hub needs no access into a station's network.
4. **A new model is checked before it is used.** The hub only publishes models that perform better across
   stations, and a station only adopts a model if it does not estimate worse on its own master recordings. Until
   then it runs "on probation", just as the learning model today is only used once it is *usable*.

## Picture

```
   Station A (Raspberry Pi)          Station B (studio, macOS/Windows)    Station C …
   ┌──────────────────────────┐      ┌──────────────────────────┐
   │ record → analyse → grade │      │ record → analyse → grade │
   │ manual judgements        │      │ manual judgements        │
   │ master comparison        │      │ master comparison        │
   │ outbox             ──────┼──┐   │ outbox             ──────┼──┐
   │ model (checked locally) ◄┼┐ │   │ model (checked locally) ◄┼┐ │
   └──────────────────────────┘│ │   └──────────────────────────┘│ │
                               │ │ HTTPS, the station calls       │ │
                               │ ▼                                │ ▼
                       ┌──────────────────────────────────────────────┐
                       │ vinyl-hub                                    │
                       │  inbox: judgements + features + chain profile│
                       │  training per kind, validation per station   │
                       │  versioned models, only checked ones shipped │
                       │  overview: stations, contributions, quality  │
                       └──────────────────────────────────────────────┘
```

## What a station sends

| Content | Source today | Why |
|---|---|---|
| **Feature vector per judged detection** (`learning.FEATURES`) with the feature version | `learning.features()` | the features need the recording's music reference and the density of **all** detections within ±0.5 s, so they cannot be recomputed centrally from the existing export (`/api/audit/export`, judged events only) |
| **Classification** per detection: manual (click, false alarm, unsure) or from the master comparison (vinyl / music) | event judgements, master checks | master classifications provide most of the examples, and manual judgements are the most valuable (weighted five times today) |
| kind, region, strength, level, duration, channel, network value, time | events | traceability and future features |
| **Chain profile**: turntable, cartridge, phono stage, converter, chain noise, reference level, click floor, sensitivity, speed | turntable profile | the same defect looks different through another chain. Without the profile, the hub would learn chains instead of clicks. |
| recording key: WAV checksum, side, Discogs release, master yes/no | recordings | detect duplicates, cross-validate per record |
| software and detector version | `__version__`, detector parameters | keep examples from older detector versions apart |

**Not sent:** audio, notes on records, Discogs credentials, findings, remarks. A detection is identified centrally by
station + WAV checksum + time (to 1 ms).

## What the hub returns

- A **model** in the format of `learning.train()` today (weights and normalisation per kind, cross-validation,
  `id`), plus its version, the feature version and the detector versions it applies to.
- A **calibration per station** (second stage). The shared model separates clicks from music, and a small
  correction per station adjusts for the chain. The hub learns it from that station's examples once there are
  enough.
- Later, possibly: recommended grade limits and target presets, once the hub knows enough records of known condition.

## Flow

1. **Enrolment.** A station receives its own key when it is set up, by invitation of the hub operator.
2. **Collect.** Every new or changed judgement and every finished master comparison goes into an outbox table on
   the station, with a timestamp. A withdrawn judgement is sent as a deletion.
3. **Sync.** Every few minutes, and only while no recording is running, the outbox is sent to the hub in batches.
   The station then asks whether a newer model exists (with an ETag, so usually without a payload).
4. **Learn.** The hub retrains when enough new data has arrived. Validation **leaves one station out**: train on all
   others and test on the missing one. Only that shows whether what was learned transfers to an unknown chain.
5. **Adopt.** The station evaluates the new model on its own master recordings and adopts it only if it is not
   worse. Otherwise it keeps its local model and reports this back.

## Where the hub runs

A small service: Python with FastAPI like the station, PostgreSQL (SQLite would do at the start), run as a container
on a small server behind a reverse proxy with TLS. The code lives in the same repository as the station, sharing
`learning.py` and the feature definitions, so that station and hub compute the same way.

## Stations on macOS and Windows

The station is pure Python except for capture, which today uses `arecord` on an ALSA device (Linux). For macOS and
Windows it needs a second capture backend based on PortAudio (`sounddevice`), and a prototype has already recorded
successfully on macOS. On Windows, ASIO support matters for studio interfaces. Beyond capture, each platform needs:

- an installer or app bundle instead of Debian packages and systemd,
- data and configuration in the user's profile instead of `/var/lib` and `/etc`,
- an optional LAN mode, so that phones and tablets near the turntable can use the station's interface.

The path is to put `capture.py` behind an interface (ALSA and PortAudio backends) first, then package. New code
must not add Linux assumptions.

## Stages

| Stage | Content | Benefit |
|---|---|---|
| 0 | **Judgement package as a file**: export feature vectors with classification and chain profile, import on another station; `learning.training_data` includes imported examples | exchange with a second station right away, without any server, and evidence of whether foreign examples help at all |
| 1 | station ID, chain profile in the package, outbox table; a minimal hub (accept, train, deliver a model); first stations | protocol and flow at small scale |
| 2 | stations at other sites (for example a mastering studio); validation and calibration per station | real generalisation across chains |
| 3 | macOS and Windows stations (PortAudio capture, installer) | studios without a Raspberry Pi |
| 4 | snippets with consent, central training of detector and network, station overview | better detection itself, not only better estimates |

## Open decisions

- Who may join: invited stations only, or open later?
- Should grades and record conditions per Discogs release be shared too (a shared condition database), or only
  judgements of detections?
- Audio snippets at all, and if so, with what consent per station?
- Licensing and redistribution terms for models trained on contributions from several stations.
- Weighting of trained ears: should a mastering engineer's judgement count more than a casual listener's?
