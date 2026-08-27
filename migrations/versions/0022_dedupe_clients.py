"""Злити задвоєні картки клієнтів і зробити повтор неможливим.

Revision ID: 0022_dedupe_clients
Revises: 0021_add_action_log

На проді екран «Клієнти» показував 458 карток, де кожне ім'я стояло двічі:
ANTON, ANTON, Allakhverdiev, Allakhverdiev…

Причина — гонка, а не логіка. `_ensure_client_profiles` читає наявні картки,
потім додає відсутні; унікального обмеження на `canonical_name` не було, тож
два одночасні запити читали «клієнта нема» й обидва його створювали.
Вікно гонки відкривала повільна видача: вона викликає ту саму функцію і
трималась 60+ секунд на обході мережевого сховища (див. 0.3.18–0.3.20).
Достатньо було відкрити «Клієнти», поки видача ще крутиться.

Зливати безпечно: на `clients.id` не посилається НІЩО. Прив'язка папки
живе в `client_name_aliases` за іменем, роботи знаходяться нечітким
зіставленням `Order.client_name`, зовнішніх ключів немає.

Правило злиття: лишаємо картку, де оператор устиг щось заповнити
(телефон / пошта / нотатки), інакше найстарішу. Решту видаляємо.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0022_dedupe_clients"
down_revision: str | None = "0021_add_action_log"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()

    rows = bind.execute(
        sa.text(
            "SELECT id, canonical_name, phone, email, notes FROM clients"
            " WHERE canonical_name IS NOT NULL"
        )
    ).fetchall()

    groups: dict[str, list] = {}
    for row in rows:
        key = (row.canonical_name or "").strip().casefold()
        if key:
            groups.setdefault(key, []).append(row)

    def _weight(r):
        """Кого лишаємо: спершу заповнену оператором картку, потім ту, чиє
        ім'я вже охайне (без крайніх пробілів, не суцільним нижнім
        регістром), далі найстарішу. Інакше з пари «Aqua Cad» / «  aqua cad »
        лишалась би гірша."""
        name = r.canonical_name or ""
        filled = sum(1 for v in (r.phone, r.email, r.notes) if (v or "").strip())
        tidy = (name == name.strip()) and not name.islower()
        return (-filled, not tidy, r.id)

    doomed: list[int] = []
    renames: list[tuple[int, str]] = []
    for duplicates in groups.values():
        keep = min(duplicates, key=_weight)
        # Крайні пробіли в імені — те, з чого починаються нові дублі.
        tidy_name = (keep.canonical_name or "").strip()
        if tidy_name and tidy_name != keep.canonical_name:
            renames.append((keep.id, tidy_name))
        doomed.extend(r.id for r in duplicates if r.id != keep.id)

    for client_id, tidy_name in renames:
        bind.execute(
            sa.text("UPDATE clients SET canonical_name = :n WHERE id = :i"),
            {"n": tidy_name, "i": client_id},
        )

    if doomed:
        # Пакетами — SQLite має ліміт на кількість параметрів у запиті.
        for start in range(0, len(doomed), 400):
            chunk = doomed[start : start + 400]
            placeholders = ", ".join(f":id{i}" for i in range(len(chunk)))
            bind.execute(
                sa.text(f"DELETE FROM clients WHERE id IN ({placeholders})"),
                {f"id{i}": value for i, value in enumerate(chunk)},
            )

    # Гонка більше не пройде: другий запис просто впаде на обмеженні, а
    # застосунок це переживе (див. _ensure_client_profiles).
    op.create_index(
        "ix_clients_canonical_name_unique",
        "clients",
        [sa.text("lower(trim(canonical_name))")],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_clients_canonical_name_unique", table_name="clients")
    # Видалені дублікати не відновлюємо — вони не несли даних, на які
    # хтось посилається.
