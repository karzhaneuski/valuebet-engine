"""add_1x2_odds_columns

Revision ID: b13589d81812
Revises: a86d9da4539e
Create Date: 2026-10-25 14:01:07.034276
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = 'b13589d81812'
down_revision: str | None = 'a86d9da4539e'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("prematch_odds") as batch_op:
        batch_op.add_column(sa.Column("home_price", sa.Numeric(precision=8, scale=3), nullable=True))
        batch_op.add_column(sa.Column("draw_price", sa.Numeric(precision=8, scale=3), nullable=True))
        batch_op.add_column(sa.Column("away_price", sa.Numeric(precision=8, scale=3), nullable=True))

    with op.batch_alter_table("closing_odds") as batch_op:
        batch_op.add_column(sa.Column("home_price", sa.Numeric(precision=8, scale=3), nullable=True))
        batch_op.add_column(sa.Column("draw_price", sa.Numeric(precision=8, scale=3), nullable=True))
        batch_op.add_column(sa.Column("away_price", sa.Numeric(precision=8, scale=3), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("closing_odds") as batch_op:
        batch_op.drop_column("away_price")
        batch_op.drop_column("draw_price")
        batch_op.drop_column("home_price")

    with op.batch_alter_table("prematch_odds") as batch_op:
        batch_op.drop_column("away_price")
        batch_op.drop_column("draw_price")
        batch_op.drop_column("home_price")
