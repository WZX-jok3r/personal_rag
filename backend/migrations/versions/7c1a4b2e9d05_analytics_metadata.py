"""analytics metadata tables

Revision ID: 7c1a4b2e9d05
Revises: 35fd0e1e9233
Create Date: 2026-10-08

新增 Text2SQL 的元数据两表（建在系统元数据库 rag，走 Alembic 管理）：
- analytics_tables : 表级登记 + LLM 可见性白名单（is_enabled）
- analytics_columns: 列语义 + 枚举值内联（提升生成准确率的关键）

注意：本迁移**只建元数据表**，不建业务表。
业务表在 kb_analytics 库，由 app.analytics.ddl / seed 动态创建 —— 因为业务表
随数据源变化而变，若走 Alembic 会导致"上传一个 xlsx 就要生成一个迁移文件"。
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '7c1a4b2e9d05'
down_revision: Union[str, None] = '35fd0e1e9233'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'analytics_tables',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('table_name', sa.String(length=64), nullable=False),
        sa.Column('display_name', sa.String(length=128), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('source', sa.String(length=512), nullable=False),
        sa.Column('sheet_name', sa.String(length=128), nullable=True),
        sa.Column('row_count', sa.Integer(), server_default='0', nullable=False),
        sa.Column('tenant_id', sa.String(length=64), nullable=True),
        sa.Column('is_enabled', sa.Boolean(), server_default='true', nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_analytics_tables')),
        sa.UniqueConstraint('table_name', name=op.f('uq_analytics_tables_table_name')),
    )
    op.create_index(op.f('ix_analytics_tables_tenant_id'), 'analytics_tables', ['tenant_id'], unique=False)

    op.create_table(
        'analytics_columns',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('table_id', sa.Integer(), nullable=False),
        sa.Column('ordinal', sa.Integer(), server_default='0', nullable=False),
        sa.Column('column_name', sa.String(length=64), nullable=False),
        sa.Column('data_type', sa.String(length=32), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('enum_values', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('is_primary', sa.Boolean(), server_default='false', nullable=False),
        sa.Column('is_nullable', sa.Boolean(), server_default='true', nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(
            ['table_id'], ['analytics_tables.id'],
            name=op.f('fk_analytics_columns_table_id_analytics_tables'), ondelete='CASCADE',
        ),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_analytics_columns')),
        sa.UniqueConstraint('table_id', 'column_name', name='uq_analytics_columns_table_id_column_name'),
    )
    op.create_index(op.f('ix_analytics_columns_table_id'), 'analytics_columns', ['table_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_analytics_columns_table_id'), table_name='analytics_columns')
    op.drop_table('analytics_columns')
    op.drop_index(op.f('ix_analytics_tables_tenant_id'), table_name='analytics_tables')
    op.drop_table('analytics_tables')
