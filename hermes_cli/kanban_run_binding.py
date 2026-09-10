"""Strict, immutable MAOS dispatch facts attached to a Hermes run.

The binding deliberately excludes native lifecycle data. Hermes continues to
own the run row's identity, claim, process, timestamps, and terminal result.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

SCHEMA = "maos.hermes-run-binding.v1"
MAX_PAYLOAD_BYTES = 16 * 1024
MAX_TEXT_BYTES = 256
MAX_REFERENCE_BYTES = 500
MAX_WORKERS = 20
MAX_APPROVAL_REFERENCES = 20
_TOP_LEVEL_FIELDS = {"schema", "sourceRevision", "parentHarness", "workers", "leaseReference", "approvalReferences"}
_OPTIONAL_TOP_LEVEL_FIELDS = {"coordinator"}
_WORKER_FIELDS = {"id", "provider", "model", "modelEvidence", "contractReference"}
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/#@+~=-]{0,255}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_SECRET_VALUE = re.compile(
    r"(?:-----BEGIN [^-]+ PRIVATE KEY-----|(?:sk|ghp|github_pat|xox[baprs])[-_][A-Za-z0-9_-]{16,}|\bAKIA[0-9A-Z]{16}\b|\beyJ[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}|\bbearer\s+[A-Za-z0-9._~+/=-]{24,})",
    re.IGNORECASE,
)
_MODEL_EVIDENCE = {"requested", "configured", "inherited", "runtime-confirmed"}


class RunBindingError(ValueError):
    """The supplied or stored MAOS run binding is not strict bounded data."""


def _object(value: Any, field: str, expected: set[str], optional: set[str] | None = None) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RunBindingError(f"{field} must be an object")
    if any(not isinstance(key, str) for key in value):
        raise RunBindingError(f"{field} keys must be text")
    optional = optional or set()
    unknown = set(value) - expected - optional
    missing = expected - set(value)
    if unknown:
        raise RunBindingError(f"{field} has unknown fields: {', '.join(sorted(unknown))}")
    if missing:
        raise RunBindingError(f"{field} is missing fields: {', '.join(sorted(missing))}")
    return value


def _text(value: Any, field: str, limit: int = MAX_TEXT_BYTES) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RunBindingError(f"{field} must be non-empty text")
    if len(value.encode("utf-8")) > limit:
        raise RunBindingError(f"{field} exceeds {limit} bytes")
    if any(ord(char) < 32 or ord(char) == 0x7F for char in value):
        raise RunBindingError(f"{field} contains control characters")
    if _SECRET_VALUE.search(value):
        raise RunBindingError(f"{field} contains a secret-shaped value")
    return value


def _identifier(value: Any, field: str) -> str:
    value = _text(value, field)
    if _SAFE_ID.fullmatch(value) is None:
        raise RunBindingError(f"{field} is not a valid identifier")
    return value


def _reference(value: Any, field: str) -> str:
    value = _text(value, field, MAX_REFERENCE_BYTES)
    if any(part == ".." for part in re.split(r"[\\/]", value)):
        raise RunBindingError(f"{field} contains path traversal")
    return value


def _duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RunBindingError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise RunBindingError(f"non-finite JSON number is forbidden: {value}")


def _encode(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def validate_run_binding(value: Any) -> dict[str, Any]:
    """Return a normalized strict MAOS dispatch binding.

    Workers are sorted by id. Approval references are sorted and duplicate
    values are rejected, keeping persisted bytes stable and reviewable.
    """
    binding = _object(value, "run_binding", _TOP_LEVEL_FIELDS, _OPTIONAL_TOP_LEVEL_FIELDS)
    if binding["schema"] != SCHEMA:
        raise RunBindingError(f"run_binding.schema must be {SCHEMA!r}")
    source_revision = _text(binding["sourceRevision"], "run_binding.sourceRevision", 71)
    if _DIGEST.fullmatch(source_revision) is None:
        raise RunBindingError("run_binding.sourceRevision must be a sha256 digest")
    workers_value = binding["workers"]
    if not isinstance(workers_value, list) or not workers_value or len(workers_value) > MAX_WORKERS:
        raise RunBindingError(f"run_binding.workers must be a non-empty list with at most {MAX_WORKERS} items")
    workers: list[dict[str, str]] = []
    for index, worker_value in enumerate(workers_value):
        worker = _object(worker_value, f"run_binding.workers[{index}]", _WORKER_FIELDS)
        evidence = _text(worker["modelEvidence"], f"run_binding.workers[{index}].modelEvidence", 32)
        if evidence not in _MODEL_EVIDENCE:
            raise RunBindingError(f"run_binding.workers[{index}].modelEvidence is not supported")
        workers.append({
            "id": _identifier(worker["id"], f"run_binding.workers[{index}].id"),
            "provider": _identifier(worker["provider"], f"run_binding.workers[{index}].provider"),
            "model": _identifier(worker["model"], f"run_binding.workers[{index}].model"),
            "modelEvidence": evidence,
            "contractReference": _reference(worker["contractReference"], f"run_binding.workers[{index}].contractReference"),
        })
    workers.sort(key=lambda worker: worker["id"])
    if len({worker["id"] for worker in workers}) != len(workers):
        raise RunBindingError("run_binding.workers has duplicate worker ids")
    approvals_value = binding["approvalReferences"]
    if not isinstance(approvals_value, list) or len(approvals_value) > MAX_APPROVAL_REFERENCES:
        raise RunBindingError(f"run_binding.approvalReferences must be a list with at most {MAX_APPROVAL_REFERENCES} items")
    approvals = [_reference(item, f"run_binding.approvalReferences[{index}]") for index, item in enumerate(approvals_value)]
    if len(set(approvals)) != len(approvals):
        raise RunBindingError("run_binding.approvalReferences has duplicate references")
    normalized: dict[str, Any] = {
        "schema": SCHEMA,
        "sourceRevision": source_revision,
        "parentHarness": _identifier(binding["parentHarness"], "run_binding.parentHarness"),
        "workers": workers,
        "leaseReference": _reference(binding["leaseReference"], "run_binding.leaseReference"),
        "approvalReferences": sorted(approvals),
    }
    if "coordinator" in binding:
        normalized["coordinator"] = _identifier(binding["coordinator"], "run_binding.coordinator")
    if len(_encode(normalized).encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise RunBindingError("run_binding exceeds the bounded payload size")
    return normalized


def encode_run_binding(value: Any) -> str:
    """Validate then encode a binding as deterministic JSON for persistence."""
    return _encode(validate_run_binding(value))


def decode_run_binding(raw: str) -> dict[str, Any]:
    """Decode and revalidate stored JSON, rejecting ambiguous encodings."""
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise RunBindingError("stored run_binding is not bounded text")
    try:
        value = json.loads(raw, object_pairs_hook=_duplicate_keys, parse_constant=_reject_constant)
    except RunBindingError:
        raise
    except (json.JSONDecodeError, TypeError, ValueError, UnicodeError) as exc:
        raise RunBindingError("stored run_binding is not strict JSON") from exc
    return validate_run_binding(value)
