"""Роли: системные и пользовательские (создаёт только администратор).

Revision ID: 0003
Revises: 0002
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "roles",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("name", sa.String(80), nullable=False, unique=True),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("permissions", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("system", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.String(64), nullable=True),
    )
    roles = sa.table("roles", sa.column("id", sa.String), sa.column("name", sa.String), sa.column("description", sa.Text),
                     sa.column("permissions", postgresql.JSONB), sa.column("system", sa.Boolean),
                     sa.column("active", sa.Boolean), sa.column("created_at", sa.DateTime(timezone=True)),
                     sa.column("created_by", sa.String))
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    names = {"train_dispatcher": "Поездной диспетчер", "station_dispatcher": "Станционный диспетчер",
             "duty_officer": "Дежурный по станции", "admin": "Администратор", "observer": "Наблюдатель"}
    op.bulk_insert(roles, [{"id": k, "name": v, "description": "Системная роль (права заданы в коде)",
                            "permissions": [], "system": True, "active": True, "created_at": now,
                            "created_by": "migration"} for k, v in names.items()])


def downgrade():
    op.drop_table("roles")
