"""Explicit owner configuration; paths and task identity are never model supplied."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class OutOfProjectScope(ValueError):
    """A valid event belongs elsewhere; ignoring it is not a capture failure."""


class Check(BaseModel):
    model_config = ConfigDict(extra="forbid")
    argv: list[str] = Field(min_length=1)
    scope: Literal["syntax", "fields", "frontend", "task"]
    description: str = Field(min_length=1, max_length=500)
    timeout: int = Field(default=60, ge=1, le=600)


class Project(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str
    root: Path
    data_dir: Path
    task_id: str
    stage: str = Field(min_length=1, max_length=100)
    customer_id: str | None = None
    site_id: str | None = None
    checks: dict[str, Check] = Field(default_factory=dict)

    @field_validator("project_id", "task_id")
    @classmethod
    def stable_id(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", value):
            raise ValueError("IDs must be stable path-safe identifiers (1..80 characters)")
        return value

    @field_validator("root", "data_dir")
    @classmethod
    def absolute_path(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("Project and data paths must be absolute")
        return value.resolve()

    @property
    def store(self) -> Path:
        return self.data_dir / self.project_id

    @property
    def knowledge_dir(self) -> Path:
        return self.store / "knowledge"

    @property
    def gap_file(self) -> Path:
        return self.store / "capture-gaps" / f"{self.task_id}.json"

    @property
    def pending_dir(self) -> Path:
        return self.store / "capture-pending" / self.task_id

    def check_cwd(self, cwd: object) -> None:
        if not isinstance(cwd, str) or not Path(cwd).is_absolute():
            raise ValueError("Hook must identify an absolute working directory")
        if not Path(cwd).resolve().is_relative_to(self.root):
            raise OutOfProjectScope("Hook is outside registered project")
        # Parent hooks are inherited by tools launched in nested repositories.
        # A separate .git boundary must never enter the parent's collection scope.
        current = Path(cwd).resolve()
        while current != self.root:
            if (current / ".git").exists():
                raise OutOfProjectScope("Nested repository is outside registered project")
            current = current.parent


def load_project(path: Path) -> Project:
    if not path.is_absolute():
        raise ValueError("Project configuration path must be absolute")
    project = Project.model_validate_json(path.read_text(encoding="utf-8"))
    if not project.root.is_dir():
        raise ValueError("Registered project root does not exist")
    return project


def bind_store(project: Project) -> None:
    """Prevent accidentally reusing an ID/store for a different checkout."""
    project.store.mkdir(parents=True, exist_ok=True)
    path = project.store / "project-binding.json"
    expected = {"project_id": project.project_id, "root": str(project.root)}
    try:
        with path.open("x", encoding="utf-8") as stream:
            json.dump(expected, stream)
    except FileExistsError:
        if json.loads(path.read_text(encoding="utf-8")) != expected:
            raise ValueError("Store already belongs to a different project root") from None


def code_version(project: Project, *, timeout: float = 1) -> dict[str, str | bool | None]:
    """References only. A timeout or a non-Git project leaves version unknown."""
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=project.root,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=True,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=project.root,
            capture_output=True,
            timeout=timeout,
            check=True,
        ).stdout
        return {
            "commit": sha,
            "dirty": bool(dirty),
            "worktree_status_sha256": hashlib.sha256(dirty).hexdigest(),
        }
    except (OSError, subprocess.SubprocessError):
        return {"commit": None, "dirty": None, "worktree_status_sha256": None}
