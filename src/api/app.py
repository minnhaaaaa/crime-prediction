"""Authenticated FastAPI boundary for the Phase 2 recorded-video slice."""

from __future__ import annotations

import hashlib
import json
import math
import os
import threading
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, File, Form, Header, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field
from starlette.background import BackgroundTask
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import FileResponse

from src.data.store import IngestionStore
from src.data.video import (
    FakeRekaVisionProvider,
    FfmpegHlsCapture,
    RekaVisionProvider,
    SimulatedVideoCapture,
    VideoPipelineService,
    VideoStore,
)
from src.data.video.broker import JobBroker, JobMessage
from src.data.video.errors import VideoPipelineError
from src.data.video.transcode import FfmpegWebmTranscoder
from src.models.contracts import validate_contract
from src.models.data import parse_utc
from src.models.operational import ForecastService
from src.models.registry import FilesystemApprovedModelRegistry

from . import demo_data, reka
from .dispatch import DispatchApiDependencies, create_dispatch_router
from .dispatch_development import (
    DevelopmentConfirmedIncident,
    create_development_dispatch_dependencies,
)
from .errors import install_error_handlers, problem
from .forecasting import ForecastOrchestrator, InMemoryForecastRepository
from .security import ApiSecurityMiddleware, InMemoryRateLimiter, RateLimiter
from .settings import Settings
from .state import AuditLog, IdempotencyStore
from .tenancy import (
    AuthenticationProvider,
    DevelopmentAuthenticationProvider,
    OidcAuthenticationProvider,
    TenantContext,
    context_for,
    require_operator,
    require_owner,
    require_reviewer,
    require_tenant,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
LIMITATIONS = [
    "Forecasts are aggregate area-level estimates with uncertainty, not ground truth.",
    "Historical incident reports may reflect reporting and enforcement patterns.",
    "Prohibited: individual assessment, suspect identification, facial recognition, or automated enforcement.",
    "Suppressed cells expose no numeric estimate and must not be interpreted as zero risk.",
    "Drivers are associations, not causes; human interpretation is required.",
]
CATEGORIES = ("property", "violence", "public_order", "traffic_safety")
DEMO_HLS_SOURCE_ID = "20000000-0000-4000-8000-000000000099"
DEMO_SIMULATED_SOURCE_ID = "20000000-0000-4000-8000-000000000098"
DEMO_HLS_LOCATION_ID = "30000000-0000-4000-8000-000000000099"
DEMO_HLS_LOCATION_REF = (
    "secret://tenant/00000000-0000-4000-8000-000000000001/locations/"
    f"{DEMO_HLS_LOCATION_ID}"
)
DEMO_RECORDED_LOCATION_REF = "secret://tenant/demo-one/cameras/entrance/location"


def _demo_hls_location_ref(tenant_id: str) -> str:
    return f"secret://tenant/{tenant_id}/locations/{DEMO_HLS_LOCATION_ID}"


class RecordedSourceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    timezone: str = Field(min_length=1, max_length=80)
    registered_location_id: uuid.UUID
    retention_policy_days: int = Field(ge=1, le=30)


class LiveSourceCreate(RecordedSourceCreate):
    connection_secret_id: uuid.UUID
    transport: str = Field(pattern="^(hls|rtsp|onvif)$")


class ReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["confirmed", "rejected"]
    confirmed_category: (
        Literal["property", "violence", "public_order", "traffic_safety", "other"]
        | None
    ) = None
    rejection_reason: (
        Literal[
            "false_positive",
            "insufficient_evidence",
            "duplicate",
            "outside_scope",
            "other",
        ]
        | None
    ) = None


class CopilotMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=2000)


class NearLiveCaptureRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_key: Literal["louisiana-dot-i20"] = "louisiana-dot-i20"
    duration_seconds: int | None = Field(default=None, ge=5, le=60)


class SimulatedCaptureRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    duration_seconds: int = Field(default=8, ge=5, le=30)


class ModelPromotionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_version: str = Field(
        min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._-]+$"
    )
    reason: str = Field(min_length=1, max_length=500)


class ModelRollbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=500)


class _DemoLocationResolver:
    """Restricted demo coordinates; values never cross the public API boundary."""

    def resolve(self, tenant_id: str, location_ref: str) -> dict[str, float]:
        if location_ref == _demo_hls_location_ref(tenant_id):
            return {"latitude": 32.46091, "longitude": -93.831}
        if location_ref == DEMO_RECORDED_LOCATION_REF:
            return {"latitude": 12.9716, "longitude": 77.5946}
        raise VideoPipelineError(
            "location_unavailable", "Source location could not be resolved"
        )


def _public_run(run: dict[str, Any]) -> dict[str, Any]:
    return {
        key: run.get(key)
        for key in (
            "run_id",
            "state",
            "stage",
            "label",
            "source_name",
            "source_attribution",
            "capture_seconds",
            "asset_id",
            "candidate_count",
            "error_code",
            "analysis_mode",
            "created_at",
            "updated_at",
        )
        if run.get(key) is not None
    }


def _durable_run(
    store: Any, tenant_id: str, root_job: dict[str, Any], analysis_mode: str
) -> dict[str, Any]:
    """Collapse a durable upload/index/analyze chain into one public run."""
    operation_order = {"upload": 0, "index": 1, "analyze": 2, "delete": 3}
    jobs = store.jobs_for_asset(tenant_id, root_job["asset_id"])
    selected = max(
        jobs or [root_job],
        key=lambda item: (
            operation_order[item["operation"]],
            item["created_at"],
            item["job_id"],
        ),
    )
    state = selected["state"]
    if state == "retry":
        state = "queued"
    elif state == "cancelled":
        state = "failed"
    elif state == "completed" and selected["operation"] in {"upload", "index"}:
        # The next worker stage is dispatched immediately after completion. Do
        # not tell the browser the entire processing run is already finished.
        state = "running"

    candidate_count = sum(
        candidate.get("asset_id") == root_job["asset_id"]
        for candidate in store.list_candidates(tenant_id)
    )
    stage = selected["operation"]
    if state == "completed" and stage == "analyze":
        stage = "awaiting_human_review"
    elif state == "completed" and stage == "delete":
        stage = "deleted"

    return {
        "run_id": root_job["job_id"],
        "state": state,
        "stage": stage,
        "label": "recorded video upload",
        "asset_id": root_job["asset_id"],
        "candidate_count": candidate_count,
        "analysis_mode": analysis_mode,
        "error_code": selected.get("last_error_code"),
        "created_at": root_job["created_at"],
        "updated_at": selected["updated_at"],
    }


def _load_fixture(name: str) -> dict[str, Any]:
    return json.loads(
        (REPO_ROOT / "contracts" / "fixtures" / f"{name}.json").read_text()
    )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _public_source(source: dict[str, Any]) -> dict[str, Any]:
    return {
        key: source[key]
        for key in (
            "schema_version",
            "tenant_id",
            "source_id",
            "name",
            "mode",
            "status",
            "timezone",
            "retention_policy_days",
            "created_at",
            "updated_at",
        )
    }


def _parse_bbox(value: str | None) -> tuple[float, float, float, float] | None:
    if value is None:
        return None
    try:
        west, south, east, north = (float(part) for part in value.split(","))
    except (ValueError, TypeError) as exc:
        raise problem(
            422, "invalid_bbox", "bbox must be west,south,east,north"
        ) from exc
    if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
        raise problem(
            422, "invalid_bbox", "bbox coordinates are out of range or unordered"
        )
    return west, south, east, north


