"""SQLite engine and session management (Milestone 2 Task 2.1, revised in M3).

``Database`` is the only place in AER that owns the SQLAlchemy engine and the
session/transaction lifecycle. Repositories depend on it; nothing above the
storage layer does.

Deliberate constraints:

* SQLite only -- no PostgreSQL, no Redis, no external service;
* synchronous API, embedded in the caller's Python process;
* the schema is provisioned by **Alembic**, never by ``create_all``.

Constructing a ``Database`` therefore means "give me a path, get a usable AER
store": the migration history is applied on the spot, so there is no way to end up
with an engine pointing at a half-created schema. ``Base.metadata`` is still the
source of truth, but only as the input to Alembic autogenerate.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event, inspect
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from aer.exceptions import StorageError
from aer.storage.connection import SQLITE_PRAGMAS, apply_sqlite_pragmas, sqlite_url
from aer.storage.migrations import current_revision, upgrade_to_head

__all__ = ["SQLITE_PRAGMAS", "Database", "storage_errors"]


@contextmanager
def storage_errors(action: str) -> Iterator[None]:
    """Translate ORM/driver failures into :class:`~aer.exceptions.StorageError`.

    The original exception is preserved as ``__cause__`` (``raise ... from exc``),
    so the driver-level message and traceback are never lost. ``AERError``
    subclasses raised inside the block (e.g. ``RecordNotFoundError``) pass through
    untouched.
    """
    try:
        yield
    except SQLAlchemyError as exc:
        raise StorageError(f"{action} failed: {exc}") from exc


class Database:
    """An embedded SQLite database holding the AER store."""

    def __init__(self, path: str | Path, *, echo: bool = False, migrate: bool = True) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

        if migrate:
            upgrade_to_head(self._path)

        self._engine = create_engine(sqlite_url(self._path), echo=echo, future=True)
        event.listen(self._engine, "connect", apply_sqlite_pragmas)

        self._session_factory = sessionmaker(
            bind=self._engine,
            expire_on_commit=False,
            future=True,
        )

    # -- introspection -----------------------------------------------------

    @property
    def path(self) -> Path:
        """Filesystem location of the SQLite database file."""
        return self._path

    @property
    def engine(self) -> Engine:
        """The underlying SQLAlchemy engine (storage-layer use only)."""
        return self._engine

    def schema_revision(self) -> str | None:
        """Revision currently recorded in the database, or ``None`` when unmigrated."""
        return current_revision(self._path)

    def table_names(self) -> list[str]:
        """Names of the tables that currently exist, sorted."""
        with storage_errors("inspect database schema"):
            return sorted(inspect(self._engine).get_table_names())

    def read_pragmas(self) -> dict[str, object]:
        """Read back the pragmas actually in force on a fresh connection.

        Used by diagnostics and tests: a PRAGMA silently ignored by SQLite (for
        example ``journal_mode`` on an unsupported filesystem) would otherwise go
        unnoticed.
        """
        values: dict[str, object] = {}
        with storage_errors("read sqlite pragmas"), self._engine.connect() as connection:
            for pragma, _ in SQLITE_PRAGMAS:
                values[pragma] = connection.exec_driver_sql(f"PRAGMA {pragma}").scalar()
        return values

    # -- connection / session lifecycle ------------------------------------

    @contextmanager
    def session(self, action: str = "database operation") -> Iterator[Session]:
        """Yield a session inside a transaction, translating driver failures.

        Commits on clean exit, rolls back on **any** exception, always closes the
        session, and converts ORM/driver errors into
        :class:`~aer.exceptions.StorageError` with ``action`` as context. The original
        exception is preserved as ``__cause__``.

        ``AERError`` (for example ``RecordNotFoundError``) passes through untouched.

        This is storage-internal API: business code must go through a repository,
        never through a raw session.
        """
        session = self._session_factory()
        try:
            yield session
            session.commit()
        except SQLAlchemyError as exc:
            session.rollback()
            raise StorageError(f"{action} failed: {exc}") from exc
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()

    def dispose(self) -> None:
        """Close every pooled connection and release the database file.

        After a clean dispose, SQLite checkpoints and removes the ``-wal`` file,
        which is what makes the restart-persistence guarantee observable.
        """
        with storage_errors("dispose database"):
            self._engine.dispose()

    def __repr__(self) -> str:
        return f"Database(path={self._path.as_posix()!r})"
