"""Deployment configuration: the minimum environment surface AER needs to run.

This module exists because AER has to be startable in three places without three
code paths -- a developer checkout, a CI runner, and a container with mounted
volumes. The only thing that differs between them is *where the data lives*, so
that is the only thing this module reads.

Deliberately **not** a settings framework: no files, no precedence rules, no
validation DSL, no dependency. Just environment variables, defaults, and a
handful of checks that are cheap to reason about at 3am during a rollback.

Path resolution:

* ``AER_DB_PATH`` -- an exact database **file**. This name predates this module:
  it is already the contract ``alembic.ini`` and ``migrations/env.py`` resolve
  against, so deployment reuses it instead of inventing a second one.
* ``AER_DATA_DIR`` -- the directory the database lives in. Used when
  ``AER_DB_PATH`` is not set, yielding ``<data_dir>/aer.db``.
* ``AER_ARTIFACT_DIR`` / ``AER_KNOWLEDGE_DIR`` / ``AER_BACKUP_DIR`` -- the other
  three persistent directories. ``knowledge`` is reserved for a later milestone
  and stays empty; ``backups`` is deliberately outside ``data`` so a backup does
  not die with the database it protects.

An empty string counts as unset. ``AER_DATA_DIR=`` is how a container ends up
writing to ``/app/data`` instead of the mounted volume, and silently defaulting
would turn that typo into data loss; treating it as unset keeps the documented
default in force.

In ``AER_ENV=production`` every path must be **absolute**. Relative paths are a
container footgun: they resolve against the working directory, which is the
image, not the volume. Requiring absolute paths makes the mistake a startup
error instead of a second, empty database that quietly replaces the real one.

"Absolute" is checked under **either** the host's semantics or POSIX semantics,
because the paths being validated describe the deployment target, not the machine
doing the validating. ``/data`` is a perfectly absolute container path, yet
``pathlib.Path("/data").is_absolute()`` is ``False`` on Windows; judging it there
would reject a correct production config and, worse, teach people to weaken the
check.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from aer.exceptions import ConfigurationError

__all__ = [
    "DATABASE_FILENAME",
    "DEFAULT_ARTIFACT_DIR",
    "DEFAULT_BACKUP_DIR",
    "DEFAULT_BACKUP_RETENTION",
    "DEFAULT_DATA_DIR",
    "DEFAULT_ENVIRONMENT",
    "DEFAULT_KNOWLEDGE_DIR",
    "DEFAULT_LOG_LEVEL",
    "ENVIRONMENT_PRODUCTION",
    "DeploymentConfig",
    "load_deployment_config",
]

#: Default environment when ``AER_ENV`` is unset.
DEFAULT_ENVIRONMENT = "development"
#: The one environment name that switches on the stricter path rules.
ENVIRONMENT_PRODUCTION = "production"

#: Matches ``AER()``'s historical default data directory and ``alembic.ini``'s
#: documented ``./data/aer.db``, so behaviour is unchanged for existing callers.
DEFAULT_DATA_DIR = "data"
DEFAULT_ARTIFACT_DIR = "artifacts"
#: Reserved for the future knowledge index (NeuG). Nothing reads it yet; the
#: directory is created so the volume mount point exists from day one.
DEFAULT_KNOWLEDGE_DIR = "knowledge"
#: Where ``scripts/backup_sqlite.py`` writes. Deliberately *not* under the data
#: directory: a backup stored beside the database it protects dies with it.
DEFAULT_BACKUP_DIR = "backups"
#: How many timestamped backups to keep. 20 deployments is roughly a month of
#: dailies: large enough to survive a bad release discovered late, small enough
#: that nobody has to think about disk.
DEFAULT_BACKUP_RETENTION = 20
DEFAULT_LOG_LEVEL = "INFO"

#: Fixed instead of configurable: the database filename is part of the backup
#: naming convention (``aer-<timestamp>-<sha>.db``) and the documented layout.
DATABASE_FILENAME = "aer.db"

#: Accepted ``AER_LOG_LEVEL`` values. An explicit set rather than
#: ``logging.getLevelNamesMapping()`` so a typo fails here, deterministically,
#: instead of being accepted and then ignored by the logging configuration.
_LOG_LEVELS = frozenset({"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"})


@dataclass(frozen=True, slots=True)
class DeploymentConfig:
    """Where this AER process reads and writes.

    Frozen: configuration is resolved once at startup and never edited in place,
    so code that receives a config can treat it as a constant.
    """

    environment: str
    data_dir: Path
    artifact_dir: Path
    knowledge_dir: Path
    backup_dir: Path
    backup_retention: int
    log_level: str
    db_path: Path

    @property
    def is_production(self) -> bool:
        """Whether this process believes it is serving real data."""
        return self.environment == ENVIRONMENT_PRODUCTION

    def describe(self) -> dict[str, str]:
        """A log-safe summary.

        Contains no credentials by construction -- :func:`load_deployment_config`
        reads only locations and a log level, never a secret, so this mapping can
        be printed during a deployment without leaking anything.
        """
        return {
            "environment": self.environment,
            "data_dir": self.data_dir.as_posix(),
            "db_path": self.db_path.as_posix(),
            "artifact_dir": self.artifact_dir.as_posix(),
            "knowledge_dir": self.knowledge_dir.as_posix(),
            "backup_dir": self.backup_dir.as_posix(),
            "backup_retention": str(self.backup_retention),
            "log_level": self.log_level,
        }


def _read(environ: Mapping[str, str], name: str, default: str) -> str:
    """Environment value for ``name``, treating empty/whitespace as unset."""
    raw = environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip()


def _is_absolute_for_deployment(path: Path) -> bool:
    """Whether ``path`` is absolute for the deployment target *or* this host."""
    if path.is_absolute():
        return True
    return PurePosixPath(path.as_posix()).is_absolute()


def load_deployment_config(environ: Mapping[str, str] | None = None) -> DeploymentConfig:
    """Build the deployment configuration from ``environ`` (defaults to ``os.environ``).

    ``environ`` is an explicit parameter so tests can exercise every branch --
    including the production path rules -- without mutating the real process
    environment.

    Raises:
        ConfigurationError: an unreadable log level, or a relative path while
            ``AER_ENV=production``.
    """
    source = os.environ if environ is None else environ

    environment = _read(source, "AER_ENV", DEFAULT_ENVIRONMENT)
    data_dir = Path(_read(source, "AER_DATA_DIR", DEFAULT_DATA_DIR))
    artifact_dir = Path(_read(source, "AER_ARTIFACT_DIR", DEFAULT_ARTIFACT_DIR))
    knowledge_dir = Path(_read(source, "AER_KNOWLEDGE_DIR", DEFAULT_KNOWLEDGE_DIR))
    backup_dir = Path(_read(source, "AER_BACKUP_DIR", DEFAULT_BACKUP_DIR))
    log_level = _read(source, "AER_LOG_LEVEL", DEFAULT_LOG_LEVEL).upper()

    if log_level not in _LOG_LEVELS:
        raise ConfigurationError(
            f"AER_LOG_LEVEL={log_level!r} is not a standard logging level; "
            f"expected one of {', '.join(sorted(_LOG_LEVELS))}"
        )

    retention_raw = _read(source, "AER_BACKUP_RETENTION", str(DEFAULT_BACKUP_RETENTION))
    try:
        backup_retention = int(retention_raw)
    except ValueError as exc:
        raise ConfigurationError(
            f"AER_BACKUP_RETENTION={retention_raw!r} is not an integer"
        ) from exc
    if backup_retention < 1:
        # Zero would mean "delete every backup", which is never what an operator
        # means. Refuse rather than silently disabling the safety net.
        raise ConfigurationError(
            f"AER_BACKUP_RETENTION={backup_retention} must be at least 1; "
            "refusing to run with backups effectively disabled"
        )

    db_override = _read(source, "AER_DB_PATH", "")
    db_path = Path(db_override) if db_override else data_dir / DATABASE_FILENAME

    config = DeploymentConfig(
        environment=environment,
        data_dir=data_dir,
        artifact_dir=artifact_dir,
        knowledge_dir=knowledge_dir,
        backup_dir=backup_dir,
        backup_retention=backup_retention,
        log_level=log_level,
        db_path=db_path,
    )

    if config.is_production:
        relative = [
            name
            for name, path in (
                ("AER_DATA_DIR", data_dir),
                ("AER_ARTIFACT_DIR", artifact_dir),
                ("AER_KNOWLEDGE_DIR", knowledge_dir),
                ("AER_BACKUP_DIR", backup_dir),
                ("AER_DB_PATH", db_path),
            )
            if not _is_absolute_for_deployment(path)
        ]
        if relative:
            raise ConfigurationError(
                "AER_ENV=production requires absolute paths; these are relative: "
                f"{', '.join(relative)}. A relative path resolves against the "
                "working directory (the image), not the mounted volume."
            )

    return config
