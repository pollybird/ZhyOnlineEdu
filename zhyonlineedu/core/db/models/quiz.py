"""题库与作答（§6.8 / §10.4 · M2.6）。

口径：
- `quiz_question` 挂课时，题型 `single` / `multiple` / `judge`；`answer` 只存服务端，
  **任何学员端响应都不得下发**（M2.6 验收口径一）；
- `quiz_attempt` 是作答事实：开始作答时冻结 `question_snapshot`（可下发，不含答案）与
  `answer_key`（不可下发），交卷判分只读快照 → 题库事后修改不影响在途作答；
- `attempt_uid` 是幂等自然键（`Idempotency-Key`）：唯一索引 + 条件更新，
  同一 key 重试只返回首次结果，绝不产生第二份成绩（M2.6 验收口径二）。
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Boolean, Index, Integer, Numeric, String, Text, false
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base
from ..types import SoftDeleteMixin, UTCDatetime, VersionedMixin


def utcnow() -> datetime:
    return datetime.now(UTC)


QUESTION_KINDS = ("single", "multiple", "judge")
ATTEMPT_STATUSES = ("in_progress", "submitted", "expired")


class QuizQuestion(SoftDeleteMixin, VersionedMixin, Base):
    """题目：`options` 与 `answer` 均为 JSON 文本。

    `answer` 存选项键数组（如 `["A"]` / `["A","C"]` / `["T"]`）；
    judge 题型的选项由服务端固定生成，不接受客户端自定义。
    """

    __tablename__ = "quiz_question"
    # D19a：软删唯一键 (lesson_id, stem, delete_seq)，避免同课时同题干重复录题
    __soft_delete_uq__ = ("lesson_id", "stem")
    __table_args__ = (
        Index("ix_quiz_question_lesson", "lesson_id"),
        Index("ix_quiz_question_course", "course_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    course_id: Mapped[int] = mapped_column(Integer, nullable=False)
    # 题库挂课时：判分/入口都按课时聚合；course_id 冗余供后台按课程筛选
    lesson_id: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(
        String(16), nullable=False, default="single", server_default="single"
    )
    stem: Mapped[str] = mapped_column(Text, nullable=False)
    options: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str] = mapped_column(Text, nullable=False)
    explanation: Mapped[str | None] = mapped_column(Text, nullable=True)
    score: Mapped[int] = mapped_column(Integer, nullable=False, default=5, server_default="5")
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=1024)
    is_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1"
    )
    created_at: Mapped[datetime] = mapped_column(UTCDatetime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDatetime, nullable=False, default=utcnow)


class QuizAttempt(Base):
    """一次作答：开始即冻结题目快照与答案键，交卷只做一次条件更新。"""

    __tablename__ = "quiz_attempt"
    __table_args__ = (
        Index("uq_quiz_attempt_uid", "attempt_uid", unique=True),
        Index("ix_quiz_attempt_user", "user_id"),
        Index("ix_quiz_attempt_lesson", "lesson_id"),
        Index("ix_quiz_attempt_course", "course_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 32 hex：同时充当 Idempotency-Key 与自然键（§10.4 交卷幂等）
    attempt_uid: Mapped[str] = mapped_column(String(48), nullable=False)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    lesson_id: Mapped[int] = mapped_column(Integer, nullable=False)
    course_id: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="in_progress", server_default="in_progress"
    )
    question_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    score_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    score_earned: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    percent: Mapped[float] = mapped_column(
        Numeric(5, 2), nullable=False, default=0, server_default="0"
    )
    passed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    # 可下发快照（题干 + 选项 + 分值，无答案）
    question_snapshot: Mapped[str] = mapped_column(Text, nullable=False)
    # 不可下发答案键（question_id / choices / score / explanation）
    answer_key: Mapped[str] = mapped_column(Text, nullable=False)
    answers: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 幂等指纹：同 key 但 payload 不同 → 409 CONFLICT_RETRY（不覆盖首次事实）
    request_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, default="", server_default=""
    )
    started_at: Mapped[datetime] = mapped_column(UTCDatetime, nullable=False, default=utcnow)
    submitted_at: Mapped[datetime | None] = mapped_column(UTCDatetime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDatetime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDatetime, nullable=False, default=utcnow)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
