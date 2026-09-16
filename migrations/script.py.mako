"""${message}

Revision ID: ${up_revision}
Revises: ${'%s' % down_revision if down_revision else '-'}
Create Date: ${create_date}

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
${imports if imports else ""}

# revision identifiers, used by Alembic.
revision: str = ${'"%s"' % up_revision}
down_revision: str | Sequence[str] | None = ${"None" if down_revision is None else '"%s"' % down_revision}
branch_labels: str | Sequence[str] | None = ${"None" if branch_labels is None else '"%s"' % branch_labels}
depends_on: str | Sequence[str] | None = ${"None" if depends_on is None else '"%s"' % depends_on}


def upgrade() -> None:
    """Apply this revision."""
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    """Revert this revision."""
    ${downgrades if downgrades else "pass"}
