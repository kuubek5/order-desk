"""Сторож матеріалу: замовлене ≠ те, що фрезерується (власник 18.11.26).

MATERIAL_GUARD_BRIEF.md. Червона картка в черзі, коли диск програми (з її
назви: матеріал, лінія, колір) не збігається з тим, що замовлено в роботі з
тим самим Sum3D ID. Другий шар — «лист ↔ вписане оператором»: клієнт написав
одне, оператор при прийнятті вписав інше (окремий перемикач).

**Якомога раніше.** Програми беремо там, де вони з'являються НАЙРАНІШЕ: кеш
слідів `program_tracks` (збирач відкриває нову програму в теці ще до того, як
верстат її забере) плюс покази верстатів як запас. Вікно — `RECENT_HOURS`.

**Хибна тривога гірша за мовчання** (звірка 7 днів 18.11.26: 20 розбіжностей,
з них 9 — висота в межах похибки усадки, 1 — збіг Sum3D ID з роботою 5 днів
тому). Тому: висоти тут немає зовсім; роботу шукаємо лише в день програми ±1;
порівнюємо лише те, що розібрано з ОБОХ боків (`disc_check.compare`).

**Погоджено** (`MaterialGuardAck`): для роботи (клієнт передумав по телефону) або
для ВСЬОГО диска (невірно названий диск — власник: Monolith «A4» x05). Лише
читання бази й кешу слідів, крім запису «Погоджено».
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import EmailMessage, MaterialGuardAck, Order, User
from app.services import disc_check, feature_access
from app.services.order_dates import order_date

RECENT_HOURS = 24
CACHE_SECONDS = 20.0
LAYER_DISC = "disc"
LAYER_LETTER = "letter"
VIEWS = ("", "card")  # "" — банер над чергою (A), "card" — картка-порівняння (B)

_cache_lock = threading.Lock()
_cache: tuple[float, list["Alert"]] = (0.0, [])


@dataclass
class Alert:
    layer: str
    order_id: int
    client: str
    quantity: str
    ordered: str                      # як записано в роботі
    ordered_label: str                # «Emotions A3»
    actual: str                       # диск («16-Z-Nature-A3-x12») або що в листі
    actual_label: str
    reasons: list[str] = field(default_factory=list)
    disc_key: str = ""
    program: str = ""
    where: str = ""                   # верстат або «тека програм»
    seen_at: Optional[datetime] = None

    @property
    def key(self) -> str:
        return f"{self.layer}:{self.order_id}:{self.disc_key}"

    @property
    def title(self) -> str:
        return "Не той диск" if self.layer == LAYER_DISC else "Не той матеріал у листі"


# ── Що замовлено / що на диску ──────────────────────────────────────────────


def _label(text: Optional[str]) -> str:
    from app.services.disc_suggest import want_of

    want = want_of(text)
    label = want.label()
    return label or (text or "").strip()


def _line(text: Optional[str]) -> Optional[str]:
    from app.services.disc_suggest import want_of

    return want_of(text).line


def disc_reasons(disc: disc_check.ProgramDisc, order: Order) -> list[str]:
    """Розбіжності матеріалу, лінії й кольору (без висоти — див. модуль)."""
    from app.services.disc_suggest import LINES, lines_conflict

    verdict = disc_check.compare(disc, order)
    reasons = [r for r in verdict.issues if not r.startswith("висота")]
    wanted, got = _line(order.material_color), _line(disc.head)
    if wanted and got and lines_conflict(wanted, got):
        reasons.insert(0, f"лінія: замовлено {LINES[wanted][0]}, диск {LINES[got][0]}")
    return reasons


def orders_on_day(db: Session, disc: disc_check.ProgramDisc) -> list[Order]:
    """Роботи з тим самим Sum3D ID у день програми ±1. Sum3D ID — лише час
    доби (CLAUDE.md §14): ширше вікно ловило б чужу роботу (8241, 18.11.26)."""
    if disc.made_on is None:
        return []
    low, high = disc.made_on - timedelta(days=1), disc.made_on + timedelta(days=1)
    return [o for o in disc_check.orders_for(db, disc) if low <= order_date(o) <= high]


# ── Звідки програми ─────────────────────────────────────────────────────────


def _machine_names(db: Session) -> dict[str, str]:
    from app.models import Machine

    names: dict[str, str] = {}
    for host, port, name in db.execute(select(Machine.host, Machine.port, Machine.name)).all():
        names[f"{host}-{port}"] = name
        names.setdefault(host, name)
    return names


def recent_programs(db: Session, now: datetime, *, tracks_dir: Optional[Path] = None) -> dict[str, tuple[str, datetime]]:
    """Назва програми → (де, коли вперше бачили) за `RECENT_HOURS`.

    Кеш слідів — найраніше (програма в теці ще до верстата); покази верстатів —
    запас (програма, якої збирач не бачив)."""
    since = now - timedelta(hours=RECENT_HOURS)
    out: dict[str, tuple[str, datetime]] = {}
    folder_names: dict[str, str] = {}
    try:
        from app.services import program_tracks

        tracks_dir = tracks_dir or program_tracks.cache_dir()
        folder_names = {f.path.rstrip("\\/").lower(): f.machine for f in program_tracks.known_folders(db)}
    except Exception:  # noqa: BLE001 — без тек сторож працює на показах верстатів
        import logging

        logging.getLogger(__name__).debug("Сторож матеріалу: теки програм недоступні", exc_info=True)
    if tracks_dir is not None and tracks_dir.is_dir():
        cutoff = since.timestamp()
        for path in tracks_dir.glob("*.json"):
            try:
                stat = path.stat()
                if stat.st_mtime < cutoff:
                    continue
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            name = str(raw.get("name") or "").rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
            if not name:
                continue
            source = str(raw.get("source") or "").rstrip("\\/").lower()
            where = folder_names.get(source) or "тека програм"
            out.setdefault(name, (where, datetime.fromtimestamp(stat.st_mtime)))
    names = _machine_names(db)
    for host, iso, first in disc_check.programs_seen(db, since):
        if iso not in out:
            out[iso] = (names.get(host, host), first)
        elif names.get(host) and out[iso][0] == "тека програм":
            out[iso] = (names[host], out[iso][1])
    return out


# ── Шари ────────────────────────────────────────────────────────────────────


def disc_alerts(db: Session, now: datetime, **kw: Any) -> list[Alert]:
    alerts: list[Alert] = []
    for program, (where, seen) in recent_programs(db, now, **kw).items():
        disc = disc_check.program_disc(program)
        if disc is None:
            continue
        for order in orders_on_day(db, disc):
            reasons = disc_reasons(disc, order)
            if not reasons:
                continue
            alerts.append(Alert(
                layer=LAYER_DISC, order_id=order.id, client=order.client_name or order.work_order_no or "",
                quantity=(order.quantity or "").strip(), ordered=order.material_color or "",
                ordered_label=_label(order.material_color), actual=disc.head,
                actual_label=_label(disc.head), reasons=reasons, disc_key=disc.key,
                program=program, where=where, seen_at=seen,
            ))
    return alerts


def letter_alerts(db: Session, now: datetime) -> list[Alert]:
    """Лист ↔ вписане оператором: свіжі поштові роботи, де нинішнє правило
    картки впевнено бачить у листі ІНШИЙ матеріал чи колір (`mail_learn`)."""
    from app.services import mail_learn

    since = now - timedelta(hours=RECENT_HOURS)
    orders = db.scalars(
        select(Order).where(Order.source_email_id.is_not(None), Order.created_at >= since,
                            Order.archived_at.is_(None))
    ).all()
    alerts: list[Alert] = []
    for order in orders:
        if not (order.material_color or "").strip():
            continue
        email = db.get(EmailMessage, order.source_email_id)
        if email is None:
            continue
        row = mail_learn._judge(db, email, order, None)
        if row["висновок"] not in (mail_learn.OTHER_MATERIAL, mail_learn.OTHER_SHADE) or not row["система"]:
            continue
        reason = ("матеріал" if row["висновок"] == mail_learn.OTHER_MATERIAL else "колір")
        alerts.append(Alert(
            layer=LAYER_LETTER, order_id=order.id, client=order.client_name or "",
            quantity=(order.quantity or "").strip(), ordered=order.material_color or "",
            ordered_label=_label(order.material_color), actual=row["система"],
            actual_label=_label(row["система"]),
            reasons=[f"{reason}: у листі {row['система']}, вписано {order.material_color}"],
            seen_at=order.created_at,
        ))
    return alerts


def _acked(db: Session) -> tuple[set[tuple[str, int]], set[tuple[str, str]]]:
    orders: set[tuple[str, int]] = set()
    discs: set[tuple[str, str]] = set()
    for ack in db.scalars(select(MaterialGuardAck)).all():
        if ack.order_id is not None:
            orders.add((ack.layer, ack.order_id))
        elif ack.disc_key:
            discs.add((ack.layer, ack.disc_key))
    return orders, discs


def all_alerts(db: Session, now: Optional[datetime] = None, *, use_cache: bool = True) -> list[Alert]:
    """Усі непогоджені картки обох шарів, найновіші першими. Кеш 20 с: черга
    политься в кожного оператора, а обчислення однакове для всіх."""
    global _cache
    if use_cache:
        with _cache_lock:
            stamp, cached = _cache
            if time.monotonic() - stamp < CACHE_SECONDS:
                return list(cached)
    now = now or datetime.now()
    found = disc_alerts(db, now) + letter_alerts(db, now)
    by_order, by_disc = _acked(db)
    seen: set[str] = set()
    out: list[Alert] = []
    for alert in sorted(found, key=lambda a: a.seen_at or datetime.min, reverse=True):
        if alert.key in seen or (alert.layer, alert.order_id) in by_order:
            continue
        if alert.disc_key and (alert.layer, alert.disc_key) in by_disc:
            continue
        seen.add(alert.key)
        out.append(alert)
    with _cache_lock:
        _cache = (time.monotonic(), out)
    return list(out)


def invalidate() -> None:
    global _cache
    with _cache_lock:
        _cache = (0.0, [])


# Доступ, яким керує адмін (власник 20.11.26) — спільне правило
# `feature_access` (так само живе повідомлення «піч без зв'язку»).
FEATURE = feature_access.BY_KEY["material_guard"]
ACCESS_CHOICE = feature_access.CHOICE
ACCESS_DENY = feature_access.DENY
ACCESS_FORCE = feature_access.FORCE


def access_of(user: Optional[User]) -> str:
    return feature_access.access_of(user, FEATURE)


def disc_layer_on(user: Optional[User]) -> bool:
    """Основна перевірка (диск програми ↔ замовлене) для цього оператора."""
    return feature_access.is_on(user, FEATURE)


def letter_layer_on(user: Optional[User]) -> bool:
    """«Лист ↔ вписане»: заборона адміна її теж вимикає; «завжди» — ні, вона
    лишається окремим вибором оператора."""
    if user is None or access_of(user) == ACCESS_DENY:
        return False
    return bool(user.material_guard_letter)


def alerts_for(db: Session, user: Optional[User]) -> list[Alert]:
    """Картки, які бачить цей оператор (його перемикачі й доступ від адміна)."""
    layers = {LAYER_DISC} if disc_layer_on(user) else set()
    if letter_layer_on(user):
        layers.add(LAYER_LETTER)
    if not layers:
        return []
    return [a for a in all_alerts(db) if a.layer in layers]


def acknowledge(db: Session, user: Optional[User], *, layer: str, order_id: Optional[int],
                disc_key: Optional[str], whole_disc: bool) -> None:
    """«Погоджено»: робота або весь диск. Слід — у журналі дій роботи."""
    from app.services.undo import log_action

    if layer not in (LAYER_DISC, LAYER_LETTER):
        raise ValueError("layer")
    key = (disc_key or "").strip()[:160] or None
    if whole_disc and not key:
        raise ValueError("disc_key")
    db.add(MaterialGuardAck(order_id=None if whole_disc else order_id, disc_key=key, layer=layer,
                            operator_id=getattr(user, "id", None), created_at=datetime.now()))
    order = db.get(Order, order_id) if order_id else None
    if order is not None:
        what = f"увесь диск {key}" if whole_disc else "цю роботу"
        log_action(db, order=order, operator=user, action_type="material_guard_ack",
                   note=f"Сторож матеріалу: погоджено {what}")
    db.commit()
    invalidate()


__all__ = ["Alert", "all_alerts", "alerts_for", "acknowledge", "invalidate", "disc_reasons",
           "orders_on_day", "recent_programs", "VIEWS", "LAYER_DISC", "LAYER_LETTER"]
