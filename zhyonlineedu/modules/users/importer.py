"""用户批量导入（§10.5 · M2.7）。

契约：
- `import_batch_no` 沿用计划书口径：`IMP` + UTC 时间戳 + 随机 hex，落 `sys_import_batch`；
- 事务批次走 `UpsertResult` 批量路径；`username` / `(org_id, user_no)` 冲突时由
  `on_conflict_action` 决定：默认 `report_only` **只记错误行，不静默覆盖**；
- report_only 的新行走 upsert，但若原生路径意外命中冲突，用 savepoint 回滚该行，
  确保绝不修改原值；overwrite 才真正更新；
- 逐行结果只含用户名 / 学号 / 动作 / 原因，**绝不记录口令或哈希**。
"""

from __future__ import annotations

import csv
import io
import json
import secrets
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session
from werkzeug.datastructures import FileStorage

from zhyonlineedu.core.auth.passwords import hash_password, password_problem
from zhyonlineedu.core.codes import VALIDATION_FAILED
from zhyonlineedu.core.db.models.account import AccountUser
from zhyonlineedu.core.db.models.users import SysImportBatch
from zhyonlineedu.core.db.query import not_deleted
from zhyonlineedu.core.db.upsert import upsert
from zhyonlineedu.core.errors import AppError

ROW_KEYS: tuple[str, ...] = ("username", "password", "user_no", "display_name", "expire_at")
MAX_FIELD_LEN = 128


class ImportRejected(Exception):
    """整批被拒（文件格式 / 行数超限），由 API 转 422。"""


class _ReportOnlyConflict(Exception):
    """report_only 下 upsert 命中冲突：回滚该行，按 skipped 记。"""


def new_batch_no(now: datetime | None = None) -> str:
    moment = now or datetime.now(UTC)
    return f"IMP{moment:%Y%m%d%H%M%S}{secrets.token_hex(3).upper()}"


def parse_file_rows(file: FileStorage, *, max_rows: int) -> list[dict[str, Any]]:
    """解析 .xlsx / .csv 为行字典；表头大小写与空白不敏感。"""
    filename = file.filename or ""
    suffix = Path(filename).suffix.lower()
    if suffix in {".xlsx", ".xlsm"}:
        rows = _parse_xlsx(file, max_rows=max_rows)
    elif suffix in {".csv", ".txt"}:
        rows = _parse_csv(file, max_rows=max_rows)
    else:
        raise ImportRejected("仅支持 .xlsx / .csv 文件")
    if not rows:
        raise ImportRejected("文件中没有可导入的数据行")
    return rows


