# Reka API capability matrix

Verified against the official Reka documentation, published OpenAPI documents,
and the authenticated model/feature endpoints on 2026-08-31. This document
separates what Reka offers from what CivicHalo currently uses.

## Executive decision

CivicHalo uses a two-path Reka integration:

1. Clips at or below 30 seconds use Reka Vision Quick Tag for a bounded native
   description, then `reka-flash-3` structured output to map that fallible
   observation into the product taxonomy. Direct multimodal
   `reka-edge-2603` analysis of the complete clip is the fallback when Quick
   Tag is unavailable.
2. Longer recordings use Reka Vision upload, indexing, status polling, and
   indexed video Q&A. They produce the same exact five-field candidate array.

The exact provider result is either `[]` or an array of:

```json
{
  "offset_seconds": 0,
  "category": "other",
  "event_type": "structural_collapse",
  "description": "A multistorey structure visibly collapses.",
  "confidence": 0.85
}
```

Every row is an unconfirmed visual observation. It becomes an incident only
after an authorized reviewer checks the evidence and confirms it.

## Capability categories

| Reka family | Official capabilities | CivicHalo status | Product use |
|---|---|---|---|
| Chat API | OpenAI-compatible chat completions, multimodal image/video/audio input, streaming, model discovery, structured output on supported models | Implemented for candidate mapping, multimodal fallback, and aggregate explanations | Map bounded Quick Tag observations with Flash; analyze the complete clip with Edge on fallback; explain allowlisted aggregate forecast facts |
| Vision v1 | Managed video upload/index/get/list/delete, semantic search, video Q&A, tagging, groups, clips, and images | Implemented core lifecycle and indexed Q&A | Durable long-video lifecycle and candidate analysis |
| Vision Quick Tag | Short-video native description and advertising-oriented tags, including a violence flag | Implemented as the primary under-30-second visual description | Supply bounded fallible evidence to the structured text classifier; never sufficient to confirm an incident |
| Vision v2 | Upload without implicit indexing, feature catalog/plan/trigger/status, captions/transcript/scenes/objects/embeddings, v2 search/chat, feature reads, open-vocabulary segmentation | Researched; staged next | Selectively compute only required features for long-video search, evidence localization, and cost control |
| Edge models | Multimodal video reasoning intended for low-latency and edge/physical-AI scenarios | Current hosted Chat model is `reka-edge-2603` | Temporal distinction such as play/gesture versus a visible physical attack |

## Chat API

