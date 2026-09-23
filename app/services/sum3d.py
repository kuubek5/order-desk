"""Вписання Sum3D ID у роботу — доменне ядро.

Спільне для двох викликачів: одиночної дії в черзі
(`POST /orders/{id}/sum3d-id`) і групового призначення на набір «мої зараз»
(`POST /orders/sum3d-batch`). Один Sum3D-проєкт часто накриває кілька робіт
(кілька клієнтів з пошти, а буває й лаба+пошта разом), тож той самий ID треба
класти в усі їхні рядки — і робити це РІВНО так само, як одиночна дія, інакше
дві гілки розійдуться.

Модуль без Request/Response і без `db.commit()`: транзакцією володіє роут — так
само, як `focus` і `undo`. Роут комітить, а тоді сам вирішує, як писати в
таблицю (переробка чекає на запис, звичайна робота стає в пачку) — тому Google
тут не чіпаємо, лише повертаємо все, що для цього треба.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ActionLog, Order, StatusEvent, User
from app.services.focus import release as release_focus
from app.services.sheet_writeback import order_writes_to_sheet
from app.services.undo import log_action
from app.statuses import STATUS_ACCEPTED, STATUS_CALCULATED, STATUS_NEW


def status_before_sum3d(db: Session, order: Order) -> str | None:
    """Статус, який робота мала ДО того, як оператор вписав Sum3D.

    Береться зі знімка «до» останньої дії Sum3D у журналі — того самого, яким
    користується «Крок назад». Свого поля під це заводити не треба: знімок уже
    пишеться на кожну таку дію.

    Шукається не просто остання дія Sum3D, а остання, у знімку якої статус ЩЕ НЕ
    «прораховано» — тобто та, що роботу туди й підняла. Брати буквально останню
    не можна: оператор часто спершу вписує ID, потім виправляє одруківку в ньому,
    і в знімку ДРУГОЇ дії статус уже «прораховано». Тоді відкат порівнював
    «прораховано» з «прораховано», вирішував, що міняти нічого, і статус
    залишався висіти при порожній таблиці.

    Повертає None, коли доказу немає (роботу імпортували з таблиці, журнал
    підчистили). Тоді статус НЕ чіпаємо: «нове» тут було б здогадкою, а робота,
    прийнята з пошти, мала «прийнято» — і здогадка тихо стерла б цей факт.
    """
    entries = db.execute(
        select(ActionLog)
        .where(ActionLog.order_id == order.id, ActionLog.action_type == "sum3d")
        .order_by(ActionLog.id.desc())
    ).scalars().all()
    for entry in entries:
        if not entry.old_value:
            continue
        try:
            previous = json.loads(entry.old_value).get("status")
        except (ValueError, TypeError):
            continue
        if isinstance(previous, str) and previous and previous != STATUS_CALCULATED:
            return previous
    return None


@dataclass
class Sum3DApplied:
    """Наслідок вписання Sum3D в ОДНУ роботу, ще до коміту.

    Роут бере звідси все, що йому лишається: як писати в таблицю (`rework` каже
    яка гілка, `write_fields`/`erase_fields` — пачка звичайної роботи, `stamp` —
    літера колонки Х переробки), і що показати (`log_entry` для undo/тоста,
    `note` — людський опис дії).
    """

    order: Order
    log_entry: ActionLog
    note: str
    value: str | None
    rework: object | None
    stamp: str | None
    write_fields: set[str] = field(default_factory=set)
    erase_fields: set[str] = field(default_factory=set)


def apply_sum3d(db: Session, order: Order, value: str | None, user: User) -> Sum3DApplied:
    """Вписати (або стерти) Sum3D ID у роботу — уся доменна робота, БЕЗ коміту.

    `value` уже нормалізований роутом: непорожній рядок або None (очищення).
    Логіка дослівно та сама, що була в `set_sum3d_id`, — переробка проти
    звичайної роботи, авто-стемп літери «прораховано», авто-статус, зняття
    мітки «мої зараз», позначка «ще не в таблиці». Групове призначення тому
    поводиться з кожною роботою рівно як одиночне.
    """
    # Вписаний Sum3D ID — це і є момент «я прорахував це в Sum3D», тож портал
    # ставить літеру оператора в колонку «Прорахував» — М для звичайної роботи,
    # Х для переробки. Лише коли є що ставити (літера задана) і значення
    # вписується (ніколи на очищенні); оператор без літери просто отримує
    # записаний Sum3D.
    initial = (user.sheet_initial or "").strip() or None
    stamp = initial if (initial and value) else None
    rework = order.active_rework
    write_fields: set[str] = set()
    erase_fields: set[str] = set()
    # Повний знімок «до», щоб «Скасувати» повернуло ВСЕ, чого дія торкнулась
    # (Sum3D + авто-літера + авто-статус), а не лише клітинку Sum3D.
    if rework is not None:
        before = {"rework.sum3d_id": rework.sum3d_id, "rework.calculated_raw": rework.calculated_raw}
    else:
        before = {"sum3d_id": order.sum3d_id, "calculated_raw": order.calculated_raw, "status": order.status}

    if rework is not None:
        # Переробка — ID, який вписує оператор, це Sum3D повторного прорахунку
        # (колонка W), НЕ ID оригінальної роботи (колонка L лишається як
        # «попередній прорахунок»). Літера йде в «Прорахував» переробки (Х).
        rework.sum3d_id = value
        if stamp:
            rework.calculated_raw = stamp
        elif not value:
            # Очищення ID повертає переробку в «не прораховано», тож літера
            # оператора в колонці Х лишатись не має.
            rework.calculated_raw = None
        after = {"rework.sum3d_id": rework.sum3d_id, "rework.calculated_raw": rework.calculated_raw}
        note = f"Sum3D переробки → {value}" if value else "Sum3D переробки очищено"
        undo_field = "rework.sum3d_id"
    else:
        order.sum3d_id = value
        write_fields = {"sum3d_id"}
        # Те саме для звичайної роботи: стерли ID — стираємо й літеру в колонці М.
        # `erase` обовʼязковий, бо calculated_raw маркерне поле: без нього запис
        # прочитав би живу літеру, ЗБЕРІГ її і ще й повернув у базу.
        if not value and order.calculated_raw:
            order.calculated_raw = None
            write_fields.add("calculated_raw")
            erase_fields.add("calculated_raw")
        # Вписаний ID — це і є момент «прораховано», тож стертий ID мусить
        # забрати статус назад разом із літерою. Повертаємо РІВНО той статус, що
        # був (з журналу), і лише коли робота відтоді нікуди не рушила.
        if not value and order.status == STATUS_CALCULATED:
            previous = status_before_sum3d(db, order)
            if previous and previous != order.status:
                order.status = previous
                db.add(StatusEvent(
                    order_id=order.id, operator_id=user.id,
                    status=previous, actor=user.username,
                ))
        if stamp:
            order.calculated_raw = stamp
            write_fields.add("calculated_raw")
            # Літера в М — маркер «прораховано», тож підводимо статус у базі
            # (ніколи не опускаємо далі), записуючи справжнього оператора.
            if order.status in (STATUS_NEW, STATUS_ACCEPTED):
                order.status = "прораховано"
                db.add(StatusEvent(
                    order_id=order.id, operator_id=user.id,
                    status="прораховано", actor=user.username,
                ))
        after = {"sum3d_id": order.sum3d_id, "calculated_raw": order.calculated_raw, "status": order.status}
        note = f"Sum3D → {value}" if value else "Sum3D очищено"
        undo_field = "sum3d_id"

    log_entry = log_action(
        db, order=order, operator=user, action_type="sum3d", field=undo_field,
        old=json.dumps(before, ensure_ascii=False),
        new=json.dumps(after, ensure_ascii=False), note=note,
    )
    # Мітка «беру зараз» існує рівно для того, щоб не загубити, КУДИ вписувати
    # Sum3D. Вписали — причина відпала, знімається лише МОЯ мітка. При очищенні
    # (value порожнє) мітку не чіпаємо — робота знову «в руках».
    if value:
        release_focus(db, order, user)
    # Позначка «Sum3D ще не в таблиці» ставиться ДО звернення в Google — будь-яке
    # падіння тоді означає лише затримку: рядок показує «ще не в таблиці», синк не
    # стирає значення порожньою колонкою, фоновий повтор допише.
    if rework is None and value and order_writes_to_sheet(order):
        order.sum3d_pending = value

    return Sum3DApplied(
        order=order, log_entry=log_entry, note=note, value=value,
        rework=rework, stamp=stamp,
        write_fields=write_fields, erase_fields=erase_fields,
    )