def _parse_xlsx(file: FileStorage, *, max_rows: int) -> list[dict[str, Any]]:
    from openpyxl import load_workbook  # 延迟导入：仅导入路径需要

    try:
        workbook = load_workbook(file.stream, read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001 - 解析失败统一按不可读处理
        raise ImportRejected("Excel 文件无法解析") from exc
    try:
        sheet = workbook.active
        rows_iter = sheet.iter_rows(values_only=True)
        header = next(rows_iter, None)
        if header is None:
            return []
        index = _header_index(header)
        out: list[dict[str, Any]] = []
        for raw in rows_iter:
            if raw is None:
                continue
            values = {key: raw[position] for key, position in index.items() if position < len(raw)}
            if _is_blank(values):
                continue
            out.append(_normalize(values))
            if len(out) > max_rows:
                raise ImportRejected(f"单批最多 {max_rows} 行")
        return out
    finally:
        workbook.close()


def _parse_csv(file: FileStorage, *, max_rows: int) -> list[dict[str, Any]]:
    raw = file.stream.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ImportRejected("CSV 必须为 UTF-8 编码") from exc
    reader = csv.reader(io.StringIO(text))
    header = next(reader, None)
    if header is None:
        return []
    index = _header_index(header)
    out: list[dict[str, Any]] = []
    for raw_row in reader:
        values = {
            key: raw_row[position] for key, position in index.items() if position < len(raw_row)
        }
        if _is_blank(values):
            continue
        out.append(_normalize(values))
        if len(out) > max_rows:
            raise ImportRejected(f"单批最多 {max_rows} 行")
    return out


def _header_index(header: Sequence[object]) -> dict[str, int]:
    index: dict[str, int] = {}
    for position, cell in enumerate(header):
        name = str(cell or "").strip().lower()
        if name in ROW_KEYS and name not in index:
            index[name] = position
    if "username" not in index:
        raise ImportRejected("表头必须包含 username")
    return index


def _normalize(values: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in ROW_KEYS:
        value = values.get(key)
        if isinstance(value, str):
            value = value.strip()
        out[key] = value
    return out


def _is_blank(values: Mapping[str, Any]) -> bool:
    return all(value in (None, "") for value in values.values())


def run_import(
    session: Session,
    rows: Sequence[object],
    *,
    on_conflict_action: str,
    operator_id: int,
    max_rows: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    """执行导入并落批次；调用方负责 commit。"""
    moment = now or datetime.now(UTC)
    if len(rows) > max_rows:
        raise AppError(VALIDATION_FAILED, data={"errors": [f"单批最多 {max_rows} 行"]})
    results: list[dict[str, Any]] = []
    created = updated = skipped = invalid = 0
    seen_usernames: set[str] = set()

    for index, raw in enumerate(rows, start=1):
        action, reason = _apply_row(
            session,
            raw,
            index=index,
            on_conflict_action=on_conflict_action,
            seen_usernames=seen_usernames,
            now=moment,
        )
        if action == "created":
            created += 1
        elif action == "updated":
            updated += 1
        elif action == "skipped":
            skipped += 1
        else:
            invalid += 1
        results.append(
            {
                "row": index,
                "username": _row_text(raw, "username"),
                "user_no": _row_text(raw, "user_no") or None,
                "action": action,
                "reason": reason,
            }
        )

    processed = created + updated
    if processed == 0:
        status = "failed"
    elif skipped or invalid:
        status = "partial"
    else:
        status = "completed"

    batch_no = new_batch_no(moment)
    batch = SysImportBatch(
        batch_no=batch_no,
        kind="user",
        status=status,
        on_conflict_action=on_conflict_action,
        total_rows=len(rows),
        created_rows=created,
        updated_rows=updated,
        skipped_rows=skipped,
        invalid_rows=invalid,
        rows_json=json.dumps(results, ensure_ascii=False, separators=(",", ":")),
        operator_id=operator_id,
        created_at=moment,
        finished_at=moment,
    )
    session.add(batch)
    session.flush()
    return {
        "batch_no": batch_no,
        "kind": batch.kind,
        "status": status,
        "on_conflict_action": on_conflict_action,
        "total_rows": len(rows),
        "created_rows": created,
        "updated_rows": updated,
        "skipped_rows": skipped,
        "invalid_rows": invalid,
        "operator_id": operator_id,
        "created_at": moment.isoformat(timespec="seconds"),
        "finished_at": moment.isoformat(timespec="seconds"),
        "rows": results,
    }


def _apply_row(
    session: Session,
    raw: object,
    *,
    index: int,
    on_conflict_action: str,
    seen_usernames: set[str],
    now: datetime,
) -> tuple[str, str | None]:
    prepared = _prepare(raw)
    if isinstance(prepared, str):
        return "invalid", prepared
    username = str(prepared["username"])
    if username in seen_usernames:
        return "invalid", "duplicate_in_batch"
    seen_usernames.add(username)

    try:
        with session.begin_nested():
            return _write_row(
                session,
                prepared,
                on_conflict_action=on_conflict_action,
                now=now,
            )
    except _ReportOnlyConflict:
        return "skipped", "conflict_keep_original"
    except IntegrityError:
        return "skipped", "conflict_keep_original"
    except SQLAlchemyError:
        return "invalid", "row_error"


def _prepare(raw: object) -> dict[str, Any] | str:
    if not isinstance(raw, Mapping):
        return "row 必须是对象"
    username = _clean(raw.get("username"))
    if not username:
        return "username 不能为空"
    if len(username) > 64:
        return "username 长度不能超过 64"
    password = raw.get("password")
    if password not in (None, ""):
        problem = password_problem(password)
        if problem is not None:
            return problem
    user_no = _clean(raw.get("user_no")) or None
    if user_no is not None and len(user_no) > 32:
        return "user_no 长度不能超过 32"
    display_name = _clean(raw.get("display_name")) or None
    if display_name is not None and len(display_name) > 64:
        return "display_name 长度不能超过 64"
    expire_at = _parse_datetime(raw.get("expire_at"))
    if isinstance(expire_at, str):
        return expire_at
    return {
        "username": username,
        "password": password if password not in (None, "") else None,
        "user_no": user_no,
        "display_name": display_name,
        "expire_at": expire_at,
        "has_display_name": "display_name" in raw,
        "has_expire_at": "expire_at" in raw,
    }


def _write_row(
    session: Session, prepared: Mapping[str, Any], *, on_conflict_action: str, now: datetime
) -> tuple[str, str | None]:
    username = str(prepared["username"])
    user_no = prepared["user_no"]
    by_name = session.execute(
        select(AccountUser).where(AccountUser.username == username)
    ).scalar_one_or_none()
    if by_name is not None and by_name.deleted_at is not None:
        return "invalid", "username_reserved"

    org_id = int(by_name.org_id) if by_name is not None else 1
    by_no: AccountUser | None = None
    if user_no is not None:
        by_no = session.execute(
            select(AccountUser).where(
                not_deleted(AccountUser),
                AccountUser.org_id == org_id,
                AccountUser.user_no == user_no,
            )
        ).scalar_one_or_none()
    if by_no is not None and (by_name is None or int(by_no.id) != int(by_name.id)):
        return "skipped", "user_no_conflict"

    existing = by_name
    if existing is not None and on_conflict_action == "report_only":
        return "skipped", "conflict_keep_original"
    if existing is None and prepared["password"] is None:
        return "invalid", "password_required_for_new_user"

    expire_at: datetime | None = (
        prepared["expire_at"]
        if prepared["has_expire_at"]
        else (existing.expire_at if existing is not None else None)
    )
    if existing is not None and existing.status == "disabled":
        status = "disabled"
    elif expire_at is not None and expire_at <= now:
        status = "expired"
    else:
        status = existing.status if existing is not None else "active"
    values: dict[str, Any] = {
        "org_id": org_id,
        "username": username,
        "user_no": user_no or (str(existing.user_no) if existing is not None else _new_user_no()),
        "display_name": (
            prepared["display_name"]
            if prepared["has_display_name"]
            else (existing.display_name if existing is not None else None)
        ),
        "expire_at": expire_at,
        "status": status,
        "password_hash": (
            hash_password(str(prepared["password"]))
            if prepared["password"] is not None
            else str(existing.password_hash if existing is not None else "")
        ),
        "token_version": int(existing.token_version) if existing is not None else 1,
        "created_at": existing.created_at if existing is not None else now,
        "updated_at": now,
        "version": 1,
    }
    update_cols = (
        "user_no",
        "display_name",
        "expire_at",
        "status",
        "password_hash",
        "updated_at",
    )
    result = upsert(
        AccountUser,
        values,
        natkey_cols=("username",),
        update_cols=update_cols,
        session=session,
    )
    if result.conflict:
        raise _ReportOnlyConflict
    if result.created:
        return "created", None
    if on_conflict_action != "overwrite":  # pragma: no cover - 前置已拦截
        raise _ReportOnlyConflict
    return "updated", None


def _parse_datetime(value: object) -> datetime | None | str:
    """返回 datetime / None / 错误文案（str）。"""
    if value is None or (isinstance(value, str) and value.strip() == ""):
        return None
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, str):
        try:
            moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return "expire_at 不是合法的 ISO8601 时间"
    else:
        return "expire_at 必须是 ISO8601 字符串"
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def _clean(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, int | float) and not isinstance(value, bool):
        return str(value)
    return str(value).strip()


def _row_text(raw: object, key: str) -> str | None:
    if not isinstance(raw, Mapping):
        return None
    value = _clean(raw.get(key))
    return value[:MAX_FIELD_LEN] or None


def _new_user_no() -> str:
    return "U" + secrets.token_hex(6).upper()


def extract_rows(file: FileStorage | None, *, max_rows: int) -> list[dict[str, Any]] | None:
    """从 multipart 文件解析行；无文件返回 None（走 JSON rows）。"""
    if file is None or not (file.filename or "").strip():
        return None
    return parse_file_rows(file, max_rows=max_rows)


__all__ = [
    "ImportRejected",
    "ROW_KEYS",
    "extract_rows",
    "new_batch_no",
    "parse_file_rows",
    "run_import",
]
