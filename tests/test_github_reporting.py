"""Deterministic reporting and annotation tests for the GitHub Action."""

from __future__ import annotations

from pathlib import Path

from governance.github_ci.report import (
    MAX_ANNOTATIONS,
    build_annotations,
    escape_markdown,
    render_human_summary,
    render_report,
    write_annotations_file,
    write_report_file,
)
from governance.github_ci.result import (
    build_action_result,
    canonical_json_text,
    empty_plan,
    empty_policy,
    empty_validation,
    write_action_result,
)


def _passed_result() -> dict:
    return build_action_result(
        operation="plan",
        status="passed",
        failure_code=None,
        validation={
            "status": "passed",
            "cli_exit_code": 0,
            "result_path": ".governance/config-result.json",
        },
        policy={
            "status": "passed",
            "cli_exit_code": 0,
            "violation_count": 0,
            "error_count": 0,
            "warning_count": 0,
            "result_path": ".governance/policy-result.json",
        },
        plan={
            "status": "generated",
            "cli_exit_code": 0,
            "create_count": 1,
            "update_count": 0,
            "unchanged_count": 2,
            "remote_only_count": 0,
            "plan_path": ".governance/governance.gplan",
            "result_path": None,
        },
        consistency_status="passed",
    )


def _blocked_result() -> dict:
    return build_action_result(
        operation="check",
        status="blocked",
        failure_code="policy_blocked",
        validation={
            "status": "passed",
            "cli_exit_code": 0,
            "result_path": ".governance/config-result.json",
        },
        policy={
            "status": "blocked",
            "cli_exit_code": 3,
            "violation_count": 1,
            "error_count": 1,
            "warning_count": 0,
            "result_path": ".governance/policy-result.json",
        },
    )


def _failed_result() -> dict:
    return build_action_result(
        operation="plan",
        status="failed",
        failure_code="configuration_failed",
        validation={
            "status": "failed",
            "cli_exit_code": 1,
            "result_path": ".governance/config-result.json",
        },
    )


def _phase_a_style_result() -> dict:
    return build_action_result(
        operation="plan",
        status="failed",
        failure_code="action_contract_invalid",
        validation=empty_validation(),
        policy=empty_policy(),
        plan=empty_plan(),
    )


def _policy_report_with_reason(reason: str) -> dict:
    return {
        "report_schema": "governance-policy-report",
        "report_version": "1",
        "ok": False,
        "violations": [
            {
                "severity": "error",
                "policy_id": "tables-require-owner",
                "object_kind": "table",
                "object_id": "table:demo/db/public/t",
                "object_name": "customers<script>",
                "reason": reason,
            }
        ],
    }


def test_action_result_bytes_deterministic(tmp_path: Path) -> None:
    result = _passed_result()
    path_a = tmp_path / "a.json"
    path_b = tmp_path / "b.json"
    bytes_a = write_action_result(path_a, result)
    bytes_b = write_action_result(path_b, result)
    assert bytes_a == bytes_b
    assert bytes_a == canonical_json_text(result).encode("utf-8")
    assert path_a.read_bytes() == path_b.read_bytes()


def test_report_bytes_deterministic(tmp_path: Path) -> None:
    result = _blocked_result()
    policy = _policy_report_with_reason("missing owner")
    report_a = render_report(result, policy, None, artifacts_relative=".governance")
    report_b = render_report(result, policy, None, artifacts_relative=".governance")
    assert report_a == report_b
    assert report_a.encode("utf-8") == report_b.encode("utf-8")
    path = write_report_file(tmp_path, report_a)
    assert path.read_text(encoding="utf-8") == report_a


def test_action_result_independent_of_comment_delivery() -> None:
    result = _passed_result()
    # Comment delivery fields must never appear on the governance contract.
    assert "comment_status" not in result
    assert "comment-status" not in result
    assert "github-token" not in result
    assert result["execution"]["status"] == "not_requested"
    before = canonical_json_text(result)
    # Comment delivery outcomes must not mutate governance artifact bytes.
    _ = "created"
    after = canonical_json_text(result)
    assert before == after
    report_before = render_report(result, None, None, artifacts_relative=".governance")
    report_after = render_report(result, None, None, artifacts_relative=".governance")
    assert report_before == report_after


