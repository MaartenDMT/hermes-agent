"""Validation for optional MAOS fields owned by a Hermes Kanban task.

The Kanban row remains the source of task identity, title, project, state,
dependencies, and timestamps.  This contract stores only the normalized
planning and routing fields that those native columns do not represent.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any


SCHEMA = "maos.hermes-task-contract.v1"
MAX_PAYLOAD_BYTES = 64 * 1024
MAX_TEXT_BYTES = 1_000
MAX_REFERENCE_BYTES = 500
MAX_LIST_ITEMS = 20

LANES = {
    "platform",
    "research",
    "product-engineering",
    "content-growth",
    "trading-advisory",
}
RISK_CLASSES = {"local-safe", "approval-required", "high-risk"}
SOURCE_KINDS = {
    "repository-todo",
    "workflow-goals",
    "engineering-roadmap",
    "hermes-task",
    "user-session",
}
AUTONOMY_LEVELS = {"A1", "A2"}

_TOP_LEVEL_FIELDS = {
    "schema",
    "source",
    "lane",
    "acceptanceCriteria",
    "expectedArtifacts",
    "owner",
    "riskClass",
    "approvalRequirements",
    "route",
}
_OPTIONAL_TOP_LEVEL_FIELDS = {"delegationDecision"}
_SOURCE_FIELDS = {"kind", "owner", "reference", "revision", "digest"}
_ROUTE_FIELDS = {
    "eligibleHarnesses",
    "requiredCapabilities",
    "requiredSkills",
    "autonomy",
}

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/#@+~=-]{0,255}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_SECRET_VALUE = re.compile(
    r"(?:-----BEGIN [^-]+ PRIVATE KEY-----|"
    r"(?:sk|ghp|github_pat|xox[baprs])[-_][A-Za-z0-9_-]{16,}|"
    r"\bAKIA[0-9A-Z]{16}\b|"
    r"\beyJ[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}|"
    r"\bbearer\s+[A-Za-z0-9._~+/=-]{24,})",
    re.IGNORECASE,
)

# The tool schema uses a printable-ASCII subset so JSON Schema's character
# limits also imply the byte limits enforced by the canonical validator.
# The direct Python API remains Unicode-capable and is bounded by UTF-8 bytes.
_TOOL_SECRET_PATTERN = (
    r"(?:-----[Bb][Ee][Gg][Ii][Nn] [^-]+ "
    r"[Pp][Rr][Ii][Vv][Aa][Tt][Ee] [Kk][Ee][Yy]-----|"
    r"(?:[Ss][Kk]|[Gg][Hh][Pp]|[Gg][Ii][Tt][Hh][Uu][Bb]_[Pp][Aa][Tt]|"
    r"[Xx][Oo][Xx][bBaApPrRsS])[-_][A-Za-z0-9_-]{16,}|"
    r"[Aa][Kk][Ii][Aa][0-9A-Z]{16}|"
    r"[Ee][Yy][Jj][A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\."
    r"[A-Za-z0-9_-]{16,}|"
    r"[Bb][Ee][Aa][Rr][Ee][Rr]\s+[A-Za-z0-9._~+/=-]{24,})"
)
_TOOL_TRAVERSAL_PATTERN = r"(?:^|[\\/])\.\.(?:$|[\\/])"


class TaskContractError(ValueError):
    """The supplied or stored task contract is not strict, bounded data."""


def _object(value: Any, field: str, expected: set[str], optional: set[str] | None = None) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TaskContractError(f"{field} must be an object")
    if any(not isinstance(key, str) for key in value):
        raise TaskContractError(f"{field} keys must be text")
    optional = optional or set()
    keys = set(value)
    unknown = keys - expected - optional
    missing = expected - keys
    if unknown:
        raise TaskContractError(f"{field} has unknown fields: {', '.join(sorted(unknown))}")
    if missing:
        raise TaskContractError(f"{field} is missing fields: {', '.join(sorted(missing))}")
    return value


def _text(value: Any, field: str, limit: int = MAX_TEXT_BYTES) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TaskContractError(f"{field} must be non-empty text")
    if len(value.encode("utf-8")) > limit:
        raise TaskContractError(f"{field} exceeds {limit} bytes")
    if any(ord(char) < 32 or ord(char) == 0x7F for char in value):
        raise TaskContractError(f"{field} contains control characters")
    if _SECRET_VALUE.search(value):
        raise TaskContractError(f"{field} contains a secret-shaped value")
    return value


def _identifier(value: Any, field: str) -> str:
    value = _text(value, field, 256)
    if _SAFE_ID.fullmatch(value) is None:
        raise TaskContractError(f"{field} is not a valid identifier")
    return value


def _reference(value: Any, field: str) -> str:
    value = _text(value, field, MAX_REFERENCE_BYTES)
    if any(part == ".." for part in re.split(r"[\\/]", value)):
        raise TaskContractError(f"{field} contains path traversal")
    return value


def _list(value: Any, field: str, item_validator) -> list[str]:
    if not isinstance(value, list) or len(value) > MAX_LIST_ITEMS:
        raise TaskContractError(f"{field} must be a list with at most {MAX_LIST_ITEMS} items")
    return [item_validator(item, f"{field}[{index}]") for index, item in enumerate(value)]


def _duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise TaskContractError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise TaskContractError(f"non-finite JSON number is forbidden: {value}")


def validate_task_contract(value: Any) -> dict[str, Any]:
    """Return a normalized copy of a strict MAOS Hermes task contract."""
    contract = _object(
        value,
        "work_contract",
        _TOP_LEVEL_FIELDS,
        _OPTIONAL_TOP_LEVEL_FIELDS,
    )
    if contract["schema"] != SCHEMA:
        raise TaskContractError(f"work_contract.schema must be {SCHEMA!r}")

    source = _object(contract["source"], "work_contract.source", _SOURCE_FIELDS)
    source_kind = _text(source["kind"], "work_contract.source.kind", 64)
    if source_kind not in SOURCE_KINDS:
        raise TaskContractError("work_contract.source.kind is not supported")
    digest = _text(source["digest"], "work_contract.source.digest", 71)
    if _DIGEST.fullmatch(digest) is None:
        raise TaskContractError("work_contract.source.digest must be a sha256 digest")

    lane = _text(contract["lane"], "work_contract.lane", 64)
    if lane not in LANES:
        raise TaskContractError("work_contract.lane is not supported")
    risk_class = _text(contract["riskClass"], "work_contract.riskClass", 64)
    if risk_class not in RISK_CLASSES:
        raise TaskContractError("work_contract.riskClass is not supported")

    route = _object(contract["route"], "work_contract.route", _ROUTE_FIELDS)
    autonomy = _text(route["autonomy"], "work_contract.route.autonomy", 2)
    if autonomy not in AUTONOMY_LEVELS:
        raise TaskContractError("work_contract.route.autonomy is not supported")

    normalized: dict[str, Any] = {
        "schema": SCHEMA,
        "source": {
            "kind": source_kind,
            "owner": _identifier(source["owner"], "work_contract.source.owner"),
            "reference": _reference(source["reference"], "work_contract.source.reference"),
            "revision": _identifier(source["revision"], "work_contract.source.revision"),
            "digest": digest,
        },
        "lane": lane,
        "acceptanceCriteria": _list(
            contract["acceptanceCriteria"],
            "work_contract.acceptanceCriteria",
            _text,
        ),
        "expectedArtifacts": _list(
            contract["expectedArtifacts"],
            "work_contract.expectedArtifacts",
            _reference,
        ),
        "owner": _identifier(contract["owner"], "work_contract.owner"),
        "riskClass": risk_class,
        "approvalRequirements": _list(
            contract["approvalRequirements"],
            "work_contract.approvalRequirements",
            _identifier,
        ),
        "route": {
            "eligibleHarnesses": _list(
                route["eligibleHarnesses"],
                "work_contract.route.eligibleHarnesses",
                _identifier,
            ),
            "requiredCapabilities": _list(
                route["requiredCapabilities"],
                "work_contract.route.requiredCapabilities",
                _identifier,
            ),
            "requiredSkills": _list(
                route["requiredSkills"],
                "work_contract.route.requiredSkills",
                _identifier,
            ),
            "autonomy": autonomy,
        },
    }
    if "delegationDecision" in contract:
        normalized["delegationDecision"] = _reference(
            contract["delegationDecision"],
            "work_contract.delegationDecision",
        )

    if len(encode_task_contract(normalized).encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise TaskContractError("work_contract exceeds the bounded payload size")
    return normalized


def encode_task_contract(value: Any) -> str:
    """Encode a validated contract as deterministic JSON for persistence."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def decode_task_contract(raw: str) -> dict[str, Any]:
    """Decode and revalidate a stored contract, rejecting ambiguous JSON."""
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise TaskContractError("stored work_contract is not bounded text")
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except TaskContractError:
        raise
    except (json.JSONDecodeError, TypeError, ValueError, UnicodeError) as exc:
        raise TaskContractError("stored work_contract is not strict JSON") from exc
    return validate_task_contract(value)


