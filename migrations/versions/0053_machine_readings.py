"""Історія показань верстатів: таблиця machine_readings.

Revision ID: 0053_machine_readings
Revises: 0052_user_handout_flow

Агент верстата вже читає з екрана RemiCORE відсоток виконання і назву
.iso-програми, у якій зашитий час її запуску. Досі все це жило лише в памʼяті
процесу (`app/services/machines._states`) і зникало на рестарті — на відміну
від печей, у яких історія показань є з самого початку.

Через це в системі не існувало жодного способу відповісти, СКІЛЬКИ насправді
фрезерувалась робота: у базі нуль подій «у фрезеруванні» за всю історію, а
колонка «Відфрезерував» у таблиці містить ініціали людини, не час. Оператор
оцінює цей час на око, і від його оцінки залежить рішення закривати піч.

Таблиця навмисно дзеркалить `furnace_readings`: те саме «залізо з екраном»,
та сама політика запису (подія — негайно, потік — з підлогою, раз на хвилину
«я живий»), та сама причина тримати `error` окремим рядком, а не мовчати.
Зміна прив'язки .iso = межа програми, тобто старт і кінець реальної роботи.

Нічого не міняє в роботі оператора: пишемо те, що вже читається.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0053_machine_readings"
down_revision: str | None = "0052_user_handout_flow"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "machine_readings"


def _has_table(bind) -> bool:
    return _TABLE in sa.inspect(bind).get_table_names()


def upgrade() -> None:
    bind = op.get_bind()
    if _has_table(bind):
        return
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Integer(), primary_key=True),
        # Адреса, а не назва: верстат у налаштуваннях можуть перейменувати,
        # і історія показань не має від цього розсипатись (як у печей).
        sa.Column("host", sa.String(60), nullable=False, index=True),
        sa.Column("captured_at", sa.DateTime(timezone=False), nullable=False, index=True),
        sa.Column("percent", sa.Integer(), nullable=True),
        sa.Column("iso_name", sa.String(200), nullable=True),
        # Хвіст HH-MM-SS із назви програми — ключ до рядка черги.
        sa.Column("sum3d_id", sa.String(100), nullable=True, index=True),
        sa.Column("program_at", sa.DateTime(timezone=False), nullable=True),
        # SLM-принтер SISMA: шар N з M замість відсотка.
        sa.Column("layer", sa.Integer(), nullable=True),
        sa.Column("layers_total", sa.Integer(), nullable=True),
        sa.Column("error", sa.String(300), nullable=True),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if _has_table(bind):
        op.drop_table(_TABLE)
