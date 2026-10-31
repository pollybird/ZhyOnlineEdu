// 由 scripts/export_openapi.py 从后端路由与 Pydantic schema 生成，禁止手改。
// 重新生成：uv run python scripts/export_openapi.py
/* eslint-disable */

/** 统一信封：成功与失败同构，失败时 data 携带原因（§15.1）。 */
export interface ApiEnvelope<T> {
  code: number | string;
  message?: string;
  data: T;
  request_id: string;
}

export type ErrorCode =
  | 202
  | 10101
  | 40201
  | 40202
  | 40203
  | 40204
  | 40205
  | 40206
  | 50101
  | 50102
  | 50103
  | "ACCESS_CLASS_REQUIRED"
  | "ACCESS_LEVEL_REQUIRED"
  | "ACCOUNT_DISABLED"
  | "ACCOUNT_EXPIRED"
  | "AUTH_INVALID_CREDENTIALS"
  | "AUTH_REFRESH_REPLAY"
  | "AUTH_TOKEN_EXPIRED"
  | "AUTH_TOKEN_REVOKED"
  | "CATALOG_NOT_EMPTY"
  | "CONFLICT_RETRY"
  | "COURSE_ASSET_NOT_READY"
  | "COURSE_NOT_PUBLISHED"
  | "ENC-1001"
  | "ENC-1002"
  | "ENC-1003"
  | "ENC-1004"
  | "ENC-1005"
  | "ENC-1006"
  | "ENC-1007"
  | "ENC-1008"
  | "INSTALL_ALREADY_DONE"
  | "INSTALL_BLOCKED"
  | "INSTALL_REQUIRED"
  | "INSTALL_STEP_INVALID"
  | "INSTALL_TOKEN_INVALID"
  | "INTERNAL_ERROR"
  | "METHOD_NOT_ALLOWED"
  | "MODE_ACCESS_DENIED"
  | "MODE_COURSE_MISMATCH"
  | "MODE_SWITCH_BLOCKED"
  | "MODE_UNCHANGED"
  | "NOT_FOUND"
  | "OK"
  | "PAY_AMOUNT_MISMATCH"
  | "PAY_CHANNEL_MISMATCH"
  | "PAY_SIGNATURE_INVALID"
  | "PAY_STATE_INVALID"
  | "PERMISSION_DENIED"
  | "PLUGIN_CAP_DENIED"
  | "REGISTRATION_CLOSED"
  | "SUBJECT_DISABLED"
  | "TRANSCODE_PAUSED"
  | "VALIDATION_FAILED";

export interface AssetDeleteOut {
  lesson_id: number;
  deleted: boolean;
}

export interface AssetDetailOut {
  lesson_id: number;
  course_id: number | null;
  /** pending|uploaded|transcoding|encoded|failed */
  status: string;
  /** 发布成功才递增；失败保持旧值 */
  playlist_version: number;
  duration_sec: number;
  /** AES key 的匿名标识，与用户解耦 */
  kid: string | null;
  profiles: string[];
  poster_path: string | null;
  error_code: string | null;
  /** 沙箱后端/配额/ffmpeg 版本 */
  sandbox: Record<string, unknown> | null;
  updated_at: string | null;
  job: TranscodeJobOut | null;
}

export interface AssetListOut {
  items: AssetOut[];
  total: number;
}

export interface AssetOut {
  lesson_id: number;
  course_id: number | null;
  /** pending|uploaded|transcoding|encoded|failed */
  status: string;
  /** 发布成功才递增；失败保持旧值 */
  playlist_version: number;
  duration_sec: number;
  /** AES key 的匿名标识，与用户解耦 */
  kid: string | null;
  profiles: string[];
  poster_path: string | null;
  error_code: string | null;
  /** 沙箱后端/配额/ffmpeg 版本 */
  sandbox: Record<string, unknown> | null;
  updated_at: string | null;
}

export interface AuthUserOut {
  id: number;
  username: string;
  display_name: string | null;
  roles: string[];
  permissions: string[];
  token_version: number;
}

