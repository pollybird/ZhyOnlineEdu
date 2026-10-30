"""ORM 基类与命名规约（§5.3 / D19a / D37）。

- 全小写下划线、DDL 永不双引号（D37）；
- 软删表统一 `UNIQUE(uq_cols, delete_seq)`：模型声明 `__soft_delete_uq__` 后由
  `Base.__init_subclass__` 在建表前自动注入，`ensure_soft_delete_uk` 在迁移/启动时断言；
- 所有表集中在 `Base.metadata`，Alembic 以该 metadata 为唯一 DDL 来源（§5.6）。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import MetaData, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase

# 约束命名规约：生成名全小写；显式命名优先于本规约（如软删复合 UK）。
NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

SOFT_DELETE_UQ_ATTR = "__soft_delete_uq__"


def inject_soft_delete_uk(cls: type) -> None:
    """为具体表注入 `UNIQUE(uq_cols, delete_seq)`（D19a/F6）。

    必须在 SQLAlchemy 建表前调用（`Base.__init_subclass__` 里先注入再 super()）。
    `__soft_delete_uq__` 为空、或类不是具体表（无自有 `__tablename__`）时跳过。
    """
    uq_cols = tuple(getattr(cls, SOFT_DELETE_UQ_ATTR, ()) or ())
    if not uq_cols:
        return
    table_name = cls.__dict__.get("__tablename__")
    if not table_name:
        return

    constraint = UniqueConstraint(
        *uq_cols,
        "delete_seq",
        name=f"uq_{table_name}_no_deleted",
    )
    raw_args = cls.__dict__.get("__table_args__", ())
    if isinstance(raw_args, dict):
        # 字典必须是元组最后一项
        new_args: tuple[Any, ...] = (constraint, raw_args)
    else:
        new_args = (*tuple(raw_args or ()), constraint)
    cls.__table_args__ = new_args  # type: ignore[attr-defined]  # 建表前改写声明参数


class Base(DeclarativeBase):
    """全项目唯一声明基类。"""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    def __init_subclass__(cls, **kwargs: Any) -> None:
        # 先注入再交给 DeclarativeBase 建表：super() 之后修改 __table_args__ 不生效（M1.3 实测）。
        inject_soft_delete_uk(cls)
        super().__init_subclass__(**kwargs)
