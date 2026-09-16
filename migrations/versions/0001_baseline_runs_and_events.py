"""baseline runs and events

Revision ID: 0001
Revises: -
Create Date: 2026-09-16 09:53:22.307899

Baseline for the schema AER shipped before Alembic was adopted: ``runs`` and
``events``, exactly as ``Base.metadata`` describes them.

**This revision is idempotent on purpose.** AER Milestone 1-2 initialised
databases with ``Base.metadata.create_all()``, so real databases already contain
these two tables. Adopting them into migration history must not require deleting
``aer.db`` or hand-editing data, therefore every step checks for what already
exists and only creates what is missing. See docs/DECISIONS.md D-012.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _existing_tables() -> set[str]:
    """Names of the tables already present in the database being migrated."""
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    """Apply this revision."""
    existing = _existing_tables()

    if "runs" not in existing:
        op.create_table(
            "runs",
            sa.Column("id", sa.Text(), nullable=False),
            sa.Column("task_type", sa.Text(), nullable=True),
            sa.Column("task_description", sa.Text(), nullable=False),
            sa.Column("agent_name", sa.Text(), nullable=True),
            sa.Column("agent_version", sa.Text(), nullable=True),
            sa.Column("model_provider", sa.Text(), nullable=True),
            sa.Column("model_name", sa.Text(), nullable=True),
            sa.Column("status", sa.Text(), nullable=False),
            sa.Column("started_at", sa.DateTime(), nullable=False),
            sa.Column("ended_at", sa.DateTime(), nullable=True),
            sa.Column("final_score", sa.Float(), nullable=True),
            sa.Column("metadata_json", sa.Text(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
        with op.batch_alter_table("runs", schema=None) as batch_op:
            batch_op.create_index("ix_runs_started_at", ["started_at"], unique=False)
            batch_op.create_index("ix_runs_status", ["status"], unique=False)

    if "events" not in existing:
        op.create_table(
            "events",
            # INTEGER PRIMARY KEY AUTOINCREMENT: rowids are never reused, which is
            # the right default for an append-only audit log.
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("run_id", sa.Text(), nullable=False),
            sa.Column("sequence", sa.Integer(), nullable=False),
            sa.Column("event_type", sa.Text(), nullable=False),
            sa.Column("input_json", sa.Text(), nullable=True),
            sa.Column("output_json", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("duration_ms", sa.Integer(), nullable=True),
            sa.Column("metadata_json", sa.Text(), nullable=True),
            sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            # Makes a duplicate or regressed sequence physically impossible.
            sa.UniqueConstraint("run_id", "sequence", name="uq_events_run_sequence"),
            sqlite_autoincrement=True,
        )
        with op.batch_alter_table("events", schema=None) as batch_op:
            batch_op.create_index("ix_events_run_sequence", ["run_id", "sequence"], unique=False)


def downgrade() -> None:
    """Revert this revision."""
    existing = _existing_tables()

    if "events" in existing:
        with op.batch_alter_table("events", schema=None) as batch_op:
            batch_op.drop_index("ix_events_run_sequence")
        op.drop_table("events")

    if "runs" in existing:
        with op.batch_alter_table("runs", schema=None) as batch_op:
            batch_op.drop_index("ix_runs_status")
            batch_op.drop_index("ix_runs_started_at")
        op.drop_table("runs")
