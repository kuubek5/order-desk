"""Журнал обривів зв'язку з верстатами + галочка детального режиму.

Revision ID: 0055_machine_link_events
Revises: 0054_cam_blanks

Скарга власника 09.09.26: «періодично обривається зв'язок віджета зі
станком». Відповісти на це було нічим. Стан верстата живе в памʼяті процесу
(`app/services/machines._states`) і зникає на рестарті, а в базі є лише
`machine_readings`, і політика запису туди свідомо ощадна: перша невдача
негайно, далі однакові раз на 15 хвилин. Для «скільки простояв» цього
досить, для «чому воно рветься регулярно» — ні: обрив на дві хвилини не
лишає між двома п'ятнадцятихвилинними рядками ЖОДНОГО сліду, а саме короткі
обриви й невловимі.

Тут рядок пишеться на ПЕРЕХІД: рівно двічі на обрив (початок і кінець),
скільки б той не тривав. Мертвий за ніч верстат дає один рядок замість
тисяч, і при цьому жоден обрив не губиться. Це дає дві речі, яких не було:
частоту («о котрій рветься») і одночасність («усі три разом = мережа, один
= той ПК»).

У рядку лежать лише ФАКТИ — клас поломки, сирий текст, відповіді портів із
часом. Людський розбір будується при показі (`app/services/machine_link`),
тому виправлене формулювання лікує і вже записані обриви.

`machines.diagnose_link` — галочка на ОКРЕМИЙ верстат. Сам журнал працює
завжди й для всіх (він дешевий і має бути гарантований); галочка вмикає
дорожчу частину: стук у порти на кожному обриві, покроковий результат
кожного порту з часом відповіді, перелік змін тексту помилки за обрив.
Ставиться поштучно, бо рветься зазвичай один верстат, а платити зайвим
стуком за всі десять немає за що.

Час локальний і без серверного дефолту — як у показаннях обладнання: його
звіряють з годинником на стіні, а дефолт на SQLite писав би за Гринвічем.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0055_machine_link_events"
down_revision: str | None = "0054_cam_blanks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "machine_link_events"
_COLUMN = "diagnose_link"


def _has_table(bind) -> bool:
    return _TABLE in sa.inspect(bind).get_table_names()


def _has_column(bind) -> bool:
    if "machines" not in sa.inspect(bind).get_table_names():
        return True  # немає самої таблиці верстатів — нічого й додавати
    names = {c["name"] for c in sa.inspect(bind).get_columns("machines")}
    return _COLUMN in names


def upgrade() -> None:
    bind = op.get_bind()

    if not _has_column(bind):
        # server_default обовʼязковий: колонка NOT NULL додається до таблиці,
        # у якій уже є рядки. Дефолт лишається на місці — переливати таблицю
        # заради його зняття на SQLite дорожче, ніж він коштує.
        op.add_column(
            "machines",
            sa.Column(
                _COLUMN, sa.Boolean(), nullable=False, server_default=sa.text("0")
            ),
        )

    if _has_table(bind):
        return
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Integer(), primary_key=True),
        # MachineTarget.key (host або host-port) — як у machine_readings.
        sa.Column("host", sa.String(60), nullable=False, index=True),
        # Назва знімком, не через FK: верстат можуть перейменувати чи
        # видалити, а історія обривів має лишитись читабельною.
        sa.Column("name", sa.String(120), nullable=False, server_default=""),
        # Остання успішна відповідь перед обривом. NULL = верстат не
        # відповідав жодного разу з моменту запуску застосунку.
        sa.Column("started_at", sa.DateTime(timezone=False), nullable=True),
        sa.Column("detected_at", sa.DateTime(timezone=False), nullable=False, index=True),
        # NULL = обрив триває або застосунок перезапустили посеред нього.
        sa.Column("ended_at", sa.DateTime(timezone=False), nullable=True),
        sa.Column("error", sa.String(300), nullable=False, server_default=""),
        sa.Column("cause", sa.String(30), nullable=False, server_default="", index=True),
        # [{"port": 445, "result": "refused", "ms": 14}, ...]; NULL = не стукали.
        sa.Column("probe_json", sa.Text(), nullable=True),
        sa.Column("probe_verdict", sa.String(300), nullable=True),
        # Стук завершився вже після відновлення: на плитку такий вирок не
        # чіпляють, але в журналі він лишається з міткою — це все одно доказ.
        sa.Column("probe_late", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("failed_polls", sa.Integer(), nullable=False, server_default=sa.text("0")),
        # [{"at": "HH:MM:SS", "error": "..."}] — лише в детальному режимі.
        sa.Column("error_trail", sa.Text(), nullable=True),
        sa.Column("deep", sa.Boolean(), nullable=False, server_default=sa.text("0")),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if _has_table(bind):
        op.drop_table(_TABLE)
    if not _has_column(bind):
        return
    if "machines" in sa.inspect(bind).get_table_names():
        op.drop_column("machines", _COLUMN)
