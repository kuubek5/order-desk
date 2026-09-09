"""Шлях роботи: одна стрічка від появи в черзі до останньої дії.

Паспорт показував дві окремі стрічки — «Хронологія статусів» і «Журнал дій», —
і жодна з них не відповідала на просте питання «що з цією роботою відбувалось».
Статуси не знали, ХТО прорахував; журнал дій знав, але жив нижче й окремо, і
зіставляти їх доводилось очима. А найперша подія — коли робота взагалі
зʼявилась — не була записана ніде (прохання власника 07.09.26).

Тут вони зводяться в один список за часом. Джерело правди лишається тим самим
(`StatusEvent` і `ActionLog`), нічого не дублюється в базі.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

from app.models import ActionLog, Order

# Дії, які щось кажуть про шлях роботи. Решта (наприклад зміна коментаря для
# CAM) лишається в журналі дій: у стрічці вона була б шумом.
ACTION_LABELS: dict[str, str] = {
    "sum3d": "прораховано в Sum3D",
    "operator": "оператор у таблиці",
    "cam_comment": "коментар для CAM",
    "delete": "видалено з черги",
    "undo": "скасовано дію",
    "redo": "повторено дію",
}
# `status` і `create` навмисно НЕ тут: перший завжди пишеться разом зі
# StatusEvent, другий — разом із появою роботи, і обидві події вже в стрічці.
# Взяти їх сюди означало б показати ту саму подію двома рядками поспіль.


@dataclass(frozen=True)
class PathStep:
    """Одна подія шляху: коли, що і хто."""

    at: datetime | None
    label: str
    who: str
    detail: str = ""
    kind: str = "status"


def _person(operator, actor: str | None) -> str:
    if operator is not None:
        return operator.full_name or operator.username
    if actor:
        # «sync» у базі — це не людина, а синхронізація з таблицею; писати так
        # і треба, інакше виглядає, ніби роботу вів хтось на імʼя sync.
        return "таблиця" if actor == "sync" else actor
    return "система"


def _detail(action: ActionLog) -> str:
    """Людський «що саме», без службових решток.

    `new_value` — це матеріал для скасування, а не текст для людини: у Sum3D
    там JSON-знімок усіх зачеплених полів, у видаленні — слово `archived`.
    Показувати його як є означає вивалити в стрічку рядок, у якому оператор
    шукає очима один номер, або підпис, що нічого не додає до назви події.
    """
    if action.action_type in ("sum3d", "operator", "cam_comment"):
        raw = (action.new_value or "").strip()
        if raw.startswith("{"):
            try:
                data = json.loads(raw)
            except ValueError:
                return action.note or ""
            for key in ("sum3d_id", "rework.sum3d_id", "calculated_raw"):
                value = data.get(key)
                if value:
                    return str(value)
            return ""
        return raw
    # Решта подій уже названа міткою; підпис має сенс лише коли додає щось
    # понад неї — наприклад, ЩО саме скасували.
    note = (action.note or "").strip()
    label = ACTION_LABELS.get(action.action_type, "")
    if note.lower().startswith(label.lower()) and len(note) > len(label):
        note = note[len(label):].lstrip(" :·—-")
    return "" if note.lower() == label.lower() else note

def build_path(order: Order, actions: list[ActionLog]) -> list[PathStep]:
    """Стрічка від появи роботи до останньої дії — найновіше зверху."""
    steps: list[PathStep] = []

    if order.created_at is not None:
        source = {
            "lab": "з таблиці, лабораторія",
            "sheet_client": "з таблиці, клієнт",
            "email": "з пошти",
        }.get(order.source or "", order.source or "")
        steps.append(PathStep(
            at=order.created_at,
            label="зʼявилась у черзі",
            who=source,
            kind="birth",
        ))

    # Зникнення роботи з черги — теж подія її шляху, і питання «коли вона
    # пішла в архів» задають рівно тоді, коли її вже не видно в черзі.
    # Виїзд за вікном 30 днів сюди не пише нічого (він рахується з дати
    # вкладки), тож штамп тут означає РАННЄ прибирання: зникла вкладка,
    # зник рядок, або хтось видалив роботу руками.
    if order.archived_at is not None:
        steps.append(PathStep(
            at=order.archived_at,
            label="прибрано з черги",
            who="",
            kind="archive",
        ))

    for event in order.status_events or []:
        # Перший «нове» від синку — та сама подія, що й поява роботи; двома
        # рядками поспіль вона читається як щось, що сталося двічі.
        if (
            event.status == "нове"
            and order.created_at is not None
            and event.occurred_at is not None
            and abs((event.occurred_at - order.created_at).total_seconds()) < 120
        ):
            continue
        steps.append(PathStep(
            at=event.occurred_at,
            label=event.status,
            who=_person(event.operator, event.actor),
            detail=(event.note or ""),
            kind="status",
        ))

    for action in actions:
        label = ACTION_LABELS.get(action.action_type)
        if label is None:
            continue
        # Sum3D цікавий саме номером: «прораховано» без нього не відповідає
        # на питання «під яким ID шукати в CAM».
        detail = _detail(action)
        steps.append(PathStep(
            at=action.created_at,
            label=label,
            who=_person(action.operator, None),
            detail=detail,
            kind="action",
        ))

    # Найновіше зверху — так само, як було в обох старих стрічках; подія без
    # часу (старі рядки без штампа) не має зникати, тож іде в кінець.
    return sorted(
        steps,
        key=lambda step: (step.at is not None, step.at or datetime.min),
        reverse=True,
    )
