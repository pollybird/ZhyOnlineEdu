"""用户 / 班级 / 导入 / 过期服务（§6.5 / §10.5 · M2.7）。

口径：
- 所有写操作走 `version` 乐观锁，冲突统一 `CONFLICT_RETRY(409)`；
- 停用 / 过期：状态置位 + `token_version+1` + 吊销全部会话 + 写 `sys_log_security`；
- `class` 课程授权：任一**启用**班级的成员即视为有权限（没有班级-课程映射表）；
  退班 / 停班立即失去授权，playback 的 `access_hash` 同步纳入成员位，旧票据 40204；
- 导入默认 `report_only`：冲突行只记 skipped，绝不覆盖原值（§10.5）。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, NoReturn, cast

from sqlalchemy import delete, func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from zhyonlineedu.core.auth.passwords import hash_password, password_problem
from zhyonlineedu.core.auth.rbac import ROLE_NAMES
from zhyonlineedu.core.codes import CONFLICT_RETRY, NOT_FOUND, VALIDATION_FAILED
from zhyonlineedu.core.config import Config
from zhyonlineedu.core.db.engine import Database, ensure_database
from zhyonlineedu.core.db.models.account import (
    AccountSession,
    AccountUser,
    SysLogSecurity,
    SysUserRole,
    new_user_no,
)
from zhyonlineedu.core.db.models.users import EduClass, EduClassMember, SysImportBatch
from zhyonlineedu.core.db.query import not_deleted
from zhyonlineedu.core.errors import AppError
from zhyonlineedu.modules.users import importer

PAGE_SIZE_DEFAULT = 20
PAGE_SIZE_MAX = 100
USER_STATUSES: tuple[str, ...] = ("active", "disabled", "expired")
SORT_STEP = 1024
ON_CONFLICT_ACTIONS: tuple[str, ...] = ("report_only", "overwrite")


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") if value is not None else None


def _fail(message: str) -> NoReturn:
    raise AppError(VALIDATION_FAILED, data={"errors": [message]})


def _text(value: object, field: str, *, max_len: int) -> str:
    if not isinstance(value, str):
        _fail(f"{field} 必须是字符串")
    text = value.strip()
    if text == "":
        _fail(f"{field} 不能为空")
    if len(text) > max_len:
        _fail(f"{field} 长度不能超过 {max_len}")
    return text


def _optional_text(value: object, field: str, *, max_len: int) -> str | None:
    if value is None:
        return None
    return _text(value, field, max_len=max_len)


def _int(value: object, field: str, *, minimum: int = 0, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(f"{field} 必须是整数")
    if value < minimum:
        _fail(f"{field} 不能小于 {minimum}")
    if maximum is not None and value > maximum:
        _fail(f"{field} 不能大于 {maximum}")
    return value


def _choice(value: object, field: str, allowed: Sequence[str]) -> str:
    joined = "/".join(allowed)
    if not isinstance(value, str) or value not in allowed:
        _fail(f"{field} 只能是 {joined}")
    return value


def _query_int(
    value: object, field: str, *, default: int, minimum: int, maximum: int | None = None
) -> int:
    if value is None or value == "":
        return default
    if isinstance(value, str) and value.lstrip("-").isdigit():
        value = int(value)
    return _int(value, field, minimum=minimum, maximum=maximum)


def _version(body: Mapping[str, Any]) -> int:
    return _int(body.get("version"), "version", minimum=1)


def _datetime(value: object, field: str) -> datetime | None:
    """ISO8601 → UTC；空串 / None 表示清除过期时间。"""
    if value is None or (isinstance(value, str) and value.strip() == ""):
        return None
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, str):
        try:
            moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            _fail(f"{field} 不是合法的 ISO8601 时间")
    else:
        _fail(f"{field} 必须是 ISO8601 字符串")
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def _roles(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        _fail("roles 必须是角色码数组")
    seen: list[str] = []
    for item in value:
        code = _choice(item, "roles", tuple(ROLE_NAMES))
        if code not in seen:
            seen.append(code)
    if not seen:
        _fail("roles 不能为空")
    return tuple(seen)


# ── 班级授权（playback / assess 共用）──────────────────────────────────


def is_class_member(session: Session, user_id: int) -> bool:
    """任一启用班级的成员即拥有 class 课程授权（退班 / 停班立即失效）。"""
    row = session.execute(
        select(EduClassMember.class_id)
        .join(EduClass, EduClass.id == EduClassMember.class_id)
        .where(
            EduClassMember.user_id == user_id,
            not_deleted(EduClass),
            EduClass.is_enabled.is_(True),
        )
        .limit(1)
    ).first()
    return row is not None


def _class_brief(row: EduClass) -> dict[str, Any]:
    return {"id": row.id, "name": row.name, "is_enabled": bool(row.is_enabled)}


def _user_out(
    row: AccountUser,
    *,
    roles: Sequence[str],
    classes: Sequence[dict[str, Any]],
    class_count: int,
) -> dict[str, Any]:
    return {
        "id": row.id,
        "org_id": row.org_id,
        "user_no": row.user_no,
        "username": row.username,
        "display_name": row.display_name,
        "status": row.status,
        "expire_at": _iso(row.expire_at),
        "roles": list(roles),
        "classes": list(classes),
        "class_count": class_count,
        "version": row.version,
        "last_login_at": _iso(row.last_login_at),
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


def _class_out(row: EduClass, *, member_count: int) -> dict[str, Any]:
    return {
        "id": row.id,
        "org_id": row.org_id,
        "name": row.name,
        "teacher_id": row.teacher_id,
        "is_enabled": bool(row.is_enabled),
        "sort_order": row.sort_order,
        "member_count": member_count,
        "version": row.version,
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


def _batch_out(row: SysImportBatch, *, rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "batch_no": row.batch_no,
        "kind": row.kind,
        "status": row.status,
        "on_conflict_action": row.on_conflict_action,
        "total_rows": row.total_rows,
        "created_rows": row.created_rows,
        "updated_rows": row.updated_rows,
        "skipped_rows": row.skipped_rows,
        "invalid_rows": row.invalid_rows,
        "operator_id": row.operator_id,
        "created_at": _iso(row.created_at),
        "finished_at": _iso(row.finished_at),
        "rows": rows,
    }
    return payload


class UsersService:
    """无状态服务：每个动作自建短会话。"""

    def __init__(self, config: Config, *, db: Database | None = None) -> None:
        self.config = config
        self._db = db

    @property
    def db(self) -> Database:
        if self._db is None:
            self._db = ensure_database(self.config)
        return self._db

    @property
    def on_conflict_action(self) -> str:
        return self.config.get_str("users.on_conflict_action")

    # ── 用户 ────────────────────────────────────────────────────────────

    def list_users(self, query: Mapping[str, Any]) -> dict[str, Any]:
        page = _query_int(query.get("page"), "page", default=1, minimum=1)
        page_size = _query_int(
            query.get("page_size"),
            "page_size",
            default=PAGE_SIZE_DEFAULT,
            minimum=1,
            maximum=PAGE_SIZE_MAX,
        )
        conditions: list[Any] = [not_deleted(AccountUser)]
        status = query.get("status")
        if status not in (None, ""):
            conditions.append(AccountUser.status == _choice(status, "status", USER_STATUSES))
        keyword = query.get("keyword")
        if isinstance(keyword, str) and keyword.strip():
            like = f"%{keyword.strip()}%"
            conditions.append(
                AccountUser.username.like(like)
                | AccountUser.user_no.like(like)
                | AccountUser.display_name.like(like)
            )
        role = query.get("role")
        if role not in (None, ""):
            role_code = _choice(role, "role", tuple(ROLE_NAMES))
            conditions.append(
                AccountUser.id.in_(
                    select(SysUserRole.user_id).where(SysUserRole.role_code == role_code)
                )
            )
        class_id = query.get("class_id")
        if class_id not in (None, ""):
            cid = _query_int(class_id, "class_id", default=1, minimum=1)
            conditions.append(
                AccountUser.id.in_(
                    select(EduClassMember.user_id).where(EduClassMember.class_id == cid)
                )
            )

        with Session(self.db.engine) as session:
            total = int(
                session.execute(
                    select(func.count()).select_from(AccountUser).where(*conditions)
                ).scalar_one()
            )
            rows = (
                session.execute(
                    select(AccountUser)
                    .where(*conditions)
                    .order_by(AccountUser.id)
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )
                .scalars()
                .all()
            )
            roles = self._roles_by_user(session, [row.id for row in rows])
            classes = self._classes_by_user(session, [row.id for row in rows])
        items = [
            _user_out(
                row,
                roles=roles.get(row.id, ()),
                classes=classes.get(row.id, ())[:5],
                class_count=len(classes.get(row.id, ())),
            )
            for row in rows
        ]
        return {"items": items, "total": total, "page": page, "page_size": page_size}

    def get_user(self, user_id: int) -> dict[str, Any]:
        with Session(self.db.engine) as session:
            row = self._user_row(session, user_id)
            roles = self._roles_by_user(session, [row.id]).get(row.id, ())
            classes = self._classes_by_user(session, [row.id]).get(row.id, ())
        return _user_out(row, roles=roles, classes=classes, class_count=len(classes))

    def create_user(self, body: Mapping[str, Any]) -> dict[str, Any]:
        username = _text(body.get("username"), "username", max_len=64)
        problem = password_problem(body.get("password"))
        if problem is not None:
            _fail(problem)
        user_no = _optional_text(body.get("user_no"), "user_no", max_len=32) or new_user_no()
        display_name = _optional_text(body.get("display_name"), "display_name", max_len=64)
        org_id = 1 if body.get("org_id") is None else _int(body.get("org_id"), "org_id", minimum=1)
        expire_at = _datetime(body.get("expire_at"), "expire_at")
        roles = _roles(body.get("roles", ["student"]))
        now = _utcnow()

        with Session(self.db.engine) as session:
            self._ensure_username_free(session, username)
            self._ensure_user_no_free(session, org_id, user_no)
            row = AccountUser(
                org_id=org_id,
                user_no=user_no,
                username=username,
                password_hash=hash_password(str(body.get("password"))),
                display_name=display_name,
                status="active",
                expire_at=expire_at,
                token_version=1,
                version=1,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.flush()
            for code in roles:
                session.add(SysUserRole(user_id=row.id, role_code=code))
            session.commit()
            user_id = row.id
        return self.get_user(user_id)

    def update_user(self, user_id: int, body: Mapping[str, Any]) -> dict[str, Any]:
        version = _version(body)
        now = _utcnow()
        with Session(self.db.engine) as session:
            row = self._user_row(session, user_id)
            if row.version != version:
                self._conflict("user", user_id, version)
            values: dict[str, Any] = {}
            if "username" in body:
                username = _text(body.get("username"), "username", max_len=64)
                if username != row.username:
                    self._ensure_username_free(session, username, exclude_id=user_id)
                values["username"] = username
            if "user_no" in body:
                user_no = _text(body.get("user_no"), "user_no", max_len=32)
                org_id = row.org_id
                if user_no != row.user_no:
                    self._ensure_user_no_free(session, org_id, user_no, exclude_id=user_id)
                values["user_no"] = user_no
            if "display_name" in body:
                values["display_name"] = _optional_text(
                    body.get("display_name"), "display_name", max_len=64
                )
            if "password" in body:
                problem = password_problem(body.get("password"))
                if problem is not None:
                    _fail(problem)
                values["password_hash"] = hash_password(str(body.get("password")))
            expired_now = False
            if "expire_at" in body:
                expire_at = _datetime(body.get("expire_at"), "expire_at")
                values["expire_at"] = expire_at
                if expire_at is not None and expire_at <= now and row.status == "active":
                    values["status"] = "expired"
                    values["token_version"] = int(row.token_version) + 1
                    expired_now = True
            roles = _roles(body["roles"]) if "roles" in body else None
            if not values and roles is None:
                _fail("没有可更新的字段")

            result = cast(
                CursorResult[Any],
                session.execute(
                    update(AccountUser)
                    .where(
                        AccountUser.id == user_id,
                        AccountUser.version == version,
                        not_deleted(AccountUser),
                    )
                    .values(**values, version=AccountUser.version + 1, updated_at=now)
                ),
            )
            if result.rowcount != 1:
                self._conflict("user", user_id, version)
            if roles is not None:
                session.execute(delete(SysUserRole).where(SysUserRole.user_id == user_id))
                for code in roles:
                    session.add(SysUserRole(user_id=user_id, role_code=code))
            if expired_now:
                self._revoke_sessions(session, user_id, now=now, reason="expired")
                session.add(
                    SysLogSecurity(
                        event="USER_EXPIRED",
                        user_id=user_id,
                        detail="source=update_expire_at",
                        created_at=now,
                    )
                )
            session.commit()
        return self.get_user(user_id)

    def disable_user(self, user_id: int, body: Mapping[str, Any]) -> dict[str, Any]:
        version = _version(body)
        now = _utcnow()
        with Session(self.db.engine) as session:
            row = self._user_row(session, user_id)
            if row.version != version:
                self._conflict("user", user_id, version)
            if row.status == "disabled":
                session.rollback()
            else:
                result = cast(
                    CursorResult[Any],
                    session.execute(
                        update(AccountUser)
                        .where(
                            AccountUser.id == user_id,
                            AccountUser.version == version,
                            not_deleted(AccountUser),
                        )
                        .values(
                            status="disabled",
                            token_version=AccountUser.token_version + 1,
                            version=AccountUser.version + 1,
                            updated_at=now,
                        )
                    ),
                )
                if result.rowcount != 1:
                    self._conflict("user", user_id, version)
                revoked = self._revoke_sessions(session, user_id, now=now, reason="disabled")
                session.add(
                    SysLogSecurity(
                        event="USER_DISABLED",
                        user_id=user_id,
                        detail=f"revoked_sessions={revoked}",
                        created_at=now,
                    )
                )
                session.commit()
        return self.get_user(user_id)

    def expire_user(self, user_id: int, body: Mapping[str, Any]) -> dict[str, Any]:
        """管理员手动置为过期：立即吊销会话，不等待每日 worker。"""
        version = _version(body)
        now = _utcnow()
        expire_at = _datetime(body.get("expire_at"), "expire_at") or now
        with Session(self.db.engine) as session:
            row = self._user_row(session, user_id)
            if row.version != version:
                self._conflict("user", user_id, version)
            result = cast(
                CursorResult[Any],
                session.execute(
                    update(AccountUser)
                    .where(
                        AccountUser.id == user_id,
                        AccountUser.version == version,
                        not_deleted(AccountUser),
                    )
                    .values(
                        status="expired",
                        expire_at=expire_at,
                        token_version=AccountUser.token_version + 1,
                        version=AccountUser.version + 1,
                        updated_at=now,
                    )
                ),
            )
            if result.rowcount != 1:
                self._conflict("user", user_id, version)
            revoked = self._revoke_sessions(session, user_id, now=now, reason="expired")
            session.add(
                SysLogSecurity(
                    event="USER_EXPIRED",
                    user_id=user_id,
                    detail=f"source=manual;revoked_sessions={revoked}",
                    created_at=now,
                )
            )
            session.commit()
        return self.get_user(user_id)

    # ── 班级 ────────────────────────────────────────────────────────────

    def list_classes(self, query: Mapping[str, Any]) -> dict[str, Any]:
        page = _query_int(query.get("page"), "page", default=1, minimum=1)
        page_size = _query_int(
            query.get("page_size"),
            "page_size",
            default=PAGE_SIZE_DEFAULT,
            minimum=1,
            maximum=PAGE_SIZE_MAX,
        )
        conditions: list[Any] = [not_deleted(EduClass)]
        keyword = query.get("keyword")
        if isinstance(keyword, str) and keyword.strip():
            conditions.append(EduClass.name.like(f"%{keyword.strip()}%"))
        if query.get("is_enabled") not in (None, ""):
            flag = _choice(query.get("is_enabled"), "is_enabled", ("true", "false"))
            conditions.append(EduClass.is_enabled == (flag == "true"))

        with Session(self.db.engine) as session:
            total = int(
                session.execute(
                    select(func.count()).select_from(EduClass).where(*conditions)
                ).scalar_one()
            )
            rows = (
                session.execute(
                    select(EduClass)
                    .where(*conditions)
                    .order_by(EduClass.sort_order, EduClass.id)
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )
                .scalars()
                .all()
            )
            counts = self._member_counts(session, [row.id for row in rows])
        items = [_class_out(row, member_count=counts.get(row.id, 0)) for row in rows]
        return {"items": items, "total": total, "page": page, "page_size": page_size}

    def create_class(self, body: Mapping[str, Any]) -> dict[str, Any]:
        name = _text(body.get("name"), "name", max_len=64)
        org_id = 1 if body.get("org_id") is None else _int(body.get("org_id"), "org_id", minimum=1)
        teacher_id = body.get("teacher_id")
        teacher = None if teacher_id is None else _int(teacher_id, "teacher_id", minimum=1)
        is_enabled = bool(body.get("is_enabled", True))
        now = _utcnow()
        with Session(self.db.engine) as session:
            self._ensure_class_name_free(session, org_id, name)
            if teacher is not None:
                self._user_row(session, teacher)
            max_order = int(
                session.execute(
                    select(func.coalesce(func.max(EduClass.sort_order), 0)).where(
                        not_deleted(EduClass)
                    )
                ).scalar_one()
            )
            row = EduClass(
                org_id=org_id,
                name=name,
                teacher_id=teacher,
                is_enabled=is_enabled,
                sort_order=max_order + SORT_STEP,
                version=1,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.commit()
            class_id = row.id
        with Session(self.db.engine) as session:
            return _class_out(self._class_row(session, class_id), member_count=0)

    def add_members(self, class_id: int, body: Mapping[str, Any]) -> dict[str, Any]:
        raw = body.get("user_ids")
        if raw is None and body.get("user_id") is not None:
            raw = [body.get("user_id")]
        if not isinstance(raw, list) or not raw:
            _fail("user_ids 必须是非空数组")
        user_ids: list[int] = []
        for item in raw:
            user_id = _int(item, "user_ids", minimum=1)
            if user_id not in user_ids:
                user_ids.append(user_id)
        now = _utcnow()
        with Session(self.db.engine) as session:
            self._class_row(session, class_id)
            existing_users = set(
                session.execute(
                    select(AccountUser.id).where(
                        AccountUser.id.in_(user_ids), not_deleted(AccountUser)
                    )
                )
                .scalars()
                .all()
            )
            missing = [user_id for user_id in user_ids if user_id not in existing_users]
            if missing:
                raise AppError(NOT_FOUND, data={"resource": "user", "ids": missing})
            current = set(
                session.execute(
                    select(EduClassMember.user_id).where(EduClassMember.class_id == class_id)
                )
                .scalars()
                .all()
            )
            added = [user_id for user_id in user_ids if user_id not in current]
            for user_id in added:
                session.add(EduClassMember(class_id=class_id, user_id=user_id, created_at=now))
            session.commit()
            members = self._members(session, class_id)
        return {"class_id": class_id, "added": len(added), "members": members}

    def remove_member(self, class_id: int, user_id: int) -> dict[str, Any]:
        with Session(self.db.engine) as session:
            self._class_row(session, class_id)
            result = cast(
                CursorResult[Any],
                session.execute(
                    delete(EduClassMember).where(
                        EduClassMember.class_id == class_id,
                        EduClassMember.user_id == user_id,
                    )
                ),
            )
            session.commit()
            removed = result.rowcount == 1
        return {"class_id": class_id, "user_id": user_id, "removed": removed}

    # ── 导入 ────────────────────────────────────────────────────────────

    def import_users(
        self, body: Mapping[str, Any], *, operator_id: int, file_rows: list[dict[str, Any]] | None
    ) -> dict[str, Any]:
        action = body.get("on_conflict_action")
        if action is None or action == "":
            action = self.on_conflict_action
        action = _choice(action, "on_conflict_action", ON_CONFLICT_ACTIONS)
        max_rows = self.config.get_int("users.import_max_rows")
        rows = file_rows if file_rows is not None else body.get("rows")
        if not isinstance(rows, list) or not rows:
            _fail("rows 必须是非空数组（或上传 .xlsx/.csv 文件）")
        if len(rows) > max_rows:
            _fail(f"单批最多 {max_rows} 行")
        now = _utcnow()
        with Session(self.db.engine) as session:
            report = importer.run_import(
                session,
                rows,
                on_conflict_action=action,
                operator_id=operator_id,
                max_rows=max_rows,
                now=now,
            )
            session.commit()
        return report

    def list_imports(self, query: Mapping[str, Any]) -> dict[str, Any]:
        page = _query_int(query.get("page"), "page", default=1, minimum=1)
        page_size = _query_int(
            query.get("page_size"),
            "page_size",
            default=PAGE_SIZE_DEFAULT,
            minimum=1,
            maximum=PAGE_SIZE_MAX,
        )
        conditions: list[Any] = []
        status = query.get("status")
        if status not in (None, ""):
            conditions.append(
                SysImportBatch.status
                == _choice(status, "status", ("completed", "partial", "failed"))
            )
        with Session(self.db.engine) as session:
            total = int(
                session.execute(
                    select(func.count()).select_from(SysImportBatch).where(*conditions)
                ).scalar_one()
            )
            rows = (
                session.execute(
                    select(SysImportBatch)
                    .where(*conditions)
                    .order_by(SysImportBatch.id.desc())
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )
                .scalars()
                .all()
            )
        return {
            "items": [_batch_out(row) for row in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    def get_import(self, batch_no: str) -> dict[str, Any]:
        if not batch_no.strip():
            raise AppError(NOT_FOUND, data={"resource": "import", "batch_no": batch_no})
        with Session(self.db.engine) as session:
            row = session.execute(
                select(SysImportBatch).where(SysImportBatch.batch_no == batch_no.strip())
            ).scalar_one_or_none()
            if row is None:
                raise AppError(NOT_FOUND, data={"resource": "import", "batch_no": batch_no})
            parsed: list[dict[str, Any]] | None = None
            if row.rows_json:
                try:
                    loaded = json.loads(row.rows_json)
                except json.JSONDecodeError:
                    loaded = None
                if isinstance(loaded, list):
                    parsed = [item for item in loaded if isinstance(item, dict)]
            return _batch_out(row, rows=parsed)

    # ── 内部 ────────────────────────────────────────────────────────────

    def _user_row(self, session: Session, user_id: int) -> AccountUser:
        row = session.execute(
            select(AccountUser).where(AccountUser.id == user_id, not_deleted(AccountUser))
        ).scalar_one_or_none()
        if row is None:
            raise AppError(NOT_FOUND, data={"resource": "user", "id": user_id})
        return row

    def _class_row(self, session: Session, class_id: int) -> EduClass:
        row = session.execute(
            select(EduClass).where(EduClass.id == class_id, not_deleted(EduClass))
        ).scalar_one_or_none()
        if row is None:
            raise AppError(NOT_FOUND, data={"resource": "class", "id": class_id})
        return row

    @staticmethod
    def _conflict(resource: str, row_id: int, version: int) -> NoReturn:
        raise AppError(
            CONFLICT_RETRY,
            data={"resource": resource, "id": row_id, "expected_version": version},
        )

    @staticmethod
    def _ensure_username_free(
        session: Session, username: str, *, exclude_id: int | None = None
    ) -> None:
        # username 为登录唯一键：软删后也不回收，因此查询不过滤 deleted_at
        stmt = select(AccountUser.id).where(AccountUser.username == username)
        if exclude_id is not None:
            stmt = stmt.where(AccountUser.id != exclude_id)
        if session.execute(stmt).first() is not None:
            _fail("用户名已存在")

    @staticmethod
    def _ensure_user_no_free(
        session: Session,
        org_id: int,
        user_no: str,
        *,
        exclude_id: int | None = None,
    ) -> None:
        stmt = select(AccountUser.id).where(
            not_deleted(AccountUser),
            AccountUser.org_id == org_id,
            AccountUser.user_no == user_no,
        )
        if exclude_id is not None:
            stmt = stmt.where(AccountUser.id != exclude_id)
        if session.execute(stmt).first() is not None:
            _fail("学号/工号已存在")

    @staticmethod
    def _ensure_class_name_free(
        session: Session, org_id: int, name: str, *, exclude_id: int | None = None
    ) -> None:
        stmt = select(EduClass.id).where(
            not_deleted(EduClass), EduClass.org_id == org_id, EduClass.name == name
        )
        if exclude_id is not None:
            stmt = stmt.where(EduClass.id != exclude_id)
        if session.execute(stmt).first() is not None:
            _fail("班级名称已存在")

    @staticmethod
    def _roles_by_user(session: Session, ids: Sequence[int]) -> dict[int, tuple[str, ...]]:
        if not ids:
            return {}
        rows = session.execute(
            select(SysUserRole.user_id, SysUserRole.role_code)
            .where(SysUserRole.user_id.in_(list(ids)))
            .order_by(SysUserRole.role_code)
        ).all()
        out: dict[int, list[str]] = {}
        for user_id, role_code in rows:
            out.setdefault(int(user_id), []).append(str(role_code))
        return {key: tuple(value) for key, value in out.items()}

    @staticmethod
    def _classes_by_user(session: Session, ids: Sequence[int]) -> dict[int, list[dict[str, Any]]]:
        if not ids:
            return {}
        rows = session.execute(
            select(EduClassMember.user_id, EduClass)
            .join(EduClass, EduClass.id == EduClassMember.class_id)
            .where(EduClassMember.user_id.in_(list(ids)), not_deleted(EduClass))
            .order_by(EduClass.sort_order, EduClass.id)
        ).all()
        out: dict[int, list[dict[str, Any]]] = {}
        for user_id, row in rows:
            out.setdefault(int(user_id), []).append(_class_brief(row))
        return out

    @staticmethod
    def _member_counts(session: Session, class_ids: Sequence[int]) -> dict[int, int]:
        if not class_ids:
            return {}
        rows = session.execute(
            select(EduClassMember.class_id, func.count())
            .where(EduClassMember.class_id.in_(list(class_ids)))
            .group_by(EduClassMember.class_id)
        ).all()
        return {int(class_id): int(count) for class_id, count in rows}

    @staticmethod
    def _members(session: Session, class_id: int) -> list[dict[str, Any]]:
        rows = session.execute(
            select(AccountUser)
            .join(EduClassMember, EduClassMember.user_id == AccountUser.id)
            .where(
                EduClassMember.class_id == class_id,
                not_deleted(AccountUser),
            )
            .order_by(AccountUser.id)
        ).scalars()
        return [
            {
                "user_id": row.id,
                "user_no": row.user_no,
                "username": row.username,
                "display_name": row.display_name,
            }
            for row in rows
        ]

    @staticmethod
    def _revoke_sessions(session: Session, user_id: int, *, now: datetime, reason: str) -> int:
        rows = (
            session.execute(
                select(AccountSession).where(
                    AccountSession.user_id == user_id,
                    AccountSession.revoked_at.is_(None),
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            row.revoked_at = now
            row.revoked_by = "admin"
            row.revoke_reason = reason
        return len(rows)


def new_batch_no(now: datetime | None = None) -> str:
    return importer.new_batch_no(now)


__all__ = [
    "ON_CONFLICT_ACTIONS",
    "USER_STATUSES",
    "UsersService",
    "is_class_member",
    "new_batch_no",
]
