"""How AER opens a SQLite file: the connection URL and the PRAGMA hook.

Split out of :mod:`aer.storage.database` so that the runtime engine and the Alembic
environment can share it **without importing each other**:

.. code-block:: text

    connection.py                 <- URL + PRAGMA, no dependencies
        ^              ^
    database.py     migrations.py <- each depends on connection.py only
        ^              ^
        +------- AER --+

Without this split, ``Database`` (which provisions the schema) and
``aer.storage.migrations`` (which knows how to provision it) would import each
other.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import URL

#: PRAGMA configuration required by agent.md #7 and the technical design #4.
#: Applied on **every** new DBAPI connection -- SQLite scopes most PRAGMAs
#: (``foreign_keys`` in particular) to the connection, so they cannot be set once
#: for the lifetime of the engine.
SQLITE_PRAGMAS: tuple[tuple[str, str], ...] = (
    ("journal_mode", "WAL"),
    ("foreign_keys", "ON"),
    ("synchronous", "NORMAL"),
    ("busy_timeout", "5000"),
)


def sqlite_url(path: str | Path) -> URL:
    """Build the SQLAlchemy URL for a SQLite database file.

    A POSIX-style absolute path is the portable way to hand Windows drive letters
    to SQLAlchemy's SQLite URL parsing. This is the single place where AER decides
    how a filesystem path becomes a connection URL.
    """
    return URL.create("sqlite+pysqlite", database=Path(path).as_posix())


def apply_sqlite_pragmas(dbapi_connection: Any, connection_record: Any) -> None:
    """Connection hook that configures a freshly opened SQLite connection.

    Used as a SQLAlchemy ``connect`` event listener by both the runtime engine and
    ``migrations/env.py``, so migrations run under the same journal,
    foreign-key, sync and lock-wait settings as normal traffic.
    """
    cursor = dbapi_connection.cursor()
    try:
        for pragma, value in SQLITE_PRAGMAS:
            cursor.execute(f"PRAGMA {pragma}={value}")
    finally:
        cursor.close()
