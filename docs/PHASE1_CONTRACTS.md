# Phase 1 Contract Freeze

Status: **implementation target; teammate review required before final freeze**

Contract date: 2026-08-29

This is the canonical integration guide for the recorded-video and live-camera product. JSON Schemas in `contracts/schemas/` are authoritative when prose and examples disagree.

## Product language

The product has three distinct records. The UI, API, logs, and documentation must never merge their meanings.

| Record | Meaning | May be public? |
|---|---|---|
| Candidate detection | Reka Vision found a possible safety incident in tenant-approved footage; it is unconfirmed and not a declaration of crime | Reviewer-only metadata and expiring evidence |
| Confirmed incident | A human confirmed a candidate or an approved incident source supplied a record | Only after H3/time/category aggregation |
| Forecast | A model estimates future aggregate incident volume for an H3 cell and time window | Yes, after suppression |

The system does not identify people, infer guilt or intent, expose victim addresses, or recommend enforcement.

## Canonical flow

```text
Recorded upload or tenant-owned live camera
                  |
                  v
        camera-source + video-asset
                  |
                  v
       FastAPI -> Reka Vision
      upload / index / Q&A / tags
                  |
                  v
   restricted candidate-detection
                  |
                  v
       immutable candidate-review
          | confirmed only
          v
 restricted incident-event + coverage-snapshot
                  |
                  v
 historical feature-row / future forecast-feature-row
                  |
                  v
        operational forecast
                  |
                  v
     tenant-authenticated API and UI
```

## Contract ownership and visibility

- Every persisted record is tenant-scoped.
- `tenant_id` in a payload is routing and audit metadata, never authorization proof.
- The server derives `tenant-context` from authenticated claims. Clients cannot submit or override it.
- Exact camera location, credentials, raw video, evidence clips, raw coordinates, event identifiers, and candidate detections are restricted.
- One server-side `REKA_API_KEY` authorizes backend Reka Vision and Reka Chat calls. Browsers never receive or use it.
- Public forecast endpoints return only `forecast.schema.json` payloads after authorization, bounding, and suppression.
- Every schema has a matching synthetic fixture with the same basename.

## Frozen schemas

### Camera and footage

- `camera-source.schema.json` registers `recorded_video` or `live_camera` sources. Exact locations and endpoints use `secret://` references.
- Recorded sources accept uploaded assets. Live sources use RTSP, ONVIF, or an
  allowlisted public HLS endpoint through secret references. RTSP/ONVIF require
  credential references; public HLS sources do not. Browsers never supply raw
  stream URLs.
- `video-asset.schema.json` is restricted metadata for an uploaded file or live segment. Its restricted storage reference resolves to the tenant-mapped Reka `video_id`; the public API never returns it. The maximum contract size is 10 GiB; Reka/account/runtime quotas may be lower.
- Raw media is never stored in Git, logs, analytics exports, model artifacts, or public APIs.
- Only tenant-owned, lawfully obtained, explicitly approved video may be uploaded to Reka Vision. Reka deletion is part of the retention workflow.

### Detection and review

- `candidate-detection.schema.json` 1.1 contains the proposed category, concrete visible `event_type`, bounded neutral Reka `description`, analysis confidence/version, evidence reference, and review state. The description is AI-generated and unconfirmed.
- `reka-candidate-proposals.schema.json` freezes the exact provider boundary as an array of five-field objects: `offset_seconds`, `category`, `event_type`, `description`, and `confidence`. Wrapper objects and extra fields are rejected.
- Confidence is Reka analysis confidence, not the probability that a crime occurred.
- `candidate-review.schema.json` is immutable. A detection receives at most one final decision.
- Only the `reviewer`, `tenant_admin`, or explicitly audited `platform_operator` role may decide.
- A confirmed decision requires a canonical category and deterministic `promoted_external_event_id`.
- A rejected decision requires a reason and never creates an `IncidentEvent`.

### Coverage

`coverage-snapshot.schema.json` measures one source over one feature interval. Implementations must enforce:

```text
0 <= detector_available_seconds <= processable_seconds
   <= connected_seconds <= expected_seconds

coverage_ratio = detector_available_seconds / expected_seconds
```

Coverage is unknown until measured; it must never default silently to `1.0`. Aggregate cell coverage must use a documented deterministic weighting rule and record its version.

### Historical training versus future inference

- `feature-row.schema.json` remains the labelled historical training/evaluation row. It includes `event_count` for its interval.
- `forecast-feature-row.schema.json` is the operational future row. It intentionally excludes `event_count` because the future outcome is unknown.
- Both use the prediction key `(tenant_id, cell_id, interval_start, category)`.
- Every predictor must use information available strictly before `interval_start`; `data_as_of < interval_start` is mandatory in application validation.

