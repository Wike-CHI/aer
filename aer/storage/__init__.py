"""AER storage layer: SQLite connection, migrations, ORM tables, repositories.

Import surface for the layer:

* :func:`~aer.storage.migrations.upgrade_to_head` -- the only way the schema is
  provisioned;
* :class:`~aer.storage.database.Database` -- engine, pragmas, sessions;
* repositories -- the only sanctioned way to read or write AER data.
"""

from aer.storage.connection import SQLITE_PRAGMAS, apply_sqlite_pragmas, sqlite_url
from aer.storage.database import Database, storage_errors
from aer.storage.migrations import current_revision, head_revision, upgrade_to_head
from aer.storage.repositories import (
    ErrorRepository,
    EventRepository,
    ExperienceRepository,
    ExperienceSourceRepository,
    RecoveryRepository,
    RunRepository,
    VerificationRepository,
)

__all__ = [
    "SQLITE_PRAGMAS",
    "Database",
    "ErrorRepository",
    "EventRepository",
    "ExperienceRepository",
    "ExperienceSourceRepository",
    "RecoveryRepository",
    "RunRepository",
    "VerificationRepository",
    "apply_sqlite_pragmas",
    "current_revision",
    "head_revision",
    "sqlite_url",
    "storage_errors",
    "upgrade_to_head",
]
