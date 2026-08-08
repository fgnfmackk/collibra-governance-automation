"""Sticky PR comment publisher tests using httpx.MockTransport."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import pytest

from governance.github_ci.comment import (
    COMMENT_MARKER,
    CommentError,
    create_or_update_comment,
    find_sticky_comment,
)
from governance.github_ci.comment import (
    main as comment_main,
)
from governance.github_ci.finalize import (
    _PUBLIC_COMMENT_STATUSES,
    emit_annotations_and_comment_state,
    resolve_comment_state,
)


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


def _pr_event(*, number: int = 42, full_name: str = "acme/demo") -> dict[str, Any]:
    return {
        "pull_request": {
            "number": number,
            "head": {"repo": {"full_name": full_name}},
        }
    }


def _write_event(tmp_path: Path, payload: dict[str, Any]) -> Path:
    path = tmp_path / "event.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _prepare_comment_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    eligibility: str = "requested",
    token: str | None = "ghs_test_token",
    api_url: str = "https://api.github.example",
    repository: str = "acme/demo",
    event: dict[str, Any] | None = None,
) -> Path:
    github_output = tmp_path / "github_output.txt"
    github_output.write_text("", encoding="utf-8")
    report = tmp_path / "report.md"
    report.write_text("# Governance as Code\n\nOVERALL: PASS\n", encoding="utf-8")
    event_path = _write_event(tmp_path, event if event is not None else _pr_event())
    monkeypatch.setenv("GITHUB_OUTPUT", str(github_output))
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("REPORT_PATH", "report.md")
    monkeypatch.setenv("COMMENT_ELIGIBILITY", eligibility)
    monkeypatch.setenv("GITHUB_API_URL", api_url)
    monkeypatch.setenv("GITHUB_REPOSITORY", repository)
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    if token is None:
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    else:
        monkeypatch.setenv("GITHUB_TOKEN", token)
    return github_output


class _ApiRecorder:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.comments: list[dict[str, Any]] = []
        self.status_by_path: dict[str, int] = {}
        self.create_status = 201
        self.update_status = 200
        self.list_status = 200
        self.raw_error_body = '{"message":"secret-body-should-not-log","documentation_url":"x"}'

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        parsed = urlparse(str(request.url))
        path = parsed.path
        if request.method == "GET" and path.endswith("/comments"):
            page = int(httpx.QueryParams(parsed.query).get("page", "1"))
            per_page = int(httpx.QueryParams(parsed.query).get("per_page", "100"))
            if self.list_status >= 400:
                return httpx.Response(self.list_status, text=self.raw_error_body)
            start = (page - 1) * per_page
            end = start + per_page
            chunk = self.comments[start:end]
            return httpx.Response(200, json=chunk)
        if request.method == "POST" and path.endswith("/comments"):
            if self.create_status >= 400:
                return httpx.Response(self.create_status, text=self.raw_error_body)
            body = json.loads(request.content.decode("utf-8"))
            item = {
                "id": 1000 + len(self.comments),
                "body": body["body"],
                "user": {"login": "github-actions[bot]"},
            }
            self.comments.append(item)
            return httpx.Response(self.create_status, json=item)
        if request.method == "PATCH" and "/issues/comments/" in path:
            if self.update_status >= 400:
                return httpx.Response(self.update_status, text=self.raw_error_body)
            comment_id = int(path.rstrip("/").split("/")[-1])
            body = json.loads(request.content.decode("utf-8"))
            for item in self.comments:
                if item["id"] == comment_id:
                    item["body"] = body["body"]
                    return httpx.Response(self.update_status, json=item)
            return httpx.Response(404, text=self.raw_error_body)
        return httpx.Response(404, text=self.raw_error_body)


def _client(recorder: _ApiRecorder) -> httpx.Client:
    return httpx.Client(
        transport=httpx.MockTransport(recorder.handler),
        headers={"Authorization": "Bearer ghs_test_token"},
    )


def test_comment_missing_token_failed_zero_api(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Phase B: trusted PR + pr-comment + missing token → failed, zero API.
    public, eligibility = resolve_comment_state(
        phase_a_failed=False,
        pr_comment=True,
        event_name="pull_request",
        event=_pr_event(),
        repository="acme/demo",
        token_present=False,
    )
    assert public == "failed"
    assert eligibility == ""

    github_output = _prepare_comment_env(
        tmp_path,
        monkeypatch,
        eligibility="requested",
        token=None,
    )
    recorder = _ApiRecorder()

    def boom_client(*_a: Any, **_k: Any) -> httpx.Client:
        raise AssertionError("httpx.Client must not be constructed without a token")

    monkeypatch.setattr("governance.github_ci.comment.httpx.Client", boom_client)
    assert comment_main() == 0
    outputs = _read_github_output(github_output)
    assert outputs["comment-status"] == "failed"
    assert recorder.requests == []


def test_comment_transport_error_writes_failed_exits_zero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Transport failures must not skip GITHUB_OUTPUT or fail the comment step."""
    github_output = _prepare_comment_env(tmp_path, monkeypatch)

    def boom_client(*_a: Any, **_k: Any) -> httpx.Client:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr("governance.github_ci.comment.httpx.Client", boom_client)
    assert comment_main() == 0
    outputs = _read_github_output(github_output)
    assert outputs["comment-status"] == "failed"


