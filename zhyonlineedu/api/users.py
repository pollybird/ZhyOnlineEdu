"""用户 / 班级 / 批量导入接口（§10.5 · M2.7）。

权限：全部 `user:manage`（super_admin / admin）；teacher / student 一律 403
`PERMISSION_DENIED` —— 这是「越权」验收口径的第一层。

导入（`POST /users/import`）：
- 支持 JSON `rows`，也支持 multipart `.xlsx/.csv`（字段名同 `ImportRowIn`）；
- `on_conflict_action` 缺省取 `users.on_conflict_action`（默认 `report_only`）：
  **冲突行只报告，不覆盖原值**；`overwrite` 才显式覆盖；
- 返回 `import_batch_no` 与逐行动作，批次可经 `GET /users/imports/{batch_no}` 复核。
"""

from __future__ import annotations

from typing import Any

from flask import Blueprint, Response, current_app, request

from zhyonlineedu.api.schemas import (
    ClassCreateIn,
    ClassListOut,
    ClassMemberAddIn,
    ClassMemberDeleteOut,
    ClassMembersOut,
    ClassOut,
    ImportBatchListOut,
    ImportBatchOut,
    UserCreateIn,
    UserDisableIn,
    UserExpireIn,
    UserImportIn,
    UserListOut,
    UserOut,
    UserUpdateIn,
)
from zhyonlineedu.core.auth.decorators import authenticate_request, require_permission
from zhyonlineedu.core.codes import VALIDATION_FAILED
from zhyonlineedu.core.envelope import ok
from zhyonlineedu.core.errors import AppError
from zhyonlineedu.core.openapi import documented
from zhyonlineedu.modules.users.importer import ImportRejected, extract_rows
from zhyonlineedu.modules.users.service import UsersService

bp = Blueprint("users", __name__, url_prefix="/api/v1")

MANAGE = "user:manage"


def _service() -> UsersService:
    return current_app.extensions["zhy_users"]  # type: ignore[no-any-return]


def _payload() -> dict[str, Any]:
    if request.mimetype == "multipart/form-data":
        return dict(request.form.items())
    raw = request.get_json(silent=True)
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise AppError(VALIDATION_FAILED, data={"errors": ["请求体必须是 JSON 对象"]})
    return raw


def _import_rows(payload: dict[str, Any]) -> list[dict[str, Any]] | None:
    max_rows = _service().config.get_int("users.import_max_rows")
    try:
        return extract_rows(request.files.get("file"), max_rows=max_rows)
    except ImportRejected as exc:
        raise AppError(VALIDATION_FAILED, data={"errors": [str(exc)]}) from exc


# ── 用户 ──────────────────────────────────────────────────────────────────


@bp.get("/users")
@require_permission(MANAGE)
@documented(
    "用户列表",
    description=(
        "支持 status / role / class_id / keyword（用户名、学号、姓名）过滤与分页；"
        "每条返回角色与已加入班级（最多预览 5 个，class_count 为真实数量）。"
    ),
    response=UserListOut,
    tags=("users",),
)
def list_users() -> tuple[Response, int]:
    return ok(_service().list_users(request.args))


@bp.post("/users")
@require_permission(MANAGE)
@documented(
    "创建用户",
    description=(
        "username 为登录唯一键（软删后不回收）；user_no 缺省自动生成，"
        "与 org_id 组成软删自然键；口令按 Argon2id 入库且不回显。"
    ),
    request=UserCreateIn,
    response=UserOut,
    tags=("users",),
)
def create_user() -> tuple[Response, int]:
    return ok(_service().create_user(_payload()), http=201)


@bp.get("/users/<int:user_id>")
@require_permission(MANAGE)
@documented("用户详情", response=UserOut, tags=("users",))
def get_user(user_id: int) -> tuple[Response, int]:
    return ok(_service().get_user(user_id))


@bp.patch("/users/<int:user_id>")
@require_permission(MANAGE)
@documented(
    "更新用户",
    description=(
        "必须回传 version（乐观锁，不匹配 CONFLICT_RETRY）；可改用户名、学号、姓名、口令、"
        "角色与过期时间。expire_at 设为过去会立即置 expired 并吊销全部会话。"
    ),
    request=UserUpdateIn,
    response=UserOut,
    tags=("users",),
)
def update_user(user_id: int) -> tuple[Response, int]:
    return ok(_service().update_user(user_id, _payload()))


