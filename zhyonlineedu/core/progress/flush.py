"""进度落库（单写者 · §12.5 · F2 · M2.5）。

**谁写 `learn_progress`**：只有本模块（`ProgressFlush.flush_once`），且由
`learn_progress_gate` 的租约保证同一时刻只有一个写者（多实例重复启动也安全）。

算法（崩溃/重放安全）：
1. 抢租约（`leased_until <= now OR writer_id = 自己` 的条件更新）；
2. 取 `learn_progress_log` 中 `flushed_at IS NULL` 的行（按 id，批量 `flush_batch`）；
3. `preview` 行只打 flushed 标记（**不进统计**，§9.4 #18）；
4. 对受影响的 (user, lesson) **按 log 汇总重算绝对值**，再以
   `watch_seconds = GREATEST(watch_seconds, :sum)` 写入投影（红线：绝不回退）；
5. 提交后 `bus.ack(up_to=最大 gw_seq)`，再推进 `last_gw_seq` / `last_flush_at`。

因为第 4 步是"重算绝对值"而非"累加增量"，同一行被重放两次也不会双计；
即使总线文件全部丢失，第 2 步仍能从 DB 事实源补齐（HBT-002 兜底）。
"""

from __future__ import annotations

import logging
import os
import socket
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import case, func, select, text, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from zhyonlineedu.core.config import Config
from zhyonlineedu.core.db.compat import FLAVOR_SPECS, Dialect
from zhyonlineedu.core.db.engine import Database, ensure_database
from zhyonlineedu.core.db.models.catalog import EduCourseLesson
from zhyonlineedu.core.db.models.learn import (
    DEFAULT_SHARD,
    LearnCourseProgress,
    LearnProgress,
    LearnProgressGate,
    LearnProgressLog,
    utcnow,
)
from zhyonlineedu.core.db.query import not_deleted
from zhyonlineedu.core.progress.bus import ProgressBus, get_bus

logger = logging.getLogger(__name__)


def greatest_sql(dialect: Dialect, column: str, param: str) -> str:
    """按方言产出"取大"表达式；SQLite 无 GREATEST，用两参 MAX 标量函数。"""
    if dialect is Dialect.SQLITE:
        return f"MAX({column}, :{param})"
    return f"GREATEST({column}, :{param})"


def writer_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


