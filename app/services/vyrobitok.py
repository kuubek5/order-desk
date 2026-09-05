"""Розділ «Виробіток» — місячний облік виготовлених одиниць для нарахування ЗП.

Замінює ручну ODS-таблицю. Один екран рахує сам, оператор може виправити будь-що.
Правила підрахунку відкалібровані на серпні 2026 проти реальної зарплатної
відомості (див. project_vyrobitok_screen / project_slm_tally_rules у пам'яті).

ЩО тут вирішено:

* **одиниці** = сума `Order.quantity` за роботами місяця, не архівними
  (`archived_at IS NULL`), без переробок (`mill_count` ≥ 2 — колонка «Який раз
  фрезерується»); джерело `lab` → колонка «Лабораторія», `email`/`sheet_client`
  → «Пошта»;
* **СЛМ поки ручний.** СЛМ не потрапляє в базу взагалі (`_is_non_queue_row`
  відкидає ці рядки до збереження), тому авто по ньому нуль; оператор вписує
  число сам. Фаза 2 внесе СЛМ у базу з ознакою `off_queue` й авто-рахунок
  замінить ручний — тому `slm` навмисно ПОЗА `AUTO_MATERIALS`;
* **підкови / диски / опаки** — ручні завжди: це не «матеріал у базі», а ознака
  роботи (підкови), витрачений ресурс (диски) або окремий облік по людях
  (опаки);
* **знімок авто.** Лаба чистить старі вкладки → синк архівує роботи того дня →
  рахунок наживо дав би 0 за минулий місяць. Тому щоразу, коли день ще «живий»
  (є роботи в базі), авто-число знімається в `VyrobitokCell.auto_value` і потім
  читається звідти, коли роботи зникли.

Гроші (тарифна сітка з відомості; довідник коефіцієнтів власник дасть повний
пізніше — поки Zn = 0,634, а коефіцієнт поза відомою смугою дає попередження, не
здогад):
* коефіцієнт цирконію = (Zr_лаб + Zr_пошта) ÷ диски → ставка Zn;
* підкови (мости цирконію 11+ од.) оплачуються ВДВІЧІ; їхні одиниці ВХОДЯТЬ у
  цирконій, тому їх віднімають, а не додають;
* одиниці діляться порівну між операторами зміни; опаки НЕ діляться (їх на цьому
  екрані показуємо лише кількістю по людях, без грошей).
"""

from __future__ import annotations

import calendar
from collections import defaultdict
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.business_day import business_today
from app.models import Order, VyrobitokCell, VyrobitokMonth
from app.services.order_dates import order_date
from app.stats import parse_int_safe

# ── Колонки й родини матеріалів ─────────────────────────────────────────────
# Ключ колонки материалу = коротка форма назви з каталогу (Material.name).
MATERIAL_KEY_BY_NAME = {
    "Цирконій": "zr",
    "ПММА": "pmma",
    "Віск": "wax",
    "СЛМ": "slm",
    "Титан": "ti",
}

# Порядок і підпис колонок матеріалів у групах «Лабораторія» та «Пошта».
MATERIAL_COLS: list[tuple[str, str]] = [
    ("zr", "ZrO"),
    ("pmma", "PMMA"),
    ("wax", "Wax"),
    ("slm", "SLM"),
    ("ti", "Ti"),
]

# Кольори-родини (rgb для CSS --h) — ті самі, що фарбують край рядка в черзі.
HUE = {
    "zr": "143,191,224",
    "pmma": "216,180,106",
    "wax": "63,182,201",
    "slm": "154,167,180",
    "ti": "99,200,139",
    "disks": "168,149,120",
    "pidkovy": "143,191,224",
    "op": "255,180,84",
}

# Опаки — фіксований список людей (рішення власника). Індекс = колонка opakN.
OPAK_PEOPLE = ["Денис", "Костя", "Стас", "Вадим", "Рома"]

# Матеріали, які рахуються АВТОМАТИЧНО з бази. slm поза списком — його рядки в
# базу не потрапляють (Фаза 1); Фаза 2 (off_queue) додасть "slm" сюди.
AUTO_MATERIALS = ("zr", "pmma", "wax", "ti")
AUTO_COLS = {
    f"{src}_{key}" for src in ("lab", "mail") for key in AUTO_MATERIALS
}