export interface BootstrapOut {
  user: AuthUserOut;
  mode: string | null;
  server_time: string;
}

export interface ChapterCreateIn {
  title: string;
}

export interface ChapterOut {
  id: number;
  course_id: number;
  title: string;
  sort_order: number;
  version: number;
  lessons: LessonOut[];
}

export interface ChapterReorderOut {
  chapter_id: number;
  ordered_ids: number[];
}

export interface ChapterUpdateIn {
  version: number;
  title?: string | null;
}

export interface ClassBriefOut {
  id: number;
  name: string;
  is_enabled: boolean;
}

export interface ClassCreateIn {
  name: string;
  org_id?: number;
  teacher_id?: number | null;
  is_enabled?: boolean;
}

export interface ClassListOut {
  items: ClassOut[];
  total: number;
  page: number;
  page_size: number;
}

export interface ClassMemberAddIn {
  /** 批量加入；已存在成员幂等跳过 */
  user_ids: number[];
}

export interface ClassMemberDeleteOut {
  class_id: number;
  user_id: number;
  removed: boolean;
}

export interface ClassMemberOut {
  user_id: number;
  user_no: string;
  username: string;
  display_name: string | null;
}

export interface ClassMembersOut {
  class_id: number;
  added: number;
  members: ClassMemberOut[];
}

export interface ClassOut {
  id: number;
  org_id: number;
  name: string;
  teacher_id: number | null;
  is_enabled: boolean;
  sort_order: number;
  member_count: number;
  version: number;
  created_at: string | null;
  updated_at: string | null;
}

export interface CourseCreateIn {
  subject_id: number;
  title: string;
  summary?: string | null;
  cover_url?: string | null;
  mode?: "internal" | "external" | "both";
  access_type?: "public" | "member" | "class";
  teacher_id?: number | null;
}

export interface CourseDetailOut {
  id: number;
  subject_id: number;
  subject_name: string | null;
  title: string;
  summary: string | null;
  cover_url: string | null;
  /** internal|external|both */
  mode: string;
  /** public|member|class */
  access_type: string;
  /** draft|published|archived */
  status: string;
  teacher_id: number | null;
  sort_order: number;
  chapter_count: number;
  lesson_count: number;
  version: number;
  published_at: string | null;
  created_at: string | null;
  updated_at: string | null;
  chapters: ChapterOut[];
}

export interface CourseListOut {
  items: CourseOut[];
  total: number;
  page: number;
  page_size: number;
}

