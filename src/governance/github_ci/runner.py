"""Orchestrate read-only governance CLI runs for the official GitHub Action."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, TextIO

from governance.github_ci.paths import PathValidationError, WorkspacePaths
from governance.github_ci.report import (
    build_annotations,
    render_human_summary,
    render_report,
    write_annotations_file,
    write_report_file,
)
from governance.github_ci.result import (
    ACTION_RESULT_NAME,
    CONFIG_RESULT_NAME,
    PLAN_RESULT_NAME,
    POLICY_RESULT_NAME,
    RESULT_VERSION,
    CliContractError,
    action_result_outputs,
    build_action_result,
    check_plan_identity_consistency,
    count_plan_actions,
    count_policy_violations,
    empty_plan,
    empty_policy,
    empty_validation,
    parse_cli_payload,
    write_action_result,
    write_canonical_json,
)

_SCRUB_ENV_KEYS = frozenset(
    {
        "PYTHONPATH",
        "PYTHONHOME",
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "INPUT_GITHUB_TOKEN",
    }
)

_PHASE_A_STDERR = "invalid action output directory"


def _parse_bool(raw: str, *, field: str) -> bool:
    value = raw.strip().lower()
    if value == "true":
        return True
    if value == "false":
        return False
    raise ValueError(f"{field} must be true or false")


def write_github_output(outputs: Mapping[str, str], env: Mapping[str, str] | None = None) -> None:
    """Append scalar outputs to GITHUB_OUTPUT when present."""
    source = env if env is not None else os.environ
    path_raw = source.get("GITHUB_OUTPUT", "").strip()
    if not path_raw:
        return
    path = Path(path_raw)
    lines: list[str] = []
    for key, value in outputs.items():
        if "\n" in value or "\r" in value:
            raise ValueError(f"output {key} must be a scalar without newlines")
        lines.append(f"{key}={value}")
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines))
        handle.write("\n")


def scrubbed_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    source = dict(base if base is not None else os.environ)
    for key in _SCRUB_ENV_KEYS:
        source.pop(key, None)
    # Also scrub any Action-internal comment token variable if present.
    source.pop("GOVERNANCE_COMMENT_TOKEN", None)
    return source


def build_cli_argv(
    *,
    executable: str,
    args: Sequence[str],
) -> list[str]:
    return [executable, "-I", "-m", "governance", *args]


def run_governance_cli(
    argv_tail: Sequence[str],
    *,
    workspace: Path,
    env: Mapping[str, str] | None = None,
    executable: str | None = None,
) -> subprocess.CompletedProcess[str]:
    exe = executable or sys.executable
    return subprocess.run(
        build_cli_argv(executable=exe, args=argv_tail),
        cwd=str(workspace),
        env=scrubbed_env(env),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
        check=False,
    )


def desired_exit_code(*, status: str, fail_on_policy_error: bool) -> int:
    if status == "passed":
        return 0
    if status == "blocked":
        return 3 if fail_on_policy_error else 0
    return 1


def _phase_a_outputs() -> dict[str, str]:
    return {
        "contract-version": RESULT_VERSION,
        "status": "failed",
        "validation-status": "not_run",
        "policy-status": "not_run",
        "policy-violation-count": "0",
        "policy-error-count": "0",
        "policy-warning-count": "0",
        "plan-status": "not_run",
        "create-count": "0",
        "update-count": "0",
        "unchanged-count": "0",
        "remote-only-count": "0",
        "writes-performed": "0",
        "plan-path": "",
        "result-path": "",
        "report-path": "",
        "artifacts-path": "",
        "desired-exit-code": "1",
        "phase-a-failed": "true",
        "annotations-path": "",
    }


def _append_profile(argv: list[str], profile: str) -> None:
    if profile.strip():
        argv.extend(["--profile", profile.strip()])


def _write_step_summary(report_text: str, env: Mapping[str, str]) -> None:
    summary = env.get("GITHUB_STEP_SUMMARY", "").strip()
    if not summary:
        return
    with Path(summary).open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(report_text)


def _materialize_phase_b(
    *,
    output_rel: str,
    output_abs: Path,
    action_result: dict[str, Any],
    policy_report: dict[str, Any] | None,
    plan_dict: dict[str, Any] | None,
    fail_on_policy_error: bool,
    output_format: str,
    stdout: TextIO,
) -> int:
    output_abs.mkdir(parents=True, exist_ok=True)
    action_path = output_abs / ACTION_RESULT_NAME
    result_bytes = write_action_result(action_path, action_result)
    report_text = render_report(
        action_result,
        policy_report,
        plan_dict,
        artifacts_relative=output_rel,
    )
    write_report_file(output_abs, report_text)
    annotations = build_annotations(action_result, policy_report)
    write_annotations_file(output_abs, annotations)
    _write_step_summary(report_text, os.environ)

    result_rel = f"{output_rel}/{ACTION_RESULT_NAME}"
    report_rel = f"{output_rel}/report.md"
    annotations_rel = f"{output_rel}/annotations.txt"
    plan_path_out = action_result["plan"]["plan_path"] or ""

    outputs = action_result_outputs(action_result)
    outputs.update(
        {
            "result-path": result_rel,
            "report-path": report_rel,
            "artifacts-path": output_rel,
            "plan-path": "" if not plan_path_out else str(plan_path_out),
            "desired-exit-code": str(
                desired_exit_code(
                    status=str(action_result["status"]),
                    fail_on_policy_error=fail_on_policy_error,
                )
            ),
            "phase-a-failed": "false",
            "annotations-path": annotations_rel,
        }
    )
    write_github_output(outputs)

    if output_format == "json":
        stdout.write(result_bytes.decode("utf-8"))
    else:
        stdout.write(render_human_summary(action_result))
    return 0


def _config_result_path(output_rel: str) -> str:
    return f"{output_rel}/{CONFIG_RESULT_NAME}"


def _policy_result_path(output_rel: str) -> str:
    return f"{output_rel}/{POLICY_RESULT_NAME}"


def _plan_result_path(output_rel: str) -> str:
    return f"{output_rel}/{PLAN_RESULT_NAME}"


def _run_validate(
    *,
    paths: WorkspacePaths,
    config: str,
    profile: str,
    output_abs: Path,
    output_rel: str,
) -> tuple[int, dict[str, Any] | None, dict[str, Any]]:
    argv = ["config", "validate", "--config", config, "--json"]
    _append_profile(argv, profile)
    completed = run_governance_cli(argv, workspace=paths.workspace)
    try:
        payload = parse_cli_payload(completed.stdout, expect="config-diagnostics")
    except CliContractError:
        return completed.returncode, None, empty_validation()

    write_canonical_json(output_abs / CONFIG_RESULT_NAME, payload)
    rel = _config_result_path(output_rel)
    if completed.returncode == 0 and payload.get("ok") is True:
        validation = {"status": "passed", "cli_exit_code": 0, "result_path": rel}
        return 0, payload, validation
    validation = {
        "status": "failed",
        "cli_exit_code": completed.returncode if completed.returncode != 0 else 1,
        "result_path": rel,
    }
    return validation["cli_exit_code"], payload, validation


def _run_check(
    *,
    paths: WorkspacePaths,
    config: str,
    profile: str,
    output_abs: Path,
    output_rel: str,
) -> tuple[int, dict[str, Any] | None, dict[str, Any], str | None]:
    """Return exit, policy_report|diagnostic, policy stage, failure_code hint."""
    argv = ["check", "--config", config, "--format", "json"]
    _append_profile(argv, profile)
    completed = run_governance_cli(argv, workspace=paths.workspace)
    try:
        payload = parse_cli_payload(completed.stdout, expect="diagnostic-or-policy")
    except CliContractError:
        return completed.returncode, None, empty_policy(), "action_contract_invalid"

    if payload.get("report_schema") == "governance-policy-report":
        write_canonical_json(output_abs / POLICY_RESULT_NAME, payload)
        rel = _policy_result_path(output_rel)
        total, errors, warnings = count_policy_violations(payload)
        if completed.returncode == 0:
            policy = {
                "status": "passed",
                "cli_exit_code": 0,
                "violation_count": total,
                "error_count": errors,
                "warning_count": warnings,
                "result_path": rel,
            }
            return 0, payload, policy, None
        if completed.returncode == 3:
            policy = {
                "status": "blocked",
                "cli_exit_code": 3,
                "violation_count": total,
                "error_count": errors,
                "warning_count": warnings,
                "result_path": rel,
            }
            return 3, payload, policy, "policy_blocked"
        policy = {
            "status": "failed",
            "cli_exit_code": completed.returncode,
            "violation_count": total,
            "error_count": errors,
            "warning_count": warnings,
            "result_path": rel,
        }
        code = "configuration_failed" if completed.returncode == 4 else "operational_failure"
        return completed.returncode, payload, policy, code

    # Diagnostic family
    write_canonical_json(output_abs / POLICY_RESULT_NAME, payload)
    rel = _policy_result_path(output_rel)
    policy = {
        "status": "failed",
        "cli_exit_code": completed.returncode if completed.returncode != 0 else 1,
        "violation_count": 0,
        "error_count": 0,
        "warning_count": 0,
        "result_path": rel,
    }
    if completed.returncode == 4:
        return 4, payload, policy, "configuration_failed"
    if completed.returncode == 1:
        return 1, payload, policy, "operational_failure"
    return policy["cli_exit_code"], payload, policy, "action_contract_invalid"


def _run_plan(
    *,
    paths: WorkspacePaths,
    config: str,
    profile: str,
    plan_rel: str,
    output_abs: Path,
    output_rel: str,
) -> tuple[int, dict[str, Any] | None, dict[str, Any] | None, dict[str, Any], str | None]:
    """Return exit, plan_dict, policy_report_if_exit3, plan stage, failure_code."""
    argv = ["plan", "--config", config, "--output", plan_rel, "--format", "json"]
    _append_profile(argv, profile)
    completed = run_governance_cli(argv, workspace=paths.workspace)
    try:
        payload = parse_cli_payload(completed.stdout, expect="plan-or-policy-or-diagnostic")
    except CliContractError:
        return completed.returncode, None, None, empty_plan(), "action_contract_invalid"

    if payload.get("report_schema") == "governance-policy-report":
        # Authoritative later policy report (plan exit 3).
        write_canonical_json(output_abs / POLICY_RESULT_NAME, payload)
        total, errors, warnings = count_policy_violations(payload)
        plan = {
            "status": "blocked",
            "cli_exit_code": 3,
            "create_count": 0,
            "update_count": 0,
            "unchanged_count": 0,
            "remote_only_count": 0,
            "plan_path": None,
            "result_path": None,
        }
        return 3, None, payload, plan, "policy_blocked"

    if payload.get("plan_schema") == "governance-plan":
        create, update, unchanged, remote_only = count_plan_actions(payload)
        if completed.returncode != 0:
            write_canonical_json(output_abs / PLAN_RESULT_NAME, payload)
            plan = {
                "status": "failed",
                "cli_exit_code": completed.returncode,
                "create_count": create,
                "update_count": update,
                "unchanged_count": unchanged,
                "remote_only_count": remote_only,
                "plan_path": None,
                "result_path": _plan_result_path(output_rel),
            }
            return completed.returncode, payload, None, plan, "action_contract_invalid"
        plan = {
            "status": "generated",
            "cli_exit_code": 0,
            "create_count": create,
            "update_count": update,
            "unchanged_count": unchanged,
            "remote_only_count": remote_only,
            "plan_path": plan_rel,
            "result_path": None,
        }
        return 0, payload, None, plan, None

    write_canonical_json(output_abs / PLAN_RESULT_NAME, payload)
    plan = {
        "status": "failed",
        "cli_exit_code": completed.returncode if completed.returncode != 0 else 1,
        "create_count": 0,
        "update_count": 0,
        "unchanged_count": 0,
        "remote_only_count": 0,
        "plan_path": None,
        "result_path": _plan_result_path(output_rel),
    }
    if completed.returncode == 4:
        return 4, None, None, plan, "configuration_failed"
    if completed.returncode == 1:
        return 1, None, None, plan, "operational_failure"
    return plan["cli_exit_code"], None, None, plan, "action_contract_invalid"


def run_orchestration(args: argparse.Namespace, *, stdout: TextIO | None = None) -> int:
    out = sys.stdout if stdout is None else stdout
    try:
        operation = args.operation.strip()
        if operation not in {"validate", "check", "plan"}:
            raise ValueError("operation must be validate, check, or plan")
        output_format = args.output_format.strip()
        if output_format not in {"human", "json"}:
            raise ValueError("output-format must be human or json")
        fail_on = _parse_bool(args.fail_on_policy_error, field="fail-on-policy-error")
        _parse_bool(args.pr_comment, field="pr-comment")
        paths = WorkspacePaths.from_env()
        output_rel, output_abs = paths.validate_output_directory(args.output_directory)
    except (ValueError, PathValidationError) as exc:
        message = _PHASE_A_STDERR
        if isinstance(exc, PathValidationError) and exc.code == "missing_workspace":
            message = "GITHUB_WORKSPACE is required"
        elif isinstance(exc, ValueError):
            message = str(exc) or _PHASE_A_STDERR
        print(message, file=sys.stderr)
        write_github_output(_phase_a_outputs())
        return 0

    # Phase B: safe artifact root.
    output_abs.mkdir(parents=True, exist_ok=True)

    plan_rel: str | None = None
    if operation == "plan":
        try:
            plan_rel, _plan_abs = paths.validate_plan_path(
                args.plan_path,
                output_directory_relative=output_rel,
                output_directory_absolute=output_abs,
            )
        except PathValidationError:
            action_result = build_action_result(
                operation=operation,
                status="failed",
                failure_code="action_contract_invalid",
                validation=empty_validation(),
            )
            return _materialize_phase_b(
                output_rel=output_rel,
                output_abs=output_abs,
                action_result=action_result,
                policy_report=None,
                plan_dict=None,
                fail_on_policy_error=fail_on,
                output_format=output_format,
                stdout=out,
            )

    config = args.config
    profile = args.profile or ""

    try:
        config_rel = paths.normalize_relative(config, field="config")
        paths.resolve_under_workspace(config_rel, field="config")
    except PathValidationError:
        action_result = build_action_result(
            operation=operation,
            status="failed",
            failure_code="action_contract_invalid",
            validation=empty_validation(),
        )
        return _materialize_phase_b(
            output_rel=output_rel,
            output_abs=output_abs,
            action_result=action_result,
            policy_report=None,
            plan_dict=None,
            fail_on_policy_error=fail_on,
            output_format=output_format,
            stdout=out,
        )

    # Always validate first for all operations.
    try:
        _v_code, config_payload, validation = _run_validate(
            paths=paths,
            config=config,
            profile=profile,
            output_abs=output_abs,
            output_rel=output_rel,
        )
    except CliContractError:
        action_result = build_action_result(
            operation=operation,
            status="failed",
            failure_code="action_contract_invalid",
            validation=empty_validation(),
        )
        return _materialize_phase_b(
            output_rel=output_rel,
            output_abs=output_abs,
            action_result=action_result,
            policy_report=None,
            plan_dict=None,
            fail_on_policy_error=fail_on,
            output_format=output_format,
            stdout=out,
        )

    if config_payload is None:
        action_result = build_action_result(
            operation=operation,
            status="failed",
            failure_code="action_contract_invalid",
            validation=empty_validation(),
        )
        return _materialize_phase_b(
            output_rel=output_rel,
            output_abs=output_abs,
            action_result=action_result,
            policy_report=None,
            plan_dict=None,
            fail_on_policy_error=fail_on,
            output_format=output_format,
            stdout=out,
        )

    if validation["status"] != "passed":
        action_result = build_action_result(
            operation=operation,
            status="failed",
            failure_code="configuration_failed",
            validation=validation,
        )
        return _materialize_phase_b(
            output_rel=output_rel,
            output_abs=output_abs,
            action_result=action_result,
            policy_report=None,
            plan_dict=None,
            fail_on_policy_error=fail_on,
            output_format=output_format,
            stdout=out,
        )

    if operation == "validate":
        action_result = build_action_result(
            operation=operation,
            status="passed",
            failure_code=None,
            validation=validation,
        )
        return _materialize_phase_b(
            output_rel=output_rel,
            output_abs=output_abs,
            action_result=action_result,
            policy_report=None,
            plan_dict=None,
            fail_on_policy_error=fail_on,
            output_format=output_format,
            stdout=out,
        )

    try:
        _c_code, check_payload, policy, policy_fail = _run_check(
            paths=paths,
            config=config,
            profile=profile,
            output_abs=output_abs,
            output_rel=output_rel,
        )
    except CliContractError:
        action_result = build_action_result(
            operation=operation,
            status="failed",
            failure_code="action_contract_invalid",
            validation=validation,
        )
        return _materialize_phase_b(
            output_rel=output_rel,
            output_abs=output_abs,
            action_result=action_result,
            policy_report=None,
            plan_dict=None,
            fail_on_policy_error=fail_on,
            output_format=output_format,
            stdout=out,
        )

    policy_report = (
        check_payload
        if check_payload is not None
        and check_payload.get("report_schema") == "governance-policy-report"
        else None
    )

    if policy_fail == "action_contract_invalid" or check_payload is None:
        action_result = build_action_result(
            operation=operation,
            status="failed",
            failure_code="action_contract_invalid",
            validation=validation,
            policy=policy,
        )
        return _materialize_phase_b(
            output_rel=output_rel,
            output_abs=output_abs,
            action_result=action_result,
            policy_report=policy_report,
            plan_dict=None,
            fail_on_policy_error=fail_on,
            output_format=output_format,
            stdout=out,
        )

    if policy["status"] == "blocked":
        plan_stage = empty_plan()
        if operation == "plan":
            plan_stage = {**empty_plan(), "status": "blocked"}
        action_result = build_action_result(
            operation=operation,
            status="blocked",
            failure_code="policy_blocked",
            validation=validation,
            policy=policy,
            plan=plan_stage,
        )
        return _materialize_phase_b(
            output_rel=output_rel,
            output_abs=output_abs,
            action_result=action_result,
            policy_report=policy_report,
            plan_dict=None,
            fail_on_policy_error=fail_on,
            output_format=output_format,
            stdout=out,
        )

    if policy["status"] == "failed":
        plan_stage = empty_plan()
        if operation == "plan":
            plan_stage = {**empty_plan(), "status": "failed"}
        action_result = build_action_result(
            operation=operation,
            status="failed",
            failure_code=policy_fail or "operational_failure",
            validation=validation,
            policy=policy,
            plan=plan_stage,
        )
        return _materialize_phase_b(
            output_rel=output_rel,
            output_abs=output_abs,
            action_result=action_result,
            policy_report=policy_report,
            plan_dict=None,
            fail_on_policy_error=fail_on,
            output_format=output_format,
            stdout=out,
        )

    if operation == "check":
        action_result = build_action_result(
            operation=operation,
            status="passed",
            failure_code=None,
            validation=validation,
            policy=policy,
        )
        return _materialize_phase_b(
            output_rel=output_rel,
            output_abs=output_abs,
            action_result=action_result,
            policy_report=policy_report,
            plan_dict=None,
            fail_on_policy_error=fail_on,
            output_format=output_format,
            stdout=out,
        )

    assert plan_rel is not None
    assert config_payload is not None
    assert policy_report is not None

    try:
        _p_code, plan_dict, later_policy, plan_stage, plan_fail = _run_plan(
            paths=paths,
            config=config,
            profile=profile,
            plan_rel=plan_rel,
            output_abs=output_abs,
            output_rel=output_rel,
        )
    except CliContractError:
        action_result = build_action_result(
            operation=operation,
            status="failed",
            failure_code="action_contract_invalid",
            validation=validation,
            policy=policy,
        )
        return _materialize_phase_b(
            output_rel=output_rel,
            output_abs=output_abs,
            action_result=action_result,
            policy_report=policy_report,
            plan_dict=None,
            fail_on_policy_error=fail_on,
            output_format=output_format,
            stdout=out,
        )

    if later_policy is not None:
        total, errors, warnings = count_policy_violations(later_policy)
        policy = {
            "status": "blocked",
            "cli_exit_code": 3,
            "violation_count": total,
            "error_count": errors,
            "warning_count": warnings,
            "result_path": _policy_result_path(output_rel),
        }
        action_result = build_action_result(
            operation=operation,
            status="blocked",
            failure_code="policy_blocked",
            validation=validation,
            policy=policy,
            plan=plan_stage,
            consistency_status="not_applicable",
        )
        return _materialize_phase_b(
            output_rel=output_rel,
            output_abs=output_abs,
            action_result=action_result,
            policy_report=later_policy,
            plan_dict=None,
            fail_on_policy_error=fail_on,
            output_format=output_format,
            stdout=out,
        )

    if plan_stage["status"] != "generated" or plan_dict is None:
        action_result = build_action_result(
            operation=operation,
            status="failed",
            failure_code=plan_fail or "operational_failure",
            validation=validation,
            policy=policy,
            plan=plan_stage,
        )
        return _materialize_phase_b(
            output_rel=output_rel,
            output_abs=output_abs,
            action_result=action_result,
            policy_report=policy_report,
            plan_dict=plan_dict,
            fail_on_policy_error=fail_on,
            output_format=output_format,
            stdout=out,
        )

    if not check_plan_identity_consistency(
        config_diagnostics=config_payload,
        policy_report=policy_report,
        plan_dict=plan_dict,
    ):
        plan_stage = {**plan_stage, "status": "failed", "plan_path": None}
        action_result = build_action_result(
            operation=operation,
            status="failed",
            failure_code="inputs_changed_during_run",
            validation=validation,
            policy=policy,
            plan=plan_stage,
            consistency_status="failed",
        )
        return _materialize_phase_b(
            output_rel=output_rel,
            output_abs=output_abs,
            action_result=action_result,
            policy_report=policy_report,
            plan_dict=plan_dict,
            fail_on_policy_error=fail_on,
            output_format=output_format,
            stdout=out,
        )

    action_result = build_action_result(
        operation=operation,
        status="passed",
        failure_code=None,
        validation=validation,
        policy=policy,
        plan=plan_stage,
        consistency_status="passed",
    )
    return _materialize_phase_b(
        output_rel=output_rel,
        output_abs=output_abs,
        action_result=action_result,
        policy_report=policy_report,
        plan_dict=plan_dict,
        fail_on_policy_error=fail_on,
        output_format=output_format,
        stdout=out,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="governance.github_ci")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Run governance Action orchestration")
    run.add_argument("--config", required=True)
    run.add_argument("--profile", default="")
    run.add_argument("--operation", required=True)
    run.add_argument("--output-format", required=True)
    run.add_argument("--fail-on-policy-error", required=True)
    run.add_argument("--output-directory", required=True)
    run.add_argument("--plan-path", required=True)
    run.add_argument("--pr-comment", required=True)

    sub.add_parser(
        "emit-annotations-and-comment-state",
        help="Emit annotations and compute comment eligibility",
    )
    sub.add_parser("comment", help="Publish sticky PR comment")
    sub.add_parser("finalize", help="Aggregate final Action outputs")
    sub.add_parser("fail-gate", help="Exit with final-exit-code")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.command == "run":
        return run_orchestration(args)
    if args.command == "emit-annotations-and-comment-state":
        from governance.github_ci.finalize import emit_annotations_and_comment_state

        return emit_annotations_and_comment_state()
    if args.command == "comment":
        from governance.github_ci.comment import main as comment_main

        return comment_main()
    if args.command == "finalize":
        from governance.github_ci.finalize import finalize_outputs

        return finalize_outputs()
    if args.command == "fail-gate":
        from governance.github_ci.finalize import fail_gate

        return fail_gate()
    parser.error(f"unknown command: {args.command}")
    return 2
