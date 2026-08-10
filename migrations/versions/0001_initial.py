"""Current Order Desk schema baseline.

Revision ID: 0001_initial
Revises:
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("username", sa.String(length=100), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("full_name", sa.String(length=200), nullable=True),
        sa.Column("role", sa.String(length=50), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_users_username", "users", ["username"], unique=True)

    op.create_table(
        "orders",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=50), nullable=False),
        sa.Column("sheet_tab", sa.String(length=50), nullable=True),
        sa.Column("row_number", sa.Integer(), nullable=True),
        sa.Column("work_order_no", sa.String(length=100), nullable=True),
        sa.Column("job_code", sa.String(length=100), nullable=True),
        sa.Column("quantity", sa.String(length=100), nullable=True),
        sa.Column("material_color", sa.String(length=200), nullable=True),
        sa.Column("kind", sa.String(length=200), nullable=True),
        sa.Column("due_time", sa.String(length=20), nullable=True),
        sa.Column("technician_name", sa.String(length=200), nullable=True),
        sa.Column("cam_comment", sa.Text(), nullable=True),
        sa.Column("sum3d_id", sa.String(length=100), nullable=True),
        sa.Column("calculated_raw", sa.String(length=200), nullable=True),
        sa.Column("milled_raw", sa.String(length=200), nullable=True),
        sa.Column("last_milled_date", sa.String(length=50), nullable=True),
        sa.Column("mill_count", sa.String(length=50), nullable=True),
        sa.Column("client_name", sa.String(length=200), nullable=True),
        sa.Column("status", sa.String(length=100), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "client_name_aliases",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("sheet_name", sa.String(length=200), nullable=False),
        sa.Column("export_folder_name", sa.String(length=200), nullable=False),
        sa.Column("confirmed", sa.Boolean(), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_client_name_aliases_sheet_name", "client_name_aliases", ["sheet_name"], unique=False)

    op.create_table(
        "sync_logs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("direction", sa.String(length=50), nullable=False),
        sa.Column("sheet_tab", sa.String(length=50), nullable=True),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "app_settings",
        sa.Column("key", sa.String(length=100), nullable=False),
        sa.Column("value_encrypted", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )

    op.create_table(
        "status_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("order_id", sa.Integer(), nullable=False),
        sa.Column("operator_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=100), nullable=False),
        sa.Column("actor", sa.String(length=200), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(["operator_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_status_events_order_id", "status_events", ["order_id"], unique=False)

    op.create_table(
        "comments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("order_id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=50), nullable=False),
        sa.Column("author", sa.String(length=200), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("synced_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_comments_order_id", "comments", ["order_id"], unique=False)

    op.create_table(
        "rework_records",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("order_id", sa.Integer(), nullable=False),
        sa.Column("occurrence", sa.Integer(), nullable=True),
        sa.Column("blame", sa.String(length=100), nullable=True),
        sa.Column("blame_quantity", sa.String(length=100), nullable=True),
        sa.Column("redo_quantity", sa.String(length=100), nullable=True),
        sa.Column("cam_comment", sa.Text(), nullable=True),
        sa.Column("sum3d_id", sa.String(length=100), nullable=True),
        sa.Column("calculated_raw", sa.String(length=200), nullable=True),
        sa.Column("milled_raw", sa.String(length=200), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_rework_records_order_id", "rework_records", ["order_id"], unique=False)

    op.create_table(
        "email_messages",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("uid", sa.String(length=100), nullable=False),
        sa.Column("from_address", sa.String(length=300), nullable=True),
        sa.Column("subject", sa.String(length=500), nullable=True),
        sa.Column("body_text", sa.Text(), nullable=True),
        sa.Column("received_at", sa.DateTime(), nullable=True),
        sa.Column("client_name_guess", sa.String(length=200), nullable=True),
        sa.Column("material_color_guess", sa.String(length=200), nullable=True),
        sa.Column("kind_guess", sa.String(length=200), nullable=True),
        sa.Column("quantity_guess", sa.String(length=50), nullable=True),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("order_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_email_messages_uid", "email_messages", ["uid"], unique=True)

    op.create_table(
        "attachments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("email_message_id", sa.Integer(), nullable=False),
        sa.Column("filename", sa.String(length=300), nullable=False),
        sa.Column("saved_path", sa.String(length=500), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(["email_message_id"], ["email_messages.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_attachments_email_message_id", "attachments", ["email_message_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_attachments_email_message_id", table_name="attachments")
    op.drop_table("attachments")
    op.drop_index("ix_email_messages_uid", table_name="email_messages")
    op.drop_table("email_messages")
    op.drop_index("ix_rework_records_order_id", table_name="rework_records")
    op.drop_table("rework_records")
    op.drop_index("ix_comments_order_id", table_name="comments")
    op.drop_table("comments")
    op.drop_index("ix_status_events_order_id", table_name="status_events")
    op.drop_table("status_events")
    op.drop_table("app_settings")
    op.drop_table("sync_logs")
    op.drop_index("ix_client_name_aliases_sheet_name", table_name="client_name_aliases")
    op.drop_table("client_name_aliases")
    op.drop_table("orders")
    op.drop_index("ix_users_username", table_name="users")
    op.drop_table("users")
