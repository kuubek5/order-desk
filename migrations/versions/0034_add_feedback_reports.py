"""Форма зворотного зв'язку: таблиці feedback + feedback_images.

Revision ID: 0034_add_feedback_reports
Revises: 0033_default_theme_forge

Оператор шле баг/ідею/питання зі скріншотом. Джерело правди — база (запис
лягає завжди, навіть коли Telegram недосяжний); пуш у Telegram — окремий
необов'язковий крок, стан якого лежить у telegram_* полях. feedback_images —
дзеркало shift_note_images (той самий патерн зберігання скріншотів).
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0034_add_feedback_reports"
down_revision: str | None = "0033_default_theme_forge"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "feedback",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("severity", sa.String(length=20), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("screen", sa.String(length=60), nullable=True),
        sa.Column("app_version", sa.String(length=40), nullable=True),
        sa.Column("author_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="new"),
        sa.Column("seen_at", sa.DateTime(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.Column("telegram_sent_at", sa.DateTime(), nullable=True),
        sa.Column("telegram_error", sa.String(length=300), nullable=True),
        sa.Column("telegram_attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index("ix_feedback_kind", "feedback", ["kind"])
    op.create_index("ix_feedback_author_id", "feedback", ["author_id"])
    op.create_index("ix_feedback_created_at", "feedback", ["created_at"])
    op.create_index("ix_feedback_status", "feedback", ["status"])

    op.create_table(
        "feedback_images",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "feedback_id",
            sa.Integer(),
            sa.ForeignKey("feedback.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("filename", sa.String(length=300), nullable=False),
        sa.Column("saved_path", sa.String(length=500), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_feedback_images_feedback_id", "feedback_images", ["feedback_id"])
    op.create_index("ix_feedback_images_created_at", "feedback_images", ["created_at"])


def downgrade() -> None:
    op.drop_table("feedback_images")
    op.drop_table("feedback")