### Operational forecast

`forecast.schema.json` replaces `prediction.schema.json` at the public API boundary.

- `prediction.schema.json` remains a legacy model-evaluation artifact until existing consumers migrate.
- A forecast window must start after `data_as_of` and use the same interval length as its model bundle.
- `expected_count` and `occurrence_probability` have separate intervals and named methods.
- Occurrence probabilities carry the fitted calibration version, or an explicit method stating why calibration is unavailable.
- `coverage_ratio`, model/data/feature versions, generation time, and data freshness are mandatory.
- Suppressed forecasts expose null numeric estimates, a `suppressed` risk band, no drivers, and a reason. Suppression must never be represented as zero risk.
- Drivers describe associations, not causes.

## Roles

| Role | View forecasts | Upload/register source | Review candidates | Manage tenant |
|---|---:|---:|---:|---:|
| `viewer` | yes | no | no | no |
| `reviewer` | yes | no | yes | no |
| `tenant_admin` | yes | yes | yes | yes |
| `platform_operator` | audited support only | audited support only | audited support only | platform operations |

Every API request receives a server-created `tenant-context.schema.json`. A platform operator must still have an explicit, audited active tenant context; platform status is not an unbounded cross-tenant query permission.

## API conventions

- All timestamps are UTC at API and storage boundaries.
- All failures use `api-error.schema.json` and a stable snake-case code.
- Mutation endpoints require an idempotency key and emit an audit event.
- Collection endpoints are bounded and paginated.
- Browser requests never include `tenant_id` as a query parameter or writable field.
- Evidence URLs are short-lived, reviewer-authorized references; APIs never return `secret://` values.
- Video uploads go to FastAPI; FastAPI calls Reka Vision using `REKA_API_KEY`. Direct browser-to-Reka calls using the secret are prohibited.

The initial endpoint surface is:

```text
GET    /health
GET    /v1/me/tenants
PUT    /v1/me/active-tenant/{tenant_id}
GET    /v1/metadata
GET    /v1/sources
POST   /v1/sources/recorded-video
POST   /v1/sources/live-camera
POST   /v1/video-assets/uploads
GET    /v1/ingestion/runs/{run_id}
POST   /v1/ingestion/runs/{run_id}/reanalyze
POST   /v1/demo/simulated-cctv/captures
GET    /v1/candidate-detections
GET    /v1/candidate-detections/{detection_id}/evidence
POST   /v1/candidate-detections/{detection_id}/review
GET    /v1/coverage
GET    /v1/forecasts?window_start=...&category=...&bbox=...
GET    /v1/forecasts/{forecast_id}
GET    /v1/model-card
```

## Cross-field validation not expressible in JSON Schema

Services and tests must enforce:

- start timestamps precede end timestamps;
- `data_as_of < window_start` and `generated_at <= window_start`;
- estimate `lower <= value <= upper`;
- occurrence probability values are at most one;
- coverage duration ordering and ratio formula;
- source, asset, detection, review, event, feature, model, and forecast belong to the same tenant;
- each candidate has at most one immutable final review;
- promotion occurs only from a confirmed review and is idempotent;
- recorded assets belong to recorded sources and live segments belong to live sources;
- evidence and media have not expired before access.
- every Reka `video_id` resolves through a tenant-scoped local mapping;
- Reka output validates before candidate persistence, and remote deletion is monitored before retention deletion is complete.
- A validated empty Reka candidate array completes analysis with zero candidates;
  exhausted indexing fails with `reka_index_timeout` and is never treated as a
  no-candidate result.
- Re-analysis never reopens a terminal job. An authorized tenant administrator
  may create one fresh, idempotent analysis job for retained, indexed media;
  newer active or completed analysis work blocks duplicate re-analysis.
- Starting a non-production demo session may delete only tenant-scoped,
  unreviewed `awaiting_review` candidates from earlier demo sessions. Immutable
  confirmed/rejected reviews and promoted incident events are never deleted by
  session cleanup. The active Review Queue displays only `awaiting_review` rows.
- Simulated capture is non-production, visibly labeled, and creates a bounded
  synthetic `live_segment`; it follows the same restricted media, Reka,
  candidate, evidence, and human-review boundaries as approved real footage.
  A simulated candidate can be rejected but can never be confirmed or promoted
  into incident history.

## Change procedure

1. Update the synthetic fixture first.
2. Update the JSON Schema.
3. Update producers, consumers, generated API types, and tests in the same change or publish a documented migration window.
4. Add a changelog entry to `contracts/README.md`.
5. Obtain review from a teammate outside the schema author's primary area.

No team should create a private duplicate of these types. Frontend types must be generated from the API/OpenAPI representation of these contracts.
