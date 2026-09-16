"""Tests for the minimal deployment configuration (``aer/config.py``).

The loader is small on purpose, but every branch of it decides *which database a
process opens*, so each one is pinned by a test. The dangerous cases are the quiet
ones: an empty environment variable, or a relative path in production, both of
which look like a working deployment right up until the data turns out to be
somewhere else.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from aer import ConfigurationError, DeploymentConfig, load_deployment_config
from aer.config import (
    DATABASE_FILENAME,
    DEFAULT_BACKUP_RETENTION,
    DEFAULT_DATA_DIR,
    DEFAULT_ENVIRONMENT,
)

PRODUCTION_ENV = {
    "AER_ENV": "production",
    "AER_DATA_DIR": "/data",
    "AER_ARTIFACT_DIR": "/artifacts",
    "AER_KNOWLEDGE_DIR": "/knowledge",
    "AER_BACKUP_DIR": "/backups",
}


class TestDefaults:
    def test_nothing_set_reproduces_the_historical_defaults(self) -> None:
        config = load_deployment_config({})

        assert config.environment == DEFAULT_ENVIRONMENT
        assert config.data_dir == Path(DEFAULT_DATA_DIR)
        # Same file `alembic.ini` documents, so the CLI and the runtime agree.
        assert config.db_path == Path(DEFAULT_DATA_DIR) / DATABASE_FILENAME
        assert config.backup_retention == DEFAULT_BACKUP_RETENTION
        assert config.log_level == "INFO"
        assert config.is_production is False

    def test_the_container_layout_resolves_as_documented(self) -> None:
        config = load_deployment_config(PRODUCTION_ENV)

        assert config.is_production is True
        assert config.db_path == Path("/data") / DATABASE_FILENAME
        assert config.backup_dir == Path("/backups")

    def test_an_empty_variable_counts_as_unset(self) -> None:
        """``AER_DATA_DIR=`` in a compose file must not create a database at ``/``."""
        config = load_deployment_config({"AER_DATA_DIR": "", "AER_BACKUP_RETENTION": "  "})

        assert config.data_dir == Path(DEFAULT_DATA_DIR)
        assert config.backup_retention == DEFAULT_BACKUP_RETENTION

    def test_whitespace_is_trimmed(self) -> None:
        config = load_deployment_config({"AER_DATA_DIR": "  /data  ", "AER_LOG_LEVEL": " debug "})

        assert config.data_dir == Path("/data")
        assert config.log_level == "DEBUG"


class TestDatabasePath:
    def test_aer_db_path_names_the_file_and_wins_over_the_directory(self) -> None:
        config = load_deployment_config({"AER_DATA_DIR": "/data", "AER_DB_PATH": "/data/other.db"})

        assert config.db_path == Path("/data/other.db")
        # The data directory is still reported as configured: the file override does
        # not silently redefine where artifacts live.
        assert config.data_dir == Path("/data")


class TestProductionPathRules:
    """Relative paths are the container footgun that loses a database."""

    def test_production_requires_absolute_paths(self) -> None:
        with pytest.raises(ConfigurationError, match="requires absolute paths"):
            load_deployment_config({"AER_ENV": "production"})

    def test_the_error_names_every_relative_variable(self) -> None:
        with pytest.raises(ConfigurationError) as excinfo:
            load_deployment_config(
                {"AER_ENV": "production", "AER_DATA_DIR": "data", "AER_BACKUP_DIR": "backups"}
            )

        message = str(excinfo.value)
        assert "AER_DATA_DIR" in message
        assert "AER_BACKUP_DIR" in message

    def test_development_tolerates_relative_paths(self) -> None:
        config = load_deployment_config({"AER_DATA_DIR": "data"})

        assert config.data_dir == Path("data")

    def test_container_paths_count_as_absolute_even_on_windows(self) -> None:
        """``/data`` is absolute for the *deployment target*, whatever the host says.

        ``pathlib.Path("/data").is_absolute()`` is False on Windows; judging a
        production config there would reject a correct file and teach people to
        weaken the check.
        """
        config = load_deployment_config(PRODUCTION_ENV)

        assert config.data_dir == Path("/data")
        assert config.is_production is True


class TestValidation:
    def test_an_unknown_log_level_is_rejected(self) -> None:
        with pytest.raises(ConfigurationError, match="not a standard logging level"):
            load_deployment_config({"AER_LOG_LEVEL": "chatty"})

    def test_the_level_is_case_insensitive(self) -> None:
        assert load_deployment_config({"AER_LOG_LEVEL": "warning"}).log_level == "WARNING"

    @pytest.mark.parametrize("value", ["0", "-1", "many"])
    def test_retention_must_be_a_positive_integer(self, value: str) -> None:
        """Zero would mean "delete every backup", which an operator never means."""
        with pytest.raises(ConfigurationError):
            load_deployment_config({"AER_BACKUP_RETENTION": value})

    def test_retention_is_configurable(self) -> None:
        assert load_deployment_config({"AER_BACKUP_RETENTION": "5"}).backup_retention == 5


class TestShape:
    def test_the_config_is_frozen(self) -> None:
        config = load_deployment_config({})

        with pytest.raises(FrozenInstanceError):
            config.data_dir = Path("/somewhere-else")  # type: ignore[misc]

    def test_describe_is_safe_to_log(self) -> None:
        description = load_deployment_config(PRODUCTION_ENV).describe()

        assert description["environment"] == "production"
        assert description["db_path"] == "/data/aer.db"
        # Nothing that could be a credential: the loader reads locations only.
        forbidden = ("token", "secret", "password", "key", "credential")
        assert not [field for field in description if any(word in field for word in forbidden)]

    def test_the_result_is_a_deployment_config(self) -> None:
        assert isinstance(load_deployment_config({}), DeploymentConfig)
