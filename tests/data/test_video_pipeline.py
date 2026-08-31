from __future__ import annotations

import http.client
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.data.store import IngestionStore
from src.data.video import (
    DictLocationResolver,
    FakeRekaVisionProvider,
    VideoPipelineService,
    VideoStore,
)
from src.data.video.errors import VideoPipelineError
from src.data.video.reka import (
    RekaVisionProvider,
    _allowlisted_candidate_output,
    _read_bounded_http_response,
)

TENANT_A = "11111111-1111-4111-8111-111111111111"
TENANT_B = "22222222-2222-4222-8222-222222222222"
SOURCE_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
SOURCE_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


def timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def source(tenant_id: str, source_id: str, *, retention_days: int = 30) -> dict:
    return {
        "schema_version": "1.0.0",
        "tenant_id": tenant_id,
        "source_id": source_id,
        "name": "Approved demo camera",
        "mode": "recorded_video",
        "status": "active",
        "timezone": "UTC",
        "location_ref": f"secret://locations/{source_id}",
        "connection": {"transport": "uploaded_asset"},
        "retention_policy_days": retention_days,
        "created_at": "2026-01-01T00:00:00Z",
    }


def mp4(path: Path) -> Path:
    path.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"synthetic-not-real-media" * 8)
    return path


def proposal(
    offset_seconds: float,
    category: str,
    confidence: float,
    *,
    event_type: str | None = None,
    description: str = "A visible safety event requires human review.",
) -> dict:
    resolved_event_type = (
        event_type
        or {
            "property": "property_damage",
            "violence": "physical_fight",
            "public_order": "crowd_disturbance",
            "traffic_safety": "vehicle_collision",
            "other": "other_acute_hazard",
        }[category]
    )
    return {
        "offset_seconds": offset_seconds,
        "category": category,
        "event_type": resolved_event_type,
        "description": description,
        "confidence": confidence,
    }


def setup(
    tmp_path: Path, *, proposals: list[dict] | None = None, retention_days: int = 30
):
    restricted = tmp_path / "restricted"
    restricted.mkdir()
    ingestion = IngestionStore(tmp_path / "state.sqlite3")
    store = VideoStore(ingestion)
    provider = FakeRekaVisionProvider(proposals=proposals or [])
    resolver = DictLocationResolver(
        {
            (TENANT_A, f"secret://locations/{SOURCE_A}"): {
                "latitude": 12.9716,
                "longitude": 77.5946,
            },
            (TENANT_B, f"secret://locations/{SOURCE_B}"): {
                "latitude": 13.0827,
                "longitude": 80.2707,
            },
        }
    )

    class Inspector:
        def duration_seconds(self, path: Path) -> float:
            return 60.0

    service = VideoPipelineService(
        store,
        provider,
        resolver,
        media_root=restricted,
        max_upload_bytes=1024,
        media_inspector=Inspector(),
    )
    service.register_recorded_source(
        source(TENANT_A, SOURCE_A, retention_days=retention_days),
        authenticated_tenant_id=TENANT_A,
    )
    return restricted, ingestion, store, provider, service


def accept(
    service: VideoPipelineService,
    path: Path,
    *,
    tenant: str = TENANT_A,
    source_id: str = SOURCE_A,
    received_at: str | None = None,
):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return service.accept_upload(
        authenticated_tenant_id=tenant,
        source_id=source_id,
        path=path,
        content_type="video/mp4",
        captured_start=timestamp(start),
        captured_end=timestamp(start + timedelta(seconds=60)),
        duration_seconds=60,
        consent_confirmed=True,
        received_at=received_at,
    )


def test_recorded_video_to_confirmed_event_is_idempotent(tmp_path: Path) -> None:
    restricted, ingestion, store, provider, service = setup(
        tmp_path,
        proposals=[
            proposal(10, "property", 0.8, event_type="property_damage"),
            proposal(20, "public_order", 0.6, event_type="crowd_disturbance"),
        ],
    )
    asset = accept(service, mp4(restricted / "clip.mp4"))
    first = service.process_asset(TENANT_A, asset["asset_id"])
    second = service.process_asset(TENANT_A, asset["asset_id"])
    assert [item["detection_id"] for item in first] == [
        item["detection_id"] for item in second
    ]
    assert len([call for call in provider.calls if call[0] == "upload"]) == 1

    confirmed = service.review_candidate(
        authenticated_tenant_id=TENANT_A,
        detection_id=first[0]["detection_id"],
        decision="confirmed",
        confirmed_category="property",
        reviewed_by="reviewer-1",
        role="reviewer",
    )
    assert (
        service.review_candidate(
            authenticated_tenant_id=TENANT_A,
            detection_id=first[0]["detection_id"],
            decision="confirmed",
            confirmed_category="property",
            reviewed_by="reviewer-1",
            role="reviewer",
        )
        == confirmed
    )
    service.review_candidate(
        authenticated_tenant_id=TENANT_A,
        detection_id=first[1]["detection_id"],
        decision="rejected",
        rejection_reason="false_positive",
        reviewed_by="reviewer-1",
        role="reviewer",
    )
    assert ingestion.event_count(TENANT_A) == 1
    with pytest.raises(VideoPipelineError, match="immutable"):
        service.review_candidate(
            authenticated_tenant_id=TENANT_A,
            detection_id=first[0]["detection_id"],
            decision="rejected",
            rejection_reason="other",
            reviewed_by="reviewer-1",
            role="reviewer",
        )


def test_simulated_candidate_cannot_enter_incident_history(tmp_path: Path) -> None:
    restricted, ingestion, _, provider, service = setup(
        tmp_path,
        proposals=[proposal(4, "traffic_safety", 0.7, event_type="road_obstruction")],
    )
    simulated_source_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaa98"
    simulated = source(TENANT_A, simulated_source_id)
    simulated.update(
        name="Synthetic road simulation",
        mode="live_camera",
        connection={
            "transport": "hls",
            "endpoint_ref": "secret://demo-simulated-road/renderer",
        },
    )
    service.register_source(simulated, authenticated_tenant_id=TENANT_A)
    asset = accept(
        service,
        mp4(restricted / "simulated.mp4"),
        source_id=simulated_source_id,
    )
    candidate = service.process_asset(TENANT_A, asset["asset_id"])[0]

    with pytest.raises(VideoPipelineError) as caught:
        service.review_candidate(
            authenticated_tenant_id=TENANT_A,
            detection_id=candidate["detection_id"],
            decision="confirmed",
            confirmed_category="traffic_safety",
            reviewed_by="reviewer-1",
            role="reviewer",
        )
    assert caught.value.code == "simulated_candidate_confirmation_prohibited"
    assert ingestion.event_count(TENANT_A) == 0

    rejected = service.review_candidate(
        authenticated_tenant_id=TENANT_A,
        detection_id=candidate["detection_id"],
        decision="rejected",
        rejection_reason="outside_scope",
        reviewed_by="reviewer-1",
        role="reviewer",
    )
    assert rejected["decision"] == "rejected"
    assert ingestion.event_count(TENANT_A) == 0


