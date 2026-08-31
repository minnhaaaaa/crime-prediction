"""Safe, classified errors for the restricted video worker boundary."""

from __future__ import annotations

from typing import Any

_CANDIDATE_FIELDS = frozenset(
    {"offset_seconds", "category", "event_type", "description", "confidence"}
)
_FORMAT_STAGES = frozenset(
    {
        "indexed_video_candidate",
        "short_video_candidate",
        # Retained so persisted pre-1.1.0 job/DLQ diagnostics remain readable.
        "short_video_screen",
    }
)
_FORMAT_REASONS = frozenset(
    {
        "content_shape_invalid",
        "json_format_invalid",
        "response_shape_invalid",
        # Retained so persisted pre-1.1.0 job/DLQ diagnostics remain readable.
        "token_format_invalid",
        "token_limit_reached",
    }
)


def _validated_safe_diagnostics(value: dict[str, Any] | None) -> dict[str, Any]:
    """Accept only bounded, value-free diagnostics at the worker boundary."""
    if value is None:
        return {}
    if set(value) - {
        "proposal_index",
        "missing_fields",
        "invalid_fields",
        "unexpected_field_count",
        "format_stage",
        "format_reason",
    }:
        raise ValueError("Unsupported safe diagnostic field")
    result: dict[str, Any] = {}
    if ("format_stage" in value) != ("format_reason" in value):
        raise ValueError("format_stage and format_reason must be provided together")
    if "proposal_index" in value:
        proposal_index = value["proposal_index"]
        if isinstance(proposal_index, bool) or not isinstance(proposal_index, int):
            raise ValueError("proposal_index must be an integer")
        if not 0 <= proposal_index <= 99:
            raise ValueError("proposal_index is outside the bounded proposal list")
        result["proposal_index"] = proposal_index
    if "missing_fields" in value:
        missing_fields = value["missing_fields"]
        if not isinstance(missing_fields, list) or any(
            not isinstance(field, str) or field not in _CANDIDATE_FIELDS
            for field in missing_fields
        ):
            raise ValueError("missing_fields must contain only allowlisted field names")
        result["missing_fields"] = sorted(set(missing_fields))
    if "invalid_fields" in value:
        invalid_fields = value["invalid_fields"]
        if not isinstance(invalid_fields, list) or any(
            not isinstance(field, str) or field not in _CANDIDATE_FIELDS
            for field in invalid_fields
        ):
            raise ValueError("invalid_fields must contain only allowlisted field names")
        result["invalid_fields"] = sorted(set(invalid_fields))
    if "unexpected_field_count" in value:
        count = value["unexpected_field_count"]
        if (
            isinstance(count, bool)
            or not isinstance(count, int)
            or not 0 <= count <= 100
        ):
            raise ValueError("unexpected_field_count must be a bounded integer")
        result["unexpected_field_count"] = count
    if "format_stage" in value:
        stage = value["format_stage"]
        if not isinstance(stage, str) or stage not in _FORMAT_STAGES:
            raise ValueError("format_stage must be an allowlisted stage")
        result["format_stage"] = stage
    if "format_reason" in value:
        reason = value["format_reason"]
        if not isinstance(reason, str) or reason not in _FORMAT_REASONS:
            raise ValueError("format_reason must be an allowlisted reason")
        result["format_reason"] = reason
    return result


class VideoPipelineError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        safe_diagnostics: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.safe_diagnostics = _validated_safe_diagnostics(safe_diagnostics)
