"""Резервна копія мусить охоплювати ВСЮ базу.

Чому тест, а не уважність. До 07.09.26 `_TABLE_MODELS` перелічував 13 таблиць
із 27, і список ніхто не поповнював, коли зʼявлялась нова: копія «для переїзду
на інший ПК» мовчки лишала позаду пічки, верстати (разом із зашифрованими
паролями VNC і токенами агента), матеріали з синонімами, фільтри пошти,
памʼять відправників, збережені вигляди черги, звернення, журнал дій і ВЕСЬ
Виробіток — тобто зарплатні цифри. Дізнатись про це можна було лише вже на
новому компʼютері.

Тепер нова таблиця без рядка в `_TABLE_MODELS` валить цей тест. Якщо таблицю
свідомо не бекапимо — вписати її в `DELIBERATELY_NOT_BACKED_UP` з причиною.
"""

from __future__ import annotations

import app.models  # noqa: F401 — реєструє всі таблиці в metadata
from app.backup import _TABLE_MODELS
from app.db import Base
from app.models import AppSetting

# Таблиці, які в копію не йдуть СВІДОМО. Порожньо — і це правильний стан:
# все, що описане в моделях, або переїжджає з копією, або має тут причину.
DELIBERATELY_NOT_BACKED_UP: dict[str, str] = {}

# `app_settings` бекапиться окремою гілкою (значення розшифровуються ключем
# цього ПК і перешифровуються під пароль копії), тому в `_TABLE_MODELS` його
# немає й бути не повинно.
_HANDLED_SEPARATELY = {AppSetting.__tablename__}


def test_every_table_is_either_backed_up_or_explicitly_excluded():
    all_tables = set(Base.metadata.tables)
    covered = {model.__tablename__ for model in _TABLE_MODELS} | _HANDLED_SEPARATELY
    forgotten = sorted(all_tables - covered - set(DELIBERATELY_NOT_BACKED_UP))
    assert not forgotten, (
        "Ці таблиці не потраплять у резервну копію: "
        + ", ".join(forgotten)
        + ". Додай модель у app/backup.py::_TABLE_MODELS (батьки перед дітьми) "
        "або впиши таблицю в DELIBERATELY_NOT_BACKED_UP із причиною."
    )


def test_exclusion_list_has_no_stale_entries():
    """Виняток для таблиці, якої вже немає, мовчки послаблює сторожа."""
    stale = sorted(set(DELIBERATELY_NOT_BACKED_UP) - set(Base.metadata.tables))
    assert not stale, f"У списку винятків неіснуючі таблиці: {stale}"


def test_app_settings_is_not_listed_twice():
    assert AppSetting not in _TABLE_MODELS


def test_no_duplicates_in_the_table_list():
    names = [model.__tablename__ for model in _TABLE_MODELS]
    assert len(names) == len(set(names)), "таблиця перелічена двічі — вставка задублює рядки"


def test_children_come_after_their_parents():
    """Порядок вставки: батько раніше за дитину.

    Єдиний свідомий виняток — взаємне посилання `orders` ↔ `email_messages`
    (обидві колонки nullable): роботи вставляються першими, лист прилітає з
    уже заповненим `order_id`.
    """
    order = {model.__tablename__: i for i, model in enumerate(_TABLE_MODELS)}
    known_cycle = {("orders", "email_messages")}
    problems = []
    for name, index in order.items():
        table = Base.metadata.tables[name]
        for fk in table.foreign_keys:
            parent = fk.column.table.name
            if parent == name or parent in _HANDLED_SEPARATELY:
                continue
            if (name, parent) in known_cycle:
                continue
            parent_index = order.get(parent)
            if parent_index is None or parent_index < index:
                continue
            problems.append(f"{name} стоїть перед своїм батьком {parent}")
    assert not problems, "; ".join(problems)