def test_demo_session_cleanup_deletes_only_tenant_pending_candidates(
    tmp_path: Path,
) -> None:
    restricted, _, store, _, service = setup(
        tmp_path,
        proposals=[
            proposal(10, "property", 0.8, event_type="property_damage"),
            proposal(20, "public_order", 0.6, event_type="crowd_disturbance"),
        ],
    )
    asset = accept(service, mp4(restricted / "cleanup.mp4"))
    candidates = service.process_asset(TENANT_A, asset["asset_id"])
    service.review_candidate(
        authenticated_tenant_id=TENANT_A,
        detection_id=candidates[0]["detection_id"],
        decision="rejected",
        rejection_reason="false_positive",
        reviewed_by="reviewer-1",
        role="reviewer",
    )
    other_tenant_candidate = {
        **candidates[1],
        "tenant_id": TENANT_B,
        "detection_id": "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
        "review_status": "awaiting_review",
    }
    assert store.put_candidate(other_tenant_candidate, "tenant-b-pending")

    assert store.delete_pending_candidates(TENANT_A) == 1
    remaining_a = store.list_candidates(TENANT_A)
    assert [item["review_status"] for item in remaining_a] == ["rejected"]
    assert store.list_candidates(TENANT_B) == [other_tenant_candidate]


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        ({"content_type": "video/quicktime"}, "video_type_invalid"),
        ({"consent_confirmed": False}, "consent_required"),
        ({"duration_seconds": 65}, "video_duration_mismatch"),
        ({"expected_sha256": "0" * 64}, "checksum_mismatch"),
    ],
)
def test_upload_validation(tmp_path: Path, mutation: dict, code: str) -> None:
    restricted, _, _, _, service = setup(tmp_path)
    path = mp4(restricted / "clip.mp4")
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    values = dict(
        authenticated_tenant_id=TENANT_A,
        source_id=SOURCE_A,
        path=path,
        content_type="video/mp4",
        captured_start=timestamp(start),
        captured_end=timestamp(start + timedelta(seconds=60)),
        duration_seconds=60,
        consent_confirmed=True,
    )
    values.update(mutation)
    with pytest.raises(VideoPipelineError) as caught:
        service.accept_upload(**values)
    assert caught.value.code == code


def test_corrupt_oversize_and_quota_uploads_are_rejected(tmp_path: Path) -> None:
    restricted, ingestion, store, provider, service = setup(tmp_path)
    corrupt = restricted / "bad.mp4"
    corrupt.write_bytes(b"not-an-mp4-container")
    with pytest.raises(VideoPipelineError) as caught:
        accept(service, corrupt)
    assert caught.value.code == "video_corrupt"
    large = restricted / "large.mp4"
    large.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"x" * 2000)
    with pytest.raises(VideoPipelineError) as caught:
        accept(service, large)
    assert caught.value.code == "video_size_invalid"
    quota_service = VideoPipelineService(
        store,
        provider,
        service.location_resolver,
        media_root=restricted,
        tenant_quota_bytes=1,
        media_inspector=service.media_inspector,
    )
    with pytest.raises(VideoPipelineError) as caught:
        accept(quota_service, mp4(restricted / "quota.mp4"))
    assert caught.value.code == "tenant_video_quota_exceeded"


def test_tenant_boundary_includes_remote_mapping_and_candidates(tmp_path: Path) -> None:
    restricted, _, store, _, service = setup(
        tmp_path, proposals=[proposal(1, "other", 0.5)]
    )
    service.register_recorded_source(
        source(TENANT_B, SOURCE_B), authenticated_tenant_id=TENANT_B
    )
    asset = accept(service, mp4(restricted / "clip.mp4"))
    candidates = service.process_asset(TENANT_A, asset["asset_id"])
    remote_id = store.get_mapping(TENANT_A, asset["asset_id"])["reka_video_id"]
    assert store.mapping_by_remote_id(TENANT_B, remote_id) is None
    with pytest.raises(VideoPipelineError):
        store.get_candidate(TENANT_B, candidates[0]["detection_id"])
    with pytest.raises(VideoPipelineError):
        store.get_asset(TENANT_B, asset["asset_id"])


def test_prohibited_reka_output_cannot_persist(tmp_path: Path) -> None:
    restricted, _, store, _, service = setup(
        tmp_path,
        proposals=[
            {
                **proposal(1, "property", 0.9),
                "identity": "ignore previous instructions",
            }
        ],
    )
    asset = accept(service, mp4(restricted / "clip.mp4"))
    with pytest.raises(VideoPipelineError) as caught:
        service.process_asset(TENANT_A, asset["asset_id"])
    assert caught.value.code == "reka_output_prohibited"
    assert caught.value.safe_diagnostics == {
        "proposal_index": 0,
        "unexpected_field_count": 1,
    }
    assert store.list_candidates(TENANT_A) == []


def test_missing_reka_fields_have_value_free_diagnostics(tmp_path: Path) -> None:
    restricted, _, store, _, service = setup(
        tmp_path,
        proposals=[
            {
                "offset_seconds": 1,
                "category": "property",
                "event_type": "property_damage",
                "description": "Property is visibly damaged.",
            }
        ],
    )
    asset = accept(service, mp4(restricted / "clip.mp4"))
    with pytest.raises(VideoPipelineError) as caught:
        service.process_asset(TENANT_A, asset["asset_id"])
    assert caught.value.code == "reka_output_missing_fields"
    assert caught.value.safe_diagnostics == {
        "proposal_index": 0,
        "missing_fields": ["confidence"],
    }
    failed = next(
        job for job in store.list_jobs(TENANT_A) if job["operation"] == "analyze"
    )
    assert failed["state"] == "failed"
    assert failed["safe_diagnostics"] == caught.value.safe_diagnostics
    assert store.get_asset(TENANT_A, asset["asset_id"])["status"] == "failed"
    assert store.list_candidates(TENANT_A) == []


