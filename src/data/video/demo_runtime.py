"""Self-contained, Postgres-backed runtime for deployment demonstrations."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from src.api import demo_data, reka
from src.api.app import CATEGORIES, DEMO_HLS_LOCATION_REF, create_app
from src.api.forecasting import ForecastOrchestrator, PostgresForecastRepository
from src.api.settings import Settings
from src.api.state import PostgresAuditLog, PostgresIdempotencyStore
from src.api.tenancy import DevelopmentAuthenticationProvider
from src.data.postgres import PostgresIngestionStore, TenantPostgres
from src.features import FeatureBuildConfig, FutureFeatureBuilder
from src.features.builder import floor_interval
from src.models.operational import ForecastPolicy, ForecastService

from .broker import PostgresJobBroker
from .coverage import StoreCoverageProvider
from .errors import VideoPipelineError
from .postgres import PostgresVideoStore
from .reka import RekaVisionProvider
from .service import VideoPipelineService
from .storage import LocalMediaStorage, NoOpMediaScanner


class DemoLocationResolver:
    """Maps opaque registered locations to tenant-specific demo centres."""

    def resolve(self, tenant_id: str, location_ref: str) -> dict[str, float]:
        expected = f"secret://tenant/{tenant_id}/locations/"
        allowed_demo_public_source = location_ref == DEMO_HLS_LOCATION_REF
        if (
            not (location_ref.startswith(expected) or allowed_demo_public_source)
            or tenant_id not in demo_data.TENANT_CENTRES
        ):
            raise VideoPipelineError(
                "location_unavailable", "Demo location could not be resolved"
            )
        latitude, longitude = demo_data.TENANT_CENTRES[tenant_id]
        return {"latitude": latitude, "longitude": longitude}


@dataclass
class DemoRuntime:
    database: TenantPostgres
    ingestion_store: PostgresIngestionStore
    video_store: PostgresVideoStore
    broker: PostgresJobBroker
    service: VideoPipelineService
    forecasts: ForecastOrchestrator

    def close(self) -> None:
        self.database.close()


def create_demo_runtime() -> DemoRuntime:
    dsn = os.environ.get("DATABASE_URL", "").strip()
    if not dsn:
        raise ValueError("DATABASE_URL is required for the integrated demo")
    media_root = (
        Path(os.environ.get("RUNTIME_DIR", "/app/data/runtime")).resolve()
        / "restricted-media"
    )
    media_root.mkdir(parents=True, exist_ok=True)
    database = TenantPostgres(dsn)
    ingestion = PostgresIngestionStore(database)
    video_store = PostgresVideoStore(database, ingestion)
    broker = PostgresJobBroker(database, visibility_seconds=120)
    settings = Settings.from_environment()
    if not settings.reka_configured:
        raise ValueError("REKA_API_KEY is required for the live CCTV demo")
    provider = RekaVisionProvider(
        settings.reka_api_key,
        base_url=settings.reka_vision_base_url,
        chat_base_url=settings.reka_chat_base_url,
        chat_model=settings.reka_video_model,
        text_model=settings.reka_model,
        timeout_seconds=settings.reka_timeout_seconds,
        use_quick_tag_pipeline=True,
    )
    service = VideoPipelineService(
        video_store,
        provider,
        DemoLocationResolver(),
        media_root=media_root,
        media_storage=LocalMediaStorage(media_root),
        media_scanner=NoOpMediaScanner(),
        max_upload_bytes=64 * 1024 * 1024,
        tenant_quota_bytes=256 * 1024 * 1024,
        max_duration_seconds=10 * 60,
        prompt_version=settings.reka_prompt_version,
    )
    forecast_service = ForecastService(
        policy=ForecastPolicy(minimum_recent_support=1.0, minimum_coverage_ratio=0.5)
    )
    forecasts = ForecastOrchestrator(
        forecast_service, PostgresForecastRepository(database)
    )
    return DemoRuntime(database, ingestion, video_store, broker, service, forecasts)


class DemoForecastRefresher:
    def __init__(self, runtime: DemoRuntime) -> None:
        self.runtime = runtime

    def __call__(self, tenant_id: str, now: datetime) -> dict[str, Any]:
        snapshots = self.runtime.video_store.list_coverage(tenant_id, limit=100)
        source_ids = tuple(dict.fromkeys(item["source_id"] for item in snapshots))
        if not source_ids:
            raise VideoPipelineError(
                "coverage_unavailable",
                "Process a recording before publishing the demo forecast",
            )
        interval = timedelta(hours=6)
        current = now.astimezone(UTC)
        target = floor_interval(current, interval) + interval
        config = FeatureBuildConfig(
            tenant_id=tenant_id,
            source_ids=source_ids,
            start=target,
            end=target + interval,
            interval=interval,
            h3_resolution=demo_data.H3_RESOLUTION,
            domain_cells=tuple(demo_data.tenant_cells(tenant_id)),
            categories=tuple(CATEGORIES),
            coverage_ratio=0.0,
        )
        rows = FutureFeatureBuilder(self.runtime.video_store).build_and_persist(config)
        published = self.runtime.forecasts.publish_future_rows(
            tenant_id, rows, generated_at=current
        )
        return {
            "tenant_id": tenant_id,
            "window_start": published[0]["window_start"],
            "forecast_count": len(published),
            "feature_snapshot_version": rows[0]["feature_snapshot_version"],
            "coverage_ratio": rows[0]["coverage_ratio"],
        }


def build_demo_app():
    runtime = create_demo_runtime()
    settings = Settings.from_environment()
    app = create_app(
        provider=reka.FakeRekaProvider(),
        settings=settings,
        auth_provider=DevelopmentAuthenticationProvider(),
        forecast_service=runtime.forecasts.service,
        coverage_provider=StoreCoverageProvider(runtime.video_store),
        video_service=runtime.service,
        forecast_orchestrator=runtime.forecasts,
        audit_log=PostgresAuditLog(runtime.database),
        idempotency_store=PostgresIdempotencyStore(runtime.database),
        video_broker=runtime.broker,
        forecast_refresher=DemoForecastRefresher(runtime),
        seed_demo_fixtures=False,
        deployment_mode="live_demo",
    )
    app.state.demo_runtime = runtime
    app.router.on_shutdown.append(runtime.close)
    return app
