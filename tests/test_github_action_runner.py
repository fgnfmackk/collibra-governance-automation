"""Unit tests for GitHub Action orchestration runner."""

from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from governance.github_ci.result import ACTION_RESULT_NAME, POLICY_RESULT_NAME, canonical_json_text
from governance.github_ci.runner import (
    build_cli_argv,
    desired_exit_code,
    run_governance_cli,
    run_orchestration,
    scrubbed_env,
)

GOVERNANCE_YAML_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "governance_yaml"


def _read_github_output(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value
    return values


def _run_args(**overrides: Any) -> argparse.Namespace:
    defaults: dict[str, Any] = {
        "config": "governance.yaml",
        "profile": "",
        "operation": "plan",
        "output_format": "human",
        "fail_on_policy_error": "true",
        "output_directory": ".governance",
        "plan_path": ".governance/governance.gplan",
        "pr_comment": "false",
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _identity(digest: str = "a" * 64) -> dict[str, str]:
    return {
        "algorithm": "sha256",
        "digest": digest,
        "hashing_contract_version": "1",
    }


def _config_diagnostics(*, ok: bool = True, digest: str = "a" * 64) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "diagnostic_schema": "governance-config-diagnostics",
        "diagnostic_version": "1",
        "ok": ok,
        "errors": [],
    }
    if ok:
        payload["config_identity"] = _identity(digest)
    else:
        payload["errors"] = [{"code": "schema_validation_failed", "message": "bad config"}]
    return payload


def _policy_report(
    *,
    ok: bool = True,
    digest: str = "a" * 64,
    violations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    items = violations if violations is not None else []
    return {
        "report_schema": "governance-policy-report",
        "report_version": "1",
        "ok": ok,
        "violations": items,
        "policy_identity": _identity(digest),
        "snapshot_identity": _identity("b" * 64),
    }


def _blocked_violation() -> dict[str, Any]:
    return {
        "severity": "error",
        "policy_id": "tables-require-owner",
        "object_kind": "table",
        "object_id": "table:demo/db/public/t",
        "object_name": "t",
        "reason": "missing owner",
    }


def _plan_document(*, digest: str = "a" * 64) -> dict[str, Any]:
    return {
        "plan_schema": "governance-plan",
        "plan_version": "1",
        "actions": [
            {"action_type": "create", "local_id": "table:demo/db/public/t"},
            {"action_type": "unchanged", "local_id": "schema:demo/db/public"},
        ],
        "config_identity": _identity(digest),
        "policy_identity": _identity(digest),
        "snapshot_identity": _identity("b" * 64),
    }


def _completed(
    stdout: dict[str, Any] | str,
    *,
    returncode: int = 0,
) -> subprocess.CompletedProcess[str]:
    text = stdout if isinstance(stdout, str) else canonical_json_text(stdout)
    return subprocess.CompletedProcess(
        args=["python", "-I", "-m", "governance"],
        returncode=returncode,
        stdout=text,
        stderr="",
    )


def _prepare_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    github_output = tmp_path / "github_output.txt"
    github_output.write_text("", encoding="utf-8")
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("GITHUB_OUTPUT", str(github_output))
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    (tmp_path / "governance.yaml").write_text("schema_version: '1'\n", encoding="utf-8")
    return github_output


def _cli_router(
    responses: list[tuple[tuple[str, ...], subprocess.CompletedProcess[str]]],
) -> Any:
    """Return a side_effect that matches argv tails and records calls."""
    calls: list[list[str]] = []

    def _side_effect(
        argv_tail: Any,
        *,
        workspace: Path,
        env: Any = None,
        executable: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        del workspace, env, executable
        tail = list(argv_tail)
        calls.append(tail)
        for prefix, response in responses:
            if tuple(tail[: len(prefix)]) == prefix:
                return response
        raise AssertionError(f"unexpected CLI argv: {tail}")

    _side_effect.calls = calls  # type: ignore[attr-defined]
    return _side_effect


def test_build_cli_argv_uses_isolated_mode() -> None:
    argv = build_cli_argv(executable=sys.executable, args=["check", "--config", "x.yaml"])
    assert argv[:4] == [sys.executable, "-I", "-m", "governance"]
    assert "apply" not in argv
    assert "sync" not in argv


def test_cli_subprocess_strips_token_and_pythonpath(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare_workspace(tmp_path, monkeypatch)
    captured: dict[str, Any] = {}

    def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured["args"] = args[0] if args else kwargs.get("args")
        captured["env"] = kwargs["env"]
        captured["cwd"] = kwargs["cwd"]
        captured["shell"] = kwargs.get("shell")
        return _completed(_config_diagnostics())

    monkeypatch.setenv("GITHUB_TOKEN", "ghs_secret_token")
    monkeypatch.setenv("GH_TOKEN", "gh_secret")
    monkeypatch.setenv("INPUT_GITHUB_TOKEN", "input_secret")
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    monkeypatch.setenv("PYTHONHOME", str(tmp_path / "pyhome"))

    with patch("governance.github_ci.runner.subprocess.run", side_effect=fake_run):
        run_governance_cli(
            ["config", "validate", "--config", "governance.yaml", "--json"],
            workspace=tmp_path,
            env=os.environ,
        )

    env = captured["env"]
    assert "GITHUB_TOKEN" not in env
    assert "GH_TOKEN" not in env
    assert "INPUT_GITHUB_TOKEN" not in env
    assert "PYTHONPATH" not in env
    assert "PYTHONHOME" not in env
    assert captured["args"][1] == "-I"
    assert captured["shell"] is False
    assert captured["cwd"] == str(tmp_path)


def test_scrubbed_env_removes_sensitive_keys() -> None:
    cleaned = scrubbed_env(
        {
            "PATH": "/usr/bin",
            "GITHUB_TOKEN": "x",
            "PYTHONPATH": "evil",
            "KEEP": "1",
        }
    )
    assert cleaned == {"PATH": "/usr/bin", "KEEP": "1"}


def test_import_shadow_workspace_package_not_executed(tmp_path: Path) -> None:
    sentinel = tmp_path / "import-shadow-sentinel.txt"
    pkg = tmp_path / "governance"
    pkg.mkdir()
    (pkg / "__init__.py").write_text(
        f"from pathlib import Path\nPath(r'{sentinel}').write_text('shadowed', encoding='utf-8')\n",
        encoding="utf-8",
    )
    (pkg / "__main__.py").write_text(
        "from pathlib import Path\n"
        f"Path(r'{sentinel}').write_text('shadowed-main', encoding='utf-8')\n",
        encoding="utf-8",
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(tmp_path)
    env["GITHUB_TOKEN"] = "should-be-stripped"
    completed = run_governance_cli(["--version"], workspace=tmp_path, env=env)
    assert completed.returncode == 0
    assert "1.0.0" in completed.stdout
    assert not sentinel.exists()


def test_plan_path_must_be_under_output_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    github_output = _prepare_workspace(tmp_path, monkeypatch)
    stdout = io.StringIO()
    code = run_orchestration(
        _run_args(
            operation="plan",
            output_directory=".governance",
            plan_path="elsewhere/governance.gplan",
            output_format="json",
        ),
        stdout=stdout,
    )
    assert code == 0
    result_path = tmp_path / ".governance" / ACTION_RESULT_NAME
    assert result_path.is_file()
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    assert payload["status"] == "failed"
    assert payload["failure_code"] == "action_contract_invalid"
    assert payload["validation"]["status"] == "not_run"
    outputs = _read_github_output(github_output)
    assert outputs["validation-status"] == "not_run"
    assert outputs["status"] == "failed"


def test_valid_root_invalid_plan_path_validation_not_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare_workspace(tmp_path, monkeypatch)
    stdout = io.StringIO()
    code = run_orchestration(
        _run_args(
            operation="plan",
            output_directory=".governance",
            plan_path="../escape.gplan",
            output_format="json",
        ),
        stdout=stdout,
    )
    assert code == 0
    payload = json.loads(
        (tmp_path / ".governance" / ACTION_RESULT_NAME).read_text(encoding="utf-8")
    )
    assert payload["validation"]["status"] == "not_run"
    assert payload["failure_code"] == "action_contract_invalid"


def test_invalid_output_directory_writes_nothing_empty_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    github_output = _prepare_workspace(tmp_path, monkeypatch)
    before = {p.relative_to(tmp_path) for p in tmp_path.rglob("*") if p.is_file()}
    stdout = io.StringIO()
    code = run_orchestration(
        _run_args(output_directory="../outside", operation="validate"),
        stdout=stdout,
    )
    assert code == 0
    after = {p.relative_to(tmp_path) for p in tmp_path.rglob("*") if p.is_file()}
    assert after == before
    assert not (tmp_path / "outside").exists()
    outputs = _read_github_output(github_output)
    assert outputs["phase-a-failed"] == "true"
    assert outputs["result-path"] == ""
    assert outputs["report-path"] == ""
    assert outputs["artifacts-path"] == ""
    assert outputs["plan-path"] == ""
    assert outputs["writes-performed"] == "0"


def test_invalid_output_directory_validation_status_not_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    github_output = _prepare_workspace(tmp_path, monkeypatch)
    code = run_orchestration(
        _run_args(output_directory="/absolute/out", operation="check"),
        stdout=io.StringIO(),
    )
    assert code == 0
    outputs = _read_github_output(github_output)
    assert outputs["validation-status"] == "not_run"
    assert outputs["policy-status"] == "not_run"
    assert outputs["plan-status"] == "not_run"
    assert outputs["status"] == "failed"
    assert outputs["phase-a-failed"] == "true"


def test_malformed_config_validation_status_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    github_output = _prepare_workspace(tmp_path, monkeypatch)
    config_name = "bad-config.yaml"
    (tmp_path / config_name).write_text(
        (GOVERNANCE_YAML_FIXTURES / "invalid_schema_version.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    code = run_orchestration(
        _run_args(
            config=config_name,
            operation="validate",
            output_format="json",
        ),
        stdout=io.StringIO(),
    )
    assert code == 0
    payload = json.loads(
        (tmp_path / ".governance" / ACTION_RESULT_NAME).read_text(encoding="utf-8")
    )
    assert payload["status"] == "failed"
    assert payload["failure_code"] == "configuration_failed"
    assert payload["validation"]["status"] == "failed"
    outputs = _read_github_output(github_output)
    assert outputs["validation-status"] == "failed"
    assert outputs["writes-performed"] == "0"


def test_plan_exit_3_replaces_authoritative_policy_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    github_output = _prepare_workspace(tmp_path, monkeypatch)
    early = _policy_report(ok=True, violations=[])
    later = _policy_report(ok=False, violations=[_blocked_violation()], digest="c" * 64)
    router = _cli_router(
        [
            (("config", "validate"), _completed(_config_diagnostics())),
            (("check",), _completed(early, returncode=0)),
            (("plan",), _completed(later, returncode=3)),
        ]
    )
    with patch("governance.github_ci.runner.run_governance_cli", side_effect=router):
        code = run_orchestration(
            _run_args(operation="plan", output_format="json", fail_on_policy_error="true"),
            stdout=io.StringIO(),
        )
    assert code == 0
    policy_path = tmp_path / ".governance" / POLICY_RESULT_NAME
    assert json.loads(policy_path.read_text(encoding="utf-8")) == later
    result = json.loads((tmp_path / ".governance" / ACTION_RESULT_NAME).read_text(encoding="utf-8"))
    assert result["status"] == "blocked"
    assert result["failure_code"] == "policy_blocked"
    assert result["policy"]["status"] == "blocked"
    assert result["policy"]["result_path"] == ".governance/policy-result.json"
    assert result["plan"]["status"] == "blocked"
    outputs = _read_github_output(github_output)
    assert outputs["desired-exit-code"] == "3"
    assert outputs["policy-status"] == "blocked"


def test_blocked_with_fail_on_still_materializes_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    github_output = _prepare_workspace(tmp_path, monkeypatch)
    blocked = _policy_report(ok=False, violations=[_blocked_violation()])
    router = _cli_router(
        [
            (("config", "validate"), _completed(_config_diagnostics())),
            (("check",), _completed(blocked, returncode=3)),
        ]
    )
    stdout = io.StringIO()
    with patch("governance.github_ci.runner.run_governance_cli", side_effect=router):
        code = run_orchestration(
            _run_args(
                operation="check",
                output_format="json",
                fail_on_policy_error="true",
            ),
            stdout=stdout,
        )
    assert code == 0
    out_dir = tmp_path / ".governance"
    assert (out_dir / ACTION_RESULT_NAME).is_file()
    assert (out_dir / "report.md").is_file()
    assert (out_dir / "annotations.txt").is_file()
    assert (out_dir / POLICY_RESULT_NAME).is_file()
    outputs = _read_github_output(github_output)
    assert outputs["status"] == "blocked"
    assert outputs["desired-exit-code"] == "3"
    assert outputs["writes-performed"] == "0"
    assert outputs["phase-a-failed"] == "false"
    result = json.loads((out_dir / ACTION_RESULT_NAME).read_text(encoding="utf-8"))
    assert result["execution"]["writes_performed"] == 0
    assert "apply" not in " ".join(" ".join(call) for call in router.calls)


def test_fail_on_false_desired_exit_zero_when_blocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    github_output = _prepare_workspace(tmp_path, monkeypatch)
    blocked = _policy_report(ok=False, violations=[_blocked_violation()])
    router = _cli_router(
        [
            (("config", "validate"), _completed(_config_diagnostics())),
            (("check",), _completed(blocked, returncode=3)),
        ]
    )
    with patch("governance.github_ci.runner.run_governance_cli", side_effect=router):
        code = run_orchestration(
            _run_args(
                operation="check",
                fail_on_policy_error="false",
                output_format="json",
            ),
            stdout=io.StringIO(),
        )
    assert code == 0
    outputs = _read_github_output(github_output)
    assert outputs["status"] == "blocked"
    assert outputs["desired-exit-code"] == "0"
    assert desired_exit_code(status="blocked", fail_on_policy_error=False) == 0


def test_no_apply_or_sync_in_cli_argv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare_workspace(tmp_path, monkeypatch)
    plan = _plan_document()
    policy = _policy_report(ok=True, violations=[])
    router = _cli_router(
        [
            (("config", "validate"), _completed(_config_diagnostics())),
            (("check",), _completed(policy, returncode=0)),
            (("plan",), _completed(plan, returncode=0)),
        ]
    )
    with patch("governance.github_ci.runner.run_governance_cli", side_effect=router):
        code = run_orchestration(
            _run_args(operation="plan", output_format="json"),
            stdout=io.StringIO(),
        )
    assert code == 0
    joined = [" ".join(call) for call in router.calls]
    assert all("apply" not in call for call in joined)
    assert all("sync" not in call for call in joined)
    result = json.loads((tmp_path / ".governance" / ACTION_RESULT_NAME).read_text(encoding="utf-8"))
    assert result["execution"]["writes_performed"] == 0
    assert result["status"] == "passed"
    assert result["plan"]["status"] == "generated"


def test_output_format_json_stdout_equals_action_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare_workspace(tmp_path, monkeypatch)
    router = _cli_router(
        [
            (("config", "validate"), _completed(_config_diagnostics())),
        ]
    )
    stdout = io.StringIO()
    with patch("governance.github_ci.runner.run_governance_cli", side_effect=router):
        code = run_orchestration(
            _run_args(operation="validate", output_format="json"),
            stdout=stdout,
        )
    assert code == 0
    action_bytes = (tmp_path / ".governance" / ACTION_RESULT_NAME).read_bytes()
    assert stdout.getvalue().encode("utf-8") == action_bytes


def test_json_stdout_contains_no_workflow_annotation_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare_workspace(tmp_path, monkeypatch)
    blocked = _policy_report(ok=False, violations=[_blocked_violation()])
    router = _cli_router(
        [
            (("config", "validate"), _completed(_config_diagnostics())),
            (("check",), _completed(blocked, returncode=3)),
        ]
    )
    stdout = io.StringIO()
    with patch("governance.github_ci.runner.run_governance_cli", side_effect=router):
        run_orchestration(
            _run_args(operation="check", output_format="json"),
            stdout=stdout,
        )
    text = stdout.getvalue()
    assert "::" not in text
    assert "::error::" not in text
    assert "::warning::" not in text


def test_annotations_txt_prepared_separately_from_runner_stdout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare_workspace(tmp_path, monkeypatch)
    blocked = _policy_report(ok=False, violations=[_blocked_violation()])
    router = _cli_router(
        [
            (("config", "validate"), _completed(_config_diagnostics())),
            (("check",), _completed(blocked, returncode=3)),
        ]
    )
    stdout = io.StringIO()
    with patch("governance.github_ci.runner.run_governance_cli", side_effect=router):
        run_orchestration(
            _run_args(operation="check", output_format="human"),
            stdout=stdout,
        )
    annotations = (tmp_path / ".governance" / "annotations.txt").read_text(encoding="utf-8")
    assert "::error::" in annotations
    assert "::error::" not in stdout.getvalue()
    assert "OVERALL: BLOCKED" in stdout.getvalue()


def test_nested_plan_path_under_output_directory_ok(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare_workspace(tmp_path, monkeypatch)
    plan = _plan_document()
    policy = _policy_report(ok=True, violations=[])
    router = _cli_router(
        [
            (("config", "validate"), _completed(_config_diagnostics())),
            (("check",), _completed(policy, returncode=0)),
            (("plan",), _completed(plan, returncode=0)),
        ]
    )
    with patch("governance.github_ci.runner.run_governance_cli", side_effect=router):
        code = run_orchestration(
            _run_args(
                operation="plan",
                output_directory=".governance",
                plan_path=".governance/nested/plan.gplan",
                output_format="json",
            ),
            stdout=io.StringIO(),
        )
    assert code == 0
    result = json.loads((tmp_path / ".governance" / ACTION_RESULT_NAME).read_text(encoding="utf-8"))
    assert result["status"] == "passed"
    assert result["plan"]["plan_path"] == ".governance/nested/plan.gplan"
    plan_calls = [call for call in router.calls if call and call[0] == "plan"]
    assert any(
        "--output" in call and ".governance/nested/plan.gplan" in call for call in plan_calls
    )


def test_identity_mismatch_fails_without_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare_workspace(tmp_path, monkeypatch)
    plan = _plan_document(digest="d" * 64)  # mismatched config/policy digests
    policy = _policy_report(ok=True, violations=[])
    router = _cli_router(
        [
            (("config", "validate"), _completed(_config_diagnostics())),
            (("check",), _completed(policy, returncode=0)),
            (("plan",), _completed(plan, returncode=0)),
        ]
    )
    with patch("governance.github_ci.runner.run_governance_cli", side_effect=router):
        code = run_orchestration(
            _run_args(operation="plan", output_format="json"),
            stdout=io.StringIO(),
        )
    assert code == 0
    result = json.loads((tmp_path / ".governance" / ACTION_RESULT_NAME).read_text(encoding="utf-8"))
    assert result["status"] == "failed"
    assert result["failure_code"] == "inputs_changed_during_run"
    assert result["consistency"]["status"] == "failed"
    assert sum(1 for call in router.calls if call and call[0] == "plan") == 1
