/**
 * Typed client for the frozen Phase 2 API surface.
 *
 * Response/domain types come from generated artifacts only:
 *  - `contracts.gen.ts` — generated from `contracts/schemas/*.schema.json`
 *  - `types.gen.ts`    — generated from `contracts/openapi.json`
 * Do not hand-maintain duplicate domain interfaces here.
 */

import type { components } from "./types.gen";
import type {
  AggregateForecastModelCard,
  CameraSource,
  CandidateReviewDecision,
  OperationalAggregateForecast,
  RestrictedCandidateDetection,
  SourceCoverageSnapshot,
  TypedApiError,
} from "./contracts.gen";

export type RecordedSourceCreate = components["schemas"]["RecordedSourceCreate"];
export type LiveSourceCreate = components["schemas"]["LiveSourceCreate"];
export type ReviewRequest = components["schemas"]["ReviewRequest"];

export type Role = "viewer" | "reviewer" | "tenant_admin" | "platform_operator";

export interface TenantMembership {
  tenant_id: string;
  slug: string;
  display_name: string;
  role: Role;
}

export interface MeTenants {
  active_tenant_id: string;
  tenants: TenantMembership[];
}

export interface Metadata {
  categories: string[];
  h3_resolution: number;
  forecast_window_minutes: number;
  forecast_data: "operational" | "synthetic_demo";
  limitations: string[];
}

export interface ForecastPage {
  items: OperationalAggregateForecast[];
  page: number;
  page_size: number;
  total: number;
}

export interface Readiness {
  status: string;
  deployment_mode: string;
  reka_chat: string;
  reka_vision: string;
  video_service: string;
  queue: string;
  near_live_capture: string;
  forecast_models: string;
  forecast_data: "operational" | "synthetic_demo";
}

export interface DemoForecastRefresh {
  tenant_id: string;
  window_start: string;
  forecast_count: number;
  feature_snapshot_version: string;
  coverage_ratio: number;
}

export interface CopilotInsight {
  request_id: string;
  answer: string;
  claims: Array<{ text: string; fact_ids: string[] }>;
  limitations: string[];
  data_as_of: string;
  data_version: string;
  model_version: string;
  reka_model: string;
  refusal_code: string;
}

export interface NearLiveRun {
  run_id: string;
  state: "queued" | "running" | "completed" | "failed" | "retry" | "cancelled";
  stage: string;
  label:
    | "near-live CCTV segment"
    | "simulated live segment"
    | "recorded video upload"
    | "recorded video processing"
    | "controlled video re-analysis";
  source_name?: string;
  source_attribution?: string;
  capture_seconds?: number;
  asset_id?: string;
  candidate_count?: number;
  error_code?: string;
  analysis_mode: "reka_vision" | "deterministic_fake";
  created_at: string;
  updated_at: string;
}

export interface LiveCctvSource {
  source_key: string;
  name: string;
  playback_url: string;
  attribution: string;
  status: "live" | "unavailable";
  analysis_mode: "reka_vision" | "deterministic_fake";
  limitations: string[];
}

export type ContactRole = components["schemas"]["DispatchContactSummary"]["role"];

/** Browser-safe response-contact projection generated from FastAPI/OpenAPI. */
export type ResponseContact = components["schemas"]["ResponseContactView"];
export type ResponseContactWrite = components["schemas"]["ResponseContactCreate"];
export type ResponseContactPatch = components["schemas"]["ResponseContactPatch"];
export type TestCallResult = components["schemas"]["TestCallView"];
export type DispatchState = components["schemas"]["DispatchCaseView"]["state"];
export type DispatchAttemptState = components["schemas"]["DispatchAttemptView"]["state"];
export type DispatchContactSummary = components["schemas"]["DispatchContactSummary"];
export type DispatchAttempt = Omit<
  components["schemas"]["DispatchAttemptView"],
  "attempt_number"
> & { attempt_number: 1 | 2 | 3 };
export type DispatchCase = Omit<
  components["schemas"]["DispatchCaseView"],
  "attempts" | "next_attempt_at" | "canceled_at"
