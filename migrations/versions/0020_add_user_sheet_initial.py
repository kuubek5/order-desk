"""Operator sheet initial — the 1-2 letter mark of who calculated a work.

Revision ID: 0020_add_user_sheet_initial
Revises: 0019_enable_sheet_changed_notify

The lab writes a single/double letter in the sheet's "Прорахував" column (М for
a normal work, Х for a rework) to record which cam operator calculated it in
Sum3D — Р=Рома, К=Костя, СТ=Стас, В=Вадім… Giving each operator account its own
letter lets the portal stamp that mark automatically when the operator enters a
Sum3D ID, so the sheet keeps its existing human convention and the "who
calculated" is tied to the real logged-in operator. NULL until an admin assigns
one; letters are kept unique across operators by the settings route.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0020_add_user_sheet_initial"
down_revision: str | None = "0019_enable_sheet_changed_notify"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("sheet_initial", sa.String(length=2), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.drop_column("sheet_initial")
