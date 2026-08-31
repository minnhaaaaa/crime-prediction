from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event

import pytest

import src.data.video.runtime as video_runtime
from src.data.store import IngestionStore
from src.data.video import (
    DatabaseJobBroker,
    DictLocationResolver,
    FakeRekaVisionProvider,
    InMemoryCoverageTelemetry,
    JobMessage,
    S3MediaStorage,
    SqsJobBroker,
    VideoJobWorker,
    VideoPipelineService,
    VideoStore,
)
from src.data.video.capture import LiveCaptureWorker, resolve_camera_connection
from src.data.video.coverage import CoverageObservation, persist_measured_snapshot
from src.data.video.errors import VideoPipelineError
from src.data.video.runtime import PlatformSettings

TENANT = "11111111-1111-4111-8111-111111111111"
SOURCE = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def _time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _source() -> dict:
    return {
        "schema_version": "1.0.0",
        "tenant_id": TENANT,
        "source_id": SOURCE,
        "name": "Durable worker test",
        "mode": "recorded_video",
        "status": "active",
        "timezone": "UTC",
        "location_ref": f"secret://locations/{SOURCE}",
        "connection": {"transport": "uploaded_asset"},
        "retention_policy_days": 30,
        "created_at": "2026-01-01T00:00:00Z",
    }


def _setup(tmp_path: Path, *, fail_upload: bool = False):
    root = tmp_path / "restricted"
    root.mkdir()
    ingestion = IngestionStore(tmp_path / "state.sqlite3")
    store = VideoStore(ingestion)
    provider = FakeRekaVisionProvider(
        proposals=[{
            "offset_seconds": 1,
            "category": "property",
            "event_type": "property_damage",
            "description": "Property is visibly damaged.",
            "confidence": 0.8,
        }],
        fail_operations={"upload"} if fail_upload else set(),
    )

    class Inspector:
        def duration_seconds(self, path: Path) -> float:
            return 30.0

    service = VideoPipelineService(
        store,
        provider,
        DictLocationResolver(
            {
                (TENANT, f"secret://locations/{SOURCE}"): {
                    "latitude": 12.9,
                    "longitude": 77.5,
                }
            }
        ),
        media_root=root,
        media_inspector=Inspector(),
    )
    service.register_recorded_source(_source(), authenticated_tenant_id=TENANT)
    path = root / "clip.mp4"
    path.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"safe-test-media" * 8)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    asset = service.accept_upload(
        authenticated_tenant_id=TENANT,
        source_id=SOURCE,
        path=path,
        content_type="video/mp4",
        captured_start=_time(start),
        captured_end=_time(start + timedelta(seconds=30)),
        duration_seconds=30,
        consent_confirmed=True,
    )
    return store, provider, service, asset


def test_separate_workers_resume_persisted_chain_after_restart(tmp_path: Path) -> None:
    store, provider, service, asset = _setup(tmp_path)
    upload = store.enqueue(TENANT, asset["asset_id"], "upload")
    broker = DatabaseJobBroker(store)
    broker.publish(JobMessage(TENANT, upload["job_id"], "upload"))

    upload_worker = VideoJobWorker(
        store=store,
        broker=broker,
        service=service,
        operations=("upload",),
        worker_id="upload-1",
    )
    assert upload_worker.poll_once()[0].state == "completed"
    assert len([call for call in provider.calls if call[0] == "upload"]) == 1
    index_job = next(
        job for job in store.list_jobs(TENANT) if job["operation"] == "index"
    )
    assert index_job["max_attempts"] == 20

    # Re-open both stores to prove queued state is not tied to worker memory.
    restarted_store = VideoStore(IngestionStore(tmp_path / "state.sqlite3"))
    restarted_broker = DatabaseJobBroker(restarted_store)
    restarted_service = VideoPipelineService(
        restarted_store,
        provider,
        service.location_resolver,
        media_root=tmp_path / "restricted",
        media_inspector=service.media_inspector,
    )
    index_worker = VideoJobWorker(
        store=restarted_store,
        broker=restarted_broker,
        service=restarted_service,
        operations=("index",),
        worker_id="index-1",
    )
    telemetry = InMemoryCoverageTelemetry()
    analyze_worker = VideoJobWorker(
        store=restarted_store,
        broker=restarted_broker,
        service=restarted_service,
        operations=("analyze",),
        worker_id="analyze-1",
        telemetry=telemetry,
    )
    assert index_worker.poll_once()[0].state == "completed"
    assert analyze_worker.poll_once()[0].state == "completed"
    assert len(restarted_store.list_candidates(TENANT)) == 1
    assert restarted_store.job_metrics(TENANT) == {"completed": 3}
    assert telemetry.observations[0].detector_available is True


