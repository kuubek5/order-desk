"""Навчання на рішеннях: знайти кандидатів у правила й перевірити їх на минулому.

Без нейромережі: до півтори сотні прийнять на тиждень — для неї замало, а
правило «клієнт Х замість a3 бере a3.5 — 6 за, 0 проти» адмін читає сам.

Звідки дані. Прийняті листи в базі CRM (роботи з одним листом — інакше кількість і
матеріал не приписати). Що «підставила б картка» рахується з `before=` —
станом ДО цієї роботи, щоб правило не підглядало у відповідь. Із журналу рішень
беруться прийняття, де відомо справді ПОКАЗАНЕ; вони перебивають перерахунок.

Доказ правдивий лише наполовину, тому іспит:
  * кандидата шукаємо на старіших 70 % листів;
  * перевіряємо на новіших 30 %, яких пошук не бачив.
Кандидат, що на іспиті помиляється частіше, ніж влучає, не показується.

`mine` важкий (розпізнавання сотень листів) — ЛИШЕ окремим процесом
(`mcp_isolated`), не в потоці CRM: GIL 18.11.26 клав прод.
"""

from __future__ import annotations

import hashlib
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Any, Callable, Iterable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app import decisions_store as store
from app.material_classifier import match_key
from app.models import EmailMessage, Order
from app.services import decision_rules as rules
from app.services import decisions as d
from app.services.stl_units import count_units

DAYS = 90
MIN_FOR = 3
MIN_SHARE = 0.8
TRAIN_SHARE = 0.7
PAUSE_MIN_USES = 5
PAUSE_SHARE = 0.6
_CHUNK = 300


def _rows(db: Session, days: int, now: datetime) -> list[dict[str, Any]]:
    """Прийняті листи від старіших до новіших."""
    from app.services import mail_letter_signals as signals
    from app.services.material_suggest import shades_in

    low = now - timedelta(days=days)
    orders = db.scalars(
        select(Order).where(Order.source_email_id.is_not(None), Order.created_at >= low)
    ).all()
    by_email: dict[int, list[Order]] = defaultdict(list)
    for order in orders:
        by_email[order.source_email_id].append(order)  # type: ignore[index]
    single = {eid: found[0] for eid, found in by_email.items() if len(found) == 1}
    shown = _shown_materials(single)
    out: list[dict[str, Any]] = []
    ids = sorted(single)
    for start in range(0, len(ids), _CHUNK):
        emails = db.scalars(
            select(EmailMessage).where(EmailMessage.id.in_(ids[start: start + _CHUNK]))
            .options(selectinload(EmailMessage.attachments))
        ).all()
        for email in emails:
            order = single[email.id]
            context = signals.material_context(email)
            system = signals.card_material(
                db, email, context, client_name=order.client_name, before=order.created_at
            )
            out.append({
                "at": order.created_at,
                "email": email.id,
                "order": order.id,
                "client": order.client_name or "",
                "system": shown.get(email.id, system) or "",
                "chosen": (order.material_color or "").strip(),
                "quantity": (order.quantity or "").strip(),
                "files": [a.filename or "" for a in email.attachments],
                "shades": shades_in(context),
            })
        # Листи з текстами накопичувались би в пам'яті сесії.
        db.expunge_all()
    out.sort(key=lambda r: (r["at"], r["order"]))
    return out


def _shown_materials(single: dict[int, Any]) -> dict[int, str]:
    """Впевнений матеріал, який оператор СПРАВДІ бачив, — з журналу рішень."""
    if not single:
        return {}
    out: dict[int, str] = {}
    for row in store.query(
        "SELECT email_id, data FROM events WHERE step = ? ORDER BY id", (d.STEP_CARD,)
    ):
        data = row["data"] if isinstance(row["data"], dict) else {}
        if row["email_id"] in single and "впевнений_матеріал" in data:
            out[row["email_id"]] = data["впевнений_матеріал"] or ""
    return out


