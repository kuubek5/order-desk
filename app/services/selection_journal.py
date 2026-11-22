"""Журнал підбору матеріалу й диска (власник 22.11.26).

«Періодично аналізувати, як я прораховую листи з пошти: які матеріали, які
диски й висоти беру — щоб система фіксувала, а ти робив аналіз і будував
ідеальний алгоритм підбору».

Три частини:
  * `remember_shown` — роути підказки диска (картка листа й Конвеєр)
    запам'ятовують, ЩО САМЕ показали оператору. Перераховувати це під час
    прийняття не можна: розстановка — до 2,5 с, а файли дисків `.blk` Sum3D
    переписує після кожної розстановки, тож через день «що бачив оператор» уже
    не відтворити.
  * `record_accept` — після успішного прийняття листа один рядок
    `SelectionRecord`: здогад листа, що картка підставила б у матеріал, що
    лишив оператор, показана підказка диска. Збій тут прийняття НЕ ламає.
  * `resolve_actual` — добовий прохід (перед чисткою показань верстатів, вони
    живуть 30 днів) дописує фактичний диск і висоту з назви програми на
    верстаті з тим самим Sum3D.

`report` — лише читання, для MCP `kmill_selection_journal`. Тексту листа, імен
клієнтів і назв STL (імена пацієнтів) не віддає.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from collections import Counter
from datetime import datetime, timedelta
from typing import Any, Iterable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.material_classifier import match_key
from app.models import MachineReading, Order, SelectionRecord

logger = logging.getLogger(__name__)

SHOWN_TTL_SECONDS = 6 * 3600
"""Скільки пам'ятати показану підказку: лист відкривають і приймають у межах
зміни; старша — уже не те, що бачив оператор."""
RESOLVE_DAYS = 28
"""Скільки днів шукати програму для рядка (показання верстатів живуть 30)."""
GIVE_UP_DAYS = 14
"""Після стількох днів без програми рядок отримує позначку й більше не шукається."""
SUM3D_WINDOW_DAYS = 7
"""Sum3D ID — лише час доби й повторюється між днями: програму беремо в
межах тижня від прийняття, не раніше за добу до нього."""

_shown: dict[int, tuple[float, dict[str, Any]]] = {}
_shown_lock = threading.Lock()


def _pick(p) -> dict[str, Any]:
    return {"disc": p.name, "height": p.height, "free_pct": p.free_pct,
            "started": p.started, "fit": p.fit, "on_machine": p.on_machine}


def hint_summary(hint) -> dict[str, Any]:
    """Підказка диска коротко — лише те, що бачив оператор і що треба аналізу."""
    return {
        "thickness": round(hint.thickness, 2) if hint.thickness else None,
        "heights": list(hint.heights or []),
        "best": _pick(hint.best) if hint.best else None,
        "alts": [_pick(p) for p in (hint.alts or [])],
        "new_disc": hint.new_disc or None,
        "note": hint.note or None,
        "rejected": hint.rejected,
    }


def batch_key(email_ids: Iterable[int]) -> str:
    raw = ",".join(str(i) for i in sorted(set(email_ids)))
    return "c" + hashlib.sha1(raw.encode()).hexdigest()[:12]


def remember_shown(email_ids: Iterable[int], material: str, hint, *, mode: str = "card",
                   key: Optional[str] = None) -> None:
    """Підказку щойно показали оператору — запам'ятати для `record_accept`."""
    ids = list(email_ids)
    entry = {"material": material, "hint": hint_summary(hint), "mode": mode, "batch_key": key}
    now = time.monotonic()
    with _shown_lock:
        for eid in ids:
            _shown[eid] = (now, entry)
        for stale in [k for k, (t, _e) in _shown.items() if now - t > SHOWN_TTL_SECONDS]:
            del _shown[stale]


def shown_for(email_id: int) -> Optional[dict[str, Any]]:
    with _shown_lock:
        item = _shown.get(email_id)
    if item is None or time.monotonic() - item[0] > SHOWN_TTL_SECONDS:
        return None
    return item[1]


