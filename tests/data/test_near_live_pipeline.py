from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import threading
import time

from fastapi.testclient import TestClient

from src.api import reka
from src.api.app import DEMO_HLS_LOCATION_REF, create_app
from src.api.settings import Settings
from src.api.tenancy import DEMO_TENANT_ONE
from src.data.store import IngestionStore
from src.data.video import DictLocationResolver, FakeRekaVisionProvider, VideoPipelineService, VideoStore
from src.data.video.live import CapturedSegment, DEFAULT_HLS_SOURCES, HlsSourceDefinition


class Inspector:
    def duration_seconds(self, path: Path) -> float:
        return 10.0


class FakeCapture:
    definition = HlsSourceDefinition(
        key="louisiana-dot-i20",
        name="Louisiana DOT test feed",
        url="https://example.invalid/playlist.m3u8",
        attribution="LADOTD / 511 Louisiana",
    )

    def source(self, key: str) -> HlsSourceDefinition:
        assert key == self.definition.key
        return self.definition

    def capture(self, key: str, destination: Path, *, duration_seconds: int) -> CapturedSegment:
        assert key == self.definition.key
        assert duration_seconds == 10
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"bounded-live-segment" * 8)
        return CapturedSegment(
            destination,
            datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        )


class BlockingCapture(FakeCapture):
    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()

    def capture(self, key: str, destination: Path, *, duration_seconds: int) -> CapturedSegment:
        self.entered.set()
        if not self.release.wait(timeout=2):
            raise AssertionError("test did not release the bounded capture")
        return super().capture(key, destination, duration_seconds=duration_seconds)


class FakeSimulatedCapture:
    def capture(self, destination: Path, *, duration_seconds: int) -> CapturedSegment:
        assert duration_seconds == 8
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(
            b"\x00\x00\x00\x18ftypmp42" + b"bounded-synthetic-segment" * 8
        )
        return CapturedSegment(
            destination,
            datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        )


def test_default_live_feed_uses_the_official_catalog_video_url() -> None:
    source = DEFAULT_HLS_SOURCES["louisiana-dot-i20"]
    assert source.url == (
        "https://ITSStreamingBR2.dotd.la.gov/public/"
        "shr-cam-002.streams/playlist.m3u8"
    )
    assert source.catalog_api_url == "https://511la.org/api/v2/get/cameras"
    assert source.catalog_source_id == "101"
    assert source.catalog_view_id == "2206"


def test_near_live_capture_returns_before_bounded_recording_finishes(
    tmp_path: Path,
) -> None:
    ingestion = IngestionStore(tmp_path / "restricted.sqlite3")
    video_store = VideoStore(ingestion)
    video_service = VideoPipelineService(
        video_store,
        FakeRekaVisionProvider(),
        DictLocationResolver(
            {
                (DEMO_TENANT_ONE, DEMO_HLS_LOCATION_REF): {
                    "latitude": 32.46,
                    "longitude": -93.83,
                }
            }
        ),
        media_root=tmp_path / "media",
        media_inspector=Inspector(),
    )
    capture = BlockingCapture()
    client = TestClient(
        create_app(
            provider=reka.FakeRekaProvider(),
            settings=Settings(
                app_environment="test",
                runtime_dir=tmp_path / "runtime",
                near_live_capture_seconds=10,
                reka_index_poll_seconds=0,
                reka_index_max_polls=2,
            ),
            video_service=video_service,
            hls_capture=capture,  # type: ignore[arg-type]
        )
    )
    started = client.post(
        "/v1/demo/near-live-cctv/captures",
        json={"source_key": "louisiana-dot-i20", "duration_seconds": 10},
        headers={
            "Authorization": "Bearer demo-token-one",
            "Idempotency-Key": "nonblocking-capture-0001",
        },
    )
    try:
        assert started.status_code == 202
        assert started.json()["state"] == "queued"
        assert started.json()["stage"] == "capturing_hls"
        assert "asset_id" not in started.json()
        assert capture.entered.wait(timeout=1)
        polled = client.get(
            f"/v1/ingestion/runs/{started.json()['run_id']}",
            headers={"Authorization": "Bearer demo-token-one"},
        )
        assert polled.status_code == 200
        assert polled.json()["stage"] == "capturing_hls"
    finally:
        capture.release.set()


