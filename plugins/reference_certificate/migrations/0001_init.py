"""参考插件迁移链（M3.7 · 插件独立迁移链）。

约束（安装期强校验，违反即拒装并回滚）：
- 表名必须带 `plg_reference_certificate_` 前缀；
- 必须含 `delete_seq`（软删，D19a）；
- 禁止任何方言裸 SQL（`ON CONFLICT` / `ON DUPLICATE` 等，黑名单与 CI 同一份）。
"""

import sqlalchemy as sa

revision = "reference_certificate_0001"
down_revision = None


def upgrade(op):
    op.create_table(
        "plg_reference_certificate_cert",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("lesson_id", sa.Integer(), nullable=False),
        sa.Column("course_id", sa.Integer(), nullable=False),
        sa.Column("percent", sa.String(length=16), nullable=False),
        sa.Column("issued_at", sa.DateTime(), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("delete_seq", sa.BigInteger(), server_default="0", nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "lesson_id", "delete_seq"),
    )


def downgrade(op):
    op.drop_table("plg_reference_certificate_cert")
