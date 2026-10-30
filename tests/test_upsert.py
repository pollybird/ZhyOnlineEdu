"""T-DB-UPSERT-001~007：upsert 七项专项（§16.2 F1 专项）。

SQLite 实测；PG/MySQL 分支以编译产物断言（运行期由 test_dialect_matrix 按 DSN 门控）。
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import BigInteger, Integer, UniqueConstraint, create_engine, select, text
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column
from zhyonlineedu.core.db import compat
from zhyonlineedu.core.db.upsert import (
    _MAX_CONFLICT_RETRIES,
    compile_upsert,
    rehydrate_by_natkey,
    upsert,
    upsert_many,
)


class Base(DeclarativeBase):
    pass


class Progress(Base):
    __tablename__ = "t_db_progress"
    __table_args__ = (UniqueConstraint("uid", "lesson"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    uid: Mapped[int] = mapped_column(BigInteger, nullable=False)
    lesson: Mapped[int] = mapped_column(BigInteger, nullable=False)
    pos: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


@pytest.fixture(autouse=True)
def _reset_compat() -> Iterator[None]:
    compat.reset()
    yield
    compat.reset()


@pytest.fixture()
def session() -> Iterator[Session]:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as sess:
        yield sess
        sess.rollback()
    engine.dispose()


def _row(uid: int = 1, lesson: int = 1, pos: int = 100) -> dict[str, int]:
    return {"uid": uid, "lesson": lesson, "pos": pos}


def test_t_db_upsert_001_insert_returns_id(session: Session) -> None:
    result = upsert(Progress, _row(), ["uid", "lesson"], target_flavor="sqlite", session=session)
    assert result.created is True
    assert result.pk is not None
    assert result.version == 1


def test_t_db_upsert_002_conflict_keeps_natkey(session: Session) -> None:
    first = upsert(
        Progress, _row(pos=10), ["uid", "lesson"], target_flavor="sqlite", session=session
    )
    second = upsert(
        Progress, _row(pos=20), ["uid", "lesson"], target_flavor="sqlite", session=session
    )
    assert first.pk == second.pk
    assert second.created is False and second.version == 2
    rows = session.execute(select(Progress)).scalars().all()
    assert len(rows) == 1
    assert rows[0].pos == 20 and rows[0].uid == 1 and rows[0].lesson == 1

    compiled = compile_upsert("sqlite", "t_db_progress", _row(), ["uid", "lesson"])
    _, _, update_part = compiled.sql.partition("DO UPDATE")
    assert "uid = " not in update_part
    assert "lesson = " not in update_part
    assert "id = " not in update_part


def test_t_db_upsert_003_rehydrate_after_conflict(session: Session) -> None:
    first = upsert(Progress, _row(), ["uid", "lesson"], target_flavor="sqlite", session=session)
    second = upsert(
        Progress, _row(pos=300), ["uid", "lesson"], target_flavor="sqlite", session=session
    )
    rehydrated = rehydrate_by_natkey(Progress, {"uid": 1, "lesson": 1}, session=session)
    assert rehydrated == first.pk == second.pk


def test_t_db_upsert_004_batch_mixed(session: Session) -> None:
    compat.configure(flavor="sqlite")
    report = upsert_many(
        Progress,
        [_row(pos=1), _row(pos=2), _row(uid=2, pos=3), {"uid": 3, "pos": 4}],
        ["uid", "lesson"],
        session=session,
    )
    assert len(report.results) == 3
    assert len(report.failed_rows) == 1
    assert report.results[0].created is True
    assert report.results[1].created is False
    assert session.execute(select(Progress)).all()


def test_t_db_upsert_005_fallback_retries(session: Session) -> None:
    # og5 的 MySQL 风 SQL 在 SQLite 上不可执行 → 必然走兜底 select→update/insert
    first = upsert(
        Progress, _row(pos=5), ["uid", "lesson"], target_flavor="opengauss_5", session=session
    )
    second = upsert(
        Progress, _row(pos=9), ["uid", "lesson"], target_flavor="opengauss_5", session=session
    )
    assert first.created is True and first.pk is not None
    assert second.created is False and second.version == 2
    assert _MAX_CONFLICT_RETRIES == 2


def test_t_db_upsert_006_pg_returning() -> None:
    for flavor in ("sqlite", "postgresql", "kingbase", "opengauss_7"):
        compiled = compile_upsert(
            flavor, "t_db_progress", {**_row(), "version": 1}, ["uid", "lesson"]
        )
        assert compiled.returning is True
        assert "RETURNING id" in compiled.sql
        assert "t_db_progress.version + 1" in compiled.sql


def test_t_db_upsert_007_og5_no_returning(session: Session) -> None:
    compiled = compile_upsert("opengauss_5", "t_db_progress", _row(), ["uid", "lesson"])
    assert compiled.returning is False
    assert "RETURNING" not in compiled.sql
    assert compiled.readback_sql is not None and "WHERE" in compiled.readback_sql
    with pytest.raises(ValueError, match="禁止更新自然键"):
        compile_upsert(
            "opengauss_5",
            "t_db_progress",
            _row(),
            ["uid", "lesson"],
            update_cols=["uid", "pos"],
        )
    result = upsert(
        Progress, _row(pos=7), ["uid", "lesson"], target_flavor="opengauss_5", session=session
    )
    assert result.pk == rehydrate_by_natkey(Progress, {"uid": 1, "lesson": 1}, session=session)
    assert session.execute(text("SELECT version FROM t_db_progress")).scalar() == 1