def test_retry_uses_persisted_exponential_backoff(tmp_path: Path) -> None:
    store, _, service, asset = _setup(tmp_path, fail_upload=True)
    job = store.enqueue(TENANT, asset["asset_id"], "upload")
    worker = VideoJobWorker(
        store=store,
        broker=DatabaseJobBroker(store),
        service=service,
        operations=("upload",),
        worker_id="upload-1",
    )
    result = worker.poll_once()[0]
    persisted = store.get_job(TENANT, job["job_id"])
    assert result.state == "retry"
    assert persisted["state"] == "retry"
    assert persisted["last_error_code"] == "reka_unavailable"
    assert worker.poll_once() == []


def test_worker_renews_persisted_and_broker_leases_during_provider_call(
    tmp_path: Path,
) -> None:
    store, provider, service, asset = _setup(tmp_path)
    job = store.enqueue(TENANT, asset["asset_id"], "upload")
    broker = DatabaseJobBroker(store)
    provider_started = Event()
    release_provider = Event()
    renewed = Event()
    original_upload = provider.upload
    heartbeat_count = 0

    class RecordingStore:
        def __getattr__(self, name: str):
            return getattr(store, name)

        def heartbeat(self, *args, **kwargs) -> None:
            nonlocal heartbeat_count
            store.heartbeat(*args, **kwargs)
            heartbeat_count += 1
            if heartbeat_count >= 2:
                renewed.set()

    def blocking_upload(*args, **kwargs):
        provider_started.set()
        assert release_provider.wait(timeout=5)
        return original_upload(*args, **kwargs)

    provider.upload = blocking_upload
    worker = VideoJobWorker(
        store=RecordingStore(),
        broker=broker,
        service=service,
        operations=("upload",),
        worker_id="lease-renewal-worker",
        lease_seconds=1,
    )

    with ThreadPoolExecutor(max_workers=1) as executor:
        result = executor.submit(worker.poll_once)
        assert provider_started.wait(timeout=5)
        assert renewed.wait(timeout=5)
        release_provider.set()
        assert result.result(timeout=5)[0].state == "completed"

    assert store.get_job(TENANT, job["job_id"])["state"] == "completed"
    assert heartbeat_count >= 2


def test_only_one_analysis_job_can_be_active_per_asset(tmp_path: Path) -> None:
    store, _, _, asset = _setup(tmp_path)
    first = store.enqueue(
        TENANT, asset["asset_id"], "analyze", idempotency_key="analysis-attempt-one"
    )
    assert first["state"] == "queued"
    with pytest.raises(VideoPipelineError) as caught:
        store.enqueue(
            TENANT,
            asset["asset_id"],
            "analyze",
            idempotency_key="analysis-attempt-two",
        )
    assert caught.value.code == "job_active_conflict"


