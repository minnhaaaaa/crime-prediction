"""Validated server configuration with non-serializable secret storage."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _boolean_value(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value not in {"true", "false"}:
        raise ValueError(f"{name} must be true or false")
    return value == "true"


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
class Settings:
    app_environment: str = "development"
    reka_api_key: str = field(default="", repr=False)
    reka_chat_base_url: str = "https://api.reka.ai/v1"
    reka_vision_base_url: str = "https://vision-agent.api.reka.ai"
    reka_model: str = "reka-flash-3"
    reka_video_model: str = "reka-edge-2603"
    reka_prompt_version: str = "1.2.0"
    reka_timeout_seconds: float = 20.0
    reka_provider_verified: bool = False
    cors_origins: tuple[str, ...] = ("http://localhost:5173",)
    runtime_dir: Path = Path("data/runtime")
    near_live_capture_seconds: int = 20
    public_hls_demo_enabled: bool = False
    synthetic_demo_forecasts: bool = False
    reka_index_poll_seconds: float = 3.0
    reka_index_max_polls: int = 20
    oidc_issuer: str = ""
    oidc_audience: str = ""
    oidc_jwks_url: str = ""
    oidc_algorithms: tuple[str, ...] = ("RS256",)
    oidc_memberships_claim: str = "tenant_memberships"
    trusted_hosts: tuple[str, ...] = ("localhost", "127.0.0.1", "testserver")
    max_request_bytes: int = 512 * 1024 * 1024
    api_rate_limit_requests: int = 120
    api_rate_limit_window_seconds: int = 60

    @classmethod
    def from_environment(cls) -> Settings:
        try:
            from dotenv import load_dotenv
        except ImportError:
            pass
        else:
            load_dotenv()
        origins = tuple(
            item.strip()
            for item in os.environ.get("CORS_ORIGINS", "http://localhost:5173").split(
                ","
            )
            if item.strip()
        )
        trusted_hosts = tuple(
            item.strip()
            for item in os.environ.get(
                "TRUSTED_HOSTS", "localhost,127.0.0.1,testserver"
            ).split(",")
            if item.strip()
        )
        algorithms = tuple(
            item.strip()
            for item in os.environ.get("OIDC_ALGORITHMS", "RS256").split(",")
            if item.strip()
        )
        settings = cls(
            app_environment=os.environ.get("APP_ENVIRONMENT", "development")
            .strip()
            .lower(),
            reka_api_key=_secret_value("REKA_API_KEY"),
            reka_chat_base_url=os.environ.get(
                "REKA_CHAT_BASE_URL",
                os.environ.get("REKA_BASE_URL", "https://api.reka.ai/v1"),
            ).strip(),
            reka_vision_base_url=os.environ.get(
                "REKA_VISION_BASE_URL", "https://vision-agent.api.reka.ai"
            ).strip(),
            reka_model=os.environ.get("REKA_MODEL", "reka-flash-3").strip(),
            reka_video_model=os.environ.get(
                "REKA_VIDEO_MODEL", "reka-edge-2603"
            ).strip(),
            reka_prompt_version=os.environ.get(
                "REKA_VIDEO_PROMPT_VERSION",
                os.environ.get("REKA_PROMPT_VERSION", "1.2.0"),
            ).strip(),
            reka_timeout_seconds=float(os.environ.get("REKA_TIMEOUT_SECONDS", "20")),
            reka_provider_verified=_boolean_value("REKA_PROVIDER_VERIFIED"),
            cors_origins=origins,
            runtime_dir=Path(os.environ.get("RUNTIME_DIR", "data/runtime")),
            near_live_capture_seconds=int(
                os.environ.get("NEAR_LIVE_CAPTURE_SECONDS", "20")
            ),
            public_hls_demo_enabled=_boolean_value("PUBLIC_HLS_DEMO_ENABLED"),
            synthetic_demo_forecasts=_boolean_value("SYNTHETIC_DEMO_FORECASTS"),
            reka_index_poll_seconds=float(
                os.environ.get("REKA_INDEX_POLL_SECONDS", "3")
            ),
            reka_index_max_polls=int(os.environ.get("REKA_INDEX_MAX_POLLS", "20")),
            oidc_issuer=os.environ.get("OIDC_ISSUER", "").strip(),
            oidc_audience=os.environ.get("OIDC_AUDIENCE", "").strip(),
            oidc_jwks_url=os.environ.get("OIDC_JWKS_URL", "").strip(),
            oidc_algorithms=algorithms,
            oidc_memberships_claim=os.environ.get(
                "OIDC_MEMBERSHIPS_CLAIM", "tenant_memberships"
            ).strip(),
            trusted_hosts=trusted_hosts,
            max_request_bytes=int(
                os.environ.get("MAX_REQUEST_BYTES", str(512 * 1024 * 1024))
            ),
            api_rate_limit_requests=int(
                os.environ.get("API_RATE_LIMIT_REQUESTS", "120")
            ),
            api_rate_limit_window_seconds=int(
                os.environ.get("API_RATE_LIMIT_WINDOW_SECONDS", "60")
            ),
        )
        if not 1 <= settings.reka_timeout_seconds <= 120:
            raise ValueError("REKA_TIMEOUT_SECONDS must be between 1 and 120")
        if not settings.reka_chat_base_url.startswith("https://"):
            raise ValueError("REKA_CHAT_BASE_URL must use HTTPS")
        if not settings.reka_vision_base_url.startswith("https://"):
            raise ValueError("REKA_VISION_BASE_URL must use HTTPS")
        if not settings.cors_origins:
            raise ValueError("At least one CORS origin is required")
        if settings.app_environment not in {"development", "test", "production"}:
            raise ValueError("APP_ENVIRONMENT must be development, test, or production")
        if not 5 <= settings.near_live_capture_seconds <= 60:
            raise ValueError("NEAR_LIVE_CAPTURE_SECONDS must be between 5 and 60")
        if not 0 <= settings.reka_index_poll_seconds <= 30:
            raise ValueError("REKA_INDEX_POLL_SECONDS must be between 0 and 30")
        if not 1 <= settings.reka_index_max_polls <= 100:
            raise ValueError("REKA_INDEX_MAX_POLLS must be between 1 and 100")
        if not settings.trusted_hosts or "*" in settings.trusted_hosts:
            raise ValueError("TRUSTED_HOSTS must be explicit and cannot contain '*'")
        if not 1024 <= settings.max_request_bytes <= 10 * 1024 * 1024 * 1024:
            raise ValueError("MAX_REQUEST_BYTES must be between 1 KiB and 10 GiB")
        if not 1 <= settings.api_rate_limit_requests <= 100000:
            raise ValueError("API_RATE_LIMIT_REQUESTS is outside the supported range")
        if not 1 <= settings.api_rate_limit_window_seconds <= 3600:
            raise ValueError(
                "API_RATE_LIMIT_WINDOW_SECONDS is outside the supported range"
            )
        allowed_algorithms = {"RS256", "RS384", "RS512", "ES256", "ES384", "ES512"}
        if (
            not settings.oidc_algorithms
            or not set(settings.oidc_algorithms) <= allowed_algorithms
        ):
            raise ValueError(
                "OIDC_ALGORITHMS contains an unsupported asymmetric algorithm"
            )
        if settings.app_environment == "production":
            required = {
                "OIDC_ISSUER": settings.oidc_issuer,
                "OIDC_AUDIENCE": settings.oidc_audience,
                "OIDC_JWKS_URL": settings.oidc_jwks_url,
            }
            missing = sorted(name for name, value in required.items() if not value)
            if missing:
                raise ValueError(
                    f"Missing production OIDC settings: {', '.join(missing)}"
                )
            if not settings.oidc_issuer.startswith("https://"):
                raise ValueError("OIDC_ISSUER must use HTTPS in production")
            if not settings.oidc_jwks_url.startswith("https://"):
                raise ValueError("OIDC_JWKS_URL must use HTTPS in production")
        return settings

    @property
    def reka_configured(self) -> bool:
        return bool(self.reka_api_key)