Official references: [Chat overview](https://docs.reka.ai/chat/overview),
[multimodal chat](https://docs.reka.ai/chat/chat-with-image-video-and-audio),
[models](https://docs.reka.ai/chat/models),
[create completion](https://docs.reka.ai/chat/api-reference/create), and
[function calling](https://docs.reka.ai/chat/function-calling).

Relevant operations:

- `GET /v1/models` discovers models and account-specific input/features.
- `POST /v1/chat/completions` performs text or multimodal inference.
- The documentation recommends Chat for short videos under 30 seconds and
  Vision for longer video workflows.
- `finish_reason` can report `stop`, `length`, or `context`. Both truncation
  outcomes fail closed in CivicHalo and receive at most one bounded repair.
- Function calling is not used for video candidate extraction. Candidate
  output is data constrained by JSON Schema, not a tool side effect.

Authenticated compatibility findings:

- `reka-edge-2603` advertised text, image, and video input together with
  `structured_outputs`, tools, and streaming.
- The live endpoint accepted a strict JSON-schema `response_format` whose root
  is an array.
- The live endpoint required `video_url` as
  `{"url":"data:video/mp4;base64,..."}`. The current static OpenAPI and example
  depict a scalar string, so this repository has a regression test for the
  live-compatible nested shape.
- The account's `reka-flash-3` entry was text-only at verification time. It
  does not receive raw video; it maps the bounded native Vision description
  and powers redacted aggregate text explanations.

## Vision v1

Official references: [Vision overview](https://docs.reka.ai/vision/overview),
[video management](https://docs.reka.ai/vision/video-management),
[video search](https://docs.reka.ai/vision/video-search),
[video Q&A](https://docs.reka.ai/vision/video-qa),
[tagging](https://docs.reka.ai/vision/tagging),
[groups](https://docs.reka.ai/vision/groups),
[clips](https://docs.reka.ai/vision/clips), and
[images](https://docs.reka.ai/vision/images).

Current CivicHalo flow:

```text
tenant-authorized S3 object
  -> POST /v1/videos/upload with index=true
  -> GET /v1/videos/{video_id} until indexed/failed
  -> POST /v1/qa/chat for recordings over 30 seconds
  -> strict local five-field validation
  -> restricted Postgres candidate
  -> human review
  -> DELETE /v1/videos/{video_id} during retention cleanup
```

The opaque Reka video identifier is stored only in the tenant-scoped restricted
registry. It is never accepted from or returned to the browser.

Quick Tag is the primary native description for short video. Only its bounded
description and violence flag enter the second-stage structured prompt. Its
advertising-oriented tag taxonomy is not the product contract and its violence
flag is deliberately non-decisive. A live benign-gathering test returned a
noisy `Violence=true`; the grounded Flash stage correctly returned `[]` from
the description instead of turning the tag into a fight candidate.

## Vision v2

Official references: [upload video](https://docs.reka.ai/vision/api-reference/v-2/upload-video-v-2-videos-post),
[feature catalog](https://docs.reka.ai/vision/api-reference/v-2/get-feature-catalog-v-2-features-get),
[feature plan](https://docs.reka.ai/vision/api-reference/v-2/plan-features-v-2-videos-video-id-features-plan-post),
[trigger captions](https://docs.reka.ai/vision/api-reference/v-2/trigger-captions-v-2-videos-video-id-features-captions-post),
[search](https://docs.reka.ai/vision/api-reference/v-2/search-v-2-search-post),
[chat](https://docs.reka.ai/vision/api-reference/v-2/chat-v-2-chat-post),
[captions](https://docs.reka.ai/vision/api-reference/v-2/list-captions-v-2-videos-video-id-captions-get),
[scenes](https://docs.reka.ai/vision/api-reference/v-2/list-scenes-v-2-videos-video-id-scenes-get), and
[segmentation](https://docs.reka.ai/vision/api-reference/v-2/segment-v-2-videos-video-id-segment-post).

V2 decomposes ingestion from feature computation. Feature states are
`none`, `pending`, `processing`, `ready`, `failed`, or `blocked`. The published
schema names transcript, captions, reel captions, embeddings, objects, scenes,
audio events, chapters, and thumbnails. Feature availability is account- and
deployment-specific; the authenticated catalog is authoritative.

The verified account catalog exposed this usable dependency chain:

```text
transcript -> captions -> embeddings
     |
     +-----------> objects
     +-----------> scenes
```

Recommended migration after the hackathon review:

1. Upload once with v2.
2. Read the live feature catalog and plan only the features required by the
   current job.
3. Trigger transcript/captions for evidence localization and embeddings only
   when semantic search is requested.
4. Poll each required feature to a terminal state with the existing durable
   queue/backoff/DLQ mechanics.
5. Use v2 chat/search with an explicit video/time context and retrieve bounded
   captions/scenes for reviewer navigation.
6. Use open-vocabulary segmentation only to highlight an object in a short
   reviewer clip; never infer identity from it.

This is not enabled immediately because changing the stable long-video path
immediately before the demo would add account-feature and migration risk
without fixing the current schema problem.

## Classification policy

The model checks the entire clip. It proposes violence only when harmful
physical force or an attack is visibly occurring, such as punching, kicking,
striking, aggressive grappling, or weapon use. Rock-paper-scissors, hand games,
high-fives, dancing, ordinary gestures, consensual sport, playful/mock
movement, conversation, and an argument without a visible physical attack are
explicitly excluded.

Structural collapse, falling debris, fire, explosion, serious collision, or
another visible acute hazard can produce a candidate even when it is not a
crime. This is why `category` and the more specific `event_type` are separate.

## Reliability, limits, and cost controls

Official references: [errors](https://docs.reka.ai/errors),
[Vision rate limits](https://docs.reka.ai/vision/rate-limits),
[Chat pricing](https://docs.reka.ai/pricing), and
[Vision pricing](https://docs.reka.ai/vision/pricing).

- Authentication uses `X-Api-Key` server-side. The response body and key are
  never copied into public errors or logs.
- HTTP 429 is retryable with exponential backoff; transient server/network
  failures are retryable; credentials and invalid outputs fail closed.
- Vision documents rolling 24-hour free-tier/request limits. At verification,
  the documented limits included 50 uploads, 50 video searches, 100 image
  searches, and 10 clips; the account dashboard and current pricing page remain
  authoritative.
- Managed Vision content is subject to documented retention/deletion behavior.
  CivicHalo also schedules explicit remote and encrypted S3 deletion according
  to the tenant policy.
- Short clips are bounded by API Gateway/media limits and avoid unnecessary
  Vision feature computation. Long recordings use durable jobs rather than
  holding an API request open.
- Every Reka operation records only safe provenance: model, prompt version,
  operation family, duration/size, latency, retry count, candidate count, and a
  typed error code.

## Non-goals and safety boundary

Reka is not used for face recognition, identity watchlists, guilt/intent
inference, person-level crime prediction, or automatic enforcement. Exact
locations, identities, incident IDs, credentials, and unrelated tenant context
are not included in prompts. Model confidence measures confidence in a visual
observation; it is not a crime probability and is not the forecast score.
