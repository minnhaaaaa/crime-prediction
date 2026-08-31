"""Recorded-video intake, Reka orchestration, review, and retention."""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import subprocess
import tempfile
import uuid
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from src.data.contracts import validate_contract
from src.data.service import _payload_hash
from src.data.store import utc_now

from .errors import VideoPipelineError
from .reka import (
    CANDIDATE_DESCRIPTION_PATTERN,
    CANDIDATE_EVENT_CATEGORIES,
    CANDIDATE_EVENT_TYPES,
    VisionProvider,
)
from .storage import LocalMediaStorage, MediaScanner, MediaStorage, NoOpMediaScanner
from .store import VideoStore

ALLOWED_CATEGORIES = {
    "property",
    "violence",
    "public_order",
    "traffic_safety",
    "other",
    "unmapped",
}
REVIEW_ROLES = {"reviewer", "tenant_admin", "platform_operator"}
NAMESPACE = uuid.UUID("e3978285-344f-40c8-b807-d44464a23ed3")


def _parse_utc(value: str, name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except (AttributeError, ValueError) as error:
        raise VideoPipelineError(
            "timestamp_invalid", f"{name} must be a valid ISO 8601 timestamp"
        ) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise VideoPipelineError(
            "timestamp_timezone_missing", f"{name} must include a timezone"
        )
    return parsed.astimezone(UTC)


def _utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class LocationResolver(Protocol):
    def resolve(self, tenant_id: str, location_ref: str) -> dict[str, float]: ...


class MediaInspector(Protocol):
    def duration_seconds(self, path: Path) -> float: ...


class FfprobeMediaInspector:
    """Probe media server-side; no client-provided duration is trusted."""

    def duration_seconds(self, path: Path) -> float:
        try:
            completed = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-protocol_whitelist",
                    "file,pipe",
                    "-show_entries",
                    "format=duration,format_name",
                    "-of",
                    "json",
                    str(path),
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
            payload = json.loads(completed.stdout)
            format_data = payload["format"]
            if "mp4" not in str(format_data["format_name"]).split(","):
                raise ValueError("not mp4")
            return float(format_data["duration"])
        except (
            OSError,
            subprocess.SubprocessError,
            KeyError,
            ValueError,
            json.JSONDecodeError,
        ) as error:
            raise VideoPipelineError(
                "video_probe_failed", "Server could not validate MP4 duration"
            ) from error


@dataclass
class DictLocationResolver:
    locations: dict[tuple[str, str], dict[str, float]]

    def resolve(self, tenant_id: str, location_ref: str) -> dict[str, float]:
        value = self.locations.get((tenant_id, location_ref))
        if value is None:
            raise VideoPipelineError(
                "location_unavailable", "Source location could not be resolved"
            )
        return dict(value)


class VideoPipelineService:
    def __init__(
        self,
        store: VideoStore,
        provider: VisionProvider,
        location_resolver: LocationResolver,
        *,
        media_root: Path,
        max_upload_bytes: int = 500 * 1024 * 1024,
        tenant_quota_bytes: int = 2 * 1024 * 1024 * 1024,
        max_duration_seconds: int = 4 * 60 * 60,
        prompt_version: str = "1.2.0",
        review_ttl: timedelta = timedelta(days=7),
        media_inspector: MediaInspector | None = None,
        media_storage: MediaStorage | None = None,
        media_scanner: MediaScanner | None = None,
    ) -> None:
        self.store = store
        self.provider = provider
        self.location_resolver = location_resolver
        self.media_root = media_root.resolve()
        self.max_upload_bytes = max_upload_bytes
        self.tenant_quota_bytes = tenant_quota_bytes
        self.max_duration_seconds = max_duration_seconds
        self.prompt_version = prompt_version
        self.review_ttl = review_ttl
        self.media_inspector = media_inspector or FfprobeMediaInspector()
        self.media_storage = media_storage or LocalMediaStorage(self.media_root)
        self.media_scanner = media_scanner or NoOpMediaScanner()

    def register_source(
        self, payload: dict[str, Any], *, authenticated_tenant_id: str
    ) -> dict[str, Any]:
        if payload.get("tenant_id") != authenticated_tenant_id:
            raise VideoPipelineError(
                "tenant_mismatch", "Source does not belong to authenticated tenant"
            )
        validate_contract("camera-source.schema.json", payload)
        transport = payload["connection"]["transport"]
        valid = (
            payload["mode"] == "recorded_video" and transport == "uploaded_asset"
        ) or (
            payload["mode"] == "live_camera" and transport in {"rtsp", "onvif", "hls"}
        )
        if not valid:
            raise VideoPipelineError(
                "source_mode_invalid", "Camera mode and transport are incompatible"
            )
        self.store.put_source(payload)
        self.store.ingestion_store.register_camera_source(payload)
        self.store.audit(
            authenticated_tenant_id,
            "source.register",
            "source",
            payload["source_id"],
            "success",
        )
        return dict(payload)

    def register_live_source(
        self, payload: dict[str, Any], *, authenticated_tenant_id: str
    ) -> dict[str, Any]:
        """Backward-compatible live-source entrypoint."""
        if payload.get("mode") != "live_camera":
            raise VideoPipelineError(
                "source_mode_invalid", "Expected a live-camera source"
            )
        return self.register_source(
            payload, authenticated_tenant_id=authenticated_tenant_id
        )

    def register_recorded_source(
        self, payload: dict[str, Any], *, authenticated_tenant_id: str
    ) -> dict[str, Any]:
        """Backward-compatible recorded-source entrypoint."""
        if payload.get("mode") != "recorded_video":
            raise VideoPipelineError(
                "source_mode_invalid", "Expected a recorded-video source"
            )
        return self.register_source(
            payload, authenticated_tenant_id=authenticated_tenant_id
        )

    def accept_upload(
        self,
        *,
        authenticated_tenant_id: str,
        source_id: str,
        path: Path,
        content_type: str,
        captured_start: str,
        captured_end: str,
        duration_seconds: float | None,
        consent_confirmed: bool,
        expected_sha256: str | None = None,
        received_at: str | None = None,
        kind: str | None = None,
    ) -> dict[str, Any]:
        source = self.store.get_source(authenticated_tenant_id, source_id)
        expected_kind = (
            "recorded_upload" if source["mode"] == "recorded_video" else "live_segment"
        )
        asset_kind = kind or expected_kind
        if asset_kind != expected_kind:
            raise VideoPipelineError(
                "asset_kind_invalid",
                "Video asset kind does not match its camera source",
            )
        if not consent_confirmed:
            raise VideoPipelineError(
                "consent_required", "Lawful-use and consent confirmation is required"
            )
        if content_type != "video/mp4" or path.suffix.lower() != ".mp4":
            raise VideoPipelineError(
                "video_type_invalid", "Only MP4 video is accepted in this phase"
            )
        resolved = path.resolve()
        if not resolved.is_file() or not resolved.is_relative_to(self.media_root):
            raise VideoPipelineError(
                "video_path_invalid",
                "Video must be inside the restricted media directory",
            )
        size = resolved.stat().st_size
        if size < 12 or size > self.max_upload_bytes:
            raise VideoPipelineError(
                "video_size_invalid", "Video size is outside configured bounds"
            )
        with resolved.open("rb") as handle:
            header = handle.read(12)
        if header[4:8] != b"ftyp":
            raise VideoPipelineError(
                "video_corrupt", "MP4 container signature is invalid"
            )
        # No media parser receives attacker-controlled bytes before the
        # production malware boundary. WebM intake applies the same rule to
        # the original container before transcoding, then reaches this scan
        # again for the generated MP4.
        self.media_scanner.scan(resolved)
        probed_duration = self.media_inspector.duration_seconds(resolved)
        if (
            not math.isfinite(probed_duration)
            or probed_duration <= 0
            or probed_duration > self.max_duration_seconds
        ):
            raise VideoPipelineError(
                "video_duration_invalid", "Video duration is outside configured bounds"
            )
        start = _parse_utc(captured_start, "captured_start")
        end = _parse_utc(captured_end, "captured_end")
        if end <= start or abs((end - start).total_seconds() - probed_duration) > 2:
            raise VideoPipelineError(
                "video_duration_mismatch",
                "Capture timestamps do not match probed duration",
            )
        if duration_seconds is not None and abs(duration_seconds - probed_duration) > 2:
            raise VideoPipelineError(
                "video_duration_mismatch",
                "Declared and probed video durations do not match",
            )
        checksum = _sha256(resolved)
        if expected_sha256 is not None and checksum != expected_sha256.lower():
            raise VideoPipelineError(
                "checksum_mismatch", "Video checksum did not match"
            )
        if (
            self.store.tenant_asset_bytes(authenticated_tenant_id) + size
            > self.tenant_quota_bytes
        ):
            raise VideoPipelineError(
                "tenant_video_quota_exceeded",
                "Tenant video storage quota would be exceeded",
            )
        received = _parse_utc(received_at or utc_now(), "received_at")
        retention_until = received + timedelta(days=source["retention_policy_days"])
        asset_id = str(
            uuid.uuid5(NAMESPACE, f"{authenticated_tenant_id}:{source_id}:{checksum}")
        )
        payload = {
            "schema_version": "1.0.0",
            "tenant_id": authenticated_tenant_id,
            "asset_id": asset_id,
            "source_id": source_id,
            "kind": asset_kind,
            "status": "ready",
            "storage_ref": f"secret://video-assets/{asset_id}",
            "content_type": content_type,
            "size_bytes": size,
            "sha256": checksum,
            "captured_start": _utc(start),
            "captured_end": _utc(end),
            "received_at": _utc(received),
            "retention_until": _utc(retention_until),
        }
        validate_contract("video-asset.schema.json", payload)
        storage_ref: str | None = None
        metadata_persisted = False
        try:
            storage_ref = self.media_storage.store(
                resolved,
                tenant_id=authenticated_tenant_id,
                asset_id=asset_id,
                sha256=checksum,
            )
            self.store.put_asset(payload, storage_ref)
            metadata_persisted = True
            self.store.audit(
                authenticated_tenant_id, "video.accept", "asset", asset_id, "success"
            )
            return payload
        except Exception:
            if storage_ref is not None and not metadata_persisted:
                with suppress(VideoPipelineError):
                    self.media_storage.delete(
                        storage_ref,
                        tenant_id=authenticated_tenant_id,
                        asset_id=asset_id,
                    )
            raise
        finally:
            if not getattr(self.media_storage, "development_only", False):
                resolved.unlink(missing_ok=True)

    def process_asset(self, tenant_id: str, asset_id: str) -> list[dict[str, Any]]:
        """Run the idempotent upload/index/analyze chain once.

        Pending indexing is recorded as a retry so a scheduler can call this
        method again without duplicating the upload or candidates.
        """
        self._run_operation(tenant_id, asset_id, "upload")
        status = self._run_operation(tenant_id, asset_id, "index")
        if status != "indexed":
            return []
        return self._run_operation(tenant_id, asset_id, "analyze")

    def _run_operation(self, tenant_id: str, asset_id: str, operation: str) -> Any:
        job = self.store.enqueue(tenant_id, asset_id, operation)
        if job["state"] == "completed":
            if operation == "analyze":
                return [
                    value
                    for value in self.store.list_candidates(tenant_id)
                    if value["asset_id"] == asset_id
                ]
            if operation == "index":
                mapping = self.store.get_mapping(tenant_id, asset_id)
                return mapping["indexing_status"] if mapping else None
            return None
        if job["state"] in {"failed", "cancelled"}:
            raise VideoPipelineError("job_not_runnable", "Processing job is final")
        job = self.store.transition_job(tenant_id, job["job_id"], "running")
        try:
            result = self._execute(tenant_id, asset_id, operation)
            if operation == "index" and result in {"pending", "indexing"}:
                self.store.transition_job(
                    tenant_id, job["job_id"], "retry", "reka_index_pending"
                )
            else:
                self.store.transition_job(tenant_id, job["job_id"], "completed")
            self.store.audit(
                tenant_id, f"reka.{operation}", "asset", asset_id, "success"
            )
            return result
        except VideoPipelineError as error:
            updated = self.store.get_job(tenant_id, job["job_id"])
            retry = error.retryable and updated["attempts"] < updated["max_attempts"]
            self.store.transition_job(
                tenant_id,
                job["job_id"],
                "retry" if retry else "failed",
                error.code,
                safe_diagnostics=error.safe_diagnostics,
            )
            if not retry and operation in {"upload", "index", "analyze"}:
                self.store.update_asset_status(
                    tenant_id, asset_id, "failed", error.code
                )
            self.store.audit(
                tenant_id, f"reka.{operation}", "asset", asset_id, "failure", error.code
            )
            raise

    def _execute(self, tenant_id: str, asset_id: str, operation: str) -> Any:
        asset = self.store.get_asset(tenant_id, asset_id)
        mapping = self.store.get_mapping(tenant_id, asset_id)
        if operation == "upload":
            if mapping:
                return None
            storage_ref = self._asset_storage_ref(tenant_id, asset_id)
            with self.media_storage.materialize(
                storage_ref, tenant_id=tenant_id, asset_id=asset_id
            ) as local_path:
                video_id = self.provider.upload(
                    local_path,
                    video_name=f"{asset_id}.mp4",
                    captured_start=asset["captured_start"],
                )
            if not isinstance(video_id, str) or not video_id:
                raise VideoPipelineError(
                    "reka_response_invalid", "Reka upload returned no video identifier"
                )
            self.store.put_mapping(
                tenant_id, asset["source_id"], asset_id, video_id, "pending"
            )
            self.store.update_asset_status(tenant_id, asset_id, "processing")
            return None
        if mapping is None:
            raise VideoPipelineError(
                "reka_mapping_missing", "Video has not been uploaded"
            )
        if operation == "index":
            status = self.provider.indexing_status(mapping["reka_video_id"])
            if status not in {"pending", "indexing", "indexed", "failed"}:
                raise VideoPipelineError(
                    "reka_response_invalid", "Reka returned an invalid indexing status"
                )
            self.store.put_mapping(
                tenant_id,
                asset["source_id"],
                asset_id,
                mapping["reka_video_id"],
                status,
            )
            if status == "failed":
                raise VideoPipelineError(
                    "reka_index_failed", "Reka video indexing failed"
                )
            return status
        if operation == "analyze":
            if mapping["indexing_status"] != "indexed":
                raise VideoPipelineError(
                    "reka_index_pending",
                    "Video indexing is not complete",
                    retryable=True,
                )
            storage_ref = self._asset_storage_ref(tenant_id, asset_id)
            duration_seconds = (
                _parse_utc(asset["captured_end"], "captured_end")
                - _parse_utc(asset["captured_start"], "captured_start")
            ).total_seconds()
            if duration_seconds <= 30:
                with self.media_storage.materialize(
                    storage_ref, tenant_id=tenant_id, asset_id=asset_id
                ) as local_path:
                    proposals = self.provider.propose_candidates(
                        mapping["reka_video_id"],
                        prompt_version=self.prompt_version,
                        media_path=local_path,
                        duration_seconds=duration_seconds,
                    )
            else:
                proposals = self.provider.propose_candidates(
                    mapping["reka_video_id"],
                    prompt_version=self.prompt_version,
                    duration_seconds=duration_seconds,
                )
            if not isinstance(proposals, list) or len(proposals) > 100:
                raise VideoPipelineError(
                    "reka_output_invalid",
                    "Reka returned malformed or excessive candidate proposals",
                )
            candidates: list[tuple[dict[str, Any], str]] = []
            out_of_range_errors: list[VideoPipelineError] = []
            for proposal_index, proposal in enumerate(proposals):
                try:
                    candidates.append(
                        self._candidate(
                            asset,
                            mapping["reka_video_id"],
                            proposal,
                            proposal_index=proposal_index,
                        )
                    )
                except VideoPipelineError as error:
                    offset = (
                        proposal.get("offset_seconds")
                        if isinstance(proposal, dict)
                        else None
                    )
                    is_out_of_range = (
                        error.code == "reka_output_invalid"
                        and error.safe_diagnostics.get("invalid_fields")
                        == ["offset_seconds"]
                        and not isinstance(offset, bool)
                        and isinstance(offset, (int, float))
                        and math.isfinite(offset)
                        and offset > duration_seconds
                    )
                    if not is_out_of_range:
                        raise
                    out_of_range_errors.append(error)
            # A malformed sibling timestamp must not erase an independently
            # valid proposal, but an entirely out-of-range response remains a
            # fail-closed provider error rather than a false clear result.
            if not candidates and out_of_range_errors:
                raise out_of_range_errors[0]
            for candidate, semantic_key in candidates:
                self.store.put_candidate(candidate, semantic_key)
            self.store.update_asset_status(tenant_id, asset_id, "processed")
            return [candidate for candidate, _ in candidates]
        if operation == "delete":
            if mapping and not mapping.get("remote_deleted_at"):
                self.provider.delete(mapping["reka_video_id"])
                self.store.mark_remote_deleted(tenant_id, asset_id)
            storage_ref = self._asset_storage_ref(tenant_id, asset_id)
            self.media_storage.delete(
                storage_ref, tenant_id=tenant_id, asset_id=asset_id
            )
            self.store.update_asset_status(tenant_id, asset_id, "deleted")
            return None
        raise ValueError("Unsupported operation")

    def execute_operation(self, tenant_id: str, asset_id: str, operation: str) -> Any:
        """Execute one already-claimed durable job operation."""
        if operation not in {"upload", "index", "analyze", "delete"}:
            raise VideoPipelineError(
                "job_operation_invalid", "Unsupported video job operation"
            )
        return self._execute(tenant_id, asset_id, operation)

    def request_reanalysis(
        self,
        tenant_id: str,
        failed_job_id: str,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Create fresh analysis work without mutating a terminal job."""
        failed_job = self.store.get_job(tenant_id, failed_job_id)
        if failed_job["operation"] != "analyze" or failed_job["state"] != "failed":
            raise VideoPipelineError(
                "reanalysis_not_allowed",
                "Only a terminal failed analysis job can be re-analyzed",
            )
        asset_id = failed_job["asset_id"]
        asset = self.store.get_asset(tenant_id, asset_id)
        if asset.get("status") == "deleted":
            raise VideoPipelineError(
                "reanalysis_not_allowed", "Deleted media cannot be re-analyzed"
            )
        mapping = self.store.get_mapping(tenant_id, asset_id)
        if mapping is None or mapping.get("indexing_status") != "indexed":
            raise VideoPipelineError(
                "reanalysis_not_allowed",
                "Re-analysis requires an indexed tenant-scoped video",
            )
        related = [
            job
            for job in self.store.list_jobs(tenant_id, limit=100)
            if job["asset_id"] == asset_id
            and job["operation"] == "analyze"
            and job["job_id"] != failed_job_id
        ]
        if any(job["state"] in {"queued", "running", "retry"} for job in related):
            raise VideoPipelineError(
                "reanalysis_in_progress", "A fresh analysis job is already active"
            )
        if any(
            job["state"] == "completed" and job["created_at"] > failed_job["created_at"]
            for job in related
        ):
            raise VideoPipelineError(
                "reanalysis_already_completed",
                "A newer analysis job has already completed",
            )
        job = self.store.enqueue(
            tenant_id,
            asset_id,
            "analyze",
            idempotency_key=f"reanalysis:{failed_job_id}:{idempotency_key}",
        )
        self.store.update_asset_status(tenant_id, asset_id, "processing")
        self.store.audit(
            tenant_id,
            "reka.analyze_requeued",
            "asset",
            asset_id,
            "success",
        )
        return job

    @contextmanager
    def candidate_evidence(
        self,
        tenant_id: str,
        asset_id: str,
        occurred_at: str,
        *,
        max_response_bytes: int = 8 * 1024 * 1024,
    ):
        """Materialize a bounded reviewer clip without exposing storage references."""
        asset = self.store.get_asset(tenant_id, asset_id)
        storage_ref = self._asset_storage_ref(tenant_id, asset_id)
        with self.media_storage.materialize(
            storage_ref, tenant_id=tenant_id, asset_id=asset_id
        ) as original:
            if original.stat().st_size <= max_response_bytes:
                yield original
                return

            directory = Path(tempfile.mkdtemp(prefix="candidate-evidence-"))
            clip = directory / "evidence.mp4"
            start = max(
                (
                    _parse_utc(occurred_at, "occurred_at")
                    - _parse_utc(asset["captured_start"], "captured_start")
                ).total_seconds()
                - 4,
                0,
            )
            try:
                completed = subprocess.run(
                    [
                        "ffmpeg",
                        "-nostdin",
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-protocol_whitelist",
                        "file,pipe",
                        "-ss",
                        str(start),
                        "-i",
                        str(original),
                        "-t",
                        "12",
                        "-map",
                        "0:v:0",
                        "-an",
                        "-vf",
                        "scale=854:-2:force_original_aspect_ratio=decrease",
                        "-c:v",
                        "libx264",
                        "-preset",
                        "veryfast",
                        "-crf",
                        "28",
                        "-maxrate",
                        "800k",
                        "-bufsize",
                        "1600k",
                        "-movflags",
                        "+faststart",
                        "-y",
                        str(clip),
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=90,
                    check=False,
                )
                if (
                    completed.returncode != 0
                    or not clip.is_file()
                    or clip.stat().st_size <= 0
                    or clip.stat().st_size > max_response_bytes
                ):
                    raise VideoPipelineError(
                        "evidence_transcode_failed",
                        "Candidate evidence could not be prepared",
                        retryable=True,
                    )
                yield clip
            except (OSError, subprocess.TimeoutExpired) as error:
                raise VideoPipelineError(
                    "evidence_transcode_failed",
                    "Candidate evidence could not be prepared",
                    retryable=True,
                ) from error
            finally:
                shutil.rmtree(directory, ignore_errors=True)

    def _asset_storage_ref(self, tenant_id: str, asset_id: str) -> str:
        getter = getattr(self.store, "asset_storage_ref", None)
        if getter is not None:
            return str(getter(tenant_id, asset_id))
        return f"file://{self.store.asset_path(tenant_id, asset_id).resolve()}"

    def _candidate(
        self,
        asset: dict[str, Any],
        remote_id: str,
        proposal: dict[str, Any],
        *,
        proposal_index: int = 0,
    ) -> tuple[dict[str, Any], str]:
        required_fields = {
            "offset_seconds",
            "category",
            "event_type",
            "description",
            "confidence",
        }
        if not isinstance(proposal, dict):
            raise VideoPipelineError(
                "reka_output_prohibited",
                "Candidate output was not an allowlisted object",
                safe_diagnostics={"proposal_index": proposal_index},
            )
        proposal_fields = set(proposal)
        unexpected_fields = proposal_fields - required_fields
        if unexpected_fields:
            raise VideoPipelineError(
                "reka_output_prohibited",
                "Candidate output contained prohibited fields",
                safe_diagnostics={
                    "proposal_index": proposal_index,
                    "unexpected_field_count": min(len(unexpected_fields), 100),
                },
            )
        missing_fields = required_fields - proposal_fields
        if missing_fields:
            raise VideoPipelineError(
                "reka_output_missing_fields",
                "Candidate output omitted required fields",
                safe_diagnostics={
                    "proposal_index": proposal_index,
                    "missing_fields": sorted(missing_fields),
                },
            )
        offset = proposal["offset_seconds"]
        category = proposal["category"]
        event_type = proposal["event_type"]
        description = proposal["description"]
        confidence = proposal["confidence"]
        if (
            isinstance(offset, bool)
            or not isinstance(offset, (int, float))
            or not math.isfinite(offset)
            or offset < 0
        ):
            raise VideoPipelineError(
                "reka_output_invalid",
                "Candidate timestamp offset is invalid",
                safe_diagnostics={
                    "proposal_index": proposal_index,
                    "invalid_fields": ["offset_seconds"],
                },
            )
        if not isinstance(category, str) or category not in ALLOWED_CATEGORIES:
            raise VideoPipelineError(
                "reka_output_invalid",
                "Candidate category is invalid",
                safe_diagnostics={
                    "proposal_index": proposal_index,
                    "invalid_fields": ["category"],
                },
            )
        if not isinstance(event_type, str) or event_type not in CANDIDATE_EVENT_TYPES:
            raise VideoPipelineError(
                "reka_output_invalid",
                "Candidate event type is invalid",
                safe_diagnostics={
                    "proposal_index": proposal_index,
                    "invalid_fields": ["event_type"],
                },
            )
        if (
            not isinstance(description, str)
            or description != description.strip()
            or CANDIDATE_DESCRIPTION_PATTERN.fullmatch(description) is None
        ):
            raise VideoPipelineError(
                "reka_output_invalid",
                "Candidate description is invalid",
                safe_diagnostics={
                    "proposal_index": proposal_index,
                    "invalid_fields": ["description"],
                },
            )
        if CANDIDATE_EVENT_CATEGORIES[event_type] != category:
            raise VideoPipelineError(
                "reka_output_invalid",
                "Candidate event type and category are inconsistent",
                safe_diagnostics={
                    "proposal_index": proposal_index,
                    "invalid_fields": ["category"],
                },
            )
        if (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not math.isfinite(confidence)
            or not 0 <= confidence <= 1
        ):
            raise VideoPipelineError(
                "reka_output_invalid",
                "Candidate confidence is invalid",
                safe_diagnostics={
                    "proposal_index": proposal_index,
                    "invalid_fields": ["confidence"],
                },
            )
        start = _parse_utc(asset["captured_start"], "captured_start")
        end = _parse_utc(asset["captured_end"], "captured_end")
        occurred = start + timedelta(seconds=float(offset))
        if occurred > end:
            raise VideoPipelineError(
                "reka_output_invalid",
                "Candidate timestamp falls outside the video",
                safe_diagnostics={
                    "proposal_index": proposal_index,
                    "invalid_fields": ["offset_seconds"],
                },
            )
        occurred_text = _utc(occurred)
        semantic_key = f"{asset['tenant_id']}:{asset['asset_id']}:{remote_id}:{self.prompt_version}:{occurred_text}:{category}:{event_type}"
        detection_id = str(uuid.uuid5(NAMESPACE, semantic_key))
        now = datetime.now(UTC)
        expires = min(
            now + self.review_ttl,
            _parse_utc(asset["retention_until"], "retention_until"),
        )
        candidate = {
            "schema_version": "1.1.0",
            "tenant_id": asset["tenant_id"],
            "detection_id": detection_id,
            "source_id": asset["source_id"],
            "asset_id": asset["asset_id"],
            "occurred_at": occurred_text,
            "received_at": _utc(now),
            "proposed_category": category,
            "event_type": event_type,
            "description": description,
            "confidence": float(confidence),
            "detector_version": f"reka-vision:{self.prompt_version}",
            "evidence_ref": f"secret://candidate-evidence/{detection_id}",
            "review_status": "awaiting_review",
            "expires_at": _utc(expires),
        }
        validate_contract("candidate-detection.schema.json", candidate)
        return candidate, semantic_key

    def review_candidate(
        self,
        *,
        authenticated_tenant_id: str,
        detection_id: str,
        decision: str,
        reviewed_by: str,
        role: str,
        confirmed_category: str | None = None,
        rejection_reason: str | None = None,
        reviewed_at: str | None = None,
    ) -> dict[str, Any]:
        if role not in REVIEW_ROLES:
            raise VideoPipelineError(
                "review_forbidden", "Role is not permitted to review candidates"
            )
        existing = self.store.get_review_for_candidate(
            authenticated_tenant_id, detection_id
        )
        if existing:
            same = existing["decision"] == decision
            if decision == "confirmed":
                same = same and existing.get("confirmed_category") == confirmed_category
            else:
                same = same and existing.get("rejection_reason") == rejection_reason
            if same:
                return existing
            raise VideoPipelineError(
                "review_already_final",
                "Candidate already has an immutable final review",
            )
        candidate = self.store.get_candidate(authenticated_tenant_id, detection_id)
        when = _parse_utc(reviewed_at or utc_now(), "reviewed_at")
        if candidate["review_status"] == "expired" or when >= _parse_utc(
            candidate["expires_at"], "expires_at"
        ):
            raise VideoPipelineError(
                "candidate_expired", "Expired candidate cannot be reviewed"
            )
        if decision not in {"confirmed", "rejected"}:
            raise VideoPipelineError(
                "review_decision_invalid", "Decision must be confirmed or rejected"
            )
        external_id = f"video-candidate:{detection_id}"
        promoted_event: dict[str, Any] | None = None
        review: dict[str, Any] = {
            "schema_version": "1.0.0",
            "tenant_id": authenticated_tenant_id,
            "review_id": str(
                uuid.uuid5(
                    NAMESPACE, f"review:{authenticated_tenant_id}:{detection_id}"
                )
            ),
            "detection_id": detection_id,
            "decision": decision,
            "reviewed_by": reviewed_by,
            "reviewed_at": _utc(when),
        }
        if decision == "confirmed":
            if confirmed_category not in ALLOWED_CATEGORIES - {"unmapped"}:
                raise VideoPipelineError(
                    "confirmed_category_invalid", "Confirmed category is invalid"
                )
            source = self.store.get_source(
                authenticated_tenant_id, candidate["source_id"]
            )
            if (
                source.get("connection", {}).get("endpoint_ref")
                == "secret://demo-simulated-road/renderer"
            ):
                raise VideoPipelineError(
                    "simulated_candidate_confirmation_prohibited",
                    "Simulated candidates cannot be promoted to incident history",
                )
            review.update(
                confirmed_category=confirmed_category,
                promoted_external_event_id=external_id,
            )
            location = self.location_resolver.resolve(
                authenticated_tenant_id, source["location_ref"]
            )
            promoted_event = {
                "schema_version": "1.0.0",
                "tenant_id": authenticated_tenant_id,
                "source_id": candidate["source_id"],
                "external_event_id": external_id,
                "occurred_at": candidate["occurred_at"],
                "received_at": _utc(when),
                "category": confirmed_category,
                "location": location,
                "attributes": {
                    "reporting_channel": "reka_vision_confirmed",
                    "source_quality": candidate["confidence"],
                },
            }
            validate_contract("incident-event.schema.json", promoted_event)
        else:
            if rejection_reason not in {
                "false_positive",
                "insufficient_evidence",
                "duplicate",
                "outside_scope",
                "other",
            }:
                raise VideoPipelineError(
                    "rejection_reason_invalid", "A valid rejection reason is required"
                )
            review["rejection_reason"] = rejection_reason
        validate_contract("candidate-review.schema.json", review)
        if promoted_event is not None:
            self.store.put_review_and_event(
                review, promoted_event, _payload_hash(promoted_event)
            )
        else:
            self.store.put_review(review)
        self.store.audit(
            authenticated_tenant_id,
            "candidate.review",
            "candidate",
            detection_id,
            "success",
        )
        return review

    def record_coverage(
        self,
        *,
        tenant_id: str,
        source_id: str,
        interval_start: str,
        interval_end: str,
        connected_seconds: int,
        processable_seconds: int,
        detector_available_seconds: int,
        degraded_reason_codes: list[str] | None = None,
    ) -> dict[str, Any]:
        self.store.get_source(tenant_id, source_id)
        start = _parse_utc(interval_start, "interval_start")
        end = _parse_utc(interval_end, "interval_end")
        expected = int((end - start).total_seconds())
        if (
            expected <= 0
            or not 0
            <= detector_available_seconds
            <= processable_seconds
            <= connected_seconds
            <= expected
        ):
            raise VideoPipelineError(
                "coverage_duration_invalid",
                "Coverage durations must be ordered within the interval",
            )
        payload = {
            "schema_version": "1.0.0",
            "tenant_id": tenant_id,
            "source_id": source_id,
            "interval_start": _utc(start),
            "interval_end": _utc(end),
            "expected_seconds": expected,
            "connected_seconds": connected_seconds,
            "processable_seconds": processable_seconds,
            "detector_available_seconds": detector_available_seconds,
            "coverage_ratio": detector_available_seconds / expected,
            "degraded_reason_codes": sorted(set(degraded_reason_codes or [])),
            "computed_at": utc_now(),
        }
        validate_contract("coverage-snapshot.schema.json", payload)
        self.store.put_coverage(payload)
        return payload

    def expire_due_candidates(self, tenant_id: str, *, now: str | None = None) -> int:
        cutoff = _parse_utc(now or utc_now(), "now")
        count = 0
        for candidate in self.store.list_candidates(tenant_id):
            if (
                candidate["review_status"] == "awaiting_review"
                and _parse_utc(candidate["expires_at"], "expires_at") <= cutoff
            ):
                candidate["review_status"] = "expired"
                validate_contract("candidate-detection.schema.json", candidate)
                self.store.update_candidate(candidate)
                count += 1
        return count

    def enforce_retention(
        self, *, tenant_id: str | None = None, now: str | None = None
    ) -> list[str]:
        deleted: list[str] = []
        cutoff = now or utc_now()
        if tenant_id is None:
            try:
                expired = self.store.expired_assets(cutoff)
            except TypeError as error:
                raise ValueError(
                    "tenant_id is required for production retention"
                ) from error
        else:
            try:
                expired = self.store.expired_assets(cutoff, tenant_id=tenant_id)
            except TypeError:
                expired = [
                    item
                    for item in self.store.expired_assets(cutoff)
                    if item[0] == tenant_id
                ]
        for asset_tenant_id, asset_id in expired:
            job = self.store.enqueue(asset_tenant_id, asset_id, "delete")
            if job["state"] == "completed":
                continue
            job = self.store.transition_job(asset_tenant_id, job["job_id"], "running")
            try:
                self.execute_operation(asset_tenant_id, asset_id, "delete")
                self.store.transition_job(asset_tenant_id, job["job_id"], "completed")
                self.store.audit(
                    asset_tenant_id, "reka.delete", "asset", asset_id, "success"
                )
                deleted.append(asset_id)
            except (VideoPipelineError, OSError) as error:
                if isinstance(error, OSError):
                    error = VideoPipelineError(
                        "local_retention_failed",
                        "Local transient deletion failed",
                        retryable=True,
                    )
                updated = self.store.get_job(asset_tenant_id, job["job_id"])
                state = (
                    "retry"
                    if error.retryable and updated["attempts"] < updated["max_attempts"]
                    else "failed"
                )
                self.store.transition_job(
                    asset_tenant_id,
                    job["job_id"],
                    state,
                    error.code,
                    safe_diagnostics=error.safe_diagnostics,
                )
                self.store.audit(
                    asset_tenant_id,
                    "reka.delete",
                    "asset",
                    asset_id,
                    "failure",
                    error.code,
                )
        return deleted