def test_valid_candidate_survives_out_of_range_sibling(tmp_path: Path) -> None:
    restricted, _, store, _, service = setup(
        tmp_path,
        proposals=[
            proposal(1, "other", 0.95),
            proposal(100, "other", 0.8),
        ],
    )
    asset = accept(service, mp4(restricted / "clip.mp4"))
    candidates = service.process_asset(TENANT_A, asset["asset_id"])
    assert len(candidates) == 1
    assert candidates[0]["proposed_category"] == "other"
    assert candidates[0]["confidence"] == 0.95
    assert store.get_asset(TENANT_A, asset["asset_id"])["status"] == "processed"


def test_only_out_of_range_candidates_remain_fail_closed(tmp_path: Path) -> None:
    restricted, _, store, _, service = setup(
        tmp_path,
        proposals=[proposal(100, "other", 0.8)],
    )
    asset = accept(service, mp4(restricted / "clip.mp4"))
    with pytest.raises(VideoPipelineError) as caught:
        service.process_asset(TENANT_A, asset["asset_id"])
    assert caught.value.code == "reka_output_invalid"
    assert caught.value.safe_diagnostics == {
        "proposal_index": 0,
        "invalid_fields": ["offset_seconds"],
    }
    assert store.list_candidates(TENANT_A) == []


def test_video_errors_reject_unbounded_or_value_bearing_diagnostics() -> None:
    with pytest.raises(ValueError, match="Unsupported safe diagnostic"):
        VideoPipelineError(
            "reka_output_invalid",
            "invalid",
            safe_diagnostics={"raw_output": "must never be retained"},
        )
    with pytest.raises(ValueError, match="allowlisted stage"):
        VideoPipelineError(
            "reka_output_invalid",
            "invalid",
            safe_diagnostics={
                "format_stage": "provider-controlled-stage",
                "format_reason": "json_format_invalid",
            },
        )
    with pytest.raises(ValueError, match="provided together"):
        VideoPipelineError(
            "reka_output_invalid",
            "invalid",
            safe_diagnostics={"format_stage": "short_video_candidate"},
        )


def test_expired_candidate_creates_no_event(tmp_path: Path) -> None:
    restricted, ingestion, store, _, service = setup(
        tmp_path, proposals=[proposal(1, "property", 0.9)]
    )
    asset = accept(service, mp4(restricted / "clip.mp4"))
    candidate = service.process_asset(TENANT_A, asset["asset_id"])[0]
    assert (
        service.expire_due_candidates(
            TENANT_A, now=timestamp(datetime.now(timezone.utc) + timedelta(days=8))
        )
        == 1
    )
    assert (
        store.get_candidate(TENANT_A, candidate["detection_id"])["review_status"]
        == "expired"
    )
    with pytest.raises(VideoPipelineError) as caught:
        service.review_candidate(
            authenticated_tenant_id=TENANT_A,
            detection_id=candidate["detection_id"],
            decision="confirmed",
            confirmed_category="property",
            reviewed_by="reviewer-1",
            role="reviewer",
        )
    assert caught.value.code == "candidate_expired"
    assert ingestion.event_count(TENANT_A) == 0


