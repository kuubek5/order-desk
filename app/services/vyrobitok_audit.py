"""Звірка «Виробітку»: що на екрані, що дають живі рядки за старими й новими правилами.

Потрібна, щоб показати, що змінюють правила підрахунку від 22.11.26 (`vyrobitok_rows`,
власник 22.11.26): це гроші, тож спершу «було → стало» по кожному дню, і лише
потім перерахунок закритих днів. Лише читання: табель рахується з `persist=False`, у базу нічого
не пишеться.

Три числа на колонку дня:
  * **екран** — те, що показує «Виробіток» зараз (закритий день — зі знімка,
    відкритий — з живих рядків; ручна правка оператора — поверх);
  * **живі_старі** — сума живих рядків за старими правилами (відрізняється від
    екрана, коли день закрився до правок у таблиці чи до нового синоніма
    матеріалу);
  * **нові** — сума живих рядків за новими правилами. Це число стане на екрані
    для закритого дня після «Перерахувати закриті дні місяця».

Імен клієнтів не віддає — лише ініціали; назв файлів тут немає взагалі.
"""

from __future__ import annotations

import calendar
from collections import Counter, defaultdict
from datetime import date
from typing import Any

from sqlalchemy.orm import Session

from app.services import vyrobitok, vyrobitok_rows


def _initials(name: str) -> str:
    parts = [p for p in (name or "").replace("_", " ").split() if p]
    return ".".join(p[0].upper() for p in parts[:3]) + "." if parts else ""


def _who(v: vyrobitok_rows.Verdict) -> str:
    return f"наряд {v.work_order_no}" if v.work_order_no else f"клієнт {_initials(v.client_name) or '—'}"


def _row(v: vyrobitok_rows.Verdict, why: str) -> dict[str, Any]:
    return {
        "день": v.day.strftime("%d.%m"),
        "звідки": "лаб" if v.bucket == "lab" else "пошта",
        "рядок": v.row_number,
        "хто": _who(v),
        "матеріал": v.material_color,
        "кількість": v.quantity,
        "робота": v.order_id,
        "чому": why,
    }


def _sums(verdicts: list[vyrobitok_rows.Verdict]) -> dict[tuple[date, str], int]:
    out: dict[tuple[date, str], int] = defaultdict(int)
    for v in verdicts:
        if v.col_key is not None:
            out[(v.day, v.col_key)] += v.units
    return out


def audit(db: Session, year: int, month: int, limit: int = 60) -> dict[str, Any]:
    first = date(year, month, 1)
    last = date(year, month, calendar.monthrange(year, month)[1])
    old, _ = vyrobitok_rows.judge(db, first, last, v2=False)
    new, _ = vyrobitok_rows.judge(db, first, last, v2=True)
    old_sum, new_sum = _sums(old), _sums(new)
    grid = vyrobitok.compute_month(db, year, month, persist=False)
    cols = sorted(vyrobitok.ORDER_COLS)

    days: list[dict[str, Any]] = []
    totals: dict[str, Counter[str]] = {key: Counter() for key in ("екран", "живі_старі", "нові")}
    for row in grid.rows:
        d = date.fromisoformat(row["date"])
        differing: dict[str, dict[str, Any]] = {}
        for col in cols:
            cell = row["cells"][col]
            numbers = {"екран": cell["num"], "живі_старі": old_sum.get((d, col), 0), "нові": new_sum.get((d, col), 0)}
            for name, value in numbers.items():
                totals[name][col] += value
            if len(set(numbers.values())) > 1:
                if cell["edited"]:
                    numbers["вписав_оператор"] = True
                differing[col] = numbers
        if differing:
            days.append({"день": d.strftime("%d.%m"), "закрито": bool(row["frozen"]), "колонки": differing})

    frozen_days = {date.fromisoformat(row["date"]) for row in grid.rows if row["frozen"]}
    vanished = vyrobitok_rows.vanished_rows(db, first, last, v2=grid.rules_v2)
    untouched = [
        {"день": d.strftime("%d.%m"), "зниклих_рядків": n}
        for d, n in sorted(vanished.items()) if d in frozen_days
    ]

    old_by_id = {v.order_id: v for v in old}
    newly_skipped = [
        _row(v, v.reason_label) for v in new
        if not v.counted and old_by_id[v.order_id].counted and old_by_id[v.order_id].units
    ]
    silent = [
        _row(v, v.reason_label) for v in new
        if v.reason in (vyrobitok_rows.REASON_NO_MATERIAL, vyrobitok_rows.REASON_NO_QUANTITY)
    ]
    live_prefix = vyrobitok_rows.NOTE_TWIN_LIVE.split("{")[0]
    moved_prefix = vyrobitok_rows.NOTE_TWIN_MOVED.split("{")[0]
    twins = [_row(v, v.note) for v in new if v.counted and v.note.startswith(live_prefix)]
    moved = [_row(v, v.note) for v in new if v.counted and v.note.startswith(moved_prefix)]
    mail_reworks = [_row(v, v.note) for v in new if v.note == vyrobitok_rows.NOTE_MAIL_REWORK]

    def by_reason(verdicts: list[vyrobitok_rows.Verdict]) -> dict[str, dict[str, int]]:
        rows: Counter = Counter()
        units: Counter = Counter()
        for v in verdicts:
            if v.reason is None:
                continue
            rows[v.reason] += 1
            units[v.reason] += vyrobitok_rows.parse_int_safe(v.quantity) or 0
        return {reason: {"рядків": rows[reason], "одиниць_у_рядках": units[reason]} for reason in sorted(rows)}

    return {
        "місяць": f"{month:02d}.{year}",
        "нові_правила_ввімкнено": grid.rules_v2,
        "разом": {name: dict(sorted(counter.items())) for name, counter in totals.items()},
        "дні_з_різницею": days,
        "не_пораховано_за_причиною": {"старі": by_reason(old), "нові": by_reason(new)},
        "нові_правила_знімуть": {"рядків": len(newly_skipped), "приклади": newly_skipped[:limit]},
        "мовчазні_нулі": {"рядків": len(silent), "приклади": silent[:limit]},
        "закриті_дні_яких_перерахунок_не_чіпатиме": untouched,
        "пораховано_в_двох_днях_той_самий_прорахунок": {
            "рядків": len(twins),
            "одиниць": sum(vyrobitok_rows.parse_int_safe(item["кількість"]) or 0 for item in twins),
            "приклади": twins[:limit],
        },
        "прорахований_рядок_перенесено": {"рядків": len(moved), "приклади": moved[:limit]},
        "переробки_з_листа_рахуються": {"рядків": len(mail_reworks), "приклади": mail_reworks[:limit]},
        "пояснення": (
            "екран — що показує «Виробіток» зараз (закритий день зі знімка, правка оператора поверх); "
            "живі_старі — сума живих рядків за старими правилами; нові — за новими (без Sum3D не "
            "рахується й пошта; повторний прорахунок наряду з тим самим видом, матеріалом і кількістю — "
            "один раз). Закритий день, з якого зникли прораховані рядки, перерахунок місяця не чіпає: "
            "серед живих робіт їх уже немає, число впало б. «Пораховано в двох днях» — той самий "
            "наряд, вид, матеріал, кількість і Sum3D живими рядками у двох вкладках: рахується двічі "
            "і за старими, і за новими правилами (рішення за власником). Колонки: lab_/mail_ + zr/pmma/wax/ti. "
            "Лише читання; імена — ініціалами."
        ),
    }