@bp.post("/users/<int:user_id>/disable")
@require_permission(MANAGE)
@documented(
    "停用用户",
    description=(
        "status=disabled + token_version+1 + 吊销全部会话 + 写安全日志；"
        "已停用时幂等返回。之后登录/刷新返回 ACCOUNT_DISABLED。"
    ),
    request=UserDisableIn,
    response=UserOut,
    tags=("users",),
    extensions={"x-zhy-idempotent": True},
)
def disable_user(user_id: int) -> tuple[Response, int]:
    return ok(_service().disable_user(user_id, _payload()))


@bp.post("/users/<int:user_id>/expire")
@require_permission(MANAGE)
@documented(
    "标记过期",
    description=(
        "立即置 status=expired、写 expire_at、token_version+1 并吊销会话；"
        "鉴权侧同时按 expire_at 兜底，不依赖每日 02:00 worker 是否已跑。"
    ),
    request=UserExpireIn,
    response=UserOut,
    tags=("users",),
    extensions={"x-zhy-idempotent": True},
)
def expire_user(user_id: int) -> tuple[Response, int]:
    return ok(_service().expire_user(user_id, _payload()))


# ── 班级 ──────────────────────────────────────────────────────────────────


@bp.get("/classes")
@require_permission(MANAGE)
@documented(
    "班级列表",
    description="支持 keyword / is_enabled 过滤；返回 member_count。",
    response=ClassListOut,
    tags=("users",),
)
def list_classes() -> tuple[Response, int]:
    return ok(_service().list_classes(request.args))


@bp.post("/classes")
@require_permission(MANAGE)
@documented(
    "创建班级",
    description=(
        "班级名在组织内唯一；teacher_id 必须指向未删除账号。"
        "class 类型课程的授权 = 任一启用班级的成员（无班级-课程映射）。"
    ),
    request=ClassCreateIn,
    response=ClassOut,
    tags=("users",),
)
def create_class() -> tuple[Response, int]:
    return ok(_service().create_class(_payload()), http=201)


@bp.post("/classes/<int:class_id>/members")
@require_permission(MANAGE)
@documented(
    "加入班级成员",
    description=(
        "批量加入，已存在成员幂等跳过（added 为本次新增数）；"
        "user_ids 中任一用户不存在返回 404。加入后立即获得 class 课程播放/测验授权。"
    ),
    request=ClassMemberAddIn,
    response=ClassMembersOut,
    tags=("users",),
)
def add_members(class_id: int) -> tuple[Response, int]:
    result = _service().add_members(class_id, _payload())
    return ok(result, http=201 if result["added"] else 200)


@bp.delete("/classes/<int:class_id>/members/<int:user_id>")
@require_permission(MANAGE)
@documented(
    "移出班级成员",
    description=(
        "硬删成员关系；退班后旧播放票据因授权指纹变化返回 40204，"
        "再次播放/测验返回 ACCESS_CLASS_REQUIRED。"
    ),
    response=ClassMemberDeleteOut,
    tags=("users",),
)
def remove_member(class_id: int, user_id: int) -> tuple[Response, int]:
    return ok(_service().remove_member(class_id, user_id))


# ── 导入 ──────────────────────────────────────────────────────────────────


@bp.post("/users/import")
@require_permission(MANAGE)
@documented(
    "批量导入用户",
    description=(
        "JSON rows 或 multipart .xlsx/.csv（表头 username/password/user_no/"
        "display_name/expire_at）。默认 report_only：username 或 (org_id,user_no) "
        "冲突只记 skipped，绝不覆盖原值；overwrite 才更新。批次号 import_batch_no "
        "落 sys_import_batch，逐行结果不含口令/哈希。"
    ),
    request=UserImportIn,
    response=ImportBatchOut,
    tags=("users",),
    extensions={"x-zhy-idempotent": False},
)
def import_users() -> tuple[Response, int]:
    user = authenticate_request()
    payload = _payload()
    result = _service().import_users(payload, operator_id=user.id, file_rows=_import_rows(payload))
    return ok(result, http=201)


@bp.get("/users/imports")
@require_permission(MANAGE)
@documented(
    "导入批次列表",
    description="按 status（completed|partial|failed）过滤，倒序分页；不返回逐行结果。",
    response=ImportBatchListOut,
    tags=("users",),
)
def list_imports() -> tuple[Response, int]:
    return ok(_service().list_imports(request.args))


@bp.get("/users/imports/<batch_no>")
@require_permission(MANAGE)
@documented(
    "导入批次详情",
    description="按 import_batch_no 查询，含逐行动作与原因；不含口令/哈希。",
    response=ImportBatchOut,
    tags=("users",),
)
def get_import(batch_no: str) -> tuple[Response, int]:
    return ok(_service().get_import(batch_no))