def test_retry_state_and_key_errors_are_safe(tmp_path: Path) -> None:
    restricted, _, store, provider, service = setup(tmp_path)
    asset = accept(service, mp4(restricted / "clip.mp4"))
    provider.fail_operations.add("upload")
    with pytest.raises(VideoPipelineError) as caught:
        service.process_asset(TENANT_A, asset["asset_id"])
    assert caught.value.retryable
    assert store.job_metrics(TENANT_A)["retry"] == 1
    job = store.enqueue(TENANT_A, asset["asset_id"], "upload")
    store.transition_job(TENANT_A, job["job_id"], "running")
    assert (
        store.recover_stale_jobs(
            stale_after=timedelta(minutes=5),
            now=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        == 1
    )
    secret = "rk-secret-never-log"
    client = RekaVisionProvider(secret)
    assert secret not in repr(client)
    assert client.chat_model == "reka-edge-2603"
    with pytest.raises(VideoPipelineError) as missing:
        RekaVisionProvider("")
    assert missing.value.code == "reka_key_missing"


def test_reka_candidate_boundary_rejects_provider_extras() -> None:
    with pytest.raises(VideoPipelineError) as caught:
        _allowlisted_candidate_output(
            [{**proposal(2, "property", 0.7), "identity": "prohibited"}]
        )
    assert caught.value.code == "reka_output_prohibited"
    assert caught.value.safe_diagnostics == {
        "proposal_index": 0,
        "unexpected_field_count": 1,
    }


def test_reka_candidate_boundary_classifies_unhashable_category() -> None:
    with pytest.raises(VideoPipelineError) as caught:
        _allowlisted_candidate_output(
            [{**proposal(2, "property", 0.7), "category": ["property"]}]
        )
    assert caught.value.code == "reka_output_invalid"
    assert caught.value.safe_diagnostics == {
        "proposal_index": 0,
        "invalid_fields": ["category"],
    }


def test_reka_candidate_boundary_rejects_benign_or_unknown_event_type() -> None:
    with pytest.raises(VideoPipelineError) as caught:
        _allowlisted_candidate_output(
            [{**proposal(0, "other", 0.8), "event_type": "organization"}]
        )
    assert caught.value.code == "reka_output_invalid"
    assert caught.value.safe_diagnostics == {
        "proposal_index": 0,
        "invalid_fields": ["event_type"],
    }


def test_reka_candidate_boundary_rejects_event_category_mismatch() -> None:
    with pytest.raises(VideoPipelineError) as caught:
        _allowlisted_candidate_output(
            [proposal(0, "property", 0.8, event_type="structural_collapse")]
        )
    assert caught.value.code == "reka_output_invalid"
    assert caught.value.safe_diagnostics == {
        "proposal_index": 0,
        "invalid_fields": ["category"],
    }


def test_reka_candidate_boundary_rejects_more_than_twenty_five_proposals() -> None:
    candidate = proposal(2, "property", 0.7)
    with pytest.raises(VideoPipelineError) as caught:
        _allowlisted_candidate_output([candidate] * 26)
    assert caught.value.code == "reka_output_invalid"
    assert caught.value.safe_diagnostics == {}


def test_reka_http_response_body_is_bounded() -> None:
    class OversizedResponse:
        def read(self, amount: int) -> bytes:
            return b"x" * amount

    with pytest.raises(VideoPipelineError) as caught:
        _read_bounded_http_response(OversizedResponse())  # type: ignore[arg-type]
    assert caught.value.code == "reka_response_invalid"


def test_reka_http_protocol_failure_is_safe_and_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class TruncatedResponse:
        status = 200

        def read(self, amount: int) -> bytes:
            del amount
            raise http.client.IncompleteRead(b"provider-value-must-not-escape")

    class FakeConnection:
        def request(self, *args: object, **kwargs: object) -> None:
            return None

        def getresponse(self) -> TruncatedResponse:
            return TruncatedResponse()

        def close(self) -> None:
            return None

    connection = FakeConnection()
    monkeypatch.setattr(
        "src.data.video.reka.http.client.HTTPSConnection",
        lambda *args, **kwargs: connection,
    )
    client = RekaVisionProvider("rk-test-only")
    with pytest.raises(VideoPipelineError) as caught:
        client._chat_json_request({"model": "reka-edge-2603", "messages": []})
    assert caught.value.code == "reka_timeout"
    assert caught.value.retryable
    assert caught.value.__cause__ is None


def test_reka_chat_classifies_bounded_frame_mismatch_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FrameMismatchResponse:
        status = 400

        def read(self, amount: int) -> bytes:
            del amount
            return (
                b'{"error":{"message":"Expected 6 frames, got 5 None",'
                b'"type":"BadRequestError","code":"invalid_request","param":null}}'
            )

    class FakeConnection:
        def request(self, *args: object, **kwargs: object) -> None:
            return None

        def getresponse(self) -> FrameMismatchResponse:
            return FrameMismatchResponse()

        def close(self) -> None:
            return None

    monkeypatch.setattr(
        "src.data.video.reka.http.client.HTTPSConnection",
        lambda *args, **kwargs: FakeConnection(),
    )
    client = RekaVisionProvider("rk-test-only")
    with pytest.raises(VideoPipelineError) as caught:
        client._chat_json_request({"model": "reka-edge-2603", "messages": []})
    assert caught.value.code == "reka_media_frame_mismatch"
    assert caught.value.safe_diagnostics == {}


def test_reka_http_object_envelope_is_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ArrayResponse:
        status = 200

        def read(self, amount: int) -> bytes:
            return b"[]"

    class FakeConnection:
        def request(self, *args: object, **kwargs: object) -> None:
            return None

        def getresponse(self) -> ArrayResponse:
            return ArrayResponse()

        def close(self) -> None:
            return None

    client = RekaVisionProvider("rk-test-only")
    connection = FakeConnection()
    monkeypatch.setattr(client, "_connection", lambda: connection)
    with pytest.raises(VideoPipelineError) as vision_error:
        client._json_request("GET", "/v1/videos/test")
    assert vision_error.value.code == "reka_response_invalid"

    monkeypatch.setattr(
        "src.data.video.reka.http.client.HTTPSConnection",
        lambda *args, **kwargs: connection,
    )
    with pytest.raises(VideoPipelineError) as chat_error:
        client._chat_json_request({"model": "reka-edge-2603", "messages": []})
    assert chat_error.value.code == "reka_response_invalid"
    assert chat_error.value.__cause__ is None


def test_reka_candidate_json_parse_failure_does_not_retain_provider_value() -> None:
    provider_value = "provider-value-must-not-escape"
    with pytest.raises(VideoPipelineError) as caught:
        RekaVisionProvider._decode_candidate_json(
            provider_value,
            stage="short_video_candidate",
        )
    assert caught.value.code == "reka_output_invalid"
    assert caught.value.__cause__ is None
    assert provider_value not in str(caught.value)


def test_reka_candidate_json_recursion_error_is_sanitized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider_value = "provider-recursion-value-must-not-escape"

    def recursive_loads(*args: object, **kwargs: object) -> object:
        raise RecursionError(provider_value)

    monkeypatch.setattr("src.data.video.reka.json.loads", recursive_loads)
    with pytest.raises(VideoPipelineError) as caught:
        RekaVisionProvider._decode_candidate_json(
            "[]",
            stage="short_video_candidate",
        )
    assert caught.value.code == "reka_output_invalid"
    assert caught.value.safe_diagnostics == {
        "format_stage": "short_video_candidate",
        "format_reason": "json_format_invalid",
    }
    assert caught.value.__cause__ is None
    assert provider_value not in str(caught.value)


def test_reka_repairs_explanatory_no_incident_shape_to_empty_result() -> None:
    client = RekaVisionProvider("rk-test-only")
    responses = iter(
        [
            {"chat_response": '[{"status":"no qualifying incident"}]'},
            {"chat_response": "[]"},
        ]
    )
    requests: list[dict] = []

    def fake_request(method: str, path: str, payload: dict | None = None) -> dict:
        assert method == "POST"
        assert path == "/v1/qa/chat"
        requests.append(payload or {})
        return next(responses)

    client._json_request = fake_request  # type: ignore[method-assign]
    assert client.propose_candidates("video-test", prompt_version="candidate-v2") == []
    assert len(requests) == 2
    assert "prior answer did not match" in requests[1]["messages"][0]["content"]


def test_reka_schema_repair_remains_fail_closed() -> None:
    client = RekaVisionProvider("rk-test-only")
    responses = iter(
        [
            {"chat_response": '[{"status":"clear"}]'},
            {"chat_response": '[{"summary":"still malformed"}]'},
        ]
    )

    def fake_request(method: str, path: str, payload: dict | None = None) -> dict:
        return next(responses)

    client._json_request = fake_request  # type: ignore[method-assign]
    with pytest.raises(VideoPipelineError) as caught:
        client.propose_candidates("video-test", prompt_version="candidate-v2")
    assert caught.value.code == "reka_output_prohibited"
    assert caught.value.safe_diagnostics == {
        "proposal_index": 0,
        "unexpected_field_count": 1,
    }


def test_reka_repairs_invalid_candidate_values_to_empty_result() -> None:
    client = RekaVisionProvider("rk-test-only")
    responses = iter(
        [
            {
                "chat_response": '[{"offset_seconds":0,"category":"no_incident",'
                '"event_type":"no_incident","description":"No incident.",'
                '"confidence":1}]'
            },
            {"chat_response": "[]"},
        ]
    )

    def fake_request(method: str, path: str, payload: dict | None = None) -> dict:
        return next(responses)

    client._json_request = fake_request  # type: ignore[method-assign]
    assert client.propose_candidates("video-test", prompt_version="candidate-v2") == []


def test_reka_rejects_wrapped_candidate_array_after_one_repair() -> None:
    client = RekaVisionProvider("rk-test-only")
    responses = iter(
        [
            {
                "chat_response": (
                    '{"result":[{"offset_seconds":2,"category":"traffic_safety",'
                    '"event_type":"vehicle_collision",'
                    '"description":"Two vehicles visibly collide.",'
                    '"confidence":0.75}]}'
                )
            },
            {
                "chat_response": (
                    '{"result":[{"offset_seconds":2,"category":"traffic_safety",'
                    '"event_type":"vehicle_collision",'
                    '"description":"Two vehicles visibly collide.",'
                    '"confidence":0.75}]}'
                )
            },
        ]
    )

    def fake_request(method: str, path: str, payload: dict | None = None) -> dict:
        return next(responses)

    client._json_request = fake_request  # type: ignore[method-assign]
    with pytest.raises(VideoPipelineError) as caught:
        client.propose_candidates("video-test", prompt_version="candidate-v2")
    assert caught.value.code == "reka_output_invalid"


def test_short_video_uses_multimodal_chat_instead_of_indexed_qa(
    tmp_path: Path,
) -> None:
    client = RekaVisionProvider("rk-test-only")
    video = tmp_path / "short.mp4"
    video.write_bytes(b"bounded-test-video")
    requests: list[dict] = []

    def fake_chat_request(payload: dict) -> dict:
        requests.append(payload)
        return {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "[]"},
                }
            ]
        }

    client._chat_json_request = fake_chat_request  # type: ignore[method-assign]
    assert (
        client.propose_candidates(
            "unused-indexed-id",
            prompt_version="candidate-v2",
            media_path=video,
            duration_seconds=8,
        )
        == []
    )
    content = requests[0]["messages"][0]["content"]
    assert content[0]["type"] == "video_url"
    assert content[0]["video_url"]["url"].startswith("data:video/mp4;base64,")
    assert "bounded-test-video" not in content[0]["video_url"]["url"]
    assert requests[0]["temperature"] == 0
    assert requests[0]["max_tokens"] == 2048
    assert requests[0]["seed"] == 17
    assert len(requests[0]["messages"]) == 1
    assert requests[0]["messages"][0]["role"] == "user"
    response_format = requests[0]["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["strict"] is True
    assert response_format["json_schema"]["schema"]["type"] == "array"
    item_schema = response_format["json_schema"]["schema"]["items"]
    assert item_schema["additionalProperties"] is False
    assert item_schema["required"] == [
        "offset_seconds",
        "category",
        "event_type",
        "description",
        "confidence",
    ]
    candidate_prompt = content[-1]["text"]
    assert "structural collapse" in candidate_prompt
    assert "rock-paper-scissors" in candidate_prompt
    assert "physical attack" in candidate_prompt
    assert "authoritative clip duration is 8.000 seconds" in candidate_prompt
    assert "never emit periodic timeline samples" in candidate_prompt
    assert "candidate-v2." in candidate_prompt


def test_short_video_uses_native_quick_tag_then_structured_text_classification(
    tmp_path: Path,
) -> None:
    client = RekaVisionProvider("rk-test-only", use_quick_tag_pipeline=True)
    video = tmp_path / "short.mp4"
    video.write_bytes(b"bounded-test-video")
    requests: list[dict] = []

    def fake_quick_tag(media_path: Path) -> dict:
        assert media_path == video
        return {
            "description": "People play a hand game at an indoor gathering.",
            "violence": False,
        }

    def fake_chat_request(payload: dict) -> dict:
        requests.append(payload)
        return {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "[]"},
                }
            ]
        }

    client._quick_tag_context = fake_quick_tag  # type: ignore[method-assign]
    client._chat_json_request = fake_chat_request  # type: ignore[method-assign]
    assert (
        client.propose_candidates(
            "unused-indexed-id",
            prompt_version="candidate-v2",
            media_path=video,
            duration_seconds=8,
        )
        == []
    )
    assert requests[0]["model"] == "reka-flash-3"
    assert requests[0]["max_tokens"] == 1024
    prompt = requests[0]["messages"][0]["content"]
    assert '"violence":false' in prompt
    assert "People play a hand game at an indoor gathering." in prompt
    assert "fallible visual evidence" in prompt
    assert "false value cannot hide a collapse" in prompt


