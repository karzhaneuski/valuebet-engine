"""prematch_odds table (phase 4b)

Revision ID: a86d9da4539e
Revises: cb28a4e4d2c4
Create Date: 2026-10-26
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'a86d9da4539e'
down_revision: str | None = 'cb28a4e4d2c4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "prematch_odds",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("match_id", sa.Integer(), sa.ForeignKey("matches.id"),
                  nullable=False),
        sa.Column("bookmaker", sa.String(10), nullable=False),
        sa.Column("over_price", sa.Numeric(8, 3), nullable=True),
        sa.Column("under_price", sa.Numeric(8, 3), nullable=True),
        sa.UniqueConstraint("match_id", "bookmaker",
                            name="uq_prematch_odds_match_book"),
        sa.CheckConstraint("bookmaker IN ('Avg', 'P', 'Max')",
                           name="ck_prematch_odds_bookmaker"),
    )
    op.create_index("ix_prematch_odds_match_id", "prematch_odds",
                    ["match_id"])


def downgrade() -> None:
    op.drop_index("ix_prematch_odds_match_id", table_name="prematch_odds")
    op.drop_table("prematch_odds")