export interface CourseOut {
  id: number;
  subject_id: number;
  subject_name: string | null;
  title: string;
  summary: string | null;
  cover_url: string | null;
  /** internal|external|both */
  mode: string;
  /** public|member|class */
  access_type: string;
  /** draft|published|archived */
  status: string;
  teacher_id: number | null;
  sort_order: number;
  chapter_count: number;
  lesson_count: number;
  version: number;
  published_at: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface CourseProgressDetailOut {
  course: CourseProgressOut;
  chapters: ProgressChapterOut[];
  metric_version: string;
  freshness_seconds: number;
  generated_at: string;
}

export interface CourseProgressListOut {
  items: CourseProgressOut[];
  total: number;
  page: number;
  page_size: number;
  /** 统计口径版本（zhy-progress-v2） */
  metric_version: string;
  /** 投影滞后秒数；前端展示「更新于 X 分钟前」 */
  freshness_seconds: number;
  generated_at: string;
}

export interface CourseProgressOut {
  course_id: number;
  course_title: string;
  subject_id: number;
  subject_name: string | null;
  cover_url: string | null;
  /** 累计屏幕观看秒数（已剔除预览） */
  watch_seconds: number;
  /** 按 1x 折算的有效学习秒数（已剔除预览） */
  speed_time: number;
  total_lessons: number;
  completed_lessons: number;
  total_duration_sec: number;
  /** 完成率 = 已完成课时 / 有效课时总数 */
  percent: number;
  completed: boolean;
  /** 最近学习课时；无记录为 null */
  resume_lesson_id: number | null;
  resume_position: number;
  first_watch_at: string | null;
  last_watch_at: string | null;
  /** 投影更新时间；异步落库允许秒级滞后 */
  updated_at: string | null;
}

export interface CoursePublishOut {
  course: CourseOut;
  ready: boolean;
  not_ready: NotReadyLessonOut[];
  published_at: string | null;
}

export interface CourseReorderOut {
  course_id: number;
  ordered_ids: number[];
}

export interface CourseUnpublishOut {
  course: CourseOut;
}

export interface CourseUpdateIn {
  version: number;
  subject_id?: number | null;
  title?: string | null;
  summary?: string | null;
  cover_url?: string | null;
  mode?: "internal" | "external" | "both" | null;
  access_type?: "public" | "member" | "class" | null;
  teacher_id?: number | null;
}

export interface DbSetupOut {
  /** start|db|admin|mode|finish|done */
  step: string;
  is_installed: boolean;
  schema_rev: string | null;
  flavor: string | null;
  blockers: string[];
  /** 向导步骤顺序 */
  steps: string[];
  already: boolean;
}

export interface DbTestOut {
  ok: boolean;
  flavor: string | null;
  dialect: string | null;
  caps: Record<string, boolean>;
  warnings: string[];
  blockers: string[];
}

export interface DeleteOut {
  id: number;
  deleted: boolean;
}

export interface ErrorEnvelope {
  code: string | number;
  message?: string;
  data: unknown;
  request_id: string;
}

export interface ImportBatchListOut {
  items: ImportBatchOut[];
  total: number;
  page: number;
  page_size: number;
}

export interface ImportBatchOut {
  batch_no: string;
  kind: string;
  /** completed|partial|failed */
  status: string;
  on_conflict_action: string;
  total_rows: number;
  created_rows: number;
  updated_rows: number;
  skipped_rows: number;
  invalid_rows: number;
  operator_id: number | null;
  created_at: string | null;
  finished_at: string | null;
  /** 列表接口不返回逐行结果 */
  rows: ImportRowOut[] | null;
}

export interface ImportRowIn {
  username: string;
  password?: string | null;
  user_no?: string | null;
  display_name?: string | null;
  expire_at?: string | null;
}

export interface ImportRowOut {
  row: number;
  username: string | null;
  user_no: string | null;
  /** created|updated|skipped|invalid */
  action: string;
  reason: string | null;
}

export interface InstallAdminIn {
  username: string;
  password: string;
}

export interface InstallAdminOut {
  username: string;
  step: string;
  already: boolean;
}

export interface InstallFinishOut {
  installed: boolean;
  step: string;
  /** 业务路由需重启进程后装配 */
  restart_required: boolean;
  flavor: string | null;
  caps: Record<string, boolean>;
  warnings: string[];
  admin: string | null;
}

export interface InstallModeIn {
  mode: "internal" | "external";
}

export interface InstallModeOut {
  mode: string;
  step: string;
  already: boolean;
}

export interface InstallStateOut {
  /** start|db|admin|mode|finish|done */
  step: string;
  is_installed: boolean;
  schema_rev: string | null;
  flavor: string | null;
  blockers: string[];
  /** 向导步骤顺序 */
  steps: string[];
}

export interface InstallTokenIn {
  /** 启动日志或 ZHY_INSTALL_TOKEN 中的一次性安装令牌 */
  token: string;
}

export interface InstallTokenOut {
  /** 后续安装请求放入 X-ZHY-Install-Session */
  session: string;
  expires_in: number;
}

export interface LearnChapterOut {
  chapter_id: number;
  title: string;
  lessons: LearnLessonOut[];
}

export interface LearnCourseDetailOut {
  course: LearnCourseOut;
  chapters: LearnChapterOut[];
  metric_version: string;
  freshness_seconds: number;
  generated_at: string;
}

export interface LearnCourseListOut {
  items: LearnCourseOut[];
  total: number;
  page: number;
  page_size: number;
  metric_version: string;
  freshness_seconds: number;
  generated_at: string;
}

export interface LearnCourseOut {
  course_id: number;
  title: string;
  summary: string | null;
  cover_url: string | null;
  subject_id: number;
  subject_name: string | null;
  /** public|member|class */
  access_type: string;
  /** internal|external|both */
  mode: string;
  total_lessons: number;
  total_duration_sec: number;
  /** 首个可播课时；空课程为 null */
  first_lesson_id: number | null;
  /** UI 提示，最终裁决在取票接口 */
  access_ok: boolean;
  progress: LearnCourseProgressOut | null;
}

export interface LearnCourseProgressOut {
  /** 本人该课程累计观看秒数（已剔除预览） */
  watch_seconds: number;
  /** 完成率 = 已完成课时 / 有效课时总数 */
  percent: number;
  completed: boolean;
  completed_lessons: number;
  resume_lesson_id: number | null;
  resume_position: number;
  last_watch_at: string | null;
}

export interface LearnLessonOut {
  lesson_id: number;
  title: string;
  kind: string;
  duration_sec: number;
  is_free: boolean;
  /** encoded 才可播放；None 表示未挂载 */
  asset_status: string | null;
  progress: LearnLessonProgressOut | null;
}

export interface LearnLessonProgressOut {
  watch_seconds: number;
  percent: number;
  completed: boolean;
  /** 续播位置（秒） */
  last_position: number;
  last_watch_at: string | null;
}

export interface LessonCreateIn {
  title: string;
  kind?: "video" | "text" | "live";
  is_free?: boolean;
}

export interface LessonOut {
  id: number;
  chapter_id: number;
  course_id: number;
  title: string;
  /** video|text|live */
  kind: string;
  duration_sec: number;
  is_free: boolean;
  sort_order: number;
  version: number;
  /** None=尚未挂载资产；encoded 才可上架 */
  asset_status: string | null;
}

export interface LessonUpdateIn {
  version: number;
  title?: string | null;
  kind?: "video" | "text" | "live" | null;
  is_free?: boolean | null;
}

export interface LoginIn {
  username: string;
  password: string;
}

export interface LoginOut {
  access_token: string;
  token_type: "Bearer";
  /** AT 有效期（秒），即 tv 窗口长度 */
  expires_in: number;
  user: AuthUserOut;
}

export interface LogoutOut {
  logged_out: boolean;
  session_revoked: boolean;
}

export interface MemberGrantIn {
  user_id: number;
  level_id: number;
  /** 缺省取档位时长 */
  duration_days?: number | null;
}

export interface MemberGrantOut {
  subscription: SubscriptionOut;
  member: MemberMeOut;
}

export interface MemberLevelCreateIn {
  code: string;
  name: string;
  level?: number;
  /** 十进制字符串，最多 6 位小数 */
  price?: string;
  duration_days?: number;
  is_enabled?: boolean;
  sort_order?: number | null;
}

export interface MemberLevelListOut {
  items: MemberLevelOut[];
  total: number;
}

export interface MemberLevelOut {
  id: number;
  code: string;
  name: string;
  /** 会员等级；会员课程当前统一要求 level>=1 */
  level: number;
  /** 金额快照字符串（6 位小数，禁止 float） */
  price: string;
  duration_days: number;
  is_enabled: boolean;
  sort_order: number;
  version: number;
  created_at: string | null;
  updated_at: string | null;
}

export interface MemberLevelUpdateIn {
  version: number;
  code?: string | null;
  name?: string | null;
  level?: number | null;
  price?: string | null;
  duration_days?: number | null;
  is_enabled?: boolean | null;
  sort_order?: number | null;
}

export interface MemberMeOut {
  user_id: number;
  active: boolean;
  level: number | null;
  level_code: string | null;
  level_name: string | null;
  expire_at: string | null;
  subscriptions: SubscriptionOut[];
}

export interface ModeCheckOut {
  /** mode_supported|transcode_idle|quiz_idle|course_visibility|active_tickets|preview_pollution|compat_flags|self_registered */
  code: string;
  status: "ok" | "warn" | "blocked";
  message: string;
  count?: number | null;
}

export interface ModeCountsOut {
  active_tickets: number;
  preview_logs: number;
  quarantined_logs: number;
  affected_users: number;
  revoked_sessions: number;
  quiz_in_progress: number;
  transcode_active: number;
  courses_hidden: number;
}

export interface ModePolicyOut {
  ttl_playlist: number;
  ttl_key: number;
  preview_seconds: number;
  progress_interval_sec: number;
}

export interface ModePrecheckIn {
  target_mode: "internal" | "external";
}

export interface ModePrecheckOut {
  target_mode: "internal" | "external";
  current_mode: "internal" | "external";
  epoch: number;
  /** 无 blocked 项即可切换 */
  ready: boolean;
  checks: ModeCheckOut[];
  blockers: string[];
  warnings: string[];
  counts: ModeCountsOut;
}

export interface ModeSummaryOut {
  mode: "internal" | "external";
  /** 每次切换 +1；请求内只读一次（MODE-FLIP-001） */
  epoch: number;
  previous_mode: string | null;
  switched_at: string | null;
  switched_by: number | null;
  migration_batch_id: string | null;
  version: number;
  counts: ModeCountsOut;
  other_mode: "internal" | "external";
  policy: ModePolicyOut;
}

export interface ModeSwitchIn {
  target_mode: "internal" | "external";
  /** 当前 epoch（乐观锁，过期即 409 epoch_conflict） */
  epoch: number;
  reason?: string | null;
}

export interface ModeSwitchOut {
  mode: "internal" | "external";
  /** 每次切换 +1；请求内只读一次（MODE-FLIP-001） */
  epoch: number;
  previous_mode: string | null;
  switched_at: string | null;
  switched_by: number | null;
  migration_batch_id: string | null;
  version: number;
  counts: ModeCountsOut;
  reason: string | null;
  warnings: string[];
  checks: ModeCheckOut[];
}

export interface MyQuizSummaryOut {
  /** 已提交作答次数 */
  attempts: number;
  passed: number;
  average_percent: number;
  best_percent: number;
}

export interface MySummaryOut {
  user_id: number;
  courses_started: number;
  courses_completed: number;
  lessons_completed: number;
  watch_seconds: number;
  speed_time: number;
  /** 今日有效观看秒数（source=play，已剔除预览） */
  today_watch_seconds: number;
  recent_courses: CourseProgressOut[];
  quiz: MyQuizSummaryOut;
  metric_version: string;
  freshness_seconds: number;
  generated_at: string;
}

export interface NotReadyLessonOut {
  lesson_id: number;
  title: string;
  asset_status: string | null;
}

export interface PasswordIn {
  old_password: string;
  new_password: string;
}

export interface PasswordOut {
  token_version: number;
  sessions_revoked: number;
  /** 旧 AT 仍可用的剩余窗口 */
  at_window_sec: number;
  relogin_required: boolean;
}

export interface PayNotifyIn {
  order_no: string;
  /** 渠道事件号；重复回调幂等 */
  event_id: string;
  status: "paying" | "paid" | "failed";
  /** 与订单快照一致；不一致返回 PAY_AMOUNT_MISMATCH(409) */
  amount: string;
  paid_at?: string | null;
  provider_txn_id?: string | null;
}

export interface PayNotifyOut {
  order_no: string;
  event_id: string | null;
  status: string;
  /** false 表示重复事件/状态未推进（幂等返回） */
  applied: boolean;
  duplicate: boolean;
}

export interface PayOrderCreateIn {
  level_id: number;
  channel?: "wechat" | "alipay" | "mock";
}

export interface PayOrderListOut {
  items: PayOrderOut[];
  total: number;
  page: number;
  page_size: number;
}

export interface PayOrderOut {
  order_no: string;
  user_id: number;
  level_id: number;
  level: number;
  level_code: string;
  level_name: string;
  duration_days: number;
  /** 下单快照金额（6 位小数） */
  amount: string;
  currency: string;
  /** created|paying|paid|cancelled|expired|failed */
  status: string;
  channel: string;
  provider_txn_id: string | null;
  paid_at: string | null;
  expire_at: string | null;
  close_reason: string | null;
  version: number;
  created_at: string | null;
  updated_at: string | null;
}

export interface PayReconcileIn {
  status?: "paying" | "paid" | "failed";
  /** 缺省按订单快照金额 */
  amount?: string | null;
  provider_txn_id?: string | null;
}

export interface ProgressChapterOut {
  chapter_id: number;
  title: string;
  /** 仅返回已学课时，未学不泄露课程结构 */
  lessons: ProgressLessonOut[];
}

export interface ProgressGateOut {
  shard: string;
  writer_id: string;
  leased_until: string | null;
  /** 网关已分配的全局水位 */
  last_gw_seq: number;
  /** 已落库流水的最大 gw_seq */
  flushed_max_gw_seq: number;
  pending_logs: number;
  /** last_gw_seq - flushed_max_gw_seq；0 表示无缺口 */
  gap: number;
  /** 最老未落库流水距今秒数（投影滞后） */
  lag_seconds: number;
  last_flush_at: string | null;
  healthy: boolean;
  metric_version: string;
  generated_at: string;
}

export interface ProgressIn {
  lesson_id: number;
  /** 播放头位置；超过时长 +60s 拒绝 */
  position_sec: number;
  /** 本次上报新增播放秒数（1x） */
  played_seconds: number;
  /** 倍速 0.5..3.0 */
  speed?: number;
  /** 客户端单调时钟，仅审计 */
  client_ts_ms?: number;
}

export interface ProgressLessonOut {
  lesson_id: number;
  title: string;
  /** video|text|live */
  kind: string;
  duration_sec: number;
  watch_seconds: number;
  speed_time: number;
  percent: number;
  last_position: number;
  completed: boolean;
  completed_at: string | null;
  last_watch_at: string | null;
}

export interface ProgressOut {
  /** 网关分配的全局单调令牌 */
  gw_seq: number;
  accepted_at: string;
  /** 同一 30s 桶已计过，本次 played_seconds 不再累加 */
  deduped: boolean;
}

export interface QuizAnswerIn {
  question_id: number;
  /** 所选选项键；集合完全相等才得分 */
  choices: string[];
}

export interface QuizAttemptBriefOut {
  id: number;
  status: string;
  score_earned: number;
  score_total: number;
  percent: number;
  passed: boolean;
  started_at: string | null;
  submitted_at: string | null;
}

export interface QuizAttemptCreatedOut {
  attempt: QuizAttemptOut;
  /** 已有进行中作答时返回 false，不重复扣题 */
  created: boolean;
}

export interface QuizAttemptListOut {
  items: QuizAttemptBriefOut[];
  total: number;
  page: number;
  page_size: number;
}

export interface QuizAttemptOut {
  id: number;
  /** 同时作为交卷的 Idempotency-Key */
  attempt_uid: string;
  lesson_id: number;
  course_id: number;
  /** in_progress|submitted|expired */
  status: string;
  question_count: number;
  score_total: number;
  score_earned: number;
  percent: number;
  passed: boolean;
  questions: QuizSnapshotItemOut[];
  /** 未交卷为 null */
  result: QuizResultItemOut[] | null;
  started_at: string | null;
  submitted_at: string | null;
}

export interface QuizEntryOut {
  lesson_id: number;
  course_id: number;
  question_count: number;
  score_total: number;
  /** 及格线（百分比），来自 quiz.pass_score */
  pass_score: number;
  /** 进行中的作答；无则 null */
  attempt: QuizAttemptOut | null;
  /** 最近一次已提交作答 */
  last: QuizAttemptBriefOut | null;
}

export interface QuizOptionOut {
  /** 选项键（单选/多选 A/B/C…；判断题为 T/F） */
  key: string;
  text: string;
}

export interface QuizQuestionIn {
  lesson_id: number;
  kind?: "single" | "multiple" | "judge";
  stem: string;
  /** 判断题可省略（服务端固定生成 T/F） */
  options?: QuizOptionOut[] | null;
  /** 选项键数组；多选至少 2 项 */
  answer: string[];
  explanation?: string | null;
  score?: number;
  is_enabled?: boolean;
}

export interface QuizQuestionListOut {
  items: QuizQuestionOut[];
  total: number;
  page: number;
  page_size: number;
}

export interface QuizQuestionOut {
  id: number;
  course_id: number;
  lesson_id: number;
  /** single|multiple|judge */
  kind: string;
  stem: string;
  options: QuizOptionOut[];
  /** 标准答案选项键；学员端响应永不包含此字段 */
  answer: string[];
  explanation: string | null;
  score: number;
  sort_order: number;
  is_enabled: boolean;
  version: number;
  created_at: string | null;
  updated_at: string | null;
}

export interface QuizQuestionUpdateIn {
  version: number;
  kind?: "single" | "multiple" | "judge" | null;
  stem?: string | null;
  options?: QuizOptionOut[] | null;
  answer?: string[] | null;
  explanation?: string | null;
  score?: number | null;
  is_enabled?: boolean | null;
}

export interface QuizResultItemOut {
  question_id: number;
  correct: boolean;
  score: number;
  score_earned: number;
  /** 教师撰写解析；不含标准答案选项 */
  explanation: string | null;
}

export interface QuizSnapshotItemOut {
  question_id: number;
  kind: string;
  stem: string;
  options: QuizOptionOut[];
  score: number;
}

export interface QuizStartIn {
  lesson_id: number;
}

export interface QuizSubmitIn {
  answers: QuizAnswerIn[];
}

export interface QuizSubmitOut {
  attempt: QuizAttemptOut;
  /** false 表示同一 Idempotency-Key 重放，成绩未重算 */
  created: boolean;
}

export interface ReorderIn {
  /** 父级（课程/章）的乐观锁版本 */
  version: number;
  /** 必须覆盖全部同级节点 */
  ordered_ids: number[];
}

export interface RetranscodeIn {
  /** 缺省取 transcode.profiles */
  profiles?: string[] | null;
}

export interface RetranscodeOut {
  job: TranscodeJobOut;
  /** 已有排队/执行中的任务时返回 false */
  created: boolean;
}

export interface SubjectCreateIn {
  name: string;
  /** 为空即根节点 */
  parent_id?: number | null;
}

export interface SubjectListOut {
  items: SubjectOut[];
  total: number;
}

export interface SubjectOut {
  id: number;
  parent_id: number | null;
  name: string;
  sort_order: number;
  is_enabled: boolean;
  /** 乐观锁版本，写操作必须回传 */
  version: number;
  created_at: string | null;
  updated_at: string | null;
}

export interface SubjectUpdateIn {
  version: number;
  name?: string | null;
  /** 传 null 移动到根节点 */
  parent_id?: number | null;
  /** 同级排序：插到该节点之前 */
  before_id?: number | null;
  /** 停用后其课程不可上架 */
  is_enabled?: boolean | null;
}

export interface SubscriptionListOut {
  items: SubscriptionOut[];
  total: number;
  page: number;
  page_size: number;
}

export interface SubscriptionOut {
  id: number;
  user_id: number;
  level_id: number;
  level: number;
  level_code: string;
  level_name: string;
  /** order|grant */
  source: string;
  /** 订单来源去重键；管理端赠送为空 */
  order_no: string | null;
  start_at: string | null;
  expire_at: string | null;
  /** active|expired|revoked */
  status: string;
  version: number;
  created_at: string | null;
  updated_at: string | null;
}

export interface TicketIn {
  lesson_id: number;
}

export interface TicketOut {
  lesson_id: number;
  course_id: number;
  /** 资产匿名标识；与用户解耦，可上 CDN */
  kid: string;
  asset_status: string;
  /** true 时进度只落流水、不计入统计 */
  preview: boolean;
  /** 本次票据允许的档位 */
  profiles: string[];
  /** master.m3u8 地址，已带 ks/n/exp/sig */
  playlist_url: string;
  /** playlist 签名有效期（秒） */
  playlist_ttl: number;
  /** key 签名有效期（秒） */
  key_ttl: number;
  /** keysession 失效时间（最长层） */
  ticket_expires_at: string;
  /** 断点续播位置（无记录为 0） */
  resume_second: number;
  watermark: WatermarkOut;
  /** 统计口径版本（zhy-progress-v2） */
  metric_version: string;
}

export interface TranscodeJobOut {
  id: number;
  lesson_id: number;
  /** queued|running|succeeded|failed|canceled */
  status: string;
  attempt: number;
  profiles: string[];
  /** ENC-10xx；stderr 不对外暴露 */
  error_code: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface UploadChunkOut {
  upload_id: string;
  index: number;
  received: number;
  total_chunks: number;
  /** 同 index 同 md5 重传时幂等返回 true */
  duplicated: boolean;
}

export interface UploadCompleteOut {
  upload_id: string;
  status: string;
  lesson_id: number;
  /** uploaded 表示已通过检疫，等待 M2.4 转码 */
  asset_status: string;
  format_name: string;
  duration_sec: number;
  width: number | null;
  height: number | null;
  /** ffprobe|magic */
  probe_source: string;
  sha256: string;
  ready_for_transcode: boolean;
}

export interface UploadDeleteOut {
  upload_id: string;
  deleted: boolean;
}

export interface UploadInitIn {
  lesson_id: number;
  /** 不含路径分隔符 */
  file_name: string;
  /** 整文件字节数；超过 upload.max_file_bytes 拒绝 */
  file_size: number;
  /** 整文件 sha256 hex */
  sha256: string;
  /** 默认取 upload.chunk_size；分片数受 upload.max_chunks 限制 */
  chunk_size?: number;
}

export interface UploadInitOut {
  /** 32 位 hex；后续接口与 tmp 子目录共用 */
  upload_id: string;
  status: string;
  chunk_size: number;
  total_chunks: number;
  expires_at: string;
}

export interface UploadStatusOut {
  upload_id: string;
  /** open|processing|completed|rejected|expired|deleted */
  status: string;
  file_name: string;
  file_size: number;
  chunk_size: number;
  total_chunks: number;
  received_chunks: number[];
  missing_chunks: number[];
  expires_at: string;
  error_code: string | null;
}

export interface UserCreateIn {
  username: string;
  /** Argon2id 入库，不回显 */
  password: string;
  /** 缺省自动生成 */
  user_no?: string | null;
  display_name?: string | null;
  org_id?: number;
  /** ISO8601；为空表示不限期 */
  expire_at?: string | null;
  /** 缺省 student；super_admin/admin/teacher/student */
  roles?: string[] | null;
}

export interface UserDisableIn {
  version: number;
}

export interface UserExpireIn {
  version: number;
  /** 缺省为当前时间 */
  expire_at?: string | null;
}

export interface UserImportIn {
  /** JSON 导入；文件导入走 multipart */
  rows: ImportRowIn[];
  /** 缺省取 users.on_conflict_action（默认 report_only） */
  on_conflict_action?: "report_only" | "overwrite" | null;
}

export interface UserListOut {
  items: UserOut[];
  total: number;
  page: number;
  page_size: number;
}

export interface UserOut {
  id: number;
  org_id: number;
  /** 组织内学号/工号；与 org_id 组成软删自然键 */
  user_no: string;
  username: string;
  display_name: string | null;
  /** active|disabled|expired */
  status: string;
  expire_at: string | null;
  roles: string[];
  classes: ClassBriefOut[];
  class_count: number;
  version: number;
  last_login_at: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface UserUpdateIn {
  version: number;
  username?: string | null;
  password?: string | null;
  user_no?: string | null;
  display_name?: string | null;
  /** 传空串或 null 表示清除 */
  expire_at?: string | null;
  roles?: string[] | null;
}

export interface VersionIn {
  /** 乐观锁版本；不匹配返回 CONFLICT_RETRY */
  version: number;
}

export interface WatermarkOut {
  /** 水印文字（用户名；不含完整 IP） */
  text: string;
  opacity: number;
  /** br|bl|tr|tl */
  position: string;
}