def task_contract_tool_schema() -> dict[str, Any]:
    """Return the strict tool-input subset accepted by the runtime validator."""

    def bounded_text(limit: int) -> dict[str, Any]:
        return {
            "type": "string",
            "minLength": 1,
            "maxLength": limit,
            "allOf": [
                {"not": {"pattern": r"[^ -~]"}},
                {"pattern": r"[!-~]"},
                {"not": {"pattern": _TOOL_SECRET_PATTERN}},
            ],
        }

    def identifier() -> dict[str, Any]:
        return {
            "type": "string",
            "minLength": 1,
            "maxLength": 256,
            "pattern": r"^[A-Za-z0-9]",
            "allOf": [
                {"not": {"pattern": r"[^A-Za-z0-9._:/#@+~=-]"}},
                {"not": {"pattern": _TOOL_SECRET_PATTERN}},
            ],
        }

    def reference() -> dict[str, Any]:
        schema = bounded_text(MAX_REFERENCE_BYTES)
        schema["allOf"].append(
            {"not": {"pattern": _TOOL_TRAVERSAL_PATTERN}}
        )
        return schema

    def string_list(item: dict[str, Any]) -> dict[str, Any]:
        return {
            "type": "array",
            "maxItems": MAX_LIST_ITEMS,
            "items": item,
        }

    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "schema": {"type": "string", "enum": [SCHEMA]},
            "source": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "kind": {"type": "string", "enum": sorted(SOURCE_KINDS)},
                    "owner": identifier(),
                    "reference": reference(),
                    "revision": identifier(),
                    "digest": {
                        "type": "string",
                        "minLength": 71,
                        "maxLength": 71,
                        "pattern": "^sha256:[0-9a-f]{64}$",
                    },
                },
                "required": sorted(_SOURCE_FIELDS),
            },
            "lane": {"type": "string", "enum": sorted(LANES)},
            "acceptanceCriteria": string_list(bounded_text(MAX_TEXT_BYTES)),
            "expectedArtifacts": string_list(reference()),
            "owner": identifier(),
            "riskClass": {"type": "string", "enum": sorted(RISK_CLASSES)},
            "approvalRequirements": string_list(identifier()),
            "route": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "eligibleHarnesses": string_list(identifier()),
                    "requiredCapabilities": string_list(identifier()),
                    "requiredSkills": string_list(identifier()),
                    "autonomy": {
                        "type": "string",
                        "enum": sorted(AUTONOMY_LEVELS),
                    },
                },
                "required": sorted(_ROUTE_FIELDS),
            },
            "delegationDecision": reference(),
        },
        "required": sorted(_TOP_LEVEL_FIELDS),
    }
