"""Верстати: ручний режим збору калібрувальних кадрів.

Revision ID: 0037_machine_collect_calibration
Revises: 0036_add_user_machine_widget_prefs

Авто-збір калібрувальних кадрів працює лише для RemiCORE (кадр відкладається,
коли прочитано відсоток). На верстаті, де відсоток ще не читається (нове
покоління CORiTEC, інша розкладка), збирач мовчав, і кадри доводилось зберігати
з браузера руками. `collect_calibration` вмикає збір за ЧАСОМ для конкретного
верстата: оператор вмикає на час калібрування, качає кадри, вимикає.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0037_machine_collect_calibration"
down_revision: str | None = "0036_add_user_machine_widget_prefs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "machines",
        sa.Column(
            "collect_calibration",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("machines", "collect_calibration")
