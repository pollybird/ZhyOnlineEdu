"""接口请求/响应模型（OpenAPI 生成的唯一 schema 来源，§15.4）。

约束：
- 只描述**形状**，不作为运行时校验（运行时校验仍在服务层，失败走统一信封）；
- 与实现返回值保持字段一致，`tests/test_openapi.py` 会做漂移比对；
- 任何字段增删必须同步 `scripts/export_openapi.py` 产物（CI 校验）。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# ── 安装向导 ────────────────────────────────────────────────────────────────


class InstallStateOut(BaseModel):
    step: str = Field(description="start|db|admin|mode|finish|done")
    is_installed: bool
    schema_rev: str | None
    flavor: str | None
    blockers: list[str]
    steps: list[str] = Field(description="向导步骤顺序")


class InstallTokenIn(BaseModel):
    token: str = Field(min_length=1, description="启动日志或 ZHY_INSTALL_TOKEN 中的一次性安装令牌")


class InstallTokenOut(BaseModel):
    session: str = Field(description="后续安装请求放入 X-ZHY-Install-Session")
    expires_in: int


class DbTestOut(BaseModel):
    ok: bool
    flavor: str | None
    dialect: str | None
    caps: dict[str, bool]
    warnings: list[str]
    blockers: list[str]


class DbSetupOut(InstallStateOut):
    already: bool


class InstallAdminIn(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=8, max_length=128)


class InstallAdminOut(BaseModel):
    username: str
    step: str
    already: bool


class InstallModeIn(BaseModel):
    mode: Literal["internal", "external"]


class InstallModeOut(BaseModel):
    mode: str
    step: str
    already: bool


class InstallFinishOut(BaseModel):
    installed: bool
    step: str
    restart_required: bool = Field(description="业务路由需重启进程后装配")
    flavor: str | None
    caps: dict[str, bool]
    warnings: list[str]
    admin: str | None


# ── 认证 / RBAC ────────────────────────────────────────────────────────────


class AuthUserOut(BaseModel):
    id: int
    username: str
    display_name: str | None
    roles: list[str]
    permissions: list[str]
    token_version: int


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class LoginOut(BaseModel):
    access_token: str
    token_type: Literal["Bearer"]  # noqa: S105 - 协议固定值，非口令
    expires_in: int = Field(description="AT 有效期（秒），即 tv 窗口长度")
    user: AuthUserOut


class LogoutOut(BaseModel):
    logged_out: bool
    session_revoked: bool


class PasswordIn(BaseModel):
    old_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)


class PasswordOut(BaseModel):
    token_version: int
    sessions_revoked: int
    at_window_sec: int = Field(description="旧 AT 仍可用的剩余窗口")
    relogin_required: bool


class BootstrapOut(BaseModel):
    user: AuthUserOut
    mode: str | None
    server_time: str


# ── 学科树（M2.1）────────────────────────────────────────────────────────


class SubjectOut(BaseModel):
    id: int
    parent_id: int | None
    name: str
    sort_order: int
    is_enabled: bool
    version: int = Field(description="乐观锁版本，写操作必须回传")
    created_at: str | None
    updated_at: str | None


class SubjectListOut(BaseModel):
    items: list[SubjectOut]
    total: int


class SubjectCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    parent_id: int | None = Field(default=None, description="为空即根节点")


class SubjectUpdateIn(BaseModel):
    version: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=64)
    parent_id: int | None = Field(default=None, description="传 null 移动到根节点")
    before_id: int | None = Field(default=None, description="同级排序：插到该节点之前")
    is_enabled: bool | None = Field(default=None, description="停用后其课程不可上架")


class DeleteOut(BaseModel):
    id: int
    deleted: bool


# ── 课程三层（M2.1）──────────────────────────────────────────────────────


class LessonOut(BaseModel):
    id: int
    chapter_id: int
    course_id: int
    title: str
    kind: str = Field(description="video|text|live")
    duration_sec: int
    is_free: bool
    sort_order: int
    version: int
    asset_status: str | None = Field(description="None=尚未挂载资产；encoded 才可上架")


class ChapterOut(BaseModel):
    id: int
    course_id: int
    title: str
    sort_order: int
    version: int
    lessons: list[LessonOut]


class CourseOut(BaseModel):
    id: int
    subject_id: int
    subject_name: str | None
    title: str
    summary: str | None
    cover_url: str | None
    mode: str = Field(description="internal|external|both")
    access_type: str = Field(description="public|member|class")
    status: str = Field(description="draft|published|archived")
    teacher_id: int | None
    sort_order: int
    chapter_count: int
    lesson_count: int
    version: int
    published_at: str | None
    created_at: str | None
    updated_at: str | None


class CourseDetailOut(CourseOut):
    chapters: list[ChapterOut]


class CourseListOut(BaseModel):
    items: list[CourseOut]
    total: int
    page: int
    page_size: int


class CourseCreateIn(BaseModel):
    subject_id: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=128)
    summary: str | None = None
    cover_url: str | None = None
    mode: Literal["internal", "external", "both"] = "both"
    access_type: Literal["public", "member", "class"] = "public"
    teacher_id: int | None = Field(default=None, ge=1)


class CourseUpdateIn(BaseModel):
    version: int = Field(ge=1)
    subject_id: int | None = Field(default=None, ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=128)
    summary: str | None = None
    cover_url: str | None = None
    mode: Literal["internal", "external", "both"] | None = None
    access_type: Literal["public", "member", "class"] | None = None
    teacher_id: int | None = Field(default=None, ge=1)


class VersionIn(BaseModel):
    version: int = Field(ge=1, description="乐观锁版本；不匹配返回 CONFLICT_RETRY")


class ChapterCreateIn(BaseModel):
    title: str = Field(min_length=1, max_length=128)


class ChapterUpdateIn(BaseModel):
    version: int = Field(ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=128)


class ReorderIn(BaseModel):
    version: int = Field(ge=1, description="父级（课程/章）的乐观锁版本")
    ordered_ids: list[int] = Field(min_length=1, description="必须覆盖全部同级节点")


class CourseReorderOut(BaseModel):
    course_id: int
    ordered_ids: list[int]


class ChapterReorderOut(BaseModel):
    chapter_id: int
    ordered_ids: list[int]


class LessonCreateIn(BaseModel):
    title: str = Field(min_length=1, max_length=128)
    kind: Literal["video", "text", "live"] = "video"
    is_free: bool = False


class LessonUpdateIn(BaseModel):
    version: int = Field(ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=128)
    kind: Literal["video", "text", "live"] | None = None
    is_free: bool | None = None


class NotReadyLessonOut(BaseModel):
    lesson_id: int
    title: str
    asset_status: str | None


class CoursePublishOut(BaseModel):
    course: CourseOut
    ready: bool
    not_ready: list[NotReadyLessonOut]
    published_at: str | None


class CourseUnpublishOut(BaseModel):
    course: CourseOut


# ── 分片上传（M2.3）──────────────────────────────────────────────────────


class UploadInitIn(BaseModel):
    lesson_id: int = Field(ge=1)
    file_name: str = Field(min_length=1, max_length=255, description="不含路径分隔符")
    file_size: int = Field(ge=1, description="整文件字节数；超过 upload.max_file_bytes 拒绝")
    sha256: str = Field(min_length=64, max_length=64, description="整文件 sha256 hex")
    chunk_size: int = Field(
        default=8 * 1024 * 1024,
        ge=1024,
        le=67_108_864,
        description="默认取 upload.chunk_size；分片数受 upload.max_chunks 限制",
    )


class UploadInitOut(BaseModel):
    upload_id: str = Field(description="32 位 hex；后续接口与 tmp 子目录共用")
    status: str
    chunk_size: int
    total_chunks: int
    expires_at: str


class UploadChunkOut(BaseModel):
    upload_id: str
    index: int
    received: int
    total_chunks: int
    duplicated: bool = Field(description="同 index 同 md5 重传时幂等返回 true")


class UploadStatusOut(BaseModel):
    upload_id: str
    status: str = Field(description="open|processing|completed|rejected|expired|deleted")
    file_name: str
    file_size: int
    chunk_size: int
    total_chunks: int
    received_chunks: list[int]
    missing_chunks: list[int]
    expires_at: str
    error_code: str | None


class UploadCompleteOut(BaseModel):
    upload_id: str
    status: str
    lesson_id: int
    asset_status: str = Field(description="uploaded 表示已通过检疫，等待 M2.4 转码")
    format_name: str
    duration_sec: int
    width: int | None
    height: int | None
    probe_source: str = Field(description="ffprobe|magic")
    sha256: str
    ready_for_transcode: bool


class UploadDeleteOut(BaseModel):
    upload_id: str
    deleted: bool


# ── 转码与资产（M2.4）────────────────────────────────────────────────────


class TranscodeJobOut(BaseModel):
    id: int
    lesson_id: int
    status: str = Field(description="queued|running|succeeded|failed|canceled")
    attempt: int
    profiles: list[str]
    error_code: str | None = Field(description="ENC-10xx；stderr 不对外暴露")
    created_at: str
    started_at: str | None
    finished_at: str | None


class AssetOut(BaseModel):
    lesson_id: int
    course_id: int | None
    status: str = Field(description="pending|uploaded|transcoding|encoded|failed")
    playlist_version: int = Field(description="发布成功才递增；失败保持旧值")
    duration_sec: int
    kid: str | None = Field(description="AES key 的匿名标识，与用户解耦")
    profiles: list[str]
    poster_path: str | None
    error_code: str | None
    sandbox: dict[str, object] | None = Field(description="沙箱后端/配额/ffmpeg 版本")
    updated_at: str | None


class AssetDetailOut(AssetOut):
    job: TranscodeJobOut | None


class AssetListOut(BaseModel):
    items: list[AssetOut]
    total: int


class RetranscodeIn(BaseModel):
    profiles: list[str] | None = Field(default=None, description="缺省取 transcode.profiles")


class RetranscodeOut(BaseModel):
    job: TranscodeJobOut
    created: bool = Field(description="已有排队/执行中的任务时返回 false")


class AssetDeleteOut(BaseModel):
    lesson_id: int
    deleted: bool


# ── 播放票据与进度（M2.5）────────────────────────────────────────────────


class WatermarkOut(BaseModel):
    text: str = Field(description="水印文字（用户名；不含完整 IP）")
    opacity: float
    position: str = Field(description="br|bl|tr|tl")


class TicketIn(BaseModel):
    lesson_id: int = Field(ge=1)


class TicketOut(BaseModel):
    lesson_id: int
    course_id: int
    kid: str = Field(description="资产匿名标识；与用户解耦，可上 CDN")
    asset_status: str
    preview: bool = Field(description="true 时进度只落流水、不计入统计")
    profiles: list[str] = Field(description="本次票据允许的档位")
    playlist_url: str = Field(description="master.m3u8 地址，已带 ks/n/exp/sig")
    playlist_ttl: int = Field(description="playlist 签名有效期（秒）")
    key_ttl: int = Field(description="key 签名有效期（秒）")
    ticket_expires_at: str = Field(description="keysession 失效时间（最长层）")
    resume_second: int = Field(description="断点续播位置（无记录为 0）")
    watermark: WatermarkOut
    metric_version: str = Field(description="统计口径版本（zhy-progress-v2）")


class ProgressIn(BaseModel):
    lesson_id: int = Field(ge=1)
    position_sec: int = Field(ge=0, description="播放头位置；超过时长 +60s 拒绝")
    played_seconds: int = Field(ge=0, le=3600, description="本次上报新增播放秒数（1x）")
    speed: float = Field(default=1.0, description="倍速 0.5..3.0")
    client_ts_ms: int = Field(default=0, ge=0, description="客户端单调时钟，仅审计")


class ProgressOut(BaseModel):
    gw_seq: int = Field(description="网关分配的全局单调令牌")
    accepted_at: str
    deduped: bool = Field(description="同一 30s 桶已计过，本次 played_seconds 不再累加")


# ── 学习档案（§10.7 · M2.13）────────────────────────────────────────────


class CourseProgressOut(BaseModel):
    course_id: int
    course_title: str
    subject_id: int
    subject_name: str | None
    cover_url: str | None
    watch_seconds: int = Field(description="累计屏幕观看秒数（已剔除预览）")
    speed_time: int = Field(description="按 1x 折算的有效学习秒数（已剔除预览）")
    total_lessons: int
    completed_lessons: int
    total_duration_sec: int
    percent: float = Field(description="完成率 = 已完成课时 / 有效课时总数")
    completed: bool
    resume_lesson_id: int | None = Field(description="最近学习课时；无记录为 null")
    resume_position: int
    first_watch_at: str | None
    last_watch_at: str | None
    updated_at: str | None = Field(description="投影更新时间；异步落库允许秒级滞后")


class CourseProgressListOut(BaseModel):
    items: list[CourseProgressOut]
    total: int
    page: int
    page_size: int
    metric_version: str = Field(description="统计口径版本（zhy-progress-v2）")
    freshness_seconds: int = Field(description="投影滞后秒数；前端展示「更新于 X 分钟前」")
    generated_at: str


class ProgressLessonOut(BaseModel):
    lesson_id: int
    title: str
    kind: str = Field(description="video|text|live")
    duration_sec: int
    watch_seconds: int
    speed_time: int
    percent: float
    last_position: int
    completed: bool
    completed_at: str | None
    last_watch_at: str | None


class ProgressChapterOut(BaseModel):
    chapter_id: int
    title: str
    lessons: list[ProgressLessonOut] = Field(description="仅返回已学课时，未学不泄露课程结构")


class CourseProgressDetailOut(BaseModel):
    course: CourseProgressOut
    chapters: list[ProgressChapterOut]
    metric_version: str
    freshness_seconds: int
    generated_at: str


class ProgressGateOut(BaseModel):
    shard: str
    writer_id: str
    leased_until: str | None
    last_gw_seq: int = Field(description="网关已分配的全局水位")
    flushed_max_gw_seq: int = Field(description="已落库流水的最大 gw_seq")
    pending_logs: int
    gap: int = Field(description="last_gw_seq - flushed_max_gw_seq；0 表示无缺口")
    lag_seconds: int = Field(description="最老未落库流水距今秒数（投影滞后）")
    last_flush_at: str | None
    healthy: bool
    metric_version: str
    generated_at: str


class MyQuizSummaryOut(BaseModel):
    attempts: int = Field(description="已提交作答次数")
    passed: int
    average_percent: float
    best_percent: float


class MySummaryOut(BaseModel):
    user_id: int
    courses_started: int
    courses_completed: int
    lessons_completed: int
    watch_seconds: int
    speed_time: int
    today_watch_seconds: int = Field(description="今日有效观看秒数（source=play，已剔除预览）")
    recent_courses: list[CourseProgressOut]
    quiz: MyQuizSummaryOut
    metric_version: str
    freshness_seconds: int
    generated_at: str


# ── 学员端课程目录（M2.14）────────────────────────────────────────────────


class LearnCourseProgressOut(BaseModel):
    watch_seconds: int = Field(description="本人该课程累计观看秒数（已剔除预览）")
    percent: float = Field(description="完成率 = 已完成课时 / 有效课时总数")
    completed: bool
    completed_lessons: int
    resume_lesson_id: int | None
    resume_position: int
    last_watch_at: str | None


class LearnLessonProgressOut(BaseModel):
    watch_seconds: int
    percent: float
    completed: bool
    last_position: int = Field(description="续播位置（秒）")
    last_watch_at: str | None


class LearnCourseOut(BaseModel):
    course_id: int
    title: str
    summary: str | None
    cover_url: str | None
    subject_id: int
    subject_name: str | None
    access_type: str = Field(description="public|member|class")
    mode: str = Field(description="internal|external|both")
    total_lessons: int
    total_duration_sec: int
    first_lesson_id: int | None = Field(description="首个可播课时；空课程为 null")
    access_ok: bool = Field(description="UI 提示，最终裁决在取票接口")
    progress: LearnCourseProgressOut | None


class LearnCourseListOut(BaseModel):
    items: list[LearnCourseOut]
    total: int
    page: int
    page_size: int
    metric_version: str
    freshness_seconds: int
    generated_at: str


class LearnLessonOut(BaseModel):
    lesson_id: int
    title: str
    kind: str
    duration_sec: int
    is_free: bool
    asset_status: str | None = Field(description="encoded 才可播放；None 表示未挂载")
    progress: LearnLessonProgressOut | None


class LearnChapterOut(BaseModel):
    chapter_id: int
    title: str
    lessons: list[LearnLessonOut]


class LearnCourseDetailOut(BaseModel):
    course: LearnCourseOut
    chapters: list[LearnChapterOut]
    metric_version: str
    freshness_seconds: int
    generated_at: str


# ── 题库与作答（M2.6）────────────────────────────────────────────────────


class QuizOptionOut(BaseModel):
    key: str = Field(description="选项键（单选/多选 A/B/C…；判断题为 T/F）")
    text: str


class QuizQuestionOut(BaseModel):
    """管理端视图：含标准答案，仅 `course:edit` 可见。"""

    id: int
    course_id: int
    lesson_id: int
    kind: str = Field(description="single|multiple|judge")
    stem: str
    options: list[QuizOptionOut]
    answer: list[str] = Field(description="标准答案选项键；学员端响应永不包含此字段")
    explanation: str | None
    score: int
    sort_order: int
    is_enabled: bool
    version: int
    created_at: str | None
    updated_at: str | None


class QuizQuestionListOut(BaseModel):
    items: list[QuizQuestionOut]
    total: int
    page: int
    page_size: int


class QuizQuestionIn(BaseModel):
    lesson_id: int = Field(ge=1)
    kind: Literal["single", "multiple", "judge"] = "single"
    stem: str = Field(min_length=1, max_length=4000)
    options: list[QuizOptionOut] | None = Field(
        default=None, description="判断题可省略（服务端固定生成 T/F）"
    )
    answer: list[str] = Field(min_length=1, description="选项键数组；多选至少 2 项")
    explanation: str | None = Field(default=None, max_length=8000)
    score: int = Field(default=5, ge=1, le=100)
    is_enabled: bool = True


class QuizQuestionUpdateIn(BaseModel):
    version: int = Field(ge=1)
    kind: Literal["single", "multiple", "judge"] | None = None
    stem: str | None = Field(default=None, min_length=1, max_length=4000)
    options: list[QuizOptionOut] | None = None
    answer: list[str] | None = None
    explanation: str | None = Field(default=None, max_length=8000)
    score: int | None = Field(default=None, ge=1, le=100)
    is_enabled: bool | None = None


class QuizSnapshotItemOut(BaseModel):
    """作答快照：只有题干与选项，**不含答案**。"""

    question_id: int
    kind: str
    stem: str
    options: list[QuizOptionOut]
    score: int


class QuizResultItemOut(BaseModel):
    question_id: int
    correct: bool
    score: int
    score_earned: int
    explanation: str | None = Field(description="教师撰写解析；不含标准答案选项")


class QuizAttemptOut(BaseModel):
    id: int
    attempt_uid: str = Field(description="同时作为交卷的 Idempotency-Key")
    lesson_id: int
    course_id: int
    status: str = Field(description="in_progress|submitted|expired")
    question_count: int
    score_total: int
    score_earned: int
    percent: float
    passed: bool
    questions: list[QuizSnapshotItemOut]
    result: list[QuizResultItemOut] | None = Field(description="未交卷为 null")
    started_at: str | None
    submitted_at: str | None


class QuizAttemptBriefOut(BaseModel):
    id: int
    status: str
    score_earned: int
    score_total: int
    percent: float
    passed: bool
    started_at: str | None
    submitted_at: str | None


class QuizEntryOut(BaseModel):
    lesson_id: int
    course_id: int
    question_count: int
    score_total: int
    pass_score: int = Field(description="及格线（百分比），来自 quiz.pass_score")
    attempt: QuizAttemptOut | None = Field(description="进行中的作答；无则 null")
    last: QuizAttemptBriefOut | None = Field(description="最近一次已提交作答")


class QuizStartIn(BaseModel):
    lesson_id: int = Field(ge=1)


class QuizAttemptCreatedOut(BaseModel):
    attempt: QuizAttemptOut
    created: bool = Field(description="已有进行中作答时返回 false，不重复扣题")


class QuizAnswerIn(BaseModel):
    question_id: int = Field(ge=1)
    choices: list[str] = Field(min_length=1, description="所选选项键；集合完全相等才得分")


class QuizSubmitIn(BaseModel):
    answers: list[QuizAnswerIn] = Field(min_length=1)


class QuizSubmitOut(BaseModel):
    attempt: QuizAttemptOut
    created: bool = Field(description="false 表示同一 Idempotency-Key 重放，成绩未重算")


class QuizAttemptListOut(BaseModel):
    items: list[QuizAttemptBriefOut]
    total: int
    page: int
    page_size: int


# ── 用户 / 班级 / 导入（M2.7）────────────────────────────────────────────


class ClassBriefOut(BaseModel):
    id: int
    name: str
    is_enabled: bool


class UserOut(BaseModel):
    id: int
    org_id: int
    user_no: str = Field(description="组织内学号/工号；与 org_id 组成软删自然键")
    username: str
    display_name: str | None
    status: str = Field(description="active|disabled|expired")
    expire_at: str | None
    roles: list[str]
    classes: list[ClassBriefOut]
    class_count: int
    version: int
    last_login_at: str | None
    created_at: str | None
    updated_at: str | None


class UserListOut(BaseModel):
    items: list[UserOut]
    total: int
    page: int
    page_size: int


class UserCreateIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=8, max_length=128, description="Argon2id 入库，不回显")
    user_no: str | None = Field(default=None, max_length=32, description="缺省自动生成")
    display_name: str | None = Field(default=None, max_length=64)
    org_id: int = Field(default=1, ge=1)
    expire_at: str | None = Field(default=None, description="ISO8601；为空表示不限期")
    roles: list[str] | None = Field(
        default=None, description="缺省 student；super_admin/admin/teacher/student"
    )


class UserUpdateIn(BaseModel):
    version: int = Field(ge=1)
    username: str | None = Field(default=None, min_length=1, max_length=64)
    password: str | None = Field(default=None, min_length=8, max_length=128)
    user_no: str | None = Field(default=None, max_length=32)
    display_name: str | None = Field(default=None, max_length=64)
    expire_at: str | None = Field(default=None, description="传空串或 null 表示清除")
    roles: list[str] | None = None


class UserDisableIn(BaseModel):
    version: int = Field(ge=1)


class UserExpireIn(BaseModel):
    version: int = Field(ge=1)
    expire_at: str | None = Field(default=None, description="缺省为当前时间")


class ClassOut(BaseModel):
    id: int
    org_id: int
    name: str
    teacher_id: int | None
    is_enabled: bool
    sort_order: int
    member_count: int
    version: int
    created_at: str | None
    updated_at: str | None


class ClassListOut(BaseModel):
    items: list[ClassOut]
    total: int
    page: int
    page_size: int


class ClassCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    org_id: int = Field(default=1, ge=1)
    teacher_id: int | None = Field(default=None, ge=1)
    is_enabled: bool = True


class ClassMemberAddIn(BaseModel):
    user_ids: list[int] = Field(min_length=1, description="批量加入；已存在成员幂等跳过")


class ClassMemberOut(BaseModel):
    user_id: int
    user_no: str
    username: str
    display_name: str | None


class ClassMembersOut(BaseModel):
    class_id: int
    added: int
    members: list[ClassMemberOut]


class ClassMemberDeleteOut(BaseModel):
    class_id: int
    user_id: int
    removed: bool


class ImportRowIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str | None = Field(default=None, min_length=8, max_length=128)
    user_no: str | None = Field(default=None, max_length=32)
    display_name: str | None = Field(default=None, max_length=64)
    expire_at: str | None = None


class UserImportIn(BaseModel):
    rows: list[ImportRowIn] = Field(min_length=1, description="JSON 导入；文件导入走 multipart")
    on_conflict_action: Literal["report_only", "overwrite"] | None = Field(
        default=None, description="缺省取 users.on_conflict_action（默认 report_only）"
    )


class ImportRowOut(BaseModel):
    row: int
    username: str | None
    user_no: str | None
    action: str = Field(description="created|updated|skipped|invalid")
    reason: str | None


class ImportBatchOut(BaseModel):
    batch_no: str
    kind: str
    status: str = Field(description="completed|partial|failed")
    on_conflict_action: str
    total_rows: int
    created_rows: int
    updated_rows: int
    skipped_rows: int
    invalid_rows: int
    operator_id: int | None
    created_at: str | None
    finished_at: str | None
    rows: list[ImportRowOut] | None = Field(description="列表接口不返回逐行结果")


class ImportBatchListOut(BaseModel):
    items: list[ImportBatchOut]
    total: int
    page: int
    page_size: int


# ── 双模式（§9.3 / §9.6 · M2.10~2.11）──────────────────────────────────────


class ModePolicyOut(BaseModel):
    ttl_playlist: int
    ttl_key: int
    preview_seconds: int
    progress_interval_sec: int


class ModeCountsOut(BaseModel):
    active_tickets: int
    preview_logs: int
    quarantined_logs: int
    affected_users: int
    revoked_sessions: int
    quiz_in_progress: int
    transcode_active: int
    courses_hidden: int


class ModeStateOut(BaseModel):
    mode: Literal["internal", "external"]
    epoch: int = Field(description="每次切换 +1；请求内只读一次（MODE-FLIP-001）")
    previous_mode: str | None
    switched_at: str | None
    switched_by: int | None
    migration_batch_id: str | None
    version: int
    counts: ModeCountsOut


class ModeSummaryOut(ModeStateOut):
    other_mode: Literal["internal", "external"]
    policy: ModePolicyOut


class ModeCheckOut(BaseModel):
    code: str = Field(
        description="mode_supported|transcode_idle|quiz_idle|course_visibility|active_tickets"
        "|preview_pollution|compat_flags|self_registered"
    )
    status: Literal["ok", "warn", "blocked"]
    message: str
    count: int | None = None


class ModePrecheckIn(BaseModel):
    target_mode: Literal["internal", "external"]


class ModePrecheckOut(BaseModel):
    target_mode: Literal["internal", "external"]
    current_mode: Literal["internal", "external"]
    epoch: int
    ready: bool = Field(description="无 blocked 项即可切换")
    checks: list[ModeCheckOut]
    blockers: list[str]
    warnings: list[str]
    counts: ModeCountsOut


class ModeSwitchIn(BaseModel):
    target_mode: Literal["internal", "external"]
    epoch: int = Field(ge=1, description="当前 epoch（乐观锁，过期即 409 epoch_conflict）")
    reason: str | None = Field(default=None, max_length=200)


class ModeSwitchOut(ModeStateOut):
    reason: str | None
    warnings: list[str]
    checks: list[ModeCheckOut]


# ── 会员 / 订单 / 支付（§10.6 · M2.12）────────────────────────────────────


class MemberLevelOut(BaseModel):
    id: int
    code: str
    name: str
    level: int = Field(description="会员等级；会员课程当前统一要求 level>=1")
    price: str = Field(description="金额快照字符串（6 位小数，禁止 float）")
    duration_days: int
    is_enabled: bool
    sort_order: int
    version: int
    created_at: str | None
    updated_at: str | None


class MemberLevelListOut(BaseModel):
    items: list[MemberLevelOut]
    total: int


class MemberLevelCreateIn(BaseModel):
    code: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=64)
    level: int = Field(default=1, ge=1, le=99)
    price: str = Field(default="0", description="十进制字符串，最多 6 位小数")
    duration_days: int = Field(default=30, ge=1, le=3650)
    is_enabled: bool = True
    sort_order: int | None = Field(default=None, ge=0)


class MemberLevelUpdateIn(BaseModel):
    version: int = Field(ge=1)
    code: str | None = Field(default=None, min_length=1, max_length=32)
    name: str | None = Field(default=None, min_length=1, max_length=64)
    level: int | None = Field(default=None, ge=1, le=99)
    price: str | None = None
    duration_days: int | None = Field(default=None, ge=1, le=3650)
    is_enabled: bool | None = None
    sort_order: int | None = Field(default=None, ge=0)


class SubscriptionOut(BaseModel):
    id: int
    user_id: int
    level_id: int
    level: int
    level_code: str
    level_name: str
    source: str = Field(description="order|grant")
    order_no: str | None = Field(description="订单来源去重键；管理端赠送为空")
    start_at: str | None
    expire_at: str | None
    status: str = Field(description="active|expired|revoked")
    version: int
    created_at: str | None
    updated_at: str | None


class MemberMeOut(BaseModel):
    user_id: int
    active: bool
    level: int | None
    level_code: str | None
    level_name: str | None
    expire_at: str | None
    subscriptions: list[SubscriptionOut]


class SubscriptionListOut(BaseModel):
    items: list[SubscriptionOut]
    total: int
    page: int
    page_size: int


class MemberGrantIn(BaseModel):
    user_id: int = Field(ge=1)
    level_id: int = Field(ge=1)
    duration_days: int | None = Field(default=None, ge=1, le=3650, description="缺省取档位时长")


class MemberGrantOut(BaseModel):
    subscription: SubscriptionOut
    member: MemberMeOut


class PayOrderOut(BaseModel):
    order_no: str
    user_id: int
    level_id: int
    level: int
    level_code: str
    level_name: str
    duration_days: int
    amount: str = Field(description="下单快照金额（6 位小数）")
    currency: str
    status: str = Field(description="created|paying|paid|cancelled|expired|failed")
    channel: str
    provider_txn_id: str | None
    paid_at: str | None
    expire_at: str | None
    close_reason: str | None
    version: int
    created_at: str | None
    updated_at: str | None


class PayOrderListOut(BaseModel):
    items: list[PayOrderOut]
    total: int
    page: int
    page_size: int


class PayOrderCreateIn(BaseModel):
    level_id: int = Field(ge=1)
    channel: Literal["wechat", "alipay", "mock"] = "mock"


class PayNotifyIn(BaseModel):
    order_no: str = Field(min_length=1, max_length=32)
    event_id: str = Field(min_length=1, max_length=64, description="渠道事件号；重复回调幂等")
    status: Literal["paying", "paid", "failed"]
    amount: str = Field(description="与订单快照一致；不一致返回 PAY_AMOUNT_MISMATCH(409)")
    paid_at: str | None = None
    provider_txn_id: str | None = Field(default=None, max_length=64)


class PayNotifyOut(BaseModel):
    order_no: str
    event_id: str | None
    status: str
    applied: bool = Field(description="false 表示重复事件/状态未推进（幂等返回）")
    duplicate: bool


class PayReconcileIn(BaseModel):
    status: Literal["paying", "paid", "failed"] = "paid"
    amount: str | None = Field(default=None, description="缺省按订单快照金额")
    provider_txn_id: str | None = Field(default=None, max_length=64)
