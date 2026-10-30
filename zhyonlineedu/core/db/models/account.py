"""账号、会话、RBAC 与安全日志表（§7 · M1.6）。

字段口径：
- `account_user.token_version`（tv）递增即"窗口式下线"：旧 AT 最长存活 = AT TTL；
- `account_session` 保存当前 refresh jti，轮换后旧 jti 复用即判定重放（§7.2）；
- RBAC 三层：角色 → 权限码；`sys_user_role` 为账号与角色绑定。
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime

from sqlalchemy import Boolean, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base
from ..types import SoftDeleteMixin, UTCDatetime, VersionedMixin


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_user_no() -> str:
    """默认学号/工号：`U` + 12 位十六进制；与 `org_id` 组成软删自然键（D19a）。"""
    return "U" + secrets.token_hex(6).upper()


class AccountUser(SoftDeleteMixin, VersionedMixin, Base):
    """登录账号（M2.7 起补组织 / 学号 / 过期字段与软删自然键）。

    口径：
    - `(org_id, user_no, delete_seq)` 为软删复合唯一键（计划书 §6.5 的
      `uq_account_user_no_deleted`）；`username` 仍是登录唯一键，软删后不回收；
    - `expire_at` 到期由每日 02:00 的 `workers/expire.py` 置 `status=expired`，
      鉴权侧同时按 `expire_at` 兜底（避免 worker 未跑前的窗口期）；
    - `status`：active / disabled / expired（错误码分别 `ACCOUNT_DISABLED` / `ACCOUNT_EXPIRED`）。
    """

    __tablename__ = "account_user"
    __soft_delete_uq__ = ("org_id", "user_no")

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    # 学号/工号：组织内自然键；导入以 (org_id, user_no) 与 username 双键判冲突
    user_no: Mapped[str] = mapped_column(String(32), nullable=False, default=new_user_no)
    username: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="active", server_default="active"
    )
    # 账号过期时间（UTC）；为空 = 不限期
    expire_at: Mapped[datetime | None] = mapped_column(UTCDatetime, nullable=True)
    # 账号来源（§9.5）：self / open_admin / import / admin / install；切换对外→对内时
    # 自注册账号默认 disabled_readonly，并写入 migration_batch_id 便于回溯与批量恢复
    registration_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default="admin", server_default="admin"
    )
    migration_batch_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # 令牌版本：改密/风控重放/停用时 +1；AT 校验不逐请求比对（窗口 = AT TTL，AUTH-TV-WINDOW）
    token_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    last_login_at: Mapped[datetime | None] = mapped_column(UTCDatetime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDatetime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDatetime, nullable=False, default=utcnow)


class AccountSession(Base):
    """端会话（sid）：refresh 轮换、吊销与重放判定的事实来源（§7.2）。"""

    __tablename__ = "account_session"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sid: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    # 当前有效的 refresh jti：轮换即替换；旧值再次出现 = 重放
    refresh_jti: Mapped[str | None] = mapped_column(String(64), nullable=True)
    refresh_expires_at: Mapped[datetime] = mapped_column(UTCDatetime, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDatetime, nullable=True)
    # revoked_by/revoke_reason 为审计字段（§7.2 新增）：self/system/admin/password_change...
    revoked_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    revoke_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDatetime, nullable=False, default=utcnow)
    last_used_at: Mapped[datetime] = mapped_column(UTCDatetime, nullable=False, default=utcnow)


class SysRole(Base):
    __tablename__ = "sys_role"

    code: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    is_builtin: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )


class SysPermission(Base):
    __tablename__ = "sys_permission"

    code: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    # high / mid / low：高危权限默认无角色持有（progress:direct / media:key:any）
    risk_level: Mapped[str] = mapped_column(
        String(16), nullable=False, default="low", server_default="low"
    )


class SysUserRole(Base):
    __tablename__ = "sys_user_role"

    user_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    role_code: Mapped[str] = mapped_column(String(64), primary_key=True, index=True)


class SysRolePermission(Base):
    __tablename__ = "sys_role_permission"

    role_code: Mapped[str] = mapped_column(String(64), primary_key=True)
    permission_code: Mapped[str] = mapped_column(String(64), primary_key=True, index=True)


class SysLogSecurity(Base):
    """安全审计日志（§7.2 RT 重放必须落此表并告警）。"""

    __tablename__ = "sys_log_security"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    detail: Mapped[str] = mapped_column(String(255), nullable=False, default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(UTCDatetime, nullable=False, default=utcnow)
