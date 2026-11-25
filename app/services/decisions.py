"""Журнал рішень: що система побачила, що запропонувала, що зробив оператор.

Три точки запису, і всі три лише кладуть подію в чергу (`decisions_store.put`):

  * `note_card`   — картка листа показана: саме те, що було НА ЕКРАНІ, а не
    перераховане згодом. Перерахунок після прийняття вже бачить цю роботу у
    «звичному клієнта» й у пам'яті відправника, тобто підглядає у відповідь.
  * `note_disc`   — показана підказка диска;
  * `note_accept` — що оператор лишив у полях.

Усе, що сталося ПОТІМ (відхилили, відклали, переписали кількість у таблиці,
переробили), бачить спостерігач `decision_observer` — він порівнює стан бази з
попереднім, тож новий роут чи синк не може «забути» записати подію.

Журнал не має права зламати роботу: кожна точка обгорнута, збій — рядок у лозі.
Тексту листів, тем, назв файлів, коментарів і адрес тут немає (імена пацієнтів).
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from typing import Any, Iterable, Optional

from app import decisions_store as store
from app.services.stl_units import count_units, exocad_type, is_model_file, is_splint_file, teeth_in_filename

logger = logging.getLogger(__name__)

STEP_LETTER = "лист"
STEP_CARD = "картка"
STEP_DISC = "підказка диска"
STEP_ACCEPT = "прийняття"
STEP_FATE = "доля листа"
STEP_ORDER_NEW = "нова робота"
STEP_FIELD = "поле роботи"
STEP_REWORK = "переробка"
STEP_DISC_ACTUAL = "диск на верстаті"
STEP_RULE = "правило"

ACTOR_SYSTEM = "система"
ACTOR_OPERATOR = "оператор"
ACTOR_SHEET = "таблиця"
ACTOR_UNKNOWN = "невідомо"

# Слова типу з назви Exocad, які можна зберігати. Усе інше стає «інше»: назву
# файлу складає людина, і на місці типу може стояти прізвище.
_TYPE_WORDS = frozenset(
    "abutment anatomic anatomical attachment bar base bitesplint bridge clasp coping core "
    "crown denture eggshell full gingiva implant inlay interim model onlay overlay partial pontic "
    "post primary provisional reduced screw secondary shell slm splint structure table telescope "
    "temp temporary thimble titanium tooth veneer waxup wax".split()
)
OTHER_TYPE = "інше"

ACTOR_TTL_SECONDS = 180
"""Скільки пам'ятати, хто щойно натискав кнопку на листі: спостерігач ходить
раз на хвилину, тож більше вікно приписало б людині чужу зміну."""

_lock = threading.Lock()
_last_card: dict[tuple[int, Optional[int]], str] = {}
_actors: dict[tuple[str, int], tuple[float, int]] = {}
_LAST_CARD_MAX = 2000


def safe_type(filename: str) -> str:
    """Тип роботи з назви Exocad — лише зі словника, без слів людини."""
    raw = exocad_type(filename)
    if not raw:
        return ""
    words = raw.split("_")
    return raw if all(word in _TYPE_WORDS for word in words) else OTHER_TYPE


def file_facts(filenames: Iterable[str]) -> dict[str, Any]:
    """Що система побачила у файлах: зуби й типи без жодної назви."""
    names = list(filenames)
    units = count_units(names)
    models = []
    for name in names:
        if not is_model_file(name):
            continue
        entry: dict[str, Any] = {"зуби": teeth_in_filename(name), "тип": safe_type(name)}
        if is_splint_file(name):
            entry["капа"] = True
        models.append(entry)
    return {
        "одиниць": units.units,
        "повтори": units.repeated,
        "без_номерів": len(units.without_teeth),
        "кап": len(units.splint_jaws),
        "файлів": len(names),
        "моделі": models,
    }


def _user_id(user: Any) -> Optional[int]:
    return getattr(user, "id", None)


def note_card(email: Any, user: Any, context: dict[str, Any], *, sure_material: Optional[str] = None,
              rules: Optional[dict[str, str]] = None, shadow: Optional[dict[str, Any]] = None) -> None:
    """Картка листа показана зі СВОЇМИ значеннями (семена, не введене людиною).

    Картку перемальовують на кожне скачування файлу, тож те саме показане
    двічі поспіль пишеться один раз. `sure_material` — матеріал, у якому картка
    впевнена сама, без навчених правил; `rules` / `shadow` — що вони зробили й
    що зробили б."""
    if not store.enabled():
        return
    try:
        units = context.get("qty_units")
        files = file_facts(a.filename for a in context.get("unclaimed_attachments") or [])
        proposed = {
            "клієнт": (context.get("client_name") or "")[:160],
            "матеріал": (context.get("material_color") or "")[:80],
            "кількість": str(context.get("quantity") or "")[:16],
            "опак": str(context.get("opak_hint") or "")[:16],
        }
        data = {
            "впевнений_матеріал": (sure_material or "")[:80],
            "здогад_матеріалу": bool(email.material_color_guess),
            "матеріал_інший_за_здогад": bool(
                email.material_color_guess and proposed["матеріал"] != (email.material_color_guess or "")[:80]
            ),
            "варіанти": [_cand(c) for c in (context.get("material_cands") or [])][:6],
            "кілька_матеріалів": len(context.get("material_mix") or []),
            "кількість_у_листі": context.get("qty_letter"),
            "кількість_з_файлів": getattr(units, "units", None),
            "кількість_звідки": "файли" if context.get("qty_auto") else ("лист" if proposed["кількість"] else "немає"),
            "розбіжність_лист_файли": bool(context.get("qty_mismatch")),
            "файли": files,
            "постійний_клієнт": bool(context.get("sender_hint")),
        }
        if shadow:
            data["тінь"] = shadow
        stamp = hashlib.sha1(
            json.dumps([proposed, data, rules], ensure_ascii=False, sort_keys=True, default=str).encode()
        ).hexdigest()
        key = (email.id, _user_id(user))
        with _lock:
            if _last_card.get(key) == stamp:
                return
            if len(_last_card) > _LAST_CARD_MAX:
                _last_card.clear()
            _last_card[key] = stamp
        store.put(
            STEP_CARD, ACTOR_SYSTEM, user_id=_user_id(user), email_id=email.id,
            proposed=proposed, data=data,
            rule=json.dumps(rules, ensure_ascii=False) if rules else None,
        )
    except Exception:
        logger.exception("Журнал рішень: картку листа %s не записано", getattr(email, "id", None))


def _cand(cand: Any) -> str:
    return str(getattr(cand, "text", "") or "")[:80]


def note_disc(email_ids: Iterable[int], material: str, hint: dict[str, Any], *, mode: str) -> None:
    if not store.enabled():
        return
    try:
        ids = list(email_ids)
        for email_id in ids:
            store.put(
                STEP_DISC, ACTOR_SYSTEM, email_id=email_id, proposed=hint,
                data={"матеріал": (material or "")[:80], "режим": mode, "листів": len(ids)},
            )
    except Exception:
        logger.exception("Журнал рішень: підказку диска не записано")


def note_accept(user: Any, email: Any, order: Any, *, mode: str, client_name: str, material_color: str,
                quantity: str, opak: str, sum3d_id: str, picked_files: int, partial: bool) -> None:
    """Лист прийнято — рівно те, що було в полях."""
    if not store.enabled():
        return
    try:
        store.put(
            STEP_ACCEPT, ACTOR_OPERATOR, user_id=_user_id(user),
            email_id=email.id, order_id=getattr(order, "id", None),
            chosen={
                "клієнт": (client_name or "").strip()[:160],
                "матеріал": (material_color or "").strip()[:80],
                "кількість": (quantity or "").strip()[:16],
                "опак": (opak or "").strip()[:16],
                "sum3d": (sum3d_id or "").strip()[:40],
            },
            data={"режим": mode, "обрано_файлів": picked_files, "частина_листа": partial},
        )
    except Exception:
        logger.exception("Журнал рішень: прийняття листа %s не записано", getattr(email, "id", None))


def note_actor(entity: str, entity_id: int, user_id: Optional[int]) -> None:
    """Хто щойно діяв на листі чи роботі — для спостерігача (імені він не бачить)."""
    if user_id is None or not store.enabled():
        return
    now = time.monotonic()
    with _lock:
        _actors[(entity, entity_id)] = (now, user_id)
        for stale in [k for k, (t, _u) in _actors.items() if now - t > ACTOR_TTL_SECONDS]:
            del _actors[stale]


def actor_of(entity: str, entity_id: int) -> Optional[int]:
    with _lock:
        item = _actors.get((entity, entity_id))
    if item is None or time.monotonic() - item[0] > ACTOR_TTL_SECONDS:
        return None
    return item[1]


def reset_for_tests() -> None:
    with _lock:
        _last_card.clear()
        _actors.clear()
