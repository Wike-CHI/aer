"""Synthetic hook fixtures, isolated stores and real subprocess checks; not customer data."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from aer import AER, CallableDistillationProvider, ExperienceCandidate
from aer.adapter.claude.mapping import CLAUDE_PROVIDER
from aer.exceptions import DistillationError
from aer.projects.capture import capture
from aer.projects.cli import install
from aer.projects.closeout import closeout
from aer.projects.config import Check, Project, code_version
from aer.runtime.enums import EventType, RunStatus
from aer.runtime.models import AdapterEventRecord


def project_at(tmp_path, name="one", *, scope="task", command=None):
    root = tmp_path / name
    root.mkdir()
    subprocess.run(["git", "init", str(root)], check=True, capture_output=True)
    (root / "page.txt").write_text("fixed", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "page.txt"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-m",
            "isolated fixture",
        ],
        check=True,
        capture_output=True,
    )
    return Project(
        project_id=name,
        root=root,
        data_dir=tmp_path / "stores",
        task_id="task-1",
        stage="repair",
        checks={
            "page": Check(
                argv=command
                or [
                    sys.executable,
                    "-c",
                    "from pathlib import Path; assert Path('page.txt').read_text()=='fixed'",
                ],
                scope=scope,
                description="Check fixture page content, not a live WP site",
            )
        },
    )


def payload(project, event="UserPromptSubmit", **extra):
    return {
        "hook_event_name": event,
        "cwd": str(project.root),
        "session_id": "session-1",
        "prompt_id": "prompt-1",
        "prompt": "repair WordPress field output",
        **extra,
    }


def candidate():
    return ExperienceCandidate(
        domain="wordpress",
        title="Check field output",
        problem="field output was broken",
        failed_attempts=("old mapping failed",),
        solution="repair mapping and check rendered value",
        metadata={"applicability": "unknown", "limitations": ["fixture page only"]},
    )


def test_two_projects_restart_and_duplicate_isolation(tmp_path):
    one, two = project_at(tmp_path), project_at(tmp_path, "two")
    first = capture(one, payload(one))["run_id"]
    second = capture(two, payload(two))["run_id"]
    assert first != second
    assert capture(one, payload(one))["run_id"] == first
    event = payload(
        one, "PreToolUse", tool_name="Bash", tool_input={"command": "check"}, tool_use_id="tool-1"
    )
    assert capture(one, event)["status"] == "APPLIED"
    assert capture(one, event)["status"] == "DUPLICATE"
    with AER(one.store, knowledge_dir=one.knowledge_dir) as aer:
        assert len(aer.list_runs()) == 1
        association = aer.get_run(first).metadata["building_project"]
        assert association["project_id"] == "one"
        assert association["task_id"] == "task-1"
        assert association["customer_id"] is None
        assert association["code_version"]["commit"] == code_version(one, timeout=5)["commit"]
        assert aer.adapter_events.count(aer_run_id=first) == 1
    with pytest.raises(ValueError, match="outside"):
        capture(one, payload(two))
    nested = one.root / "nested"
    nested.mkdir()
    (nested / ".git").mkdir()
    with pytest.raises(ValueError, match="Nested repository"):
        capture(one, payload(one, cwd=str(nested)))
    with pytest.raises(ValueError, match="another task"):
        capture(one.model_copy(update={"task_id": "task-2"}), payload(one))


def test_unknown_result_and_session_end_do_not_verify(tmp_path):
    project = project_at(tmp_path)
    run_id = capture(project, payload(project))["run_id"]
    capture(project, payload(project, "PostToolUse", tool_name="Bash", tool_use_id="t"))
    capture(project, payload(project, "SessionEnd", reason="other"))
    with AER(project.store, knowledge_dir=project.knowledge_dir) as aer:
        assert aer.get_run(run_id).status == RunStatus.INCONCLUSIVE
        assert not aer.verified_success(run_id)
        assert not aer.evaluate_distillation(run_id, explicit_high_value=True).should_distill


def test_actual_check_distills_with_traceable_source(tmp_path):
    project = project_at(tmp_path)
    run_id = capture(project, payload(project))["run_id"]
    capture(
        project,
        payload(
            project,
            "PostToolUseFailure",
            tool_name="Bash",
            tool_use_id="bad",
            error="field mapping missing",
        ),
    )
    capture(
        project,
        payload(
            project,
            "PostToolUse",
            tool_name="Edit",
            tool_use_id="fix",
            tool_response={"message": "mapping changed"},
        ),
    )
    result = closeout(project, [run_id], code_version(project, timeout=5)["commit"], candidate())[0]
    assert result["verified_success"] is True
    assert result["experience"]["status"] == "VERIFIED"
    with AER(project.store, knowledge_dir=project.knowledge_dir) as aer:
        sources = aer.get_experience_sources(result["experience"]["id"])
        assert [s.run_id for s in sources] == [run_id]
        evidence = aer.get_run_evidence(run_id)
        assert evidence.errors
    assert list((project.store / "evidence" / run_id).glob("*.log"))


@pytest.mark.parametrize(
    "scope,command",
    [
        ("syntax", [sys.executable, "-c", "pass"]),
        ("task", [sys.executable, "-c", "raise SystemExit(1)"]),
    ],
)
def test_narrow_scope_or_failure_never_verified_success(tmp_path, scope, command):
    project = project_at(tmp_path, scope=scope, command=command)
    run_id = capture(project, payload(project))["run_id"]
    result = closeout(project, [run_id], code_version(project, timeout=5)["commit"], candidate())[0]
    assert result["verified_success"] is False
    if result["experience"]:
        assert result["experience"]["kind"] == "FAILURE"
        assert result["experience"]["solution"] is None


def test_verifier_exception_preserves_unknown(tmp_path):
    project = project_at(tmp_path, command=[str(tmp_path / "missing-executable")])
    # A completed narrow prefix cannot become a required PASS if the suite crashes.
    project.checks = {
        "syntax": Check(
            argv=[sys.executable, "-c", "pass"], scope="syntax", description="Syntax only"
        ),
        **project.checks,
    }
    run_id = capture(project, payload(project))["run_id"]
    with pytest.raises(FileNotFoundError):
        closeout(project, [run_id], code_version(project, timeout=5)["commit"], candidate())
    with AER(project.store, knowledge_dir=project.knowledge_dir) as aer:
        assert not aer.verified_success(run_id)
        assert aer.get_verification_summary(run_id).total == 0
        assert not aer.list_experiences()
        assert aer.get_run_evidence(run_id).errors


def test_unapplied_claim_blocks_sdk_and_closeout(tmp_path):
    project = project_at(tmp_path)
    run_id = capture(project, payload(project))["run_id"]
    with AER(
        project.store,
        distillation_provider=CallableDistillationProvider(
            name="fixture",
            func=lambda _: candidate(),
        ),
    ) as aer:
        aer.adapter_events.record(
            AdapterEventRecord(
                provider=CLAUDE_PROVIDER,
                external_event_id="lost",
                adapter_name="aer-claude-code",
                event_type=EventType.TOOL_RESULT,
                aer_run_id=run_id,
                applied=False,
            )
        )
        with pytest.raises(DistillationError, match="incomplete"):
            aer.distill_run(run_id, explicit_high_value=True)
    with pytest.raises(ValueError, match="unapplied"):
        closeout(project, [run_id], code_version(project, timeout=5)["commit"], candidate())


def test_install_preserves_existing_hooks_and_is_idempotent(tmp_path):
    project = project_at(tmp_path)
    config = tmp_path / "project.json"
    config.write_text(project.model_dump_json(), encoding="utf-8")
    target = project.root / ".claude" / "settings.local.json"
    target.parent.mkdir()
    original = {
        "permissions": {"allow": ["Read"]},
        "hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": "existing-hook"}]}]},
    }
    target.write_text(json.dumps(original), encoding="utf-8")
    install(config, Path(sys.executable))
    install(config, Path(sys.executable))
    settings = json.loads(target.read_text(encoding="utf-8"))
    assert settings["permissions"] == original["permissions"]
    assert len(settings["hooks"]["PreToolUse"]) == 2
    assert settings["hooks"]["PreToolUse"][0] == original["hooks"]["PreToolUse"][0]
    assert (project.store / "claude-settings-before-aer.json").exists()


def test_gap_marker_and_wrong_run_cannot_be_closed(tmp_path):
    project = project_at(tmp_path)
    with AER(project.store, knowledge_dir=project.knowledge_dir) as aer:
        foreign = aer.start_run(task="not registered").run_id
    with pytest.raises(ValueError, match="another project"):
        closeout(project, [foreign], code_version(project, timeout=5)["commit"], candidate())
    run_id = capture(project, payload(project))["run_id"]
    project.gap_file.parent.mkdir()
    project.gap_file.write_text("{}")
    with pytest.raises(ValueError, match="capture gaps"):
        closeout(project, [run_id], code_version(project, timeout=5)["commit"], candidate())
    with (
        AER(project.store, knowledge_dir=project.knowledge_dir) as aer,
        pytest.raises(DistillationError, match="task capture gap"),
    ):
        aer.distill_run(run_id, explicit_high_value=True)


def test_supervisor_really_times_out_and_marks_gap(tmp_path, monkeypatch, capsys):
    import io
    import time

    from aer.projects import hook

    project = project_at(tmp_path)
    config = tmp_path / "project.json"
    config.write_text(project.model_dump_json(), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["hook", "--project", str(config)])
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload(project))))
    real_run = subprocess.run

    def slow_worker(*args, **kwargs):
        return real_run([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)

    monkeypatch.setattr(hook.subprocess, "run", slow_worker)
    started = time.monotonic()
    assert hook.main() == 0
    assert time.monotonic() - started < 12
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "TimeoutExpired" in captured.err
    assert json.loads(project.gap_file.read_text())["reason"] == "TimeoutExpired"


def test_real_hook_process_with_unicode_paths(tmp_path):
    project = project_at(tmp_path)
    renamed = project.root.with_name("project-\u9879\u76ee space")
    project.root.rename(renamed)
    project = project.model_copy(update={"root": renamed})
    config = tmp_path / "config-\u9879\u76ee.json"
    config.write_text(project.model_dump_json(), encoding="utf-8")
    install(config, Path(sys.executable))
    response = subprocess.run(
        [sys.executable, "-m", "aer.projects.hook", "--project", str(config)],
        input=json.dumps(payload(project), ensure_ascii=False).encode("utf-8"),
        capture_output=True,
        timeout=15,
        check=True,
    )
    assert response.stdout == b""
    assert not project.gap_file.exists(), response.stderr.decode("utf-8")
    assert not list(project.pending_dir.glob("*.json"))
    with AER(project.store, knowledge_dir=project.knowledge_dir) as aer:
        assert len(aer.list_runs()) == 1
        assert aer.list_runs()[0].metadata["building_project"]["project_id"] == project.project_id


def test_cancelled_supervisor_leaves_pending_delivery(tmp_path, monkeypatch):
    import io

    from aer.projects import hook

    project = project_at(tmp_path)
    run_id = capture(project, payload(project))["run_id"]
    config = tmp_path / "project.json"
    config.write_text(project.model_dump_json(), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["hook", "--project", str(config)])
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload(project))))

    def cancelled(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(hook.subprocess, "run", cancelled)
    with pytest.raises(KeyboardInterrupt):
        hook.main()
    assert list(project.pending_dir.glob("*.json"))
    assert not project.gap_file.exists()
    with (
        AER(project.store, knowledge_dir=project.knowledge_dir) as aer,
        pytest.raises(DistillationError, match="pending task"),
    ):
        aer.distill_run(run_id, explicit_high_value=True)
    with pytest.raises(ValueError, match="capture gaps"):
        closeout(project, [run_id], "unused-revision", candidate())


def test_worker_failure_preserves_safe_exit_diagnostic(tmp_path, monkeypatch, capsys):
    import io

    from aer.projects import hook

    project = project_at(tmp_path)
    config = tmp_path / "project.json"
    config.write_text(project.model_dump_json(), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["hook", "--project", str(config)])
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload(project))))
    monkeypatch.setattr(
        hook.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 7, "", "secret startup payload"),
    )
    assert hook.main() == 0
    assert json.loads(project.gap_file.read_text())["worker_exit_code"] == 7
    assert list(project.pending_dir.glob("*.json"))
    assert "secret startup payload" not in capsys.readouterr().err


@pytest.mark.parametrize("nested", [False, True])
def test_out_of_scope_hook_does_not_poison_parent_task(tmp_path, monkeypatch, nested):
    import io

    from aer.projects import hook

    project = project_at(tmp_path)
    run_id = capture(project, payload(project))["run_id"]
    if nested:
        other = project.root / "nested"
        other.mkdir()
        (other / ".git").mkdir()
        (project.root / ".git" / "info" / "exclude").write_text("nested/\n")
    else:
        other = tmp_path / "other"
        other.mkdir()
    config = tmp_path / "project.json"
    config.write_text(project.model_dump_json(), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["hook", "--project", str(config)])
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload(project, cwd=str(other)))))
    assert hook.main() == 0
    assert not project.gap_file.exists()
    assert not list(project.pending_dir.glob("*.json"))
    with AER(project.store, knowledge_dir=project.knowledge_dir) as aer:
        assert [run.id for run in aer.list_runs()] == [run_id]
    result = closeout(project, [run_id], code_version(project, timeout=5)["commit"], candidate())
    assert result[0]["verified_success"] is True


def test_project_index_uses_separate_absolute_indexes(tmp_path, monkeypatch):
    from aer.projects import cli

    projects = [project_at(tmp_path, name) for name in ("one", "two")]
    opened = []

    class IndexProbe:
        def __init__(self, data_dir, *, knowledge_dir):
            opened.append((data_dir, knowledge_dir))

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def project_experiences(self):
            return "projected"

    monkeypatch.setattr(cli, "AER", IndexProbe)
    monkeypatch.chdir(tmp_path)
    for project in projects:
        config = tmp_path / f"{project.project_id}.json"
        config.write_text(project.model_dump_json(), encoding="utf-8")
        assert cli.main(["--project", str(config), "project-index"]) == 0
    assert opened == [(p.store, p.store / "knowledge") for p in projects]
    assert opened[0][1] != opened[1][1]
    assert all(index.is_absolute() for _, index in opened)
    assert not (tmp_path / "knowledge").exists()
