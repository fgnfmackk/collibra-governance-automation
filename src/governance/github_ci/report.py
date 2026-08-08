"""Deterministic Markdown reports and workflow annotation preparation."""

from __future__ import annotations

import html
import re
from pathlib import Path
from typing import Any

from governance.github_ci.result import ANNOTATIONS_NAME, REPORT_NAME
from governance.io.atomic import atomic_write_text

MAX_MARKDOWN_VIOLATIONS = 20
MAX_MARKDOWN_PLAN_ACTIONS = 20
MAX_DISPLAY_VALUE_CHARS = 200
MAX_ANNOTATIONS = 20

_STATUS_OVERALL = {
    "passed": "PASS",
    "blocked": "BLOCKED",
    "failed": "FAILED",
}
_STATUS_VALIDATION = {
    "not_run": "NOT RUN",
    "passed": "PASS",
    "failed": "FAILED",
}
_STATUS_POLICY = {
    "not_run": "NOT RUN",
    "passed": "PASS",
    "blocked": "BLOCKED",
    "failed": "FAILED",
}
_STATUS_PLAN = {
    "not_run": "NOT RUN",
    "generated": "GENERATED",
    "blocked": "BLOCKED",
    "failed": "FAILED",
}


def normalize_newlines(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n")


def truncate_display(value: str, *, limit: int = MAX_DISPLAY_VALUE_CHARS) -> str:
    text = normalize_newlines(value)
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    return text[: limit - 3] + "..."


def escape_markdown(value: str) -> str:
    """Escape untrusted strings for Markdown report bodies."""
    text = truncate_display(normalize_newlines(value))
    text = text.replace("`", "\\`")
    text = text.replace("|", "\\|")
    text = html.escape(text, quote=False)
    return text


def escape_workflow_command(value: str) -> str:
    """Escape values embedded in GitHub Actions workflow commands."""
    text = normalize_newlines(value)
    text = text.replace("%", "%25")
    text = text.replace("\r", "%0D")
    text = text.replace("\n", "%0A")
    return text


def _violation_line(item: dict[str, Any]) -> str:
    severity = str(item.get("severity", ""))
    policy_id = str(item.get("policy_id", ""))
    kind = str(item.get("object_kind", ""))
    object_id = str(item.get("object_id", ""))
    reason = str(item.get("reason", ""))
    name = item.get("object_name")
    name_part = f" name={name}" if name else ""
    return f"{severity} policy={policy_id} kind={kind} id={object_id}{name_part} reason={reason}"


def _plan_action_lines(plan_dict: dict[str, Any]) -> list[str]:
    actions = plan_dict.get("actions")
    if not isinstance(actions, list):
        return []
    lines: list[str] = []
    for item in actions:
        if not isinstance(item, dict):
            continue
        action_type = item.get("action_type")
        if action_type not in {"create", "update"}:
            continue
        local_id = str(item.get("local_id") or "")
        lines.append(f"{str(action_type).upper()} {local_id}".rstrip())
    return lines


def render_report(
    action_result: dict[str, Any],
    policy_report: dict[str, Any] | None = None,
    plan_dict: dict[str, Any] | None = None,
    *,
    artifacts_relative: str | None = None,
) -> str:
    validation = action_result["validation"]
    policy = action_result["policy"]
    plan = action_result["plan"]
    root = (artifacts_relative or "").rstrip("/")

    def under_root(name: str) -> str:
        return f"{root}/{name}" if root else name

    lines = [
        "# Governance as Code",
        "",
        "## Status",
        f"OVERALL: {_STATUS_OVERALL[action_result['status']]}",
        f"CONFIGURATION: {_STATUS_VALIDATION[validation['status']]}",
        f"POLICY: {_STATUS_POLICY[policy['status']]}",
        f"PLAN: {_STATUS_PLAN[plan['status']]}",
        "EXECUTION: NOT REQUESTED",
        "Writes performed: 0",
        "",
        "## Policy",
        (
            f"Violations: {policy['violation_count']} "
            f"(errors={policy['error_count']}, warnings={policy['warning_count']})"
        ),
    ]

    if policy_report is not None:
        violations = policy_report.get("violations")
        if isinstance(violations, list) and violations:
            shown = violations[:MAX_MARKDOWN_VIOLATIONS]
            for item in shown:
                if isinstance(item, dict):
                    lines.append(f"- {escape_markdown(_violation_line(item))}")
            omitted = len(violations) - len(shown)
            if omitted > 0:
                ref = policy.get("result_path") or under_root("policy-result.json")
                lines.append(f"- … omitted {omitted} more; see {escape_markdown(str(ref))}")

    lines.extend(
        [
            "",
            "## Plan",
            f"Create: {plan['create_count']}",
            f"Update: {plan['update_count']}",
            f"Unchanged: {plan['unchanged_count']}",
            f"Remote only: {plan['remote_only_count']}",
        ]
    )

    if plan_dict is not None and plan["status"] == "generated":
        action_lines = _plan_action_lines(plan_dict)
        shown_actions = action_lines[:MAX_MARKDOWN_PLAN_ACTIONS]
        for entry in shown_actions:
            lines.append(f"- {escape_markdown(entry)}")
        omitted_actions = len(action_lines) - len(shown_actions)
        if omitted_actions > 0:
            lines.append(f"- … omitted {omitted_actions} more CREATE/UPDATE actions")

    lines.extend(["", "## Artifacts"])
    action_result_path = under_root("action-result.json")
    lines.append(f"- action-result: {escape_markdown(action_result_path)}")
    if policy.get("result_path"):
        lines.append(f"- policy-result: {escape_markdown(str(policy['result_path']))}")
    if plan.get("plan_path"):
        lines.append(f"- plan: {escape_markdown(str(plan['plan_path']))}")
    elif plan.get("result_path"):
        lines.append(f"- plan: {escape_markdown(str(plan['result_path']))}")

    text = "\n".join(lines) + "\n"
    # Safety: never emit the forbidden apply wording.
    return re.sub(r"(?i)\bapplied\b", "not-requested", text)


def render_human_summary(action_result: dict[str, Any]) -> str:
    validation = action_result["validation"]
    policy = action_result["policy"]
    plan = action_result["plan"]
    lines = [
        f"OVERALL: {_STATUS_OVERALL[action_result['status']]}",
        f"CONFIGURATION: {_STATUS_VALIDATION[validation['status']]}",
        f"POLICY: {_STATUS_POLICY[policy['status']]}",
        f"PLAN: {_STATUS_PLAN[plan['status']]}",
        "Writes performed: 0",
        (
            f"Violations: {policy['violation_count']} "
            f"(errors={policy['error_count']}, warnings={policy['warning_count']})"
        ),
        (
            f"Create: {plan['create_count']} "
            f"Update: {plan['update_count']} "
            f"Unchanged: {plan['unchanged_count']} "
            f"Remote only: {plan['remote_only_count']}"
        ),
    ]
    if action_result.get("failure_code"):
        lines.append(f"Failure: {action_result['failure_code']}")
    return "\n".join(lines) + "\n"


def build_annotations(
    action_result: dict[str, Any],
    policy_report: dict[str, Any] | None = None,
) -> list[str]:
    hard: list[str] = []
    errors: list[str] = []
    warnings: list[str] = []

    if action_result["status"] == "failed":
        code = action_result.get("failure_code") or "action_contract_invalid"
        hard.append(f"::error::{escape_workflow_command(f'governance action failed: {code}')}")

    if policy_report is not None:
        violations = policy_report.get("violations")
        if isinstance(violations, list):
            for item in violations:
                if not isinstance(item, dict):
                    continue
                message = escape_workflow_command(_violation_line(item))
                if item.get("severity") == "error":
                    errors.append(f"::error::{message}")
                elif item.get("severity") == "warning":
                    warnings.append(f"::warning::{message}")

    candidates = hard + errors + warnings
    if len(candidates) <= MAX_ANNOTATIONS:
        return candidates

    kept = candidates[: MAX_ANNOTATIONS - 1]
    omitted = len(candidates) - len(kept)
    kept.append(
        "::warning::"
        + escape_workflow_command(
            f"governance annotations truncated; omitted {omitted} additional items"
        )
    )
    return kept


def write_annotations_file(output_directory: Path, annotations: list[str]) -> Path:
    target = output_directory / ANNOTATIONS_NAME
    payload = "\n".join(annotations)
    if annotations:
        payload += "\n"
    else:
        payload = ""
    return atomic_write_text(target, payload)


def write_report_file(output_directory: Path, report_text: str) -> Path:
    return atomic_write_text(output_directory / REPORT_NAME, report_text)
