"""Облік заготовок із теки CAM: таблиця cam_blanks.

Revision ID: 0054_cam_blanks
Revises: 0053_machine_readings

Коли оператор створює новий диск у CAM, на диску зʼявляється файл `.blk`
у теці `<матеріал>/<висота>/`. Створення диска майже завжди означає «взяв
новий з архіву», тобто файл — це готовий слід того, що треба замовити в
комірниці. Досі це замовлення складали, обходячи шухляди руками (~10 хв
щодня) і пишучи повідомлення у Viber.

ЧОМУ ПОДІЯ, А НЕ ПРОСТО СПИСОК НАЗВ. Порядковий номер у назві свій на кожну
групу (матеріал + колір + висота) і ПІСЛЯ підчистки теки починається з малого
(підтверджено власником 08.09.26). Тобто назви ГАРАНТОВАНО повторюються.
Порівнювати самі назви не можна: після першої ж підчистки нові диски
виглядали б як давно бачені. Тому рядок описує ПРИСУТНІСТЬ файлу — коли
зʼявився і коли зник; назва, що виникла знову після зникнення, це новий диск.

Побічна користь тієї ж форми: список «взяте з часу останнього замовлення»
береться з рядків, а не з вмісту теки, тож пізніше видалення файлу нічого
в історії не стирає.

Час локальний (як у ShiftNote і показань обладнання): оператор звіряє його
з годинником на стіні, а серверний дефолт на SQLite писав би за Гринвічем.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0054_cam_blanks"
down_revision: str | None = "0053_machine_readings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "cam_blanks"


def _has_table(bind) -> bool:
    return _TABLE in sa.inspect(bind).get_table_names()


def upgrade() -> None:
    bind = op.get_bind()
    if _has_table(bind):
        return
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Integer(), primary_key=True),
        # Шлях відносно кореня теки заготовок — стабільний ключ присутності.
        sa.Column("rel_path", sa.String(400), nullable=False, index=True),
        # Розібране з шляху й назви. Порожнє = не розібралось; рядок усе одно
        # лишається, бо це взятий диск, і мовчки викидати його не можна.
        sa.Column("material_dir", sa.String(60), nullable=False, server_default=""),
        sa.Column("height_dir", sa.String(20), nullable=False, server_default=""),
        sa.Column("file_name", sa.String(200), nullable=False, server_default=""),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("brand", sa.String(60), nullable=True),
        sa.Column("shade", sa.String(60), nullable=True),
        sa.Column("serial", sa.Integer(), nullable=True),
        # Висота в назві не збіглася з текою — помилка розкладання. Показуємо,
        # а не мовчимо: власник підтвердив, що такого бути не повинно.
        sa.Column("height_mismatch", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("first_seen_at", sa.DateTime(timezone=False), nullable=False, index=True),
        # Файл зник із теки (диск дороблено або теку підчистили).
        sa.Column("gone_at", sa.DateTime(timezone=False), nullable=True),
        # Коли цей диск потрапив у замовлення комірниці. NULL = ще не замовлено.
        sa.Column("ordered_at", sa.DateTime(timezone=False), nullable=True, index=True),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if _has_table(bind):
        op.drop_table(_TABLE)
