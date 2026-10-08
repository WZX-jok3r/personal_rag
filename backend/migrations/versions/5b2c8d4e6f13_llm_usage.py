"""llm usage table

Revision ID: 5b2c8d4e6f13
Revises: 3e9d1f7a5b28
Create Date: 2026-10-08

新增 LLM 用量记账表（系统元数据库 rag）。
Agent 一次问答可调 1~8 次 LLM，没有记账则成本与容量都不可见。
只记数值与模型名，**不记 prompt/回答全文**（可能含敏感信息，且体积大）。
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '5b2c8d4e6f13'
down_revision: Union[str, None] = '3e9d1f7a5b28'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'llm_usage',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('scene', sa.String(length=64), nullable=False),
        sa.Column('model', sa.String(length=128), nullable=False),
        sa.Column('actor', sa.String(length=128), nullable=True),
        sa.Column('tenant_id', sa.String(length=64), nullable=True),
        sa.Column('session_id', sa.String(length=32), nullable=True),
        sa.Column('prompt_tokens', sa.Integer(), server_default='0', nullable=False),
        sa.Column('completion_tokens', sa.Integer(), server_default='0', nullable=False),
        sa.Column('total_tokens', sa.Integer(), server_default='0', nullable=False),
        sa.Column('elapsed_ms', sa.Integer(), server_default='0', nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_llm_usage')),
    )
    op.create_index(op.f('ix_llm_usage_scene'), 'llm_usage', ['scene'], unique=False)
    op.create_index(op.f('ix_llm_usage_actor'), 'llm_usage', ['actor'], unique=False)
    op.create_index(op.f('ix_llm_usage_tenant_id'), 'llm_usage', ['tenant_id'], unique=False)
    op.create_index(op.f('ix_llm_usage_session_id'), 'llm_usage', ['session_id'], unique=False)
    op.create_index(op.f('ix_llm_usage_created_at'), 'llm_usage', ['created_at'], unique=False)


def downgrade() -> None:
    for ix in ('created_at', 'session_id', 'tenant_id', 'actor', 'scene'):
        op.drop_index(op.f(f'ix_llm_usage_{ix}'), table_name='llm_usage')
    op.drop_table('llm_usage')