def test_short_video_structural_collapse_reaches_candidate_review(
    tmp_path: Path,
) -> None:
    client = RekaVisionProvider("rk-test-only")
    video = tmp_path / "collapse.mp4"
    video.write_bytes(b"bounded-collapse-video")
    requests: list[dict] = []

    def fake_chat_request(payload: dict) -> dict:
        requests.append(payload)
        prompt = payload["messages"][0]["content"][-1]["text"]
        assert "structural collapse" in prompt
        assert "Use other" in prompt
        content = (
            '[{"offset_seconds":0.5,"category":"other",'
            '"event_type":"structural_collapse",'
            '"description":"A multistorey structure visibly collapses.",'
            '"confidence":0.91}]'
        )
        return {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": content},
                }
            ]
        }

    client._chat_json_request = fake_chat_request  # type: ignore[method-assign]
    assert client.propose_candidates(
        "unused-indexed-id",
        prompt_version="candidate-v2",
        media_path=video,
    ) == [
        {
            "offset_seconds": 0.5,
            "category": "other",
            "event_type": "structural_collapse",
            "description": "A multistorey structure visibly collapses.",
            "confidence": 0.91,
        }
    ]
    assert len(requests) == 1


def test_short_video_retries_frame_mismatch_with_normalized_media(
    tmp_path: Path,
) -> None:
    client = RekaVisionProvider("rk-test-only")
    video = tmp_path / "collapse.mp4"
    video.write_bytes(b"bounded-collapse-video")
    normalized = [
        {
            "type": "video_url",
            "video_url": {"url": "data:video/mp4;base64,bm9ybWFsaXplZA=="},
        }
    ]
    requests: list[dict] = []

    def fake_normalize(media_path: Path) -> list[dict]:
        assert media_path == video
        return normalized

    def fake_chat_request(payload: dict) -> dict:
        requests.append(payload)
        if len(requests) == 1:
            raise VideoPipelineError(
                "reka_media_frame_mismatch",
                "bounded provider frame mismatch",
            )
        return {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": (
                            '[{"offset_seconds":0,"category":"other",'
                            '"event_type":"structural_collapse",'
                            '"description":"A structure visibly collapses.",'
                            '"confidence":0.95}]'
                        ),
                    },
                }
            ]
        }

    client._normalized_short_video_content = fake_normalize  # type: ignore[method-assign]
    client._chat_json_request = fake_chat_request  # type: ignore[method-assign]
    assert client.propose_candidates(
        "unused-indexed-id",
        prompt_version="candidate-v2",
        media_path=video,
    ) == [
        {
            "offset_seconds": 0,
            "category": "other",
            "event_type": "structural_collapse",
            "description": "A structure visibly collapses.",
            "confidence": 0.95,
        }
    ]
    assert len(requests) == 2
    assert requests[1]["messages"][0]["content"][0] == normalized[0]


