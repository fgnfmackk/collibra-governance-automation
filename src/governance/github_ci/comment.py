"""Sticky pull-request comment publisher for Governance-as-Code reports."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx

from governance.github_ci.runner import write_github_output

COMMENT_MARKER = "<!-- governance-as-code:pr-report:v1 -->"
BOT_LOGIN = "github-actions[bot]"
API_VERSION = "2022-11-28"
MAX_PAGES = 20
PER_PAGE = 100


class CommentError(Exception):
    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


def _safe_api_message(status_code: int) -> str:
    if status_code == 403:
        return "pull request comment API returned 403"
    if status_code == 404:
        return "pull request comment API returned 404"
    if status_code == 422:
        return "pull request comment API returned 422"
    if 500 <= status_code <= 599:
        return "pull request comment API returned a server error"
    return "pull request comment API request failed"


def load_event(path: str | None = None) -> dict[str, Any]:
    raw = path or os.environ.get("GITHUB_EVENT_PATH", "")
    if not raw:
        raise CommentError("GITHUB_EVENT_PATH is required")
    try:
        payload = json.loads(Path(raw).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CommentError("unable to read GitHub event payload") from exc
    if not isinstance(payload, dict):
        raise CommentError("GitHub event payload must be an object")
    return payload


def pull_request_number(event: dict[str, Any]) -> int:
    pr = event.get("pull_request")
    if not isinstance(pr, dict) or "number" not in pr:
        raise CommentError("pull_request number missing from event")
    number = pr["number"]
    if not isinstance(number, int):
        raise CommentError("pull_request number invalid")
    return number


def build_comment_body(report_text: str) -> str:
    body = report_text if report_text.endswith("\n") else report_text + "\n"
    return f"{COMMENT_MARKER}\n{body}"


def _auth_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": API_VERSION,
        "User-Agent": "collibra-governance-automation",
    }


def _comments_url(api_url: str, repository: str, issue_number: int) -> str:
    base = api_url if api_url.endswith("/") else api_url + "/"
    return urljoin(base, f"repos/{repository}/issues/{issue_number}/comments")


def _comment_item_url(api_url: str, repository: str, comment_id: int) -> str:
    base = api_url if api_url.endswith("/") else api_url + "/"
    return urljoin(base, f"repos/{repository}/issues/comments/{comment_id}")


def find_sticky_comment(
    client: httpx.Client,
    *,
    api_url: str,
    repository: str,
    issue_number: int,
) -> dict[str, Any] | None:
    url = _comments_url(api_url, repository, issue_number)
    visited: set[str] = set()
    for page in range(1, MAX_PAGES + 1):
        request_url = f"{url}?per_page={PER_PAGE}&page={page}"
        if request_url in visited:
            break
        visited.add(request_url)
        response = client.get(request_url)
        if response.status_code >= 400:
            raise CommentError(_safe_api_message(response.status_code))
        items = response.json()
        if not isinstance(items, list):
            raise CommentError("pull request comment API returned an unexpected payload")
        if not items:
            return None
        for item in items:
            if not isinstance(item, dict):
                continue
            user = item.get("user")
            body = item.get("body")
            if not isinstance(user, dict) or not isinstance(body, str):
                continue
            if user.get("login") != BOT_LOGIN:
                continue
            first_line = body.splitlines()[0] if body else ""
            if first_line == COMMENT_MARKER:
                return item
        if len(items) < PER_PAGE:
            return None
    return None


def create_or_update_comment(
    *,
    token: str,
    api_url: str,
    repository: str,
    issue_number: int,
    body: str,
    client: httpx.Client | None = None,
) -> str:
    owns_client = client is None
    http = client or httpx.Client(
        headers=_auth_headers(token),
        timeout=30.0,
        follow_redirects=False,
    )
    try:
        existing = find_sticky_comment(
            http,
            api_url=api_url,
            repository=repository,
            issue_number=issue_number,
        )
        if existing is None:
            response = http.post(
                _comments_url(api_url, repository, issue_number),
                json={"body": body},
            )
            if response.status_code >= 400:
                raise CommentError(_safe_api_message(response.status_code))
            return "created"
        comment_id = existing.get("id")
        if not isinstance(comment_id, int):
            raise CommentError("sticky comment id invalid")
        response = http.patch(
            _comment_item_url(api_url, repository, comment_id),
            json={"body": body},
        )
        if response.status_code >= 400:
            raise CommentError(_safe_api_message(response.status_code))
        return "updated"
    finally:
        if owns_client:
            http.close()


def main() -> int:
    eligibility = os.environ.get("COMMENT_ELIGIBILITY", "").strip()
    if eligibility != "requested":
        write_github_output({"comment-status": "disabled"})
        return 0

    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        print("pull request comment requested but github-token is missing", file=sys.stderr)
        write_github_output({"comment-status": "failed"})
        return 0

    report_path = os.environ.get("REPORT_PATH", "").strip()
    workspace = os.environ.get("GITHUB_WORKSPACE", "").strip()
    api_url = os.environ.get("GITHUB_API_URL", "").strip()
    repository = os.environ.get("GITHUB_REPOSITORY", "").strip()

    if not report_path or not workspace or not api_url or not repository:
        print("pull request comment publisher misconfigured", file=sys.stderr)
        write_github_output({"comment-status": "failed"})
        return 0

    try:
        report_text = Path(workspace, report_path).read_text(encoding="utf-8")
        event = load_event()
        number = pull_request_number(event)
        body = build_comment_body(report_text)
        status = create_or_update_comment(
            token=token,
            api_url=api_url,
            repository=repository,
            issue_number=number,
            body=body,
        )
        write_github_output({"comment-status": status})
        return 0
    except CommentError as exc:
        print(exc.message, file=sys.stderr)
        write_github_output({"comment-status": "failed"})
        return 0
    except OSError:
        print("unable to read governance report for comment", file=sys.stderr)
        write_github_output({"comment-status": "failed"})
        return 0
    except Exception:
        # Transport / JSON / unexpected client failures must still write outputs and
        # exit 0 so finalize + fail-gate own the hard failure.
        print("pull request comment delivery failed", file=sys.stderr)
        write_github_output({"comment-status": "failed"})
        return 0
