# x-zhy-cap: returning:optional, alter_type_online:false
"""member levels / subscriptions / orders / pay events (M2.12)

Revision ID: 0011_commerce
Revises: 0010_modes
Create Date: 2026-10-30 08:00:00.000000

M2.12 会员订单（§10.6）：
- 新增 `member_level` / `member_subscription` / `pay_order` / `pay_event`；
- 订单冻结档位 / 时长 / 金额快照，订阅以 `order_no` 唯一去重；
- `pay_event.event_id` 唯一：重复回调幂等，状态只前进不后退（PAY-STATE-001~004）；
- 新增权限码 `member:manage`（super_admin / admin），用于档位维护、直接开通与查单补偿。

本迁移纯新增表/索引/种子，无类型变更。
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_commerce"
down_revision: str | None = "0010_modes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MEMBER_MANAGE = "member:manage"
_MANAGE_ROLES: tuple[str, ...] = ("super_admin", "admin")


def _sync_schema_rev(rev: str) -> None:
    op.execute(
        sa.text(
            "UPDATE sys_install_state SET schema_rev = :rev, last_good_rev = :rev WHERE id = 1"
        ).bindparams(rev=rev)
    )


def _seed_member_manage() -> None:
    permission = sa.table(
        "sys_permission",
        sa.column("code", sa.String),
        sa.column("name", sa.String),
        sa.column("risk_level", sa.String),
    )
    role_permission = sa.table(
        "sys_role_permission",
        sa.column("role_code", sa.String),
        sa.column("permission_code", sa.String),
    )
    op.bulk_insert(permission, [{"code": MEMBER_MANAGE, "name": "会员管理", "risk_level": "mid"}])
    op.bulk_insert(
        role_permission,
        [{"role_code": role, "permission_code": MEMBER_MANAGE} for role in _MANAGE_ROLES],
    )


def upgrade() -> None:
    op.create_table(
        "member_level",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("level", sa.Integer(), server_default="1", nullable=False),
        sa.Column("price", sa.Numeric(precision=18, scale=6), server_default="0", nullable=False),
        sa.Column("duration_days", sa.Integer(), server_default="30", nullable=False),
        sa.Column("is_enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default="1024", nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.Column("delete_seq", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_member_level")),
        sa.UniqueConstraint("code", "delete_seq", name="uq_member_level_no_deleted"),
    )
    op.create_index("ix_member_level_delete_seq", "member_level", ["delete_seq"], unique=False)
    op.create_index(
        "ix_member_level_enabled_sort",
        "member_level",
        ["is_enabled", "sort_order"],
        unique=False,
    )

    op.create_table(
        "pay_order",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("order_no", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("level_id", sa.Integer(), nullable=False),
        sa.Column("level", sa.Integer(), nullable=False),
        sa.Column("level_code", sa.String(length=32), nullable=False),
        sa.Column("level_name", sa.String(length=64), nullable=False),
        sa.Column("duration_days", sa.Integer(), nullable=False),
        sa.Column("amount", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("currency", sa.String(length=3), server_default="CNY", nullable=False),
        sa.Column("status", sa.String(length=16), server_default="created", nullable=False),
        sa.Column("channel", sa.String(length=16), nullable=False),
        sa.Column("provider_txn_id", sa.String(length=64), nullable=True),
        sa.Column("paid_at", sa.DateTime(), nullable=True),
        sa.Column("expire_at", sa.DateTime(), nullable=False),
        sa.Column("close_reason", sa.String(length=64), nullable=True),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_pay_order")),
        sa.UniqueConstraint("order_no", name=op.f("uq_pay_order_order_no")),
    )
    op.create_index("ix_pay_order_user_id", "pay_order", ["user_id"], unique=False)
    op.create_index("ix_pay_order_status", "pay_order", ["status"], unique=False)
    op.create_index("ix_pay_order_expire_at", "pay_order", ["expire_at"], unique=False)
    op.create_index("ix_pay_order_user_status", "pay_order", ["user_id", "status"], unique=False)

    op.create_table(
        "member_subscription",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("level_id", sa.Integer(), nullable=False),
        sa.Column("level", sa.Integer(), nullable=False),
        sa.Column("level_code", sa.String(length=32), nullable=False),
        sa.Column("level_name", sa.String(length=64), nullable=False),
        sa.Column("source", sa.String(length=16), server_default="order", nullable=False),
        sa.Column("order_no", sa.String(length=32), nullable=True),
        sa.Column("start_at", sa.DateTime(), nullable=False),
        sa.Column("expire_at", sa.DateTime(), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="active", nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_member_subscription")),
        sa.UniqueConstraint("order_no", name=op.f("uq_member_subscription_order_no")),
    )
    op.create_index(
        "ix_member_subscription_user_id", "member_subscription", ["user_id"], unique=False
    )
    op.create_index(
        "ix_member_subscription_expire_at", "member_subscription", ["expire_at"], unique=False
    )
    op.create_index(
        "ix_member_subscription_status", "member_subscription", ["status"], unique=False
    )
    op.create_index(
        "ix_member_subscription_user_status",
        "member_subscription",
        ["user_id", "status"],
        unique=False,
    )

    op.create_table(
        "pay_event",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("order_no", sa.String(length=32), nullable=False),
        sa.Column("event_id", sa.String(length=64), nullable=False),
        sa.Column("channel", sa.String(length=16), nullable=False),
        sa.Column("status_before", sa.String(length=16), nullable=True),
        sa.Column("status_after", sa.String(length=16), nullable=True),
        sa.Column("amount", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("signature_ok", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("applied", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("detail", sa.String(length=255), server_default="", nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_pay_event")),
        sa.UniqueConstraint("event_id", name=op.f("uq_pay_event_event_id")),
    )
    op.create_index(
        "ix_pay_event_order_created", "pay_event", ["order_no", "created_at"], unique=False
    )

    _seed_member_manage()
    _sync_schema_rev(revision)


def downgrade() -> None:
    op.drop_index("ix_pay_event_order_created", table_name="pay_event")
    op.drop_table("pay_event")
    op.drop_index("ix_member_subscription_user_status", table_name="member_subscription")
    op.drop_index("ix_member_subscription_status", table_name="member_subscription")
    op.drop_index("ix_member_subscription_expire_at", table_name="member_subscription")
    op.drop_index("ix_member_subscription_user_id", table_name="member_subscription")
    op.drop_table("member_subscription")
    op.drop_index("ix_pay_order_user_status", table_name="pay_order")
    op.drop_index("ix_pay_order_expire_at", table_name="pay_order")
    op.drop_index("ix_pay_order_status", table_name="pay_order")
    op.drop_index("ix_pay_order_user_id", table_name="pay_order")
    op.drop_table("pay_order")
    op.drop_index("ix_member_level_enabled_sort", table_name="member_level")
    op.drop_index("ix_member_level_delete_seq", table_name="member_level")
    op.drop_table("member_level")
    op.execute(
        sa.text("DELETE FROM sys_role_permission WHERE permission_code = :code").bindparams(
            code=MEMBER_MANAGE
        )
    )
    op.execute(
        sa.text("DELETE FROM sys_permission WHERE code = :code").bindparams(code=MEMBER_MANAGE)
    )
    _sync_schema_rev("0010_modes")
