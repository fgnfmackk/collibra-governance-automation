"""Workspace-relative path safety for the GitHub Action runner."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


class PathValidationError(Exception):
    """Rejected Action path input."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class WorkspacePaths:
    """Resolved workspace root and helpers for contained relative paths."""

    workspace: Path

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> WorkspacePaths:
        source = env if env is not None else os.environ
        raw = source.get("GITHUB_WORKSPACE", "").strip()
        if not raw:
            raise PathValidationError(
                "missing_workspace",
                "GITHUB_WORKSPACE is required",
            )
        workspace = Path(raw).resolve()
        if not workspace.is_dir():
            raise PathValidationError(
                "missing_workspace",
                "GITHUB_WORKSPACE must be an existing directory",
            )
        return cls(workspace=workspace)

    def normalize_relative(self, raw: str, *, field: str) -> str:
        """Return a normalized workspace-relative POSIX path string."""
        if not isinstance(raw, str) or not raw.strip():
            raise PathValidationError(
                "empty_path",
                f"{field} must be a non-empty relative path",
            )
        text = raw.strip().replace("\\", "/")
        if text.startswith("/") or (len(text) >= 2 and text[1] == ":"):
            raise PathValidationError(
                "absolute_path",
                f"{field} must be a workspace-relative path",
            )

        parts: list[str] = []
        for part in PurePosixPath(text).parts:
            if part in ("", "."):
                continue
            if part == "..":
                raise PathValidationError(
                    "path_escape",
                    f"{field} must not contain '..' segments",
                )
            parts.append(part)
        if not parts:
            raise PathValidationError(
                "empty_path",
                f"{field} must be a non-empty relative path",
            )
        return "/".join(parts)

    def resolve_under_workspace(self, relative_posix: str, *, field: str) -> Path:
        """Resolve ``relative_posix`` under the workspace; reject symlink escapes."""
        candidate = (self.workspace / Path(*relative_posix.split("/"))).resolve()
        try:
            candidate.relative_to(self.workspace)
        except ValueError as exc:
            raise PathValidationError(
                "symlink_escape",
                f"{field} resolves outside GITHUB_WORKSPACE",
            ) from exc
        return candidate

    def validate_output_directory(self, raw: str) -> tuple[str, Path]:
        relative = self.normalize_relative(raw, field="output-directory")
        absolute = self.resolve_under_workspace(relative, field="output-directory")
        return relative, absolute

    def validate_plan_path(
        self,
        raw: str,
        *,
        output_directory_relative: str,
        output_directory_absolute: Path,
    ) -> tuple[str, Path]:
        relative = self.normalize_relative(raw, field="plan-path")
        absolute = self.resolve_under_workspace(relative, field="plan-path")
        try:
            absolute.relative_to(output_directory_absolute.resolve())
        except ValueError as exc:
            raise PathValidationError(
                "not_under_output_directory",
                "plan-path must be a descendant of output-directory",
            ) from exc
        if relative == output_directory_relative:
            raise PathValidationError(
                "not_under_output_directory",
                "plan-path must be a descendant of output-directory",
            )
        # Reject resolved targets that escape the output directory via symlinks.
        try:
            absolute.relative_to(output_directory_absolute.resolve())
        except ValueError as exc:
            raise PathValidationError(
                "symlink_escape",
                "plan-path resolves outside output-directory",
            ) from exc
        return relative, absolute

    def to_relative_posix(self, absolute: Path) -> str:
        return absolute.resolve().relative_to(self.workspace).as_posix()