def test_exhausted_pending_index_fails_instead_of_sticking_in_retry(
    tmp_path: Path,
) -> None:
    store, provider, service, asset = _setup(tmp_path)
    provider.status = "indexing"
    store.put_mapping(TENANT, SOURCE, asset["asset_id"], "fake-indexing", "pending")
    job = store.enqueue(
        TENANT,
        asset["asset_id"],
        "index",
        max_attempts=1,
        idempotency_key="bounded-index-timeout",
    )
    telemetry = InMemoryCoverageTelemetry()
    worker = VideoJobWorker(
        store=store,
        broker=DatabaseJobBroker(store),
        service=service,
        operations=("index",),
        telemetry=telemetry,
        index_max_attempts=1,
        index_poll_seconds=0,
    )
    result = worker.poll_once()[0]
    assert result.state == "failed"
    assert result.error_code == "reka_index_timeout"
    persisted = store.get_job(TENANT, job["job_id"])
    assert persisted["state"] == "failed"
    assert persisted["last_error_code"] == "reka_index_timeout"
    assert store.get_asset(TENANT, asset["asset_id"])["status"] == "failed"
    assert store.list_coverage(TENANT)[0]["degraded_reason_codes"] == [
        "reka_index_timeout"
    ]


def test_exhausted_legacy_index_adopts_the_configured_bound(tmp_path: Path) -> None:
    store, provider, service, asset = _setup(tmp_path)
    store.put_mapping(TENANT, SOURCE, asset["asset_id"], "fake-indexed", "pending")
    job = store.enqueue(
        TENANT,
        asset["asset_id"],
        "index",
        max_attempts=1,
        idempotency_key="legacy-index-bound",
    )
    store.transition_job(TENANT, job["job_id"], "running")
    store.transition_job(TENANT, job["job_id"], "retry", "reka_index_pending")
    provider.status = "indexed"
    worker = VideoJobWorker(
        store=store,
        broker=DatabaseJobBroker(store),
        service=service,
        operations=("index",),
        index_max_attempts=3,
        index_poll_seconds=0,
    )
    result = worker.poll_once()[0]
    assert result.state == "completed"
    persisted = store.get_job(TENANT, job["job_id"])
    assert persisted["attempts"] == 2
    assert persisted["max_attempts"] == 3


def test_empty_reka_result_completes_with_zero_candidates(tmp_path: Path) -> None:
    store, provider, service, asset = _setup(tmp_path)
    provider.proposals = []
    broker = DatabaseJobBroker(store)
    upload = store.enqueue(TENANT, asset["asset_id"], "upload")
    broker.publish(JobMessage(TENANT, upload["job_id"], "upload"))
    upload_worker = VideoJobWorker(
        store=store, broker=broker, service=service, operations=("upload",)
    )
    index_worker = VideoJobWorker(
        store=store, broker=broker, service=service, operations=("index",)
    )
    analyze_worker = VideoJobWorker(
        store=store, broker=broker, service=service, operations=("analyze",)
    )
    assert upload_worker.poll_once()[0].state == "completed"
    assert index_worker.poll_once()[0].state == "completed"
    result = analyze_worker.poll_once()[0]
    assert result.state == "completed"
    assert store.list_candidates(TENANT) == []
    assert store.get_asset(TENANT, asset["asset_id"])["status"] == "processed"