def _future_row(
    tenant_id: str,
    cell_id: str,
    window_start: datetime,
    category: str,
    *,
    coverage_ratio: float,
) -> dict[str, Any]:
    seed = int(
        hashlib.sha256(f"{tenant_id}|{cell_id}|{category}".encode()).hexdigest()[:8], 16
    )
    lag_1, lag_2, lag_7, lag_14 = ((seed >> shift) % 4 for shift in (0, 3, 6, 9))
    data_as_of = min(datetime.now(UTC), window_start - timedelta(seconds=1))
    hour_angle = 2 * math.pi * window_start.hour / 24
    day_angle = 2 * math.pi * window_start.weekday() / 7

    return {
        "schema_version": "1.0.0",
        "tenant_id": tenant_id,
        "cell_id": cell_id,
        "interval_start": window_start.isoformat().replace("+00:00", "Z"),
        "category": category,
        "lag_1": lag_1,
        "lag_2": lag_2,
        "lag_7": lag_7,
        "lag_14": lag_14,
        "rolling_7_mean": (lag_1 + lag_2 + lag_7) / 3,
        "rolling_14_mean": (lag_1 + lag_2 + lag_7 + lag_14) / 4,
        "neighbor_lag_1": float((seed >> 12) % 5),
        "recent_trend": float(lag_1 - lag_7) / 3,
        "hour_sin": math.sin(hour_angle),
        "hour_cos": math.cos(hour_angle),
        "day_of_week_sin": math.sin(day_angle),
        "day_of_week_cos": math.cos(day_angle),
        "coverage_ratio": coverage_ratio,
        "data_as_of": data_as_of.isoformat().replace("+00:00", "Z"),
        "feature_snapshot_version": f"forecast-features-{window_start:%Y%m%dT%H%M%SZ}-v1",
    }


def _new_source(
    *, tenant_id: str, body: RecordedSourceCreate, mode: str, connection: dict[str, str]
) -> dict[str, Any]:
    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    source = {
        "schema_version": "1.0.0",
        "tenant_id": tenant_id,
        "source_id": str(uuid.uuid4()),
        "name": body.name,
        "mode": mode,
        "status": "active",
        "timezone": body.timezone,
        "location_ref": f"secret://tenant/{tenant_id}/locations/{body.registered_location_id}",
        "connection": connection,
        "retention_policy_days": body.retention_policy_days,
        "created_at": now,
        "updated_at": now,
    }
    validate_contract("camera-source", source)
    return source


