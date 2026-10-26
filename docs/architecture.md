# Architecture: stations and Vinyl-Mind

> **Status: design document for the roadmap.** Implemented so far: the single station (vinyl-check on a Raspberry Pi,
> macOS or Windows) and, since 1.1.0, the station side of Vinyl-Mind (stage 1 and the client of stage 2, see
> [Stages](#stages)), built to the hub's specification, protocol version 1 ([Vinyl-Mind on the station](vinyl-mind.md)).
> The hub is a separate project (Vinyl-Mind, alpha). The document settles **what belongs where**; the contract between
> the two is the hub's specification.

## Today: one station

```
Browser / desktop window ──HTTP/WebSocket──► FastAPI (web.py)
                              ├── capture.py      arecord (ALSA hw:) or PortAudio (Core Audio, WASAPI, ASIO)
                              │     └── measure.LiveMeter   fundamental, THD+N L/R five times per second
                              ├── jobs.py         one worker: analysis, alignment, comparison, master check
                              │     └── heavy.py  child process with low priority
                              │           ├── analysis.py ─► detect.py, grading.py, skips.py, second_opinion.py
                              │           ├── alignment.py ─► measure.py
                              │           ├── compare.py, master.py
                              │           └── spectro.py, technical.py
                              ├── learning.py     model from judgements and master comparisons
                              ├── vinylmind.py    Vinyl-Mind: sign-in, keys, contributions, models (off by default)
                              ├── discogs.py      collection, release data, condition write-back
                              ├── report_pdf.py   PDF reports
                              └── db.py           SQLite (WAL): records, recordings, analyses, profiles, runs
```

| Decision | Reason |
|---|---|
| Python with numpy and scipy | the core work is signal processing, and everything needed exists |
| capture without resampling | recordings stay bit-exact and reproducible (ALSA `hw:` devices, Core Audio, WASAPI exclusive, ASIO) |
| one worker, heavy work in a child process | an LP side needs 1–2 GB of RAM, and parallel analyses would only compete. The interface and an ongoing recording keep priority, and an out-of-memory crash does not take the station down. |
| database and audio in separate places | small, valuable measurements survive the loss of the drive holding the large WAV files |
| Raspberry Pi: a systemd service with a sandbox, not a container | USB audio hot-plug without workarounds and updates via the system package manager |
| desktop: the same engine in a native window | one code base and one interface on every platform (see [Stations on macOS and Windows](#stations-on-macos-and-windows)) |
| no external JavaScript libraries | the interface works without internet access |

## The idea: stations that learn together

vinyl-check gets better with every judgement: whenever someone marks a detection as *click* or *false alarm*, or a
master comparison classifies it, the learning model has one more example (see [measurement](measurement.md#learning)).
A single station only learns from its own turntables, one listener and the records in one collection. Several
stations in different places (private listening rooms, a mastering studio, a pressing plant) can pool what they
learn, so that detection improves with every station.

The central counterpart is **Vinyl-Mind**, a separate project. vinyl-check implements the station side (collecting,
contributing, adopting); Vinyl-Mind implements the hub (combining, versioning, publishing). The contract between the
two (documents, endpoints, signatures, errors, signing in) is the specification in the Vinyl-Mind repository;
[Vinyl-Mind on the station](vinyl-mind.md) says what the station does with it and what leaves a station.

## Principles

1. **A station stays self-contained.** Recording, analysis, grading, reports and approval run on the station, as
   today, also without internet. Vinyl-Mind is never in the measurement path. An LP side needs 1–2 GB of working
   memory and 600 MB of audio, which belongs next to the turntable, and a studio should not depend on someone
   else's cloud.
2. **Only knowledge travels, never audio.** In the first form only model weights and a few counts leave a station
   (see below). Whole sides never leave it. Short snippets around an event come later, if at all, and only with the
   station's explicit consent.
3. **The station calls, never the other way round.** Stations sit behind other people's routers. They open the
   connection themselves (outgoing HTTPS), so Vinyl-Mind needs no access into a station's network.
4. **A new model is checked before it is used.** Vinyl-Mind only publishes models that hold up across stations, and a
   station only adopts a model if it does not estimate worse on its own master recordings. Until then it runs "on
   probation", just as the learning model today is only used once it is *usable*.
5. **Taking part is optional.** Contributing and adopting are separate switches in the station's settings, both off
   until the user turns them on.
6. **Any Vinyl-Mind server will do.** A default server is preset, and it can be replaced by another Vinyl-Mind
   server (for example one run by a studio for its own stations).

## Federated learning with Vinyl-Mind

### Why sharing weights is enough

The learning model of a station is small: per event kind (click, crackle, pop) one logistic regression, that is an
intercept and one weight per feature (`learning.FEATURES`, ten today), plus the normalisation of the features. A
model is a few hundred bytes. Vinyl-Mind therefore needs no neural network and no audio: it is a database of the
weights stations report and a small job that combines them. This is **federated averaging**: stations train
locally, send their weights, the hub combines them into a global model and sends it back.

### The turntable profile as the zero point

A weight only means the same on two stations if the features mean the same. They do not today: a budget USB
turntable has a higher noise floor and many more detector hits in the silent groove than an aligned high-end chain,
so the same feature value says something different on each. Averaged naively, the hub would learn chains instead of
clicks.

vinyl-check already measures each chain. A **turntable profile** stands for one stable setup (turntable, cartridge,
phono stage, interface, gain) and is measured with the alignment assistant: chain noise with the stylus raised,
reference level of the 1 kHz tone, the click floor in the silent groove of the test record (detector hits per minute
that come from the chain, not from a record), crosstalk, wow & flutter, arm/cartridge resonance. The expectation is
that most users set up once and keep working with the same setup, so a station's experience is the experience with
its profile.

The plan is to express the features **relative to the profile** before learning, so that every profile has the same
zero point regardless of how good or bad the chain is:

| Feature today | Relative to the profile | Effect |
|---|---|---|
| strength (detector peak) | strength above the chain's click floor and noise | a hit that the chain itself produces scores the same low value everywhere |
| level relative to the music reference of the recording | additionally relative to the profile's reference level | interfaces with different gain become comparable |
| event density within ±0.5 s | density relative to the profile's click floor per minute | a noisy chain does not look like a crackling record |
| second-opinion score, region, channels, duration | unchanged | already independent of the chain |

Counts and grades can be normalised the same way (for example clicks per minute above the profile's floor, which
`grading` already does for the quiet groove). A new cartridge or a new alignment is a **new profile** and therefore a
new zero point; contributions stay tied to the profile they were made with.

**Feature version 2** (experimental, `feature_version = "2"`) is the first implementation of this table: the density
net of the chain's click floor, the level relative to the profile's reference tone in addition to the music
reference, and how far an event rises above the chain's own noise. The detector's strength itself stays: it is the
peak over the local residual level, which never falls below the chain's noise, so it is already relative to that noise
wherever it dominates (definitions and reasons in [measurement](measurement.md#learning)). On synthetic chains it did
not yet show a clear gain over version 1; version 1 stays the default until real stations show otherwise.

### What a profile contributes

The unit of a contribution is a turntable profile, not a station: a station with two turntables contributes two
profiles.

| Content | Why |
|---|---|
| intercept and weights per kind, in the fixed shared scaling of the feature version, with the features that never varied in the profile's examples marked as not informative | the knowledge itself; a fixed scaling (instead of each station's own mean and spread) makes weights comparable, and the hub combines each weight only from profiles where it was learned |
| number of examples per kind, split into master classifications and manual judgements | weighting: a profile with 2,000 judged events counts more than one with 20 |
| cross-check quality per kind (estimate error per recording, of the raw count, AUC; recordings with a master) | weighting and filtering out profiles whose model does not work |
| coarse chain class (USB turntable / turntable with audio interface; MM / MC / unknown), two fields of the profile | separate global models per class once a class has enough profiles |
| feature version, detector version, client name and version | weights from different feature definitions or detector versions are never mixed |
| a random profile ID chosen by the station, in the address only | updates replace earlier contributions; no names, places or hardware serial numbers |

**Not sent:** audio, feature vectors of single events, record titles, notes, Discogs data, credentials.

The default features are **feature version 1**, relative to the music reference of each recording; version 2 adds
the profile as the zero point (above). Contributions carry the feature version, and the hub never mixes versions. The
fixed common scaling is `learning.FIXED_SCALING` (version 1, exactly the hub's `spec/v1/features.json`) and
`learning.FIXED_SCALING_V2` (the station's proposal); the exact document is in
[Vinyl-Mind on the station](vinyl-mind.md#what-the-station-sends). A contribution is trained on the profile's own recordings and
**without** any prior, so the hub never gets its own result back. In version 2 only recordings whose profile has a
measured zero point count.

### How Vinyl-Mind combines

- **Weighted mean** of the weights per kind, weighted by the number of examples and by cross-check quality, with
  manual judgements counting more than master classifications (as locally, five times).
- **Robust against bad contributions**: a trimmed mean or median per weight, and profiles whose cross-check error is
  too high are left out. A station whose judgements are careless or deliberately wrong cannot drag the result.
- **Per chain class** when the classes differ clearly; otherwise one global model.
- **Validation happens on the stations**: with weights only, Vinyl-Mind has no data to test on. Each station that
  adopts or tries a global model reports how it estimates on its own master recordings compared with its local model.
  A global model version that is worse on most stations is withdrawn.
- Every global model is versioned and published only after it has held up on enough stations.

### How a station uses the global model

Locally, the ridge penalty of the training (`learning.L2`) pulls the weights towards zero when there are few
examples. With Vinyl-Mind it pulls them **towards the global weights** instead:

- a new station with few own judgements starts with what all stations have learned,
- the more examples a profile collects, the more the model becomes its own,
- if the global model is worse on the station's own master recordings, the station keeps its local model.

In code: `learning.train(data, prior=...)` takes weights in the fixed scaling, fits in that scaling and penalises the
distance of every coefficient to them, the intercept included, with `learning.PRIOR_L2` = 100 (about as strong as five
hundred master-classified events) instead of `L2` towards zero. Both follow the hub's simulation (Vinyl-Mind,
`docs/aggregation.md`): a free intercept adapts to the few records a new profile has seen and gives most of the
benefit away, and about 100 worked best for profiles with few judgements. A fetched model is checked per kind on the
station's master recordings: the leave-one-recording-out cross-check with the model as prior against the one without,
and the model applied directly, on the same recordings. A published model becomes the prior of a kind if the
cross-check with it is not worse, or if the station has no cross-check of its own yet (then the model is only a
starting point, and the station's estimates are still used only once its own cross-check makes the kind *usable*); a
candidate is checked and reported, not adopted. An imported package file (stage 1) is used as the prior the same way.

### Settings in vinyl-check

A section *Settings → Vinyl-Mind* on every station:

| Setting | Default | Meaning |
|---|---|---|
| Contribute to Vinyl-Mind | off | send the weights and counts of each turntable profile after retraining |
| Use the global model | off | fetch the global model, check it on the own master recordings, use it as the prior |
| Server | the project's hub, `https://api.vinylcheck.net` (`vinyl_mind_url` presets another) | URL of the server; *Reset to default* restores the preset; *Check server* shows its name and key fingerprint |
| Connect with Vinyl-Mind | when the server has an account service | the station's sign-in: in the browser on this computer, coming back by itself, or with a code and a QR code for a phone (see below) |
| Other ways to join | | an invitation code from the operator, or none (then the operator approves), after confirming the server key's fingerprint |
| Status | | connected, waiting for approval, revoked, server key changed, client version not accepted, server unreachable; server and station key, last contribution, model version in use, starting point (prior), last error |
| Reconnect, Disconnect | | a new key through the sign-in again; leave the server (it deletes the station and its contributions) |
| What is sent | | shows the exact document of the next contribution per profile before anything leaves the station, with the chain class (guessed, set in the profile dialog) |
| Package files | | export a profile's contribution as a file; import a contribution or model file as the starting point |

The default server URL lives in one place in the code (`vinylmind.DEFAULT_VINYL_MIND_URL`) and can be overridden in
the configuration file (`vinyl_mind_url`), so a self-hosted Vinyl-Mind can be preset for a group of stations.

### Securing the connection

The API between stations and Vinyl-Mind must not be usable for anything but contributing to and fetching the shared
model. The design:

- **Station identity by key pair.** When joining, the station creates an Ed25519 key pair for that server; only the
  public key goes to the server. Every request is signed (server key, method, path, body hash, timestamp, nonce), so
  a stolen request cannot be replayed and a leak of the server's database reveals no secret that would let anyone
  impersonate a station.
- **Joining needs an invitation or approval.** A new station signs in with an account (the account service hands
  out an invitation bound to the station's key by a signed binding, `docs/connect.md` of the hub), brings an
  invitation code from the operator, or waits for the operator to approve it. Joining is rate-limited per address and
  per day. This is the main defence against many fake stations (Sybil attacks). The secrets of signing in (one-time
  code, PKCE verifier, device code, invitation, binding) stay in the engine's memory.
- **Signed models.** The server signs every global model with its own key. The station verifies the signature with
  the key it accepted when joining (the fingerprint in the settings) before it even evaluates the model. Switching
  servers means accepting a new key explicitly.
- **TLS only**, with certificate validation; no plain HTTP, not even on the local network.
- **Strict input checks on the server**: a fixed JSON schema per `feature_version`, size limits, finite numbers
  within plausible bounds, counts that fit the reported quality.
- **Limited influence per station.** New stations start with low trust that grows with consistent contributions and
  validation reports; the weight of a single station in the average is capped; aggregation is robust (trimmed mean
  or median), and contributions with a poor self-check are left out. Together with principle 4 (every station checks
  a model on its own data before using it), a poisoned model cannot silently spread.
- **Least privilege**: a station can only replace its own profiles' contributions, report its own checks and read
  the published models. It never sees another station's contributions. Keys can be revoked by the operator and by
  the station itself (*Disconnect*), which also deletes its contributions.
- **Data minimisation**: random profile IDs, no names or places; the server keeps access logs only as long as needed
  against abuse.

The exact rules (signed message, headers, replay window, models as JWS, error codes, size limits) are in the hub's
`spec/signing.md` and `docs/api.md`. On the station the private key is a file per server in the state directory
with mode 600, never in the database, the logs or an API answer, and *Disconnect* deletes it
([Vinyl-Mind on the station](vinyl-mind.md#connecting)).

### Later: sharing examples

A second form keeps the plan of sending **feature vectors with their classification** (never audio) for each judged
event. Vinyl-Mind could then train on all examples at once, which is more accurate than averaging weights, at the
price of more data leaving the station. It also makes richer models possible (new features, the detector itself).
This form is optional and comes after federated averaging has proven itself.

| | Sharing weights (first) | Sharing examples (later) |
|---|---|---|
| leaves the station | a few hundred bytes per profile | kilobytes to megabytes of numbers |
| accuracy of the shared model | good | better |
| privacy | very high | high (no audio) |
| work on the hub | combining | training |

## Flow

1. **Connect.** The user chooses *Connect with Vinyl-Mind* and signs in with an account in the browser (or confirms
   a code on a phone); on a hub without accounts, *Join* with an invitation code or the operator's approval after
   confirming the server key's fingerprint. The station creates its key pair for that server, registers it and keeps
   the server's key. *Contribute* and *Use the global model* are switched on separately.
2. **Train locally.** As today: every judgement and master comparison retrains the station's model; each profile's
   contribution is trained from the recordings made with that profile.
3. **Contribute.** After a retraining, and only while no recording runs, the station sends each changed profile's
   weights and counts (outgoing HTTPS, at most hourly per profile, retried with backoff when offline).
4. **Combine.** Vinyl-Mind recombines when enough new contributions have arrived and publishes a new global version.
5. **Adopt.** The station asks for a newer global model (with an ETag, usually without payload), evaluates it on its
   own master recordings, uses it as the prior of its training if it is not worse, and reports the result back.

## Where Vinyl-Mind runs

A separate project and repository, Vinyl-Mind: a small service in Go (SQLite or PostgreSQL), run as a container
behind a reverse proxy with TLS, with an optional account service on the project's website for signing in. Station
and hub must compute and read the features the same way, so the feature definitions, the shared scaling, the
documents and the signatures are a versioned specification in the hub's repository (`spec/`: JSON schemas and
`features.json`), not code copied between repositories; the station's tests check against a copy of it.

## Stations on macOS and Windows

Since 1.0.1 the station runs on Linux, macOS and Windows. Capture has two backends behind the same source
interface (`capture.open_source`): `arecord` on an ALSA device (Linux, the Raspberry Pi) and PortAudio through
`sounddevice` (Core Audio on macOS, ASIO and WASAPI on Windows, `capture_portaudio.py`). Device, rate, bit depth
and inputs can be set in the interface (*Settings → Audio interface*, `audio_settings.py`). The desktop apps are the
same engine in a native window (`desktop.py`, pywebview), packed with PyInstaller, with data in the user's profile
and an optional LAN mode for phones and tablets near the turntable.

## Stages

| Stage | Content | Benefit | State |
|---|---|---|---|
| 0 | **Profile-relative features**: the turntable profile becomes the zero point of the features (see above), with a feature version; the local model works unchanged on top | the prerequisite for comparing weights at all, and measurably better local estimates across a change of chain | feature version 2 defined and selectable (experimental), evaluation tool `tools/eval_features.py`; no clear gain on synthetic chains, real stations open; version 1 stays the default |
| 1 | **Contribution package as a file**: export a profile's weights and counts, import a foreign package as the prior on another station | evidence of whether foreign knowledge helps, without any server | done on the station (1.1.0): *Settings → Vinyl-Mind → Package files*, `learning.train(prior=...)` |
| 2 | **Vinyl-Mind minimal**: joining with key pairs and approval, signed requests and signed models, accept contributions, combine robustly, publish versions; *Settings → Vinyl-Mind* on the station (switches, server, key, status, payload preview) | federated averaging at small scale | station side done (1.1.0) to the hub's protocol version 1: signing in (browser and device code) or invitation code, key per server, signed requests, verified models, adopting per kind, check reports, settings; tested against a fake hub and end to end against the real hub. The hub (alpha) and the account service on the website run. |
| 3 | stations at other sites and chain classes; validation reports from stations | real generalisation across chains | reports and chain classes are sent; open |
| 4 | optional sharing of examples (feature vectors), central training of richer models; snippets only with consent | better detection itself, not only better estimates | open |

## Open decisions

- Should a re-measured profile (new alignment of the same cartridge) get a new profile id? Today the id stays with
  the profile row; with profile-relative features (stage 0) a new alignment is a new zero point. Feature version 2
  reads the zero point from the profile as it is now, so a re-measured profile moves the zero point of its older
  recordings too.
- Whether feature version 2 becomes the default, and its exact normalisation: needs master comparisons or judgements
  of the same records on several measured chains (`tools/eval_features.py real` on a copy of a station database).
- Feature version 2 in the hub's specification (`spec/v2`), with its fixed scaling, once real stations have shown
  whether it helps.
- Should grades and record conditions per Discogs release be shared too (a shared condition database), or only
  models?
- Audio snippets at all, and if so, with what consent per station?
- Licensing and redistribution terms for global models built from contributions of several stations.
- Weighting of trained ears: should a mastering engineer's judgement count more than a casual listener's?
