"""Synthetic capture data in isolated stores; no customer corpus is used."""

import copy
import json

import pytest

from aer import AER
from aer.exceptions import DistillationError
from aer.projects.capture import capture
from aer.projects.central import closeout_central, receive
from aer.projects.config import code_version
from aer.projects.outbox import batches
from tests.projects.test_workflow import candidate, payload, project_at


def setup_stream(tmp_path, name="one"):
    project = project_at(tmp_path, name).model_copy(update={"sync_source_id": "device-1"})
    run = capture(project, payload(project))["run_id"]
    capture(
        project,
        payload(
            project,
            "PostToolUse",
            tool_use_id="tool-1",
            tool_name="Bash",
            tool_input={"command": "check"},
            tool_response="no exit evidence",
        ),
    )
    capture(project, payload(project, "SessionEnd", reason="exit"))
    manifest = tmp_path / f"{name}-registry.json"
    manifest.write_text(
        json.dumps(
            {
                "projects": {
                    name: {
                        "source_ids": ["device-1"],
                        "root": str(project.root),
                        "checks": {k: v.model_dump() for k, v in project.checks.items()},
                    }
                }
            }
        )
    )
    return project, run, manifest, batches(project)[0]


def test_replay_restart_isolation_and_trusted_closeout(tmp_path):
    project, local, manifest, body = setup_stream(tmp_path)
    with AER(tmp_path / "central", knowledge_dir=tmp_path / "knowledge") as aer:
        ack = receive(aer, manifest, body)
        remote = ack["runs"][local]
        assert remote != local
        assert aer.get_run(remote).status.value == "INCONCLUSIVE"
        assert aer.get_verification_summary(remote).total == 0
        assert receive(aer, manifest, body) == ack
    with AER(tmp_path / "central", knowledge_dir=tmp_path / "knowledge") as aer:
        assert receive(aer, manifest, body) == ack
        result = closeout_central(
            aer,
            manifest,
            project.project_id,
            project.task_id,
            "device-1",
            code_version(project)["commit"],
            candidate(),
        )
        assert result
        assert aer.get_verification_summary(remote).total == 1
    other, other_local, other_manifest, other_body = setup_stream(tmp_path, "two")
    with AER(tmp_path / "central", knowledge_dir=tmp_path / "knowledge") as aer:
        other_ack = receive(aer, other_manifest, other_body)
        assert other_ack["runs"][other_local] != remote
        assert (
            aer.get_run(other_ack["runs"][other_local]).metadata["building_project"]["project_id"]
            == other.project_id
        )


def test_reject_scope_order_conflicts_and_gaps(tmp_path):
    project, _, manifest, body = setup_stream(tmp_path)
    with AER(tmp_path / "central") as aer:
        unknown = copy.deepcopy(body)
        unknown["source_id"] = "unknown-device"
        with pytest.raises(ValueError, match="registered"):
            receive(aer, manifest, unknown)
        out_of_order = copy.deepcopy(body)
        out_of_order["items"] = out_of_order["items"][1:]
        with pytest.raises(ValueError, match="Out-of-order"):
            receive(aer, manifest, out_of_order)
        receive(aer, manifest, body)
        conflict = copy.deepcopy(body)
        conflict["items"][0]["payload"]["prompt"] = "changed"
        with pytest.raises(ValueError, match="Conflicting"):
            receive(aer, manifest, conflict)
    project.gap_file.parent.mkdir(parents=True, exist_ok=True)
    project.gap_file.write_text("{}")
    with pytest.raises(ValueError, match="incomplete"):
        batches(project)


def test_interrupted_receipt_blocks_all_distillation(tmp_path):
    _, local, manifest, body = setup_stream(tmp_path)
    with AER(tmp_path / "central") as aer:
        ack = receive(aer, manifest, body)
        path = aer.data_dir / "central-sync" / (ack["stream_key"] + ".json")
        state = json.loads(path.read_bytes())
        state["pending"] = {"sequence": 4, "hash": "unknown"}
        path.write_text(json.dumps(state))
        with pytest.raises(ValueError, match="pending"):
            receive(aer, manifest, body)
        with pytest.raises(DistillationError, match="Central capture"):
            aer.distill_run(ack["runs"][local])