class ProgressFlush:
    def __init__(
        self,
        config: Config,
        *,
        db: Database | None = None,
        bus: ProgressBus | None = None,
    ) -> None:
        self.config = config
        self._db = db
        self._bus = bus or get_bus(
            config.get_str("progress.bus"),
            ring_dir=config.get_str("progress.ring_dir"),
            url=config.get_str("redis.url"),
        )
        self.writer = writer_id()

    @property
    def db(self) -> Database:
        if self._db is None:
            self._db = ensure_database(self.config)
        return self._db

    # ── 主入口 ────────────────────────────────────────────────────────────

    def flush_once(self, *, now: datetime | None = None, batch: int | None = None) -> int:
        """返回本次落库的 log 行数；0 表示无新数据或未抢到租约。

        按 (user, lesson) 分组、每组一个短事务提交（M2.16 压测实测）：
        1000 行一次性提交 + 逐用户重算投影会让 SQLite 写锁持有超过 5s，
        网关写入方等锁超时后 HTTP 500。按用户拆事务后，单次写锁只持有毫秒级，
        同一次 flush 内同一用户的多行也只重算一次投影。投影仍是绝对值重算，
        `GREATEST` 不回退与重放安全的语义不变。
        """
        moment = now or utcnow()
        size = batch if batch is not None else self.config.get_int("progress.flush_batch")
        if size <= 0:
            return 0
        if not self._acquire_lease(moment):
            logger.debug("progress flush 未获得单写者租约，跳过本轮")
            return 0

        with Session(self.db.engine) as session:
            rows = list(
                session.execute(
                    select(LearnProgressLog)
                    .where(LearnProgressLog.flushed_at.is_(None))
                    .order_by(LearnProgressLog.id)
                    .limit(max(1, size))
                )
                .scalars()
                .all()
            )
            if not rows:
                self._extend_lease(session, moment)
                session.commit()
                return 0

        preview_ids: list[int] = []
        groups: dict[tuple[int, int, int], list[int]] = {}
        max_gw_seq = 0
        for row in rows:
            max_gw_seq = max(max_gw_seq, int(row.gw_seq))
            if row.source == "preview":
                preview_ids.append(int(row.id))
            else:
                key = (int(row.user_id), int(row.lesson_id), int(row.course_id))
                groups.setdefault(key, []).append(int(row.id))

        if preview_ids:
            with Session(self.db.engine) as session:
                self._mark_flushed(session, preview_ids, moment)
                self._extend_lease(session, moment)
                session.commit()

        for (user_id, lesson_id, course_id), ids in groups.items():
            with Session(self.db.engine) as session:
                self._mark_flushed(session, ids, moment)
                self._project(session, user_id, lesson_id, course_id, moment)
                self._project_course(session, user_id, course_id, moment)
                self._extend_lease(session, moment)
                session.commit()

        try:
            self._bus.ack(max_gw_seq)
        except (OSError, RuntimeError):
            logger.exception("progress bus ack 失败；log 已落库，下一轮会再消费")
        return len(rows)

    @staticmethod
    def _mark_flushed(session: Session, ids: list[int], moment: datetime) -> None:
        session.execute(
            update(LearnProgressLog).where(LearnProgressLog.id.in_(ids)).values(flushed_at=moment)
        )

    # ── 投影 ──────────────────────────────────────────────────────────────

    def _project(
        self, session: Session, user_id: int, lesson_id: int, course_id: int, moment: datetime
    ) -> None:
        agg = session.execute(
            select(
                func.coalesce(func.sum(LearnProgressLog.played_seconds), 0),
                func.coalesce(func.max(LearnProgressLog.position), 0),
                func.coalesce(func.max(LearnProgressLog.speed_time), 0),
                func.min(LearnProgressLog.received_at),
                func.max(LearnProgressLog.received_at),
            ).where(
                LearnProgressLog.user_id == user_id,
                LearnProgressLog.lesson_id == lesson_id,
                LearnProgressLog.source == "play",
                LearnProgressLog.quarantined.is_(False),
            )
        ).one()
        watch = max(int(agg[0]), 0)
        last_position = max(int(agg[1]), 0)
        speed_time = max(int(agg[2]), 0)
        duration = self._duration(session, lesson_id)
        percent = self._percent(watch, duration)
        completed = percent >= self.config.get_int("progress.completion_threshold") and (
            duration <= 0 or last_position + 10 >= duration
        )
        dialect = FLAVOR_SPECS[self.db.flavor].dialect

        row = session.execute(
            select(LearnProgress).where(
                LearnProgress.user_id == user_id, LearnProgress.lesson_id == lesson_id
            )
        ).scalar_one_or_none()
        if row is None:
            session.add(
                LearnProgress(
                    user_id=user_id,
                    lesson_id=lesson_id,
                    course_id=course_id,
                    watch_seconds=watch,
                    duration_sec=duration,
                    percent=Decimal(str(percent)),
                    last_position=last_position,
                    completed=completed,
                    completed_at=moment if completed else None,
                    first_watch_at=agg[3] or moment,
                    last_watch_at=agg[4] or moment,
                    speed_time=speed_time,
                    source="recovery",
                )
            )
            return

        # GREATEST：重算值低于库内值时不回退（红线 2）
        session.execute(
            text(
                "UPDATE learn_progress SET "  # noqa: S608 - 列名/表名均为常量
                f"watch_seconds = {greatest_sql(dialect, 'watch_seconds', 'watch')}, "
                f"last_position = {greatest_sql(dialect, 'last_position', 'pos')}, "
                f"percent = {greatest_sql(dialect, 'percent', 'pct')}, "
                f"speed_time = {greatest_sql(dialect, 'speed_time', 'stime')}, "
                "duration_sec = :duration, "
                "completed = CASE WHEN completed = :false THEN :completed ELSE completed END, "
                "completed_at = CASE WHEN completed_at IS NULL AND :completed = :true "
                "THEN :moment ELSE completed_at END, "
                "last_watch_at = :last_watch, course_id = :course_id, "
                "version = version + 1 "
                "WHERE user_id = :uid AND lesson_id = :lid"
            ).bindparams(
                watch=watch,
                pos=last_position,
                pct=float(percent),
                stime=speed_time,
                duration=duration,
                completed=1 if completed else 0,
                false=0,
                true=1,
                moment=moment,
                last_watch=agg[4] or moment,
                course_id=course_id,
                uid=user_id,
                lid=lesson_id,
            )
        )

    def _project_course(
        self, session: Session, user_id: int, course_id: int, moment: datetime
    ) -> None:
        """课程维度重算（§10.7）：只聚合非预览流水落成的 `learn_progress`。"""
        agg = session.execute(
            select(
                func.coalesce(func.sum(LearnProgress.watch_seconds), 0),
                func.coalesce(func.sum(LearnProgress.speed_time), 0),
                func.coalesce(func.sum(case((LearnProgress.completed.is_(True), 1), else_=0)), 0),
                func.min(LearnProgress.first_watch_at),
                func.max(LearnProgress.last_watch_at),
            )
            .join(EduCourseLesson, EduCourseLesson.id == LearnProgress.lesson_id)
            .where(
                LearnProgress.user_id == user_id,
                LearnProgress.course_id == course_id,
                not_deleted(EduCourseLesson),
            )
        ).one()
        totals = session.execute(
            select(
                func.count(),
                func.coalesce(func.sum(EduCourseLesson.duration_sec), 0),
            ).where(EduCourseLesson.course_id == course_id, not_deleted(EduCourseLesson))
        ).one()
        total_lessons = int(totals[0])
        total_duration = int(totals[1])

        watch = max(int(agg[0]), 0)
        speed_time = max(int(agg[1]), 0)
        completed_lessons = min(max(int(agg[2]), 0), total_lessons)
        percent = self._course_percent(completed_lessons, total_lessons)
        completed = total_lessons > 0 and completed_lessons >= total_lessons

        resume = session.execute(
            select(LearnProgress.lesson_id, LearnProgress.last_position)
            .join(EduCourseLesson, EduCourseLesson.id == LearnProgress.lesson_id)
            .where(
                LearnProgress.user_id == user_id,
                LearnProgress.course_id == course_id,
                not_deleted(EduCourseLesson),
            )
            .order_by(LearnProgress.last_watch_at.desc(), LearnProgress.id.desc())
            .limit(1)
        ).first()

        row = session.execute(
            select(LearnCourseProgress).where(
                LearnCourseProgress.user_id == user_id,
                LearnCourseProgress.course_id == course_id,
            )
        ).scalar_one_or_none()
        if row is None:
            session.add(
                LearnCourseProgress(
                    user_id=user_id,
                    course_id=course_id,
                    watch_seconds=watch,
                    speed_time=speed_time,
                    total_lessons=total_lessons,
                    completed_lessons=completed_lessons,
                    total_duration_sec=total_duration,
                    percent=percent,
                    completed=completed,
                    resume_lesson_id=int(resume[0]) if resume is not None else None,
                    resume_position=int(resume[1]) if resume is not None else 0,
                    first_watch_at=agg[3] or moment,
                    last_watch_at=agg[4] or moment,
                    updated_at=moment,
                    version=1,
                )
            )
            return

        dialect = FLAVOR_SPECS[self.db.flavor].dialect
        session.execute(
            text(
                "UPDATE learn_course_progress SET "  # noqa: S608 - 列名/表名均为常量
                f"watch_seconds = {greatest_sql(dialect, 'watch_seconds', 'watch')}, "
                f"speed_time = {greatest_sql(dialect, 'speed_time', 'stime')}, "
                "total_lessons = :total_lessons, "
                "completed_lessons = :completed_lessons, "
                "total_duration_sec = :total_duration, percent = :pct, "
                "completed = :completed, resume_lesson_id = :resume_lesson, "
                "resume_position = :resume_pos, last_watch_at = :last_watch, "
                "updated_at = :moment, version = version + 1 "
                "WHERE user_id = :uid AND course_id = :cid"
            ).bindparams(
                watch=watch,
                stime=speed_time,
                total_lessons=total_lessons,
                completed_lessons=completed_lessons,
                total_duration=total_duration,
                pct=float(percent),
                completed=1 if completed else 0,
                resume_lesson=int(resume[0]) if resume is not None else None,
                resume_pos=int(resume[1]) if resume is not None else 0,
                last_watch=agg[4] or moment,
                moment=moment,
                uid=user_id,
                cid=course_id,
            )
        )

    @staticmethod
    def _duration(session: Session, lesson_id: int) -> int:
        value = session.execute(
            select(EduCourseLesson.duration_sec).where(EduCourseLesson.id == lesson_id)
        ).scalar()
        return int(value or 0)

    @staticmethod
    def _course_percent(completed_lessons: int, total_lessons: int) -> Decimal:
        if total_lessons <= 0:
            return Decimal("0.00")
        return (Decimal(completed_lessons) * 100 / Decimal(total_lessons)).quantize(Decimal("0.01"))

    @staticmethod
    def _percent(watch: int, duration: int) -> Decimal:
        if duration <= 0:
            return Decimal("0")
        return Decimal(str(min(100.0, watch * 100.0 / duration))).quantize(Decimal("0.01"))

    # ── 租约 ──────────────────────────────────────────────────────────────

    def _acquire_lease(self, now: datetime) -> bool:
        until = now + timedelta(seconds=self.config.get_int("progress.lease_ttl_sec"))
        with Session(self.db.engine) as session:
            result = cast(
                CursorResult[Any],
                session.execute(
                    update(LearnProgressGate)
                    .where(LearnProgressGate.shard == DEFAULT_SHARD)
                    .where(
                        (LearnProgressGate.leased_until <= now)
                        | (LearnProgressGate.writer_id == self.writer)
                    )
                    .values(writer_id=self.writer, leased_until=until, updated_at=now)
                ),
            )
            session.commit()
            return bool(result.rowcount)

    def _extend_lease(self, session: Session, now: datetime) -> None:
        until = now + timedelta(seconds=self.config.get_int("progress.lease_ttl_sec"))
        session.execute(
            update(LearnProgressGate)
            .where(LearnProgressGate.shard == DEFAULT_SHARD)
            .where(LearnProgressGate.writer_id == self.writer)
            .values(
                leased_until=until,
                updated_at=now,
                last_flush_at=now,
            )
        )


def replay_unflushed(flush: ProgressFlush, *, rounds: int = 1) -> int:
    """worker 启动期把崩溃前未落库的 log 行补齐。"""
    total = 0
    for _ in range(max(1, rounds)):
        count = flush.flush_once()
        total += count
        if count == 0:
            break
    return total


__all__ = [
    "ProgressFlush",
    "greatest_sql",
    "replay_unflushed",
    "writer_id",
]
