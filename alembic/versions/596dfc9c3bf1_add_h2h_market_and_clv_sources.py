"""add_h2h_market_and_clv_sources

Revision ID: 596dfc9c3bf1
Revises: f8677b5ff97c
Create Date: 2026-10-26 11:22:17.095672
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '596dfc9c3bf1'
down_revision: str | None = 'f8677b5ff97c'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('odds_snapshots', schema=None) as batch_op:
        batch_op.alter_column(
            'market',
            existing_type=sa.Enum('totals', 'btts', 'correct_score', 'alternate_totals', name='odd_market', native_enum=False, create_constraint=True),
            type_=sa.Enum('totals', 'btts', 'correct_score', 'alternate_totals', 'h2h', name='odd_market', native_enum=False, create_constraint=True),
            existing_nullable=False,
        )

    with op.batch_alter_table('player_availability', schema=None) as batch_op:
        batch_op.alter_column(
            'status',
            existing_type=sa.Enum('out', 'doubtful', 'suspended', name='player_status', native_enum=False, create_constraint=True),
            type_=sa.Enum('out', 'doubtful', 'suspended', 'returning', name='player_status', native_enum=False, create_constraint=True),
            existing_nullable=False,
        )

    with op.batch_alter_table('recommendations', schema=None) as batch_op:
        batch_op.alter_column(
            'market',
            existing_type=sa.Enum('exact_score', 'multiscore', 'goals_range', 'totals', 'btts', name='reco_market', native_enum=False, create_constraint=True),
            type_=sa.Enum('exact_score', 'multiscore', 'goals_range', 'totals', 'btts', 'team_totals', 'h2h', name='reco_market', native_enum=False, create_constraint=True),
            existing_nullable=False,
        )
        batch_op.alter_column(
            'clv_source',
            existing_type=sa.Enum('avgc', 'maxc', 'own_snapshot_t3', name='clv_source', native_enum=False, create_constraint=True),
            type_=sa.Enum(
                'avgc',
                'maxc',
                'own_snapshot_t3',
                'fair_market_matrix',
                'pinnacle_correct_score',
                'football_data_closing',
                name='clv_source',
                native_enum=False,
                create_constraint=True,
            ),
            existing_nullable=True,
        )

    with op.batch_alter_table('user_bets', schema=None) as batch_op:
        batch_op.create_check_constraint(
            'ck_user_bets_clv_source',
            "clv_source IS NULL OR clv_source IN ('fair_market_matrix', 'pinnacle_correct_score', 'football_data_closing')",
        )


def downgrade() -> None:
    with op.batch_alter_table('user_bets', schema=None) as batch_op:
        batch_op.drop_constraint('ck_user_bets_clv_source', type_='check')

    with op.batch_alter_table('recommendations', schema=None) as batch_op:
        batch_op.alter_column(
            'market',
            existing_type=sa.Enum('exact_score', 'multiscore', 'goals_range', 'totals', 'btts', 'team_totals', 'h2h', name='reco_market', native_enum=False, create_constraint=True),
            type_=sa.Enum('exact_score', 'multiscore', 'goals_range', 'totals', 'btts', name='reco_market', native_enum=False, create_constraint=True),
            existing_nullable=False,
        )
        batch_op.alter_column(
            'clv_source',
            existing_type=sa.Enum(
                'avgc',
                'maxc',
                'own_snapshot_t3',
                'fair_market_matrix',
                'pinnacle_correct_score',
                'football_data_closing',
                name='clv_source',
                native_enum=False,
                create_constraint=True,
            ),
            type_=sa.Enum('avgc', 'maxc', 'own_snapshot_t3', name='clv_source', native_enum=False, create_constraint=True),
            existing_nullable=True,
        )

    with op.batch_alter_table('player_availability', schema=None) as batch_op:
        batch_op.alter_column(
            'status',
            existing_type=sa.Enum('out', 'doubtful', 'suspended', 'returning', name='player_status', native_enum=False, create_constraint=True),
            type_=sa.Enum('out', 'doubtful', 'suspended', name='player_status', native_enum=False, create_constraint=True),
            existing_nullable=False,
        )

    with op.batch_alter_table('odds_snapshots', schema=None) as batch_op:
        batch_op.alter_column(
            'market',
            existing_type=sa.Enum('totals', 'btts', 'correct_score', 'alternate_totals', 'h2h', name='odd_market', native_enum=False, create_constraint=True),
            type_=sa.Enum('totals', 'btts', 'correct_score', 'alternate_totals', name='odd_market', native_enum=False, create_constraint=True),
            existing_nullable=False,
        )
