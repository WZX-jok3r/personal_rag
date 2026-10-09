"""sql audit table

Revision ID: 3e9d1f7a5b28
Revises: 7c1a4b2e9d05
Create Date: 2026-10-08

新增 SQL 审计表（建在系统元数据库 rag）。
只记 SQL 与元数据，**不记结果集** —— 结果集可能含敏感数据，
存下来等于把 RBAC 脱敏工作白做。
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '3e9d1f7a5b28'
down_revision: Union[str, None] = '7c1a4b2e9d05'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'sql_audit',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('actor', sa.String(length=128), nullable=True),
        sa.Column('tenant_id', sa.String(length=64), nullable=True),
        sa.Column('sql', sa.Text(), nullable=False),
        sa.Column('ok', sa.Boolean(), server_default='false', nullable=False),
        sa.Column('row_count', sa.Integer(), server_default='0', nullable=False),
        sa.Column('elapsed_ms', sa.Integer(), server_default='0', nullable=False),
        sa.Column('redacted_columns', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_sql_audit')),
    )
    op.create_index(op.f('ix_sql_audit_actor'), 'sql_audit', ['actor'], unique=False)
    op.create_index(op.f('ix_sql_audit_tenant_id'), 'sql_audit', ['tenant_id'], unique=False)
    op.create_index(op.f('ix_sql_audit_ok'), 'sql_audit', ['ok'], unique=False)
    op.create_index(op.f('ix_sql_audit_created_at'), 'sql_audit', ['created_at'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_sql_audit_created_at'), table_name='sql_audit')
    op.drop_index(op.f('ix_sql_audit_ok'), table_name='sql_audit')
    op.drop_index(op.f('ix_sql_audit_tenant_id'), table_name='sql_audit')
    op.drop_index(op.f('ix_sql_audit_actor'), table_name='sql_audit')
    op.drop_table('sql_audit')
