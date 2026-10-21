"""add partial claim index

Revision ID: 9f1c2a7b4d10
Revises: 58e706c838b1
Create Date: 2026-10-21
"""

from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "9f1c2a7b4d10"
down_revision: Union[str, None] = "58e706c838b1"
branch_labels: Union[str, tuple[str, ...], None] = None
depends_on: Union[str, tuple[str, ...], None] = None


def upgrade() -> None:
    op.create_index(
        "idx_steps_claim_partial",
        "step_runs",
        ["available_at", "id"],
        unique=False,
        postgresql_where=sa.text("status IN ('PENDING', 'RETRYING')"),
    )


def downgrade() -> None:
    op.drop_index("idx_steps_claim_partial", table_name="step_runs")
