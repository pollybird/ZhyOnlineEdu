"""软删查询辅助（§6.5 D19a）：唯一合法写法 `not_deleted(model)`。

禁止在各模块手写 `deleted_at.is_(None)` / `deleted_at == None`（CI 扫描裸比较）。
"""

from __future__ import annotations

from sqlalchemy import ColumnElement

from .types import SoftDeleteMixin


def not_deleted(model: type[SoftDeleteMixin]) -> ColumnElement[bool]:
    """返回 `deleted_at IS NULL` 条件；参数为模型类（含 SoftDeleteMixin）。"""
    return model.deleted_at.is_(None)
