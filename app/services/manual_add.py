"""Ручне додавання робіт: валідація рядків форми, запис у таблицю, Order-и.

Тут немає ні `Request`, ні `RedirectResponse` — лише правила. HTTP-шар
(`app/routers/orders.py`) розбирає форму, тримає гейт паузи синку й чекає
результат запису; сам запис у Google робить передана сюди функція
`write_rows`, бо очікування `.result(timeout=…)` — межа HTTP-рівня, а не
домену (аудит 05.09.26, крок 2.8).

Порядок кроків не випадковий: спершу валідація (нічого не пишемо, поки в
даних помилка), далі захист від подвійного сабміту, потім ОДИН запис у
таблицю, і лише з отриманих номерів рядків народжуються Order-и. Інакше
робота в БД посилалась би на рядок, якого в таблиці немає.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from time import monotonic
from typing import Callable

from sqlalchemy.orm import Session

from app.statuses import STATUS_ACCEPTED, STATUS_NEW
from app.business_day import business_today
from app.material_catalog import (
    ensure_seeded,
    load_alias_rows,
    material_id_by_name,
    resolve_material_id,
)
from app.models import Order, StatusEvent, SyncLog, User
from app.parser import HEADER_ROWS
from app.services.order_dates import parse_sheet_tab
from app.services.undo import log_action

logger = logging.getLogger(__name__)

MAX_MANUAL_ROWS = 30
"""Скільки робіт можна додати одним пушем. Стеля проти випадкового
велетенського сабміту: один запис у таблицю — один суцільний блок рядків."""

# Server-side double-submit guard for manual adds. The submit button disables
# itself client-side, but an F5 re-POST, back-button resubmit, or a browser
# retry still reaches the server and would append the same rows to the sheet
# AGAIN. Keyed by user id; an identical payload within the window is treated
# as the same submit and answered with the normal redirect, writing nothing.
# In-process state is enough: the app runs as a single local process.
MANUAL_ADD_DEDUP_SECONDS = 30.0
_recent_manual_adds: dict[int, tuple[str, float]] = {}


@dataclass(frozen=True)
class ManualBatchResult:
    """Що сталося з партією: або текст помилки для оператора, або створене.

    `error` — готове повідомлення українською; роут лише кладе його у форму,
    не перекладає. `duplicate` — це був повторний сабміт (F5), у таблицю й БД
    не пішло нічого, і для оператора це виглядає як звичайний успіх.
    """

    error: str | None = None
    duplicate: bool = False
    tab: str | None = None
    created_ids: list[int] = field(default_factory=list)


def normalize_work_type(value: str | None) -> str:
    """`client` (типово) або `lab` — усе інше вважаємо клієнтським рядком."""
    return value if value in ("client", "lab") else "client"


def normalize_target_tab(value: str | None) -> str:
    """Вкладка з екрана, якщо це справжній день `дд.мм.рр`, інакше порожньо.

    `target_tab` приходить із форми, тож звіряємо його ТУТ: підроблене
    значення може лише не влучити й відкотитись на типове правило вибору
    вкладки, але ніколи не дійти до шару таблиці сміттям. Роут кличе цю
    функцію ще до валідації рядків, щоб покласти вкладку в посилання
    повернення — інакше після помилки оператор втрачав би обраний день.
    """
    wanted = value.strip() if isinstance(value, str) else ""
    return wanted if wanted and parse_sheet_tab(wanted) is not None else ""


def _at(values: list[str], i: int) -> str:
    return values[i].strip() if i < len(values) else ""


def _collect_works(
    *,
    is_lab: bool,
    client_name: list[str],
    work_order_no: list[str],
    kind: list[str],
    material_color: list[str],
    quantity: list[str],
    sum3d_id: list[str],
    job_code: list[str],
    technician_name: list[str],
) -> tuple[list[dict], str | None]:
    """Паралельні списки з форми → список робіт (або текст помилки).

    Поля приходять окремими списками (по одному значенню на рядок форми), бо
    один сабміт додає кілька робіт. Повністю порожній рядок — це зайвий набір
    полів, який оператор лишив незаповненим: мовчки пропускаємо, а не лаємось.
    """
    row_count = max(
        len(client_name), len(work_order_no), len(kind), len(material_color),
        len(quantity), len(sum3d_id), len(job_code), len(technician_name),
    )
    if row_count == 0:
        return [], "Додайте хоча б одну роботу."
    if row_count > MAX_MANUAL_ROWS:
        return [], f"Забагато рядків за раз (макс. {MAX_MANUAL_ROWS})."

    works: list[dict] = []
    for i in range(row_count):
        row_client = _at(client_name, i)
        row_naryad = _at(work_order_no, i)
        row_kind = _at(kind, i)
        row_material = _at(material_color, i)
        row_qty = _at(quantity, i)
        row_sum3d = _at(sum3d_id, i)
        row_job = _at(job_code, i)
        row_tech = _at(technician_name, i)

        if is_lab:
            if not any((row_naryad, row_kind, row_material, row_job, row_tech, row_sum3d)):
                continue  # empty lab row
            works.append({
                "source": "lab", "work_order_no": row_naryad, "kind": row_kind,
                "e_value": row_kind, "material_color": row_material, "quantity": row_qty,
                "job_code": row_job, "technician_name": row_tech, "sum3d_id": row_sum3d,
            })
        else:
            if not any((row_client, row_material, row_qty, row_job, row_tech, row_sum3d)):
                continue  # empty client row
            if not row_client:
                return [], f"Рядок {i + 1}: вкажіть імʼя клієнта."
            if not row_material:
                return [], f"Рядок {i + 1}: вкажіть матеріал / колір."
            works.append({
                "source": "sheet_client", "client_name": row_client,
                "e_value": row_client, "material_color": row_material, "quantity": row_qty,
                "job_code": row_job, "technician_name": row_tech, "sum3d_id": row_sum3d,
            })

    if not works:
        return [], "Заповніть хоча б одну роботу."
    return works, None


def _is_duplicate_submit(user_id: int, fingerprint: str, now_ts: float) -> bool:
    """Та сама партія від того самого оператора у вікні = повтор, не намір."""
    last = _recent_manual_adds.get(user_id)
    return (
        last is not None
        and last[0] == fingerprint
        and (now_ts - last[1]) < MANUAL_ADD_DEDUP_SECONDS
    )


def _remember_submit(user_id: int, fingerprint: str, now_ts: float) -> None:
    """Запамʼятати вдалий сабміт і прибрати протухлі, щоб словник не ріс."""
    _recent_manual_adds[user_id] = (fingerprint, now_ts)
    for uid, (_, ts) in list(_recent_manual_adds.items()):
        if now_ts - ts >= MANUAL_ADD_DEDUP_SECONDS:
            _recent_manual_adds.pop(uid, None)


def create_manual_batch(
    db: Session,
    *,
    user: User,
    work_type: str,
    target_tab: str,
    client_name: list[str],
    work_order_no: list[str],
    kind: list[str],
    material_color: list[str],
    quantity: list[str],
    sum3d_id: list[str],
    job_code: list[str],
    technician_name: list[str],
    write_rows: Callable[..., object],
) -> ManualBatchResult:
    """Додати одну АБО кілька робіт руками й віддзеркалити їх у таблицю.

    Два види партії:

      * client (типово) — наряд-less клієнтські рядки (імʼя клієнта у «Вид
        роботи», залиті синім як очікування лабораторії), source="sheet_client";
      * lab — звичайні внутрішні роботи: наряд у «Номер наряду», вид у «Вид
        роботи», без заливки, source="lab".

    Уся партія йде ОДНИМ суцільним блоком за один похід у таблицю — цикл
    походів через проксі лабораторії коштував би ~40 с на кожен рядок. Кожна
    робота привʼязується до свого `row_number`, щоб наступний синк оновлював
    її на місці, а не плодив дубль.

    `write_rows(day, works, *, paint_blue, placement, target_tab)` дає роут:
    там живе пул write-back і очікування результату. Повертає або `(вкладка,
    номери рядків)`, або рядок-відмову, або None, якщо датованої вкладки нема.
    """
    is_lab = normalize_work_type(work_type) == "lab"

    works, error = _collect_works(
        is_lab=is_lab,
        client_name=client_name, work_order_no=work_order_no, kind=kind,
        material_color=material_color, quantity=quantity, sum3d_id=sum3d_id,
        job_code=job_code, technician_name=technician_name,
    )
    if error is not None:
        return ManualBatchResult(error=error)

    # Захист від подвійного сабміту стоїть ПЕРЕД записом: інакше F5 дописав би
    # у таблицю ту саму партію вдруге, і оператор побачив би дублі рядків.
    fingerprint = repr((("lab" if is_lab else "client"), works))
    now_ts = monotonic()
    if _is_duplicate_submit(user.id, fingerprint, now_ts):
        return ManualBatchResult(duplicate=True)

    try:
        result = write_rows(
            business_today(), works,
            paint_blue=(not is_lab),
            placement=("lab" if is_lab else "client"),
            target_tab=target_tab,
        )
    except Exception as exc:  # noqa: BLE001 — surface any sheet failure to the operator
        logger.exception("Manual order sheet write failed")
        return ManualBatchResult(error=f"Не вдалося записати в таблицю: {exc}")
    if isinstance(result, str):
        # Пул відмовився писати (пауза) — сюди практично не доходить, бо гейт
        # у роуті вище вже відповів операторові; лишаємо як чесний шлях.
        return ManualBatchResult(error=result)
    if result is None:
        return ManualBatchResult(
            error="У таблиці немає жодної датованої вкладки — створіть день у таблиці спершу."
        )
    tab, note_rows = result

    ensure_seeded(db)
    alias_rows = load_alias_rows(db)
    name_by_id = material_id_by_name(db)
    created_ids: list[int] = []
    for work, note_row in zip(works, note_rows):
        if work["source"] == "lab":
            order = Order(
                source="lab", sheet_tab=tab, row_number=note_row - HEADER_ROWS,
                work_order_no=work["work_order_no"] or None, kind=work["kind"] or None,
                material_color=work["material_color"] or None, quantity=work["quantity"] or None,
                job_code=work["job_code"] or None, technician_name=work["technician_name"] or None,
                sum3d_id=work["sum3d_id"] or None,
                status=STATUS_ACCEPTED if work["sum3d_id"] else STATUS_NEW,
            )
        else:
            order = Order(
                source="sheet_client", sheet_tab=tab, row_number=note_row - HEADER_ROWS,
                client_name=work["client_name"], material_color=work["material_color"] or None,
                quantity=work["quantity"] or None, job_code=work["job_code"] or None,
                technician_name=work["technician_name"] or None,
                sum3d_id=work["sum3d_id"] or None, status="нове",
            )
        order.material_id = resolve_material_id(order.material_color, alias_rows, name_by_id)
        db.add(order)
        db.flush()
        db.add(StatusEvent(order_id=order.id, operator_id=user.id, status=order.status, actor=user.username))
        # Adding a work by hand IS an operator action, so it belongs in the
        # journal and the «Останні дії» popup — otherwise a work the operator
        # just created is the one thing they cannot jump back to. Logged as
        # "create": listed and locatable, but deliberately NOT undoable — «Крок
        # назад» is a quick low-friction click and must never silently delete a
        # row from the shared sheet. Removing a work stays the explicit delete
        # button, which asks first.
        log_action(
            db, order=order, operator=user, action_type="create",
            note=f"додано вручну: {order.work_order_no or order.client_name or ('#' + str(order.id))}",
        )
        created_ids.append(order.id)

    db.add(
        SyncLog(
            direction="db_to_sheet", sheet_tab=tab, status="ok",
            message=f"manual {'lab' if is_lab else 'client'} ×{len(created_ids)}: рядки {note_rows}",
        )
    )
    db.commit()

    # Памʼятаємо вдалий сабміт лише ПІСЛЯ коміту: якби запис у БД впав, повтор
    # мав би пройти, а не мовчки «зникнути» як дубль.
    _remember_submit(user.id, fingerprint, now_ts)

    return ManualBatchResult(tab=tab, created_ids=created_ids)