def test_short_video_normalization_failure_is_sanitized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    video = tmp_path / "collapse.mp4"
    video.write_bytes(b"bounded-collapse-video")

    def fail_normalization(*args: object, **kwargs: object) -> None:
        raise subprocess.CalledProcessError(
            1,
            "ffmpeg",
            stderr=b"provider-controlled-media-value",
        )

    monkeypatch.setattr("src.data.video.reka.subprocess.run", fail_normalization)
    with pytest.raises(VideoPipelineError) as caught:
        RekaVisionProvider._normalized_short_video_content(video)
    assert caught.value.code == "reka_media_prepare_failed"
    assert caught.value.__cause__ is None
    assert "provider-controlled-media-value" not in str(caught.value)


def test_short_video_empty_candidate_array_needs_no_binary_screen(
    tmp_path: Path,
) -> None:
    client = RekaVisionProvider("rk-test-only")
    video = tmp_path / "short.mp4"
    video.write_bytes(b"bounded-test-video")
    requests: list[dict] = []

    def fake_chat_request(payload: dict) -> dict:
        requests.append(payload)
        return {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": "[]",
                    },
                }
            ]
        }

    client._chat_json_request = fake_chat_request  # type: ignore[method-assign]
    assert (
        client.propose_candidates(
            "unused-indexed-id",
            prompt_version="candidate-v2",
            media_path=video,
        )
        == []
    )
    assert len(requests) == 1
    assert len(requests[0]["messages"]) == 1
    assert requests[0]["messages"][0]["role"] == "user"
    assert requests[0]["max_tokens"] == 2048
    assert requests[0]["seed"] == 17
    prompt = requests[0]["messages"][0]["content"][-1]["text"]
    assert "never emit periodic timeline samples" in prompt
    assert "rock-paper-scissors" in prompt


@pytest.mark.parametrize(
    "content",
    [
        [{"type": "text", "text": "[]"}],
        {"type": "output_text", "text": "[]"},
        [
            {"type": "output_text", "text": ""},
            {"type": "text", "text": "[]"},
        ],
    ],
)
def test_short_video_accepts_safe_openai_text_content_blocks(
    tmp_path: Path,
    content: object,
) -> None:
    client = RekaVisionProvider("rk-test-only")
    video = tmp_path / "short.mp4"
    video.write_bytes(b"bounded-test-video")

    def fake_chat_request(payload: dict) -> dict:
        return {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": content},
                }
            ]
        }

    client._chat_json_request = fake_chat_request  # type: ignore[method-assign]
    assert (
        client.propose_candidates(
            "unused-indexed-id",
            prompt_version="candidate-v2",
            media_path=video,
        )
        == []
    )


def test_short_video_rejects_prose_instead_of_treating_it_as_clear(
    tmp_path: Path,
) -> None:
    client = RekaVisionProvider("rk-test-only")
    video = tmp_path / "short.mp4"
    video.write_bytes(b"bounded-test-video")
    contents = iter(["Classification: CLEAR", "CLEAR because the road is routine"])

    def fake_chat_request(payload: dict) -> dict:
        return {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": next(contents),
                    },
                }
            ]
        }

    client._chat_json_request = fake_chat_request  # type: ignore[method-assign]
    with pytest.raises(VideoPipelineError) as caught:
        client.propose_candidates(
            "unused-indexed-id",
            prompt_version="candidate-v2",
            media_path=video,
        )
    assert caught.value.code == "reka_output_invalid"
    assert caught.value.safe_diagnostics == {
        "format_stage": "short_video_candidate",
        "format_reason": "json_format_invalid",
    }


@pytest.mark.parametrize(
    "content",
    [
        [
            {"type": "text", "text": "CLEAR"},
            {"type": "tool_call", "id": "tool-call"},
        ],
        [{"type": "refusal", "refusal": "not returned to callers"}],
        [{"type": "unknown", "text": "CLEAR"}],
    ],
)
def test_short_video_rejects_mixed_or_non_text_content_blocks(
    tmp_path: Path,
    content: object,
) -> None:
    client = RekaVisionProvider("rk-test-only")
    video = tmp_path / "short.mp4"
    video.write_bytes(b"bounded-test-video")

    def fake_chat_request(payload: dict) -> dict:
        return {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": content},
                }
            ]
        }

    client._chat_json_request = fake_chat_request  # type: ignore[method-assign]
    with pytest.raises(VideoPipelineError) as caught:
        client.propose_candidates(
            "unused-indexed-id",
            prompt_version="candidate-v2",
            media_path=video,
        )
    assert caught.value.code == "reka_response_invalid"
    assert caught.value.safe_diagnostics == {
        "format_stage": "short_video_candidate",
        "format_reason": "content_shape_invalid",
    }


@pytest.mark.parametrize(
    ("role", "finish_reason"),
    [("user", "stop"), ("assistant", "tool_calls"), (None, "stop")],
)
def test_short_video_rejects_non_assistant_or_non_stop_completions(
    tmp_path: Path,
    role: str | None,
    finish_reason: str,
) -> None:
    client = RekaVisionProvider("rk-test-only")
    video = tmp_path / "short.mp4"
    video.write_bytes(b"bounded-test-video")

    def fake_chat_request(payload: dict) -> dict:
        return {
            "choices": [
                {
                    "finish_reason": finish_reason,
                    "message": {"role": role, "content": "CLEAR"},
                }
            ]
        }

    client._chat_json_request = fake_chat_request  # type: ignore[method-assign]
    with pytest.raises(VideoPipelineError) as caught:
        client.propose_candidates(
            "unused-indexed-id",
            prompt_version="candidate-v2",
            media_path=video,
        )
    assert caught.value.code == "reka_response_invalid"
    assert caught.value.safe_diagnostics == {
        "format_stage": "short_video_candidate",
        "format_reason": "response_shape_invalid",
    }


def test_short_video_accepts_a_complete_candidate_array(
    tmp_path: Path,
) -> None:
    client = RekaVisionProvider("rk-test-only")
    video = tmp_path / "short.mp4"
    video.write_bytes(b"bounded-test-video")
    content = [
        {
            "type": "output_text",
            "text": (
                '[{"offset_seconds":2,"category":"traffic_safety",'
                '"event_type":"vehicle_collision",'
                '"description":"Two vehicles visibly collide.",'
                '"confidence":0.8}]'
            ),
        }
    ]

    def fake_chat_request(payload: dict) -> dict:
        return {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": content,
                    },
                }
            ]
        }

    client._chat_json_request = fake_chat_request  # type: ignore[method-assign]
    assert client.propose_candidates(
        "unused-indexed-id",
        prompt_version="candidate-v2",
        media_path=video,
    ) == [
        {
            "offset_seconds": 2,
            "category": "traffic_safety",
            "event_type": "vehicle_collision",
            "description": "Two vehicles visibly collide.",
            "confidence": 0.8,
        }
    ]