MATERIAL_COL_KEYS = [
    f"{src}_{key}" for src in ("lab", "mail") for key, _ in MATERIAL_COLS
]
OPAK_COL_KEYS = [f"opak{i}" for i in range(len(OPAK_PEOPLE))]
MANUAL_COL_KEYS = ["pidkovy", "disks", *OPAK_COL_KEYS]
# СЛМ теж поки ручний, хоч і стоїть у групах матеріалів.
ALL_COL_KEYS = [*MATERIAL_COL_KEYS, "pidkovy", "disks", *OPAK_COL_KEYS]

# ── Гроші ───────────────────────────────────────────────────────────────────
# Тарифи, які НЕ залежать від коефіцієнта (з відомості). Zn — окремо, за
# коефіцієнтом.
TARIFF_PMMA_WAX = 0.75
TARIFF_TI = 0.75
TARIFF_SLM = 0.8
# Ставка Zn за замовчуванням (коефіцієнт ~24,2 у серпні). Повний довідник
# коефіцієнт→ставка власник дасть пізніше; поки лишається як є.
DEFAULT_RATE_ZN = 0.634
# Межа ВІДОМОГО довідника коефіцієнтів. Вище — CRM не вгадує ставку, а просить
# її (rate_override) і показує попередження.
RATE_BAND_MAX = 26.7

MONTH_NAMES_SHORT = [
    "січ", "лют", "бер", "кві", "тра", "чер",
    "лип", "сер", "вер", "жов", "лис", "гру",
]
MONTH_NAMES_LONG = [
    "січень", "лютий", "березень", "квітень", "травень", "червень",
    "липень", "серпень", "вересень", "жовтень", "листопад", "грудень",
]


def parse_decimal(raw: str | None) -> float | None:
    """«52» / «51,5» / «0,634» → float. Кома або крапка. None — порожньо/сміття."""
    if raw is None:
        return None
    text = raw.strip().replace(",", ".")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _is_rework(mill_count: str | None) -> bool:
    """Робота фрезерується вдруге і далі — переробка, у виробіток НЕ рахується.
    Її кількість навмисно лишають порожньою в таблиці, але ознака надійніша."""
    n = parse_int_safe(mill_count)
    return n is not None and n >= 2


def nf(value: float, decimals: int = 2) -> str:
    """Українське форматування числа: пробіл-роздільник тисяч, кома-десяткова."""
    text = f"{value:,.{decimals}f}"
    return text.replace(",", " ").replace(".", ",")


@dataclass
class MonthGrid:
    year: int
    month: int
    month_label: str
    rows: list[dict]
    totals: dict[str, int]
    money: dict
    opaks: dict
    months_nav: list[dict]
    warn: dict | None


def _source_bucket(source: str) -> str:
    """lab → «Лабораторія»; усе клієнтське (email, sheet_client) → «Пошта»."""
    return "lab" if source == "lab" else "mail"


def _load_month_settings(db: Session, year: int, month: int) -> VyrobitokMonth:
    row = db.scalar(
        select(VyrobitokMonth).where(
            VyrobitokMonth.year == year, VyrobitokMonth.month == month
        )
    )
    if row is None:
        # Не зберігаємо порожній рядок на кожен перегляд — віддаємо дефолтний
        # об'єкт у пам'яті. Рядок з'явиться, щойно оператор змінить курс/склад.
        row = VyrobitokMonth(year=year, month=month, kurs="52", people_count=5)
    return row


