"""retrieval_sessions and experience_usage

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-20 09:12:04.118330

Additive: two new tables, no change to anything Milestones 1-6 created, so an
existing store is upgraded in place with no data movement. Old experiences survive
untouched and the new tables arrive empty -- a Production store with zero usage rows
is the expected state after this revision, not a failed migration (round-7 brief,
sections 76-77).

Both tables are **behaviour** facts and live in SQLite rather than in the knowledge
index, because the question they answer ("did this experience actually get used, and
did it help?") is about what agents did, not about what AER knows. Projecting them
into NeuG would put analysis data into a retrieval index that is supposed to be a
disposable copy of the experience store (section 45).

Two constraints are load-bearing rather than conventional:

* ``UNIQUE(retrieval_session_id, experience_id)`` -- the same experience cannot
  appear twice in one retrieval result. This is the database's half of the guarantee
  that a "retrieved twice" row can never quietly become a doubled statistic
  (section 49);
* ``retrieval_sessions.run_id`` is ``ON DELETE SET NULL``, not ``CASCADE``. A
  retrieval still happened if the run it informed is later deleted; removing the
  record would make a cleanup look like a search that never occurred.

Indexes are exactly those section 48 names: the run and time of a session (the two
operator questions), the query fingerprint (grouping repeated questions), and on the
usage table the experience, the session, the injection timestamp and the signal.
Nothing speculative is indexed.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Apply this revision."""
    op.create_table(
        "retrieval_sessions",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("run_id", sa.Text(), nullable=True),
        sa.Column("query_text", sa.Text(), nullable=False),
        sa.Column("query_fingerprint", sa.Text(), nullable=False),
        sa.Column("domain", sa.Text(), nullable=True),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("requested_limit", sa.Integer(), nullable=False),
        sa.Column("result_count", sa.Integer(), nullable=False),
        sa.Column("knowledge_projection_version", sa.Integer(), nullable=True),
        sa.Column("retrieval_policy_version", sa.Text(), nullable=False),
        sa.Column("retrieval_duration_ms", sa.Integer(), nullable=False),
        sa.Column("experiment_id", sa.Text(), nullable=True),
        sa.Column("assignment", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("metadata_json", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_retrieval_sessions_run_id", "retrieval_sessions", ["run_id"], unique=False)
    op.create_index(
        "ix_retrieval_sessions_created_at", "retrieval_sessions", ["created_at"], unique=False
    )
    op.create_index(
        "ix_retrieval_sessions_query_fingerprint",
        "retrieval_sessions",
        ["query_fingerprint"],
        unique=False,
    )

    op.create_table(
        "experience_usage",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("retrieval_session_id", sa.Text(), nullable=False),
        sa.Column("experience_id", sa.Text(), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("retrieval_score", sa.Float(), nullable=False),
        sa.Column("retrieved_at", sa.DateTime(), nullable=False),
        sa.Column("injected_at", sa.DateTime(), nullable=True),
        sa.Column("injection_position", sa.Integer(), nullable=True),
        sa.Column("injection_chars", sa.Integer(), nullable=True),
        sa.Column("context_fingerprint", sa.Text(), nullable=True),
        sa.Column("formatter_version", sa.Text(), nullable=True),
        sa.Column("usage_signal", sa.Text(), nullable=False),
        sa.Column("usage_signal_source", sa.Text(), nullable=True),
        sa.Column("usage_signal_at", sa.DateTime(), nullable=True),
        sa.Column("utility_label", sa.Text(), nullable=False),
        sa.Column("utility_label_source", sa.Text(), nullable=True),
        sa.Column("utility_label_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("metadata_json", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["retrieval_session_id"], ["retrieval_sessions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["experience_id"], ["experiences.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "retrieval_session_id",
            "experience_id",
            name="uq_experience_usage_session_experience",
        ),
    )
    op.create_index(
        "ix_experience_usage_experience_id", "experience_usage", ["experience_id"], unique=False
    )
    op.create_index(
        "ix_experience_usage_retrieval_session_id",
        "experience_usage",
        ["retrieval_session_id"],
        unique=False,
    )
    op.create_index(
        "ix_experience_usage_injected_at", "experience_usage", ["injected_at"], unique=False
    )
    op.create_index(
        "ix_experience_usage_usage_signal", "experience_usage", ["usage_signal"], unique=False
    )


def downgrade() -> None:
    """Revert this revision.

    Dropping the usage tables loses behaviour history irrecoverably -- it is not
    derivable from anywhere else, unlike the knowledge index. The downgrade exists
    because every revision needs a way back, not because this one is cheap.
    """
    op.drop_index("ix_experience_usage_usage_signal", table_name="experience_usage")
    op.drop_index("ix_experience_usage_injected_at", table_name="experience_usage")
    op.drop_index("ix_experience_usage_retrieval_session_id", table_name="experience_usage")
    op.drop_index("ix_experience_usage_experience_id", table_name="experience_usage")
    op.drop_table("experience_usage")

    op.drop_index("ix_retrieval_sessions_query_fingerprint", table_name="retrieval_sessions")
    op.drop_index("ix_retrieval_sessions_created_at", table_name="retrieval_sessions")
    op.drop_index("ix_retrieval_sessions_run_id", table_name="retrieval_sessions")
    op.drop_table("retrieval_sessions")
