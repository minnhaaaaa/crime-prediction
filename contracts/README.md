# Contract Index

JSON Schemas in `contracts/schemas/` are the authoritative integration boundary. The canonical product semantics and cross-field rules are in `docs/PHASE1_CONTRACTS.md`.

Every schema has a synthetic fixture with the same basename under `contracts/fixtures/`. Fixtures demonstrate shape only; they are not observed events, detector results, forecasts, or measured performance.

## Phase 1 product contracts

| Schema | Visibility | Boundary |
|---|---|---|
| `camera-source.schema.json` | restricted | recorded/live tenant source with secret location/connection references |
| `video-asset.schema.json` | restricted | uploaded recording or live segment metadata and retention |
| `candidate-detection.schema.json` | reviewer-only | unconfirmed Reka Vision candidate and expiring evidence reference |
| `candidate-review.schema.json` | restricted audit | immutable confirmation/rejection decision |
| `coverage-snapshot.schema.json` | aggregate/public-safe after authorization | measured source availability for one interval |
| `incident-event.schema.json` | restricted | confirmed canonical event envelope |
| `feature-row.schema.json` | internal | labelled historical training/evaluation row |
| `forecast-feature-row.schema.json` | internal | unlabelled future inference row |
| `forecast.schema.json` | public after suppression | operational future aggregate forecast |
| `tenant-context.schema.json` | internal | server-derived request tenant and role context |
| `api-error.schema.json` | public | typed safe error response |

## Review 3 voice-notification contract

The generated `contracts/openapi.json` document is the single public contract
for response contacts, dispatch previews, dispatch cases and their embedded call
attempts. In particular, the browser DTOs are `ResponseContactView`,
`DispatchPreviewView`, `DispatchCaseView` and `DispatchAttemptView`. Do not add
parallel JSON Schemas for these DTOs: doing so creates two independently
versioned definitions for the same HTTP response.

The public contract deliberately omits tenant IDs, callable destinations,
secret references, provider call IDs, callback tokens and raw provider events.
Callable destinations live behind restricted secret references. Durable call
events are internal audit/storage records, not a browser endpoint. A dispatch
case can be created only after an immutable confirmed review promoted the
referenced incident and a reviewer explicitly authorized the call.

## Existing model and AI artifact contracts

| Schema | Boundary |
|---|---|
| `tenant.schema.json` | tenant identity/lifecycle metadata |
| `data-source.schema.json` | legacy structured-event source definition |
| `ingestion-run.schema.json` | replay/live processing status |
| `feature-table-manifest.schema.json` | historical feature artifact provenance |
| `prediction.schema.json` | legacy held-out evaluation prediction; not the public operational forecast |
| `model-bundle.schema.json` | fitted estimator metadata and payload integrity |
| `model-run-manifest.schema.json` | split, selection, and artifact provenance |
| `evaluation-report.schema.json` | validation/test metrics and slices |
| `model-card.schema.json` | intended use, performance, and limitations |
| `reka-fact-bundle.schema.json` | deterministic aggregate AI facts |
| `reka-source-mapping.schema.json` | human-reviewed mapping proposal |
| `reka-insight.schema.json` | fact-cited aggregate explanation |
| `reka-candidate-proposals.schema.json` | exact five-field unconfirmed video-model output array |

## Security invariants

- Authentication creates tenant context; payload tenant IDs never grant access.
- Raw footage, evidence, exact locations, coordinates, event identifiers, and secret references never cross the public forecast boundary.
- Only confirmed review decisions promote candidate detections to incident events.
- Historical feature rows have labels; future forecast feature rows never do.
- Suppressed operational forecasts use null estimates, a `suppressed` band, and no drivers.
- Reka Vision may receive tenant-approved video and produce unconfirmed candidates; it never receives exact coordinates, event IDs, credentials, secret references, or cross-tenant context.
- Reka Chat receives only approved aggregate forecast facts and never calculates forecast values.
- Candidates, forecasts, and rejected reviews cannot create dispatch cases.
- Voice dispatch is explicitly human-authorized and limited to two primary
  attempts followed by one supervisor attempt.
- Browser dispatch records contain masked destinations only; secret references,
  full phone numbers, provider call IDs, and callback tokens are prohibited.

## Versioning and changes

