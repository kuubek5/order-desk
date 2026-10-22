"""Дії людини над роботою, у якої більше немає свого рядка в Google Таблиці.

Привід — #6472 (20.10.26): рядок поштової роботи свідомо видалили, на його
номер ліг інший лист. CRM правильно відмовлялась фарбувати чужий рядок, але
нескінченно повторювала заливку, а зробити з цим у CRM не було чого. Дві дії
(рішення власника 22.10.26), обидві — в «Журнал дій» і зі «Крок назад» (←):

* **«Рядок видалено свідомо — прибрати з CRM»** (`archive_rowless`) — те
  саме, що «Видалити з черги» в паспорті (архів, історія лишається, файли не
  чіпаються), АЛЕ таблицю не чіпаємо взагалі: рядка вже немає, а за його
  номером тепер може стояти чужа робота. Без діалогу підтвердження —
  скасовується стрілкою, а звичний «ОК» привчає клацати не читаючи.
* **«Забути про заливку» / «Не дописувати рядок»** (`forget_pending`) —
  робота лишається, знімається лише позначка «ще не в таблиці»; таблицю
  людина править сама.

Права — ті самі, що в «Видалити з черги»: будь-який оператор, що увійшов.
Пауза синку — відмова, як і у видаленні: на паузі «Крок назад» не працює, і
дію не можна було б скасувати.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.business_day import utc_now
from app.models import Order, StatusEvent, User
from app.services import sheet_stuck_writes
from app.services.undo import (
    FORGETTABLE_FLAGS,
    ROWLESS_ARCHIVE_MARK,
    flag_journal_value,
    log_action,
)

# Вид запису в банері → позначка в базі, яку можна зняти рукою.
FLAG_BY_KIND = {"fill": "fill_pending", "row": "sheet_row_pending"}

FORGET_LABEL = {"fill": "Забути про заливку", "row": "Не дописувати рядок"}


def sheet_links(db: Session, order_ids) -> dict[int, str]:
    """«Відкрити в таблиці ↗»: вкладка (gid з памʼяті, `sheets.sheet_row_url`)
    і збережений рядок роботи. До Google НЕ ходить; gid ще невідомий (щойно
    після перезапуску, доки синк не прочитав листинг) — посилання просто немає."""
    from sqlalchemy import select

    from app.parser import HEADER_ROWS
    from app.settings_store import get_google_sheet_id
    from app.sheets import sheet_row_url

    ids = sorted(set(order_ids))
    if not ids:
        return {}
    sheet_id = (get_google_sheet_id(db) or "").strip()
    links: dict[int, str] = {}
    for order_id, tab, row_number in db.execute(
        select(Order.id, Order.sheet_tab, Order.row_number).where(Order.id.in_(ids))
    ):
        url = sheet_row_url(
            sheet_id, tab, row_number + HEADER_ROWS if row_number is not None else None
        )
        if url:
            links[order_id] = url
    return links


def banner_extras(db: Session, items: list) -> dict:
    """Те, що банер «не дійшло» бере з бази на кожен рендер (сторінка черги й
    полл): скільки листів зведено в роботу (текст «прибрати з CRM») і
    посилання на рядок у таблиці. Один запит на кожне, лише для робіт банера."""
    from app.services.mail_merge import merged_letter_count

    lost_ids = sorted({item.order_id for item in items if item.reason in ("row", "lost")})
    return {
        "write_failure_merged": merged_letter_count(db, lost_ids) if lost_ids else {},
        "write_failure_links": sheet_links(db, [item.order_id for item in items]),
    }


def who(order: Order) -> str:
    return order.client_name or order.work_order_no or f"#{order.id}"


def archive_rowless(db: Session, order: Order, user: User, *, merged_letters: int = 0) -> str:
    """Прибрати роботу з CRM, не чіпаючи таблицю. Комітить. Повертає текст
    тосту. Зведена робота (кілька листів) прибирається ВСЯ — текст це каже."""
    order.archived_at = utc_now()
    note = "прибрано з CRM: рядка в таблиці немає (видалено свідомо)"
    db.add(StatusEvent(
        order_id=order.id, operator_id=user.id, status=order.status,
        actor=user.username, note=note,
    ))
    log_action(
        db, order=order, operator=user, action_type="delete", field="archived_at",
        old="", new=ROWLESS_ARCHIVE_MARK,
        note=f"видалено з черги (рядка в таблиці немає): {who(order)}",
    )
    db.commit()
    sheet_stuck_writes.forget_order(order.id)
    letters = f" (зведена робота — {merged_letters} листів)" if merged_letters > 1 else ""
    return (
        f"{who(order)}{letters}: прибрано з черги в «Архів», таблицю не чіпали "
        "· «Крок назад» (←) поверне"
    )


def forget_pending(db: Session, order: Order, user: User, kind: str) -> str | None:
    """Зняти позначку «ще не в таблиці» виду ``kind``. Комітить. Повертає текст
    тосту, або None — позначки вже немає (повтор встиг або хтось зняв)."""
    field = FLAG_BY_KIND.get(kind)
    if field is None or field not in FORGETTABLE_FLAGS:
        raise ValueError(f"невідомий вид позначки: {kind}")
    old = getattr(order, field)
    if old is None:
        return None
    setattr(order, field, None)
    log_action(
        db, order=order, operator=user, action_type="forget_pending", field=field,
        old=flag_journal_value(field, old), new="",
        note=f"{FORGET_LABEL[kind].lower()}: {who(order)}",
    )
    db.commit()
    sheet_stuck_writes.forget_order(order.id, kinds=(kind,))
    return f"{who(order)}: {FORGET_LABEL[kind].lower()} — позначку знято · «Крок назад» (←) поверне"