> & {
  attempts: DispatchAttempt[];
  next_attempt_at: string | null;
  canceled_at: string | null;
};
export type DispatchPreview = components["schemas"]["DispatchPreviewView"];

export type PublicCandidate = Omit<RestrictedCandidateDetection, "evidence_ref"> & {
  evidence_available: boolean;
};

export interface SourceMapLocation {
  source_id: string;
  source_name: string;
  cell_id: string;
  h3_resolution: number;
  precision: "h3_area";
}

export type PublicSource = Pick<
  CameraSource,
  | "schema_version"
  | "tenant_id"
  | "source_id"
  | "name"
  | "mode"
  | "status"
  | "timezone"
  | "retention_policy_days"
  | "created_at"
  | "updated_at"
>;

/** Typed API error carrying the contract `code` for state-specific UI. */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly requestId?: string;
  readonly retryable: boolean;

  constructor(status: number, body: Partial<TypedApiError> | undefined, fallback: string) {
    super(body?.message ?? fallback);
    this.status = status;
    this.code = body?.code ?? "unknown_error";
    this.requestId = body?.request_id;
    this.retryable = Boolean(body?.retryable);
  }
}

const BASE = ((import.meta.env.VITE_API_BASE as string | undefined) ?? "").replace(/\/$/, "");

if (import.meta.env.PROD && BASE && !BASE.startsWith("https://")) {
  throw new Error("VITE_API_BASE must use HTTPS in production");
}