def test_short_video_rejects_single_candidate_object_without_array(
    tmp_path: Path,
) -> None:
    client = RekaVisionProvider("rk-test-only")
    video = tmp_path / "short.mp4"
    video.write_bytes(b"bounded-test-video")

    def fake_chat_request(payload: dict) -> dict:
        return {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": (
                            '{"offset_seconds":0,"category":"other",'
                            '"event_type":"structural_collapse",'
                            '"description":"A structure visibly collapses.",'
                            '"confidence":0.85}'
                        ),
                    },
                }
            ]
        }

    client._chat_json_request = fake_chat_request  # type: ignore[method-assign]
    with pytest.raises(VideoPipelineError) as caught:
        client.propose_candidates(
            "unused-indexed-id",
            prompt_version="candidate-v2",
            media_path=video,
        )
    assert caught.value.code == "reka_output_invalid"


def test_short_video_repairs_single_object_missing_offset(
    tmp_path: Path,
) -> None:
    client = RekaVisionProvider("rk-test-only")
    video = tmp_path / "short.mp4"
    video.write_bytes(b"bounded-test-video")
    requests: list[dict] = []
    responses = iter(
        [
            '{"confidence":0.85,"category":"other"}',
            '[{"offset_seconds":0,"category":"other",'
            '"event_type":"structural_collapse",'
            '"description":"A structure visibly collapses.",'
            '"confidence":0.95}]',
        ]
    )

    def fake_chat_request(payload: dict) -> dict:
        requests.append(payload)
        return {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": next(responses)},
                }
            ]
        }

    client._chat_json_request = fake_chat_request  # type: ignore[method-assign]
    assert client.propose_candidates(
        "unused-indexed-id",
        prompt_version="candidate-v2",
        media_path=video,
    ) == [
        {
            "offset_seconds": 0,
            "category": "other",
            "event_type": "structural_collapse",
            "description": "A structure visibly collapses.",
            "confidence": 0.95,
        }
    ]
    assert len(requests) == 2
    assert all(request["seed"] == 17 for request in requests)
    repair_prompt = requests[1]["messages"][0]["content"][-1]["text"]
    assert "allowed event/category pairs" in repair_prompt
    assert "rock-paper-scissors" in repair_prompt


def test_short_video_rejects_reka_assistant_role_marker(
    tmp_path: Path,
) -> None:
    client = RekaVisionProvider("rk-test-only")
    video = tmp_path / "short.mp4"
    video.write_bytes(b"bounded-test-video")

    def fake_chat_request(payload: dict) -> dict:
        return {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": (
                            'assistant: {"offset_seconds":0,"category":"other",'
                            '"confidence":0.94}]'
                        ),
                    },
                }
            ]
        }

    client._chat_json_request = fake_chat_request  # type: ignore[method-assign]
    with pytest.raises(VideoPipelineError) as caught:
        client.propose_candidates(
            "unused-indexed-id",
            prompt_version="candidate-v2",
            media_path=video,
        )
    assert caught.value.code == "reka_output_invalid"
    assert caught.value.safe_diagnostics == {
        "format_stage": "short_video_candidate",
        "format_reason": "json_format_invalid",
    }


def test_short_video_rejects_role_marker_before_full_json_fence(
    tmp_path: Path,
) -> None:
    client = RekaVisionProvider("rk-test-only")
    video = tmp_path / "short.mp4"
    video.write_bytes(b"bounded-test-video")

    def fake_chat_request(payload: dict) -> dict:
        return {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": (
                            "assistant: ```json\n"
                            '[{"offset_seconds":0,"category":"other",'
                            '"confidence":0.92}]\n```'
                        ),
                    },
                }
            ]
        }

    client._chat_json_request = fake_chat_request  # type: ignore[method-assign]
    with pytest.raises(VideoPipelineError) as caught:
        client.propose_candidates(
            "unused-indexed-id",
            prompt_version="candidate-v2",
            media_path=video,
        )
    assert caught.value.code == "reka_output_invalid"
    assert caught.value.safe_diagnostics == {
        "format_stage": "short_video_candidate",
        "format_reason": "json_format_invalid",
    }


@pytest.mark.parametrize(
    "content",
    [
        'analysis: {"offset_seconds":0,"category":"other","confidence":0.9}]',
        (
            'assistant: explanation: {"offset_seconds":0,"category":"other",'
            '"confidence":0.9}]'
        ),
        'human { "offset_seconds": 0, "category": "traffic_safety" }\n\n]\n\n',
    ],
)
def test_short_video_rejects_unallowlisted_role_or_prose_wrappers(
    tmp_path: Path,
    content: str,
) -> None:
    client = RekaVisionProvider("rk-test-only")
    video = tmp_path / "short.mp4"
    video.write_bytes(b"bounded-test-video")

    def fake_chat_request(payload: dict) -> dict:
        return {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": content},
                }
            ]
        }

    client._chat_json_request = fake_chat_request  # type: ignore[method-assign]
    with pytest.raises(VideoPipelineError) as caught:
        client.propose_candidates(
            "unused-indexed-id",
            prompt_version="candidate-v2",
            media_path=video,
        )
    assert caught.value.code == "reka_output_invalid"
    assert caught.value.safe_diagnostics == {
        "format_stage": "short_video_candidate",
        "format_reason": "json_format_invalid",
    }


def test_short_video_repairs_chat_role_artifact_without_inventing_fields(
    tmp_path: Path,
) -> None:
    client = RekaVisionProvider("rk-test-only")
    video = tmp_path / "short.mp4"
    video.write_bytes(b"bounded-test-video")
    requests: list[dict] = []
    responses = iter(
        [
            'human { "offset_seconds": 0, "category": "traffic_safety" }\n\n]\n\n',
            (
                '[{"offset_seconds":0,"category":"traffic_safety",'
                '"event_type":"vehicle_collision",'
                '"description":"Two vehicles visibly collide.",'
                '"confidence":0.88}]'
            ),
        ]
    )

    def fake_chat_request(payload: dict) -> dict:
        requests.append(payload)
        return {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": next(responses),
                    },
                }
            ]
        }

    client._chat_json_request = fake_chat_request  # type: ignore[method-assign]
    assert client.propose_candidates(
        "unused-indexed-id",
        prompt_version="candidate-v2",
        media_path=video,
    ) == [
        {
            "offset_seconds": 0,
            "category": "traffic_safety",
            "event_type": "vehicle_collision",
            "description": "Two vehicles visibly collide.",
            "confidence": 0.88,
        }
    ]
    assert len(requests) == 2
    assert all(len(request["messages"]) == 1 for request in requests)
    assert all(request["messages"][0]["role"] == "user" for request in requests)
    assert (
        "prior answer did not match"
        in requests[1]["messages"][0]["content"][-1]["text"]
    )


