"""Dashboard auth, evidence semantics and non-mutating reads."""

from pathlib import Path

import pytest

pytest.importorskip("starlette")
from starlette.testclient import TestClient

from aer import AER, RunStatus
from aer.dashboard.app import COOKIE, create_app

PASSWORD = "dashboard-test-password"
SECRET = "dashboard-test-signing-secret-123456789"


def test_auth_bounds_and_read_only(tmp_path: Path) -> None:
    with AER(tmp_path) as aer:
        run = aer.start_run(task="<script>alert(1)</script>")
        run.finish(RunStatus.INCONCLUSIVE)
        before = (
            aer.runs.count(),
            aer.events.count_by_run(run.run_id),
            aer.experience_usage.count(),
            aer.retrieval_sessions.count(),
        )
        with TestClient(create_app(aer, PASSWORD, SECRET)) as client:
            assert client.get("/healthz").json() == {"status": "ok"}
            assert client.get("/api/overview").status_code == 401
            assert client.post("/login", json={"password": "wrong"}).status_code == 401
            login = client.post("/login", json={"password": PASSWORD})
            assert login.status_code == 200
            assert "HttpOnly" in login.headers["set-cookie"]
            assert "SameSite=strict" in login.headers["set-cookie"]
            overview = client.get("/api/overview")
            assert overview.json()["recent_verified"] == 0
            assert overview.json()["recent_without_required_checks"] == 1
            assert overview.headers["cache-control"] == "no-store"
            assert "frame-ancestors 'none'" in overview.headers["content-security-policy"]
            item = client.get("/api/runs").json()["items"][0]
            assert item["status"] == "INCONCLUSIVE"
            assert not item["verified_success"]
            assert client.get(f"/api/runs/{run.run_id}").status_code == 200
            assert client.get("/api/experiences").json()["items"] == []
            assert client.get("/api/runs?limit=101").status_code == 400
            assert client.get("/api/runs?offset=-1").status_code == 400
            assert client.get("/api/runs?status=INVENTED").status_code == 400
            assert client.get("/api/runs/missing").status_code == 404
            assert client.post("/api/runs", json={}).status_code == 405
            client.post("/logout")
            assert client.get("/api/overview").status_code == 401
        after = (
            aer.runs.count(),
            aer.events.count_by_run(run.run_id),
            aer.experience_usage.count(),
            aer.retrieval_sessions.count(),
        )
        assert after == before


def test_login_limits_and_assets(tmp_path: Path) -> None:
    with AER(tmp_path) as aer:
        with pytest.raises(ValueError):
            create_app(aer, "short", SECRET)
        with TestClient(create_app(aer, PASSWORD, SECRET)) as client:
            client.cookies.set(COOKIE, "9999999999.forged")
            assert client.get("/api/overview").status_code == 401
            assert (
                client.post(
                    "/login", content="x" * 5000, headers={"content-type": "application/json"}
                ).status_code
                == 413
            )
            assert client.post("/login", data={"password": PASSWORD}).status_code == 415
            for _ in range(9):
                assert client.post("/login", json={"password": "wrong"}).status_code == 401
            assert client.post("/login", json={"password": PASSWORD}).status_code == 429
            assert client.get("/").status_code == 200
            assert client.get("/assets/app.js").status_code == 200
            assert "textContent" in client.get("/assets/app.js").text
            assert "innerHTML" not in client.get("/assets/app.js").text
            assert client.get("/assets/secret.txt").status_code == 404


def test_verified_and_source_evidence(tmp_path: Path) -> None:
    from aer import CallableDistillationProvider, ExperienceCandidate, PredicateVerifier

    candidate = ExperienceCandidate(
        domain="fixture",
        title="Renderer fix",
        problem="Broken output",
        solution="Escape output",
        failed_attempts=("Raw output failed",),
    )
    with AER(
        tmp_path, distillation_provider=CallableDistillationProvider(lambda _: candidate)
    ) as aer:
        good = aer.start_run(task="renderer fix")
        good.finish(RunStatus.INCONCLUSIVE)
        aer.verify(good.run_id, PredicateVerifier(name="fixture", predicate=lambda _: True))
        experience = good.distill(explicit_high_value=True)
        assert experience is not None
        bad = aer.start_run(task="failing fixture")
        bad.success()
        aer.verify(bad.run_id, PredicateVerifier(name="fixture", predicate=lambda _: False))
        with TestClient(create_app(aer, PASSWORD, SECRET)) as client:
            client.post("/login", json={"password": PASSWORD})
            assert client.get("/api/overview").json()["recent_verified"] == 1
            assert not client.get(f"/api/runs/{bad.run_id}").json()["verified_success"]
            detail = client.get(f"/api/experiences/{experience.id}").json()
            assert detail["source_run_ids"] == [good.run_id]
            assert detail["usage"] == []
            assert aer.experience_usage.count() == 0


def test_cli_uses_configured_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    import uvicorn

    from aer.dashboard.__main__ import main

    monkeypatch.setenv("AER_DATA_DIR", str(tmp_path / "store"))
    monkeypatch.setenv("AER_DB_PATH", str(tmp_path / "override" / "custom.db"))
    monkeypatch.setenv("AER_KNOWLEDGE_DIR", str(tmp_path / "index"))
    monkeypatch.setenv("AER_DASHBOARD_PASSWORD", PASSWORD)
    monkeypatch.setenv("AER_DASHBOARD_SESSION_SECRET", SECRET)
    monkeypatch.setattr(sys, "argv", ["dashboard"])
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: None)
    main()
    assert (tmp_path / "override" / "custom.db").is_file()
    assert not (tmp_path / "store" / "aer.db").exists()