export function newIdempotencyKey(): string {
  return typeof crypto !== "undefined" && "randomUUID" in crypto
    ? crypto.randomUUID()
    : `key-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

async function request<T>(
  path: string,
  token: string,
  init?: RequestInit & { idempotencyKey?: string },
): Promise<T> {
  const headers: Record<string, string> = {
    Authorization: `Bearer ${token}`,
    ...(init?.headers as Record<string, string> | undefined),
  };
  if (init?.body && !(init.body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
  }
  if (init?.idempotencyKey) headers["Idempotency-Key"] = init.idempotencyKey;
  const response = await fetch(`${BASE}${path}`, { ...init, headers });
  if (!response.ok) {
    let body: Partial<TypedApiError> | undefined;
    try {
      body = (await response.json()) as Partial<TypedApiError>;
    } catch {
      body = undefined;
    }
    throw new ApiError(response.status, body, `Request failed with ${response.status}`);
  }
  return (await response.json()) as T;
}

async function requestBlob(path: string, token: string): Promise<Blob> {
  const response = await fetch(`${BASE}${path}`, {
    headers: { Authorization: `Bearer ${token}` },
    cache: "no-store",
  });
  if (!response.ok) {
    let body: Partial<TypedApiError> | undefined;
    try {
      body = (await response.json()) as Partial<TypedApiError>;
    } catch {
      body = undefined;
    }
    throw new ApiError(response.status, body, `Evidence request failed with ${response.status}`);
  }
  return response.blob();
}

async function requestNoContent(
  path: string,
  token: string,
  init: RequestInit & { idempotencyKey?: string },
): Promise<void> {
  const headers: Record<string, string> = {
    Authorization: `Bearer ${token}`,
    ...(init.headers as Record<string, string> | undefined),
  };
  if (init.idempotencyKey) headers["Idempotency-Key"] = init.idempotencyKey;
  const response = await fetch(`${BASE}${path}`, { ...init, headers });
  if (!response.ok) {
    let body: Partial<TypedApiError> | undefined;
    try {
      body = (await response.json()) as Partial<TypedApiError>;
    } catch {
      body = undefined;
    }
    throw new ApiError(response.status, body, `Request failed with ${response.status}`);
  }
}

export const api = {
  ready: () => fetch(`${BASE}/ready`).then((r) => r.json() as Promise<Readiness>),
  meTenants: (token: string) => request<MeTenants>("/v1/me/tenants", token),
  startDemoSession: (token: string, idempotencyKey: string) =>
    request<{ status: "started"; deleted_pending_candidates: number }>(
      "/v1/demo/session/start",
      token,
      { method: "POST", body: JSON.stringify({}), idempotencyKey },
    ),
  switchTenant: (token: string, tenantId: string) =>
    request<{ active_tenant_id: string; role: Role }>(
      `/v1/me/active-tenant/${encodeURIComponent(tenantId)}`,
      token,
      { method: "PUT", idempotencyKey: newIdempotencyKey() },
    ),
  metadata: (token: string) => request<Metadata>("/v1/metadata", token),
  liveCctv: (token: string) => request<LiveCctvSource>("/v1/demo/live-cctv", token),
  sources: (token: string) => request<{ items: PublicSource[] }>("/v1/sources", token),
  sourceMapLocation: (token: string, sourceId: string) =>
    request<SourceMapLocation>(
      `/v1/sources/${encodeURIComponent(sourceId)}/map-location`,
      token,
    ),
  createRecordedSource: (token: string, body: RecordedSourceCreate, idempotencyKey: string) =>
    request<PublicSource>("/v1/sources/recorded-video", token, {
      method: "POST",
      body: JSON.stringify(body),
      idempotencyKey,
    }),
  createLiveSource: (token: string, body: LiveSourceCreate, idempotencyKey: string) =>
    request<PublicSource>("/v1/sources/live-camera", token, {
      method: "POST",
      body: JSON.stringify(body),
      idempotencyKey,
    }),
  uploadVideo: (
    token: string,
    upload: {
      sourceId: string;
      capturedStart: string;
      capturedEnd: string;
      consentConfirmed: boolean;
      file: File;
    },
    idempotencyKey: string,
  ) => {
    const body = new FormData();
    body.append("source_id", upload.sourceId);
    body.append("captured_start", upload.capturedStart);
    body.append("captured_end", upload.capturedEnd);
    body.append("consent_confirmed", String(upload.consentConfirmed));
    body.append("file", upload.file, upload.file.name);
    return request<NearLiveRun>("/v1/video-assets/uploads", token, {
      method: "POST",
      body,
      idempotencyKey,
    });
  },
  ingestionRun: (token: string, runId: string) =>
    request<NearLiveRun>(`/v1/ingestion/runs/${encodeURIComponent(runId)}`, token),
  ingestionRuns: (token: string, limit = 25) =>
    request<{ items: NearLiveRun[] }>(`/v1/ingestion/runs?limit=${limit}`, token),
  reanalyzeRun: (token: string, runId: string, idempotencyKey: string) =>
    request<NearLiveRun>(
      `/v1/ingestion/runs/${encodeURIComponent(runId)}/reanalyze`,
      token,
      { method: "POST", idempotencyKey },
    ),
  startNearLiveCapture: (token: string, sourceKey: LiveCctvSource["source_key"], durationSeconds = 12) =>
    request<NearLiveRun>("/v1/demo/near-live-cctv/captures", token, {
      method: "POST",
      body: JSON.stringify({
        source_key: sourceKey,
        duration_seconds: durationSeconds,
      }),
      idempotencyKey: newIdempotencyKey(),
    }),
  startSimulatedCapture: (token: string, durationSeconds = 8) =>
    request<NearLiveRun>("/v1/demo/simulated-cctv/captures", token, {
      method: "POST",
      body: JSON.stringify({ duration_seconds: durationSeconds }),
      idempotencyKey: newIdempotencyKey(),
    }),
  coverage: (token: string) =>
    request<{ items: SourceCoverageSnapshot[] }>("/v1/coverage", token),
  candidates: (token: string, limit = 50) =>
    request<{ items: PublicCandidate[] }>(`/v1/candidate-detections?limit=${limit}`, token),
  candidateEvidence: (token: string, detectionId: string) =>
    requestBlob(
      `/v1/candidate-detections/${encodeURIComponent(detectionId)}/evidence`,
      token,
    ),
  reviewCandidate: (
    token: string,
    detectionId: string,
    body: ReviewRequest,
    idempotencyKey: string,
  ) =>
    request<CandidateReviewDecision>(
      `/v1/candidate-detections/${encodeURIComponent(detectionId)}/review`,
      token,
      { method: "POST", body: JSON.stringify(body), idempotencyKey },
    ),
  refreshDemoForecasts: (token: string) =>
    request<DemoForecastRefresh>("/v1/demo/forecasts/refresh", token, {
      method: "POST",
      body: JSON.stringify({}),
      idempotencyKey: newIdempotencyKey(),
    }),
  responseContacts: (token: string, zoneId?: string) => {
    const query = new URLSearchParams();
    if (zoneId) query.set("zone_id", zoneId);
    const suffix = query.size > 0 ? `?${query.toString()}` : "";
    return request<{ items: ResponseContact[]; next_cursor: string | null }>(
      `/v1/response-contacts${suffix}`,
      token,
    );
  },
  createResponseContact: (
    token: string,
    body: ResponseContactWrite,
    idempotencyKey: string,
  ) =>
    request<ResponseContact>("/v1/response-contacts", token, {
      method: "POST",
      body: JSON.stringify(body),
      idempotencyKey,
    }),
  updateResponseContact: (
    token: string,
    contactId: string,
    body: ResponseContactPatch,
    idempotencyKey: string,
  ) =>
    request<ResponseContact>(
      `/v1/response-contacts/${encodeURIComponent(contactId)}`,
      token,
      { method: "PATCH", body: JSON.stringify(body), idempotencyKey },
    ),
  deleteResponseContact: (token: string, contactId: string, idempotencyKey: string) =>
    requestNoContent(`/v1/response-contacts/${encodeURIComponent(contactId)}`, token, {
      method: "DELETE",
      idempotencyKey,
    }),
  createResponseContactTestCall: (
    token: string,
    contactId: string,
    idempotencyKey: string,
  ) =>
    request<TestCallResult>(
      `/v1/response-contacts/${encodeURIComponent(contactId)}/test-calls`,
      token,
      {
        method: "POST",
        body: JSON.stringify({ authorize_test_call: true }),
        idempotencyKey,
      },
    ),
  authorizeDispatch: (
    token: string,
    incidentId: string,
    idempotencyKey: string,
  ) =>
    request<DispatchCase>(
      `/v1/incidents/${encodeURIComponent(incidentId)}/dispatch-authorizations`,
      token,
      {
        method: "POST",
        body: JSON.stringify({
          authorize_call: true,
          message_template_version: "dispatch-alert-v1",
        }),
        idempotencyKey,
      },
    ),
  dispatchPreview: (token: string, incidentId: string) =>
    request<DispatchPreview>(
      `/v1/incidents/${encodeURIComponent(incidentId)}/dispatch-preview`,
      token,
    ),
  dispatchCase: (token: string, dispatchCaseId: string) =>
    request<DispatchCase>(
      `/v1/dispatch-cases/${encodeURIComponent(dispatchCaseId)}`,
      token,
    ),
  cancelDispatch: (
    token: string,
    dispatchCaseId: string,
    reason: string,
    idempotencyKey: string,
  ) =>
    request<DispatchCase>(
      `/v1/dispatch-cases/${encodeURIComponent(dispatchCaseId)}/cancel`,
      token,
      {
        method: "POST",
        body: JSON.stringify({ cancel_pending_calls: true, reason }),
        idempotencyKey,
      },
    ),
  forecasts: (
    token: string,
    params: { windowStart: string; category: string; page?: number; pageSize?: number; bbox?: string },
  ) => {
    const query = new URLSearchParams({
      window_start: params.windowStart,
      category: params.category,
      page: String(params.page ?? 1),
      page_size: String(params.pageSize ?? 100),
    });
    if (params.bbox) query.set("bbox", params.bbox);
    return request<ForecastPage>(`/v1/forecasts?${query.toString()}`, token);
  },
  forecastDetail: (token: string, forecastId: string) =>
    request<OperationalAggregateForecast>(
      `/v1/forecasts/${encodeURIComponent(forecastId)}`,
      token,
    ),
  modelCard: (token: string) => request<AggregateForecastModelCard>("/v1/model-card", token),
  copilot: (token: string, question: string) =>
    request<CopilotInsight>("/v1/ai/copilot/messages", token, {
      method: "POST",
      body: JSON.stringify({ question }),
    }),
};

export type { OperationalAggregateForecast, SourceCoverageSnapshot, AggregateForecastModelCard };
