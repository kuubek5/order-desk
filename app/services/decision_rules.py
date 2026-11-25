"""Правила, яких система навчилась, і що з ними дозволив адмін.

Жодне правило не вмикається саме. Стани (у `AppSetting` `learned_rules`, тобто
в резервній копії CRM, а не в журналі):

  * кандидат — рядка тут немає, його лише показує екран «Навчання»;
  * `shadow` — рахується мовчки: у полі нічого не міняє, у журнал іде «що
    підставило б»;
  * `on`     — підставляє СВОЄ значення в порожнє чи типове поле картки листа;
  * `paused` — вимкнулось само: оператори виправляють його надто часто;
  * `off`    — адмін сказав «ні», кандидатом більше не пропонується.

Параметри правила лежать разом зі станом: перерахунок кандидатів не має права
тихо змінити те, що адмін уже ввімкнув.

Правило лише підказує в картці. Прийняття, запис у таблицю й видачу не чіпає.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Optional

from sqlalchemy.orm import Session

from app.material_classifier import match_key
from app.services.decisions import safe_type
from app.services.stl_units import is_model_file, is_splint_file, teeth_in_filename
from app.settings_store import get_setting, set_setting

logger = logging.getLogger(__name__)

SETTING = "learned_rules"

STATUS_ON, STATUS_SHADOW, STATUS_OFF, STATUS_PAUSED = "on", "shadow", "off", "paused"
STATUS_AUTO = "auto"
"""Лише для порядку дисків (`disc_choice`): вмикає й вимикає доказ, а не людина."""
STATUSES = (STATUS_ON, STATUS_SHADOW, STATUS_OFF, STATUS_PAUSED, STATUS_AUTO)

KIND_REPEAT = "repeat"
"""Той самий зуб у двох файлах певних типів — дві одиниці, а не одна."""
KIND_SWAP = "swap"
"""Картка підставляє клієнтові одне, а він майже завжди бере інше."""
KIND_EMPTY = "empty"
"""Картка матеріалу не впізнала, а клієнт майже завжди бере те саме."""


def client_key(name: Optional[str]) -> str:
    return " ".join((name or "").casefold().split())


def load(db: Session) -> dict[str, dict[str, Any]]:
    raw = get_setting(db, SETTING)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        logger.warning("Навчені правила не читаються — працюємо без них")
        return {}
    return data if isinstance(data, dict) else {}


def save_status(db: Session, rule: dict[str, Any], status: str, *, user_id: Optional[int],
                note: str = "", now: Optional[datetime] = None) -> None:
    """Записати рішення адміна (коміт — за роутом)."""
    if status not in STATUSES:
        raise ValueError(status)
    rules = load(db)
    rules[rule["key"]] = {
        "rule": {k: rule[k] for k in ("key", "kind", "title", "params") if k in rule},
        "status": status,
        "user_id": user_id,
        "at": (now or datetime.now()).isoformat(timespec="seconds"),
        "note": note,
    }
    set_setting(db, SETTING, json.dumps(rules, ensure_ascii=False))


def forget(db: Session, key: str) -> None:
    """Повернути правило в кандидати."""
    rules = load(db)
    if rules.pop(key, None) is not None:
        set_setting(db, SETTING, json.dumps(rules, ensure_ascii=False))


# ── Той самий зуб у кількох файлах ─────────────────────────────────────────


def repeat_types(filenames: Iterable[str]) -> dict[int, str]:
    """Зуб → типи файлів, у яких він стоїть («crown+waxup»), лише для повторів.

    Файл без типу Exocad теж має назву: `—`. Капи зубами не рахуються."""
    seen: dict[int, list[str]] = defaultdict(list)
    for name in filenames:
        if not is_model_file(name) or is_splint_file(name):
            continue
        for tooth in teeth_in_filename(name):
            seen[tooth].append(safe_type(name) or "—")
    return {tooth: "+".join(sorted(types)) for tooth, types in seen.items() if len(types) > 1}


def repeat_extra(filenames: Iterable[str], allowed: set[str]) -> int:
    """Скільки одиниць додати за повтори дозволених пар типів."""
    return sum(types.count("+") for types in repeat_types(filenames).values() if types in allowed)


# ── Застосування в картці ──────────────────────────────────────────────────


@dataclass
class Applied:
    """Що навчені правила кажуть про цю картку."""

    extra_units: int = 0
    repeat_allowed: set[str] = field(default_factory=set)
    material: Optional[str] = None
    material_title: str = ""
    rules: dict[str, str] = field(default_factory=dict)
    """поле → ключ правила, яке СПРАЦЮВАЛО (лише `on`)."""
    shadow: dict[str, dict[str, str]] = field(default_factory=dict)
    """поле → {ключ правила, що підставило б} (лише `shadow`)."""


def empty_fits(target: str, letter_shades: Iterable[str]) -> bool:
    """Чи може «звичне клієнта» лягти в порожнє поле. Лист назвав колір — лише
    той самий єдиний колір; назвав кілька — мовчимо (різні роботи в листі)."""
    from app.services.material_suggest import shades_in

    shades = list(letter_shades)
    return not shades or (len(shades) == 1 and shades_in(target) == shades)


def _material_rule(entry: dict[str, Any], client: str, shown: str, letter_shades: list[str]) -> bool:
    rule = entry.get("rule") or {}
    params = rule.get("params") or {}
    if params.get("client") != client:
        return False
    if rule.get("kind") == KIND_SWAP:
        return bool(shown) and match_key(shown) == params.get("from")
    return (
        rule.get("kind") == KIND_EMPTY and not shown
        and empty_fits(params.get("to") or "", letter_shades)
    )


def apply(db: Session, *, client_name: str, system_material: Optional[str],
          filenames: Iterable[str], letter_shades: Iterable[str] = ()) -> Applied:
    """Без увімкнених правил — порожня відповідь за одне читання налаштування.

    `system_material` — лише те, в чому картка ВПЕВНЕНА; сирий здогад листа
    («Цирконій») для правил порожній, як і в навчанні."""
    out = Applied()
    rules = load(db)
    if not rules:
        return out
    names = list(filenames)
    shades = list(letter_shades)
    client = client_key(client_name)
    types = repeat_types(names)
    for key, entry in rules.items():
        status = entry.get("status")
        if status not in (STATUS_ON, STATUS_SHADOW):
            continue
        rule = entry.get("rule") or {}
        if rule.get("kind") == KIND_REPEAT:
            pair = str((rule.get("params") or {}).get("types") or "")
            if status == STATUS_ON:
                # І без повтору в цьому листі: галочки файлів перераховує браузер.
                out.repeat_allowed.add(pair)
            extra = sum(t.count("+") for t in types.values() if t == pair)
            if not extra:
                continue
            if status == STATUS_ON:
                out.extra_units += extra
                out.rules["кількість"] = key
            else:
                out.shadow["кількість"] = {"правило": key, "додало_б": str(extra)}
        elif client and _material_rule(entry, client, system_material or "", shades):
            target = (rule.get("params") or {}).get("to") or ""
            if status == STATUS_ON and not out.material:
                out.material = target
                out.material_title = rule.get("title") or ""
                out.rules["матеріал"] = key
            elif status == STATUS_SHADOW:
                out.shadow["матеріал"] = {"правило": key, "підставило_б": target}
    return out