def compute_month(
    db: Session, year: int, month: int, *, persist: bool = True
) -> MonthGrid:
    days_in_month = calendar.monthrange(year, month)[1]
    first = date(year, month, 1)
    last = date(year, month, days_in_month)
    today = business_today()

    orders = db.scalars(
        select(Order)
        .options(selectinload(Order.material))
        .where(Order.archived_at.is_(None))
    ).all()

    # Авто-одиниці по (день, колонка) + які місяці взагалі мають дані (для
    # притишення в стрічці місяців) + які дні місяця ще «живі» (є роботи).
    auto: dict[tuple[date, str], int] = defaultdict(int)
    live_days: set[date] = set()
    months_with_orders: set[tuple[int, int]] = set()

    for order in orders:
        d = order_date(order)
        months_with_orders.add((d.year, d.month))
        if not (first <= d <= last):
            continue
        if _is_rework(order.mill_count):
            continue
        live_days.add(d)
        material = order.material
        key = MATERIAL_KEY_BY_NAME.get(material.name) if material else None
        if key is None or key not in AUTO_MATERIALS:
            continue
        col_key = f"{_source_bucket(order.source)}_{key}"
        auto[(d, col_key)] += parse_int_safe(order.quantity) or 0

    cells = db.scalars(
        select(VyrobitokCell).where(VyrobitokCell.day.between(first, last))
    ).all()
    by_key: dict[tuple[date, str], VyrobitokCell] = {
        (c.day, c.col_key): c for c in cells
    }

    # Знімок авто у сховище для «живих» днів — щоб минулий місяць вижив, коли
    # роботи заархівуються. Пишемо лише те, що змінилось.
    if persist:
        changed = False
        for (d, col_key), value in auto.items():
            cell = by_key.get((d, col_key))
            if cell is None:
                cell = VyrobitokCell(day=d, col_key=col_key, auto_value=value)
                db.add(cell)
                by_key[(d, col_key)] = cell
                changed = True
            elif cell.auto_value != value:
                cell.auto_value = value
                changed = True
        if changed:
            db.commit()

    months_with_data = set(months_with_orders)
    for c in cells:
        months_with_data.add((c.day.year, c.day.month))

    def cell_view(d: date, col_key: str) -> dict:
        cell = by_key.get((d, col_key))
        if col_key in AUTO_COLS:
            if d in live_days:
                eff_auto = auto.get((d, col_key), 0)
            elif cell is not None and cell.auto_value is not None:
                eff_auto = cell.auto_value
            else:
                eff_auto = 0
            override = cell.override_value if cell is not None else None
            num = override if override is not None else eff_auto
            return {
                "num": num,
                "auto": eff_auto,
                "edited": override is not None,
            }
        override = cell.override_value if cell is not None else None
        num = override if override is not None else 0
        # Ручні колонки не мають «авто», тому й не бувають «виправленими» —
        # мітка правки означає «CRM порахувала одне, ти вписав інше».
        return {"num": num, "auto": None, "edited": False}

    rows: list[dict] = []
    for dayn in range(1, days_in_month + 1):
        d = date(year, month, dayn)
        weekday = d.weekday()  # 0=пн … 6=нд
        day_cells = {ck: cell_view(d, ck) for ck in ALL_COL_KEYS}
        has_any = d in live_days or any(c["num"] for c in day_cells.values())
        weekend = weekday >= 5
        rows.append(
            {
                "date": d.isoformat(),
                "dayn": dayn,
                "weekend_short": "сб" if weekday == 5 else ("нд" if weekday == 6 else ""),
                "is_off": weekend and not has_any,
                "is_today": d == today,
                "cells": day_cells,
            }
        )

    totals = {
        ck: sum(r["cells"][ck]["num"] for r in rows) for ck in ALL_COL_KEYS
    }

    money = _money(db, year, month, totals)
    opaks = {
        "month_label": f"{MONTH_NAMES_LONG[month - 1]} {year}",
        "people": [
            {"name": OPAK_PEOPLE[i], "units": totals[f"opak{i}"]}
            for i in range(len(OPAK_PEOPLE))
        ],
        "total": sum(totals[f"opak{i}"] for i in range(len(OPAK_PEOPLE))),
    }

    months_nav = [
        {
            "num": m,
            "label": MONTH_NAMES_SHORT[m - 1],
            "has_data": (year, m) in months_with_data,
            "on": m == month,
        }
        for m in range(1, 13)
    ]

    warn = None
    if money["rate_out_of_band"]:
        warn = {"coefficient": money["coefficient_str"]}

    return MonthGrid(
        year=year,
        month=month,
        month_label=f"{MONTH_NAMES_LONG[month - 1]} {year}",
        rows=rows,
        totals=totals,
        money=money,
        opaks=opaks,
        months_nav=months_nav,
        warn=warn,
    )


