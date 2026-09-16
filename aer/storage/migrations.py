"""Programmatic Alembic entry point.

AER has exactly **one** schema-history mechanism: Alembic revisions under
``migrations/versions``. Both entry points funnel through here:

* the CLI -- ``alembic upgrade head`` (reads ``alembic.ini`` directly);
* the embedded runtime -- :class:`~aer.storage.database.Database` calls
  :func:`upgrade_to_head` on construction.

Keeping a single path means a test that opens ``AER(path)`` exercises the same
schema provisioning that production uses; ``Base.metadata.create_all()`` is no
longer part of it (it survives only as the *source of truth for autogenerate*).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, event

from aer.exceptions import StorageError
from aer.storage.connection import apply_sqlite_pragmas, sqlite_url

#: Key under which the target database path is handed to ``migrations/env.py``.
DB_PATH_ATTRIBUTE = "db_path"
#: Setting this to ``True`` lets ``migrations/env.py`` reconfigure logging from
#: ``alembic.ini``. Off by default: AER migrates on every ``AER(...)``
#: construction, so logging setup belongs to the caller, not to the runtime.
CONFIGURE_LOGGER_ATTRIBUTE = "configure_logger"


def _repository_root() -> Path:
    """Locate the directory containing ``alembic.ini``.

    Walks up from this module so it works for a source checkout and for an
    editable install, without hard-coding an absolute path.
    """
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "alembic.ini").is_file():
            return candidate
    raise StorageError(
        "Cannot locate alembic.ini: AER's migration scripts are missing from the "
        "installation. Reinstall the package or run from a source checkout."
    )


@lru_cache(maxsize=1)
def _head_revision() -> str:
    """The revision ``alembic upgrade head`` resolves to.

    Scripts are read once per process; they are immutable at runtime.
    """
    config = alembic_config(Path("."))
    head = ScriptDirectory.from_config(config).get_current_head()
    if head is None:
        raise StorageError("The migration history is empty: no revisions found.")
    return head


def alembic_config(db_path: str | Path, *, configure_logger: bool = False) -> Config:
    """Build an Alembic :class:`Config` targeting ``db_path``."""
    root = _repository_root()
    config = Config(str(root / "alembic.ini"))
    # Set explicitly so the config also works when the process CWD is elsewhere.
    config.set_main_option("script_location", str(root / "migrations"))
    config.attributes[DB_PATH_ATTRIBUTE] = str(db_path)
    config.attributes[CONFIGURE_LOGGER_ATTRIBUTE] = configure_logger
    return config


def upgrade_to_head(db_path: str | Path) -> None:
    """Bring the database at ``db_path`` up to the latest revision.

    Safe to call on every startup, and cheap when there is nothing to do: a
    database already recording the head revision short-circuits before Alembic is
    invoked, which matters because ``AER(...)`` constructs a ``Database`` (and
    therefore migrates) on every instantiation.

    Also safe on a database created before Alembic was adopted -- revision ``0001``
    is idempotent (see its module docstring) and such a database simply has no
    ``alembic_version`` row yet.

    Raises:
        StorageError: the migration history could not be read or applied. The
            original Alembic/SQLAlchemy failure is preserved as ``__cause__``.
    """
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    if is_at_head(path):
        return

    try:
        command.upgrade(alembic_config(path), "head")
    except StorageError:
        raise
    except Exception as exc:  # alembic raises a wide variety of error types
        raise StorageError(f"Migrating {path.as_posix()} to head failed: {exc}") from exc


def head_revision() -> str:
    """Identifier of the newest revision in the migration history."""
    return _head_revision()


def current_revision(db_path: str | Path) -> str | None:
    """Revision currently recorded in the database, or ``None`` when unmigrated."""
    path = Path(db_path)
    if not path.is_file():
        return None

    engine = create_engine(sqlite_url(path))
    event.listen(engine, "connect", apply_sqlite_pragmas)
    try:
        with engine.connect() as connection:
            return MigrationContext.configure(connection).get_current_revision()
    except Exception as exc:
        raise StorageError(
            f"Reading the schema revision of {path.as_posix()} failed: {exc}"
        ) from exc
    finally:
        engine.dispose()


def is_at_head(db_path: str | Path) -> bool:
    """Whether the database already records the newest revision."""
    return current_revision(db_path) == _head_revision()
