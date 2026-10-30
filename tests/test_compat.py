"""M1.2 兼容层契约：flavor 探测、能力位冻结、方言 SQL 抹平、软删断言。"""

from __future__ import annotations

import pytest
from sqlalchemy import BigInteger, Column, Integer, MetaData, Table, UniqueConstraint
from zhyonlineedu.core.db import compat
from zhyonlineedu.core.db.compat import Cap, CompatError, Dialect, Flavor, SoftDeleteUKError


@pytest.fixture(autouse=True)
def _reset() -> None:
    compat.reset()
    yield
    compat.reset()


def test_detect_flavor_from_url(monkeypatch: pytest.MonkeyPatch) -> None:
    assert compat.detect_flavor("sqlite:///./zhy.db") is Flavor.SQLITE
    assert compat.detect_flavor("mysql+pymysql://u:p@db/zhy") is Flavor.MYSQL
    assert compat.detect_flavor("postgresql+psycopg://u:p@db/zhy") is Flavor.POSTGRESQL
    assert compat.detect_flavor("postgresql+ksycopg2://u:p@db/zhy") is Flavor.KINGBASE
    monkeypatch.setenv("ZHY_DB_OG_VERSION", "6.0")
    assert compat.detect_flavor("postgresql+psycopg://u:p@db/zhy") is Flavor.OPENGAUSS_6


def test_detect_flavor_rejects_unknown() -> None:
    with pytest.raises(CompatError, match="不支持的数据库方言"):
        compat.detect_flavor("oracle+cx_oracle://u:p@db/zhy")


def test_flavor_env_priority(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZHY_DB_FLAVOR", "mysql")
    assert compat.flavor() is Flavor.MYSQL
    compat.configure(flavor="sqlite")
    assert compat.flavor() is Flavor.SQLITE  # 显式装配优先于 env


def test_unconfigured_raises() -> None:
    with pytest.raises(CompatError, match="兼容层未装配"):
        compat.dialect()
    with pytest.raises(CompatError, match="方言未确定"):
        compat.flavor()
    with pytest.raises(CompatError, match="兼容层未装配"):
        compat.cap(Cap.RETURNING)


@pytest.mark.parametrize(
    ("flavor", "expected_dialect", "expected_route"),
    [
        ("sqlite", Dialect.SQLITE, "pg"),
        ("mysql", Dialect.MYSQL, "mysql"),
        ("postgresql", Dialect.PG_FAMILY, "pg"),
        ("opengauss_5", Dialect.PG_FAMILY, "og5"),
        ("opengauss_6", Dialect.PG_FAMILY, "og5"),
        ("opengauss_7", Dialect.PG_FAMILY, "pg"),
        ("kingbase", Dialect.PG_FAMILY, "pg"),
    ],
)
def test_flavor_registry(flavor: str, expected_dialect: Dialect, expected_route: str) -> None:
    snapshot = compat.configure(flavor=flavor)
    assert snapshot.dialect is expected_dialect
    assert snapshot.route == expected_route
    assert compat.dialect() is expected_dialect
    assert compat.route_for(flavor) == expected_route


def test_caps_follow_version_matrix() -> None:
    compat.configure(flavor="sqlite")
    assert compat.cap(Cap.RETURNING) is True
    assert compat.cap(Cap.UPSERT_ON_DUPLICATE) is False

    compat.configure(flavor="mysql")
    assert compat.cap(Cap.RETURNING) is False
    assert compat.cap(Cap.UPSERT_ON_DUPLICATE) is True

    compat.configure(flavor="opengauss_5")
    assert compat.cap(Cap.RETURNING) is False
    assert compat.cap(Cap.UPSERT_CONFLICT_CLAUSE) is False
    assert compat.cap(Cap.UPSERT_ON_DUPLICATE) is True

    compat.configure(flavor="opengauss_7")
    assert compat.cap(Cap.RETURNING) is True
    assert compat.cap(Cap.UPSERT_CONFLICT_CLAUSE) is True


def test_flags_override_and_unknown_cap() -> None:
    snapshot = compat.configure(flavor="sqlite", caps={"returning": False, "partial_index": True})
    assert snapshot.caps[Cap.RETURNING] is False
    assert compat.cap("partial_index") is True
    with pytest.raises(compat.UnknownCapability, match="未知能力位"):
        compat.configure(flavor="sqlite", caps={"no_such_cap": True})


def test_configure_from_url() -> None:
    snapshot = compat.configure(url="sqlite:///./zhy.db", source="test")
    assert snapshot.flavor is Flavor.SQLITE and snapshot.source == "test"


def test_greatest_sql_dialect_fallback() -> None:
    assert compat.greatest_sql("pos", "p", target_flavor="sqlite") == "max(pos, :p)"
    assert compat.greatest_sql("pos", "p", target_flavor="postgresql") == "GREATEST(pos, :p)"
    assert compat.greatest_sql("pos", "p", target_flavor="mysql") == "GREATEST(pos, :p)"


def test_empty_string_predicate() -> None:
    assert compat.empty_string_predicate("name") == "COALESCE(name, '') = ''"


def test_driver_hints() -> None:
    og = compat.driver_hint("opengauss_5", "auth failed")
    assert "psycopg2-for-opengauss" in og and "md5" in og and "禁止静默降级" in og
    kb = compat.driver_hint("kingbase")
    assert "ksycopg2" in kb and "libkci.so" in kb


def test_expected_settings() -> None:
    assert compat.expected_settings("kingbase") == {
        "database_mode": "pg",
        "ora_input_emptystr_isnull": "off",
    }
    assert compat.expected_settings("sqlite") == {}


def test_ensure_soft_delete_uk_pass_and_fail() -> None:
    good = MetaData()
    Table(
        "t_good",
        good,
        Column("id", Integer, primary_key=True),
        Column("uid", Integer),
        Column("delete_seq", BigInteger, nullable=False, default=0),
        UniqueConstraint("uid", "delete_seq"),
    )
    compat.ensure_soft_delete_uk(good)

    bad = MetaData()
    Table(
        "t_bad",
        bad,
        Column("id", Integer, primary_key=True),
        Column("delete_seq", BigInteger, nullable=False, default=0),
    )
    with pytest.raises(SoftDeleteUKError, match="t_bad"):
        compat.ensure_soft_delete_uk(bad)