def forget_shown() -> None:
    """Для тестів."""
    with _shown_lock:
        _shown.clear()


def record_accept(db: Session, user, email, order: Optional[Order], *, client_name: str,
                  material_color: str, quantity: str, mode: str = "card") -> Optional[SelectionRecord]:
    """Рядок журналу після УСПІШНОГО прийняття. Виняток — лог, не прийняття."""
    try:
        from app.services.mail_letter_signals import card_material

        suggested = card_material(db, email, client_name=client_name or None)
        shown = shown_for(email.id)
        record = SelectionRecord(
            user_id=getattr(user, "id", None),
            email_id=email.id,
            order_id=getattr(order, "id", None),
            mode=mode,
            batch_key=(shown or {}).get("batch_key") if mode == "conveyor" else None,
            guess=(email.material_color_guess or "")[:120] or None,
            suggested_material=(suggested or "")[:80] or None,
            chosen_material=(material_color or "").strip()[:80],
            quantity=(quantity or "").strip()[:16],
            client_name=(client_name or "").strip()[:160] or None,
            hint_json=json.dumps(shown["hint"], ensure_ascii=False) if shown else None,
            hint_material=(shown["material"] or "")[:80] if shown else None,
        )
        db.add(record)
        db.commit()
        return record
    except Exception:
        logger.exception("Журнал підбору: не вдалося записати прийняття листа %s", getattr(email, "id", None))
        # Відкат теж не має права зламати прийняття (тестова чи напівмертва сесія).
        try:
            db.rollback()
        except Exception:
            logger.exception("Журнал підбору: відкат після збою не вдався")
        return None


def resolve_actual(db: Session, *, now: Optional[datetime] = None) -> int:
    """Дописати фактичний диск і висоту з програми верстата. Повертає, скільки
    рядків отримали диск. Програми ще немає — пробуємо наступного дня, після
    `GIVE_UP_DAYS` — позначка й кінець."""
    from app.services.disc_check import program_disc

    now = now or datetime.now()
    rows = db.scalars(select(SelectionRecord).where(
        SelectionRecord.actual_at.is_(None),
        SelectionRecord.created_at >= now - timedelta(days=RESOLVE_DAYS),
    )).all()
    found = 0
    for rec in rows:
        order = db.get(Order, rec.order_id) if rec.order_id else None
        if order is None:
            rec.actual_note, rec.actual_at = "роботи немає", now
            continue
        tail = (order.sum3d_id or "").strip()[-8:]
        iso = None
        if tail:
            iso = db.scalar(select(MachineReading.iso_name).where(
                MachineReading.sum3d_id == tail,
                MachineReading.iso_name.is_not(None),
                MachineReading.captured_at >= rec.created_at - timedelta(days=1),
                MachineReading.captured_at <= rec.created_at + timedelta(days=SUM3D_WINDOW_DAYS),
            ).order_by(MachineReading.captured_at).limit(1))
        disc = program_disc(iso) if iso else None
        if disc is not None:
            rec.actual_disc, rec.actual_height, rec.actual_at = disc.key[:160], disc.height, now
            rec.actual_note = None
            found += 1
        elif now - rec.created_at > timedelta(days=GIVE_UP_DAYS):
            rec.actual_note = ("програма без диска в назві" if iso
                               else "без Sum3D" if not tail else "програми не знайдено")
            rec.actual_at = now
    db.commit()
    return found


# ── Звіт ────────────────────────────────────────────────────────────────────


def _same(a: Optional[str], b: Optional[str]) -> bool:
    return bool(a) and bool(b) and match_key(a or "") == match_key(b or "")


def _disc_key(name: Optional[str]) -> Optional[str]:
    if not name:
        return None
    return (name.split("_", 1)[1] if "_" in name else name).lower()


