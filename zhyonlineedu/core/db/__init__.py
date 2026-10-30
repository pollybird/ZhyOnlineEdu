"""core.db 对外 API（§5.4 契约）。

core.db.dialect() / flavor() / cap(...)   —— 能力位只读查询（安装期固化）
core.db.upsert(...) / upsert_many(...)    —— 唯一合法写入入口（红线白名单内部实现）
core.db.rehydrate_by_natkey(...)          —— pk=None 时唯一合法补 id 入口
"""

from __future__ import annotations

from .compat import (
    Cap,
    CompatError,
    Dialect,
    Flavor,
    SoftDeleteUKError,
    UnknownCapability,
    cap,
    configure,
    current,
    detect_flavor,
    dialect,
    ensure_soft_delete_uk,
    expected_settings,
    flavor,
    flavor_needs_batch,
    flavor_spec,
    greatest_sql,
    route_for,
)
from .types import DeleteSeq, Money, SoftDeleteMixin, UTCDatetime, VersionedMixin
from .upsert import (
    CompiledUpsert,
    UpsertBatchReport,
    UpsertResult,
    compile_upsert,
    rehydrate_by_natkey,
    upsert,
    upsert_many,
)

__all__ = [
    "Cap",
    "CompiledUpsert",
    "CompatError",
    "DeleteSeq",
    "Dialect",
    "Flavor",
    "Money",
    "SoftDeleteMixin",
    "SoftDeleteUKError",
    "UTCDatetime",
    "UpsertBatchReport",
    "UpsertResult",
    "UnknownCapability",
    "VersionedMixin",
    "cap",
    "compile_upsert",
    "configure",
    "current",
    "detect_flavor",
    "dialect",
    "ensure_soft_delete_uk",
    "expected_settings",
    "flavor",
    "flavor_needs_batch",
    "flavor_spec",
    "greatest_sql",
    "rehydrate_by_natkey",
    "route_for",
    "upsert",
    "upsert_many",
]