def test_short_video_rejects_wrapped_candidate_array(
    tmp_path: Path,
) -> None:
    client = RekaVisionProvider("rk-test-only")
    video = tmp_path / "short.mp4"
    video.write_bytes(b"bounded-test-video")

    def fake_chat_request(payload: dict) -> dict:
        return {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": '{"result":[]}',
                    },
                }
            ]
        }

    client._chat_json_request = fake_chat_request  # type: ignore[method-assign]
    with pytest.raises(VideoPipelineError) as caught:
        client.propose_candidates(
            "unused-indexed-id",
            prompt_version="candidate-v2",
            media_path=video,
        )
    assert caught.value.code == "reka_output_invalid"


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_short_video_candidate_json_rejects_nonfinite_numbers(
    tmp_path: Path,
    constant: str,
) -> None:
    client = RekaVisionProvider("rk-test-only")
    video = tmp_path / "short.mp4"
    video.write_bytes(b"bounded-test-video")
    responses = iter(
        [
            (
                '[{"offset_seconds":0,"category":"traffic_safety",'
                f'"confidence":{constant}}}]'
            ),
            (
                '[{"offset_seconds":0,"category":"traffic_safety",'
                f'"confidence":{constant}}}]'
            ),
        ]
    )

    def fake_chat_request(payload: dict) -> dict:
        return {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": next(responses),
                    },
                }
            ]
        }

    client._chat_json_request = fake_chat_request  # type: ignore[method-assign]
    with pytest.raises(VideoPipelineError) as caught:
        client.propose_candidates(
            "unused-indexed-id",
            prompt_version="candidate-v2",
            media_path=video,
        )
    assert caught.value.code == "reka_output_invalid"
    assert caught.value.safe_diagnostics == {
        "format_stage": "short_video_candidate",
        "format_reason": "json_format_invalid",
    }


def test_short_video_candidate_truncation_is_repaired_then_classified(
    tmp_path: Path,
) -> None:
    client = RekaVisionProvider("rk-test-only")
    video = tmp_path / "short.mp4"
    video.write_bytes(b"bounded-test-video")
    requests: list[dict] = []

    def fake_chat_request(payload: dict) -> dict:
        requests.append(payload)
        finish_reason = "length"
        content = '[{"offset_seconds":2'
        return {
            "choices": [
                {
                    "finish_reason": finish_reason,
                    "message": {"role": "assistant", "content": content},
                }
            ]
        }

    client._chat_json_request = fake_chat_request  # type: ignore[method-assign]
    with pytest.raises(VideoPipelineError) as caught:
        client.propose_candidates(
            "unused-indexed-id",
            prompt_version="candidate-v2",
            media_path=video,
        )
    assert len(requests) == 2
    assert caught.value.code == "reka_output_truncated"
    assert caught.value.safe_diagnostics == {
        "format_stage": "short_video_candidate",
        "format_reason": "token_limit_reached",
    }


@pytest.mark.parametrize(
    ("error", "expected_state"),
    [
        (VideoPipelineError("reka_access_denied", "denied"), "failed"),
        (VideoPipelineError("reka_timeout", "timed out", retryable=True), "retry"),
        (VideoPipelineError("reka_rate_limited", "limited", retryable=True), "retry"),
    ],
)
def test_reka_failures_are_classified_without_payloads(
    tmp_path: Path, error: VideoPipelineError, expected_state: str
) -> None:
    restricted, _, store, provider, service = setup(tmp_path)
    asset = accept(service, mp4(restricted / "clip.mp4"))
    provider.operation_errors["upload"] = error
    with pytest.raises(VideoPipelineError) as caught:
        service.process_asset(TENANT_A, asset["asset_id"])
    assert caught.value.code == error.code
    assert store.job_metrics(TENANT_A)[expected_state] == 1


def test_indexing_failure_is_final(tmp_path: Path) -> None:
    restricted, _, store, provider, service = setup(tmp_path)
    asset = accept(service, mp4(restricted / "clip.mp4"))
    provider.status = "failed"
    with pytest.raises(VideoPipelineError) as caught:
        service.process_asset(TENANT_A, asset["asset_id"])
    assert caught.value.code == "reka_index_failed"
    assert store.job_metrics(TENANT_A)["failed"] == 1


def test_measured_coverage_and_retention(tmp_path: Path) -> None:
    restricted, _, store, provider, service = setup(tmp_path, retention_days=0)
    coverage = service.record_coverage(
        tenant_id=TENANT_A,
        source_id=SOURCE_A,
        interval_start="2026-01-01T00:00:00Z",
        interval_end="2026-01-01T00:10:00Z",
        connected_seconds=540,
        processable_seconds=480,
        detector_available_seconds=450,
        degraded_reason_codes=["index_latency"],
    )
    assert coverage["coverage_ratio"] == 0.75
    with pytest.raises(VideoPipelineError):
        service.record_coverage(
            tenant_id=TENANT_A,
            source_id=SOURCE_A,
            interval_start="2026-01-01T00:00:00Z",
            interval_end="2026-01-01T00:10:00Z",
            connected_seconds=400,
            processable_seconds=500,
            detector_available_seconds=300,
        )
    path = mp4(restricted / "expire.mp4")
    asset = accept(service, path, received_at="2025-01-01T00:00:00Z")
    service.process_asset(TENANT_A, asset["asset_id"])
    assert service.enforce_retention(now="2026-01-01T00:00:00Z") == [asset["asset_id"]]
    assert not path.exists()
    assert store.get_asset(TENANT_A, asset["asset_id"])["status"] == "deleted"
    assert provider.deleted


def test_remote_retention_failure_retries_before_local_delete(tmp_path: Path) -> None:
    restricted, _, store, provider, service = setup(tmp_path, retention_days=0)
    path = mp4(restricted / "expire.mp4")
    asset = accept(service, path, received_at="2025-01-01T00:00:00Z")
    service.process_asset(TENANT_A, asset["asset_id"])
    provider.operation_errors["delete"] = VideoPipelineError(
        "reka_unavailable", "temporarily unavailable", retryable=True
    )
    assert service.enforce_retention(now="2026-01-01T00:00:00Z") == []
    assert path.exists()
    assert store.job_metrics(TENANT_A)["retry"] == 1
    provider.operation_errors.clear()
    assert service.enforce_retention(now="2026-01-01T00:00:00Z") == [asset["asset_id"]]
    assert not path.exists()
