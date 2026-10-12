"""«У Excel» для «Статистики»: поточна вкладка й список бічної панелі → .xlsx.

Числа ті самі, що на екрані: будуються тими самими функціями
(`stats_view.overview` / `intake` / `side_list`), а не окремим запитом.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app import stats as st
from app.services import stats_data as data
from app.services import stats_view as view
from app.services.stats_data import MATERIAL_GROUP_LABELS
from app.services.xlsx_lite import workbook

_GROUP_WORD = MATERIAL_GROUP_LABELS


def _r(value: float | None, digits: int = 1):
    return None if value is None else round(value, digits)


def overview_book(db: Session, p: view.Params) -> bytes:
    ov = view.overview(db, p)
    cur, was = ov["cur"], ov["was"]
    days = p.period.working_days()
    cover = dict(zip(days, data.coverage(db, days)))
    k = ov["kpis"]
    summary = [
        ["Показник", "Зараз", "Попередній період"],
        ["Період", view.period_label(p.period), ov["prev_label"]],
        ["Одиниць на робочий день (вкладку)", _r(cur["per_day"]), _r(was["per_day"])],
        ["Одиниць усього", cur["units"], was["units"]],
        ["Вкладок з роботами", cur["tab_days"], was["tab_days"]],
        ["Робіт", cur["works"], was["works"]],
        ["Робіт без кількості (не в сумі)", cur["noqty"], was["noqty"]],
        ["Лаба, од.", cur["lab"], was["lab"]],
        ["Клієнти (пошта), од.", cur["client"], was["client"]],
        ["Брак, % одиниць", _r(k["rework"]["pct"], 2), _r(k["rework"]["was_pct"], 2)],
        ["Брак, од.", k["rework"]["units"], None],
    ]
    if p.filters.active:
        summary.append(["Фільтри", ", ".join(f"{n}={v}" for n, v in p.query().items() if n in ("src", "mat", "tech", "client")), None])
    by_day = [["Дата", "День", "Вкладка є", "Робіт", "Лаба, од.", "Клієнти, од.", "Разом, од.", "Без кількості"]]
    for d in days:
        v = cur["by_day"][d]
        label = st.DOW[d.weekday()] + (" +сб нд" if d.weekday() == 4 else "")
        by_day.append([d.strftime("%d.%m.%y"), label, "так" if cover.get(d) else "ні", v["works"], v["lab"], v["client"], v["lab"] + v["client"], v["noqty"]])
    mats = [["Матеріал", "Одиниць", "Частка, %", "Було, од."]]
    for row in ov["mat_rows"]:
        mats.append([row["label"], row["units"], row["share"], row["prev"]])
    clients_now = data.client_units(db, days, p.filters)
    clients_was = data.client_units(db, p.period.previous().working_days(), p.filters)
    clients = [["Клієнт", "Одиниць", "Робіт", "Було, од."]]
    for key, v in sorted(clients_now.items(), key=lambda kv: (-kv[1]["units"], kv[1]["label"])):
        clients.append([v["label"], v["units"], v["works"], clients_was.get(key, {}).get("units", 0)])
    rework = [["Винуватець", "Випадків", "Одиниць переробки"]]
    for b in data.rework_summary(db, days, p.filters)["blames"]:
        rework.append([b["blame"], b["cases"], b["units"]])
    return workbook([("Огляд", summary), ("Дні", by_day), ("Матеріали", mats), ("Клієнти", clients), ("Брак", rework)])


def intake_book(db: Session, p: view.Params) -> bytes:
    it = view.intake(db, p)
    sheets = []
    caption = "поява рядка" if it["mode"] == "row" else "Sum3D (взяття в роботу)"
    for m in it["maps"]:
        rows: list[list] = [[f"{m['label']} · {caption}"] + [f"{h:02d}" for h in range(24)] + ["Разом"]]
        grid = [[0] * 24 for _ in range(7)]
        for cell in m["chart"]["cells"]:
            grid[cell["dow"]][cell["hour"]] = cell["v"]
        for dow in range(7):
            rows.append([st.DOW[dow], *grid[dow], sum(grid[dow])])
        rows.append([])
        rows.append(["Поза робочими годинами, од.", m["off"]])
        rows.append(["Усього, од.", m["total"]])
        sheets.append((f"Карта {m['label']}", rows))
    weeks: list[list] = [["Тиждень з", "Номер", "Цирконій", "ПММА", "Титан", "Інше", "Разом", "Неповний"]]
    for bar in it["weeks_chart"]["bars"]:
        weeks.append([bar["label"], bar["sub"], *(_week_part(bar, g) for g in ("zr", "pmma", "ti", "oth")), bar["total"], "так" if bar["partial"] else ""])
    colors = [["Колір", "Матеріал", "Одиниць"]]
    for c in data.color_units(db, p.period.working_days(), p.filters):
        colors.append([c["color"], _GROUP_WORD.get(c["group"], "Інше"), c["units"]])
    sheets.append(("Тижні × матеріали", weeks))
    sheets.append(("Кольори", colors))
    return workbook(sheets)


def _week_part(bar: dict, group: str) -> int:
    return int(bar["mats"].get(group, 0))


def list_book(db: Session, p: view.Params, req: view.ListRequest) -> bytes:
    side = view.side_list(db, p, req)
    rows = [["Вкладка", "Рядок", "Джерело", "Наряд", "Технік", "Клієнт", "Матеріал", "Колір", "Кількість", "Sum3D"]]
    if req.rework:
        rows[0].append("Брак")
    for group in side["groups"]:
        for w in group["rows"]:
            line = [
                w["tab"],
                w["row"],
                "лаба" if w["kind"] == "lab" else "клієнт",
                w["wo"],
                w["tech"],
                w["client"],
                _GROUP_WORD.get(w["group"], "Інше"),
                w["color"],
                w["units"] if w["units"] is not None else w["qty_raw"],
                w["sum3d"],
            ]
            if req.rework:
                line.append(w.get("rework"))
            rows.append(line)
    return workbook([(side["title"], rows)])
