"""Мобильный процесс работников: сообщения о дефектах, заявки на работы, контрольные осмотры,
история, вложения, уведомления, области доступа пользователей.

Revision ID: 0004
Revises: 0003
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

TS = sa.DateTime(timezone=True)
J = postgresql.JSONB()


def upgrade():
    op.add_column("wagons", sa.Column("track_id", sa.String(32), nullable=True))
    op.add_column("operations", sa.Column("work_order_id", sa.String(32), nullable=True))
    op.create_index("ix_operations_work_order_id", "operations", ["work_order_id"])

    op.create_table(
        "user_scopes",
        sa.Column("user_id", sa.String(32), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("station_id", sa.String(16), nullable=False),
        sa.Column("pto_id", sa.String(32)),
        sa.Column("brigade_id", sa.String(32)),
    )
    op.create_table(
        "defect_reports",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("number", sa.Integer, nullable=False),
        sa.Column("client_uuid", sa.String(64), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("station_id", sa.String(16), nullable=False, index=True),
        sa.Column("pto_id", sa.String(32)),
        sa.Column("author_id", sa.String(32), nullable=False),
        sa.Column("wagon_id", sa.String(32), index=True),
        sa.Column("wagon_number_raw", sa.String(16), nullable=False),
        sa.Column("train_id", sa.String(32)),
        sa.Column("track_id", sa.String(32)),
        sa.Column("position", sa.Integer),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("component", sa.String(120)),
        sa.Column("description", sa.Text, nullable=False),
        sa.Column("urgency", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("restriction_active", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("fault_open", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("blocking", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("decision", sa.String(24)),
        sa.Column("decision_reason", sa.Text),
        sa.Column("duplicate_of", sa.String(32)),
        sa.Column("location", J),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("client_created_at", TS),
        sa.Column("acknowledged_at", TS),
        sa.Column("acknowledged_by", sa.String(32)),
        sa.Column("updated_at", TS, nullable=False),
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
        sa.UniqueConstraint("author_id", "client_uuid", name="uq_defect_client_uuid"),
    )
    op.create_table(
        "work_orders",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("number", sa.Integer, nullable=False),
        sa.Column("station_id", sa.String(16), nullable=False, index=True),
        sa.Column("pto_id", sa.String(32)),
        sa.Column("defect_report_id", sa.String(32), index=True),
        sa.Column("wagon_id", sa.String(32), nullable=False, index=True),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("hold_from", sa.String(24)),
        sa.Column("hold_reason", sa.Text),
        sa.Column("brigade_id", sa.String(32)),
        sa.Column("assignee_id", sa.String(32), index=True),
        sa.Column("executed_by", J, nullable=False, server_default="[]"),
        sa.Column("actions", J, nullable=False, server_default="[]"),
        sa.Column("due_at", TS),
        sa.Column("replacement_wagon_id", sa.String(32)),
        sa.Column("operation_ids", J, nullable=False, server_default="[]"),
        sa.Column("report", sa.Text),
        sa.Column("materials", sa.Text),
        sa.Column("cancel_reason", sa.Text),
        sa.Column("created_by", sa.String(32), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("started_at", TS),
        sa.Column("submitted_at", TS),
        sa.Column("completed_at", TS),
        sa.Column("updated_at", TS, nullable=False),
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
    )
    # исправный вагон нельзя зарезервировать под две незавершённые замены
    op.create_index("uq_work_orders_replacement_wagon", "work_orders", ["replacement_wagon_id"], unique=True,
                    postgresql_where=sa.text("replacement_wagon_id IS NOT NULL AND status NOT IN ('completed', 'cancelled')"))
    op.create_table(
        "work_inspections",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("work_order_id", sa.String(32), nullable=False, index=True),
        sa.Column("inspector_id", sa.String(32), nullable=False),
        sa.Column("result", sa.String(16), nullable=False),
        sa.Column("comment", sa.Text, nullable=False, server_default=""),
        sa.Column("created_at", TS, nullable=False),
    )
    op.create_table(
        "work_events",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("entity_type", sa.String(16), nullable=False),
        sa.Column("entity_id", sa.String(32), nullable=False, index=True),
        sa.Column("user_id", sa.String(32)),
        sa.Column("user_name", sa.String(120), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("from_status", sa.String(24)),
        sa.Column("to_status", sa.String(24)),
        sa.Column("text", sa.Text, nullable=False, server_default=""),
        sa.Column("created_at", TS, nullable=False),
    )
    op.create_table(
        "attachments",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("client_uuid", sa.String(64), nullable=False),
        sa.Column("uploader_id", sa.String(32), nullable=False),
        sa.Column("owner_type", sa.String(16)),
        sa.Column("owner_id", sa.String(32), index=True),
        sa.Column("original_name", sa.String(120), nullable=False),
        sa.Column("content_type", sa.String(48), nullable=False),
        sa.Column("size", sa.Integer, nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("storage_name", sa.String(80), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.UniqueConstraint("uploader_id", "client_uuid", name="uq_attachment_client_uuid"),
    )
    op.create_table(
        "notifications",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.String(32), nullable=False, index=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("body", sa.Text, nullable=False, server_default=""),
        sa.Column("entity_type", sa.String(16), nullable=False),
        sa.Column("entity_id", sa.String(32), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("read_at", TS),
    )


def downgrade():
    for t in ("notifications", "attachments", "work_events", "work_inspections"):
        op.drop_table(t)
    op.drop_index("uq_work_orders_replacement_wagon", table_name="work_orders")
    op.drop_table("work_orders")
    op.drop_table("defect_reports")
    op.drop_table("user_scopes")
    op.drop_index("ix_operations_work_order_id", table_name="operations")
    op.drop_column("operations", "work_order_id")
    op.drop_column("wagons", "track_id")