def test_invalid_analysis_is_fail_closed_and_reanalysis_is_fresh(
    tmp_path: Path,
) -> None:
    store, provider, service, asset = _setup(tmp_path)
    provider.proposals = [{
        "offset_seconds": 1,
        "category": "property",
        "event_type": "property_damage",
        "description": "Property is visibly damaged.",
    }]
    broker = DatabaseJobBroker(store)
    upload = store.enqueue(TENANT, asset["asset_id"], "upload")
    broker.publish(JobMessage(TENANT, upload["job_id"], "upload"))
    upload_worker = VideoJobWorker(
        store=store, broker=broker, service=service, operations=("upload",)
    )
    index_worker = VideoJobWorker(
        store=store, broker=broker, service=service, operations=("index",)
    )
    telemetry = InMemoryCoverageTelemetry()
    analyze_worker = VideoJobWorker(
        store=store,
        broker=broker,
        service=service,
        operations=("analyze",),
        telemetry=telemetry,
    )
    assert upload_worker.poll_once()[0].state == "completed"
    assert index_worker.poll_once()[0].state == "completed"
    failed_result = analyze_worker.poll_once()[0]
    failed_job = store.get_job(TENANT, failed_result.job_id)
    assert failed_result.state == "failed"
    assert failed_job["last_error_code"] == "reka_output_missing_fields"
    assert failed_job["safe_diagnostics"] == {
        "proposal_index": 0,
        "missing_fields": ["confidence"],
    }
    failed_asset = store.get_asset(TENANT, asset["asset_id"])
    assert failed_asset["status"] == "failed"
    assert failed_asset["failure_code"] == "reka_output_missing_fields"
    assert store.list_candidates(TENANT) == []
    snapshot = store.list_coverage(TENANT)[0]
    assert snapshot["coverage_ratio"] == 0
    assert snapshot["degraded_reason_codes"] == ["detector_output_invalid"]
    assert telemetry.observations[-1].reka_available is True
    with store.ingestion_store.connect() as connection:
        audit = connection.execute(
            """SELECT action,outcome,error_code FROM video_audit_log
               WHERE tenant_id=? AND resource_id=? AND outcome='failure'""",
            (TENANT, asset["asset_id"]),
        ).fetchone()
    assert dict(audit) == {
        "action": "reka.analyze",
        "outcome": "failure",
        "error_code": "reka_output_missing_fields",
    }

    provider.proposals = [
        {
            "offset_seconds": 1,
            "category": "property",
            "event_type": "property_damage",
            "description": "Property is visibly damaged.",
            "confidence": 0.8,
        }
    ]
    fresh_job = service.request_reanalysis(
        TENANT, failed_job["job_id"], idempotency_key="controlled-reanalysis-1"
    )
    assert fresh_job["job_id"] != failed_job["job_id"]
    assert store.get_job(TENANT, failed_job["job_id"])["state"] == "failed"
    assert store.get_asset(TENANT, asset["asset_id"])["status"] == "processing"
    broker.publish(JobMessage(TENANT, fresh_job["job_id"], "analyze"))
    completed = analyze_worker.poll_once()[0]
    assert completed.job_id == fresh_job["job_id"]
    assert completed.state == "completed"
    assert store.get_asset(TENANT, asset["asset_id"])["status"] == "processed"
    assert len(store.list_candidates(TENANT)) == 1
    with pytest.raises(VideoPipelineError) as caught:
        service.request_reanalysis(
            TENANT, failed_job["job_id"], idempotency_key="controlled-reanalysis-2"
        )
    assert caught.value.code == "reanalysis_already_completed"


class FakeS3:
    def __init__(self) -> None:
        self.uploads: list[tuple] = []
        self.downloads: list[tuple] = []
        self.deletes: list[dict] = []
        self.objects: dict[tuple[str, str], tuple[bytes, dict[str, str]]] = {}

    def upload_file(self, *args, **kwargs):
        self.uploads.append((args, kwargs))
        self.objects[(args[1], args[2])] = (
            Path(args[0]).read_bytes(),
            dict(kwargs["ExtraArgs"]["Metadata"]),
        )

    def head_object(self, **kwargs):
        return {"Metadata": self.objects[(kwargs["Bucket"], kwargs["Key"])][1]}

    def download_file(self, bucket, key, target, ExtraArgs=None):
        self.downloads.append((bucket, key, target, ExtraArgs))
        Path(target).write_bytes(self.objects[(bucket, key)][0])

    def delete_object(self, **kwargs):
        self.deletes.append(kwargs)


