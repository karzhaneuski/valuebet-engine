"""add_odds_poll_attempts_table

Revision ID: c1a2b3d4e5f6
Revises: 596dfc9c3bf1
Create Date: 2026-11-14 12:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from core.orm import UTCDateTime

revision: str = 'c1a2b3d4e5f6'
down_revision: str | None = '596dfc9c3bf1'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "odds_poll_attempts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("match_id", sa.Integer(), sa.ForeignKey("matches.id"), nullable=False),
        sa.Column("slot", UTCDateTime(), nullable=False),
        sa.Column("tier", sa.Enum(
            "T-48", "T-24", "T-3", "T-1", "manual", "off_window",
            name="odd_tier", native_enum=False, create_constraint=True,
        ), nullable=False),
        sa.Column("status", sa.Enum(
            "ok", "empty", "error", name="poll_attempt_status", native_enum=False,
            create_constraint=True,
        ),
                  nullable=False),
        sa.Column("ts", UTCDateTime(), nullable=False),
    )
    op.create_index(op.f("ix_odds_poll_attempts_match_id"), "odds_poll_attempts", ["match_id"])
    op.create_index(
        "ix_odds_poll_attempts_match_slot_tier", "odds_poll_attempts", ["match_id", "slot", "tier"],
    )


def downgrade() -> None:
    op.drop_table("odds_poll_attempts")