def report(db: Session, days: int = 7, limit: int = 40, *, now: Optional[datetime] = None) -> dict[str, Any]:
    """Підсумок журналу за `days`: матеріал (картка проти оператора), диск
    (підказка проти фактичного), висота, у розрізі режиму й матеріалу.
    Без імен клієнтів і тексту листів."""
    now = now or datetime.now()
    rows = db.scalars(select(SelectionRecord).where(
        SelectionRecord.created_at >= now - timedelta(days=days)
    ).order_by(SelectionRecord.id.desc())).all()

    material: Counter[str] = Counter()
    disc: Counter[str] = Counter()
    height: Counter[str] = Counter()
    by_mode: Counter[str] = Counter()
    misses_by_line: Counter[str] = Counter()
    pairs: Counter[str] = Counter()
    examples: list[dict[str, Any]] = []
    for rec in rows:
        by_mode[rec.mode] += 1
        chosen = rec.chosen_material
        if not rec.suggested_material:
            m = "картка не підставила"
        elif _same(rec.suggested_material, chosen):
            m = "збіг"
        else:
            m = "оператор змінив"
            pairs[f"{rec.suggested_material} → {chosen}"] += 1
        material[m] += 1

        hint = json.loads(rec.hint_json) if rec.hint_json else None
        actual = (rec.actual_disc or "").lower() or None
        if hint is None:
            d = "підказку не показано"
        elif rec.actual_at is None:
            d = "фактичного ще немає"
        elif not actual:
            d = f"фактичного немає: {rec.actual_note or '?'}"
        else:
            offered = [_disc_key(p["disc"]) for p in ([hint["best"]] if hint.get("best") else []) + hint.get("alts", [])]
            if offered and offered[0] == actual:
                d = "взяли перший кандидат"
            elif actual in offered:
                d = "взяли інший кандидат"
            elif not offered:
                d = "картка: новий диск, взяли початий/інший"
            else:
                d = "взяли диск поза підказкою"
        disc[d] += 1
        if hint and rec.actual_height:
            height["висота як у підказці" if rec.actual_height in (hint.get("heights") or [])
                   else "висота інша"] += 1
        if d in ("взяли диск поза підказкою", "картка: новий диск, взяли початий/інший") or m == "оператор змінив":
            misses_by_line[match_key(chosen).split(" ", 1)[0] if chosen else "?"] += 1
            if len(examples) < limit:
                examples.append({
                    "запис": rec.id, "лист": rec.email_id, "робота": rec.order_id, "режим": rec.mode,
                    "картка": rec.suggested_material, "оператор": chosen, "матеріал": m, "диск": d,
                    "кандидати": [p["disc"] for p in ([hint["best"]] if hint and hint.get("best") else [])
                                  + (hint.get("alts", []) if hint else [])],
                    "вільно_%": [p["free_pct"] for p in ([hint["best"]] if hint and hint.get("best") else [])
                                 + (hint.get("alts", []) if hint else [])],
                    "висоти_підказки": (hint or {}).get("heights"),
                    "робота_мм": (hint or {}).get("thickness"),
                    "фактичний_диск": rec.actual_disc, "фактична_висота": rec.actual_height,
                })
    total = len(rows)
    return {
        "днів": days,
        "прийнять": total,
        "за_режимом": dict(by_mode),
        "матеріал": dict(material.most_common()),
        "матеріал_точність_%": round(100 * material["збіг"] / total, 1) if total else None,
        "пари_картка_оператор": dict(pairs.most_common(15)),
        "диск": dict(disc.most_common()),
        "висота": dict(height.most_common()),
        "промахи_за_лінією": dict(misses_by_line.most_common(15)),
        "приклади": examples,
        "пояснення": (
            "«картка» — що картка підставила в матеріал при прийнятті; «оператор» — що прийняв. "
            "«диск» — показана підказка проти диска з назви програми на верстаті з тим самим Sum3D "
            "(дописується раз на добу). Журнал веде облік з версії 0.21.159."
        ),
    }
