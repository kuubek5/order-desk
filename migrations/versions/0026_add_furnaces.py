"""Пічки спікання окремою таблицею замість текстового поля в налаштуваннях.

Revision ID: 0026_add_furnaces
Revises: 0025_add_furnace_readings

Перелік пічок жив одним рядком тексту («Назва=адреса» на рядок). Це трималось,
поки пічка була одна; з трьома потрібні окремі поля, вимикач на час ремонту й
можливість власного пароля — тобто рядки таблиці, а не рядки тексту.

Стара настройка `furnace_hosts` тут же переноситься в таблицю й видаляється:
лишити обидва джерела означало б, що одне з них рано чи пізно збреше.

Див. app/models.py::Furnace, app/services/furnace.py.
"""

from collections.abc import Sequence
from datetime import datetime

from alembic import op
import sqlalchemy as sa


revision: str = "0026_add_furnaces"
down_revision: str | None = "0025_add_furnace_readings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _decode(value: str | None) -> str:
    """Розшифрувати збережене значення налаштування.

    Імпорт локальний і в try: міграція мусить пройти навіть там, де ключа
    шифрування немає під рукою (перевірка схеми в CI) — тоді просто нічого не
    переноситься, а таблиця все одно створюється.
    """
    if not value:
        return ""
    try:
        from app.crypto import decrypt_value

        return decrypt_value(value)
    except Exception:  # noqa: BLE001 — перенесення не варте падіння міграції
        return ""


def upgrade() -> None:
    op.create_table(
        "furnaces",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("host", sa.String(length=60), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False, server_default="5900"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("password_encrypted", sa.String(length=500), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("host", "port", name="uq_furnace_host_port"),
    )

    connection = op.get_bind()
    row = connection.execute(
        sa.text("SELECT value_encrypted FROM app_settings WHERE key = 'furnace_hosts'")
    ).first()
    raw = _decode(row[0] if row else None)
    if not raw:
        return

    now = datetime.now()
    seen: set[tuple[str, int]] = set()
    order = 0
    for line in raw.replace(",", "\n").splitlines():
        entry = line.strip()
        if not entry or entry.startswith("#"):
            continue
        name, _, address = entry.rpartition("=")
        host, _, port_text = address.strip().partition(":")
        host = host.strip()
        if not host:
            continue
        try:
            port = int(port_text) if port_text else 5900
        except ValueError:
            port = 5900
        if (host, port) in seen:
            continue
        seen.add((host, port))
        connection.execute(
            sa.text(
                "INSERT INTO furnaces (name, host, port, enabled, sort_order, created_at)"
                " VALUES (:name, :host, :port, 1, :sort_order, :created_at)"
            ),
            {
                "name": name.strip() or host,
                "host": host,
                "port": port,
                "sort_order": order,
                "created_at": now,
            },
        )
        order += 1

    connection.execute(sa.text("DELETE FROM app_settings WHERE key = 'furnace_hosts'"))


def downgrade() -> None:
    op.drop_table("furnaces")