def test_s3_storage_is_tenant_prefixed_kms_encrypted_and_reference_safe(
    tmp_path: Path,
) -> None:
    client = FakeS3()
    storage = S3MediaStorage(
        bucket="restricted",
        kms_key_id="alias/video",
        region_name="ap-south-1",
        client=client,
    )
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"media")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    ref = storage.store(path, tenant_id=TENANT, asset_id=SOURCE, sha256=digest)
    args, kwargs = client.uploads[0]
    assert args[2].startswith(f"tenants/{TENANT}/video-assets/{SOURCE}/")
    assert kwargs["ExtraArgs"]["ServerSideEncryption"] == "aws:kms"
    assert kwargs["ExtraArgs"]["SSEKMSKeyId"] == "alias/video"
    assert "restricted" not in ref and args[2] not in ref
    with storage.materialize(ref, tenant_id=TENANT, asset_id=SOURCE) as materialized:
        assert materialized.read_bytes() == b"media"
    with (
        pytest.raises(VideoPipelineError),
        storage.materialize(
            ref,
            tenant_id="22222222-2222-4222-8222-222222222222",
            asset_id=SOURCE,
        ),
    ):
        pass


def test_s3_storage_rejects_tampered_objects_and_enforces_bucket_owner(
    tmp_path: Path,
) -> None:
    client = FakeS3()
    storage = S3MediaStorage(
        bucket="restricted",
        kms_key_id="alias/video",
        expected_bucket_owner="123456789012",
        region_name="ap-south-1",
        client=client,
    )
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"trusted-media")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    ref = storage.store(path, tenant_id=TENANT, asset_id=SOURCE, sha256=digest)
    _, kwargs = client.uploads[0]
    assert kwargs["ExtraArgs"]["ExpectedBucketOwner"] == "123456789012"
    key = next(iter(client.objects))
    _, metadata = client.objects[key]
    client.objects[key] = (b"tampered-media", metadata)
    with (
        pytest.raises(VideoPipelineError, match="checksum verification") as caught,
        storage.materialize(ref, tenant_id=TENANT, asset_id=SOURCE),
    ):
        pass
    assert caught.value.code == "media_integrity_mismatch"


def test_camera_connection_hides_credentials_and_rejects_embedded_userinfo() -> None:
    class Secrets:
        def __init__(self) -> None:
            self.values = {
                "secret://endpoint": {"stream_url": "rtsps://camera.example/live"},
                "secret://credentials": {
                    "username": "operator",
                    "password": "never-print-me",
                },
            }

        def resolve_json(self, ref: str) -> dict:
            return self.values[ref]

    source = {
        "connection": {
            "transport": "rtsp",
            "endpoint_ref": "secret://endpoint",
            "credential_ref": "secret://credentials",
        }
    }
    secrets = Secrets()

    def public_dns(_host: str, _port: int) -> set[str]:
        return {"8.8.8.8"}

    connection = resolve_camera_connection(source, secrets, address_resolver=public_dns)
    assert "never-print-me" not in repr(connection)
    secrets.values["secret://endpoint"] = {
        "stream_url": "rtsps://embedded:credential@camera.example/live"
    }
    with pytest.raises(VideoPipelineError) as caught:
        resolve_camera_connection(source, secrets, address_resolver=public_dns)
    assert caught.value.code == "camera_endpoint_invalid"


@pytest.mark.parametrize(
    "stream_url,address",
    [
        ("rtsps://127.0.0.1/live", "127.0.0.1"),
        ("rtsps://camera.example/live", "10.0.0.8"),
        ("rtsps://camera.example/live", "169.254.169.254"),
        ("rtsps://camera.example:8443/live", "8.8.8.8"),
    ],
)
def test_camera_connection_rejects_ssrf_targets(stream_url: str, address: str) -> None:
    class Secrets:
        def resolve_json(self, ref: str) -> dict:
            if ref == "secret://endpoint":
                return {"stream_url": stream_url}
            return {"username": "operator", "password": "safe-password"}

    source = {
        "connection": {
            "transport": "rtsp",
            "endpoint_ref": "secret://endpoint",
            "credential_ref": "secret://credentials",
        }
    }
    with pytest.raises(VideoPipelineError) as caught:
        resolve_camera_connection(
            source,
            Secrets(),
            address_resolver=lambda _host, _port: {address},
        )
    assert caught.value.code == "camera_endpoint_invalid"


