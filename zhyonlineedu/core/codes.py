"""统一错误码（附录 A 唯一来源）+ 码 → HTTP → 前端处置注册表。

信封格式（M1.1）：{"code": <int|str>, "message": ..., "data": ..., "request_id": ...}
前端约定：40201/40205 走 TicketedHls 换票处理器，不跳登录（§14.1）。
冻结：任何增删改必须同时改 ERROR_SPECS 快照测试与计划书附录 A。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import IntEnum
from types import MappingProxyType


class Code(IntEnum):
    # 2xx 语义码
    PROGRESS_ACCEPTED = 202  # 进度已接收未落库，客户端不得当失败

    # 限流
    RATE_LIMITED = 10101

    # 媒体票据（F3/F4）
    MEDIA_TICKET_EXPIRED = 40201  # 静默换票重试 1 次
    MEDIA_NOT_READY = 40202  # 转码中，按 retry_after 轮询
    MEDIA_TICKET_MISMATCH = 40203  # 串课/篡改
    MEDIA_ACCESS_REVOKED = 40204  # 风控吊销
    MEDIA_PLAYBACK_UNRECOVERABLE = 40205  # 切 origin 仍失败
    MEDIA_TICKET_REPLAY = 40206  # nonce 重放（审计，不提示"密码错"）

    # 上传
    UPLOAD_CHUNK_MD5_MISMATCH = 50101
    UPLOAD_CHUNK_MISSING = 50102
    UPLOAD_MALICIOUS = 50103


# 字符串语义码（信封 code 字段允许 str）
OK = "OK"
NOT_FOUND = "NOT_FOUND"
METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"
VALIDATION_FAILED = "VALIDATION_FAILED"
INTERNAL_ERROR = "INTERNAL_ERROR"
AUTH_TOKEN_EXPIRED = "AUTH_TOKEN_EXPIRED"
AUTH_TOKEN_REVOKED = "AUTH_TOKEN_REVOKED"
AUTH_INVALID_CREDENTIALS = "AUTH_INVALID_CREDENTIALS"  # 登录/改密的旧口令错误（不回显细节）
PERMISSION_DENIED = "PERMISSION_DENIED"  # RBAC 权限不足（区别于会员等级 ACCESS_*）
AUTH_REFRESH_REPLAY = "AUTH_REFRESH_REPLAY"  # 静默全端下线 + 安全日志
ACCOUNT_EXPIRED = "ACCOUNT_EXPIRED"
ACCOUNT_DISABLED = "ACCOUNT_DISABLED"
ACCESS_LEVEL_REQUIRED = "ACCESS_LEVEL_REQUIRED"
ACCESS_CLASS_REQUIRED = "ACCESS_CLASS_REQUIRED"
COURSE_NOT_PUBLISHED = "COURSE_NOT_PUBLISHED"
MODE_ACCESS_DENIED = "MODE_ACCESS_DENIED"  # 当前模式不支持该授权方式（如对外模式的班级课）
MODE_COURSE_MISMATCH = "MODE_COURSE_MISMATCH"  # 课程 mode 与当前模式不匹配
MODE_SWITCH_BLOCKED = "MODE_SWITCH_BLOCKED"  # 切换前置校验存在 blocker（§9.6）
MODE_UNCHANGED = "MODE_UNCHANGED"  # 目标模式与当前一致
SUBJECT_DISABLED = "SUBJECT_DISABLED"
REGISTRATION_CLOSED = "REGISTRATION_CLOSED"
INSTALL_REQUIRED = "INSTALL_REQUIRED"
INSTALL_ALREADY_DONE = "INSTALL_ALREADY_DONE"
INSTALL_STEP_INVALID = "INSTALL_STEP_INVALID"  # 向导步骤越序/未就绪
INSTALL_TOKEN_INVALID = "INSTALL_TOKEN_INVALID"  # 令牌或安装会话无效
INSTALL_BLOCKED = "INSTALL_BLOCKED"  # db/test blockers 非空，禁止下一步
CONFLICT_RETRY = "CONFLICT_RETRY"  # 乐观锁/排序并发
PLUGIN_CAP_DENIED = "PLUGIN_CAP_DENIED"  # 插件 manifest 能力违规（§11）
COURSE_ASSET_NOT_READY = "COURSE_ASSET_NOT_READY"  # 上架前置：课时 asset 未 encoded（§10.2）
CATALOG_NOT_EMPTY = "CATALOG_NOT_EMPTY"  # 目录仍有子节点/课程，禁止删除
# 会员订单 / 支付回调（§10.6）：状态只前进不后退（PAY-STATE-001~004）
PAY_STATE_INVALID = "PAY_STATE_INVALID"  # 非法状态迁移（如 paid → paying）
PAY_SIGNATURE_INVALID = "PAY_SIGNATURE_INVALID"  # 回调签名不合法
PAY_AMOUNT_MISMATCH = "PAY_AMOUNT_MISMATCH"  # 回调金额与订单快照不一致
PAY_CHANNEL_MISMATCH = "PAY_CHANNEL_MISMATCH"  # 回调渠道与订单渠道不一致
# 转码失败族（§10.3.2.3 ⑧）：落 asset/job 的 error_code，前端只看到码，不回吐 stderr
ENC_INPUT = "ENC-1001"  # 输入不可读/仅能识别容器但无法解析
ENC_FFMPEG = "ENC-1002"  # ffmpeg 非零退出
ENC_OUTPUT = "ENC-1003"  # 产物校验失败（结构/时长/编码/外链）
ENC_KEY = "ENC-1004"  # 密钥材料写入/注入失败
ENC_POSTER = "ENC-1005"  # poster 生成失败
ENC_TIMEOUT = "ENC-1006"  # 超时被杀
ENC_SANDBOX = "ENC-1007"  # 沙箱不可用/启动失败/路径越界
ENC_INTERRUPTED = "ENC-1008"  # worker 崩溃后任务被回收
TRANSCODE_PAUSED = "TRANSCODE_PAUSED"  # CVE 命中/人工暂停，停止新任务入队


@dataclass(frozen=True, slots=True)
class ErrorSpec:
    http: int
    message: str
    action: str  # refresh_ticket|stop_playback|relogin|retry|backoff|install|contact_support|none


_SPECS: dict[Code | str, ErrorSpec] = {
    Code.PROGRESS_ACCEPTED: ErrorSpec(202, "进度已接收，等待落库", "none"),
    Code.RATE_LIMITED: ErrorSpec(429, "请求过于频繁", "backoff"),
    Code.MEDIA_TICKET_EXPIRED: ErrorSpec(401, "播放票据已过期", "refresh_ticket"),
    Code.MEDIA_NOT_READY: ErrorSpec(409, "媒体尚未就绪", "retry"),
    Code.MEDIA_TICKET_MISMATCH: ErrorSpec(403, "票据与课时不匹配", "stop_playback"),
    Code.MEDIA_ACCESS_REVOKED: ErrorSpec(403, "播放权限已被吊销", "stop_playback"),
    Code.MEDIA_PLAYBACK_UNRECOVERABLE: ErrorSpec(410, "播放链路不可恢复", "contact_support"),
    Code.MEDIA_TICKET_REPLAY: ErrorSpec(403, "票据已被使用", "stop_playback"),
    Code.UPLOAD_CHUNK_MD5_MISMATCH: ErrorSpec(409, "分片校验失败，请重传", "retry"),
    Code.UPLOAD_CHUNK_MISSING: ErrorSpec(409, "分片缺失，请补传", "retry"),
    Code.UPLOAD_MALICIOUS: ErrorSpec(415, "文件格式不合规", "stop_playback"),
    OK: ErrorSpec(200, "OK", "none"),
    NOT_FOUND: ErrorSpec(404, "资源不存在", "none"),
    METHOD_NOT_ALLOWED: ErrorSpec(405, "请求方法不允许", "none"),
    VALIDATION_FAILED: ErrorSpec(422, "参数校验失败", "none"),
    INTERNAL_ERROR: ErrorSpec(500, "服务内部错误", "contact_support"),
    AUTH_TOKEN_EXPIRED: ErrorSpec(401, "登录已过期", "relogin"),
    AUTH_TOKEN_REVOKED: ErrorSpec(401, "登录已失效", "relogin"),
    AUTH_INVALID_CREDENTIALS: ErrorSpec(401, "用户名或密码错误", "none"),
    PERMISSION_DENIED: ErrorSpec(403, "无权限执行该操作", "none"),
    AUTH_REFRESH_REPLAY: ErrorSpec(401, "登录状态异常", "relogin"),
    ACCOUNT_EXPIRED: ErrorSpec(401, "账号已过期", "relogin"),
    ACCOUNT_DISABLED: ErrorSpec(401, "账号已停用", "relogin"),
    ACCESS_LEVEL_REQUIRED: ErrorSpec(403, "需要更高会员等级", "none"),
    ACCESS_CLASS_REQUIRED: ErrorSpec(403, "需要加入班级", "none"),
    COURSE_NOT_PUBLISHED: ErrorSpec(403, "课程未发布", "none"),
    MODE_ACCESS_DENIED: ErrorSpec(403, "当前模式不支持该授权方式", "none"),
    MODE_COURSE_MISMATCH: ErrorSpec(403, "课程不属于当前模式", "none"),
    MODE_SWITCH_BLOCKED: ErrorSpec(409, "模式切换前置条件未满足", "none"),
    MODE_UNCHANGED: ErrorSpec(409, "目标模式与当前模式一致", "none"),
    SUBJECT_DISABLED: ErrorSpec(403, "学科已停用", "none"),
    REGISTRATION_CLOSED: ErrorSpec(403, "注册通道已关闭", "none"),
    INSTALL_REQUIRED: ErrorSpec(409, "系统尚未完成安装", "install"),
    INSTALL_ALREADY_DONE: ErrorSpec(409, "系统已完成安装", "none"),
    INSTALL_STEP_INVALID: ErrorSpec(409, "安装步骤顺序不正确", "none"),
    INSTALL_TOKEN_INVALID: ErrorSpec(401, "安装令牌无效或已过期", "none"),
    INSTALL_BLOCKED: ErrorSpec(409, "安装前置条件未满足", "none"),
    CONFLICT_RETRY: ErrorSpec(409, "数据已更新，请重试", "retry"),
    PLUGIN_CAP_DENIED: ErrorSpec(403, "插件能力越权", "none"),
    COURSE_ASSET_NOT_READY: ErrorSpec(409, "课程资源尚未就绪，禁止上架", "none"),
    CATALOG_NOT_EMPTY: ErrorSpec(409, "目录下仍有子节点或课程", "none"),
    PAY_STATE_INVALID: ErrorSpec(409, "订单状态不允许该操作", "none"),
    PAY_SIGNATURE_INVALID: ErrorSpec(401, "支付回调签名无效", "none"),
    PAY_AMOUNT_MISMATCH: ErrorSpec(409, "支付金额与订单不一致", "contact_support"),
    PAY_CHANNEL_MISMATCH: ErrorSpec(422, "支付渠道与订单不一致", "none"),
    ENC_INPUT: ErrorSpec(500, "转码失败（输入不可读）", "contact_support"),
    ENC_FFMPEG: ErrorSpec(500, "转码失败（编码器退出）", "contact_support"),
    ENC_OUTPUT: ErrorSpec(500, "转码失败（产物校验不通过）", "contact_support"),
    ENC_KEY: ErrorSpec(500, "转码失败（密钥注入）", "contact_support"),
    ENC_POSTER: ErrorSpec(500, "转码失败（封面生成）", "contact_support"),
    ENC_TIMEOUT: ErrorSpec(500, "转码失败（超时）", "contact_support"),
    ENC_SANDBOX: ErrorSpec(500, "转码失败（沙箱不可用）", "contact_support"),
    ENC_INTERRUPTED: ErrorSpec(500, "转码失败（任务已中断）", "contact_support"),
    TRANSCODE_PAUSED: ErrorSpec(409, "转码任务已暂停", "none"),
}

ERROR_SPECS: Mapping[Code | str, ErrorSpec] = MappingProxyType(_SPECS)


def spec_for(code: Code | str) -> ErrorSpec:
    try:
        return ERROR_SPECS[code]
    except KeyError:
        raise KeyError(f"未注册错误码: {code!r}") from None


def is_media_code(code: Code | str) -> bool:
    if isinstance(code, str):
        return False
    return int(Code.MEDIA_TICKET_EXPIRED) <= int(code) <= int(Code.MEDIA_TICKET_REPLAY)
