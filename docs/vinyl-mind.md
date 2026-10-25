# Vinyl-Mind on the station

[Vinyl-Mind](https://github.com/michaelpietzsch/vinyl-mind) is the hub that combines what the learning models of
many stations have learned; vinyl-check is the station. This page says what the station does and what it sends. The
contract itself is **the hub's specification**, and where this page and the specification differ, the specification
is right:

| In the Vinyl-Mind repository | Content |
|---|---|
| `spec/README.md` | feature and detector versions, kinds, coefficients and the shared scaling, what a contribution contains, chain classes |
| `spec/signing.md` | station keys, fingerprints, signed requests, signed models (JWS), with a test vector |
| `spec/v1/features.json` | feature version 1: features in their order, the fixed shared scaling, the coefficient bound |
| `spec/v1/contribution.schema.json`, `global-model.schema.json`, `check-report.schema.json` | the documents, as JSON schemas; `spec/v1/examples/` valid and invalid examples |
| `docs/api.md` | the endpoints under `/v1`, their answers and error codes |
| `docs/connect.md` | connecting a station by signing in with an account (PKCE, device flow, binding) |
| `docs/aggregation.md` | how contributions are combined, published and withdrawn, and why the prior is as it is |
| `docs/station-side.md` | the list of what a station implements |

vinyl-check 1.1.0 implements **protocol version 1** (API `/v1`, feature version 1, `docs/connect.md` as of the hub's
commit `953f648`). The station's code is `vinylcheck/vinylmind.py`; a copy of `spec/v1` in `tests/vinyl_mind_spec/`
lets the tests check every document the station sends against the hub's schemas.

## Versions

| | Value | Where |
|---|---|---|
| client | `vinyl-check/<version>`, e.g. `vinyl-check/1.1.0`: the header `Vinyl-Mind-Client` on every request and `client` in every contribution | `vinylmind.CLIENT` |
| feature version | 1 (the default); 2 is the station's own proposal (below), sent only to a hub that lists 2 in `feature_versions` of `GET /v1/server`, otherwise the settings say why nothing is sent | configuration `feature_version` |
| shared scaling | exactly `spec/v1/features.json`; a test compares them | `learning.FIXED_SCALING` |
| detector version | `"1"`, raised whenever the detector reports other events or values on the same audio | `detect.VERSION` |

A hub can accept only some client and detector versions. When it answers `client_not_accepted`, the station stops
contributing and asks the user to update; status and disconnecting keep working.

## Connecting

The server is `https://api.vinylcheck.net` unless the configuration says otherwise (`vinyl_mind_url`, for a group of
stations with a self-hosted hub; one place in the code: `vinylmind.DEFAULT_VINYL_MIND_URL`). Plain HTTP is accepted
only for this computer itself (tests, a hub on the same machine). *Check server* asks `GET /v1/server`, checks that the
public key matches its fingerprint, shows the fingerprint, and warns when this computer's clock is more than a minute
off the hub's (signed requests are refused beyond five minutes) or when the hub does not accept this client.

**With an account** (the hub names a `connect_url`): *Connect with Vinyl-Mind*, as desktop apps connect to an online
account (`docs/connect.md`):

- **In the browser on this computer** (flow A; the desktop app, or the interface opened on `127.0.0.1`): the station
  makes a new key pair for the hub, a `state` (16 random bytes) and a PKCE `code_verifier` (32 random bytes), and
  opens the account service's page with `response_type=code`, `client_id=vinyl-check`, `client`, `redirect_uri`,
  `state`, `code_challenge` (S256), `station` (its key's fingerprint) and `lang`. The desktop app opens it in the
  system browser, not in its own window. After signing in and confirming, the browser comes back to
  `http://127.0.0.1:<port>/vinyl-mind/callback` (`[::1]` when the engine listens there; never `localhost`). The
  callback accepts only the `state` of the flow the station opened, once, within ten minutes, and only from this
  computer; it needs no access code, and it completes nothing else. It also handles `error=access_denied`.
- **With a code** (flow B; a Raspberry Pi used from a phone, or when no callback arrives): the station asks the
  account service for a device code and shows the short user code, the address and a QR code. It polls the token
  endpoint every `interval` seconds (RFC 8628: `authorization_pending` keeps polling, `slow_down` adds five seconds,
  `rate_limited` waits as told, `expired_token`, `access_denied` and `invalid_grant` end the flow) until the code
  expires.

The token endpoint answers `invitation`, `binding`, `server_url` and `server_fingerprint`. The station continues only
if `server_url` is the configured hub and `server_fingerprint` the key that `GET /v1/server` presents; then it joins
with `POST /v1/join` and the invitation and binding. A one-time code is exchanged once; when the account service
refuses it, the user starts again. A join refused for a while (the hub's daily limit, a recording) is tried again by
itself with the same invitation, kept in memory only. *Reconnect* makes a new key pair and goes through the flow again
while the station stays connected with the old key; the old station is then left (and the account service
disconnects it anyway).

The names of this flow (fields, paths, the callback) are kept in one place, `vinylmind.Account`.

**Without an account** (a self-hosted hub): *Other ways to join* takes an invitation code from the hub's operator, or
none (then the operator approves the station; the settings ask every few seconds). The user confirms that the shown
fingerprint matches the one the operator publishes, and the join stops if the hub presents another key. The
configuration key `vinyl_mind_invite_url` adds a link to where the operator hands out codes.

**Secrets.** The one-time code, the PKCE verifier, the device code, the invitation and the binding stay in the
engine's memory: never stored, logged or sent to the interface, which only sees the progress (waiting, the user code,
connected, an error). The station's private key is a file per hub in the state directory (`vinyl-mind/<hash>.key`,
mode 600, directory 700), never in the database, a log, an export or an API answer; disconnecting deletes it, a
revoked station connects again with a new one.

**States** shown in the settings: connected, waiting for approval, revoked by the server, server key changed (nothing
more is sent until the user disconnects and connects again), station key missing, client version not accepted, server
unreachable, feature version not combined by this hub.

## What the station sends

| Request | When | Body |
|---|---|---|
| `GET /v1/server` | *Check server*, connecting, once a day while connected | none (unsigned) |
| `POST /v1/join` | connecting | `public_key`, and `invitation` and `binding` when there are |
| `GET /v1/me` | while waiting for approval | none |
| `PUT /v1/profiles/{id}` | after a retraining, for each profile whose contribution changed | a contribution |
| `DELETE /v1/profiles/{id}` | a profile was deleted or has nothing to contribute any more | none |
| `GET /v1/models?feature_version=1&detector_version=1&chain_class=…` | every six hours with *Use the global model* (with `If-None-Match`, usually 304) | none |
| `GET /v1/models/{version}` | the model in use is no longer the published one: was it withdrawn? | none |
| `PUT /v1/profiles/{id}/checks/{version}` | after checking a model, per profile with master recordings | a check report |
| `DELETE /v1/me` | *Disconnect* | none |

Every request but `GET /v1/server` is signed as `spec/signing.md` says, with a new nonce each time; bodies are compact
JSON sent as bytes with `Content-Length`. Nothing is sent while a recording runs, and nothing at all while both
switches are off. Failures are retried with growing pauses, and a `Retry-After` is honoured.

**A contribution** per turntable profile: the learning model trained on the recordings of that profile only, without
any prior (the hub must not get its own result back). A kind is included once its model is fitted and cross-checked
on at least two recordings with a master, usable or not (the hub filters by the self-check), and while its numbers
are within the specification's bounds:

```json
{"spec": "vinyl-mind/contribution", "feature_version": 1, "detector_version": "1",
 "client": {"name": "vinyl-check", "version": "1.1.0"},
 "chain": {"source": "turntable_interface", "cartridge": "mc"}, "trained_at": 1793000000,
 "kinds": {"click": {"coef": [0.42, 1.31, 0.88, -0.12, 0.95, -0.21, -0.35, 0.18, -0.44, -0.27, 0.31],
                     "n_real": 812, "n_false": 1490, "n_manual": 140, "n_master": 2162,
                     "master_recordings": 10, "cv_err_est": 0.04, "cv_err_raw": 0.15, "auc": 0.91}}}
```

- `coef`: the intercept and one weight per feature in the shared scaling, converted exactly from the station's own
  standardisation (`learning.to_coef`, the formula of `spec/README.md`).
- `n_manual` counts judgements by hand, `n_master` master classifications; `master_recordings`, `cv_err_est`,
  `cv_err_raw` and `auc` are the cross-check of `learning.train`.
- `chain`: two fields of the turntable profile, asked in the profile dialog (*Connection*: USB turntable or turntable
  with audio interface; *Cartridge type*: MM, MC or unknown), guessed from the profile's device and cartridge until
  set.
- `trained_at` changes only when the content does; an unchanged contribution is not sent again.
- The profile ID is in the address only: 16 random bytes in base64url, created with the profile, never derived from
  names or hardware.

**Never sent:** audio, features of single events, record titles, notes, Discogs data, profile names, devices, serial
numbers, credentials. *What is sent* shows the exact documents before anything leaves the station.

## Using the global model

The station asks for the model of its active profile's chain class (the hub falls back to the model over all classes).
For a new candidate or published model it

1. verifies the JWS with the accepted server key **before** looking at the payload (`alg` EdDSA, `typ`
   `vinyl-mind-model+json`, `kid` the accepted fingerprint, the signature, and the payload's `server` equal to `kid`),
2. validates it against the schema and checks that feature version, features, shared scaling and detector version are
   its own,
3. checks it on its own master recordings, per kind and on the same recordings: the local cross-check, the global
   model as it is, and the local cross-check with the global model as the prior,
4. reports the local and the global error per profile (`PUT …/checks/{version}`), and
5. adopts a **published** model as the prior of the kinds where the cross-check with it is not worse than without (or
   where the station has no cross-check of its own yet: then it is only a starting point, and estimates are still used
   only once the station's own cross-check makes the kind usable). Candidates are checked and reported, not adopted.

With a prior, the training fits in the shared scaling and pulls every coefficient, the intercept included, towards the
global ones with strength 100 (`learning.PRIOR_L2`), as the hub's simulation found best for profiles with few
judgements; without a prior nothing changes. A model the hub withdraws is dropped at the next poll, and the station
falls back to its local model or the newest published one. Switching *Use the global model* off forgets the fetched
models.

## Package files

Without a hub, knowledge can travel as files: *Export* saves a profile's contribution (the document above), *Import*
takes a contribution or a global model (its payload, or the signed JWS as the hub delivers it, verified when it comes
from the connected hub) and uses it as the prior after the user confirmed what it is.

## Feature version 2 (the station's proposal)

Not part of the hub's specification yet. With `feature_version = "2"` the features are relative to the recording's
turntable profile (definitions and reasons in [measurement](measurement.md#learning)); features 0–3, 5, 6, 8 and 9
are those of version 1 with its scaling, the others:

| # | Name | Definition | Centre | Scale |
|---|---|---|---|---|
| 4 | `gc_missing` | as in version 1 | 1.0 | 0.5 |
| 7 | `log_density_net` | ln(1 + max(0, n − λ)), λ = the profile's click floor per second × 1 s | 0.5 | 0.6 |
| 10 | `amp_ref` | event level in dBFS minus the profile's reference level | −20.0 | 10.0 |
| 11 | `chain_margin` | (event level − chain noise) / 10, clipped to 0 … 4 | 4.0 | 1.0 |

Only recordings whose profile has a measured zero point count in a version-2 contribution. Versions 1 and 2 are never
mixed: not in contributions, models, reports, package files or priors. A coefficient of a feature that did not vary
in a profile's data is 0, and averaging it is only exact where the feature sat at its centre; version 2 therefore
centres the features that are constant on many profiles at that value.

## Tests

`tests/test_vinylmind.py` runs the station against a fake hub and a fake account service that implement the
specification on their own: the signing test vector, JWS refusals, schema validation of every document, both sign-in
flows with their error cases, contributions, models, reports, withdrawal, feature version 2. `tests/test_vinylmind_hub.py`
runs the station against a real hub binary when `VINYL_MIND_BIN` names one (`go build -o <dir>/vinyl-mind
./cmd/vinyl-mind` in the Vinyl-Mind repository), with the account service served over HTTPS with a throwaway
certificate; it is skipped otherwise, and `VINYL_CHECK_SKIP_HUB=1` skips it always.
