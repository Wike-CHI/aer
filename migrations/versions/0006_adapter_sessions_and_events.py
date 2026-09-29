"""adapter_sessions and adapter_events

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-20 16:40:52.331904

Additive: two new tables, no change to anything Milestones 1-7 created. An existing
store is upgraded in place with no data movement, and both new tables arrive empty --
a deployment that has not yet wired an Agent adapter has no external sessions and has
accepted no external events, which is the expected state after this revision rather
than a symptom (round-8 brief, sections 52 and 59).

Why these are separate tables rather than columns on ``events`` or ``runs``:

* the trace must not grow a column whose meaning is "how one particular vendor
  numbered this event". Section 19 asks for exactly this judgement before touching
  the core event table, and section 53 forbids rewriting M1-M4 event semantics to
  accommodate a vendor. An adapter adapts to AER, not the other way round;
* the idempotency key ``(provider, external_event_id)`` is an *integration* fact, and
  its uniqueness constraint would be the wrong kind of constraint on the trace: AER
  must be able to record two events that a vendor (incorrectly) numbered the same, at
  least far enough to notice.

Two constraints carry the milestone's guarantees:

* ``UNIQUE(provider, external_event_id)`` on ``adapter_events`` -- a retried hook or a
  replayed webhook cannot produce a second AER event (sections 18-19);
* ``UNIQUE(provider, external_session_id)`` on ``adapter_sessions`` -- one external
  session maps to one *current* run, so a reconnecting Agent resumes the run it was
  reporting into instead of silently starting a second trace (sections 20-21).

Both use the **provider** as the namespace rather than the adapter name. An external
id belongs to the vendor, so two adapters aimed at the same provider must not each be
able to claim it; recording which adapter accepted it is provenance, not identity.

Foreign keys are deliberately asymmetric:

* ``adapter_sessions.aer_run_id`` is ``ON DELETE CASCADE`` -- a session mapping to a
  run that no longer exists has nothing left to resume;
* ``adapter_events.aer_event_id`` is ``ON DELETE SET NULL`` -- forgetting the trace
  event must not make AER forget that the external event was already handled, because
  that memory is what prevents a second delivery from being applied.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Apply this revision."""
    op.create_table(
        "adapter_sessions",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("external_session_id", sa.Text(), nullable=False),
        sa.Column("adapter_name", sa.Text(), nullable=False),
        sa.Column("external_run_id", sa.Text(), nullable=True),
        sa.Column("aer_run_id", sa.Text(), nullable=False),
        sa.Column("protocol_version", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("metadata_json", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["aer_run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider", "external_session_id", name="uq_adapter_sessions_external"),
    )
    op.create_index(
        "ix_adapter_sessions_aer_run_id", "adapter_sessions", ["aer_run_id"], unique=False
    )
    op.create_index(
        "ix_adapter_sessions_adapter_name", "adapter_sessions", ["adapter_name"], unique=False
    )

    op.create_table(
        "adapter_events",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("external_event_id", sa.Text(), nullable=False),
        sa.Column("adapter_name", sa.Text(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("aer_run_id", sa.Text(), nullable=False),
        sa.Column("aer_event_id", sa.Integer(), nullable=True),
        sa.Column("applied", sa.Boolean(), nullable=False),
        sa.Column("external_sequence", sa.Integer(), nullable=True),
        sa.Column("external_timestamp", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("metadata_json", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["aer_run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["aer_event_id"], ["events.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider", "external_event_id", name="uq_adapter_events_external"),
    )
    op.create_index("ix_adapter_events_aer_run_id", "adapter_events", ["aer_run_id"], unique=False)
    op.create_index(
        "ix_adapter_events_aer_event_id", "adapter_events", ["aer_event_id"], unique=False
    )


def downgrade() -> None:
    """Revert this revision.

    Dropping these tables loses the integration's memory: which external sessions map
    to which runs, and which external events have already been applied. The traces
    themselves survive -- they are in ``events`` -- but a reconnecting Agent would
    start a new run and a retried hook would be applied twice. That is a genuine
    consequence and the reason this downgrade is only for a deliberate retreat.
    """
    op.drop_index("ix_adapter_events_aer_event_id", table_name="adapter_events")
    op.drop_index("ix_adapter_events_aer_run_id", table_name="adapter_events")
    op.drop_table("adapter_events")

    op.drop_index("ix_adapter_sessions_adapter_name", table_name="adapter_sessions")
    op.drop_index("ix_adapter_sessions_aer_run_id", table_name="adapter_sessions")
    op.drop_table("adapter_sessions")
