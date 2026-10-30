"""系统基础表（§6.3 最小集，M1.3）。

本批只落迁移链需要的四张系统表；账号/组织等业务表按里程碑在 M2 追加 revision。
字段长度受 D37/§26.7 约束（全小写、表名 ≤24 字符、DDL 永不双引号）。
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Boolean, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base
from ..types import UTCDatetime, VersionedMixin


def utcnow() -> datetime:
    return datetime.now(UTC)


class SysInstallState(VersionedMixin, Base):
    """安装状态单行表（id=1）：schema_rev / last_good_rev 供迁移回滚（§5.6）。"""

    __tablename__ = "sys_install_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    is_installed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # M1.5 向导状态机：start → db → admin → mode → finish → done（0002 revision 落列）
    install_step: Mapped[str] = mapped_column(
        String(16), nullable=False, default="start", server_default="start"
    )
    schema_rev: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_good_rev: Mapped[str | None] = mapped_column(String(32), nullable=True)
    installed_at: Mapped[datetime | None] = mapped_column(UTCDatetime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(UTCDatetime, nullable=False, default=utcnow)


class SysConfig(Base):
    """系统配置（§4.3 第三层：库内覆盖，bootstrap 键不得落此表）。"""

    __tablename__ = "sys_config"

    config_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDatetime, nullable=False, default=utcnow)


class SysModeConfig(Base):
    """双模式配置（§9）：compat_flags 以 JSON 文本落库，避免方言 JSON 差异。"""

    __tablename__ = "sys_mode_config"

    mode: Mapped[str] = mapped_column(String(16), primary_key=True)
    config_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    compat_flags: Mapped[str] = mapped_column(Text, nullable=False, default="{}")


class SysModeState(VersionedMixin, Base):
    """当前模式与 epoch 单行表（§9.3 / §9.6 · M2.10）。

    口径：
    - `epoch` 每次切换 +1，是"一次请求内只读一次"的版本基准（MODE-FLIP-001）；
    - 切换不改表结构（R5），只改本行 + `sys_mode_config` 的策略行；
    - `migration_batch_id` 记录该次切换受影响账号的批次，供批量恢复（§9.5）。
    """

    __tablename__ = "sys_mode_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    active_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default="internal", server_default="internal"
    )
    epoch: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    previous_mode: Mapped[str | None] = mapped_column(String(16), nullable=True)
    switched_at: Mapped[datetime | None] = mapped_column(UTCDatetime, nullable=True)
    switched_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 切换批次（FLIP + 时间戳 + hex），受影响账号的 migration_batch_id 指向它
    migration_batch_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(UTCDatetime, nullable=False, default=utcnow)


class SysCompatFlags(Base):
    """安装期固化的能力位（§5.4 / F1）：运行时只读，禁止逐请求探测。

    列与 `core.db.probe.SYS_COMPAT_FLAGS` 共用同一张 Table（避免两处定义漂移）。
    """

    __tablename__ = "sys_compat_flags"

    cap: Mapped[str] = mapped_column(String(64), primary_key=True)
    flavor: Mapped[str] = mapped_column(String(32), nullable=False)
    value: Mapped[int] = mapped_column(Integer, nullable=False)
    probed_at: Mapped[str] = mapped_column(String(32), nullable=False)
    detail: Mapped[str] = mapped_column(String(255), nullable=False, default="")
