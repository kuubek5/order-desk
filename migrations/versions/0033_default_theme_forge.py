"""Дефолтна тема — Amber Forge (бурштинова) замість бірюзового канону.

Revision ID: 0033_default_theme_forge
Revises: 0032_add_machines

Власник попросив зробити стандартним бурштиновий вигляд. Раніше порожній
рядок ui_theme означав «бірюзовий канон за замовчуванням»; тепер:
  - "forge"  — Amber Forge, ДЕФОЛТ;
  - "teal"   — бірюзовий канон (явний вибір);
  - ""       — лише сумісність зі старими БД, резолвиться в "forge".

Тому наявні акаунти з "" переводимо на "forge" (той самий вигляд, що вони
й так бачитимуть через ui_prefs).

Свідомо ЛИШЕ data-UPDATE, без ALTER COLUMN server_default: на SQLite зміна
дефолту вимагає batch_alter_table (перестворення таблиці users), а сторож
test_client_migration вимагає, щоб апгрейд зі старої БД був суто additive —
жодна наявна таблиця не чіпається. Рівень БД тут і не потрібен: нові рядки
створюються через ORM із Python-дефолтом "forge" (models.py), а тести через
create_all беруть model server_default="forge".
"""

from collections.abc import Sequence

from alembic import op


revision: str = "0033_default_theme_forge"
down_revision: str | None = "0032_add_machines"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Наявні «нічого не вибрав» → явний forge (стандарт для всіх акаунтів).
    op.execute("UPDATE users SET ui_theme = 'forge' WHERE ui_theme = ''")


def downgrade() -> None:
    # Зворотного перекладу немає: "forge" міг бути й явним вибором, а вгадати,
    # хто його НЕ вибирав, немає з чого. Схему не чіпали — відкочувати нічого.
    pass