- Semantic versions are carried in each payload.
- Additive optional changes increment the minor version.
- Removed fields, renamed fields, or changed meaning require a new major version.
- Consumers reject unsupported major versions.
- Update fixture, schema, producers, consumers, generated types, and tests as one coordinated migration.
- Shared contract changes require review by a teammate outside the author's primary area.

## Changelog

### 2026-08-31 — exact descriptive Reka candidate observations

- Added the strict `reka-candidate-proposals.schema.json` provider boundary:
  `offset_seconds`, `category`, concrete `event_type`, neutral bounded
  `description`, and `confidence`, with no wrappers or extra properties.
- Advanced `candidate-detection.schema.json` to 1.1.0 so the event type and
  description survive persistence, the API, and reviewer UI.
- Short video now uses native Vision Quick Tag description followed by Flash
  structured output; multimodal Edge remains the fallback with the
  live-compatible nested video media shape.
- Candidate event types use a bounded acute-event taxonomy with enforced
  event/category pairings, complete-sentence descriptions, and authoritative
  clip-duration bounds.
- The versioned prompt distinguishes visible harmful force from hand games,
  rock-paper-scissors, play, sport, dancing, gestures, and non-physical
  arguments. All output remains unconfirmed until human review.
- Existing Postgres candidates receive an explicit legacy-unclassified
  backfill; the migration never invents an event description.

### 2026-08-30 — unified video input controls

- Added a non-production, tenant-scoped simulated capture mutation whose
  bounded synthetic MP4 enters the existing restricted live-segment pipeline.
- The console now exposes recorded upload, secret-referenced live connector,
  and simulated-live inputs without accepting raw camera credentials or URLs.
- Shared endpoint-contract review is required before this addition is frozen.

### 2026-08-30 — fail-closed video re-analysis

- Missing candidate fields now have a distinct safe error classification; only
  bounded, value-free structural diagnostics are retained internally.
- Added an authenticated, tenant-scoped re-analysis mutation that creates fresh
  durable work and never reopens or mutates a dead-letter job.
- Shared endpoint-contract review is required before this addition is frozen.

### 2026-08-30 — human-authorized voice escalation

- Added browser-safe response-contact, dispatch-preview, dispatch-case and
  embedded call-attempt DTOs to the generated OpenAPI contract.
- Removed proposed parallel JSON Schemas for those HTTP DTOs; generated OpenAPI
  is authoritative for the browser integration, while durable call events stay
  internal.
- Dispatch triggers are restricted to confirmed review promotions and require
  explicit call authorization.
- The escalation contract fixes the maximum at three logical calls: two to the
  primary contact and one to the supervisor.
- Full callable destinations remain outside browser contracts and are stored
  through restricted `secret://` references.
- This shared-contract addition requires review by a teammate outside the
  schema author's primary area.

### 2026-08-30 — calibrated operational model artifacts

- Model-run manifests now record validation-only probability-calibration and
  model/data/temporal uncertainty sidecars alongside the fitted estimator.
- Top-k cell capture is evaluated independently within each forecast
  window/category group.
- This shared-contract addition requires review by a teammate outside Person 2.

### 2026-08-30 — allowlisted near-live HLS demo transport

- Added `hls` as an explicit live-camera transport. Public HLS sources require a
  restricted `endpoint_ref`; RTSP and ONVIF continue to require both endpoint
  and credential references.
- Browser clients cannot supply HLS URLs. The demo adapter resolves only a
  server allowlist and emits bounded `live_segment` assets through the same
  Reka Vision and candidate-review boundary as recorded uploads.
- Shared-contract review is still required before this addition is frozen.

### 2026-08-29 — Phase 1 implementation target

- Added recorded/live camera, video asset, candidate detection, immutable review, and coverage contracts.
- Separated labelled historical features from unlabelled future forecast features.
- Added an operational forecast contract distinct from held-out evaluation predictions.
- Defined server tenant context, product roles, and typed API errors.
- Defined suppression as null/unavailable rather than numeric zero risk.
- Selected Reka Vision as the managed video upload/index/search/Q&A/tagging/highlight and candidate-proposal layer, using the server-only shared `REKA_API_KEY`; numeric future forecasts remain local and deterministic.

Status remains implementation target until the required teammate review is recorded.
