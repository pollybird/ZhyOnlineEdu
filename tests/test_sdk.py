"""M3.4 插件 SDK 收窄（§11.9）。

契约口径（SDK-001~008）：
001 白名单：只暴露 store/progress/media/access/compat；无 `sdk.db` / session / record_raw；
002 作用域：脱离插件分发调用一律 `PLUGIN_CAP_DENIED`；
003 能力门禁：未在 manifest 声明的能力调用即 403（`capability_missing`）；
004 store：只能写 `plg_<code>_` 自有表，内核表 / 他人插件表一律拒绝；
005 store：走内核 UPSERT 契约（同自然键不重复插入、version 自增）；
006 media：插件票强制 `preview=true`、档位 ≤360、TTL ≤300s（只降不升）；
007 access：只读判定复用 `decide_access`（不签票、不写库）；
008 progress：等同 `ProgressGateway.record`，写入 `learn_progress_log` 而不动投影。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import BigInteger, Integer, String, UniqueConstraint, func, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column
from zhyonlineedu.app import create_app
from zhyonlineedu.core import sdk
from zhyonlineedu.core.auth.keys import clear_key_cache, ensure_keypair
from zhyonlineedu.core.auth.passwords import hash_password
from zhyonlineedu.core.capability import ProbeResult, ProbeState, StaticProbe
from zhyonlineedu.core.codes import (
    PLUGIN_CAP_DENIED,
    PLUGIN_MIGRATION_INVALID,
    PLUGIN_TABLE_PREFIX_INVALID,
    VALIDATION_FAILED,
)
from zhyonlineedu.core.config import Config, load_config
from zhyonlineedu.core.db import compat
from zhyonlineedu.core.db.engine import create_db_engine
from zhyonlineedu.core.db.migration import upgrade_head
from zhyonlineedu.core.db.models.account import AccountUser, SysUserRole
from zhyonlineedu.core.db.models.catalog import (
    EduCourse,
    EduCourseChapter,
    EduCourseLesson,
    EduSubject,
)
from zhyonlineedu.core.db.models.learn import LearnProgressLog, utcnow
from zhyonlineedu.core.errors import AppError
from zhyonlineedu.core.sdk.context import reset, using
from zhyonlineedu.modules.learn.playback import PlaybackService

PASSWORD = "S3cret-pass"  # noqa: S105 - 测试用固定口令
READY = ProbeResult(ProbeState.READY, flavor="sqlite", capabilities={"returning": True})
LESSON_ID = 101
STUDENT_ID = 2


class _PluginBase(DeclarativeBase):
    """测试插件自己的声明式基类（与内核 `Base` 隔离，不污染迁移表集合）。"""


class PluginNote(_PluginBase):
    __tablename__ = "plg_demo_note"
    __table_args__ = (UniqueConstraint("body", "delete_seq"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    body: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    delete_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)


@pytest.fixture(autouse=True)
def _cleanup() -> Iterator[None]:
    reset()
    compat.reset()
    clear_key_cache()
    yield
    reset()
    compat.reset()
    clear_key_cache()


def _config(tmp_path: Path) -> Config:
    return load_config(
        environ={
            "ZHY_ENV": "dev",
            "ZHY_DB_URL": f"sqlite:///{tmp_path / 'sdk.db'}",
            "ZHY_DB_FLAVOR": "sqlite",
            "ZHY_JWT_PRIVATE_KEY_PATH": str(tmp_path / "keys" / "jwt_private.pem"),
            "ZHY_JWT_PUBLIC_KEY_PATH": str(tmp_path / "keys" / "jwt_public.pem"),
            "ZHY_STORAGE_LOCAL_DIR": str(tmp_path / "storage"),
            "ZHY_PROGRESS_RING_DIR": str(tmp_path / "ring"),
            "ZHY_PLUGIN_DIR": str(tmp_path / "plugins"),
        },
        sys_config={},
    )


def _seed(tmp_path: Path) -> Config:
    config = _config(tmp_path)
    ensure_keypair(config.get_str("jwt.private_key_path"), config.get_str("jwt.public_key_path"))
    url = config.get_str("db.url")
    upgrade_head(url, "sqlite")
    compat.configure(flavor="sqlite")
    engine = create_db_engine(url, "sqlite")
    try:
        with Session(engine) as session:
            now = utcnow()
            for username, role in (("root", "super_admin"), ("student", "student")):
                user = AccountUser(
                    username=username,
                    password_hash=hash_password(PASSWORD),
                    status="active",
                    token_version=1,
                    created_at=now,
                    updated_at=now,
                )
                session.add(user)
                session.flush()
                session.add(SysUserRole(user_id=user.id, role_code=role))
            subject = EduSubject(name="数学", sort_order=1024, is_enabled=True)
            session.add(subject)
            session.flush()
            course = EduCourse(
                subject_id=subject.id,
                title="SDK 公开课",
                mode="both",
                access_type="public",
                status="published",
                sort_order=1024,
            )
            session.add(course)
            session.flush()
            chapter = EduCourseChapter(course_id=course.id, title="第一章", sort_order=1024)
            session.add(chapter)
            session.flush()
            session.add(
                EduCourseLesson(
                    id=LESSON_ID,
                    chapter_id=chapter.id,
                    course_id=course.id,
                    title="第一节",
                    kind="video",
                    duration_sec=300,
                    is_free=False,
                    sort_order=1024,
                )
            )
            session.commit()
    finally:
        engine.dispose()
    return config


def _app_context(config: Config):  # type: ignore[no-untyped-def]
    app = create_app(config=config, probe=StaticProbe(READY))
    return app.app_context()


def _count_logs(config: Config) -> int:
    engine = create_db_engine(config.get_str("db.url"), "sqlite")
    try:
        with Session(engine) as session:
            return int(session.execute(select(func.count(LearnProgressLog.id))).scalar_one())
    finally:
        engine.dispose()


# ── 001 / 002 / 003：白名单与作用域 ────────────────────────────────────────


def test_sdk_surface_is_narrow() -> None:
    assert set(sdk.__all__) == {"access", "compat", "events", "media", "progress", "store"}
    for forbidden in ("db", "session", "sql", "ddl"):
        assert not hasattr(sdk, forbidden), forbidden
    assert not hasattr(sdk.progress, "record_raw")
    assert not hasattr(sdk.progress, "direct_write")
    assert not hasattr(sdk.store, "session")
    assert not hasattr(sdk.store, "raw_sql")
    assert not hasattr(sdk.compat, "configure")
    assert not hasattr(sdk.compat, "reset")
    assert not hasattr(sdk.events, "emit")
    assert set(sdk.progress.__all__) == {"record"}
    assert set(sdk.media.__all__) == {"MAX_PROFILE", "MAX_TTL_OVERRIDE", "issue_ticket"}
    assert set(sdk.events.__all__) == {"available", "subscribe"}
    assert set(sdk.store.__all__) == {"MAX_FETCH_ROWS", "fetch", "upsert"}


def test_sdk_requires_plugin_scope() -> None:
    with pytest.raises(AppError) as exc:
        sdk.compat.flavor()
    assert exc.value.code == PLUGIN_CAP_DENIED
    assert exc.value.data["reason"] == "sdk_scope_missing"

    with pytest.raises(AppError) as exc2:
        sdk.store.upsert(PluginNote, {"body": "x"}, ["body"])
    assert exc2.value.code == PLUGIN_CAP_DENIED


def test_sdk_capability_gate() -> None:
    with using("demo", ["progress:read"]):
        with pytest.raises(AppError) as exc:
            sdk.access.evaluate(STUDENT_ID, LESSON_ID)
        assert exc.value.code == PLUGIN_CAP_DENIED
        assert exc.value.data["required"] == "access:evaluate"
        with pytest.raises(AppError) as exc2:
            sdk.progress.record(
                user_id=STUDENT_ID,
                lesson_id=LESSON_ID,
                body={"position_sec": 30, "played_seconds": 30},
                can_preview=False,
            )
        assert exc2.value.code == PLUGIN_CAP_DENIED
        with pytest.raises(AppError) as exc3:
            sdk.media.issue_ticket(LESSON_ID, user_id=STUDENT_ID)
        assert exc3.value.code == PLUGIN_CAP_DENIED


# ── 004 / 005：store 只能写自有表且走 UPSERT 契约 ──────────────────────────


def test_store_rejects_kernel_and_foreign_tables(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    with _app_context(config), using("demo", []):
        with pytest.raises(AppError) as exc:
            sdk.store.upsert(EduCourse, {"title": "x", "subject_id": 1}, ["title"])
        assert exc.value.code == PLUGIN_TABLE_PREFIX_INVALID
        assert exc.value.data["reason"] == "foreign_table"

        with pytest.raises(AppError) as exc2:
            sdk.store.upsert(OtherPluginNote, {"body": "x", "delete_seq": 0}, ["body"])
        assert exc2.value.code == PLUGIN_TABLE_PREFIX_INVALID

        with pytest.raises(AppError) as exc3:
            sdk.store.upsert(NoSoftDelete, {"body": "x"}, ["body"])
        assert exc3.value.code == PLUGIN_MIGRATION_INVALID
        assert exc3.value.data["reason"] == "missing_delete_seq"


def test_store_upsert_is_idempotent(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    url = config.get_str("db.url")
    engine = create_db_engine(url, "sqlite")
    try:
        _PluginBase.metadata.create_all(engine)
        with _app_context(config), using("demo", []):
            first = sdk.store.upsert(
                PluginNote, {"body": "hello", "delete_seq": 0}, ["body", "delete_seq"]
            )
            second = sdk.store.upsert(
                PluginNote, {"body": "hello", "delete_seq": 0}, ["body", "delete_seq"]
            )
        with Session(engine) as session:
            rows = session.execute(select(PluginNote)).scalars().all()
        assert first.created is True
        assert second.created is False
        assert second.version >= 2
        assert len(rows) == 1
        assert rows[0].body == "hello"
    finally:
        engine.dispose()


def test_store_fetch_is_scoped_and_soft_delete_aware(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    url = config.get_str("db.url")
    engine = create_db_engine(url, "sqlite")
    try:
        _PluginBase.metadata.create_all(engine)
        with _app_context(config), using("demo", []):
            sdk.store.upsert(PluginNote, {"body": "hello", "delete_seq": 0}, ["body", "delete_seq"])
            # 软删行（delete_seq != 0）对插件读取不可见
            sdk.store.upsert(PluginNote, {"body": "ghost", "delete_seq": 1}, ["body", "delete_seq"])
            rows = sdk.store.fetch(PluginNote, limit=10)
            filtered = sdk.store.fetch(PluginNote, where={"body": "hello"})
            asc = sdk.store.fetch(PluginNote, order_by="body", descending=False)

            with pytest.raises(AppError) as foreign:
                sdk.store.fetch(EduCourse)
            assert foreign.value.code == PLUGIN_TABLE_PREFIX_INVALID

            with pytest.raises(AppError) as no_soft:
                sdk.store.fetch(NoSoftDelete)
            assert no_soft.value.code == PLUGIN_MIGRATION_INVALID

            with pytest.raises(AppError) as bad_column:
                sdk.store.fetch(PluginNote, where={"unknown": 1})
            assert bad_column.value.code == VALIDATION_FAILED

            with pytest.raises(AppError) as bad_order:
                sdk.store.fetch(PluginNote, order_by="unknown")
            assert bad_order.value.code == VALIDATION_FAILED

            with pytest.raises(AppError) as bad_limit:
                sdk.store.fetch(PluginNote, limit=201)
            assert bad_limit.value.code == VALIDATION_FAILED

        assert [row["body"] for row in rows] == ["hello"]
        assert rows[0]["delete_seq"] == 0
        assert [row["body"] for row in filtered] == ["hello"]
        assert [row["body"] for row in asc] == ["hello"]
    finally:
        engine.dispose()


# ── 006：media 只降不升 ────────────────────────────────────────────────────


def test_media_ticket_is_downgraded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _seed(tmp_path)
    captured: dict[str, Any] = {}

    def fake_issue(
        self: PlaybackService,
        request: Any,
        *,
        now: Any = None,
        max_profile: str | None = None,
        ttl_override: int | None = None,
    ) -> dict[str, Any]:
        captured.update(
            max_profile=max_profile,
            ttl_override=ttl_override,
            preview=request.can_preview,
            lesson_id=request.lesson_id,
            user_id=request.user_id,
        )
        return {"preview": request.can_preview, "profiles": [max_profile], "ttl": ttl_override}

    monkeypatch.setattr(PlaybackService, "issue", fake_issue)
    with _app_context(config), using("demo", ["media:ticket:issue"]):
        payload = sdk.media.issue_ticket(LESSON_ID, user_id=STUDENT_ID, ttl_override=9_999)

    assert payload["preview"] is True
    assert payload["profiles"] == ["360"]
    assert payload["ttl"] == 300
    assert captured["preview"] is True
    assert captured["max_profile"] == "360"
    assert captured["ttl_override"] == 300
    assert captured["lesson_id"] == LESSON_ID


# ── 007 / 008：access 只读判定 + progress 走进度网关 ───────────────────────


def test_access_evaluate_is_readonly(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    with _app_context(config), using("demo", ["access:evaluate"]):
        allowed = sdk.access.evaluate(STUDENT_ID, LESSON_ID)
        with pytest.raises(AppError):
            sdk.access.evaluate(STUDENT_ID, 999)
    assert allowed["allowed"] is True
    assert allowed["rule"] == "MODE-001"
    assert allowed["lesson_id"] == LESSON_ID


def test_progress_record_goes_through_gateway(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    with _app_context(config), using("demo", ["progress:report"]):
        result = sdk.progress.record(
            user_id=STUDENT_ID,
            lesson_id=LESSON_ID,
            body={"position_sec": 30, "played_seconds": 30, "speed": 1.0, "client_ts_ms": 0},
            can_preview=False,
        )
    assert result["gw_seq"] >= 1
    assert result["deduped"] is False
    assert _count_logs(config) == 1


# ── 辅助模型（他人插件表 / 缺 delete_seq）────────────────────────────────


class OtherPluginNote(_PluginBase):
    __tablename__ = "plg_other_note"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    body: Mapped[str] = mapped_column(String(64), nullable=False)
    delete_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)


class NoSoftDelete(_PluginBase):
    __tablename__ = "plg_demo_nosoft"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    body: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
