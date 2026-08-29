"""Передача зміни: записки нічного оператора + скріншоти до них.

Revision ID: 0023_add_shift_notes
Revises: 0022_dedupe_clients

Нічний оператор іде о ~05:00, наступний приходить о ~08:00 — три години без
людей у цеху. Печі, стан верстатів, «цю не запускай» передаються СМС-ками;
у CRM цій інформації не було де жити. Дві таблиці: сама записка й вставлені
в неї скріншоти (байти лежать на диску під SHIFT_IMAGES_PATH).

Жодне поле часу не має server_default: на SQLite func.now() пише UTC, а тут
час — це зміст записки («відкрити о 9:00»). Мітки ставить сервісний шар.
Див. app/models.py::ShiftNote.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0023_add_shift_notes"
down_revision: str | None = "0022_dedupe_clients"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "shift_notes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("author_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("edited_at", sa.DateTime(), nullable=True),
        sa.Column("acknowledged_at", sa.DateTime(), nullable=True),
        sa.Column(
            "acknowledged_by_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True
        ),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.Column(
            "resolved_by_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True
        ),
    )
    op.create_index("ix_shift_notes_kind", "shift_notes", ["kind"])
    op.create_index("ix_shift_notes_author_id", "shift_notes", ["author_id"])
    op.create_index("ix_shift_notes_created_at", "shift_notes", ["created_at"])
    op.create_index(
        "ix_shift_notes_acknowledged_by_id", "shift_notes", ["acknowledged_by_id"]
    )
    op.create_index("ix_shift_notes_resolved_by_id", "shift_notes", ["resolved_by_id"])

    op.create_table(
        "shift_note_images",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "note_id",
            sa.Integer(),
            sa.ForeignKey("shift_notes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("filename", sa.String(length=300), nullable=False),
        sa.Column("saved_path", sa.String(length=500), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("pruned_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_shift_note_images_note_id", "shift_note_images", ["note_id"])
    op.create_index(
        "ix_shift_note_images_created_at", "shift_note_images", ["created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_shift_note_images_created_at", table_name="shift_note_images")
    op.drop_index("ix_shift_note_images_note_id", table_name="shift_note_images")
    op.drop_table("shift_note_images")

    op.drop_index("ix_shift_notes_resolved_by_id", table_name="shift_notes")
    op.drop_index("ix_shift_notes_acknowledged_by_id", table_name="shift_notes")
    op.drop_index("ix_shift_notes_created_at", table_name="shift_notes")
    op.drop_index("ix_shift_notes_author_id", table_name="shift_notes")
    op.drop_index("ix_shift_notes_kind", table_name="shift_notes")
    op.drop_table("shift_notes")