def test_unknown_result_and_unended_source_cannot_distill(tmp_path):
    _, local, manifest, body = setup_stream(tmp_path)
    body["items"] = body["items"][:-1]
    with AER(tmp_path / "central") as aer:
        ack = receive(aer, manifest, body)
        remote = ack["runs"][local]
        assert aer.get_verification_summary(remote).total == 0
        with pytest.raises(DistillationError, match="Central capture"):
            aer.distill_run(remote)


def test_device_binding(tmp_path):
    project, _, _, _ = setup_stream(tmp_path)
    with pytest.raises(ValueError, match="device identity"):
        batches(project.model_copy(update={"sync_source_id": "changed-device"}))


def test_failed_remote_check_does_not_verify(tmp_path):
    project, local, manifest, body = setup_stream(tmp_path)
    registration = json.loads(manifest.read_text())
    registration["projects"][project.project_id]["checks"]["page"]["argv"][-1] = (
        "raise SystemExit(1)"
    )
    manifest.write_text(json.dumps(registration))
    with AER(tmp_path / "central") as aer:
        ack = receive(aer, manifest, body)
        closeout_central(
            aer,
            manifest,
            project.project_id,
            project.task_id,
            "device-1",
            code_version(project)["commit"],
            candidate(),
        )
        assert aer.get_verification_summary(ack["runs"][local]).passed == 0


def test_session_mapping_cannot_change_source_run(tmp_path):
    _, _, manifest, body = setup_stream(tmp_path)
    with AER(tmp_path / "central") as aer:
        prefix = copy.deepcopy(body)
        prefix["items"] = prefix["items"][:1]
        receive(aer, manifest, prefix)
        changed = copy.deepcopy(body)
        changed["items"] = changed["items"][1:2]
        changed["items"][0]["local_run_id"] = "other-run"
        with pytest.raises(ValueError, match="different source run"):
            receive(aer, manifest, changed)
        assert receive(aer, manifest, body)["acknowledged"] == 3


def test_duplicate_terminal_and_closeout_retry(tmp_path):
    project, local, manifest, body = setup_stream(tmp_path)
    capture(project, payload(project, "SessionEnd", reason="exit"))
    assert len(batches(project)[0]["items"]) == 3
    with AER(tmp_path / "central") as aer:
        ack = receive(aer, manifest, body)
        revision = code_version(project)["commit"]
        closeout_central(
            aer, manifest, project.project_id, project.task_id, "device-1", revision, candidate()
        )
        with pytest.raises(ValueError, match="already checked"):
            closeout_central(
                aer,
                manifest,
                project.project_id,
                project.task_id,
                "device-1",
                revision,
                candidate(),
            )
        assert aer.get_verification_summary(ack["runs"][local]).total == 1


def test_parallel_capture_preserves_all_deliveries(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    project = project_at(tmp_path).model_copy(update={"sync_source_id": "device-1"})
    capture(project, payload(project))

    def tool(i):
        return capture(
            project,
            payload(
                project,
                "PreToolUse",
                tool_use_id=f"tool-{i}",
                tool_name="Read",
                tool_input={"path": f"file-{i}"},
            ),
        )

    with ThreadPoolExecutor(max_workers=3) as pool:
        outcomes = list(pool.map(tool, range(6)))
    assert len({result["run_id"] for result in outcomes}) == 1
    assert len(batches(project)[0]["items"]) == 7


def test_redacts_secret_before_outbox(tmp_path):
    project = project_at(tmp_path).model_copy(update={"sync_source_id": "device-1"})
    capture(project, payload(project, prompt="Inspect Authorization: Bearer super-secret-value"))
    body = batches(project)[0]
    assert "super-secret-value" not in json.dumps(body)
