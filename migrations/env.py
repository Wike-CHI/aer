"""Alembic environment for AER.

Responsibilities (and nothing more):

* resolve which SQLite file to migrate (see ``alembic.ini`` for the order);
* apply the same PRAGMA configuration the runtime uses, so migrations run under
  identical journal/foreign-key settings as normal traffic;
* expose :data:`aer.storage.models.Base.metadata` to autogenerate.

SQLite cannot ``ALTER TABLE`` most things in place, so ``render_as_batch=True`` is
enabled: future migrations that change a column are rewritten as create-new-table
/ copy / drop-old-table, which is the only safe way to evolve SQLite schemas.
"""

from __future__ import annotations

import os
from logging.config import fileConfig
from pathlib import Path
from typing import Any

from alembic import context
from sqlalchemy import create_engine, event, pool

from aer.storage.connection import apply_sqlite_pragmas, sqlite_url
from aer.storage.models import Base, UTCDateTime

config = context.config

# Programmatic migrations (aer.storage.migrations) disable this: AER migrates on
# every ``AER(...)`` construction, and re-configuring logging there would both
# spam the caller's console and stomp on their logging setup. The CLI keeps it.
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

#: Default database location, matching ``AER()``'s default data directory.
DEFAULT_DB_PATH = "./data/aer.db"
#: Key under which the programmatic runner passes an explicit path.
DB_PATH_ATTRIBUTE = "db_path"


def resolve_db_path() -> Path:
    """Work out which SQLite file this migration run targets."""
    explicit = config.attributes.get(DB_PATH_ATTRIBUTE)
    if explicit:
        return Path(explicit)

    from_x_argument = context.get_x_argument(as_dictionary=True).get(DB_PATH_ATTRIBUTE)
    if from_x_argument:
        return Path(from_x_argument)

    return Path(os.environ.get("AER_DB_PATH", DEFAULT_DB_PATH))


def render_item(type_: str, obj: Any, autogen_context: Any) -> str | bool:
    """Render AER's custom column types as their DDL equivalent.

    A revision file must never import the application package: it has to keep
    working after that package is renamed, moved or deleted. ``UTCDateTime`` is a
    ``TypeDecorator`` whose ``impl`` is ``sa.DateTime`` -- the two produce
    byte-identical DDL -- so generated revisions get ``sa.DateTime()`` and stay
    self-contained.

    Returning ``False`` hands the item back to Alembic's default rendering.
    """
    del autogen_context  # not needed for the mapping below
    if type_ == "type" and isinstance(obj, UTCDateTime):
        return "sa.DateTime()"
    return False


def _configure(**kwargs: Any) -> None:
    context.configure(
        target_metadata=target_metadata,
        render_as_batch=True,
        compare_type=True,
        render_item=render_item,
        **kwargs,
    )


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of executing it against a live connection."""
    _configure(
        url=sqlite_url(resolve_db_path()).render_as_string(hide_password=False),
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Execute migrations against a real connection."""
    connectable = create_engine(sqlite_url(resolve_db_path()), poolclass=pool.NullPool)
    event.listen(connectable, "connect", apply_sqlite_pragmas)

    try:
        with connectable.connect() as connection:
            _configure(connection=connection)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