def split(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    cut = int(len(rows) * TRAIN_SHARE)
    return rows[:cut], rows[cut:]


def _verdict(train: Counter, test: Counter) -> bool:
    total = train["за"] + train["проти"]
    if train["за"] + test["за"] < MIN_FOR or not train["за"]:
        return False
    if train["за"] / total < MIN_SHARE:
        return False
    return test["проти"] <= test["за"]


def _candidate(kind: str, title: str, params: dict[str, Any], train: Counter, test: Counter,
               examples: list[int]) -> dict[str, Any]:
    raw = kind + "|" + "|".join(f"{k}={params[k]}" for k in sorted(params))
    return {
        "key": f"{kind}:{hashlib.sha1(raw.encode()).hexdigest()[:10]}",
        "kind": kind, "title": title, "params": params,
        "train": {"за": train["за"], "проти": train["проти"]},
        "test": {"за": test["за"], "проти": test["проти"]},
        "examples": sorted(set(examples), reverse=True)[:8],
    }


# ── Родина 1: той самий зуб у кількох файлах ───────────────────────────────


def _repeat_cases(rows: Iterable[dict[str, Any]]) -> Iterable[tuple[str, bool, int]]:
    """(типи, чи порахував оператор кожен файл, робота). Неоднозначні — геть:
    різні пари типів в одному листі чи число ні те, ні інше."""
    for row in rows:
        pairs = rules.repeat_types(row["files"])
        if not pairs or len(set(pairs.values())) != 1 or not row["quantity"].isdigit():
            continue
        types = next(iter(pairs.values()))
        units = count_units(row["files"]).units
        extra = sum(t.count("+") for t in pairs.values())
        quantity = int(row["quantity"])
        if quantity == units + extra:
            yield types, True, row["order"]
        elif quantity == units:
            yield types, False, row["order"]


def _mine_repeat(train: list, test: list) -> list[dict[str, Any]]:
    trained: dict[str, Counter] = defaultdict(Counter)
    tested: dict[str, Counter] = defaultdict(Counter)
    examples: dict[str, list[int]] = defaultdict(list)
    for bucket, rows in ((trained, train), (tested, test)):
        for types, counted, order_id in _repeat_cases(rows):
            bucket[types]["за" if counted else "проти"] += 1
            examples[types].append(order_id)
    return [
        _candidate(
            rules.KIND_REPEAT,
            f"Той самий зуб у файлах {types.replace('+', ' + ')} — рахувати кожен файл",
            {"types": types}, trained[types], tested[types], examples[types],
        )
        for types in trained
        if d.OTHER_TYPE not in types.split("+") and _verdict(trained[types], tested[types])
    ]


# ── Родини 2 і 3: матеріал клієнта ─────────────────────────────────────────


def _mine_material(train: list, test: list) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in train:
        client = rules.client_key(row["client"])
        if client and match_key(row["chosen"]):
            groups[(client, match_key(row["system"]) if row["system"] else "")].append(row)
    out = []
    for (client, system), found in groups.items():
        chosen = Counter(match_key(r["chosen"]) for r in found)
        top, count = chosen.most_common(1)[0]
        if top == system or count < 2:
            continue
        spelled = Counter(r["chosen"] for r in found if match_key(r["chosen"]) == top).most_common(1)[0][0]
        if not system:
            # Рахуємо лише листи, де правило справді спрацювало б.
            found = [r for r in found if rules.empty_fits(spelled, r.get("shades") or ())]
            count = sum(1 for r in found if match_key(r["chosen"]) == top)
            if count < 2:
                continue
        trained = Counter({"за": count, "проти": len(found) - count})
        tested: Counter = Counter()
        examples = [r["order"] for r in found if match_key(r["chosen"]) == top]
        for row in test:
            if rules.client_key(row["client"]) != client:
                continue
            if (match_key(row["system"]) if row["system"] else "") != system or not match_key(row["chosen"]):
                continue
            if not system and not rules.empty_fits(spelled, row.get("shades") or ()):
                continue
            same = match_key(row["chosen"]) == top
            tested["за" if same else "проти"] += 1
            if same:
                examples.append(row["order"])
        if not _verdict(trained, tested):
            continue
        name = found[-1]["client"]
        if system:
            was = Counter(r["system"] for r in found).most_common(1)[0][0]
            out.append(_candidate(
                rules.KIND_SWAP, f"{name}: замість «{was}» бере «{spelled}»",
                {"client": client, "from": system, "to": spelled}, trained, tested, examples,
            ))
        else:
            out.append(_candidate(
                rules.KIND_EMPTY, f"{name}: матеріалу не впізнано — майже завжди «{spelled}»",
                {"client": client, "to": spelled}, trained, tested, examples,
            ))
    return out


def mine(db: Session, days: int = DAYS, *, now: Optional[datetime] = None,
         rows: Optional[list[dict[str, Any]]] = None) -> dict[str, Any]:
    now = now or datetime.now()
    rows = _rows(db, days, now) if rows is None else rows
    train, test = split(rows)
    found = _mine_repeat(train, test) + _mine_material(train, test)
    found.sort(key=lambda c: (-(c["train"]["за"] + c["test"]["за"]), c["key"]))
    return {
        "at": now.isoformat(timespec="seconds"),
        "days": days,
        "letters": len(rows),
        "exam_from": test[0]["at"].isoformat(timespec="minutes") if test else None,
        "candidates": found,
    }


# ── Сторож увімкнених правил ───────────────────────────────────────────────


def usage(days: int = 30, *, now: Optional[datetime] = None,
          since: Optional[dict[str, str]] = None) -> dict[str, dict[str, int]]:
    """Як оператори приймали картки, де спрацювало правило: залишили чи виправили.

    Для «тіні» — чи влучило б. Порівнюється останнє показане перед прийняттям.
    `since` — з якого часу рахувати правило (стан змінили — лічба спочатку)."""
    now = now or datetime.now()
    since = since or {}
    low = (now - timedelta(days=days)).isoformat(timespec="seconds")
    cards: dict[int, dict[str, Any]] = {}
    out: dict[str, dict[str, int]] = defaultdict(lambda: {"залишили": 0, "виправили": 0})
    for row in store.query(
        "SELECT at, step, email_id, proposed, chosen, rule, data FROM events "
        "WHERE at >= ? AND step IN (?, ?) AND email_id IS NOT NULL ORDER BY id",
        (low, d.STEP_CARD, d.STEP_ACCEPT),
    ):
        if row["step"] == d.STEP_CARD:
            cards[row["email_id"]] = row
            continue
        card = cards.get(row["email_id"])
        if card is None or not isinstance(row["chosen"], dict):
            continue
        used = _rule_map(card["rule"])
        proposed = card["proposed"] if isinstance(card["proposed"], dict) else {}
        for field_name, key in used.items():
            if card["at"] < since.get(key, ""):
                continue
            kept = _same(field_name, proposed.get(field_name), row["chosen"].get(field_name))
            out[key]["залишили" if kept else "виправили"] += 1
        data = card["data"] if isinstance(card["data"], dict) else {}
        for field_name, shade in (data.get("тінь") or {}).items():
            if card["at"] < since.get(shade["правило"], ""):
                continue
            would = _shadow_value(field_name, shade, proposed)
            kept = _same(field_name, would, row["chosen"].get(field_name))
            out[shade["правило"]]["залишили" if kept else "виправили"] += 1
    return dict(out)


def _rule_map(raw: Any) -> dict[str, str]:
    import json

    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _shadow_value(field_name: str, shade: dict[str, str], proposed: dict[str, Any]) -> str:
    if field_name == "кількість":
        base = str(proposed.get("кількість") or "")
        return str(int(base) + int(shade.get("додало_б") or 0)) if base.isdigit() else ""
    return shade.get("підставило_б") or ""


def _same(field_name: str, left: Any, right: Any) -> bool:
    if field_name == "матеріал":
        return match_key(str(left or "")) == match_key(str(right or ""))
    return str(left or "").strip() == str(right or "").strip()


def pause_bad(db: Session, *, now: Optional[datetime] = None,
              usage_of: Callable[..., dict[str, dict[str, int]]] = usage) -> list[str]:
    """Вимкнути правила, які оператори виправляють. Коміт — за викликачем."""
    paused = []
    entries = rules.load(db)
    used = usage_of(now=now, since={key: entry.get("at") or "" for key, entry in entries.items()})
    for key, entry in entries.items():
        counts = used.get(key)
        if entry.get("status") != rules.STATUS_ON or not counts:
            continue
        # Поріг навмисно грубий: п'ять застосувань і менше 60 % залишено.
        total = counts["залишили"] + counts["виправили"]
        if total >= PAUSE_MIN_USES and counts["залишили"] / total < PAUSE_SHARE:
            rule = entry["rule"]
            rules.save_status(
                db, rule, rules.STATUS_PAUSED, user_id=None, now=now,
                note=f"виправляли {counts['виправили']} з {total}",
            )
            store.put(d.STEP_RULE, d.ACTOR_SYSTEM, rule=key, chosen=rules.STATUS_PAUSED,
                      data={"чому": f"виправляли {counts['виправили']} з {total}"})
            paused.append(key)
    return paused


# ── Де зберігаємо знайдене ─────────────────────────────────────────────────


def save_found(found: dict[str, Any]) -> None:
    import json
    from contextlib import closing

    with closing(store.connect(store.current_path())) as conn, conn:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('candidates', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (json.dumps(found, ensure_ascii=False),),
        )


def load_found() -> dict[str, Any]:
    import json

    rows = store.query("SELECT value FROM meta WHERE key = 'candidates'")
    if not rows or not rows[0]["value"]:
        return {}
    try:
        return json.loads(rows[0]["value"])
    except ValueError:
        return {}


# ── Зведення для екрана й MCP ──────────────────────────────────────────────

_FIELDS = ("клієнт", "матеріал", "кількість")


def accuracy(days: int = 14, *, now: Optional[datetime] = None) -> dict[str, Any]:
    """Показане проти прийнятого, по полях. Лише прийняття, де картку бачили."""
    now = now or datetime.now()
    low = (now - timedelta(days=days)).isoformat(timespec="seconds")
    cards: dict[int, dict[str, Any]] = {}
    counts: dict[str, Counter] = {name: Counter() for name in _FIELDS}
    accepted = 0
    for row in store.query(
        "SELECT step, email_id, proposed, chosen FROM events "
        "WHERE at >= ? AND step IN (?, ?) AND email_id IS NOT NULL ORDER BY id",
        (low, d.STEP_CARD, d.STEP_ACCEPT),
    ):
        if row["step"] == d.STEP_CARD:
            cards[row["email_id"]] = row["proposed"] if isinstance(row["proposed"], dict) else {}
            continue
        card = cards.get(row["email_id"])
        if card is None or not isinstance(row["chosen"], dict):
            continue
        accepted += 1
        for name in _FIELDS:
            was = str(card.get(name) or "").strip()
            if not was:
                counts[name]["empty"] += 1
            else:
                counts[name]["kept" if _same(name, was, row["chosen"].get(name)) else "changed"] += 1
    return {
        "accepted": accepted,
        "fields": [
            {"name": name, "kept": counts[name]["kept"], "changed": counts[name]["changed"],
             "empty": counts[name]["empty"]}
            for name in _FIELDS
        ],
    }


def journal_state(*, now: Optional[datetime] = None) -> dict[str, Any]:
    now = now or datetime.now()
    head = store.query("SELECT COUNT(*) AS n, MIN(at) AS since FROM events")
    week = store.query(
        "SELECT step, COUNT(*) AS n FROM events WHERE at >= ? GROUP BY step ORDER BY n DESC",
        ((now - timedelta(days=7)).isoformat(timespec="seconds"),),
    )
    return {
        "total": head[0]["n"] if head else 0,
        "since": (head[0]["since"] if head else None) or "",
        "week": {row["step"]: row["n"] for row in week},
    }


def public_found(found: dict[str, Any]) -> dict[str, Any]:
    """Кандидати без імен клієнтів — для MCP (відповідь іде в хмару)."""
    def mask(c: dict[str, Any]) -> dict[str, Any]:
        out = {k: c[k] for k in ("key", "kind", "train", "test", "examples")}
        params = c["params"]
        out["params"] = {k: v for k, v in params.items() if k != "client"}
        return out

    return {**{k: v for k, v in found.items() if k != "candidates"},
            "candidates": [mask(c) for c in found.get("candidates", [])]}
