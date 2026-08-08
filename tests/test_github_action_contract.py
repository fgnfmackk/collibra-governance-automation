"""Contract tests for the official Governance-as-Code GitHub Action."""

from __future__ import annotations

import json
from importlib.resources import files
from pathlib import Path

import yaml

from governance.github_ci.finalize import _PUBLIC_COMMENT_STATUSES

REPO_ROOT = Path(__file__).resolve().parents[1]
ACTION_YML = REPO_ROOT / "action.yml"
SCHEMA_NAME = "governance-action-result.v1.schema.json"
EXPECTED_SETUP_PYTHON = "actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065 # v5.6.0"

EXPECTED_INPUTS = {
    "config": {"required": True},
    "profile": {"required": False, "default": ""},
    "operation": {"required": False, "default": "plan"},
    "output-format": {"required": False, "default": "human"},
    "fail-on-policy-error": {"required": False, "default": "true"},
    "output-directory": {"required": False, "default": ".governance"},
    "plan-path": {"required": False, "default": ".governance/governance.gplan"},
    "pr-comment": {"required": False, "default": "false"},
    "github-token": {"required": False, "default": ""},
}

EXPECTED_OUTPUTS = (
    "contract-version",
    "status",
    "validation-status",
    "policy-status",
    "policy-violation-count",
    "policy-error-count",
    "policy-warning-count",
    "plan-status",
    "create-count",
    "update-count",
    "unchanged-count",
    "remote-only-count",
    "writes-performed",
    "plan-path",
    "result-path",
    "report-path",
    "artifacts-path",
    "comment-status",
)


def _load_action() -> dict:
    return yaml.safe_load(ACTION_YML.read_text(encoding="utf-8"))


def _action_text() -> str:
    return ACTION_YML.read_text(encoding="utf-8")


def _load_schema() -> dict:
    text = files("governance.github_ci.schemas").joinpath(SCHEMA_NAME).read_text(encoding="utf-8")
    return json.loads(text)


def test_action_yml_is_composite() -> None:
    action = _load_action()
    assert action["runs"]["using"] == "composite"
    steps = action["runs"]["steps"]
    assert isinstance(steps, list)
    assert len(steps) >= 5
    ids = [step.get("id") for step in steps if "id" in step]
    assert "bootstrap" in ids
    assert "governance-run" in ids
    assert "comment-state" in ids
    assert "final" in ids


def test_action_yml_exact_inputs_and_defaults() -> None:
    action = _load_action()
    inputs = action["inputs"]
    assert set(inputs) == set(EXPECTED_INPUTS)
    for name, expected in EXPECTED_INPUTS.items():
        assert inputs[name]["required"] is expected["required"]
        if "default" in expected:
            assert inputs[name].get("default") == expected["default"]


def test_action_yml_outputs_map_only_to_steps_final() -> None:
    action = _load_action()
    outputs = action["outputs"]
    assert tuple(outputs) == EXPECTED_OUTPUTS
    for name, spec in outputs.items():
        value = spec["value"]
        assert value == f"${{{{ steps.final.outputs.{name} }}}}"
        assert "steps.governance-run" not in value
        assert "steps.comment-state" not in value
        assert "steps.governance-comment" not in value


def test_action_yml_has_no_apply_or_secret_provider_inputs() -> None:
    action = _load_action()
    names = set(action["inputs"])
    forbidden = {
        "apply",
        "sync",
        "database-url",
        "collibra-token",
        "collibra-password",
        "collibra-username",
        "secret-provider",
        "provider-credentials",
    }
    assert names.isdisjoint(forbidden)
    text = _action_text().lower()
    assert "apply" not in action["inputs"]
    assert "secret-provider" not in text


def test_pr_comment_default_false() -> None:
    action = _load_action()
    assert action["inputs"]["pr-comment"]["default"] == "false"


def test_setup_python_pinned_to_sha_with_version_comment() -> None:
    text = _action_text()
    assert EXPECTED_SETUP_PYTHON in text
    action = _load_action()
    setup = action["runs"]["steps"][0]
    assert setup["uses"].startswith("actions/setup-python@")
    assert "a26af69be951a213d495a4c3e4e4022e16d87065" in setup["uses"]


def test_action_yml_bootstrap_uses_fresh_unique_venv() -> None:
    text = _action_text()
    assert 'mktemp -d "${RUNNER_TEMP}/gac-action-venv.XXXXXX"' in text
    assert "ACTION_VENV=" in text
    assert "mktemp -d" in text
    assert "XXXXXX" in text
    assert "/tmp/gac-action-venv" not in text
    assert 'RUNNER_TEMP}/gac-action-venv"' not in text  # fixed path without XXXXXX
    assert "python -m venv" in text
    assert "${{ github.action_path }}" in text


def test_schema_packaged_via_importlib_resources() -> None:
    resource = files("governance.github_ci.schemas").joinpath(SCHEMA_NAME)
    assert resource.is_file()
    schema = _load_schema()
    assert schema["$id"] == ("urn:collibra-governance-automation:schema:governance-action-result:1")


def test_schema_id_urn_and_enums_exclude_reporting_failure() -> None:
    schema = _load_schema()
    assert schema["$id"] == ("urn:collibra-governance-automation:schema:governance-action-result:1")
    assert schema["properties"]["result_schema"]["const"] == "governance-action-result"
    assert schema["properties"]["result_version"]["const"] == "1"
    assert schema["properties"]["status"]["enum"] == ["passed", "blocked", "failed"]
    assert schema["properties"]["operation"]["enum"] == ["validate", "check", "plan"]
    failure_codes = schema["properties"]["failure_code"]["enum"]
    assert None in failure_codes
    assert "reporting_failure" not in failure_codes
    assert set(failure_codes) == {
        None,
        "configuration_failed",
        "policy_blocked",
        "operational_failure",
        "inputs_changed_during_run",
        "action_contract_invalid",
    }
    assert schema["properties"]["execution"]["properties"]["status"]["const"] == "not_requested"
    assert schema["properties"]["execution"]["properties"]["writes_performed"]["const"] == 0


def test_public_comment_status_never_requested() -> None:
    assert "requested" not in _PUBLIC_COMMENT_STATUSES
    assert (
        frozenset(
            {
                "disabled",
                "created",
                "updated",
                "skipped_non_pr",
                "skipped_untrusted_fork",
                "failed",
            }
        )
        == _PUBLIC_COMMENT_STATUSES
    )


def test_runner_steps_use_isolated_python_dash_i() -> None:
    text = _action_text()
    assert "-I -m governance.github_ci run" in text
    assert "-I -m governance.github_ci comment" in text
    assert "-I -m governance.github_ci finalize" in text
    assert "-I -m governance.github_ci fail-gate" in text
    action = _load_action()
    run_step = next(step for step in action["runs"]["steps"] if step.get("id") == "governance-run")
    env = run_step.get("env") or {}
    assert "GITHUB_TOKEN" not in env
    assert "GH_TOKEN" not in env
    assert "INPUT_GITHUB_TOKEN" not in env
    assert "Intentionally omit" in _action_text()