def test_phase_a_trusted_pr_with_token_failed_zero_api(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    public, eligibility = resolve_comment_state(
        phase_a_failed=True,
        pr_comment=True,
        event_name="pull_request",
        event=_pr_event(),
        repository="acme/demo",
        token_present=True,
    )
    assert public == "failed"
    assert eligibility == ""

    github_output = tmp_path / "out.txt"
    github_output.write_text("", encoding="utf-8")
    event_path = _write_event(tmp_path, _pr_event())
    monkeypatch.setenv("GITHUB_OUTPUT", str(github_output))
    monkeypatch.setenv("PHASE_A_FAILED", "true")
    monkeypatch.setenv("PR_COMMENT", "true")
    monkeypatch.setenv("INPUT_TOKEN_NONEMPTY", "true")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
    monkeypatch.setenv("GITHUB_REPOSITORY", "acme/demo")
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("ANNOTATIONS_PATH", "")

    calls = {"count": 0}

    def tracking_client(*_a: Any, **_k: Any) -> httpx.Client:
        calls["count"] += 1
        raise AssertionError("Phase A must not call GitHub comment API")

    monkeypatch.setattr("httpx.Client", tracking_client)
    assert emit_annotations_and_comment_state() == 0
    outputs = _read_github_output(github_output)
    assert outputs["comment-status"] == "failed"
    assert outputs["comment-eligibility"] == ""
    assert calls["count"] == 0


def test_public_comment_status_never_requested() -> None:
    assert "requested" not in _PUBLIC_COMMENT_STATUSES
    cases = [
        resolve_comment_state(
            phase_a_failed=False,
            pr_comment=True,
            event_name="pull_request",
            event=_pr_event(),
            repository="acme/demo",
            token_present=True,
        ),
        resolve_comment_state(
            phase_a_failed=True,
            pr_comment=True,
            event_name="pull_request",
            event=_pr_event(),
            repository="acme/demo",
            token_present=True,
        ),
        resolve_comment_state(
            phase_a_failed=False,
            pr_comment=False,
            event_name="pull_request",
            event=_pr_event(),
            repository="acme/demo",
            token_present=True,
        ),
        resolve_comment_state(
            phase_a_failed=False,
            pr_comment=True,
            event_name="push",
            event={},
            repository="acme/demo",
            token_present=True,
        ),
        resolve_comment_state(
            phase_a_failed=False,
            pr_comment=True,
            event_name="pull_request",
            event=_pr_event(full_name="fork/demo"),
            repository="acme/demo",
            token_present=True,
        ),
    ]
    for public, eligibility in cases:
        assert public != "requested"
        assert eligibility in {"", "requested"}
        if eligibility == "requested":
            # Interim public status is blank; finalizer takes publisher status.
            assert public == ""
        else:
            assert public in _PUBLIC_COMMENT_STATUSES


def test_create_when_no_marker() -> None:
    recorder = _ApiRecorder()
    with _client(recorder) as client:
        status = create_or_update_comment(
            token="ghs_test_token",
            api_url="https://api.github.example",
            repository="acme/demo",
            issue_number=7,
            body=f"{COMMENT_MARKER}\nhello\n",
            client=client,
        )
    assert status == "created"
    assert len(recorder.requests) == 2  # list + create
    assert recorder.requests[0].method == "GET"
    assert recorder.requests[1].method == "POST"
    assert recorder.comments[0]["body"].startswith(COMMENT_MARKER)


def test_update_when_bot_marker_first_line() -> None:
    recorder = _ApiRecorder()
    recorder.comments.append(
        {
            "id": 99,
            "body": f"{COMMENT_MARKER}\nold body\n",
            "user": {"login": "github-actions[bot]"},
        }
    )
    with _client(recorder) as client:
        status = create_or_update_comment(
            token="ghs_test_token",
            api_url="https://api.github.example",
            repository="acme/demo",
            issue_number=7,
            body=f"{COMMENT_MARKER}\nnew body\n",
            client=client,
        )
    assert status == "updated"
    assert any(req.method == "PATCH" for req in recorder.requests)
    assert recorder.comments[0]["body"] == f"{COMMENT_MARKER}\nnew body\n"


def test_ignore_user_spoof_marker() -> None:
    recorder = _ApiRecorder()
    recorder.comments.append(
        {
            "id": 11,
            "body": f"{COMMENT_MARKER}\nspoofed\n",
            "user": {"login": "evil-user"},
        }
    )
    with _client(recorder) as client:
        status = create_or_update_comment(
            token="ghs_test_token",
            api_url="https://api.github.example",
            repository="acme/demo",
            issue_number=7,
            body=f"{COMMENT_MARKER}\nofficial\n",
            client=client,
        )
    assert status == "created"
    assert any(req.method == "POST" for req in recorder.requests)
    assert not any(req.method == "PATCH" for req in recorder.requests)


def test_pagination_finds_marker_on_later_page() -> None:
    recorder = _ApiRecorder()
    # Fill first page with non-matching comments.
    for index in range(100):
        recorder.comments.append(
            {
                "id": index + 1,
                "body": "noise",
                "user": {"login": "github-actions[bot]"},
            }
        )
    recorder.comments.append(
        {
            "id": 101,
            "body": f"{COMMENT_MARKER}\npage-two\n",
            "user": {"login": "github-actions[bot]"},
        }
    )
    with _client(recorder) as client:
        found = find_sticky_comment(
            client,
            api_url="https://api.github.example",
            repository="acme/demo",
            issue_number=7,
        )
        status = create_or_update_comment(
            token="ghs_test_token",
            api_url="https://api.github.example",
            repository="acme/demo",
            issue_number=7,
            body=f"{COMMENT_MARKER}\nupdated-from-page-two\n",
            client=client,
        )
    assert found is not None
    assert found["id"] == 101
    assert status == "updated"
    get_pages = [
        req for req in recorder.requests if req.method == "GET" and "page=2" in str(req.url)
    ]
    assert get_pages


@pytest.mark.parametrize("status_code", [403, 404, 422, 500, 503])
def test_api_errors_failed_without_raw_body_logged(
    status_code: int,
    capsys: pytest.CaptureFixture[str],
) -> None:
    recorder = _ApiRecorder()
    recorder.list_status = status_code
    with _client(recorder) as client, pytest.raises(CommentError) as exc:
        create_or_update_comment(
            token="ghs_test_token",
            api_url="https://api.github.example",
            repository="acme/demo",
            issue_number=7,
            body=f"{COMMENT_MARKER}\nbody\n",
            client=client,
        )
    assert "secret-body-should-not-log" not in str(exc.value)
    assert "secret-body-should-not-log" not in exc.value.message
    assert "documentation_url" not in exc.value.message
    captured = capsys.readouterr()
    assert "secret-body-should-not-log" not in captured.out
    assert "secret-body-should-not-log" not in captured.err


def test_fork_and_non_pr_zero_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fork_public, fork_elig = resolve_comment_state(
        phase_a_failed=False,
        pr_comment=True,
        event_name="pull_request",
        event=_pr_event(full_name="other/fork"),
        repository="acme/demo",
        token_present=True,
    )
    assert fork_public == "skipped_untrusted_fork"
    assert fork_elig == ""

    non_pr_public, non_pr_elig = resolve_comment_state(
        phase_a_failed=False,
        pr_comment=True,
        event_name="push",
        event={},
        repository="acme/demo",
        token_present=True,
    )
    assert non_pr_public == "skipped_non_pr"
    assert non_pr_elig == ""

    # Publisher short-circuits when eligibility is not requested.
    github_output = _prepare_comment_env(
        tmp_path,
        monkeypatch,
        eligibility="",
        token="ghs_test_token",
    )
    monkeypatch.setattr(
        "governance.github_ci.comment.httpx.Client",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no API")),
    )
    assert comment_main() == 0
    assert _read_github_output(github_output)["comment-status"] == "disabled"


def test_github_api_url_used() -> None:
    recorder = _ApiRecorder()
    custom = "https://ghe.example.com/api/v3"
    with _client(recorder) as client:
        create_or_update_comment(
            token="ghs_test_token",
            api_url=custom,
            repository="acme/demo",
            issue_number=9,
            body=f"{COMMENT_MARKER}\nbody\n",
            client=client,
        )
    assert recorder.requests
    for request in recorder.requests:
        assert str(request.url).startswith(custom)
        assert "api.github.com" not in str(request.url)


def test_phase_a_fork_and_disabled_precedence() -> None:
    fork_status, fork_elig = resolve_comment_state(
        phase_a_failed=True,
        pr_comment=True,
        event_name="pull_request",
        event=_pr_event(full_name="fork/x"),
        repository="acme/demo",
        token_present=True,
    )
    assert fork_status == "skipped_untrusted_fork"
    assert fork_elig == ""

    disabled, disabled_elig = resolve_comment_state(
        phase_a_failed=True,
        pr_comment=False,
        event_name="pull_request",
        event=_pr_event(),
        repository="acme/demo",
        token_present=True,
    )
    assert disabled == "disabled"
    assert disabled_elig == ""