def test_platform_settings_repr_never_contains_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = {
        "DATABASE_URL": "postgresql://crime_app:database-secret@db.example/crime",
        "VIDEO_QUEUE_URL": "https://sqs.ap-south-1.amazonaws.com/123/jobs",
        "VIDEO_QUEUE_DLQ_URL": "https://sqs.ap-south-1.amazonaws.com/123/jobs-dlq",
        "VIDEO_MEDIA_BUCKET": "restricted",
        "VIDEO_MEDIA_KMS_KEY_ID": "alias/video",
        "VIDEO_MEDIA_BUCKET_OWNER": "123456789012",
        "VIDEO_MAX_UPLOAD_BYTES": "8388608",
        "LOCATION_SECRET_PREFIX": "crime/production/tenants",
        "AWS_REGION": "ap-south-1",
        "REKA_API_KEY": "reka-secret-value",
        "REKA_PROMPT_VERSION": "1.1.0",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    settings = PlatformSettings.from_environment()
    assert settings.max_upload_bytes == 8 * 1024 * 1024
    assert settings.reka_prompt_version == "1.1.0"
    rendered = repr(settings)
    assert "database-secret" not in rendered
    assert "reka-secret-value" not in rendered


def test_platform_video_prompt_version_alias_is_validated_and_forwarded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = {
        "DATABASE_URL": "postgresql://crime_app:secret@db.example/crime",
        "VIDEO_QUEUE_URL": "https://sqs.ap-south-1.amazonaws.com/123/jobs",
        "VIDEO_QUEUE_DLQ_URL": "https://sqs.ap-south-1.amazonaws.com/123/jobs-dlq",
        "VIDEO_MEDIA_BUCKET": "restricted",
        "VIDEO_MEDIA_KMS_KEY_ID": "alias/video",
        "VIDEO_MEDIA_BUCKET_OWNER": "123456789012",
        "VIDEO_MAX_UPLOAD_BYTES": "8388608",
        "LOCATION_SECRET_PREFIX": "crime/production/tenants",
        "AWS_REGION": "ap-south-1",
        "REKA_API_KEY": "server-only-test-key",
        "REKA_PROMPT_VERSION": "legacy-v1",
        "REKA_VIDEO_PROMPT_VERSION": "structural-hazards-v2",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)

    settings = PlatformSettings.from_environment()
    assert settings.reka_prompt_version == "structural-hazards-v2"

    database = object()
    captured: dict[str, object] = {}
    monkeypatch.setattr(video_runtime, "TenantPostgres", lambda *_: database)
    monkeypatch.setattr(video_runtime, "PostgresIngestionStore", lambda *_: object())
    monkeypatch.setattr(video_runtime, "PostgresVideoStore", lambda *_: object())
    monkeypatch.setattr(video_runtime, "S3MediaStorage", lambda **_: object())
    monkeypatch.setattr(video_runtime, "SqsJobBroker", lambda **_: object())
    monkeypatch.setattr(video_runtime, "RekaVisionProvider", lambda *_, **__: object())
    monkeypatch.setattr(video_runtime, "ClamAVCommandScanner", lambda: object())

    def fake_service(*args, **kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(video_runtime, "VideoPipelineService", fake_service)
    video_runtime.create_platform_runtime(settings, location_resolver=object())
    assert captured["prompt_version"] == "structural-hazards-v2"


@pytest.mark.parametrize("value", ["", "contains spaces", "x" * 65])
def test_platform_settings_reject_invalid_video_prompt_version(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    required = {
        "DATABASE_URL": "postgresql://crime_app:secret@db.example/crime",
        "VIDEO_QUEUE_URL": "https://sqs.ap-south-1.amazonaws.com/123/jobs",
        "VIDEO_QUEUE_DLQ_URL": "https://sqs.ap-south-1.amazonaws.com/123/jobs-dlq",
        "VIDEO_MEDIA_BUCKET": "restricted",
        "VIDEO_MEDIA_KMS_KEY_ID": "alias/video",
        "VIDEO_MEDIA_BUCKET_OWNER": "123456789012",
        "LOCATION_SECRET_PREFIX": "crime/production/tenants",
        "AWS_REGION": "ap-south-1",
        "REKA_API_KEY": "server-only-test-key",
        "VIDEO_MAX_UPLOAD_BYTES": "8388608",
        "REKA_VIDEO_PROMPT_VERSION": value,
    }
    for name, setting in required.items():
        monkeypatch.setenv(name, setting)
    with pytest.raises(ValueError, match="bounded version label"):
        PlatformSettings.from_environment()


@pytest.mark.parametrize("value", ["0", "10485761"])
def test_platform_settings_reject_gateway_unsafe_upload_limit(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    required = {
        "DATABASE_URL": "postgresql://crime_app:secret@db.example/crime",
        "VIDEO_QUEUE_URL": "https://sqs.ap-south-1.amazonaws.com/123/jobs",
        "VIDEO_QUEUE_DLQ_URL": "https://sqs.ap-south-1.amazonaws.com/123/jobs-dlq",
        "VIDEO_MEDIA_BUCKET": "restricted",
        "VIDEO_MEDIA_KMS_KEY_ID": "alias/video",
        "VIDEO_MEDIA_BUCKET_OWNER": "123456789012",
        "LOCATION_SECRET_PREFIX": "crime/production/tenants",
        "AWS_REGION": "ap-south-1",
        "REKA_API_KEY": "server-only-test-key",
        "VIDEO_MAX_UPLOAD_BYTES": value,
    }
    for name, setting in required.items():
        monkeypatch.setenv(name, setting)
    with pytest.raises(ValueError, match="gateway-safe"):
        PlatformSettings.from_environment()


def test_coverage_is_measured_available_seconds_over_expected(tmp_path: Path) -> None:
    _, _, service, _ = _setup(tmp_path)
    telemetry = InMemoryCoverageTelemetry()
    telemetry.record(
        CoverageObservation(
            TENANT,
            SOURCE,
            "2026-01-01T00:00:00Z",
            300,
            connected=True,
            frame_processable=True,
            detector_available=True,
            processing_latency_ms=250,
        )
    )
    telemetry.record(
        CoverageObservation(
            TENANT,
            SOURCE,
            "2026-01-01T00:05:00Z",
            300,
            connected=True,
            frame_processable=True,
            detector_available=False,
            reka_available=False,
        )
    )
    snapshot = persist_measured_snapshot(
        telemetry,
        service,
        tenant_id=TENANT,
        source_id=SOURCE,
        interval_start="2026-01-01T00:00:00Z",
        interval_end="2026-01-01T00:10:00Z",
    )
    assert snapshot["detector_available_seconds"] == 300
    assert snapshot["expected_seconds"] == 600
    assert snapshot["coverage_ratio"] == 0.5
    assert "reka_unavailable" in snapshot["degraded_reason_codes"]


def test_hls_live_capture_creates_bounded_segment_and_durable_upload_job(
    tmp_path: Path,
) -> None:
    store, _, service, _ = _setup(tmp_path)
    live_source = {
        **_source(),
        "source_id": "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
        "mode": "live_camera",
        "connection": {
            "transport": "hls",
            "endpoint_ref": "secret://camera/endpoint",
            "credential_ref": "secret://camera/credentials",
        },
    }
    service.register_live_source(live_source, authenticated_tenant_id=TENANT)

    class Secrets:
        def resolve_json(self, ref: str) -> dict:
            if ref.endswith("endpoint"):
                return {"stream_url": "https://camera.example/approved.m3u8"}
            return {}

    class Segmenter:
        def capture(self, connection, output: Path, *, duration_seconds: int) -> None:
            assert connection.transport == "hls"
            assert duration_seconds == 30
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"segment" * 20)

    telemetry = InMemoryCoverageTelemetry()
    result = LiveCaptureWorker(
        store=store,
        service=service,
        broker=DatabaseJobBroker(store),
        secrets=Secrets(),
        telemetry=telemetry,
        segmenter=Segmenter(),
        spool_root=tmp_path / "restricted",
        segment_seconds=30,
        address_resolver=lambda _host, _port: {"8.8.8.8"},
    ).capture_once(TENANT, live_source["source_id"])
    assert result and result["status"] == "queued"
    assert store.get_asset(TENANT, result["asset_id"])["kind"] == "live_segment"
    assert telemetry.observations[-1].connected is True


class FakeSqs:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.deleted: list[dict] = []
        self.visibility: list[dict] = []
        self.messages: list[dict] = []
        self.receives: list[dict] = []

    def send_message(self, **kwargs):
        self.sent.append(kwargs)

    def receive_message(self, **kwargs):
        self.receives.append(kwargs)
        return {"Messages": self.messages}

    def delete_message(self, **kwargs):
        self.deleted.append(kwargs)

    def change_message_visibility(self, **kwargs):
        self.visibility.append(kwargs)

    def get_queue_attributes(self, **kwargs):
        return {
            "Attributes": {
                "ApproximateNumberOfMessages": "2",
                "ApproximateNumberOfMessagesNotVisible": "1",
            }
        }


def test_sqs_delivery_is_minimal_retryable_and_explicitly_dead_lettered() -> None:
    client = FakeSqs()
    broker = SqsJobBroker(
        queue_url="https://sqs.example/jobs",
        dead_letter_queue_url="https://sqs.example/jobs-dlq",
        region_name="ap-south-1",
        wait_seconds=0,
        client=client,
    )
    message = JobMessage(TENANT, SOURCE, "upload")
    broker.publish(message)
    assert set(json.loads(client.sent[0]["MessageBody"])) == {
        "tenant_id",
        "job_id",
        "operation",
    }
    client.messages = [
        {
            "Body": message.body(),
            "ReceiptHandle": "receipt-1",
            "Attributes": {"ApproximateReceiveCount": "3"},
        }
    ]
    delivery = broker.receive(operations=("upload",))[0]
    broker.retry(delivery, delay_seconds=8)
    assert client.visibility[-1]["VisibilityTimeout"] == 8
    broker.dead_letter(delivery, error_code="reka_timeout")
    assert client.sent[-1]["QueueUrl"].endswith("jobs-dlq")
    assert client.deleted[-1]["ReceiptHandle"] == "receipt-1"
    assert broker.depth() == 3


def test_sqs_operation_queues_route_each_stage_without_worker_contention() -> None:
    client = FakeSqs()
    queues = {
        operation: f"https://sqs.example/{operation}"
        for operation in ("upload", "index", "analyze", "delete")
    }
    dlqs = {operation: f"{url}-dlq" for operation, url in queues.items()}
    broker = SqsJobBroker(
        queue_url=queues["upload"],
        queue_urls=queues,
        dead_letter_queue_url="https://sqs.example/jobs-dlq",
        dead_letter_queue_urls=dlqs,
        region_name="ap-south-1",
        wait_seconds=0,
        client=client,
    )
    index_message = JobMessage(TENANT, SOURCE, "index")
    broker.publish(index_message)
    assert client.sent[-1]["QueueUrl"] == queues["index"]
    client.messages = [
        {
            "Body": index_message.body(),
            "ReceiptHandle": "index-receipt",
            "Attributes": {"ApproximateReceiveCount": "1"},
        }
    ]
    delivery = broker.receive(operations=("index",))[0]
    assert client.receives[-1]["QueueUrl"] == queues["index"]
    broker.dead_letter(delivery, error_code="reka_index_failed")
    assert client.sent[-1]["QueueUrl"] == dlqs["index"]
    assert client.deleted[-1]["QueueUrl"] == queues["index"]
