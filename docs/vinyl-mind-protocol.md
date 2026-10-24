# Vinyl-Mind protocol (draft v0)

> **Status: draft v0, followed by both projects.** This is the contract between a vinyl-check station and a
> Vinyl-Mind hub. vinyl-check implements the station side as described here (`vinylcheck/vinylmind.py`) and tests it
> against a small hub built from this document (`tests/test_vinylmind.py`). Vinyl-Mind implements the hub side from
> the same document. While the protocol number is 0, fields and rules may still change, but only in both projects at
> once. The background (why weights, why per turntable profile, how the hub combines) is in
> [architecture.md](architecture.md#federated-learning-with-vinyl-mind).

## Contents

1. [Overview](#overview)
2. [What leaves a station](#what-leaves-a-station)
3. [Conventions](#conventions)
4. [Features and the common scaling](#features-and-the-common-scaling)
5. [Contribution](#contribution)
6. [Global model](#global-model)
7. [Check report](#check-report)
8. [Endpoints](#endpoints)
9. [Request signing](#request-signing)
10. [Model signing](#model-signing)
11. [Errors](#errors)
12. [Limits](#limits)
13. [Versioning](#versioning)
14. [Station behaviour](#station-behaviour)
15. [Package files](#package-files)

## Overview

```
station                                              hub
  │  GET  /v0/server                 (unsigned)        │  server name, public key, fingerprint
  │  POST /v0/stations               join              │  pending or approved
  │  GET  /v0/stations/me            status            │
  │  PUT  /v0/contributions/{id}     per profile       │  replaces the profile's last contribution
  │  GET  /v0/models/current         ETag, signed model │
  │  POST /v0/reports                check report      │
  │  DELETE /v0/stations/me          leave             │  deletes everything of the station
```

- The station always calls the hub, never the other way round (outgoing HTTPS only).
- Every request except `GET /v0/server` is signed with the station's Ed25519 key.
- Every global model is signed with the hub's Ed25519 key. The station verifies the signature with the key it
  accepted when joining before it looks at anything else in the model.
- The unit of a contribution is a **turntable profile**, not a station. A station with two turntables sends two
  contributions.

## What leaves a station

This list is the privacy statement of the protocol. The station's settings show the exact payload before anything is
sent (*Settings → Vinyl-Mind → What is sent*).

**Sent:**

| Data | When | Why |
|---|---|---|
| the station's public key and an optional invitation code | joining | identity of the station; the code proves an invitation |
| per turntable profile: a random profile id, weights and intercept per event kind, the number of examples (master classifications and manual judgements, real and false alarm), the number of recordings, the self-check quality (estimate error, AUC), a coarse chain class (USB turntable or interface; MM, MC or unknown), feature, detector and vinyl-check version | after retraining, while *Contribute* is on | the knowledge itself and what the hub needs to weight and filter it |
| per profile and kind: estimate errors of the local model, of the global model and of the local model with the global model as prior, on the station's own master recordings, and their number | after checking a new global model, while *Use the global model* is on | validation happens on the stations, because the hub has no data |
| the vinyl-check version, the time of each request (signed timestamp) | every request | protocol checks, replay protection |

The hub also sees the station's IP address, as every web server does. It should keep access logs only as long as
needed against abuse.

**Never sent:** audio of any kind (sides, snippets, single events), the features or times of single events, record
titles, artists, Discogs data, notes, names of profiles, turntables, cartridges or devices, host names, serial
numbers, credentials, and the station's private key.

*Leave* deletes the station's key, contributions and reports on the hub. The profile ids are random (128 bits) and
derived from nothing; a new turntable profile gets a new id.

## Conventions

- **Transport:** HTTPS with certificate validation, TLS 1.2 or newer. No plain HTTP, not even on a local network.
  (The station accepts `http://` only for `127.0.0.1`, `::1` and `localhost`, for tests and development.)
- **Base URL:** the server URL configured on the station, for example `https://vinyl-mind.example`, possibly with a
  path prefix (`https://example.org/vinyl-mind`). All paths below are appended to it. No query string, no fragment,
  no user name or password in it.
- **Bodies:** JSON, UTF-8, `Content-Type: application/json`. Unknown members are an error in every payload
  (`additionalProperties: false`), so a typo or a field from a newer draft never goes unnoticed.
- **Encodings:** `b64u` = base64url without padding (RFC 4648 §5); hashes and ids are lowercase hex.
- **Times** in payloads are RFC 3339 in UTC with a `Z` (`2026-10-24T12:00:00Z`); the signing timestamp is Unix
  seconds.
- **Station id** = hex SHA-256 of the station's raw 32-byte Ed25519 public key (64 characters).
- **Key fingerprint** (station or hub) = hex SHA-256 of the raw 32-byte Ed25519 public key, shown to people in 16
  groups of 4 characters: `3f2a 9c41 …`. The hub's operator publishes the hub's fingerprint so stations can compare.
- **Profile id** = 32 lowercase hex characters (128 random bits), chosen by the station once per turntable profile.

## Features and the common scaling

Each event kind (`click`, `tick` = crackle, `pop`) has a logistic regression: the probability that a detected event is
a real defect and not music is `σ(intercept + Σ wᵢ · (xᵢ − cᵢ) / sᵢ)`. The features `xᵢ` and the fixed common
scaling (`cᵢ`, `sᵢ`) are defined per **feature version**. Weights are always exchanged in this fixed scaling, never in
a station's own standardisation, so the same weight means the same on every station.

**Feature version `"1"`** (`vinylcheck/learning.py`: `FEATURES`, `FIXED_SCALING`, `features()`), in this order:

| # | Name | Definition | Centre c | Scale s |
|---|---|---|---|---|
| 0 | `log_strength` | ln(1 + detector peak strength) | 4.0 | 1.0 |
| 1 | `amp_rel` | event level in dBFS minus the music reference of the recording (−15 dBFS if unknown; −60 dBFS if the event has no level) | −15.0 | 10.0 |
| 2 | `log_dur` | ln(1 + duration in ms) | 0.5 | 0.5 |
| 3 | `gc` | second-opinion score 0…1 (0.5 if missing) | 0.5 | 0.25 |
| 4 | `gc_missing` | 1 if there is no second-opinion score, else 0 | 0.5 | 0.5 |
| 5 | `music` | 1 if the event lies in the music, else 0 | 0.5 | 0.5 |
| 6 | `both_ch` | 1 if the event is on both channels, else 0 | 0.5 | 0.5 |
| 7 | `log_density` | ln(1 + number of other events within ±0.5 s) | 0.5 | 0.75 |
| 8 | `amp_rel2` | `amp_rel`² / 100 | 3.0 | 3.0 |
| 9 | `strength_x_amp` | `log_strength` × `amp_rel` / 10 | −6.0 | 5.0 |

Feature version 1 is relative to the music reference of each recording, **not to the turntable profile**. Weights of
version 1 therefore still carry some of each chain's character (noise floor, click floor); the hub should combine them
per chain class where it can. It is the default.

**Feature version `"2"`** (experimental; a station uses it only with `feature_version = "2"` in its configuration;
`learning.py`: `FEATURES_V2`, `FIXED_SCALING_V2`) takes the recording's **turntable profile as the zero point**: its
chain noise `chain_db` (stylus up, mean power in dBFS), its reference level `ref_dbfs` (the test record's 1 kHz
reference tone, sine amplitude in dBFS) and its click floor (clicks plus crackle per minute that the detector finds in
the test record's silent groove), the last two from an accepted alignment run. Features 0–3, 5, 6, 8 and 9 are defined
and scaled as in version 1; the others:

| # | Name | Definition | Centre c | Scale s |
|---|---|---|---|---|
| 4 | `gc_missing` | as in version 1 | 1.0 | 0.5 |
| 7 | `log_density_net` | ln(1 + max(0, n − λ)): n other events within ±0.5 s, λ = click floor per second × 1 s, the neighbours the chain alone puts there | 0.5 | 0.75 |
| 10 | `amp_ref` | event level in dBFS minus `ref_dbfs` (−60 dBFS if the event has no level) | −20.0 | 10.0 |
| 11 | `chain_margin` | (event level − `chain_db`) / 10, clipped to 0 … 4 (40 dB and more: the chain plays no role) | 4.0 | 1.0 |

The reasons for each definition are in [measurement](measurement.md#learning). A station contributes in version 2
only recordings whose profile has all three values; a profile that was never measured and accepted has nothing to
contribute. Versions 1 and 2 are never mixed: not in contributions, models, reports, package files or priors.

A station fits its model on its own standardisation and converts the result exactly (`learning.to_fixed`). A weight
of a feature that did not vary in a profile's training data (for example the second-opinion score on a station
without the optional network: always missing) carries no information and is sent as 0 with `informative: false`; the
hub must leave it out of the average for that weight. That is exact only where the feature sat at its centre: a
constant value away from the centre is absorbed by the profile's intercept, and averaging that intercept with others
biases the result. Version 2 therefore centres the features that are constant on many profiles at that value
(`gc_missing` at 1, `chain_margin` at its saturation 4). In version 1, `gc_missing` is 1 on stations without the
network and mostly 0 on stations with it, so the hub should not average intercepts across that line.

## Contribution

One contribution per turntable profile, sent with `PUT /v0/contributions/{profile_id}`. It replaces the profile's
previous contribution. Only event kinds with a trained model are included; a profile without any is not sent.

The station trains the contribution **without any prior**: sending weights that were pulled towards the global model
would feed the hub's own result back to it.

```json
{
  "type": "vinyl-mind/contribution",
  "protocol": 0,
  "profile_id": "9b1f0c2e4d6a48b7a3c5e7f9012345ab",
  "feature_version": "1",
  "features": ["log_strength", "amp_rel", "log_dur", "gc", "gc_missing", "music", "both_ch", "log_density",
               "amp_rel2", "strength_x_amp"],
  "detector_version": 1,
  "vinyl_check_version": "1.1.0",
  "chain_class": {"connection": "interface", "cartridge": "mm"},
  "kinds": {
    "click": {
      "intercept": -0.731,
      "weights": [0.65, -1.1, -6.34, 0.66, 0.0, 0.0, -0.42, 0.18, 0.9, -0.3],
      "informative": [true, true, true, true, false, false, true, true, true, true],
      "examples": {"master": 1840, "manual": 64, "real": 702, "false_alarm": 1202},
      "recordings": {"total": 14, "master": 11},
      "quality": {"est_error": 0.08, "raw_error": 0.31, "auc": 0.93, "usable": true}
    }
  }
}
```

| Field | Meaning |
|---|---|
| `features` | the feature names of `feature_version`, in order; the hub rejects a list that differs from its definition |
| `chain_class.connection` | `usb_turntable` (turntable with a built-in USB converter), `interface` (turntable, phono stage and an audio interface), `unknown` |
| `chain_class.cartridge` | `mm`, `mc`, `unknown` |
| `weights`, `informative` | one entry per feature, in the fixed scaling (see above) |
| `examples.master` / `.manual` | training examples from master classifications / from judgements by hand (a judgement counts five times in the station's own training) |
| `examples.real` / `.false_alarm` | the same examples split by class; `real + false_alarm = master + manual` |
| `recordings.total` / `.master` | recordings that gave examples / recordings fully classified by a master comparison |
| `quality.est_error` | mean relative error of the estimated number of real events per master recording in the station's leave-one-recording-out cross-check (0.08 = 8 %); `null` if it could not be checked |
| `quality.raw_error` | the same for the raw count (what the estimate has to beat) |
| `quality.auc` | area under the ROC curve of the held-out events of the cross-check; `null` without a cross-check |
| `quality.usable` | the station's own verdict: `est_error ≤ 0.30` and better than `raw_error` |

Suggested use on the hub (its decision, not part of the contract): leave out kinds with `est_error` `null` or above
0.30; weight a contribution by `(manual × 5 + master) × quality factor`, capped per station.

### JSON Schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://github.com/michaelpietzsch/vinyl-check/docs/vinyl-mind-protocol/v0/contribution",
  "type": "object",
  "additionalProperties": false,
  "required": ["type", "protocol", "profile_id", "feature_version", "features", "detector_version",
               "vinyl_check_version", "chain_class", "kinds"],
  "properties": {
    "type": {"const": "vinyl-mind/contribution"},
    "protocol": {"const": 0},
    "profile_id": {"type": "string", "pattern": "^[0-9a-f]{32}$"},
    "feature_version": {"type": "string", "pattern": "^[0-9]{1,4}$"},
    "features": {"type": "array", "minItems": 1, "maxItems": 64,
                 "items": {"type": "string", "pattern": "^[a-z0-9_]{1,40}$"}},
    "detector_version": {"type": "integer", "minimum": 1, "maximum": 100000},
    "vinyl_check_version": {"type": "string", "pattern": "^[0-9]+\\.[0-9]+\\.[0-9]+[0-9A-Za-z.+-]{0,32}$"},
    "chain_class": {"$ref": "#/$defs/chain_class"},
    "kinds": {
      "type": "object", "additionalProperties": false, "minProperties": 1,
      "properties": {"click": {"$ref": "#/$defs/kind"}, "tick": {"$ref": "#/$defs/kind"},
                     "pop": {"$ref": "#/$defs/kind"}}
    }
  },
  "$defs": {
    "chain_class": {
      "type": "object", "additionalProperties": false, "required": ["connection", "cartridge"],
      "properties": {"connection": {"enum": ["usb_turntable", "interface", "unknown"]},
                     "cartridge": {"enum": ["mm", "mc", "unknown"]}}
    },
    "weight": {"type": "number", "minimum": -1000, "maximum": 1000},
    "count": {"type": "integer", "minimum": 0, "maximum": 10000000},
    "error": {"type": ["number", "null"], "minimum": 0, "maximum": 100},
    "kind": {
      "type": "object", "additionalProperties": false,
      "required": ["intercept", "weights", "informative", "examples", "recordings", "quality"],
      "properties": {
        "intercept": {"$ref": "#/$defs/weight"},
        "weights": {"type": "array", "maxItems": 64, "items": {"$ref": "#/$defs/weight"}},
        "informative": {"type": "array", "maxItems": 64, "items": {"type": "boolean"}},
        "examples": {
          "type": "object", "additionalProperties": false, "required": ["master", "manual", "real", "false_alarm"],
          "properties": {"master": {"$ref": "#/$defs/count"}, "manual": {"$ref": "#/$defs/count"},
                         "real": {"$ref": "#/$defs/count"}, "false_alarm": {"$ref": "#/$defs/count"}}
        },
        "recordings": {
          "type": "object", "additionalProperties": false, "required": ["total", "master"],
          "properties": {"total": {"$ref": "#/$defs/count"}, "master": {"$ref": "#/$defs/count"}}
        },
        "quality": {
          "type": "object", "additionalProperties": false, "required": ["est_error", "raw_error", "auc", "usable"],
          "properties": {"est_error": {"$ref": "#/$defs/error"}, "raw_error": {"$ref": "#/$defs/error"},
                         "auc": {"type": ["number", "null"], "minimum": 0, "maximum": 1},
                         "usable": {"type": "boolean"}}
        }
      }
    }
  }
}
```

Rules beyond the schema (checked by both sides): `weights` and `informative` have exactly as many entries as
`features`; `features` equals the definition of `feature_version`; all numbers are finite; `real + false_alarm =
master + manual`; `recordings.master ≤ recordings.total`; `usable` implies a non-null `est_error ≤ 0.30` below
`raw_error`; the `profile_id` in the body equals the one in the path.

## Global model

What the hub publishes for one feature version, detector version and chain class, and what `GET /v0/models/current`
returns.

```json
{
  "type": "vinyl-mind/model",
  "protocol": 0,
  "version": "2026-10-24.1",
  "feature_version": "1",
  "features": ["log_strength", "amp_rel", "log_dur", "gc", "gc_missing", "music", "both_ch", "log_density",
               "amp_rel2", "strength_x_amp"],
  "detector_version": 1,
  "chain_class": {"connection": "any", "cartridge": "any"},
  "created": "2026-10-24T12:00:00Z",
  "kinds": {
    "click": {"intercept": -0.52, "weights": [0.61, -1.02, -5.1, 0.6, 0.0, 0.0, -0.4, 0.2, 0.8, -0.3],
              "profiles": 7, "examples": 12840}
  },
  "signature": {"alg": "ed25519", "key": "3f2a9c41…(64 hex)", "value": "…(b64u, 86 characters)"}
}
```

| Field | Meaning |
|---|---|
| `version` | the hub's version name of this model, 1–64 characters `[A-Za-z0-9._-]`; also the `ETag` |
| `chain_class` | as in a contribution, plus `any` for a model combined across classes |
| `kinds.*.weights` | in the fixed scaling of `feature_version`; a kind may be missing if the hub has no model for it |
| `kinds.*.profiles`, `.examples` | how many profiles and examples went into the kind (information for the station) |
| `signature` | see [Model signing](#model-signing); `key` is the hub's key fingerprint |

### JSON Schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://github.com/michaelpietzsch/vinyl-check/docs/vinyl-mind-protocol/v0/model",
  "type": "object",
  "additionalProperties": false,
  "required": ["type", "protocol", "version", "feature_version", "features", "detector_version", "chain_class",
               "created", "kinds"],
  "properties": {
    "type": {"const": "vinyl-mind/model"},
    "protocol": {"const": 0},
    "version": {"type": "string", "pattern": "^[A-Za-z0-9._-]{1,64}$"},
    "feature_version": {"type": "string", "pattern": "^[0-9]{1,4}$"},
    "features": {"type": "array", "minItems": 1, "maxItems": 64,
                 "items": {"type": "string", "pattern": "^[a-z0-9_]{1,40}$"}},
    "detector_version": {"type": "integer", "minimum": 1, "maximum": 100000},
    "chain_class": {
      "type": "object", "additionalProperties": false, "required": ["connection", "cartridge"],
      "properties": {"connection": {"enum": ["usb_turntable", "interface", "unknown", "any"]},
                     "cartridge": {"enum": ["mm", "mc", "unknown", "any"]}}
    },
    "created": {"type": "string", "format": "date-time", "maxLength": 40},
    "kinds": {
      "type": "object", "additionalProperties": false, "minProperties": 1,
      "properties": {"click": {"$ref": "#/$defs/kind"}, "tick": {"$ref": "#/$defs/kind"},
                     "pop": {"$ref": "#/$defs/kind"}}
    },
    "signature": {
      "type": "object", "additionalProperties": false, "required": ["alg", "key", "value"],
      "properties": {"alg": {"const": "ed25519"}, "key": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                     "value": {"type": "string", "pattern": "^[A-Za-z0-9_-]{86}$"}}
    }
  },
  "$defs": {
    "kind": {
      "type": "object", "additionalProperties": false, "required": ["intercept", "weights"],
      "properties": {
        "intercept": {"type": "number", "minimum": -1000, "maximum": 1000},
        "weights": {"type": "array", "maxItems": 64, "items": {"type": "number", "minimum": -1000, "maximum": 1000}},
        "profiles": {"type": "integer", "minimum": 0, "maximum": 10000000},
        "examples": {"type": "integer", "minimum": 0, "maximum": 1000000000}
      }
    }
  }
}
```

A model served by the hub always carries `signature`. A model file passed on by hand may come without one (see
[Package files](#package-files)).

## Check report

After a station fetched and checked a new global model it reports how the model does on its own master recordings,
per profile and kind, with `POST /v0/reports`. The hub withdraws a version that is worse on most reporting profiles.

```json
{
  "type": "vinyl-mind/report",
  "protocol": 0,
  "model_version": "2026-10-24.1",
  "feature_version": "1",
  "detector_version": 1,
  "vinyl_check_version": "1.1.0",
  "adopted": {"click": true, "tick": false},
  "profiles": [
    {"profile_id": "9b1f0c2e4d6a48b7a3c5e7f9012345ab",
     "kinds": {"click": {"recordings": 6, "local_error": 0.12, "global_error": 0.10, "prior_error": 0.09}}}
  ]
}
```

| Field | Meaning |
|---|---|
| `adopted` | per kind of the model: whether the station now uses it as the prior of its training |
| `recordings` | master recordings of the profile with at least 3 real events of the kind (the basis of `global_error`) |
| `local_error` | estimate error of the profile's own model in its leave-one-recording-out cross-check, without prior |
| `global_error` | estimate error of the global model applied directly to each master recording, without any training |
| `prior_error` | the cross-check again, with the global model as the prior of the training |

An estimate error is the mean over recordings of `|estimate − true| / true`, where `true` is the number of events the
master comparison classified as real and `estimate` the sum of the model's probabilities over the classified events.
Any of the three errors is `null` where it cannot be computed (too few examples or master recordings). A profile
without any master recording is left out; a report without profiles is not sent. A station may report the same model
version again (after *Use the global model* was switched off and on, it fetches and checks anew); the hub keeps the
latest report per profile and model version.

### JSON Schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://github.com/michaelpietzsch/vinyl-check/docs/vinyl-mind-protocol/v0/report",
  "type": "object",
  "additionalProperties": false,
  "required": ["type", "protocol", "model_version", "feature_version", "detector_version", "vinyl_check_version",
               "adopted", "profiles"],
  "properties": {
    "type": {"const": "vinyl-mind/report"},
    "protocol": {"const": 0},
    "model_version": {"type": "string", "pattern": "^[A-Za-z0-9._-]{1,64}$"},
    "feature_version": {"type": "string", "pattern": "^[0-9]{1,4}$"},
    "detector_version": {"type": "integer", "minimum": 1, "maximum": 100000},
    "vinyl_check_version": {"type": "string", "pattern": "^[0-9]+\\.[0-9]+\\.[0-9]+[0-9A-Za-z.+-]{0,32}$"},
    "adopted": {
      "type": "object", "additionalProperties": false,
      "properties": {"click": {"type": "boolean"}, "tick": {"type": "boolean"}, "pop": {"type": "boolean"}}
    },
    "profiles": {
      "type": "array", "minItems": 1, "maxItems": 32,
      "items": {
        "type": "object", "additionalProperties": false, "required": ["profile_id", "kinds"],
        "properties": {
          "profile_id": {"type": "string", "pattern": "^[0-9a-f]{32}$"},
          "kinds": {
            "type": "object", "additionalProperties": false,
            "properties": {"click": {"$ref": "#/$defs/check"}, "tick": {"$ref": "#/$defs/check"},
                           "pop": {"$ref": "#/$defs/check"}}
          }
        }
      }
    }
  },
  "$defs": {
    "error": {"type": ["number", "null"], "minimum": 0, "maximum": 100},
    "check": {
      "type": "object", "additionalProperties": false,
      "required": ["recordings", "local_error", "global_error", "prior_error"],
      "properties": {"recordings": {"type": "integer", "minimum": 0, "maximum": 100000},
                     "local_error": {"$ref": "#/$defs/error"}, "global_error": {"$ref": "#/$defs/error"},
                     "prior_error": {"$ref": "#/$defs/error"}}
    }
  }
}
```

## Endpoints

All paths are relative to the base URL. "Signed" means [request signing](#request-signing) is required. A station is
`pending` after joining until it is approved (directly with a valid invitation code, otherwise by the operator),
then `approved`; a revoked station is unknown to the hub.

| Method and path | Signed | Allowed for | Request | Success |
|---|---|---|---|---|
| `GET /v0/server` | no | anyone | – | 200 server info |
| `POST /v0/stations` | yes, with the new key | anyone (rate-limited per address) | join | 201 new, 200 already known |
| `GET /v0/stations/me` | yes | pending, approved | – | 200 station status |
| `DELETE /v0/stations/me` | yes | pending, approved | – | 204 |
| `PUT /v0/contributions/{profile_id}` | yes | approved | contribution | 200 |
| `DELETE /v0/contributions/{profile_id}` | yes | approved | – | 204 (also if there was none) |
| `GET /v0/models/current` | yes | approved | query, `If-None-Match` | 200 model with `ETag`, 304 |
| `POST /v0/reports` | yes | approved | report | 202 |

**`GET /v0/server`**

```json
{"type": "vinyl-mind/server", "protocol": 0, "name": "Example hub",
 "public_key": "<b64u, 43 characters>", "fingerprint": "<64 hex>",
 "feature_versions": ["1"], "join": {"invitation": true, "approval": true}}
```

`join.invitation`: invitation codes are accepted; `join.approval`: a station without a code waits for the operator.
The station shows `fingerprint` to the user and stores `public_key` when the join succeeds; from then on it verifies
every model with that key. It checks that `fingerprint` is the SHA-256 of `public_key`.

**`POST /v0/stations`** (join)

```json
{"type": "vinyl-mind/join", "protocol": 0, "public_key": "<b64u of the station's raw public key>",
 "invitation": "K7QM-2XPA", "vinyl_check_version": "1.1.0"}
```

`invitation` may be `null`. The request is signed with the key in the body, and `VM-Station` must be the SHA-256 of
that key: this proves the station holds the private key. Response:

```json
{"station": "<station id>", "status": "pending", "server_fingerprint": "<64 hex>"}
```

A known key answers 200 with its current status (joining again is harmless). A wrong or used invitation code is
`invitation_invalid`; a hub that only admits invited stations answers `invitation_required` without a code.

**`GET /v0/stations/me`**

```json
{"station": "<station id>", "status": "approved", "joined_at": "2026-10-24T12:00:00Z",
 "contributions": [{"profile_id": "9b1f…", "received_at": "2026-10-24T13:10:00Z"}]}
```

**`PUT /v0/contributions/{profile_id}`** answers

```json
{"profile_id": "9b1f…", "received_at": "2026-10-24T13:10:00Z"}
```

**`GET /v0/models/current?feature_version=1&detector_version=1&connection=interface&cartridge=mm`**

`feature_version` and `detector_version` are required; `connection` and `cartridge` give the chain class of the
station's active profile. The hub answers with the most specific published model for that feature and detector
version: the exact chain class, else `any`. Never a model of another feature or detector version. The `ETag` is the
model `version` in double quotes; with `If-None-Match` equal to it the answer is 304 without a body. `no_model` (404)
when nothing is published yet.

**`POST /v0/reports`** answers `{"received": true}` with 202.

**`DELETE /v0/stations/me`** (leave): the hub deletes the station's contributions and reports and forgets its key;
any later request with that key is `unknown_station`. A revoked station must create a new key to join again.

## Request signing

Every signed request carries these headers:

| Header | Content |
|---|---|
| `VM-Station` | the station id (hex SHA-256 of the raw public key) |
| `VM-Timestamp` | Unix time in seconds, decimal integer |
| `VM-Nonce` | 32 lowercase hex characters, random (128 bits), new for every request |
| `VM-Content-SHA256` | hex SHA-256 of the exact body bytes (of the empty string without a body) |
| `VM-Signature` | b64u of the 64-byte Ed25519 signature over the canonical request |

The **canonical request** is the UTF-8 encoding of these seven lines, joined by a single `\n`, without a trailing
newline:

```
VM1-ED25519
<method in upper case>
<path>
<VM-Content-SHA256>
<VM-Timestamp>
<VM-Nonce>
<VM-Station>
```

`<path>` is the path of the request URL exactly as sent, starting with `/` and including the base URL's path prefix,
followed by `?` and the query string exactly as sent if there is one. A hub behind a reverse proxy that removes a
prefix must restore it before checking (it knows its public base URL).

The hub checks in this order and answers with the first failure:

1. all five headers present and well-formed, otherwise `signature_missing` (401);
2. `VM-Content-SHA256` equals the SHA-256 of the received body, otherwise `digest_mismatch` (400);
3. `|hub time − VM-Timestamp| ≤ 300 s`, otherwise `clock_skew` (401, with `server_time` in the error);
4. the station is known (for joining: `VM-Station` is the SHA-256 of `public_key` in the body), otherwise
   `unknown_station` (401);
5. the signature verifies with the station's public key, otherwise `bad_signature` (401);
6. the nonce was not used by this station within the last 600 s, otherwise `replay` (401). The hub records a nonce
   only after the signature verified, so nobody can use up a station's nonces, and keeps it for at least 600 s
   (twice the allowed skew).

A captured request therefore cannot be replayed (nonce), delayed beyond five minutes (timestamp), retargeted (method
and path are signed) or altered (body hash). Responses are protected by TLS; models are additionally signed.

The station does not correct its clock from the hub: on `clock_skew` it shows the difference and asks the user to fix
the computer's time.

## Model signing

The hub signs every global model with its Ed25519 key:

1. Take the model object without the member `signature`.
2. Serialise it as **canonical JSON**: members sorted by key (code point order), no whitespace (separators `,` and
   `:`), UTF-8 without escaping non-ASCII characters, no NaN or infinity. Numbers are written as Python's standard
   `json` module writes them: integers as digits, floats as the shortest string that reads back to the same value
   (`repr`), with `.0` for integral floats and exponent notation below 1e-4 (`5e-05`) and from 1e16. In Python:
   `json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")`.
3. Sign these bytes; put `{"alg": "ed25519", "key": <hub fingerprint>, "value": <b64u signature>}` into `signature`.

The station verifies **before using anything** in the model: the response is at most 64 KiB and valid JSON; `signature`
is present with `alg` `ed25519`; `signature.key` equals the fingerprint of the key it accepted when joining; the
signature verifies over the canonical JSON of the rest with that key. Only then does it check the schema, the
protocol, feature version, feature list and detector version against its own, and evaluate the weights. A model that
fails any step is discarded and the error shown; the station keeps its previous model. Changing the hub's key means
every station has to accept the new key explicitly (by joining again).

The JSON on the wire may be formatted in any way; the signature covers the canonical form of the parsed object. As
long as both sides are Python, this is exact. A hub in another language must reproduce Python's float formatting, or
the protocol moves to RFC 8785 (JSON Canonicalization Scheme) in a later version.

## Errors

Every error answer has this body (the `message` is English and for logs and people; stations act on `code`):

```json
{"error": {"code": "invalid_payload", "message": "kinds.click.weights: expected 10 entries",
           "details": ["kinds.click.weights: expected 10 entries"]}}
```

| Code | HTTP | Meaning | Station's reaction |
|---|---|---|---|
| `signature_missing` | 401 | signing headers missing or malformed | bug: show the error |
| `digest_mismatch` | 400 | body hash does not match | bug or tampering: show |
| `clock_skew` | 401 | timestamp more than 300 s off; the error has `server_time` | ask the user to fix the clock |
| `unknown_station` | 401 | key not known (never joined, left, or revoked) | show *not joined*; joining again is possible with a new key |
| `bad_signature` | 401 | signature does not verify | bug or tampering: show |
| `replay` | 401 | nonce already used | retry once with a new nonce |
| `pending` | 403 | the station waits for approval | show *waiting for approval*, ask again later |
| `invitation_required` | 403 | the hub only admits invited stations | ask for an invitation code |
| `invitation_invalid` | 403 | code unknown, expired or used up | show |
| `profile_mismatch` | 400 | `profile_id` in path and body differ | bug: show |
| `invalid_payload` | 422 | schema or plausibility check failed; `details` lists the problems | show, do not retry the same payload |
| `unsupported_version` | 409 | protocol, feature version or detector version not accepted by the hub | show; contributions wait for an update |
| `too_large` | 413 | body above the limit | bug: show |
| `rate_limited` | 429 | too many requests; `Retry-After` in seconds | wait as asked |
| `no_model` | 404 | no published model for this feature and detector version | nothing to do, ask again later |
| `not_found` | 404 | unknown path | show |
| `server_error` | 500, 503 | the hub has a problem; 503 may carry `Retry-After` | retry later |

## Limits

| What | Limit |
|---|---|
| request body | 16 KiB (a contribution is about 2 KiB) |
| response body, model, package file | 64 KiB (the station refuses more) |
| weights and intercepts | finite, within ±1000 |
| counts | integers 0 … 10⁷ (model examples up to 10⁹) |
| errors | 0 … 100 or `null`; AUC 0 … 1 or `null` |
| profiles per report | 32 |
| clock skew | ±300 s; nonces are kept 600 s |
| station requests (suggested hub limit) | 60 per hour per station, joins 5 per hour per address |

## Versioning

- `protocol` is 0 in every payload while this is a draft. A hub rejects payloads with a protocol it does not speak
  (`unsupported_version`). Incompatible changes after the draft get a new protocol number and a new path prefix
  (`/v1`); the hub may serve both during a transition.
- **Never mix feature versions.** Contributions, global models and reports are kept and combined strictly per
  `feature_version`. A station only uses a model with exactly its own `feature_version`, feature list and
  `detector_version`; a model or package with another version is refused, not converted.
- `detector_version` (`vinylcheck/detect.py`: `VERSION`) changes when the detector reports different events or values
  for the same audio. Weights of different detector versions are not combined either.
- `vinyl_check_version` is information for the operator (for example to ask stations with a known bug to update).
- A change to `FEATURES`, `features()` or `FIXED_SCALING` in vinyl-check needs a new feature version, documented
  here.

## Station behaviour

How vinyl-check behaves, so the hub can expect it:

- **Switches.** *Contribute* and *Use the global model* are separate and off by default. With both off the station
  sends nothing at all (joining and leaving are explicit actions of the user). Contributions are sent only while
  *Contribute* is on; models are fetched and reports sent only while *Use the global model* is on.
- **Never during a recording.** No request leaves the station while it records; the work waits for the next round.
- **When.** After a retraining of the learning model (judgements, master comparisons), at most once per hour per
  profile, and only if the payload changed since the last successful send (compared by its SHA-256). A profile that
  was deleted on the station is withdrawn with `DELETE /v0/contributions/{profile_id}`. The current model is asked for
  every six hours with `If-None-Match`, so usually without a body. *Send now* in the settings does all of it at once.
- **Retries.** Connection errors, timeouts, 429, 502, 503 and 504 are retried up to three times within a round, after
  1, 2 and 4 seconds (or `Retry-After`, up to 60 s). Other errors are not retried with the same payload. After a failed
  round the station waits 2, 4, 8 … minutes, at most six hours.
- **Timeouts.** 10 seconds per request. Redirects are not followed (a signed request is bound to its path, and a
  redirect could leave HTTPS).
- **Adopting.** A newly fetched model is checked on the station's own master recordings. Per kind it becomes the
  prior of the station's training if the cross-check with the prior is not worse than without (`prior_error ≤
  local_error`), or if the station has no cross-check of its own yet: the model is then only a starting point, and the
  station's estimates still need its own cross-check before they are used. Kinds that fail keep the local model.
- **Feature version.** A station works in exactly one feature version, the configured one (`feature_version`,
  default `"1"`): its contributions, model requests and reports carry it, and models, package files and priors of
  another version are refused or ignored. After a switch it sends its contributions anew in the new version.
- **Keys.** The station's private key is a file in its state directory, readable only by the service user (mode
  600), never stored in the database and never logged. *Leave* deletes it; joining again creates a new key and
  therefore a new station id.
- **Changing the server.** The joined hub's key belongs to its URL. To use another hub, the station leaves the old one
  first (or forgets it locally when it is gone) and joins the new one, accepting its key.

## Package files

Before a hub exists (stage 1), knowledge can travel as files:

- **Export** writes a profile's contribution to a file: exactly the contribution object of the
  [Contribution](#contribution) section, pretty-printed. A hub can accept such a file as a contribution.
- **Import** reads a contribution or a global model file and, after the user confirmed what it is, uses its weights as
  the prior of the station's training (the same way as an adopted global model). The file is checked against the
  schema of its type; its `feature_version`, feature list and `detector_version` must match the station's. A model
  file may come without `signature`; if it has one from the accepted hub key, the station says it is verified. A
  contribution of one of the station's own profiles is refused (it would only feed the station's knowledge back to
  itself).
