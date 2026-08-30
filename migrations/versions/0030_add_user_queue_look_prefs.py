"""Вигляд черги на акаунті оператора: щільність, відступ, колонка кольору, крок.

Revision ID: 0030_add_user_queue_look_prefs
Revises: 0029_add_user_mail_list_prefs

Ці два значення (щільність і вигляд колонки «Матеріал / Колір») жили в
localStorage: гинули при зміні браузера й не їхали за оператором. Тепер вони
на акаунті — ЄДИНИМ джерелом, бо два місця для одного значення рано чи пізно
розходяться. Ширини стовпців лишаються локальними свідомо: вони прив'язані до
конкретного монітора, а не до людини.

Канон — порожньо/0, тобто рівно те, що бачить оператор, який нічого не крутив.
Наявні локальні налаштування не переносяться: значення в localStorage нікому не
належать (браузер спільний), а вгадувати за них — гірше, ніж почати з канону.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0030_add_user_queue_look_prefs"
down_revision: str | None = "0029_add_user_mail_list_prefs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TEXT = ("queue_density", "queue_mat_style")
_INT = ("queue_row_pad", "queue_ui_step")


def upgrade() -> None:
    for name in _TEXT:
        op.add_column(
            "users",
            sa.Column(name, sa.String(length=20), nullable=False, server_default=""),
        )
    for name in _INT:
        op.add_column(
            "users",
            sa.Column(name, sa.Integer(), nullable=False, server_default="0"),
        )


def downgrade() -> None:
    for name in reversed(_INT + _TEXT):
        op.drop_column("users", name)
