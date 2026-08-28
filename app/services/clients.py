"""Картки клієнтів: створення з реальних робіт і прив'язка теки в `export`.

Один клієнт пишеться в таблиці кількома способами («Кривовид», «Кривовид кл»),
а на диску тека зветься ще інакше — тому і створення карток, і підбір теки
йдуть через той самий нечіткий матчер (`app/client_matcher.py`), що й видача.
Розійтись вони не мають права: інакше на видачі й у «Клієнтах» будуть різні
люди.
"""

import logging
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.client_matcher import match_client_name
from app.export_scanner import list_export_client_names_cached
from app.models import Client, ClientNameAlias, Order
from app.services.order_dates import sheet_order_key
from app.settings_store import get_export_folder_path

logger = logging.getLogger(__name__)

CLIENT_STATE_FILTERS = ("all", "unbound", "active")


def quantity_units(raw: str | None) -> int:
    """Units in a sheet quantity cell. Free text ("4", "4 шт", ""), so anything
    unparseable counts as 0 rather than breaking the client's total."""
    digits = "".join(ch for ch in (raw or "") if ch.isdigit())
    return int(digits) if digits else 0


def ensure_client_profiles(db: Session, named_orders: list[Order]) -> int:
    """Give every client that appears in real work a card, so «Клієнти» and the
    morning handout show the SAME people.

    Without this, a client only existed on the handout screen (derived from the
    free-text Order.client_name) and had no card to configure — which is where
    the export-folder binding lives, so they could never be set up at all.

    A new card is created only when no existing client already covers that name:
    the sheet spells the same lab several ways («Кривовид», «Кривовид кл»), and a
    card per spelling would fragment one client into several. Reuses the same
    fuzzy matcher the folder binding uses, with client names in place of folder
    names. Idempotent — running it on every /clients visit adds nothing once the
    cards exist. Returns how many were created."""
    existing = [c.canonical_name for c in db.scalars(select(Client)).all() if c.canonical_name]
    seen = {name.strip().casefold() for name in existing}
    created = 0

    # Stable order (sheet order, then name) so a batch of new cards lands in a
    # predictable sequence rather than SQLAlchemy's iteration order.
    order_names: list[str] = []
    # Набір складок тримаємо окремо: перебудова його всередині циклу робила
    # прохід квадратичним по кількості робіт.
    order_name_folds: set[str] = set()
    for order in sorted(named_orders, key=sheet_order_key):
        name = (order.client_name or "").strip()
        if name and name.casefold() not in order_name_folds:
            order_names.append(name)
            order_name_folds.add(name.casefold())

    for name in order_names:
        if name.casefold() in seen:
            continue
        # An existing card already spelled this client another way — reuse it.
        if existing and match_client_name(name, existing, {}).matched_folder_name:
            continue
        db.add(Client(canonical_name=name))
        existing.append(name)
        seen.add(name.casefold())
        created += 1

    if created:
        try:
            db.commit()
        except IntegrityError:
            # Гонка: паралельний запит устиг створити ту саму картку між нашим
            # читанням і комітом. Саме так на проді з'явилось по два ANTON —
            # видача трималась 60с на мережевому сховищі й тримала вікно гонки
            # відкритим. Тепер унікальний індекс (міграція 0022) її ловить, а
            # ми просто відкочуємось: картки все одно вже існують.
            db.rollback()
            logger.info("Картки клієнтів уже створив паралельний запит — пропускаю")
            return 0
    return created


def client_folder_options(db: Session, canonical_name: str) -> tuple[list[str], str | None, list[str]]:
    """(every export folder on disk, the one bound to this client, ranked guesses).

    The guesses are the fuzzy candidates the handout screen already computes —
    surfaced here so binding is one click on the right name instead of hunting
    through a few hundred folders.

    ПРОДУКТИВНІСТЬ: тут потрібні ЛИШЕ імена тек 1-го рівня. Раніше стояв
    повний обхід дерева (`scan_export_folder_cached`) — і бойовий лог
    27.08.26 показав ціну: «46148 записів, 511.42с» на Synology, тобто
    `/clients` відкривався 4+ хвилини заради списку з 746 назв, який дає
    один-єдиний scandir кореня."""
    try:
        folder_names = sorted(list_export_client_names_cached(Path(get_export_folder_path(db))))
    except Exception:  # noqa: BLE001 — an unreachable export root must not 500 the card
        folder_names = []

    alias = db.scalar(
        select(ClientNameAlias).where(
            ClientNameAlias.sheet_name == canonical_name,
            ClientNameAlias.confirmed.is_(True),
        )
    )
    bound = alias.export_folder_name if alias else None

    match = match_client_name(canonical_name, folder_names, {})
    suggestions = [name for name, _score in match.candidates if name != bound][:3]
    return folder_names, bound, suggestions
