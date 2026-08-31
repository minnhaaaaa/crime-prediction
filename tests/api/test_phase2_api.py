"""Phase 2 authentication, tenancy, forecast, and mutation behavior."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.api import reka
from src.api.app import _durable_run, create_app
from src.api.settings import Settings
from src.api.tenancy import DEMO_TENANT_ONE
from src.data.store import IngestionStore
from src.data.video import (
    DatabaseJobBroker,
    DictLocationResolver,
    FakeRekaVisionProvider,
    VideoPipelineService,
    VideoStore,
)
from src.data.video.errors import VideoPipelineError
from src.models.contracts import validate_contract

ONE = {"Authorization": "Bearer demo-token-one"}
TWO = {"Authorization": "Bearer demo-token-two"}
REVIEWER = {"Authorization": "Bearer demo-reviewer-one"}
VIEWER = {"Authorization": "Bearer demo-viewer-one"}


@pytest.fixture()
def app():
    return create_app(provider=reka.FakeRekaProvider(), settings=Settings())


@pytest.fixture()
def client(app) -> TestClient:
    return TestClient(app)


def _future_window() -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=6)).replace(
        minute=0, second=0, microsecond=0
    ).isoformat().replace("+00:00", "Z")


def test_every_auth_failure_is_a_typed_error(client):
    for headers, code in (
        ({}, "missing_token"),
        ({"Authorization": "Bearer invalid"}, "invalid_token"),
        ({"Authorization": "Bearer expired-demo-token"}, "expired_token"),
    ):
        response = client.get("/v1/metadata", headers=headers)
        assert response.status_code == 401
        assert response.json()["code"] == code
        validate_contract("api-error", response.json())
        assert response.headers["X-Request-ID"] == response.json()["request_id"]


def test_active_tenant_switch_requires_membership_and_is_idempotent(client):
    headers = {**ONE, "Idempotency-Key": "tenant-switch-0001"}
    path = "/v1/me/active-tenant/00000000-0000-4000-8000-000000000002"
    first = client.put(path, headers=headers)
    second = client.put(path, headers=headers)
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert first.json()["role"] == "viewer"

    denied = client.put(
        "/v1/me/active-tenant/00000000-0000-4000-8000-000000000001",
        headers={**TWO, "Idempotency-Key": "tenant-switch-0002"},
    )
    assert denied.status_code == 403
    assert denied.json()["code"] == "tenant_forbidden"


def test_demo_session_start_clears_only_pending_queue_and_is_idempotent(client):
    before = client.get("/v1/candidate-detections", headers=REVIEWER)
    assert before.status_code == 200
    assert len(before.json()["items"]) == 1
    denied = client.post(
        "/v1/demo/session/start",
        json={},
        headers={**REVIEWER, "Idempotency-Key": "demo-session-reviewer"},
    )
    assert denied.status_code == 403

    headers = {**ONE, "Idempotency-Key": "demo-session-admin-0001"}
    first = client.post("/v1/demo/session/start", json={}, headers=headers)
    replay = client.post("/v1/demo/session/start", json={}, headers=headers)
    assert first.status_code == replay.status_code == 200
    assert first.json() == replay.json() == {
        "status": "started",
        "deleted_pending_candidates": 1,
    }
    after = client.get("/v1/candidate-detections", headers=REVIEWER)
    assert after.status_code == 200
    assert after.json()["items"] == []


def test_source_mutations_require_idempotency_and_reject_client_tenant(client):
    body = {
        "name": "Entrance recording",
        "timezone": "Asia/Kolkata",
        "registered_location_id": "30000000-0000-4000-8000-000000000001",
        "retention_policy_days": 7,
    }
    missing = client.post("/v1/sources/recorded-video", json=body, headers=ONE)
    assert missing.status_code == 400
    assert missing.json()["code"] == "idempotency_key_required"

    headers = {**ONE, "Idempotency-Key": "recorded-source-0001"}
    first = client.post("/v1/sources/recorded-video", json=body, headers=headers)
    second = client.post("/v1/sources/recorded-video", json=body, headers=headers)
    assert first.status_code == second.status_code == 201
    assert first.json() == second.json()
    assert "location_ref" not in first.json()

    smuggled = client.post(
        "/v1/sources/recorded-video",
        json={**body, "tenant_id": "00000000-0000-4000-8000-000000000002"},
        headers={**ONE, "Idempotency-Key": "recorded-source-0002"},
    )
    assert smuggled.status_code == 422
    assert smuggled.json()["code"] == "request_validation_failed"


def test_live_source_accepts_only_secret_references_and_hides_connection(client):
    body = {
        "name": "North gate camera",
        "timezone": "UTC",
        "registered_location_id": "30000000-0000-4000-8000-000000000001",
        "retention_policy_days": 7,
        "connection_secret_id": "40000000-0000-4000-8000-000000000001",
        "transport": "rtsp",
    }
    response = client.post(
        "/v1/sources/live-camera",
        json=body,
        headers={**ONE, "Idempotency-Key": "live-source-0001"},
    )
    assert response.status_code == 201, response.text
    assert response.json()["mode"] == "live_camera"
    assert "connection" not in response.json()
    assert "secret" not in response.text

    raw_url = client.post(
        "/v1/sources/live-camera",
        json={**body, "url": "rtsp://camera.invalid/private"},
        headers={**ONE, "Idempotency-Key": "live-source-0002"},
    )
    assert raw_url.status_code == 422
    assert raw_url.json()["code"] == "request_validation_failed"


def test_source_map_location_returns_h3_area_without_raw_coordinates(client):
    response = client.get(
        "/v1/sources/20000000-0000-4000-8000-000000000001/map-location",
        headers=ONE,
    )
    assert response.status_code == 200
    assert response.json()["cell_id"] == "8860145b49fffff"
    assert response.json()["precision"] == "h3_area"
    assert "latitude" not in response.text
    assert "longitude" not in response.text


def test_forecast_endpoint_is_bounded_schema_valid_and_tenant_scoped(client):
    window = _future_window()
    response = client.get(
        f"/v1/forecasts?window_start={window}&category=property&page_size=5", headers=ONE
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["items"]) == 5
    assert body["total"] > 5
    for item in body["items"]:
        validate_contract("forecast", item)
        assert item["tenant_id"].endswith("0001")
        if item["suppression"]["suppressed"]:
            assert item["expected_count"]["value"] is None

    forecast_id = body["items"][0]["forecast_id"]
    assert client.get(f"/v1/forecasts/{forecast_id}", headers=ONE).status_code == 200
    assert client.get(f"/v1/forecasts/{forecast_id}", headers=TWO).status_code == 404

    too_large = client.get(
        f"/v1/forecasts?window_start={window}&category=property&page_size=101", headers=ONE
    )
    assert too_large.status_code == 422
    invalid_bbox = client.get(
        f"/v1/forecasts?window_start={window}&category=property&bbox=20,10,0,30",
        headers=ONE,
    )
    assert invalid_bbox.status_code == 422
    assert invalid_bbox.json()["code"] == "invalid_bbox"


def test_forecast_uses_injected_measured_coverage_not_cell_seed():
    measured_calls: list[tuple[str, str]] = []

    def measured(tenant_id: str, before: str) -> float:
        measured_calls.append((tenant_id, before))
        return 0.82

    client = TestClient(
        create_app(
            provider=reka.FakeRekaProvider(),
            settings=Settings(),
            coverage_provider=measured,
        )
    )
    window = _future_window()
    response = client.get(
        f"/v1/forecasts?window_start={window}&category=property&page_size=5", headers=ONE
    )
    assert response.status_code == 200
    assert {item["coverage_ratio"] for item in response.json()["items"]} == {0.82}
    assert len(measured_calls) == 1


def test_review_role_matrix_and_immutable_idempotent_decision(client):
    assert client.get("/v1/candidate-detections", headers=VIEWER).status_code == 403
    listing = client.get("/v1/candidate-detections", headers=REVIEWER)
    assert listing.status_code == 200
    detection_id = listing.json()["items"][0]["detection_id"]
    assert "evidence_ref" not in listing.text

    body = {"decision": "confirmed", "confirmed_category": "public_order"}
    headers = {**REVIEWER, "Idempotency-Key": "candidate-review-0001"}
    first = client.post(
        f"/v1/candidate-detections/{detection_id}/review", json=body, headers=headers
    )
    replay = client.post(
        f"/v1/candidate-detections/{detection_id}/review", json=body, headers=headers
    )
    assert first.status_code == replay.status_code == 201
    assert first.json() == replay.json()

    overwrite = client.post(
        f"/v1/candidate-detections/{detection_id}/review",
        json={"decision": "rejected", "rejection_reason": "false_positive"},
        headers={**REVIEWER, "Idempotency-Key": "candidate-review-0002"},
    )
    assert overwrite.status_code == 409
    assert overwrite.json()["code"] == "review_final"


def test_secret_configuration_never_appears_in_repr_or_openapi():
    secret = "test-secret-that-must-not-leak"
    settings = Settings(reka_api_key=secret)
    assert secret not in repr(settings)
    app = create_app(provider=reka.FakeRekaProvider(), settings=settings)
    serialized = json.dumps(app.openapi())
    assert "/v1/video-assets/uploads" in app.openapi()["paths"]
    assert "/v1/ingestion/runs/{run_id}" in app.openapi()["paths"]
    assert "/v1/ingestion/runs/{run_id}/reanalyze" in app.openapi()["paths"]
    assert "/v1/demo/simulated-cctv/captures" in app.openapi()["paths"]
    assert "/v1/candidate-detections/{detection_id}/evidence" in app.openapi()["paths"]
    assert secret not in serialized
    assert "REKA_API_KEY" not in serialized
    assert "secret_ref" not in serialized


def test_public_hls_demo_flag_is_explicit(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("APP_ENVIRONMENT", "test")
    monkeypatch.setenv("PUBLIC_HLS_DEMO_ENABLED", "true")
    assert Settings.from_environment().public_hls_demo_enabled is True


def test_durable_run_reports_the_whole_worker_chain_and_candidate_count():
    root = {
        "job_id": "70000000-0000-4000-8000-000000000001",
        "asset_id": "71000000-0000-4000-8000-000000000001",
        "operation": "upload",
        "state": "completed",
        "created_at": "2026-08-30T00:00:00Z",
        "updated_at": "2026-08-30T00:01:00Z",
    }

    class Store:
        def jobs_for_asset(self, tenant_id, asset_id):
            assert tenant_id == "tenant-one"
            assert asset_id == root["asset_id"]
            return [
                root,
                {
                    **root,
                    "job_id": "70000000-0000-4000-8000-000000000002",
                    "operation": "index",
                    "updated_at": "2026-08-30T00:02:00Z",
                },
                {
                    **root,
                    "job_id": "70000000-0000-4000-8000-000000000003",
                    "operation": "analyze",
                    "updated_at": "2026-08-30T00:03:00Z",
                },
            ]

        def list_candidates(self, tenant_id):
            assert tenant_id == "tenant-one"
            return [
                {"asset_id": root["asset_id"]},
                {"asset_id": "another-tenant-scoped-asset"},
            ]

    run = _durable_run(Store(), "tenant-one", root, "reka_vision")
    assert run["state"] == "completed"
    assert run["stage"] == "awaiting_human_review"
    assert run["candidate_count"] == 1
    assert run["run_id"] == root["job_id"]


def test_readiness_does_not_treat_an_unverified_reka_key_as_ready():
    settings = Settings(reka_api_key="configured-but-unverified")
    with TestClient(
        create_app(provider=reka.FakeRekaProvider(), settings=settings)
    ) as client:
        response = client.get("/ready")

    assert response.status_code == 200
    assert response.json()["status"] == "degraded"
    assert response.json()["reka_chat"] == "configured_unverified"
    assert response.json()["reka_vision"] == "configured_unverified"


def test_synthetic_demo_mode_is_explicitly_labelled_and_unsuppressed():
    client = TestClient(
        create_app(
            provider=reka.FakeRekaProvider(),
            settings=Settings(synthetic_demo_forecasts=True),
        )
    )
    assert client.get("/ready").json()["forecast_data"] == "synthetic_demo"
    assert client.get("/v1/metadata", headers=ONE).json()["forecast_data"] == "synthetic_demo"
    window = _future_window()
    response = client.get(
        f"/v1/forecasts?window_start={window}&category=property&page_size=5",
        headers=ONE,
    )
    assert response.status_code == 200
    assert {item["coverage_ratio"] for item in response.json()["items"]} == {1.0}
    assert any(not item["suppression"]["suppressed"] for item in response.json()["items"])


def test_production_rejects_the_development_authentication_provider():
    with pytest.raises(ValueError, match="production AuthenticationProvider"):
        create_app(
            provider=reka.FakeRekaProvider(),
            settings=Settings(app_environment="production"),
        )


def test_integrated_demo_refresh_is_tenant_derived_and_admin_only():
    calls: list[str] = []

    def refresh(tenant_id: str, now: datetime) -> dict:
        calls.append(tenant_id)
        return {
            "tenant_id": tenant_id,
            "window_start": "2099-01-01T00:00:00Z",
            "forecast_count": 4,
            "feature_snapshot_version": "future-demo",
            "coverage_ratio": 1.0,
        }

    client = TestClient(
        create_app(
            provider=reka.FakeRekaProvider(),
            settings=Settings(),
            forecast_refresher=refresh,
        )
    )
    response = client.post(
        "/v1/demo/forecasts/refresh",
        json={},
        headers={**ONE, "Idempotency-Key": "demo-refresh-0001"},
    )
    assert response.status_code == 201
    assert response.json()["tenant_id"].endswith("0001")
    assert calls == [response.json()["tenant_id"]]

    denied = client.post(
        "/v1/demo/forecasts/refresh",
        json={},
        headers={**VIEWER, "Idempotency-Key": "demo-refresh-0002"},
    )
    assert denied.status_code == 403


def test_ingestion_run_collection_is_tenant_scoped_and_bounded(client):
    response = client.get("/v1/ingestion/runs?limit=5", headers=ONE)
    assert response.status_code == 200
    assert response.json() == {"items": []}
    assert client.get("/v1/ingestion/runs?limit=101", headers=ONE).status_code == 422


def test_controlled_reanalysis_is_admin_tenant_scoped_and_idempotent(
    tmp_path: Path,
) -> None:
    source_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    ingestion = IngestionStore(tmp_path / "state.sqlite3")
    store = VideoStore(ingestion)
    vision = FakeRekaVisionProvider(
        proposals=[{"offset_seconds": 1, "category": "property"}]
    )

    class Inspector:
        def duration_seconds(self, path: Path) -> float:
            return 10.0

    service = VideoPipelineService(
        store,
        vision,
        DictLocationResolver(
            {
                (DEMO_TENANT_ONE, f"secret://locations/{source_id}"): {
                    "latitude": 12.9,
                    "longitude": 77.5,
                }
            }
        ),
        media_root=tmp_path / "media",
        media_inspector=Inspector(),
    )
    service.register_recorded_source(
        {
            "schema_version": "1.0.0",
            "tenant_id": DEMO_TENANT_ONE,
            "source_id": source_id,
            "name": "Re-analysis fixture",
            "mode": "recorded_video",
            "status": "active",
            "timezone": "UTC",
            "location_ref": f"secret://locations/{source_id}",
            "connection": {"transport": "uploaded_asset"},
            "retention_policy_days": 7,
            "created_at": "2026-08-30T00:00:00Z",
        },
        authenticated_tenant_id=DEMO_TENANT_ONE,
    )
    clip = tmp_path / "media" / "clip.mp4"
    clip.parent.mkdir(parents=True, exist_ok=True)
    clip.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"safe-media" * 8)
    asset = service.accept_upload(
        authenticated_tenant_id=DEMO_TENANT_ONE,
        source_id=source_id,
        path=clip,
        content_type="video/mp4",
        captured_start="2026-08-30T00:00:00Z",
        captured_end="2026-08-30T00:00:10Z",
        duration_seconds=10,
        consent_confirmed=True,
    )
    with pytest.raises(VideoPipelineError) as caught:
        service.process_asset(DEMO_TENANT_ONE, asset["asset_id"])
    assert caught.value.code == "reka_output_missing_fields"
    failed_job = next(
        job
        for job in store.list_jobs(DEMO_TENANT_ONE)
        if job["operation"] == "analyze"
    )
    broker = DatabaseJobBroker(store)
    api_client = TestClient(
        create_app(
            provider=reka.FakeRekaProvider(),
            settings=Settings(),
            video_service=service,
            video_broker=broker,
        )
    )
    path = f"/v1/ingestion/runs/{failed_job['job_id']}/reanalyze"
    headers = {**ONE, "Idempotency-Key": "reanalyze-api-0001"}
    missing_key = api_client.post(path, headers=ONE)
    assert missing_key.status_code == 400
    assert missing_key.json()["code"] == "idempotency_key_required"
    first = api_client.post(path, headers=headers)
    replay = api_client.post(path, headers=headers)
    assert first.status_code == replay.status_code == 202
    assert first.json() == replay.json()
    assert first.json()["run_id"] != failed_job["job_id"]
    assert store.get_job(DEMO_TENANT_ONE, failed_job["job_id"])["state"] == "failed"
    original = api_client.get(
        f"/v1/ingestion/runs/{failed_job['job_id']}", headers=ONE
    )
    listing = api_client.get("/v1/ingestion/runs?limit=50", headers=ONE)
    assert original.status_code == listing.status_code == 200
    assert "safe_diagnostics" not in original.text
    assert "safe_diagnostics" not in listing.text

    viewer = api_client.post(
        path,
        headers={**VIEWER, "Idempotency-Key": "reanalyze-api-viewer"},
    )
    assert viewer.status_code == 403
    other_tenant = api_client.post(
        path,
        headers={
            "Authorization": "Bearer demo-admin-two",
            "Idempotency-Key": "reanalyze-api-other",
        },
    )
    assert other_tenant.status_code == 404
    assert "safe_diagnostics" not in first.text


def test_live_cctv_metadata_is_authenticated_fixed_and_secret_free(client):
    denied = client.get("/v1/demo/live-cctv")
    assert denied.status_code == 401

    response = client.get("/v1/demo/live-cctv", headers=ONE)
    assert response.status_code == 200
    body = response.json()
    assert body["source_key"] == "louisiana-dot-i20"
    assert body["playback_url"].startswith("https://ITSStreamingBR2.dotd.la.gov/")
    assert "secret" not in response.text.lower()
    assert "reka" not in body.get("playback_url", "").lower()


def test_near_live_capture_rejects_every_non_allowlisted_source_key(client):
    response = client.post(
        "/v1/demo/near-live-cctv/captures",
        json={
            "source_key": "https://attacker.example/live.m3u8",
            "duration_seconds": 10,
        },
        headers={**ONE, "Idempotency-Key": "non-allowlisted-hls-source"},
    )
    assert response.status_code == 422


def test_candidate_evidence_requires_reviewer_before_lookup(client):
    response = client.get(
        "/v1/candidate-detections/00000000-0000-4000-8000-000000000000/evidence",
        headers=VIEWER,
    )
    assert response.status_code == 403
    assert response.json()["code"] == "role_forbidden"