def create_app(
    provider: reka.RekaProvider | None = None,
    *,
    lifespan: Any | None = None,
    settings: Settings | None = None,
    auth_provider: AuthenticationProvider | None = None,
    forecast_service: ForecastService | None = None,
    coverage_provider: Callable[[str, str], float] | None = None,
    video_service: VideoPipelineService | None = None,
    hls_capture: FfmpegHlsCapture | None = None,
    simulated_capture: SimulatedVideoCapture | None = None,
    rate_limiter: RateLimiter | None = None,
    forecast_orchestrator: ForecastOrchestrator | None = None,
    model_registry: FilesystemApprovedModelRegistry | None = None,
    audit_log: Any | None = None,
    idempotency_store: Any | None = None,
    video_broker: JobBroker | None = None,
    forecast_refresher: Callable[[str, datetime], dict[str, Any]] | None = None,
    seed_demo_fixtures: bool = True,
    deployment_mode: str = "development",
    media_transcoder: FfmpegWebmTranscoder | None = None,
    dispatch_dependencies: DispatchApiDependencies | None = None,
) -> FastAPI:
    active_settings = settings or Settings.from_environment()
    production = active_settings.app_environment == "production"
    durable_mode = production or deployment_mode in {"integrated_demo", "live_demo"}
    public_hls_enabled = not production or active_settings.public_hls_demo_enabled
    if auth_provider is None and production:
        if not all(
            (
                active_settings.oidc_issuer,
                active_settings.oidc_audience,
                active_settings.oidc_jwks_url,
            )
        ):
            raise ValueError(
                "A production AuthenticationProvider or complete OIDC settings must be supplied"
            )
        auth_provider = OidcAuthenticationProvider(
            issuer=active_settings.oidc_issuer,
            audience=active_settings.oidc_audience,
            jwks_url=active_settings.oidc_jwks_url,
            algorithms=active_settings.oidc_algorithms,
            memberships_claim=active_settings.oidc_memberships_claim,
        )
    if production and getattr(auth_provider, "development_only", False):
        raise ValueError(
            "A development AuthenticationProvider cannot run in production"
        )
    if production and any(
        not origin.startswith("https://") for origin in active_settings.cors_origins
    ):
        raise ValueError("Production CORS origins must use HTTPS")
    if rate_limiter is None:
        if production:
            raise ValueError("A durable production RateLimiter must be injected")
        rate_limiter = InMemoryRateLimiter(
            active_settings.api_rate_limit_requests,
            active_settings.api_rate_limit_window_seconds,
        )
    if production and getattr(rate_limiter, "development_only", False):
        raise ValueError("A development RateLimiter cannot run in production")
    if provider is None:
        provider = (
            reka.RekaAPIProvider(
                api_key=active_settings.reka_api_key,
                base_url=active_settings.reka_chat_base_url,
                model=active_settings.reka_model,
                prompt_version=active_settings.reka_prompt_version,
                timeout_seconds=active_settings.reka_timeout_seconds,
            )
            if active_settings.reka_configured
            else reka.FakeRekaProvider()
        )

    app = FastAPI(
        title="Aggregate Incident Forecasting API",
        version="0.2.0",
        description="Tenant-isolated aggregate forecasts. No identity analysis or automated enforcement.",
        lifespan=lifespan,
    )
    install_error_handlers(app)
    app.add_middleware(
        ApiSecurityMiddleware,
        max_request_bytes=active_settings.max_request_bytes,
        rate_limiter=rate_limiter,
        production=production,
    )
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=list(active_settings.trusted_hosts)
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(active_settings.cors_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key"],
        expose_headers=["X-Request-ID"],
    )
    app.state.settings = active_settings
    app.state.auth_provider = auth_provider or DevelopmentAuthenticationProvider()
    if production and model_registry is None:
        raise ValueError("An approved production model registry must be injected")
    app.state.model_registry = model_registry
    app.state.forecast_service = forecast_service or ForecastService(
        models=model_registry
    )
    if forecast_orchestrator is None:
        if production:
            raise ValueError("A production ForecastOrchestrator must be injected")
        forecast_orchestrator = ForecastOrchestrator(
            app.state.forecast_service, InMemoryForecastRepository()
        )
    if production and getattr(
        forecast_orchestrator.repository, "development_only", False
    ):
        raise ValueError("A development forecast repository cannot run in production")
    app.state.forecast_orchestrator = forecast_orchestrator
    audit_log = audit_log or AuditLog()
    idempotency_store = idempotency_store or IdempotencyStore()
    if production and getattr(audit_log, "development_only", False):
        raise ValueError("A durable production audit log must be injected")
    if production and getattr(idempotency_store, "development_only", False):
        raise ValueError("A durable production idempotency store must be injected")
    app.state.audit = audit_log
    app.state.idempotency = idempotency_store
    first_source = (
        None if production or not seed_demo_fixtures else _load_fixture("camera-source")
    )
    app.state.sources = (
        {} if first_source is None else {first_source["tenant_id"]: [first_source]}
    )
    candidate = (
        None
        if production or not seed_demo_fixtures
        else _load_fixture("candidate-detection")
    )
    app.state.candidates = (
        {}
        if candidate is None
        else {candidate["tenant_id"]: {candidate["detection_id"]: candidate}}
    )
    app.state.reviews = {}
    runtime_dir = active_settings.runtime_dir.resolve()
    media_root = runtime_dir / "restricted-media"
    media_root.mkdir(parents=True, exist_ok=True)
    if production and video_service is None:
        raise ValueError("The production Postgres/S3 video service must be injected")
    if production and video_broker is None:
        raise ValueError("The production durable video broker must be injected")
    if production and coverage_provider is None:
        raise ValueError("A measured production coverage provider must be injected")
    if video_service is None:
        ingestion_store = IngestionStore(runtime_dir / "video-pipeline.sqlite3")
        vision_provider = (
            RekaVisionProvider(
                active_settings.reka_api_key,
                base_url=active_settings.reka_vision_base_url,
                chat_base_url=active_settings.reka_chat_base_url,
                chat_model=active_settings.reka_video_model,
                text_model=active_settings.reka_model,
                timeout_seconds=active_settings.reka_timeout_seconds,
                use_quick_tag_pipeline=True,
            )
            if active_settings.reka_configured
            else FakeRekaVisionProvider(
                proposals=[
                    {
                        "offset_seconds": 3,
                        "category": "traffic_safety",
                        "event_type": "vehicle_collision",
                        "description": "Two vehicles visibly collide.",
                        "confidence": 0.58,
                    }
                ]
            )
        )
        video_service = VideoPipelineService(
            VideoStore(ingestion_store),
            vision_provider,
            _DemoLocationResolver(),
            media_root=media_root,
            prompt_version=active_settings.reka_prompt_version,
        )
    app.state.video_service = video_service
    app.state.video_broker = video_broker
    app.state.media_transcoder = media_transcoder or FfmpegWebmTranscoder()
    app.state.hls_capture = hls_capture or FfmpegHlsCapture()
    app.state.simulated_capture = simulated_capture or SimulatedVideoCapture()
    app.state.video_runs = {}
    app.state.vision_mode = (
        "reka_vision" if active_settings.reka_configured else "deterministic_fake"
    )
    if production and dispatch_dependencies is None:
        raise ValueError("Production dispatch dependencies must be injected")
    if not production and dispatch_dependencies is None:

        def resolve_development_incident(
            tenant_id: str, incident_id: str
        ) -> DevelopmentConfirmedIncident | None:
            try:
                uuid.UUID(incident_id)
            except ValueError:
                return None
            detection_id = incident_id
            review = app.state.reviews.get((tenant_id, detection_id))
            if review is None:
                try:
                    review = app.state.video_service.store.get_review_for_candidate(
                        tenant_id, detection_id
                    )
                except VideoPipelineError:
                    return None
            if (
                review is None
                or review.get("decision") != "confirmed"
                or not review.get("promoted_external_event_id")
            ):
                return None
            try:
                candidate_record = app.state.video_service.store.get_candidate(
                    tenant_id, detection_id
                )
            except VideoPipelineError:
                candidate_record = app.state.candidates.get(tenant_id, {}).get(
                    detection_id
                )
            if candidate_record is None:
                return None
            occurred_at = datetime.fromisoformat(str(candidate_record["occurred_at"]))
            return DevelopmentConfirmedIncident(
                incident_id=detection_id,
                category=str(review["confirmed_category"]),
                occurred_at=occurred_at,
            )

        dispatch_dependencies = create_development_dispatch_dependencies(
            idempotency=app.state.idempotency,
            resolve_incident=resolve_development_incident,
        )
    app.state.dispatch_dependencies = dispatch_dependencies
    if dispatch_dependencies is not None:
        app.include_router(create_dispatch_router(dispatch_dependencies))
    if first_source is not None:
        try:
            app.state.video_service.register_recorded_source(
                first_source, authenticated_tenant_id=first_source["tenant_id"]
            )
        except VideoPipelineError:
            # A durable development store may already contain the fixture source.
            pass

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready")
    def ready() -> dict[str, Any]:
        provider_ready = (
            active_settings.reka_configured and active_settings.reka_provider_verified
        )
        return {
            "status": "ready" if provider_ready else "degraded",
            "deployment_mode": deployment_mode,
            "reka_chat": (
                "verified"
                if provider_ready
                else "configured_unverified"
                if active_settings.reka_configured
                else "deterministic_fallback"
            ),
            "reka_vision": (
                "verified"
                if provider_ready
                else "configured_unverified"
                if active_settings.reka_configured
                else "deterministic_fake"
            ),
            "video_service": "durable_connected"
            if video_broker is not None
            else "connected",
            "queue": "durable_connected"
            if video_broker is not None
            else "development_thread",
            "near_live_capture": "allowlisted_hls"
            if public_hls_enabled
            else "disabled",
            "forecast_models": "approved_or_historical_fallback",
            "forecast_data": (
                "synthetic_demo"
                if active_settings.synthetic_demo_forecasts
                else "operational"
            ),
            "dispatch_voice": (
                dispatch_dependencies.twilio_mode
                if dispatch_dependencies is not None
                else "disabled"
            ),
            "external_calls_enabled": bool(
                dispatch_dependencies is not None
                and dispatch_dependencies.external_calls_enabled
            ),
        }

    @app.get("/v1/demo/live-cctv")
    def live_cctv(ctx: TenantContext = Depends(require_tenant)) -> dict[str, Any]:
        definition = app.state.hls_capture.source("louisiana-dot-i20")
        return {
            "source_key": definition.key,
            "name": definition.name,
            "playback_url": definition.url,
            "attribution": definition.attribution,
            "status": "live",
            "analysis_mode": app.state.vision_mode,
            "limitations": [
                "The public feed may be delayed or unavailable at the source.",
                "Playback is context only; Reka analyzes bounded captured segments.",
            ],
        }

    @app.get("/v1/me/tenants")
    def me_tenants(
        request: Request, ctx: TenantContext = Depends(require_tenant)
    ) -> dict[str, Any]:
        principal = request.state.principal
        return {
            "active_tenant_id": ctx.tenant_id,
            "tenants": [item.__dict__ for item in principal.memberships],
        }

    @app.post("/v1/demo/session/start")
    def start_demo_session(
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        ctx: TenantContext = Depends(require_owner),
    ) -> dict[str, Any]:
        if production:
            raise problem(
                404,
                "demo_session_cleanup_disabled",
                "Demo session cleanup is unavailable in production",
            )

        def action() -> dict[str, Any]:
            deleted = app.state.video_service.store.delete_pending_candidates(
                ctx.tenant_id
            )
            fixture_bucket = app.state.candidates.get(ctx.tenant_id, {})
            fixture_ids = [
                detection_id
                for detection_id, candidate_record in fixture_bucket.items()
                if candidate_record.get("review_status") == "awaiting_review"
            ]
            for detection_id in fixture_ids:
                del fixture_bucket[detection_id]
            total = deleted + len(fixture_ids)
            app.state.audit.record(
                tenant_id=ctx.tenant_id,
                principal_id=ctx.principal_id,
                request_id=ctx.request_id,
                action="demo_session_started",
                resource_type="candidate_review_queue",
                resource_id=ctx.tenant_id,
            )
            return {
                "status": "started",
                "deleted_pending_candidates": total,
            }

        return app.state.idempotency.execute(
            tenant_id=ctx.tenant_id,
            operation="start_demo_session",
            key=idempotency_key,
            payload={"tenant_id": ctx.tenant_id},
            action=action,
        )

    @app.put("/v1/me/active-tenant/{tenant_id}")
    def switch_tenant(
        tenant_id: str,
        request: Request,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        ctx: TenantContext = Depends(require_tenant),
    ) -> dict[str, Any]:
        principal = request.state.principal
        if tenant_id not in {item.tenant_id for item in principal.memberships}:
            raise problem(403, "tenant_forbidden", "Tenant membership is required")

        def action() -> dict[str, Any]:
            principal = app.state.auth_provider.switch_active_tenant(
                request.state.bearer_token, tenant_id
            )
            switched = context_for(principal, ctx.request_id)
            app.state.audit.record(
                tenant_id=switched.tenant_id,
                principal_id=ctx.principal_id,
                request_id=ctx.request_id,
                action="active_tenant_changed",
                resource_type="tenant",
                resource_id=switched.tenant_id,
            )
            return {"active_tenant_id": switched.tenant_id, "role": switched.role}

        return app.state.idempotency.execute(
            tenant_id=tenant_id,
            operation=f"switch_active_tenant:{ctx.principal_id}",
            key=idempotency_key,
            payload={"tenant_id": tenant_id},
            action=action,
        )

    @app.get("/v1/metadata")
    def metadata(ctx: TenantContext = Depends(require_tenant)) -> dict[str, Any]:
        return {
            "categories": list(CATEGORIES),
            "h3_resolution": demo_data.H3_RESOLUTION,
            "forecast_window_minutes": 360,
            "forecast_data": (
                "synthetic_demo"
                if active_settings.synthetic_demo_forecasts
                else "operational"
            ),
            "limitations": LIMITATIONS,
        }

    @app.get("/v1/sources")
    def sources(ctx: TenantContext = Depends(require_tenant)) -> dict[str, Any]:
        source_ids = app.state.video_service.store.list_source_ids(ctx.tenant_id)
        records = [
            app.state.video_service.store.get_source(ctx.tenant_id, source_id)
            for source_id in source_ids
        ]
        return {"items": [_public_source(item) for item in records]}

    @app.get("/v1/sources/{source_id}/map-location")
    def source_map_location(
        source_id: uuid.UUID,
        ctx: TenantContext = Depends(require_tenant),
    ) -> dict[str, Any]:
        source = app.state.video_service.store.get_source(ctx.tenant_id, str(source_id))
        try:
            location = app.state.video_service.location_resolver.resolve(
                ctx.tenant_id, source["location_ref"]
            )
        except VideoPipelineError as error:
            raise problem(
                404,
                "source_location_unavailable",
                "A map location has not been configured for this source",
            ) from error
        import h3

        cell_id = h3.latlng_to_cell(
            location["latitude"], location["longitude"], demo_data.H3_RESOLUTION
        )
        return {
            "source_id": str(source_id),
            "source_name": source["name"],
            "cell_id": cell_id,
            "h3_resolution": demo_data.H3_RESOLUTION,
            "precision": "h3_area",
        }

    def create_source_record(
        body: RecordedSourceCreate,
        ctx: TenantContext,
        idempotency_key: str | None,
        mode: str,
        connection: dict[str, str],
    ) -> dict[str, Any]:
        def action() -> dict[str, Any]:
            source = _new_source(
                tenant_id=ctx.tenant_id, body=body, mode=mode, connection=connection
            )
            app.state.sources.setdefault(ctx.tenant_id, []).append(source)
            app.state.video_service.register_source(
                source, authenticated_tenant_id=ctx.tenant_id
            )
            app.state.audit.record(
                tenant_id=ctx.tenant_id,
                principal_id=ctx.principal_id,
                request_id=ctx.request_id,
                action="source_registered",
                resource_type="camera_source",
                resource_id=source["source_id"],
            )
            return _public_source(source)

        return app.state.idempotency.execute(
            tenant_id=ctx.tenant_id,
            operation=f"create_{mode}_source",
            key=idempotency_key,
            payload=body.model_dump(),
            action=action,
        )

    @app.post("/v1/sources/recorded-video", status_code=201)
    def create_recorded_source(
        body: RecordedSourceCreate,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        ctx: TenantContext = Depends(require_owner),
    ) -> dict[str, Any]:
        return create_source_record(
            body,
            ctx,
            idempotency_key,
            "recorded_video",
            {"transport": "uploaded_asset"},
        )

    @app.post("/v1/sources/live-camera", status_code=201)
    def create_live_source(
        body: LiveSourceCreate,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        ctx: TenantContext = Depends(require_owner),
    ) -> dict[str, Any]:
        return create_source_record(
            body,
            ctx,
            idempotency_key,
            "live_camera",
            {
                "transport": body.transport,
                "endpoint_ref": (
                    f"secret://tenant/{ctx.tenant_id}/connections/"
                    f"{body.connection_secret_id}/endpoint"
                ),
                "credential_ref": (
                    f"secret://tenant/{ctx.tenant_id}/connections/"
                    f"{body.connection_secret_id}/credentials"
                ),
            },
        )

    def process_asset_run(run_id: str, tenant_id: str, asset_id: str) -> None:
        run = app.state.video_runs[run_id]
        try:
            run.update(
                state="running",
                stage="reka_upload",
                updated_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            )
            candidates_found: list[dict[str, Any]] = []
            for poll in range(active_settings.reka_index_max_polls):
                candidates_found = app.state.video_service.process_asset(
                    tenant_id, asset_id
                )
                mapping = app.state.video_service.store.get_mapping(tenant_id, asset_id)
                if mapping and mapping["indexing_status"] == "indexed":
                    break
                run.update(
                    stage="reka_indexing",
                    updated_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                )
                if poll + 1 < active_settings.reka_index_max_polls:
                    time.sleep(active_settings.reka_index_poll_seconds)
            else:
                raise VideoPipelineError(
                    "reka_index_timeout",
                    "Reka indexing did not complete in the bounded polling window",
                    retryable=True,
                )
            run.update(
                state="completed",
                stage="awaiting_human_review",
                candidate_count=len(candidates_found),
                updated_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            )
        except VideoPipelineError as error:
            run.update(
                state="failed",
                stage="failed",
                error_code=error.code,
                updated_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            )

    def enqueue_asset_run(tenant_id: str, asset_id: str, label: str) -> dict[str, Any]:
        now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        if app.state.video_broker is not None:
            job = app.state.video_service.store.enqueue(tenant_id, asset_id, "upload")
            app.state.video_broker.publish(
                JobMessage(tenant_id, job["job_id"], "upload")
            )
            return {
                "run_id": job["job_id"],
                "tenant_id": tenant_id,
                "state": job["state"],
                "stage": "accepted",
                "label": label,
                "asset_id": asset_id,
                "candidate_count": 0,
                "analysis_mode": app.state.vision_mode,
                "created_at": job.get("created_at", now),
                "updated_at": job.get("updated_at", now),
            }
        run_id = str(uuid.uuid4())
        run = {
            "run_id": run_id,
            "tenant_id": tenant_id,
            "state": "queued",
            "stage": "accepted",
            "label": label,
            "asset_id": asset_id,
            "candidate_count": 0,
            "analysis_mode": app.state.vision_mode,
            "created_at": now,
            "updated_at": now,
        }
        app.state.video_runs[run_id] = run
        threading.Thread(
            target=process_asset_run,
            args=(run_id, tenant_id, asset_id),
            name=f"video-worker-{run_id[:8]}",
            daemon=True,
        ).start()
        return run

    @app.post("/v1/video-assets/uploads", status_code=202)
    async def create_video_upload(
        source_id: uuid.UUID = Form(...),
        captured_start: str = Form(...),
        captured_end: str = Form(...),
        consent_confirmed: bool = Form(...),
        file: UploadFile = File(...),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        ctx: TenantContext = Depends(require_owner),
    ) -> dict[str, Any]:
        supplied_content_type = (
            (file.content_type or "").split(";", 1)[0].strip().lower()
        )
        if supplied_content_type not in {"video/mp4", "video/webm"}:
            await file.close()
            raise problem(
                415,
                "video_type_invalid",
                "Only MP4 or WebM mobile video is accepted",
            )
        upload_id = str(uuid.uuid4())
        suffix = ".mp4" if supplied_content_type == "video/mp4" else ".webm"
        destination = (
            app.state.video_service.media_root / ctx.tenant_id / f"{upload_id}{suffix}"
        )
        converted = destination.with_suffix(".mp4")
        destination.parent.mkdir(parents=True, exist_ok=True)
        checksum = hashlib.sha256()
        received_bytes = 0
        try:
            with destination.open("wb") as handle:
                while chunk := await file.read(1024 * 1024):
                    received_bytes += len(chunk)
                    if received_bytes > app.state.video_service.max_upload_bytes:
                        raise problem(
                            413,
                            "video_size_invalid",
                            "Video exceeds the configured upload limit",
                        )
                    checksum.update(chunk)
                    handle.write(chunk)
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        finally:
            await file.close()
        checksum_hex = checksum.hexdigest()
        accepted_persisted = False

        def accept() -> dict[str, Any]:
            nonlocal accepted_persisted
            accepted_path = destination
            try:
                if supplied_content_type == "video/webm":
                    # Scan the original container before invoking a media decoder,
                    # then scan the resulting MP4 again in accept_upload.
                    app.state.video_service.media_scanner.scan(destination)
                    app.state.media_transcoder.transcode(destination, converted)
                    if (
                        converted.stat().st_size
                        > app.state.video_service.max_upload_bytes
                    ):
                        raise problem(
                            413,
                            "video_size_invalid",
                            "Converted video exceeds the configured upload limit",
                        )
                    accepted_path = converted
                accepted_checksum = _file_sha256(accepted_path)
                asset = app.state.video_service.accept_upload(
                    authenticated_tenant_id=ctx.tenant_id,
                    source_id=str(source_id),
                    path=accepted_path,
                    content_type="video/mp4",
                    captured_start=captured_start,
                    captured_end=captured_end,
                    duration_seconds=None,
                    consent_confirmed=consent_confirmed,
                    expected_sha256=accepted_checksum,
                )
                accepted_persisted = True
            except Exception:
                destination.unlink(missing_ok=True)
                converted.unlink(missing_ok=True)
                raise
            finally:
                if supplied_content_type == "video/webm":
                    destination.unlink(missing_ok=True)
            run = enqueue_asset_run(
                ctx.tenant_id, asset["asset_id"], "recorded video upload"
            )
            app.state.audit.record(
                tenant_id=ctx.tenant_id,
                principal_id=ctx.principal_id,
                request_id=ctx.request_id,
                action="video_upload_accepted",
                resource_type="video_asset",
                resource_id=asset["asset_id"],
            )
            return _public_run(run)

        try:
            return app.state.idempotency.execute(
                tenant_id=ctx.tenant_id,
                operation="create_video_upload",
                key=idempotency_key,
                payload={
                    "source_id": str(source_id),
                    "captured_start": captured_start,
                    "captured_end": captured_end,
                    "consent_confirmed": consent_confirmed,
                    "content_type": supplied_content_type,
                    "sha256": checksum_hex,
                },
                action=accept,
            )
        finally:
            if supplied_content_type == "video/webm" or not accepted_persisted:
                destination.unlink(missing_ok=True)
            if not accepted_persisted:
                converted.unlink(missing_ok=True)

    def ensure_demo_hls_source(tenant_id: str) -> dict[str, Any]:
        try:
            return app.state.video_service.store.get_source(
                tenant_id, DEMO_HLS_SOURCE_ID
            )
        except VideoPipelineError:
            now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
            definition = app.state.hls_capture.source("louisiana-dot-i20")
            source = {
                "schema_version": "1.0.0",
                "tenant_id": tenant_id,
                "source_id": DEMO_HLS_SOURCE_ID,
                "name": definition.name,
                "mode": "live_camera",
                "status": "active",
                "timezone": "UTC",
                "location_ref": _demo_hls_location_ref(tenant_id),
                "connection": {
                    "transport": "hls",
                    "endpoint_ref": "secret://demo-public-hls/louisiana-dot-i20/endpoint",
                },
                "retention_policy_days": 1,
                "created_at": now,
                "updated_at": now,
            }
            app.state.video_service.register_source(
                source, authenticated_tenant_id=tenant_id
            )
            app.state.sources.setdefault(tenant_id, []).append(source)
            return source

    def ensure_demo_simulated_source(tenant_id: str) -> dict[str, Any]:
        try:
            return app.state.video_service.store.get_source(
                tenant_id, DEMO_SIMULATED_SOURCE_ID
            )
        except VideoPipelineError:
            now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
            source = {
                "schema_version": "1.0.0",
                "tenant_id": tenant_id,
                "source_id": DEMO_SIMULATED_SOURCE_ID,
                "name": "Synthetic road simulation",
                "mode": "live_camera",
                "status": "active",
                "timezone": "UTC",
                "location_ref": _demo_hls_location_ref(tenant_id),
                "connection": {
                    "transport": "hls",
                    "endpoint_ref": "secret://demo-simulated-road/renderer",
                },
                "retention_policy_days": 1,
                "created_at": now,
                "updated_at": now,
            }
            app.state.video_service.register_source(
                source, authenticated_tenant_id=tenant_id
            )
            app.state.sources.setdefault(tenant_id, []).append(source)
            return source

    def capture_near_live_run(run_id: str, tenant_id: str, source_key: str) -> None:
        run = app.state.video_runs[run_id]
        try:
            run.update(
                state="running",
                stage="capturing_hls",
                updated_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            )
            source = ensure_demo_hls_source(tenant_id)
            destination = (
                app.state.video_service.media_root / tenant_id / f"{run_id}.mp4"
            )
            segment = app.state.hls_capture.capture(
                source_key, destination, duration_seconds=run["capture_seconds"]
            )
            duration = app.state.video_service.media_inspector.duration_seconds(
                segment.path
            )
            start = datetime.fromisoformat(segment.captured_start)
            end = start + timedelta(seconds=duration)
            asset = app.state.video_service.accept_upload(
                authenticated_tenant_id=tenant_id,
                source_id=source["source_id"],
                path=segment.path,
                content_type="video/mp4",
                captured_start=segment.captured_start,
                captured_end=end.isoformat().replace("+00:00", "Z"),
                duration_seconds=duration,
                consent_confirmed=True,
                kind="live_segment",
            )
            run.update(asset_id=asset["asset_id"], stage="segment_validated")
            if app.state.video_broker is not None:
                durable = enqueue_asset_run(
                    tenant_id, asset["asset_id"], "near-live CCTV segment"
                )
                run.update(
                    state="running",
                    stage="reka_upload_queued",
                    durable_run_id=durable["run_id"],
                    updated_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                )
            else:
                process_asset_run(run_id, tenant_id, asset["asset_id"])
        except VideoPipelineError as error:
            run.update(
                state="failed",
                stage="failed",
                error_code=error.code,
                updated_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            )

    def capture_simulated_run(run_id: str, tenant_id: str) -> None:
        run = app.state.video_runs[run_id]
        try:
            run.update(
                state="running",
                stage="generating_simulated_segment",
                updated_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            )
            source = ensure_demo_simulated_source(tenant_id)
            destination = (
                app.state.video_service.media_root / tenant_id / f"{run_id}.mp4"
            )
            segment = app.state.simulated_capture.capture(
                destination, duration_seconds=run["capture_seconds"]
            )
            duration = app.state.video_service.media_inspector.duration_seconds(
                segment.path
            )
            start = datetime.fromisoformat(segment.captured_start)
            end = start + timedelta(seconds=duration)
            asset = app.state.video_service.accept_upload(
                authenticated_tenant_id=tenant_id,
                source_id=source["source_id"],
                path=segment.path,
                content_type="video/mp4",
                captured_start=segment.captured_start,
                captured_end=end.isoformat().replace("+00:00", "Z"),
                duration_seconds=duration,
                consent_confirmed=True,
                kind="live_segment",
            )
            run.update(asset_id=asset["asset_id"], stage="segment_validated")
            if app.state.video_broker is not None:
                durable = enqueue_asset_run(
                    tenant_id, asset["asset_id"], "simulated live segment"
                )
                run.update(
                    state="running",
                    stage="reka_upload_queued",
                    durable_run_id=durable["run_id"],
                    updated_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                )
            else:
                process_asset_run(run_id, tenant_id, asset["asset_id"])
        except VideoPipelineError as error:
            run.update(
                state="failed",
                stage="failed",
                error_code=error.code,
                updated_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            )

    @app.post("/v1/demo/near-live-cctv/captures", status_code=202)
    def start_near_live_capture(
        body: NearLiveCaptureRequest,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        ctx: TenantContext = Depends(require_owner),
    ) -> dict[str, Any]:
        if not public_hls_enabled:
            raise problem(
                404,
                "demo_capture_disabled",
                "The public demonstration camera is disabled in production",
            )
        duration = body.duration_seconds or active_settings.near_live_capture_seconds

        def start() -> dict[str, Any]:
            definition = app.state.hls_capture.source(body.source_key)
            now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
            run_id = str(uuid.uuid4())
            run = {
                "run_id": run_id,
                "tenant_id": ctx.tenant_id,
                "state": "queued",
                "stage": "capturing_hls",
                "label": "near-live CCTV segment",
                "source_name": definition.name,
                "source_attribution": definition.attribution,
                "capture_seconds": duration,
                "candidate_count": 0,
                "analysis_mode": app.state.vision_mode,
                "created_at": now,
                "updated_at": now,
            }
            app.state.video_runs[run_id] = run
            response = _public_run(run)
            threading.Thread(
                target=capture_near_live_run,
                args=(run_id, ctx.tenant_id, body.source_key),
                name=f"hls-capture-{run_id[:8]}",
                daemon=True,
            ).start()
            app.state.audit.record(
                tenant_id=ctx.tenant_id,
                principal_id=ctx.principal_id,
                request_id=ctx.request_id,
                action="near_live_capture_started",
                resource_type="ingestion_run",
                resource_id=run_id,
            )
            return response

        return app.state.idempotency.execute(
            tenant_id=ctx.tenant_id,
            operation="near_live_cctv_capture",
            key=idempotency_key,
            payload=body.model_dump() | {"resolved_duration_seconds": duration},
            action=start,
        )

    @app.post("/v1/demo/simulated-cctv/captures", status_code=202)
    def start_simulated_capture(
        body: SimulatedCaptureRequest,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        ctx: TenantContext = Depends(require_owner),
    ) -> dict[str, Any]:
        if not public_hls_enabled:
            raise problem(
                404,
                "demo_capture_disabled",
                "Simulated capture is disabled in production",
            )

        def start() -> dict[str, Any]:
            now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
            run_id = str(uuid.uuid4())
            run = {
                "run_id": run_id,
                "tenant_id": ctx.tenant_id,
                "state": "queued",
                "stage": "generating_simulated_segment",
                "label": "simulated live segment",
                "source_name": "Synthetic road simulation",
                "source_attribution": "Generated locally · no real people or events",
                "capture_seconds": body.duration_seconds,
                "candidate_count": 0,
                "analysis_mode": app.state.vision_mode,
                "created_at": now,
                "updated_at": now,
            }
            app.state.video_runs[run_id] = run
            response = _public_run(run)
            threading.Thread(
                target=capture_simulated_run,
                args=(run_id, ctx.tenant_id),
                name=f"simulated-capture-{run_id[:8]}",
                daemon=True,
            ).start()
            app.state.audit.record(
                tenant_id=ctx.tenant_id,
                principal_id=ctx.principal_id,
                request_id=ctx.request_id,
                action="simulated_capture_started",
                resource_type="ingestion_run",
                resource_id=run_id,
            )
            return response

        return app.state.idempotency.execute(
            tenant_id=ctx.tenant_id,
            operation="simulated_cctv_capture",
            key=idempotency_key,
            payload=body.model_dump(),
            action=start,
        )

    @app.get("/v1/ingestion/runs")
    def ingestion_runs(
        limit: int = Query(default=25, ge=1, le=100),
        ctx: TenantContext = Depends(require_tenant),
    ) -> dict[str, Any]:
        jobs = app.state.video_service.store.list_jobs(ctx.tenant_id, limit=limit)
        candidate_counts: dict[str, int] = {}
        for candidate_record in app.state.video_service.store.list_candidates(
            ctx.tenant_id
        ):
            asset_id = candidate_record["asset_id"]
            candidate_counts[asset_id] = candidate_counts.get(asset_id, 0) + 1
        return {
            "items": [
                _public_run(
                    {
                        "run_id": job["job_id"],
                        "state": job["state"],
                        "stage": job["operation"],
                        "label": "recorded video processing",
                        "asset_id": job["asset_id"],
                        "candidate_count": candidate_counts.get(job["asset_id"], 0),
                        "analysis_mode": app.state.vision_mode,
                        "error_code": job.get("last_error_code"),
                        "created_at": job["created_at"],
                        "updated_at": job["updated_at"],
                    }
                )
                for job in jobs
            ]
        }

    @app.get("/v1/ingestion/runs/{run_id}")
    def ingestion_run(
        run_id: uuid.UUID, ctx: TenantContext = Depends(require_tenant)
    ) -> dict[str, Any]:
        run = app.state.video_runs.get(str(run_id))
        if run is not None and run["tenant_id"] == ctx.tenant_id:
            durable_run_id = run.get("durable_run_id")
            if durable_run_id:
                try:
                    job = app.state.video_service.store.get_job(
                        ctx.tenant_id, durable_run_id
                    )
                except VideoPipelineError:
                    raise problem(
                        404, "ingestion_run_not_found", "Ingestion run was not found"
                    )
                durable = _durable_run(
                    app.state.video_service.store,
                    ctx.tenant_id,
                    job,
                    app.state.vision_mode,
                )
                return durable | {
                    "run_id": str(run_id),
                    "label": run.get("label", "near-live CCTV segment"),
                    "source_name": run.get("source_name"),
                    "source_attribution": run.get("source_attribution"),
                    "capture_seconds": run.get("capture_seconds"),
                }
            return _public_run(run)
        if app.state.video_broker is not None:
            try:
                job = app.state.video_service.store.get_job(ctx.tenant_id, str(run_id))
            except VideoPipelineError:
                raise problem(
                    404, "ingestion_run_not_found", "Ingestion run was not found"
                )
            return _durable_run(
                app.state.video_service.store,
                ctx.tenant_id,
                job,
                app.state.vision_mode,
            )
        raise problem(404, "ingestion_run_not_found", "Ingestion run was not found")

    @app.post("/v1/ingestion/runs/{run_id}/reanalyze", status_code=202)
    def reanalyze_ingestion_run(
        run_id: uuid.UUID,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        ctx: TenantContext = Depends(require_owner),
    ) -> dict[str, Any]:
        def action() -> dict[str, Any]:
            assert idempotency_key is not None
            if app.state.video_broker is None:
                raise problem(
                    503,
                    "reanalysis_unavailable",
                    "Durable video re-analysis is unavailable",
                    retryable=True,
                )
            job = app.state.video_service.request_reanalysis(
                ctx.tenant_id,
                str(run_id),
                idempotency_key=idempotency_key,
            )
            app.state.video_broker.publish(
                JobMessage(ctx.tenant_id, job["job_id"], "analyze")
            )
            app.state.audit.record(
                tenant_id=ctx.tenant_id,
                principal_id=ctx.principal_id,
                request_id=ctx.request_id,
                action="video_reanalysis_requested",
                resource_type="ingestion_run",
                resource_id=str(run_id),
            )
            return _public_run(
                {
                    "run_id": job["job_id"],
                    "state": job["state"],
                    "stage": "analyze",
                    "label": "controlled video re-analysis",
                    "asset_id": job["asset_id"],
                    "candidate_count": 0,
                    "analysis_mode": app.state.vision_mode,
                    "created_at": job["created_at"],
                    "updated_at": job["updated_at"],
                }
            )

        return app.state.idempotency.execute(
            tenant_id=ctx.tenant_id,
            operation=f"reanalyze_ingestion_run:{run_id}",
            key=idempotency_key,
            payload={"run_id": str(run_id)},
            action=action,
        )

    @app.get("/v1/candidate-detections")
    def candidates(
        limit: int = Query(default=50, ge=1, le=100),
        ctx: TenantContext = Depends(require_reviewer),
    ) -> dict[str, Any]:
        durable = app.state.video_service.store.list_candidates(ctx.tenant_id)
        fixture_records = (
            []
            if production
            else list(app.state.candidates.get(ctx.tenant_id, {}).values())
        )
        seen = {record["detection_id"] for record in durable}
        records = (
            durable
            + [
                record
                for record in fixture_records
                if record["detection_id"] not in seen
            ]
        )[:limit]
        return {
            "items": [
                {
                    key: value
                    for key, value in record.items()
                    if key not in {"evidence_ref"}
                }
                | {
                    "evidence_available": True,
                    "record_type": "unconfirmed_candidate_detection",
                }
                for record in records
            ],
            "limitations": [
                "Candidates are unconfirmed machine proposals and are not declarations that a crime occurred.",
                "Human review is required before any candidate can enter aggregate incident history.",
            ],
        }

    @app.get("/v1/candidate-detections/{detection_id}/evidence")
    def candidate_evidence(
        detection_id: uuid.UUID,
        ctx: TenantContext = Depends(require_reviewer),
    ) -> FileResponse:
        try:
            candidate = app.state.video_service.store.get_candidate(
                ctx.tenant_id, str(detection_id)
            )
        except VideoPipelineError:
            raise problem(404, "candidate_not_found", "Candidate was not found")
        if parse_utc(candidate["expires_at"]) <= datetime.now(UTC):
            raise problem(410, "evidence_expired", "Candidate evidence has expired")
        materialized = app.state.video_service.candidate_evidence(
            ctx.tenant_id,
            candidate["asset_id"],
            candidate["occurred_at"],
        )
        path = materialized.__enter__()
        app.state.audit.record(
            tenant_id=ctx.tenant_id,
            principal_id=ctx.principal_id,
            request_id=ctx.request_id,
            action="candidate_evidence_viewed",
            resource_type="candidate_detection",
            resource_id=str(detection_id),
        )
        return FileResponse(
            path,
            media_type="video/mp4",
            headers={
                "Cache-Control": "private, no-store, max-age=0",
                "Content-Disposition": f'inline; filename="candidate-{detection_id}.mp4"',
                "X-Content-Type-Options": "nosniff",
            },
            background=BackgroundTask(materialized.__exit__, None, None, None),
        )

    @app.post("/v1/candidate-detections/{detection_id}/review", status_code=201)
    def review_candidate(
        detection_id: str,
        body: ReviewRequest,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        ctx: TenantContext = Depends(require_reviewer),
    ) -> dict[str, Any]:
        durable_candidate: dict[str, Any] | None = None
        try:
            durable_candidate = app.state.video_service.store.get_candidate(
                ctx.tenant_id, detection_id
            )
        except VideoPipelineError:
            pass
        candidate_record = durable_candidate or (
            None
            if production
            else app.state.candidates.get(ctx.tenant_id, {}).get(detection_id)
        )
        if candidate_record is None:
            raise problem(
                404, "candidate_not_found", "Candidate detection was not found"
            )
        if body.decision == "confirmed" and not body.confirmed_category:
            raise problem(
                422,
                "confirmed_category_required",
                "Confirmed reviews require a category",
            )
        if body.decision == "rejected" and not body.rejection_reason:
            raise problem(
                422, "rejection_reason_required", "Rejected reviews require a reason"
            )

        def action() -> dict[str, Any]:
            if durable_candidate is not None:
                result = app.state.video_service.review_candidate(
                    authenticated_tenant_id=ctx.tenant_id,
                    detection_id=detection_id,
                    decision=body.decision,
                    confirmed_category=body.confirmed_category,
                    rejection_reason=body.rejection_reason,
                    reviewed_by=ctx.principal_id,
                    role=ctx.role,
                )
                app.state.audit.record(
                    tenant_id=ctx.tenant_id,
                    principal_id=ctx.principal_id,
                    request_id=ctx.request_id,
                    action=f"candidate_{body.decision}",
                    resource_type="candidate_detection",
                    resource_id=detection_id,
                )
                return result
            review_key = (ctx.tenant_id, detection_id)
            if review_key in app.state.reviews:
                raise problem(409, "review_final", "A final review already exists")
            result = {
                "schema_version": "1.0.0",
                "tenant_id": ctx.tenant_id,
                "review_id": str(uuid.uuid4()),
                "detection_id": detection_id,
                "decision": body.decision,
                "reviewed_by": ctx.principal_id,
                "reviewed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            }
            if body.decision == "confirmed":
                result["confirmed_category"] = body.confirmed_category
                result["promoted_external_event_id"] = f"detection:{detection_id}"
            else:
                result["rejection_reason"] = body.rejection_reason
            validate_contract("candidate-review", result)
            app.state.reviews[review_key] = result
            candidate_record["review_status"] = body.decision
            app.state.audit.record(
                tenant_id=ctx.tenant_id,
                principal_id=ctx.principal_id,
                request_id=ctx.request_id,
                action=f"candidate_{body.decision}",
                resource_type="candidate_detection",
                resource_id=detection_id,
            )
            return result

        return app.state.idempotency.execute(
            tenant_id=ctx.tenant_id,
            operation=f"review_candidate:{detection_id}",
            key=idempotency_key,
            payload=body.model_dump(),
            action=action,
        )

    @app.get("/v1/coverage")
    def coverage(ctx: TenantContext = Depends(require_tenant)) -> dict[str, Any]:
        if durable_mode:
            return {
                "items": app.state.video_service.store.list_coverage(
                    ctx.tenant_id, limit=100
                )
            }
        fixture = _load_fixture("coverage-snapshot")
        fixture["tenant_id"] = ctx.tenant_id
        return {"items": [fixture]}

    @app.post("/v1/demo/forecasts/refresh", status_code=201)
    def refresh_demo_forecasts(
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        ctx: TenantContext = Depends(require_owner),
    ) -> dict[str, Any]:
        if forecast_refresher is None:
            raise problem(
                404,
                "demo_refresh_unavailable",
                "Integrated demo refresh is unavailable",
            )

        def action() -> dict[str, Any]:
            result = forecast_refresher(ctx.tenant_id, datetime.now(UTC))
            app.state.audit.record(
                tenant_id=ctx.tenant_id,
                principal_id=ctx.principal_id,
                request_id=ctx.request_id,
                action="demo_forecast_refreshed",
                resource_type="forecast_window",
                resource_id=result["window_start"],
            )
            return result

        return app.state.idempotency.execute(
            tenant_id=ctx.tenant_id,
            operation="demo_forecast_refresh",
            key=idempotency_key,
            payload={"tenant_id": ctx.tenant_id},
            action=action,
        )

    @app.get("/v1/forecasts")
    def forecasts(
        window_start: str = Query(...),
        category: str = Query(...),
        bbox: str | None = Query(default=None),
        page: int = Query(default=1, ge=1, le=10000),
        page_size: int = Query(default=50, ge=1, le=100),
        ctx: TenantContext = Depends(require_tenant),
    ) -> dict[str, Any]:
        if category not in CATEGORIES:
            raise problem(422, "invalid_category", "Category is not allowlisted")
        start = parse_utc(window_start)
        now = datetime.now(UTC)
        if start <= now:
            raise problem(
                422, "window_not_future", "Forecast window must start in the future"
            )
        if start > now + timedelta(days=7):
            raise problem(
                422, "window_too_distant", "Forecast horizon is limited to seven days"
            )
        bounds = _parse_bbox(bbox)
        start_text = start.isoformat().replace("+00:00", "Z")
        items = app.state.forecast_orchestrator.repository.list_window(
            ctx.tenant_id, start_text, category
        )
        if not items and (not production or active_settings.synthetic_demo_forecasts):
            measured_coverage = (
                1.0
                if active_settings.synthetic_demo_forecasts
                else coverage_provider(ctx.tenant_id, start_text)
                if coverage_provider is not None
                else 0.0
            )
            rows = [
                _future_row(
                    ctx.tenant_id,
                    cell,
                    start,
                    category,
                    coverage_ratio=measured_coverage,
                )
                for cell in demo_data.tenant_cells(ctx.tenant_id)
            ]
            items = app.state.forecast_orchestrator.publish_future_rows(
                ctx.tenant_id, rows, generated_at=now
            )
        if not items:
            raise problem(
                404,
                "forecast_window_not_generated",
                "The scheduled forecast window has not been published",
            )
        if bounds is not None:
            import h3

            west, south, east, north = bounds

            def inside_bounds(item: dict[str, Any]) -> bool:
                latitude, longitude = h3.cell_to_latlng(item["cell_id"])
                return south <= latitude <= north and west <= longitude <= east

            items = [item for item in items if inside_bounds(item)]
        total = len(items)
        offset = (page - 1) * page_size
        selected = items[offset : offset + page_size]
        return {"items": selected, "page": page, "page_size": page_size, "total": total}

    @app.get("/v1/forecasts/{forecast_id}")
    def forecast_detail(
        forecast_id: str, ctx: TenantContext = Depends(require_tenant)
    ) -> dict[str, Any]:
        item = app.state.forecast_orchestrator.repository.get(
            ctx.tenant_id, forecast_id
        )
        if item is None:
            raise problem(404, "forecast_not_found", "Forecast was not found")
        return item

    @app.get("/v1/model-card")
    def model_card(ctx: TenantContext = Depends(require_tenant)) -> dict[str, Any]:
        if app.state.model_registry is not None:
            approved_card = app.state.model_registry.model_card_for(ctx.tenant_id)
            if approved_card is not None:
                return approved_card
        if production:
            raise problem(
                404, "approved_model_not_found", "No approved model is active"
            )
        card = _load_fixture("model-card")
        card["tenant_id"] = ctx.tenant_id
        return card

    @app.get("/v1/model-registry")
    def model_registry_status(
        ctx: TenantContext = Depends(require_operator),
    ) -> dict[str, Any]:
        if app.state.model_registry is None:
            return {"active_model_version": None, "history": []}
        return app.state.model_registry.status(ctx.tenant_id)

    @app.post("/v1/model-registry/promotions", status_code=201)
    def promote_model(
        body: ModelPromotionRequest,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        ctx: TenantContext = Depends(require_operator),
    ) -> dict[str, Any]:
        if app.state.model_registry is None:
            raise problem(
                503,
                "model_registry_unavailable",
                "Model registry is unavailable",
                retryable=True,
            )

        def action() -> dict[str, Any]:
            result = app.state.model_registry.promote(
                ctx.tenant_id,
                body.model_version,
                approved_by=ctx.principal_id,
                reason=body.reason,
            )
            app.state.audit.record(
                tenant_id=ctx.tenant_id,
                principal_id=ctx.principal_id,
                request_id=ctx.request_id,
                action="model_promoted",
                resource_type="model",
                resource_id=body.model_version,
            )
            return result

        return app.state.idempotency.execute(
            tenant_id=ctx.tenant_id,
            operation=f"promote_model:{body.model_version}",
            key=idempotency_key,
            payload=body.model_dump(),
            action=action,
        )

    @app.post("/v1/model-registry/rollback", status_code=201)
    def rollback_model(
        body: ModelRollbackRequest,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        ctx: TenantContext = Depends(require_operator),
    ) -> dict[str, Any]:
        if app.state.model_registry is None:
            raise problem(
                503,
                "model_registry_unavailable",
                "Model registry is unavailable",
                retryable=True,
            )

        def action() -> dict[str, Any]:
            result = app.state.model_registry.rollback(
                ctx.tenant_id, approved_by=ctx.principal_id, reason=body.reason
            )
            app.state.audit.record(
                tenant_id=ctx.tenant_id,
                principal_id=ctx.principal_id,
                request_id=ctx.request_id,
                action="model_rolled_back",
                resource_type="model",
                resource_id=result["model_version"],
            )
            return result

        return app.state.idempotency.execute(
            tenant_id=ctx.tenant_id,
            operation="rollback_model",
            key=idempotency_key,
            payload=body.model_dump(),
            action=action,
        )

    @app.post("/v1/ai/copilot/messages")
    def copilot(
        body: CopilotMessage, ctx: TenantContext = Depends(require_tenant)
    ) -> dict[str, Any]:
        return reka.answer_question(ctx.tenant_id, body.question, provider)

    # Legacy routes remain read-only while Person 3 migrates to the frozen API.
    @app.get("/v1/risk", include_in_schema=False)
    def legacy_risk(
        window_start: str = Query(...),
        category: str = Query("all"),
        ctx: TenantContext = Depends(require_tenant),
    ) -> dict[str, Any]:
        if production:
            raise problem(404, "endpoint_not_found", "Endpoint is not available")
        valid_windows = {item["window_start"] for item in demo_data.windows()}
        if window_start not in valid_windows:
            raise problem(422, "invalid_window", "Window is not available")
        if category not in demo_data.CATEGORIES:
            raise problem(422, "invalid_category", "Category is not available")
        return demo_data.risk_feature_collection(ctx.tenant_id, window_start, category)

    @app.get("/v1/cells/{cell_id}/explanation", include_in_schema=False)
    def legacy_explanation(
        cell_id: str,
        window_start: str = Query(...),
        category: str = Query("all"),
        ctx: TenantContext = Depends(require_tenant),
    ) -> dict[str, Any]:
        if production:
            raise problem(404, "endpoint_not_found", "Endpoint is not available")
        if cell_id not in set(demo_data.tenant_cells(ctx.tenant_id)):
            raise problem(404, "cell_not_found", "Cell was not found")
        return {
            "prediction": demo_data.prediction_for(
                ctx.tenant_id, cell_id, window_start, category
            ),
            "recent_trend": demo_data.recent_trend(ctx.tenant_id, cell_id, category),
            "limitations": LIMITATIONS,
        }

    return app


# Importing the reusable factory must not instantiate development adapters in a
# production process. Production deployments use
# ``src.data.video.production_app:app`` which injects all durable dependencies.
app = (
    create_app()
    if os.environ.get("APP_ENVIRONMENT", "development").lower() != "production"
    else None
)
