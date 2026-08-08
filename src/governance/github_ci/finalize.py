"""Composite Action helpers: annotations, comment eligibility, final outputs, fail-gate."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from governance.github_ci.report import escape_workflow_command
from governance.github_ci.result import RESULT_VERSION
from governance.github_ci.runner import write_github_output

_PUBLIC_COMMENT_STATUSES = frozenset(
    {
        "disabled",
        "created",
        "updated",
        "skipped_non_pr",
        "skipped_untrusted_fork",
        "failed",
    }
)

_GOVERNANCE_OUTPUT_KEYS = (
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
)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "true" if default else "false").strip().lower()
    return raw == "true"


def _load_event() -> dict[str, Any]:
    path = os.environ.get("GITHUB_EVENT_PATH", "").strip()
    if not path:
        return {}
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _is_pull_request(event_name: str) -> bool:
    return event_name == "pull_request"


def _is_trusted_same_repo(event: dict[str, Any], repository: str) -> bool:
    pr = event.get("pull_request")
    if not isinstance(pr, dict):
        return False
    head = pr.get("head")
    if not isinstance(head, dict):
        return False
    repo = head.get("repo")
    if not isinstance(repo, dict):
        return False
    full_name = repo.get("full_name")
    return isinstance(full_name, str) and full_name == repository


def resolve_comment_state(
    *,
    phase_a_failed: bool,
    pr_comment: bool,
    event_name: str,
    event: dict[str, Any],
    repository: str,
    token_present: bool,
) -> tuple[str, str]:
    """Return ``(public_comment_status, internal_eligibility)``.

    ``internal_eligibility`` is ``requested`` only when Step 5 should run.
    """
    is_pr = _is_pull_request(event_name)
    trusted = is_pr and _is_trusted_same_repo(event, repository)

    if phase_a_failed:
        if not pr_comment:
            return "disabled", ""
        if not is_pr:
            return "skipped_non_pr", ""
        if not trusted:
            return "skipped_untrusted_fork", ""
        # Trusted same-repo PR with Phase A failure: failed, zero API.
        return "failed", ""

    if not pr_comment:
        return "disabled", ""
    if not is_pr:
        return "skipped_non_pr", ""
    if not trusted:
        return "skipped_untrusted_fork", ""
    if not token_present:
        return "failed", ""
    # Public status comes from the publisher/finalizer; eligibility is internal only.
    return "", "requested"


def emit_annotations_and_comment_state() -> int:
    phase_a_failed = _env_bool("PHASE_A_FAILED", default=False)
    pr_comment = _env_bool("PR_COMMENT", default=False)
    event_name = os.environ.get("GITHUB_EVENT_NAME", "").strip()
    repository = os.environ.get("GITHUB_REPOSITORY", "").strip()
    workspace = os.environ.get("GITHUB_WORKSPACE", "").strip()
    annotations_path = os.environ.get("ANNOTATIONS_PATH", "").strip()
    token = os.environ.get("COMMENT_TOKEN_PRESENT", "").strip().lower() == "true"
    # Token presence is signaled without exposing the token value.
    if os.environ.get("INPUT_TOKEN_NONEMPTY", "").strip().lower() == "true":
        token = True

    event = _load_event()
    public_status, eligibility = resolve_comment_state(
        phase_a_failed=phase_a_failed,
        pr_comment=pr_comment,
        event_name=event_name,
        event=event,
        repository=repository,
        token_present=token,
    )

    if phase_a_failed:
        print(
            f"::error::{escape_workflow_command('invalid action output directory')}",
            flush=True,
        )
        if public_status == "failed" and pr_comment:
            print(
                "pull request comment was not published because action preflight failed",
                file=sys.stderr,
            )
    elif annotations_path and workspace:
        path = Path(workspace) / annotations_path
        if path.is_file():
            content = path.read_text(encoding="utf-8")
            sys.stdout.write(content)
            if content and not content.endswith("\n"):
                sys.stdout.write("\n")

    if (
        public_status == "failed"
        and not phase_a_failed
        and pr_comment
        and eligibility != "requested"
    ):
        print(
            "pull request comment requested but github-token is missing",
            file=sys.stderr,
        )

    write_github_output(
        {
            "comment-status": public_status,
            "comment-eligibility": eligibility,
        }
    )
    return 0


def _read_env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def finalize_outputs() -> int:
    phase_a_failed = _env_bool("PHASE_A_FAILED", default=False)
    desired = _read_env("DESIRED_EXIT_CODE", "1")
    comment_from_state = _read_env("COMMENT_STATUS_STATE", "disabled")
    eligibility = _read_env("COMMENT_ELIGIBILITY", "")
    comment_from_publisher = _read_env("COMMENT_STATUS_PUBLISHER", "")
    fail_on = _env_bool("FAIL_ON_POLICY_ERROR", default=True)

    if eligibility == "requested":
        comment_status = comment_from_publisher or "failed"
    else:
        comment_status = comment_from_state or "disabled"

    if comment_status == "requested" or comment_status not in _PUBLIC_COMMENT_STATUSES:
        # Never leak internal eligibility publicly.
        comment_status = "failed" if eligibility == "requested" else "disabled"

    outputs: dict[str, str] = {}
    for key in _GOVERNANCE_OUTPUT_KEYS:
        env_key = "GOV_" + key.upper().replace("-", "_")
        outputs[key] = _read_env(env_key, "")

    if phase_a_failed:
        outputs.setdefault("contract-version", RESULT_VERSION)
        outputs["status"] = outputs.get("status") or "failed"
        outputs["validation-status"] = "not_run"
        outputs["policy-status"] = outputs.get("policy-status") or "not_run"
        outputs["plan-status"] = outputs.get("plan-status") or "not_run"
        for count_key in (
            "policy-violation-count",
            "policy-error-count",
            "policy-warning-count",
            "create-count",
            "update-count",
            "unchanged-count",
            "remote-only-count",
        ):
            outputs[count_key] = outputs.get(count_key) or "0"
        outputs["writes-performed"] = "0"
        outputs["plan-path"] = ""
        outputs["result-path"] = ""
        outputs["report-path"] = ""
        outputs["artifacts-path"] = ""
        desired = "1"
    else:
        # Minimal hard-failure contract when essential outputs are missing.
        if not outputs.get("result-path") and not outputs.get("status"):
            outputs.update(
                {
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
                }
            )
            desired = "1"
        outputs.setdefault("contract-version", RESULT_VERSION)
        outputs.setdefault("writes-performed", "0")

    final_exit = desired
    if comment_status == "failed":
        final_exit = "1"
    elif outputs.get("status") == "blocked" and not fail_on:
        # desired already accounts for fail-on; keep consistent
        final_exit = desired
    elif final_exit not in {"0", "1", "3"}:
        final_exit = "1"

    outputs["comment-status"] = comment_status
    outputs["final-exit-code"] = final_exit
    write_github_output(outputs)
    return 0


def fail_gate() -> int:
    raw = os.environ.get("FINAL_EXIT_CODE", "1").strip()
    try:
        code = int(raw)
    except ValueError:
        return 1
    if code not in {0, 1, 3}:
        return 1
    return code
