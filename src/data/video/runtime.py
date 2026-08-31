"""Production Person 1 runtime composition."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.data.postgres import PostgresIngestionStore, TenantPostgres

from .broker import SqsJobBroker
from .postgres import PostgresVideoStore
from .reka import RekaVisionProvider
from .service import LocationResolver, VideoPipelineService
from .storage import ClamAVCommandScanner, S3MediaStorage

_PROMPT_VERSION_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


def _secret_value(name: str) -> str:
    direct = os.environ.get(name, "").strip()
    file_name = os.environ.get(f"{name}_FILE", "").strip()
    if direct and file_name:
        raise ValueError(f"Set only one of {name} or {name}_FILE")
    if not file_name:
        return direct
    path = Path(file_name)
    try:
        if path.stat().st_size > 64 * 1024:
            raise ValueError(f"{name}_FILE exceeds the secret size limit")
        return path.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise ValueError(f"{name}_FILE could not be read") from error


@dataclass(frozen=True)
class PlatformSettings:
    database_url: str = field(repr=False)
    queue_url: str = field(repr=False)
    queue_dlq_url: str = field(repr=False)
    operation_queue_urls: dict[str, str] = field(repr=False)
    operation_dlq_urls: dict[str, str] = field(repr=False)
    media_bucket: str
    media_kms_key_id: str
    media_bucket_owner: str | None
    location_secret_prefix: str
    aws_region: str
    reka_api_key: str = field(repr=False)
    reka_vision_base_url: str
    reka_chat_base_url: str
    reka_model: str
    reka_video_model: str
    reka_prompt_version: str
    reka_timeout_seconds: float
    worker_lease_seconds: int
    max_upload_bytes: int
    restricted_spool_root: Path

    @classmethod
    def from_environment(cls) -> PlatformSettings:
        required = {
            "database_url": "DATABASE_URL",
            "queue_url": "VIDEO_QUEUE_URL",
            "queue_dlq_url": "VIDEO_QUEUE_DLQ_URL",
            "media_bucket": "VIDEO_MEDIA_BUCKET",
            "media_kms_key_id": "VIDEO_MEDIA_KMS_KEY_ID",
            "aws_region": "AWS_REGION",
            "reka_api_key": "REKA_API_KEY",
        }
        values: dict[str, Any] = {}
        missing: list[str] = []
        for setting_field, variable in required.items():
            value = (
                _secret_value(variable)
                if variable in {"DATABASE_URL", "REKA_API_KEY"}
                else os.getenv(variable, "").strip()
            )
            if not value or value.startswith("replace-"):
                missing.append(variable)
            values[setting_field] = value
        if missing:
            raise ValueError(
                f"Missing production platform settings: {', '.join(sorted(missing))}"
            )
        values.update(
            reka_vision_base_url=os.getenv(
                "REKA_VISION_BASE_URL", "https://vision-agent.api.reka.ai"
            ),
            reka_chat_base_url=os.getenv(
                "REKA_CHAT_BASE_URL", "https://api.reka.ai/v1"
            ),
            reka_model=os.getenv("REKA_MODEL", "reka-flash-3"),
            reka_video_model=os.getenv("REKA_VIDEO_MODEL", "reka-edge-2603"),
            reka_prompt_version=os.getenv(
                "REKA_VIDEO_PROMPT_VERSION",
                os.getenv("REKA_PROMPT_VERSION", "1.2.0"),
            ).strip(),
            reka_timeout_seconds=float(os.getenv("REKA_TIMEOUT_SECONDS", "20")),
            worker_lease_seconds=int(os.getenv("VIDEO_WORKER_LEASE_SECONDS", "120")),
            max_upload_bytes=int(
                os.getenv("VIDEO_MAX_UPLOAD_BYTES", str(8 * 1024 * 1024))
            ),
            restricted_spool_root=Path(
                os.getenv("VIDEO_SPOOL_ROOT", "/var/lib/crime-video-spool")
            ),
            media_bucket_owner=os.getenv("VIDEO_MEDIA_BUCKET_OWNER", "").strip()
            or None,
            location_secret_prefix=os.getenv("LOCATION_SECRET_PREFIX", "").strip(),
            operation_queue_urls={
                operation: os.getenv(f"VIDEO_QUEUE_URL_{operation.upper()}", "").strip()
                for operation in ("upload", "index", "analyze", "delete")
                if os.getenv(f"VIDEO_QUEUE_URL_{operation.upper()}", "").strip()
            },
            operation_dlq_urls={
                operation: os.getenv(
                    f"VIDEO_QUEUE_DLQ_URL_{operation.upper()}", ""
                ).strip()
                for operation in ("upload", "index", "analyze", "delete")
                if os.getenv(f"VIDEO_QUEUE_DLQ_URL_{operation.upper()}", "").strip()
            },
        )
        settings = cls(**values)
        if not settings.reka_vision_base_url.startswith("https://"):
            raise ValueError("REKA_VISION_BASE_URL must use HTTPS")
        if not settings.reka_chat_base_url.startswith("https://"):
            raise ValueError("REKA_CHAT_BASE_URL must use HTTPS")
        if _PROMPT_VERSION_PATTERN.fullmatch(settings.reka_prompt_version) is None:
            raise ValueError(
                "REKA_VIDEO_PROMPT_VERSION must be a non-empty, bounded version label"
            )
        if not 1 <= settings.reka_timeout_seconds <= 120:
            raise ValueError("REKA_TIMEOUT_SECONDS must be between 1 and 120")
        if not 30 <= settings.worker_lease_seconds <= 43200:
            raise ValueError("VIDEO_WORKER_LEASE_SECONDS must be between 30 and 43200")
        if not 1024 * 1024 <= settings.max_upload_bytes <= 8 * 1024 * 1024:
            raise ValueError(
                "VIDEO_MAX_UPLOAD_BYTES must be between 1 MiB and the "
                "8 MiB gateway-safe limit"
            )
        if settings.media_bucket_owner is not None and (
            len(settings.media_bucket_owner) != 12
            or not settings.media_bucket_owner.isdecimal()
        ):
            raise ValueError(
                "VIDEO_MEDIA_BUCKET_OWNER must be a 12-digit AWS account ID"
            )
        if not settings.location_secret_prefix:
            raise ValueError("LOCATION_SECRET_PREFIX is required")
        if settings.operation_queue_urls and set(settings.operation_queue_urls) != {
            "upload",
            "index",
            "analyze",
            "delete",
        }:
            raise ValueError(
                "Configure all four operation-specific VIDEO_QUEUE_URL_* values"
            )
        if settings.operation_dlq_urls and set(settings.operation_dlq_urls) != {
            "upload",
            "index",
            "analyze",
            "delete",
        }:
            raise ValueError(
                "Configure all four operation-specific VIDEO_QUEUE_DLQ_URL_* values"
            )
        return settings


@dataclass
class PlatformRuntime:
    database: TenantPostgres
    ingestion_store: PostgresIngestionStore
    video_store: PostgresVideoStore
    media_storage: S3MediaStorage
    broker: SqsJobBroker
    service: VideoPipelineService

    def close(self) -> None:
        self.database.close()


def create_platform_runtime(
    settings: PlatformSettings, *, location_resolver: LocationResolver
) -> PlatformRuntime:
    database = TenantPostgres(settings.database_url)
    ingestion_store = PostgresIngestionStore(database)
    video_store = PostgresVideoStore(database, ingestion_store)
    media_storage = S3MediaStorage(
        bucket=settings.media_bucket,
        kms_key_id=settings.media_kms_key_id,
        expected_bucket_owner=settings.media_bucket_owner,
        region_name=settings.aws_region,
    )
    broker = SqsJobBroker(
        queue_url=settings.queue_url,
        dead_letter_queue_url=settings.queue_dlq_url,
        queue_urls=settings.operation_queue_urls or None,
        dead_letter_queue_urls=settings.operation_dlq_urls or None,
        region_name=settings.aws_region,
        visibility_seconds=settings.worker_lease_seconds,
    )
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
        location_resolver,
        media_root=settings.restricted_spool_root,
        media_storage=media_storage,
        media_scanner=ClamAVCommandScanner(),
        max_upload_bytes=settings.max_upload_bytes,
        prompt_version=settings.reka_prompt_version,
    )
    return PlatformRuntime(
        database, ingestion_store, video_store, media_storage, broker, service
    )