def _money(db: Session, year: int, month: int, totals: dict[str, int]) -> dict:
    settings = _load_month_settings(db, year, month)
    people = settings.people_count or 5
    kurs = parse_decimal(settings.kurs) or 0.0
    rate_override = parse_decimal(settings.rate_override)

    zr_total = totals["lab_zr"] + totals["mail_zr"]
    pidkovy = totals["pidkovy"]
    disks = totals["disks"]
    pmma_wax = totals["lab_pmma"] + totals["mail_pmma"] + totals["lab_wax"] + totals["mail_wax"]
    ti_total = totals["lab_ti"] + totals["mail_ti"]
    slm_total = totals["lab_slm"] + totals["mail_slm"]

    coefficient = zr_total / disks if disks else None
    rate_out_of_band = (
        coefficient is not None
        and coefficient > RATE_BAND_MAX
        and rate_override is None
    )
    rate_zn = rate_override if rate_override is not None else DEFAULT_RATE_ZN

    zn_plain = zr_total - pidkovy  # цирконій без підков
    types = [
        {"name": "Цирконій", "units": zn_plain, "rate": rate_zn},
        {"name": "Підкови", "units": pidkovy, "rate": rate_zn * 2},
        {"name": "ПММА / воск", "units": pmma_wax, "rate": TARIFF_PMMA_WAX},
        {"name": "Титан", "units": ti_total, "rate": TARIFF_TI},
        {"name": "SLM", "units": slm_total, "rate": TARIFF_SLM},
    ]
    for t in types:
        t["sum"] = t["units"] * t["rate"]
        t["units_str"] = nf(t["units"], 0)
        t["rate_str"] = nf(t["rate"], 3)
        t["sum_str"] = nf(t["sum"], 2)

    total_units = sum(t["units"] for t in types)
    share = total_units / people if people else 0.0
    uo_share = sum((t["units"] / people) * t["rate"] for t in types) if people else 0.0
    grn_share = uo_share * kurs

    return {
        "zr_total": zr_total,
        "disks_total": disks,
        "pidkovy_total": pidkovy,
        "coefficient": coefficient,
        "coefficient_str": nf(coefficient, 1) if coefficient is not None else "—",
        "rate_zn": rate_zn,
        "rate_str": nf(rate_zn, 3),
        "rate_pid_str": nf(rate_zn * 2, 3),
        "rate_override": settings.rate_override or "",
        "rate_out_of_band": rate_out_of_band,
        "types": types,
        "total_units": total_units,
        "total_units_str": nf(total_units, 0),
        "people_count": people,
        "share": share,
        "share_str": nf(share, 2),
        "kurs": settings.kurs or "",
        "uo_share": uo_share,
        "grn_share": grn_share,
        "grn_share_str": nf(grn_share, 0),
    }


def set_cell(db: Session, day: date, col_key: str, value: int | None) -> None:
    """Зберегти правку оператора. value=None — стерти правку (повернути авто).

    Ручні колонки використовують те саме override_value: там авто немає, тож
    введене число і є значенням клітинки."""
    if col_key not in ALL_COL_KEYS:
        raise ValueError(f"невідома колонка: {col_key}")
    cell = db.scalar(
        select(VyrobitokCell).where(
            VyrobitokCell.day == day, VyrobitokCell.col_key == col_key
        )
    )
    if cell is None:
        cell = VyrobitokCell(day=day, col_key=col_key)
        db.add(cell)
    cell.override_value = value
    db.commit()


def save_month_settings(
    db: Session,
    year: int,
    month: int,
    *,
    kurs: str | None = None,
    people_count: int | None = None,
    rate_override: str | None = None,
) -> VyrobitokMonth:
    row = db.scalar(
        select(VyrobitokMonth).where(
            VyrobitokMonth.year == year, VyrobitokMonth.month == month
        )
    )
    if row is None:
        row = VyrobitokMonth(year=year, month=month)
        db.add(row)
    if kurs is not None:
        row.kurs = kurs.strip() or "52"
    if people_count is not None:
        row.people_count = max(1, people_count)
    if rate_override is not None:
        row.rate_override = rate_override.strip() or None
    db.commit()
    return row
