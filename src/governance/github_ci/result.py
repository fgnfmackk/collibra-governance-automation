"""Build, parse, and atomically write governance-action-result artifacts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from governance.io.atomic import atomic_write_text

RESULT_SCHEMA = "governance-action-result"
RESULT_VERSION = "1"

CONFIG_DIAGNOSTIC_SCHEMA = "governance-config-diagnostics"
POLICY_REPORT_SCHEMA = "governance-policy-report"
PLAN_SCHEMA = "governance-plan"

KNOWN_DIAGNOSTIC_SCHEMAS = frozenset(
    {
        CONFIG_DIAGNOSTIC_SCHEMA,
        "governance-policy-diagnostics",
        "governance-plan-diagnostics",
        "governance-operation-diagnostics",
        "governance-config-resolution-diagnostics",
    }
)

CONFIG_RESULT_NAME = "config-result.json"
POLICY_RESULT_NAME = "policy-result.json"
PLAN_RESULT_NAME = "plan-result.json"
ACTION_RESULT_NAME = "action-result.json"
REPORT_NAME = "report.md"
ANNOTATIONS_NAME = "annotations.txt"

CONTRACT_VERSION_VALUE = "1"


class CliContractError(Exception):
    """CLI JSON did not match an expected versioned contract."""


def canonical_json_text(payload: dict[str, Any]) -> str:
    return (
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        + "\n"
    )


def write_canonical_json(path: Path, payload: dict[str, Any]) -> Path:
    return atomic_write_text(path, canonical_json_text(payload))


def empty_validation() -> dict[str, Any]:
    return {"status": "not_run", "cli_exit_code": None, "result_path": None}


def empty_policy() -> dict[str, Any]:
    return {
        "status": "not_run",
        "cli_exit_code": None,
        "violation_count": 0,
        "error_count": 0,
        "warning_count": 0,
        "result_path": None,
    }


def empty_plan() -> dict[str, Any]:
    return {
        "status": "not_run",
        "cli_exit_code": None,
        "create_count": 0,
        "update_count": 0,
        "unchanged_count": 0,
        "remote_only_count": 0,
        "plan_path": None,
        "result_path": None,
    }


def build_action_result(
    *,
    operation: str,
    status: str,
    failure_code: str | None,
    validation: dict[str, Any] | None = None,
    policy: dict[str, Any] | None = None,
    plan: dict[str, Any] | None = None,
    consistency_status: str = "not_applicable",
) -> dict[str, Any]:
    if status == "passed":
        failure_code = None
    return {
        "consistency": {"status": consistency_status},
        "execution": {"status": "not_requested", "writes_performed": 0},
        "failure_code": failure_code,
        "operation": operation,
        "plan": plan if plan is not None else empty_plan(),
        "policy": policy if policy is not None else empty_policy(),
        "result_schema": RESULT_SCHEMA,
        "result_version": RESULT_VERSION,
        "status": status,
        "validation": validation if validation is not None else empty_validation(),
    }


def write_action_result(path: Path, action_result: dict[str, Any]) -> bytes:
    text = canonical_json_text(action_result)
    atomic_write_text(path, text)
    return text.encode("utf-8")


def parse_json_object(raw: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CliContractError("CLI stdout is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise CliContractError("CLI stdout must be a JSON object")
    return payload


def _require_version(
    payload: dict[str, Any], schema_key: str, version_key: str, schema: str
) -> None:
    if payload.get(schema_key) != schema:
        raise CliContractError(f"expected {schema_key}={schema}")
    if payload.get(version_key) != CONTRACT_VERSION_VALUE:
        raise CliContractError(f"expected {version_key}={CONTRACT_VERSION_VALUE}")


def parse_config_diagnostics(raw: str) -> dict[str, Any]:
    payload = parse_json_object(raw)
    _require_version(payload, "diagnostic_schema", "diagnostic_version", CONFIG_DIAGNOSTIC_SCHEMA)
    return payload


def parse_policy_report(raw: str) -> dict[str, Any]:
    payload = parse_json_object(raw)
    _require_version(payload, "report_schema", "report_version", POLICY_REPORT_SCHEMA)
    return payload


def parse_plan_document(raw: str) -> dict[str, Any]:
    payload = parse_json_object(raw)
    _require_version(payload, "plan_schema", "plan_version", PLAN_SCHEMA)
    return payload


def parse_known_diagnostic(raw: str) -> dict[str, Any]:
    payload = parse_json_object(raw)
    schema = payload.get("diagnostic_schema")
    if schema not in KNOWN_DIAGNOSTIC_SCHEMAS:
        raise CliContractError("unrecognized diagnostic_schema")
    if payload.get("diagnostic_version") != CONTRACT_VERSION_VALUE:
        raise CliContractError("unexpected diagnostic_version")
    return payload


def parse_cli_payload(
    raw: str,
    *,
    expect: str,
) -> dict[str, Any]:
    """Parse CLI JSON for a stage.

    ``expect`` is one of: ``config-diagnostics``, ``policy-report``, ``plan``,
    ``diagnostic-or-policy``, ``plan-or-policy-or-diagnostic``.
    """
    payload = parse_json_object(raw)
    if expect == "config-diagnostics":
        _require_version(
            payload, "diagnostic_schema", "diagnostic_version", CONFIG_DIAGNOSTIC_SCHEMA
        )
        return payload
    if expect == "policy-report":
        _require_version(payload, "report_schema", "report_version", POLICY_REPORT_SCHEMA)
        return payload
    if expect == "plan":
        _require_version(payload, "plan_schema", "plan_version", PLAN_SCHEMA)
        return payload
    if expect == "diagnostic-or-policy":
        if payload.get("report_schema") == POLICY_REPORT_SCHEMA:
            _require_version(payload, "report_schema", "report_version", POLICY_REPORT_SCHEMA)
            return payload
        schema = payload.get("diagnostic_schema")
        if schema in KNOWN_DIAGNOSTIC_SCHEMAS:
            if payload.get("diagnostic_version") != CONTRACT_VERSION_VALUE:
                raise CliContractError("unexpected diagnostic_version")
            return payload
        raise CliContractError("expected policy report or known diagnostic family")
    if expect == "plan-or-policy-or-diagnostic":
        if payload.get("plan_schema") == PLAN_SCHEMA:
            _require_version(payload, "plan_schema", "plan_version", PLAN_SCHEMA)
            return payload
        if payload.get("report_schema") == POLICY_REPORT_SCHEMA:
            _require_version(payload, "report_schema", "report_version", POLICY_REPORT_SCHEMA)
            return payload
        schema = payload.get("diagnostic_schema")
        if schema in KNOWN_DIAGNOSTIC_SCHEMAS:
            if payload.get("diagnostic_version") != CONTRACT_VERSION_VALUE:
                raise CliContractError("unexpected diagnostic_version")
            return payload
        raise CliContractError("expected plan, policy report, or known diagnostic family")
    raise CliContractError(f"unknown expect mode: {expect}")


def count_policy_violations(policy_report: dict[str, Any]) -> tuple[int, int, int]:
    violations = policy_report.get("violations")
    if not isinstance(violations, list):
        raise CliContractError("policy report missing violations list")
    error_count = 0
    warning_count = 0
    for item in violations:
        if not isinstance(item, dict):
            raise CliContractError("invalid policy violation entry")
        severity = item.get("severity")
        if severity == "error":
            error_count += 1
        elif severity == "warning":
            warning_count += 1
        else:
            raise CliContractError("invalid policy violation severity")
    return len(violations), error_count, warning_count


def count_plan_actions(plan_dict: dict[str, Any]) -> tuple[int, int, int, int]:
    actions = plan_dict.get("actions")
    if not isinstance(actions, list):
        raise CliContractError("plan missing actions list")
    create = update = unchanged = remote_only = 0
    for item in actions:
        if not isinstance(item, dict):
            raise CliContractError("invalid plan action entry")
        action_type = item.get("action_type")
        if action_type == "create":
            create += 1
        elif action_type == "update":
            update += 1
        elif action_type == "unchanged":
            unchanged += 1
        elif action_type == "remote_only":
            remote_only += 1
        else:
            raise CliContractError("invalid plan action_type")
    return create, update, unchanged, remote_only


def identities_equal(left: Any, right: Any) -> bool:
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    keys = ("algorithm", "digest", "hashing_contract_version")
    return all(left.get(key) == right.get(key) for key in keys)


def check_plan_identity_consistency(
    *,
    config_diagnostics: dict[str, Any],
    policy_report: dict[str, Any],
    plan_dict: dict[str, Any],
) -> bool:
    return (
        identities_equal(
            config_diagnostics.get("config_identity"), plan_dict.get("config_identity")
        )
        and identities_equal(policy_report.get("policy_identity"), plan_dict.get("policy_identity"))
        and identities_equal(
            policy_report.get("snapshot_identity"), plan_dict.get("snapshot_identity")
        )
    )


def action_result_outputs(action_result: dict[str, Any]) -> dict[str, str]:
    """Scalar Action outputs derived from a governance-action-result."""
    validation = action_result["validation"]
    policy = action_result["policy"]
    plan = action_result["plan"]
    return {
        "contract-version": RESULT_VERSION,
        "status": str(action_result["status"]),
        "validation-status": str(validation["status"]),
        "policy-status": str(policy["status"]),
        "policy-violation-count": str(policy["violation_count"]),
        "policy-error-count": str(policy["error_count"]),
        "policy-warning-count": str(policy["warning_count"]),
        "plan-status": str(plan["status"]),
        "create-count": str(plan["create_count"]),
        "update-count": str(plan["update_count"]),
        "unchanged-count": str(plan["unchanged_count"]),
        "remote-only-count": str(plan["remote_only_count"]),
        "writes-performed": "0",
        "plan-path": "" if plan["plan_path"] is None else str(plan["plan_path"]),
    }
