"""Transport, trust boundaries and real-index reuse acceptance."""

import hashlib
import json
import os
import sys

import pytest

pytest.importorskip("mcp")
import anyio
from mcp.client.stdio import stdio_client
from mcp.server.fastmcp.exceptions import ToolError
from starlette.testclient import TestClient

from aer import AER
from aer.config import load_deployment_config
from aer.mcp import BearerAuth, create_server, file_verifiers
from mcp import ClientSession, StdioServerParameters


def config(tmp_path):
    return load_deployment_config(
        {
            "AER_DATA_DIR": str(tmp_path / "data"),
            "AER_KNOWLEDGE_DIR": str(tmp_path / "knowledge"),
        }
    )


def manifest(tmp_path):
    target = tmp_path / "result.txt"
    target.write_text("actual result", encoding="utf-8")
    owner_config = tmp_path / "checks.json"
    owner_config.write_text(
        json.dumps(
            {
                "result_sha": {
                    "path": str(target),
                    "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                }
            }
        ),
        encoding="utf-8",
    )
    return owner_config, target


async def call(server, name, **arguments):
    result = await server.call_tool(name, arguments)
    if isinstance(result, tuple):
        return json.loads(result[0][0].text)
    if isinstance(result, dict):
        return result
    return json.loads(result[0].text)


def test_stdio_real_process_and_restart(tmp_path):
    env = {
        **os.environ,
        "AER_DATA_DIR": str(tmp_path / "data"),
        "AER_KNOWLEDGE_DIR": str(tmp_path / "knowledge"),
    }
    parameters = StdioServerParameters(command=sys.executable, args=["-m", "aer.mcp"], env=env)

    async def scenario():
        async with (
            stdio_client(parameters) as (reader, writer),
            ClientSession(reader, writer) as session,
        ):
            await session.initialize()
            tools = await session.list_tools()
            assert len(tools.tools) == 13
            started = await session.call_tool("aer_start_run", {"task": "real transport"})
            run_id = json.loads(started.content[0].text)["id"]
            forged = await session.call_tool(
                "aer_record_event",
                {
                    "run_id": run_id,
                    "event_type": "VERIFICATION",
                    "payload": {"passed": True},
                },
            )
            assert forged.isError
            ended = await session.call_tool(
                "aer_finish_run", {"run_id": run_id, "status": "SUCCESS"}
            )
            assert not ended.isError
        async with (
            stdio_client(parameters) as (reader, writer),
            ClientSession(reader, writer) as session,
        ):
            await session.initialize()
            result = await session.call_tool("aer_get_run", {"run_id": run_id})
            assert json.loads(result.content[0].text)["verified_success"] is False

    anyio.run(scenario)


def test_http_auth_and_protocol(tmp_path):
    server = create_server(config(tmp_path))
    token = "x" * 40
    with TestClient(
        BearerAuth(server.streamable_http_app(), token), base_url="http://127.0.0.1:8765"
    ) as client:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"},
            },
        }
        headers = {"Accept": "application/json, text/event-stream"}
        assert client.post("/mcp", json=payload, headers=headers).status_code == 401
        assert (
            client.post(
                "/mcp", json=payload, headers={**headers, "Authorization": "Bearer wrong"}
            ).status_code
            == 401
        )
        response = client.post(
            "/mcp", json=payload, headers={**headers, "Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 200
        assert response.json()["result"]["serverInfo"]["name"] == "AER"
    with pytest.raises(ValueError, match="32"):
        BearerAuth(server.streamable_http_app(), "short")


def test_redaction_terminal_guards_and_named_checks(tmp_path):
    cfg = config(tmp_path)
    check_file, target = manifest(tmp_path)
    server = create_server(cfg, verifiers=file_verifiers(check_file))

    async def scenario():
        started = await call(server, "aer_start_run", task="test password=secret123")
        run_id = started["id"]
        assert "secret123" not in json.dumps(started)
        event = await call(
            server,
            "aer_record_event",
            run_id=run_id,
            event_type="TOOL_RESULT",
            payload={"api_key": "private", "chain_of_thought": "hidden"},
        )
        assert "private" not in json.dumps(event)
        assert "hidden" not in json.dumps(event)
        with pytest.raises(ToolError, match="Finish"):
            await call(server, "aer_verify_run", run_id=run_id, check_name="result_sha")
        await call(server, "aer_finish_run", run_id=run_id, status="SUCCESS")
        # Safe retry does not create another end event.
        await call(server, "aer_finish_run", run_id=run_id, status="SUCCESS")
        with pytest.raises(ToolError, match="Unknown"):
            await call(server, "aer_verify_run", run_id=run_id, check_name="arbitrary-command")
        with pytest.raises(ToolError):
            await call(
                server, "aer_record_event", run_id=run_id, event_type="TOOL_CALL", payload={}
            )
        verdict = await call(server, "aer_verify_run", run_id=run_id, check_name="result_sha")
        assert verdict["passed"] is True
        target.write_text("wrong", encoding="utf-8")
        verdict = await call(server, "aer_verify_run", run_id=run_id, check_name="result_sha")
        assert verdict["passed"] is False

    anyio.run(scenario)
    with AER(cfg.data_dir) as aer:
        assert (
            sum(e.event_type.value == "TASK_END" for e in aer.get_events(aer.list_runs()[0].id))
            == 1
        )


def test_real_index_recovery_reuse(tmp_path):
    pytest.importorskip("neug")
    cfg = config(tmp_path)
    check_file, _ = manifest(tmp_path)
    server = create_server(cfg, verifiers=file_verifiers(check_file))

    async def scenario():
        first = (await call(server, "aer_start_run", task="WordPress permission repair"))["id"]
        error = await call(
            server,
            "aer_record_failure",
            run_id=first,
            error_type="PermissionError",
            message="missing edit_posts",
        )
        recovery = await call(
            server,
            "aer_start_recovery",
            run_id=first,
            reason="grant edit_posts",
            error_id=error["id"],
        )
        await call(
            server,
            "aer_finish_recovery",
            run_id=first,
            recovery_id=recovery["id"],
            success=True,
            outcome={"summary": "grant edit_posts"},
        )
        await call(server, "aer_finish_run", run_id=first, status="SUCCESS")
        await call(server, "aer_verify_run", run_id=first, check_name="result_sha")
        candidate = {
            "domain": "wordpress",
            "title": "WordPress permission repair",
            "problem": "WordPress edit_posts permission denied",
            "solution": "grant edit_posts",
            "root_cause": "missing permission",
        }
        experience = (await call(server, "aer_distill_run", run_id=first, candidate=candidate))[
            "experience"
        ]
        assert experience["kind"] == "RECOVERY"
        assert experience["outcome_verified"] is True
        repeated = await call(server, "aer_distill_run", run_id=first, candidate=candidate)
        assert repeated["experience"]["id"] == experience["id"]
        second = (await call(server, "aer_start_run", task="WordPress permission repair"))["id"]
        found = await call(server, "aer_retrieve", query="WordPress permission", run_id=second)
        assert found["injection_recorded"] is False
        assert "grant edit_posts" in found["context"]
        session_id = found["session_id"]
        with AER(cfg.data_dir) as aer:
            assert aer.get_session_usage(session_id)[0].is_injected is False
        await call(
            server,
            "aer_acknowledge_injection",
            session_id=session_id,
            experience_ids=[experience["id"]],
        )
        await call(
            server,
            "aer_record_adoption",
            session_id=session_id,
            experience_id=experience["id"],
            signal="ADOPTED",
        )
        await call(server, "aer_finish_run", run_id=second, status="SUCCESS")
        await call(server, "aer_verify_run", run_id=second, check_name="result_sha")
        # Reconstruct server: no in-memory run handles are required.
        reopened = create_server(cfg)
        result = await call(reopened, "aer_get_run", run_id=second)
        assert result["verified_success"] is True
        with AER(cfg.data_dir) as aer:
            report = aer.experience_effectiveness(experience["id"])
            assert report.explicit_adoption_count == 1
            assert report.verified_success_runs == 1

    anyio.run(scenario)