def test_allowlisted_hls_capture_reaches_validated_human_review(tmp_path: Path) -> None:
    ingestion = IngestionStore(tmp_path / "restricted.sqlite3")
    video_store = VideoStore(ingestion)
    vision = FakeRekaVisionProvider(
        proposals=[{
            "offset_seconds": 3,
            "category": "traffic_safety",
            "event_type": "vehicle_collision",
            "description": "Two vehicles visibly collide.",
            "confidence": 0.7,
        }]
    )
    resolver = DictLocationResolver(
        {(DEMO_TENANT_ONE, DEMO_HLS_LOCATION_REF): {"latitude": 32.46, "longitude": -93.83}}
    )
    video_service = VideoPipelineService(
        video_store,
        vision,
        resolver,
        media_root=tmp_path / "media",
        media_inspector=Inspector(),
    )
    app = create_app(
        provider=reka.FakeRekaProvider(),
        settings=Settings(
            app_environment="test",
            runtime_dir=tmp_path / "runtime",
            near_live_capture_seconds=10,
            reka_index_poll_seconds=0,
            reka_index_max_polls=2,
        ),
        video_service=video_service,
        hls_capture=FakeCapture(),  # type: ignore[arg-type]
    )
    client = TestClient(app)
    assert client.get("/health").status_code == 200
    admin = {
        "Authorization": "Bearer demo-token-one",
        "Idempotency-Key": "near-live-test-0001",
    }

    started = client.post(
        "/v1/demo/near-live-cctv/captures",
        json={"source_key": "louisiana-dot-i20", "duration_seconds": 10},
        headers=admin,
    )
    assert started.status_code == 202, started.text
    for _ in range(50):
        run = client.get(
            f"/v1/ingestion/runs/{started.json()['run_id']}",
            headers={"Authorization": "Bearer demo-token-one"},
        )
        if run.json()["state"] in {"completed", "failed"}:
            break
        time.sleep(0.02)
    assert run.status_code == 200
    assert run.json()["state"] == "completed"
    assert run.json()["label"] == "near-live CCTV segment"
    assert run.json()["candidate_count"] == 1

    listing = client.get(
        "/v1/candidate-detections",
        headers={"Authorization": "Bearer demo-token-one"},
    )
    candidate = next(
        item for item in listing.json()["items"] if item.get("asset_id") == run.json()["asset_id"]
    )
    assert "evidence_ref" not in candidate
    assert candidate["record_type"] == "unconfirmed_candidate_detection"
    assert candidate["event_type"] == "vehicle_collision"
    assert candidate["description"] == "Two vehicles visibly collide."

    evidence = client.get(
        f"/v1/candidate-detections/{candidate['detection_id']}/evidence",
        headers={"Authorization": "Bearer demo-token-one"},
    )
    assert evidence.status_code == 200, evidence.text
    assert evidence.headers["content-type"] == "video/mp4"
    assert "no-store" in evidence.headers["cache-control"]
    assert evidence.content.startswith(b"\x00\x00\x00\x18ftypmp42")
    assert b"secret://" not in evidence.content

    viewer_denied = client.get(
        f"/v1/candidate-detections/{candidate['detection_id']}/evidence",
        headers={"Authorization": "Bearer demo-viewer-one"},
    )
    assert viewer_denied.status_code == 403

    reviewed = client.post(
        f"/v1/candidate-detections/{candidate['detection_id']}/review",
        json={"decision": "confirmed", "confirmed_category": "traffic_safety"},
        headers={
            "Authorization": "Bearer demo-token-one",
            "Idempotency-Key": "near-live-review-0001",
        },
    )
    assert reviewed.status_code == 201, reviewed.text
    assert ingestion.event_count(DEMO_TENANT_ONE) == 1

    denied = client.get(
        f"/v1/ingestion/runs/{started.json()['run_id']}",
        headers={"Authorization": "Bearer demo-token-two"},
    )
    assert denied.status_code == 404


def test_simulated_capture_uses_the_same_reviewable_pipeline(tmp_path: Path) -> None:
    ingestion = IngestionStore(tmp_path / "restricted.sqlite3")
    video_store = VideoStore(ingestion)
    video_service = VideoPipelineService(
        video_store,
        FakeRekaVisionProvider(),
        DictLocationResolver(
            {
                (DEMO_TENANT_ONE, DEMO_HLS_LOCATION_REF): {
                    "latitude": 32.46,
                    "longitude": -93.83,
                }
            }
        ),
        media_root=tmp_path / "media",
        media_inspector=Inspector(),
    )
    client = TestClient(
        create_app(
            provider=reka.FakeRekaProvider(),
            settings=Settings(
                app_environment="test",
                runtime_dir=tmp_path / "runtime",
                reka_index_poll_seconds=0,
                reka_index_max_polls=2,
            ),
            video_service=video_service,
            simulated_capture=FakeSimulatedCapture(),  # type: ignore[arg-type]
        )
    )
    started = client.post(
        "/v1/demo/simulated-cctv/captures",
        json={"duration_seconds": 8},
        headers={
            "Authorization": "Bearer demo-token-one",
            "Idempotency-Key": "simulated-capture-0001",
        },
    )
    assert started.status_code == 202, started.text
    assert started.json()["label"] == "simulated live segment"
    assert started.json()["source_attribution"].startswith("Generated locally")

    for _ in range(50):
        run = client.get(
            f"/v1/ingestion/runs/{started.json()['run_id']}",
            headers={"Authorization": "Bearer demo-token-one"},
        )
        if run.json()["state"] in {"completed", "failed"}:
            break
        time.sleep(0.02)
    assert run.status_code == 200
    assert run.json()["state"] == "completed"
    assert run.json()["label"] == "simulated live segment"
    assert run.json()["candidate_count"] == 0

    source_listing = client.get(
        "/v1/sources", headers={"Authorization": "Bearer demo-token-one"}
    )
    simulated = next(
        item
        for item in source_listing.json()["items"]
        if item["name"] == "Synthetic road simulation"
    )
    assert simulated["mode"] == "live_camera"
    assert "connection" not in simulated
