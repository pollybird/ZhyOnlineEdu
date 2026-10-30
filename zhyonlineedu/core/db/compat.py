"""方言兼容层（§5.4 契约 · F1 核心）。

红线（§5.2）：
- 禁止运行时逐请求探测方言；能力位由安装期 `sys_probe` 固化进 `sys_compat_flags`。
- 禁止 `try/except` 猜能力；一切分支以 `cap()` 返回值为准。

运行时状态由 `configure()` 在装配阶段写入（安装期探测 / 应用启动读 flags 后），
其余代码只读。测试可用 `reset()` 清空。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal

from sqlalchemy.engine import make_url


class Dialect(StrEnum):
    """粗粒度方言族（§5.1）。"""

    SQLITE = "sqlite"
    MYSQL = "mysql"
    PG_FAMILY = "pg_family"  # postgresql / opengauss / kingbase 共用


class Flavor(StrEnum):
    """细粒度方言（§5.1 版本矩阵）。"""

    SQLITE = "sqlite"
    MYSQL = "mysql"
    POSTGRESQL = "postgresql"
    OPENGAUSS_5 = "opengauss_5"
    OPENGAUSS_6 = "opengauss_6"
    OPENGAUSS_7 = "opengauss_7"
    KINGBASE = "kingbase"


class Cap(StrEnum):
    """能力位（与 sys_compat_flags.cap 一一对应，§26.3）。"""

    RETURNING = "returning"
    UPSERT_CONFLICT_CLAUSE = "upsert_conflict_clause"
    UPSERT_ON_DUPLICATE = "upsert_on_duplicate"
    SOFT_DELETE_COMPOSITE = "soft_delete_composite"
    NATIVE_JSON = "native_json"
    PARTIAL_INDEX = "partial_index"
    BULK_RETURNING_IDS = "bulk_returning_ids"
    ALTER_TYPE_ONLINE = "alter_type_online"


Route = Literal["pg", "mysql", "og5"]


class CompatError(RuntimeError):
    """兼容层未装配或能力位非法。"""


class UnknownCapability(CompatError):
    """sys_compat_flags 中出现未知能力位（禁止静默忽略）。"""


class SoftDeleteUKError(RuntimeError):
    """软删表缺少复合唯一键（D19a fail-fast）。"""


@dataclass(frozen=True, slots=True)
class FlavorSpec:
    flavor: Flavor
    dialect: Dialect
    route: Route
    caps: Mapping[Cap, bool]
    # 安装期必须 `SHOW` 复核的会话/库级参数（§5.5 / D35）
    required_settings: tuple[str, ...] = ()
    driver_hint: str = ""


def _caps(
    *,
    returning: bool,
    conflict: bool,
    on_duplicate: bool,
    native_json: bool,
    soft_delete: bool = True,
    partial_index: bool = True,
    bulk_returning_ids: bool = False,
    alter_type_online: bool = False,
) -> dict[Cap, bool]:
    return {
        Cap.RETURNING: returning,
        Cap.UPSERT_CONFLICT_CLAUSE: conflict,
        Cap.UPSERT_ON_DUPLICATE: on_duplicate,
        Cap.SOFT_DELETE_COMPOSITE: soft_delete,
        Cap.NATIVE_JSON: native_json,
        Cap.PARTIAL_INDEX: partial_index,
        Cap.BULK_RETURNING_IDS: bulk_returning_ids,
        Cap.ALTER_TYPE_ONLINE: alter_type_online,
    }


# 冻结表（§5.1）：文档预期值，安装期以真实探测覆盖。
FLAVOR_SPECS: Mapping[Flavor, FlavorSpec] = {
    Flavor.SQLITE: FlavorSpec(
        flavor=Flavor.SQLITE,
        dialect=Dialect.SQLITE,
        route="pg",
        caps=_caps(
            returning=True,
            conflict=True,
            on_duplicate=False,
            native_json=False,
            bulk_returning_ids=False,
        ),
    ),
    Flavor.MYSQL: FlavorSpec(
        flavor=Flavor.MYSQL,
        dialect=Dialect.MYSQL,
        route="mysql",
        caps=_caps(
            returning=False,
            conflict=False,
            on_duplicate=True,
            native_json=True,
        ),
    ),
    Flavor.POSTGRESQL: FlavorSpec(
        flavor=Flavor.POSTGRESQL,
        dialect=Dialect.PG_FAMILY,
        route="pg",
        caps=_caps(
            returning=True,
            conflict=True,
            on_duplicate=False,
            native_json=True,
            bulk_returning_ids=True,
            alter_type_online=True,
        ),
        driver_hint="PostgreSQL 需 psycopg(binary)；连接串示例 postgresql+psycopg://...",
    ),
    Flavor.OPENGAUSS_5: FlavorSpec(
        flavor=Flavor.OPENGAUSS_5,
        dialect=Dialect.PG_FAMILY,
        route="og5",
        caps=_caps(
            returning=False,
            conflict=False,
            on_duplicate=True,
            native_json=True,
        ),
        required_settings=("database_mode", "password_encryption_type"),
        driver_hint=(
            "openGauss 默认 SHA-256 认证（password_encryption_type=2）时原生 psycopg2 直连失败："
            "请安装 openGauss 定制驱动 psycopg2-for-opengauss，"
            "或按安全流程切 md5（需客户书面同意），禁止静默降级为 trust/明文"
        ),
    ),
    Flavor.OPENGAUSS_6: FlavorSpec(
        flavor=Flavor.OPENGAUSS_6,
        dialect=Dialect.PG_FAMILY,
        route="og5",
        caps=_caps(
            returning=False,
            conflict=False,
            on_duplicate=True,
            native_json=True,
        ),
        required_settings=("database_mode", "password_encryption_type"),
        driver_hint=(
            "openGauss 6.x 同 5.x：仅 MySQL 风 UPSERT、无 RETURNING；认证失败请装 "
            "psycopg2-for-opengauss 或按流程切 md5"
        ),
    ),
    Flavor.OPENGAUSS_7: FlavorSpec(
        flavor=Flavor.OPENGAUSS_7,
        dialect=Dialect.PG_FAMILY,
        route="pg",
        caps=_caps(
            returning=True,
            conflict=True,
            on_duplicate=True,
            native_json=True,
        ),
        required_settings=("database_mode",),
        driver_hint="openGauss 7.0 LTS 可走 psycopg；仍需 SHOW database_mode 复核",
    ),
    Flavor.KINGBASE: FlavorSpec(
        flavor=Flavor.KINGBASE,
        dialect=Dialect.PG_FAMILY,
        route="pg",
        caps=_caps(
            returning=True,
            conflict=True,
            on_duplicate=False,
            native_json=True,
            bulk_returning_ids=True,
            alter_type_online=True,
        ),
        required_settings=("database_mode", "ora_input_emptystr_isnull"),
        driver_hint=(
            "Kingbase 需 ksycopg2 + libkci.so（离线包 step 4）；缺库时加载失败，"
            "禁止回退到 psycopg"
        ),
    ),
}


@dataclass(frozen=True, slots=True)
class RuntimeCompat:
    """装配后的能力快照（不可变）。"""

    flavor: Flavor
    dialect: Dialect
    route: Route
    caps: Mapping[Cap, bool] = field(repr=False)
    source: str = "unknown"

    def describe(self) -> dict[str, object]:
        return {
            "flavor": self.flavor.value,
            "dialect": self.dialect.value,
            "route": self.route,
            "source": self.source,
            "capabilities": {c.value: self.caps[c] for c in Cap},
        }


_runtime: RuntimeCompat | None = None


def _env_flavor() -> Flavor | None:
    raw = os.environ.get("ZHY_DB_FLAVOR", "").strip()
    if not raw:
        return None
    try:
        return Flavor(raw)
    except ValueError as exc:
        allowed = ", ".join(f.value for f in Flavor)
        raise CompatError(f"ZHY_DB_FLAVOR 非法：{raw!r}（可选 {allowed}）") from exc


def _og_version() -> str | None:
    raw = os.environ.get("ZHY_DB_OG_VERSION", "").strip()
    return raw or None


def detect_flavor(url: str) -> Flavor:
    """从连接串推断 flavor；openGauss 版本无法从 URL 判别，读 ZHY_DB_OG_VERSION。"""
    try:
        parsed = make_url(url)
    except Exception as exc:  # noqa: BLE001 - SQLAlchemy 抛 ArgumentError 等多型异常
        raise CompatError(f"无法解析数据库连接串：{url!r}") from exc
    backend = parsed.get_backend_name()
    driver = parsed.drivername
    if backend == "sqlite":
        return Flavor.SQLITE
    if backend in {"mysql", "mariadb"}:
        return Flavor.MYSQL
    if backend.startswith("postgres"):
        if "kingbase" in driver or "kci" in driver or "ksycopg" in driver:
            return Flavor.KINGBASE
        og = _og_version()
        if og:
            major = og.split(".", 1)[0]
            mapping = {
                "5": Flavor.OPENGAUSS_5,
                "6": Flavor.OPENGAUSS_6,
                "7": Flavor.OPENGAUSS_7,
            }
            if major not in mapping:
                raise CompatError(f"ZHY_DB_OG_VERSION 非法：{og!r}（支持 5 / 6 / 7）")
            return mapping[major]
        return Flavor.POSTGRESQL
    raise CompatError(f"不支持的数据库方言：{backend}（URL={url!r}）")


def configure(
    *,
    url: str | None = None,
    flavor: str | Flavor | None = None,
    caps: Mapping[str, bool] | None = None,
    source: str = "runtime",
) -> RuntimeCompat:
    """装配兼容层快照。

    flavor 优先级：显式参数 → ZHY_DB_FLAVOR → 连接串推断（§5.4）。
    caps 为 sys_compat_flags 的覆盖值，未知能力位视为介质/探测异常直接报错。
    """
    resolved = (
        Flavor(flavor)
        if isinstance(flavor, str) and flavor
        else flavor if isinstance(flavor, Flavor) else _env_flavor()
    )
    if resolved is None:
        if not url:
            raise CompatError("configure() 需要 flavor 或 url 之一")
        resolved = detect_flavor(url)

    spec = FLAVOR_SPECS[resolved]
    merged: dict[Cap, bool] = dict(spec.caps)
    for key, value in (caps or {}).items():
        try:
            cap_key = Cap(key)
        except ValueError as exc:
            raise UnknownCapability(f"sys_compat_flags 含未知能力位：{key!r}") from exc
        merged[cap_key] = bool(value)

    global _runtime
    _runtime = RuntimeCompat(
        flavor=resolved,
        dialect=spec.dialect,
        route=spec.route,
        caps=merged,
        source=source,
    )
    return _runtime


def current() -> RuntimeCompat:
    if _runtime is None:
        raise CompatError("兼容层未装配：安装期必须先 probe() 或调用 configure()")
    return _runtime


def reset() -> None:
    """仅测试/重装使用。"""
    global _runtime
    _runtime = None


def flavor() -> Flavor:
    """细粒度方言。优先显式装配值，其次 ZHY_DB_FLAVOR（bootstrap 键）。"""
    if _runtime is not None:
        return _runtime.flavor
    env = _env_flavor()
    if env is not None:
        return env
    raise CompatError("方言未确定：请先安装期探测或设置 ZHY_DB_FLAVOR/ZHY_DB_URL")


def dialect() -> Dialect:
    return current().dialect


def route_for(target: Flavor | str) -> Route:
    return FLAVOR_SPECS[Flavor(target)].route


def cap(c: Cap | str) -> bool:
    """能力位查询。数据源：sys_compat_flags（安装期固化），运行时只读。"""
    key = Cap(c)
    return current().caps[key]


def flavor_spec(target: Flavor | str) -> FlavorSpec:
    return FLAVOR_SPECS[Flavor(target)]


# 厂商参数期望值（§5.5 / D35）；只对承诺档生效，安装期 `SHOW` 后逐项比对。
_EXPECTED_SETTINGS: Mapping[Flavor, Mapping[str, str]] = {
    Flavor.KINGBASE: {
        "database_mode": "pg",
        "ora_input_emptystr_isnull": "off",
    },
}


def expected_settings(target: Flavor | str) -> Mapping[str, str]:
    """返回该 flavor 安装期必须比对的参数-期望值（空表示只记录不断言）。"""
    return _EXPECTED_SETTINGS.get(Flavor(target), {})


def greatest_sql(column: str, bind: str, *, target_flavor: Flavor | str | None = None) -> str:
    """进度单调写入的方言抹平（F2 / D10a）。

    SQLite 无 GREATEST（M0 实测），降级为标量 max(a,b)；MySQL/PG 族用 GREATEST。
    业务 SQL 禁止裸写 GREATEST，一律经此生成。
    """
    target = Flavor(target_flavor) if target_flavor else flavor()
    if target is Flavor.SQLITE:
        return f"max({column}, :{bind})"
    return f"GREATEST({column}, :{bind})"


def empty_string_predicate(column: str) -> str:
    """D35：空串判空统一 COALESCE(col,'')=''，禁止 col = ''。"""
    return f"COALESCE({column}, '') = ''"


# 迁移必须走 batch（四步模板）的 flavor：SQLite DDL 能力弱；国产库 D34 要求
# "加新列→回填→改名→删旧列"，禁止自动生成直接 ALTER COLUMN TYPE。
_BATCH_FLAVORS: frozenset[Flavor] = frozenset(
    {
        Flavor.SQLITE,
        Flavor.OPENGAUSS_5,
        Flavor.OPENGAUSS_6,
        Flavor.OPENGAUSS_7,
        Flavor.KINGBASE,
    }
)


def flavor_needs_batch(target: Flavor | str) -> bool:
    """Alembic 是否按 batch/四步模板渲染（§5.6 / D34）。

    PG 与 MySQL 可在线变更类型，返回 False；其余 flavor 必须走四步模板。
    """
    return Flavor(target) in _BATCH_FLAVORS


def driver_hint(target: Flavor | str, error: str = "") -> str:
    """连接失败的可读诊断（§5.5）：附厂商驱动/参数处置建议。"""
    spec = flavor_spec(target)
    hint = spec.driver_hint or "请检查驱动与连接串"
    return f"{hint}；原始错误：{error}" if error else hint


def ensure_soft_delete_uk(metadata: object) -> None:
    """启动断言：含 delete_seq 的表必须有包含该列的复合唯一键（D19a）。

    缺则 fail-fast，禁止部署到生产后靠数据事故发现。
    """
    from sqlalchemy import Table, UniqueConstraint

    tables = getattr(metadata, "tables", None)
    if tables is None:
        raise TypeError("ensure_soft_delete_uk 需要 MetaData")
    bad: list[str] = []
    for table in tables.values():
        if not isinstance(table, Table) or "delete_seq" not in table.c:
            continue
        has_uk = any(
            isinstance(constraint, UniqueConstraint)
            and "delete_seq" in {col.name for col in constraint.columns}
            for constraint in table.constraints
        )
        if not has_uk:
            bad.append(table.name)
    if bad:
        raise SoftDeleteUKError(
            "以下表含 delete_seq 但缺少 (uq_cols, delete_seq) 复合唯一键：" + ", ".join(sorted(bad))
        )