def test_annotation_budget_includes_truncation_notice(tmp_path: Path) -> None:
    violations = []
    for index in range(MAX_ANNOTATIONS + 5):
        violations.append(
            {
                "severity": "error",
                "policy_id": f"policy-{index}",
                "object_kind": "table",
                "object_id": f"table:{index}",
                "reason": f"violation {index}",
            }
        )
    action_result = build_action_result(
        operation="check",
        status="blocked",
        failure_code="policy_blocked",
        validation={"status": "passed", "cli_exit_code": 0, "result_path": None},
        policy={
            "status": "blocked",
            "cli_exit_code": 3,
            "violation_count": len(violations),
            "error_count": len(violations),
            "warning_count": 0,
            "result_path": ".governance/policy-result.json",
        },
    )
    policy_report = {
        "report_schema": "governance-policy-report",
        "report_version": "1",
        "ok": False,
        "violations": violations,
    }
    annotations = build_annotations(action_result, policy_report)
    assert len(annotations) == MAX_ANNOTATIONS
    assert "truncated" in annotations[-1]
    assert "omitted" in annotations[-1]
    path = write_annotations_file(tmp_path, annotations)
    body = path.read_text(encoding="utf-8")
    assert "truncated" in body
    assert body.count("\n") == MAX_ANNOTATIONS


def test_report_pass_rendering() -> None:
    text = render_report(_passed_result(), None, None, artifacts_relative=".governance")
    assert "OVERALL: PASS" in text
    assert "CONFIGURATION: PASS" in text
    assert "POLICY: PASS" in text
    assert "PLAN: GENERATED" in text
    assert "EXECUTION: NOT REQUESTED" in text
    assert "Writes performed: 0" in text
    assert "Applied" not in text


def test_report_blocked_rendering() -> None:
    policy = _policy_report_with_reason("owner missing")
    text = render_report(_blocked_result(), policy, None, artifacts_relative=".governance")
    assert "OVERALL: BLOCKED" in text
    assert "POLICY: BLOCKED" in text
    assert "tables-require-owner" in text
    assert "Applied" not in text


def test_report_failed_rendering() -> None:
    text = render_report(_failed_result(), None, None, artifacts_relative=".governance")
    assert "OVERALL: FAILED" in text
    assert "CONFIGURATION: FAILED" in text
    assert "EXECUTION: NOT REQUESTED" in text


def test_report_configuration_not_run() -> None:
    text = render_report(_phase_a_style_result(), None, None, artifacts_relative=".governance")
    assert "CONFIGURATION: NOT RUN" in text
    assert "POLICY: NOT RUN" in text
    assert "PLAN: NOT RUN" in text


def test_report_escapes_markdown_and_html_special_chars() -> None:
    reason = "bad `|` value with <script>alert(1)</script> and `code`"
    policy = _policy_report_with_reason(reason)
    text = render_report(_blocked_result(), policy, None, artifacts_relative=".governance")
    assert "<script>" not in text
    assert "&lt;script&gt;" in text
    escaped = escape_markdown(reason)
    assert "&lt;" in escaped
    assert "\\`" in escaped
    assert "\\|" in escaped
    assert escaped in text


def test_report_has_no_applied_language() -> None:
    # Even if upstream strings contain Applied, renderer must neutralize it.
    policy = _policy_report_with_reason("Applied change should not appear")
    text = render_report(_blocked_result(), policy, None, artifacts_relative=".governance")
    assert "Applied" not in text
    assert "applied" not in text
    human = render_human_summary(_passed_result())
    assert "Applied" not in human


def test_report_contains_no_secrets() -> None:
    text = render_report(
        _blocked_result(),
        _policy_report_with_reason("missing owner"),
        None,
        artifacts_relative=".governance",
    )
    assert "GITHUB_TOKEN" not in text
    assert "Authorization:" not in text
    assert "Bearer " not in text
    assert "password" not in text.lower()
    human = render_human_summary(_blocked_result())
    assert "token" not in human.lower()
    assert "password" not in human.lower()


def test_human_summary_safe_console() -> None:
    text = render_human_summary(_blocked_result())
    assert "OVERALL: BLOCKED" in text
    assert "::" not in text
    assert "Writes performed: 0" in text
