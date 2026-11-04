# x-zhy-cap: returning:optional, alter_type_online:false
"""plugin entry_web (frontend ESM entry)

Revision ID: 0015_plugin_web_entry
Revises: 0014_plugin_framework
Create Date: 2026-11-01 14:00:00.000000

M3.5：插件前端 ESM 接入。
- `plugin_registry` 增加可空列 `entry_web`（`web/` 下的相对路径，如 `web/index.js`）；
- 菜单/入口地址由注册表下发，前端据此动态 `import()` 并渲染插件菜单；
- 新增列可空、无默认值回填，SQLite / 国产库 batch 重建语义一致；同步 `schema_rev`。
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015_plugin_web_entry"
down_revision: str | None = "0014_plugin_framework"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _sync_schema_rev(rev: str) -> None:
    op.execute(
        sa.text(
            "UPDATE sys_install_state SET schema_rev = :rev, last_good_rev = :rev WHERE id = 1"
        ).bindparams(rev=rev)
    )


def upgrade() -> None:
    with op.batch_alter_table("plugin_registry") as batch_op:
        batch_op.add_column(sa.Column("entry_web", sa.String(length=255), nullable=True))
    _sync_schema_rev(revision)


def downgrade() -> None:
    with op.batch_alter_table("plugin_registry") as batch_op:
        batch_op.drop_column("entry_web")
    _sync_schema_rev(down_revision or "0014_plugin_framework")
