"""Resident surfaces are opt-in and reachable only through the host loopback."""

from pathlib import Path

import yaml


def test_resident_services_preserve_runtime_and_limit_exposure() -> None:
    root = Path(__file__).resolve().parents[2]
    original = yaml.safe_load((root / "deploy/compose.yaml").read_text())
    assert list(original["services"]) == ["aer-runtime"]
    services = yaml.safe_load((root / "deploy/compose.services.yaml").read_text())["services"]
    assert set(services) == {"aer-dashboard", "aer-mcp"}
    for service in services.values():
        assert service["extends"] == {"file": "compose.yaml", "service": "aer-runtime"}
        assert service["restart"] == "unless-stopped"
        assert service["env_file"] == [".env", "services.env"]
        assert all(port.startswith("127.0.0.1:") for port in service["ports"])
        assert service["mem_limit"] and service["pids_limit"] == 128
    assert services["aer-dashboard"]["ports"] == ["127.0.0.1:8788:8788"]
    assert services["aer-mcp"]["ports"] == ["127.0.0.1:18765:8765"]
