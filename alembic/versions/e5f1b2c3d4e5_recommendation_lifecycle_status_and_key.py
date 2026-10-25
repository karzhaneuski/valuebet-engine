"""recommendation_lifecycle_status_and_key

Revision ID: e5f1b2c3d4e5
Revises: 48fd4f90f079
Create Date: 2026-10-25 21:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'e5f1b2c3d4e5'
down_revision: str | None = '48fd4f90f079'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('recommendations', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('selection_key', sa.String(length=80), nullable=False, server_default='')
        )
        batch_op.add_column(
            sa.Column(
                'status',
                sa.Enum(
                    'active',
                    'superseded',
                    'withdrawn',
                    name='reco_status',
                    native_enum=False,
                    create_constraint=True,
                ),
                nullable=False,
                server_default='active',
            )
        )
        batch_op.alter_column('p_model', existing_type=sa.Numeric(precision=6, scale=4), nullable=True)
        batch_op.drop_constraint('uq_recommendations_idempotency', type_='unique')
        batch_op.create_index('ix_recommendations_selection_key', ['selection_key'], unique=False)
        batch_op.create_index(
            'uq_recommendations_active',
            ['match_id', 'market', 'selection_key', 'variant'],
            unique=True,
            sqlite_where=sa.text("status = 'active'"),
            postgresql_where=sa.text("status = 'active'"),
        )


def downgrade() -> None:
    with op.batch_alter_table('recommendations', schema=None) as batch_op:
        batch_op.drop_index('uq_recommendations_active')
        batch_op.drop_index('ix_recommendations_selection_key')
        batch_op.create_unique_constraint(
            'uq_recommendations_idempotency',
            ['match_id', 'market', 'outcome', 'variant', 'input_hash'],
        )
        batch_op.alter_column('p_model', existing_type=sa.Numeric(precision=6, scale=4), nullable=False)
        batch_op.drop_constraint('reco_status', type_='check')
        batch_op.drop_column('status')
        batch_op.drop_column('selection_key')
