"""参考插件：结业证书（M3.7）。

演示口径（也是插件审核清单的正面样例）：
1. 证书**只**来自 `lesson.finished` 事件订阅：插件不在 HTTP 请求路径里写进度，
   也拿不到 `learn_progress` / `learn_progress_log`（进度唯一写入入口仍是内核网关）；
2. `via_ticket_preview=true`（试看票来源）一律跳过，不计数、不发证书；
3. 写入只经 `sdk.store.upsert`，表必须是自有前缀 `plg_reference_certificate_` 且含
   `delete_seq`；自然键 `(user_id, lesson_id, delete_seq)` 保证重复派发幂等；
4. 读取只经 `sdk.store.fetch`（自动过滤软删行，行数上限 200）。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, Integer, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from zhyonlineedu.core import sdk

TABLE_NAME = "plg_reference_certificate_cert"
NATKEY: tuple[str, ...] = ("user_id", "lesson_id", "delete_seq")
ISSUED_COLUMNS: tuple[str, ...] = ("course_id", "percent")
MAX_ROWS = 50


class _Base(DeclarativeBase):
    """插件自有声明式基类（与内核 `Base` 隔离，不污染内核迁移表集合）。"""


class Certificate(_Base):
    """结业证书：一个用户的一节课最多一张（自然键幂等）。"""

    __tablename__ = TABLE_NAME
    __table_args__ = (UniqueConstraint("user_id", "lesson_id", "delete_seq"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    lesson_id: Mapped[int] = mapped_column(Integer, nullable=False)
    course_id: Mapped[int] = mapped_column(Integer, nullable=False)
    percent: Mapped[str] = mapped_column(String(16), nullable=False)
    issued_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    delete_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)


def on_lesson_finished(payload: Mapping[str, Any]) -> None:
    """事件订阅回调：只发证书，不做任何进度写入。"""
    if payload.get("via_ticket_preview"):
        # §11 事件表红线：试看票来源的进度对所有订阅方等同"未发生"
        return
    sdk.store.upsert(
        Certificate,
        {
            "user_id": int(payload["user"]),
            "lesson_id": int(payload["lesson"]),
            "course_id": int(payload["course"]),
            "percent": f"{float(payload['percent']):.2f}",
            # 与内核 utcnow 同口径：naive UTC（跨方言 DateTime 不丢时区语义）
            "issued_at": datetime.now(UTC).replace(tzinfo=None),
            "delete_seq": 0,
        },
        list(NATKEY),
        list(ISSUED_COLUMNS),
    )


def certificates() -> dict[str, Any]:
    """证书列表（只读自有表）。"""
    items = sdk.store.fetch(Certificate, order_by="id", descending=True, limit=MAX_ROWS)
    return {"items": items, "total": len(items)}


def setup() -> None:
    """挂载钩子：内核在 manifest 作用域内执行本函数。"""
    sdk.events.subscribe("lesson.finished", on_lesson_finished)


HANDLERS = {"certificates": certificates}
