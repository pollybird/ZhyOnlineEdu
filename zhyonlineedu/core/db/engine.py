"""引擎与连接池装配（§5.5 连接与池参数矩阵）。

职责：
- 按 flavor 装配 pool / connect_args / 建连事件（SQLite PRAGMA、MySQL sql_mode、
  PG 族 timezone；openGauss/Kingbase 安装期再 `SHOW` 复核，见 probe）；
- 提供进程级会话工厂与 `session_scope()`，供 upsert 等适配层取当前会话。

禁止在本模块做能力猜测：能力位一律来自 compat（安装期固化，§5.2 红线）。
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool, QueuePool, StaticPool

from zhyonlineedu.core.config import Config

from .compat import Flavor, detect_flavor

_MEMORY_URLS = {"sqlite://", "sqlite:///:memory:"}

_current_session: ContextVar[Session | None] = ContextVar("zhy_current_session", default=None)
_database: Database | None = None


def _is_sqlite(flavor: Flavor) -> bool:
    return flavor is Flavor.SQLITE


def _sqlite_connect_args() -> dict[str, Any]:
    return {"check_same_thread": False, "timeout": 15}


def is_lock_error(exc: BaseException) -> bool:
    """是否 SQLite 写锁竞争（`database is locked` / `database is busy`）。

    M2.16 压测实测：先读后写的事务升级为写事务时，若期间别的连接提交过数据，
    SQLite 直接返回 `SQLITE_BUSY_SNAPSHOT`，`busy_timeout` 对它**不生效**，
    SQLAlchemy 统一抛 `OperationalError`。调用方据此退避重试或拆短事务。
    """
    orig = getattr(exc, "orig", None)
    detail = str(orig if orig is not None else exc).lower()
    return "locked" in detail or "busy" in detail


def _install_connect_events(engine: Engine, flavor: Flavor) -> None:
    if _is_sqlite(flavor):
        memory = str(engine.url) in _MEMORY_URLS

        @event.listens_for(engine, "connect")
        def _sqlite_on_connect(dbapi_connection: Any, _record: Any) -> None:
            cursor = dbapi_connection.cursor()
            try:
                if not memory:
                    cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA busy_timeout=5000")
                cursor.execute("PRAGMA synchronous=NORMAL")
            finally:
                cursor.close()

        return

    if flavor is Flavor.MYSQL:

        @event.listens_for(engine, "connect")
        def _mysql_on_connect(dbapi_connection: Any, _record: Any) -> None:
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute(
                    "SET SESSION sql_mode='STRICT_TRANS_TABLES,NO_ZERO_DATE,NO_ZERO_IN_DATE'"
                )
            finally:
                cursor.close()

        return

    @event.listens_for(engine, "connect")
    def _pg_on_connect(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("SET TIME ZONE 'UTC'")
        finally:
            cursor.close()


def create_db_engine(url: str, flavor: Flavor | str | None = None) -> Engine:
    """按 §5.5 建引擎；SQLite 文件库用 NullPool，内存库用 StaticPool（测试）。"""
    target = Flavor(flavor) if flavor is not None else detect_flavor(url)
    kwargs: dict[str, Any] = {"future": True}
    if target is Flavor.SQLITE:
        kwargs["poolclass"] = StaticPool if url in _MEMORY_URLS else NullPool
        kwargs["connect_args"] = _sqlite_connect_args()
    else:
        kwargs["poolclass"] = QueuePool
        kwargs["pool_size"] = 10
        kwargs["max_overflow"] = 10
        kwargs["pool_pre_ping"] = True
        kwargs["pool_recycle"] = 1800
        connect_args: dict[str, Any] = {}
        if target is Flavor.MYSQL:
            connect_args["charset"] = "utf8mb4"
        else:
            connect_args["client_encoding"] = "UTF8"
            connect_args["application_name"] = "zhy-api"
        kwargs["connect_args"] = connect_args

    engine = create_engine(url, **kwargs)
    _install_connect_events(engine, target)
    return engine


@dataclass(frozen=True, slots=True)
class Database:
    engine: Engine
    flavor: Flavor
    session_factory: sessionmaker[Session]

    @contextmanager
    def session_scope(self) -> Generator[Session, None, None]:
        """会话作用域：提交/回滚由调用方事务边界决定，异常回滚。"""
        session = self.session_factory()
        token = _current_session.set(session)
        try:
            yield session
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
            _current_session.reset(token)


def init_database(config: Config) -> Database:
    """装配进程级 Database（不自动探测能力位）。"""
    global _database
    url = config.get_str("db.url")
    explicit = config.get_str("db.flavor") or None
    flavor = Flavor(explicit) if explicit else detect_flavor(url)
    if _database is not None and _database.engine.url != make_url(url):
        # 换库（测试/重装）时释放旧池，避免跨用例串库
        _database.engine.dispose()
    engine = create_db_engine(url, flavor)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    _database = Database(engine=engine, flavor=flavor, session_factory=factory)
    return _database


def ensure_database(config: Config) -> Database:
    """已装配且 DSN 未变则复用，否则重新装配（应用工厂 / 认证服务共用）。"""
    if _database is not None and _database.engine.url == make_url(config.get_str("db.url")):
        return _database
    return init_database(config)


def database() -> Database:
    if _database is None:
        raise RuntimeError("Database 未装配：请先 init_database()")
    return _database


def dispose_database() -> None:
    """测试/重装释放连接池。"""
    global _database
    if _database is not None:
        _database.engine.dispose()
    _database = None


def current_session() -> Session:
    """取当前请求/任务会话；未在 session_scope 内则报错（禁止隐式建会话）。"""
    session = _current_session.get()
    if session is None:
        raise RuntimeError("当前无会话：请在 engine.session_scope() 内调用，或显式传入 session=")
    return session
