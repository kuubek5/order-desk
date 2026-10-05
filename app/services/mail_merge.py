"""Зведення листів Конвеєра в ОДНУ роботу й один рядок таблиці (власник 05.10.26).

Мультипрорахунок: у Конвеєрі кілька клієнтів. Якщо в одного клієнта кілька
листів ОДНОГО кольору, і вони лягають в ОДНУ теку (той самий ключ
`planned_export_rel`, що вже групує теки з 0.21.38) з тим самим Sum3D, то це
одна робота: кількість сумується, у Google Таблиці один рядок і одна синя
заливка, у черзі й на видачі — один рядок. Інші клієнти того самого Конвеєра
лишаються окремими.

Рішення власника:
  * «Вид роботи» для пошти не важливий — на зведення не впливає;
  * різні Sum3D — не зводити (рядок тримає ОДИН Sum3D);
  * відкат одного листа повертає лише ЦЕЙ лист (`detach_letter`).

Запобіжник: перемикач адміністратора, за замовчуванням ВИМКНЕНО. Вимкнено —
Конвеєр іде рівно старою дорогою (лист = робота = рядок). Відкат зведених
листів від перемикача НЕ залежить: роботи, зведені, поки він був увімкнений,
мусять коректно відкочуватись і після вимкнення.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import EmailMessage, Order, OrderEmail
from app.services.opak import format_opak, opak_units
from app.settings_store import get_setting, set_setting

MERGE_KEY = "mail_merge_folder_rows"


def merge_enabled(db: Session) -> bool:
    """Невідоме/порожнє значення = вимкнено: нова база й зіпсований ключ
    шифрування (`get_setting` деградує в None, §14) не мають раптово міняти
    процес прийняття."""
    return (get_setting(db, MERGE_KEY) or "") == "1"


def set_merge_enabled(db: Session, enabled: bool) -> None:
    """Не комітить — коміт лишається за роутом, як у решті налаштувань."""
    set_setting(db, MERGE_KEY, "1" if enabled else "")


def _digits(value: object) -> int | None:
    text = str(value or "").strip()
    return int(text) if text.isdigit() else None


def _opak_digits(value: object) -> int | None:
    """Опак картки: порожньо → 0, самі цифри → число, інше → None (не зводимо:
    довільний текст у коментарі CAM не додається)."""
    text = str(value or "").strip()
    if not text:
        return 0
    return int(text) if text.isdigit() else None


@dataclass
class MergePlan:
    """Хто з ким зводиться. `group_of[eid]` — ключ групи; лише для груп з ≥2
    придатних листів. `last_of[key]` — eid останнього листа групи в порядку
    Конвеєра: після нього пишеться рядок таблиці з сумою."""

    group_of: dict[int, tuple[str, str]]
    last_of: dict[tuple[str, str], int]


def plan_merge(
    db: Session,
    members: list[tuple[dict, int, EmailMessage]],
    planned: dict[int, str | None],
) -> MergePlan:
    """Розкласти листи Конвеєра на групи зведення.

    `members` — (картка, id листа, лист) у порядку Конвеєра, лише ті, що
    пройшли попередні перевірки. Лист придатний, коли:
      * відома тека (`planned` не None) — інакше не групуємо, а не вгадуємо;
      * кількість — самі цифри (сумувати «2+1» не можна) і опак порожній або
        цифри;
      * у листа ще немає жодної роботи (частково прийнятий у картці лист має
        власну роботу, і відкат такого змішаного листа заплутався б).
    Ключ групи — (тека, Sum3D): різні Sum3D не зводяться.
    """
    by_key: dict[tuple[str, str], list[int]] = {}
    for item, eid, email in members:
        folder = planned.get(eid)
        if not folder:
            continue
        if _digits(item.get("quantity")) is None:
            continue
        if _opak_digits(item.get("opak")) is None:
            continue
        if email.order_id is not None or _has_orders(db, email.id):
            continue
        key = (folder, str(item.get("sum3d_id") or "").strip())
        by_key.setdefault(key, []).append(eid)
    group_of: dict[int, tuple[str, str]] = {}
    last_of: dict[tuple[str, str], int] = {}
    for key, eids in by_key.items():
        if len(eids) < 2:
            continue
        for eid in eids:
            group_of[eid] = key
        last_of[key] = eids[-1]
    return MergePlan(group_of=group_of, last_of=last_of)


def _has_orders(db: Session, email_id: int) -> bool:
    return db.scalar(
        select(Order.id).where(Order.source_email_id == email_id).limit(1)
    ) is not None


def link_primary(db: Session, order: Order, email: EmailMessage, quantity: str, opak: str) -> None:
    """Головний лист групи: його внесок записується так само, як інших, щоб
    відкат головного віднімав саме його кількість."""
    db.add(OrderEmail(
        order_id=order.id, email_message_id=email.id,
        quantity=_digits(quantity) or 0, opak_units=_opak_digits(opak) or None,
    ))


def add_contribution(db: Session, order: Order, email: EmailMessage, quantity: str, opak: str) -> None:
    """Додати лист до зведеної роботи: кількість і опак сумуються в роботі,
    внесок пишеться окремим рядком. Не комітить — прийняття комітить разом із
    переносом файлів (і відкочує разом із ним)."""
    q = _digits(quantity) or 0
    o = _opak_digits(opak) or 0
    order.quantity = str((_digits(order.quantity) or 0) + q)
    if o:
        total = (order.opak_units or 0) + o
        order.cam_comment = format_opak(str(total)) or None
        order.opak_units = opak_units(order.cam_comment)
    db.add(OrderEmail(
        order_id=order.id, email_message_id=email.id, quantity=q, opak_units=o or None,
    ))
    if email.order_id is None:
        # Legacy «перша робота листа»: решта коду (чи має лист роботу, перенос
        # у папки пошти) питає саме це поле.
        email.order_id = order.id


def detachable_link(db: Session, email: EmailMessage) -> OrderEmail | None:
    """Внесок листа у зведену роботу, яку ще тримають ІНШІ листи. Тоді відкат
    цього листа — лише відняти внесок, а не видаляти роботу. None — лист не в
    групі або він останній у ній (тоді звичайний відкат: робота видаляється)."""
    link = db.scalar(select(OrderEmail).where(OrderEmail.email_message_id == email.id))
    if link is None:
        return None
    others = db.scalar(
        select(OrderEmail.id).where(
            OrderEmail.order_id == link.order_id,
            OrderEmail.email_message_id != email.id,
        ).limit(1)
    )
    return link if others is not None else None


def detach_letter(db: Session, link: OrderEmail, email: EmailMessage) -> tuple[Order, set[str]]:
    """Відняти внесок листа від зведеної роботи. Робота лишається з рештою
    листів; якщо відкочується головний лист, головність переходить до
    наступного (найменший id), щоб `source_email_id` не вказував на лист,
    який уже у «Вхідних». Не комітить і не чіпає файли й таблицю — це робить
    викликач (`_unaccept_email`).

    Повертає роботу й поля, які треба переписати в рядку таблиці. Коментар
    CAM переписується, лише коли в ньому досі рівно наш текст «N opaq»: його
    могли доповнити в спільній таблиці, і синк приніс би той текст сюди —
    затерти чуже уточнення гірше, ніж лишити опак як є."""
    order = link.order
    changed = {"quantity"}
    order.quantity = str(max((_digits(order.quantity) or 0) - (link.quantity or 0), 0))
    if link.opak_units:
        total = order.opak_units or 0
        if (order.cam_comment or "") == format_opak(str(total)):
            left = max(total - link.opak_units, 0)
            order.cam_comment = format_opak(str(left)) or None
            order.opak_units = opak_units(order.cam_comment)
            changed.add("cam_comment")
    if order.source_email_id == email.id:
        heir = db.scalar(
            select(OrderEmail.email_message_id)
            .where(OrderEmail.order_id == order.id, OrderEmail.email_message_id != email.id)
            .order_by(OrderEmail.email_message_id)
            .limit(1)
        )
        order.source_email_id = heir
    db.delete(link)
    return order, changed


def merged_letter_count(db: Session, order_ids: list[int]) -> dict[int, int]:
    """Скільки листів зведено в кожну роботу (лише зведені, ≥2)."""
    if not order_ids:
        return {}
    from sqlalchemy import func

    rows = db.execute(
        select(OrderEmail.order_id, func.count(OrderEmail.id))
        .where(OrderEmail.order_id.in_(order_ids))
        .group_by(OrderEmail.order_id)
    ).all()
    return {oid: n for oid, n in rows if n >= 2}
