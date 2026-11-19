"""Install project-local hooks, inspect linked runs and close out a building task."""

from __future__ import annotations

import argparse
import json
import shlex
import shutil
import sys
from pathlib import Path

from aer import AER
from aer.projects.config import Project, bind_store, load_project

HOOKS = ("UserPromptSubmit", "PreToolUse", "PostToolUse", "PostToolUseFailure", "SessionEnd")


def install(project_path: Path, python: Path) -> Path:
    """Merge only our exact command. Existing hooks and other settings survive."""
    project = load_project(project_path)
    if not python.is_absolute() or not python.is_file():
        raise ValueError("Python interpreter must be an existing absolute path")
    bind_store(project)
    # Provision/migrate off the latency-sensitive hook path.
    with AER(project.store, knowledge_dir=project.knowledge_dir):
        pass
    target = project.root / ".claude" / "settings.local.json"
    settings = json.loads(target.read_text(encoding="utf-8")) if target.exists() else {}
    # Claude runs command hooks through a shell, including Git Bash on Windows.
    # Forward slashes avoid backslash consumption; shlex protects shell metacharacters.
    command = shlex.join(
        [
            python.as_posix(),
            "-X",
            "utf8",
            "-m",
            "aer.projects.hook",
            "--project",
            project_path.as_posix(),
        ]
    )
    hooks = settings.setdefault("hooks", {})
    for event in HOOKS:
        groups = hooks.setdefault(event, [])
        if not isinstance(groups, list):
            raise ValueError("Existing hook configuration is not a list")
        for group in groups:
            for hook in group.get("hooks", []):
                try:
                    parts = shlex.split(hook.get("command", ""))
                    owned = (
                        "aer.projects.hook" in parts
                        and "--project" in parts
                        and Path(parts[parts.index("--project") + 1]).resolve()
                        == project_path.resolve()
                    )
                except (ValueError, IndexError):
                    owned = False
                if owned:
                    hook.update(command=command, timeout=10)
        if not any(h.get("command") == command for g in groups for h in g.get("hooks", [])):
            groups.append({"hooks": [{"type": "command", "command": command, "timeout": 10}]})
    target.parent.mkdir(parents=True, exist_ok=True)
    backup = project.store / "claude-settings-before-aer.json"
    if target.exists() and not backup.exists():
        shutil.copy2(target, backup)
    temporary = target.with_suffix(".json.aer-tmp")
    temporary.write_text(
        json.dumps(settings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(target)
    return target


def runs(project: Project) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    with AER(project.store, knowledge_dir=project.knowledge_dir) as aer:
        offset = 0
        while batch := aer.list_runs(limit=100, offset=offset):
            for run in batch:
                meta = run.metadata.get("building_project")
                if isinstance(meta, dict) and meta.get("task_id") == project.task_id:
                    rows.append(
                        {
                            "run_id": run.id,
                            "status": run.status.value,
                            "association": meta,
                            "unapplied_events": aer.adapter_events.count(
                                aer_run_id=run.id, applied=False
                            ),
                        }
                    )
            offset += len(batch)
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    installer = commands.add_parser("install")
    installer.add_argument("--python", type=Path, default=Path(sys.executable))
    commands.add_parser("runs")
    finish = commands.add_parser("closeout")
    finish.add_argument("--run", action="append", required=True)
    finish.add_argument("--revision", required=True)
    finish.add_argument("--candidate", type=Path, required=True)
    commands.add_parser(
        "project-index", help="Explicit index projection; never runs in a hook/read"
    )
    args = parser.parse_args(argv)
    project = load_project(args.project)
    if args.command == "install":
        output: object = {"settings": str(install(args.project, args.python))}
    elif args.command == "runs":
        output = runs(project)
    elif args.command == "project-index":
        with AER(project.store, knowledge_dir=project.knowledge_dir) as aer:
            output = str(aer.project_experiences())
    else:
        from aer.projects.closeout import closeout, load_candidate

        output = closeout(project, args.run, args.revision, load_candidate(args.candidate))
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
