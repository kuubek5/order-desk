"""«Статистика»: параметри адреси → контекст шаблону (вкладки Огляд/Надходження).

Адреса — джерело стану, щоб посиланням можна було поділитись:
`/stats?tab=intake&period=90&src=lab&mat=1&tech=кіра&client=…&cmp=0&hm=row`.
Типові значення в адресу не пишуться.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import date, timedelta
from urllib.parse import urlencode

from sqlalchemy.orm import Session

from app import stats as st
from app.business_day import tab_day
from app.services import stats_charts as charts
from app.services import stats_data as data
from app.services.stats_data import MATERIAL_GROUP_LABELS, MATERIAL_GROUPS, Filters

# (ключ, назва, етап-заглушка або None, лише адмін)
TABS = (
    ("overview", "Огляд", None, False),
    ("intake", "Надходження", None, False),
    ("techs", "Техніки", 3, True),
    ("mail", "Пошта", 2, False),
    ("production", "Виробництво", 4, False),
    ("forecast", "Прогноз", 5, False),
)
TAB_KEYS = {t[0] for t in TABS}

STAGE_TEXT = {
    "techs": "На техніка: одиниці, роботи, медіана й 80-й перцентиль «наряд → модель», частка «на швидку», брак з вини техніка. Час — з 12.10.26.",
    "mail": "Карта день × година за часом листа, клієнти й тренд (зростає / падає / зник), матеріали й кольори клієнтів пошти.",
    "production": "Де робота чекає найдовше: наряд → шлях → Sum3D → фрезерування → знайдено → видано, медіани й розподіл.",
    "forecast": "Тиждень наперед по днях: усього, лаба, пошта, по матеріалах; прогноз записується наперед і звіряється з фактом.",
}

TOP_CLIENTS = 8
CLIENT_SELECT_LIMIT = 300
WEEKS = 12


@dataclass(frozen=True)
class Params:
    tab: str = "overview"
    period_key: str = st.DEFAULT_PERIOD
    period: st.Period = st.Period(date(2000, 1, 1), date(2000, 1, 30))
    cmp: bool = True
    hm: str = "sum3d"
    filters: Filters = Filters()

    def query(self, **over) -> dict[str, str]:
        p = replace(self, **{k: v for k, v in over.items() if k in self.__dataclass_fields__})
        f = over.get("filters_over")
        flt = replace(p.filters, **f) if f else p.filters
        q: dict[str, str] = {}
        if p.tab != "overview":
            q["tab"] = p.tab
        if p.period_key != st.DEFAULT_PERIOD:
            q["period"] = p.period_key
        if p.period_key == "custom":
            q["from"] = p.period.start.isoformat()
            q["to"] = p.period.end.isoformat()
        if not p.cmp:
            q["cmp"] = "0"
        for name in ("src", "mat", "tech", "client"):
            value = getattr(flt, name)
            if value:
                q[name] = value
        if p.hm != "sum3d":
            q["hm"] = p.hm
        return q

    def qs(self, **over) -> str:
        q = self.query(**over)
        return ("?" + urlencode(q)) if q else ""


def parse_params(query: Mapping[str, str], today: date, is_admin: bool) -> Params:
    tab = query.get("tab") or "overview"
    if tab not in TAB_KEYS or (tab == "techs" and not is_admin):
        tab = "overview"
    key, period = st.resolve_period(query.get("period"), query.get("from"), query.get("to"), today)
    return Params(
        tab=tab,
        period_key=key,
        period=period,
        cmp=query.get("cmp") != "0",
        hm="row" if query.get("hm") == "row" else "sum3d",
        filters=Filters.parse(query.get("src"), query.get("mat"), query.get("tech"), query.get("client")),
    )


def period_label(period: st.Period) -> str:
    a, b = period.start, period.end
    return f"{a.day:02d}.{a.month:02d}–{b.day:02d}.{b.month:02d}.{b.year % 100:02d}"


def _src_toggle(src: str, which: str) -> str:
    """Клік по чипу джерела: вимкнути його; обидва вимкнути не можна."""
    if src == "":
        return "client" if which == "lab" else "lab"
    return ""


# ── Шапка: вкладки, період, фільтри ────────────────────────────────────────
def chrome(db: Session, p: Params, is_admin: bool) -> dict:
    days = p.period.working_days()
    cover = data.coverage(db, days)
    f = p.filters
    tabs = [
        {"key": k, "label": label, "stage": stage, "admin": admin, "on": k == p.tab, "qs": p.qs(tab=k)}
        for k, label, stage, admin in TABS
        if not admin or is_admin
    ]
    periods = [
        {"key": k, "label": f"{k} днів", "short": k, "on": p.period_key == k, "qs": p.qs(period_key=k)}
        for k in ("7", "30", "90")
    ]
    mats = [m for m in data.materials(db)]
    techs = sorted(data.tech_groups(db).items(), key=lambda kv: (-kv[1]["n"], kv[1]["label"]))
    clients_now = data.client_units(db, days, replace(f, client=""))
    client_opts = sorted(
        ((k, v) for k, v in clients_now.items() if k),
        key=lambda kv: (-kv[1]["units"], kv[1]["label"]),
    )[:CLIENT_SELECT_LIMIT]
    if f.client and f.client not in dict(client_opts):
        label = data.client_groups(db).get(f.client, {}).get("label", f.client)
        client_opts.insert(0, (f.client, {"label": label, "units": 0}))
    tech_label = data.tech_groups(db).get(f.tech, {}).get("label", f.tech) if f.tech else ""
    mat_label = ""
    if f.mat == "none":
        mat_label = "не визначено"
    elif f.mat:
        mat_label = next((name for mid, name, _ in mats if str(mid) == f.mat), "")
    return {
        "tabs": tabs,
        "periods": periods,
        "period_label": period_label(p.period),
        "period_days": p.period.length,
        "custom_on": p.period_key == "custom",
        "cover": [{"day": d, "has": h, "tip": f"{st.DOW[d.weekday()]} {d.day:02d}.{d.month:02d}" + ("" if h else " · немає вкладки")} for d, h in zip(days, cover)],
        "cover_have": sum(cover),
        "cover_total": len(days),
        "src_lab_on": f.src in ("", "lab"),
        "src_client_on": f.src in ("", "client"),
        "src_lab_qs": p.qs(filters_over={"src": _src_toggle(f.src, "lab")}),
        "src_client_qs": p.qs(filters_over={"src": _src_toggle(f.src, "client")}),
        "mat_options": [("", "усі")] + [(str(mid), name) for mid, name, _ in mats] + [("none", "не визначено")],
        "tech_options": [("", "усі")] + [(k, f"{v['label']} · {v['n']}") for k, v in techs],
        "client_options": [("", "усі")] + [(k, v["label"]) for k, v in client_opts],
        "mat_label": mat_label,
        "tech_label": tech_label,
        "client_label": (clients_now.get(f.client) or data.client_groups(db).get(f.client) or {}).get("label", f.client) if f.client else "",
        "clear_mat_qs": p.qs(filters_over={"mat": ""}),
        "clear_tech_qs": p.qs(filters_over={"tech": ""}),
        "clear_client_qs": p.qs(filters_over={"client": ""}),
        "show_tech": f.src != "client",
        "show_client": f.src != "lab",
        "cmp_qs_on": p.qs(cmp=True),
        "cmp_qs_off": p.qs(cmp=False),
        "export_qs": p.qs(),
        "can_export": p.tab in ("overview", "intake"),
        "hidden": p.query(),
    }


# ── Огляд ──────────────────────────────────────────────────────────────────
def _arrow(delta: float | None, unit: str = "%", digits: int = 0, *, bad_up: bool = False) -> dict | None:
    if delta is None:
        return None
    up = delta > 0
    text = f"{abs(delta):.{digits}f}".replace(".", ",") if digits else f"{abs(round(delta))}"
    return {"sym": "↑" if up else ("↓" if delta < 0 else "→"), "text": f"{text} {unit}", "bad": up == bad_up and delta != 0}


def overview(db: Session, p: Params) -> dict:
    f = p.filters
    days = p.period.working_days()
    prev_period = p.period.previous()
    prev_days = prev_period.working_days()
    group_of = data._material_group_map(db)
    cur = data.summarize(data.facts(db, days, f), days, group_of)
    was = data.summarize(data.facts(db, prev_days, f), prev_days, group_of)
    cover = data.coverage(db, days)

    rw_now = data.rework_summary(db, days, f)
    rw_was = data.rework_summary(db, prev_days, f)
    rw_pct = (rw_now["units"] / cur["units"] * 100) if cur["units"] else None
    rw_pct_was = (rw_was["units"] / was["units"] * 100) if was["units"] else None

    lead = data.lead_minutes(db, days, f)
    lead_ready = data.has_job_code_moments(db)
    lab_share = (cur["lab"] / cur["units"] * 100) if cur["units"] else None

    kpis = {
        "per_day": {
            "v": cur["per_day"],
            "was": was["per_day"],
            "text": st.nf(cur["per_day"]),
            "was_text": st.nf(was["per_day"]),
            "arrow": _arrow(st.delta_pct(cur["per_day"], was["per_day"])),
            "sum": st.nf(cur["units"]),
            "tab_days": cur["tab_days"],
            "was_tab_days": was["tab_days"],
        },
        "units": {
            "v": cur["units"],
            "was": was["units"],
            "text": st.nf(cur["units"]),
            "was_text": st.nf(was["units"]),
            "works": st.nf(cur["works"]),
            "noqty": cur["noqty"],
        },
        "split": {
            "lab_pct": lab_share,
            "lab": st.nf(cur["lab"]),
            "client": st.nf(cur["client"]),
        },
        "rework": {
            "pct": rw_pct,
            "units": rw_now["units"],
            "cases": rw_now["cases"],
            "was_pct": rw_pct_was,
            "arrow": _arrow(
                None if rw_pct is None or rw_pct_was is None else rw_pct - rw_pct_was,
                "в.п.",
                1,
                bad_up=True,
            ),
        },
        "lead": {
            "ready": lead_ready and bool(lead),
            "collecting": not lead_ready,
            "median": st.format_minutes(st.median(lead)),
            "n": len(lead),
        },
    }

    # Матеріали: частка й зміна частки в п.п.; риска — одиниці «було».
    mat_rows = []
    for g in MATERIAL_GROUPS:
        now_u, was_u = cur["materials"][g], was["materials"][g]
        share = now_u / cur["units"] * 100 if cur["units"] else 0.0
        was_share = was_u / was["units"] * 100 if was["units"] else None
        mat_rows.append(
            {
                "group": g,
                "label": MATERIAL_GROUP_LABELS[g],
                "units": now_u,
                "units_text": st.nf(now_u),
                "share": round(share),
                "prev": was_u,
                "dpp": None if was_share is None else share - was_share,
            }
        )
    mat_rows = charts.share_rows(mat_rows)
    mat_filter = {mid: str(mid) for mid, _, _ in data.materials(db)}
    group_ids: dict[str, list[str]] = {}
    for mid, _, grp in data.materials(db):
        group_ids.setdefault(grp, []).append(mat_filter[mid])
    for row in mat_rows:
        ids = group_ids.get(row["group"], [])
        row["qs"] = p.qs(filters_over={"mat": ids[0]}) if len(ids) == 1 else None

    clients_now = data.client_units(db, days, f)
    clients_was = data.client_units(db, prev_days, f)
    ranked = sorted(((k, v) for k, v in clients_now.items() if k), key=lambda kv: (-kv[1]["units"], kv[1]["label"]))
    client_rows = charts.share_rows(
        [
            {
                "key": k,
                "label": v["label"],
                "units": v["units"],
                "units_text": st.nf(v["units"]),
                "prev": clients_was.get(k, {}).get("units", 0),
                "delta": st.delta_pct(v["units"], clients_was.get(k, {}).get("units", 0)),
                "new": not clients_was.get(k, {}).get("units"),
                "qs": p.qs(filters_over={"client": k}),
            }
            for k, v in ranked[: TOP_CLIENTS * 4]
        ]
    )
    nameless = clients_now.get("", {}).get("units", 0)

    by_day = cur["by_day"]
    wide = charts.day_chart(days, by_day, cover, cur["per_day"], was["per_day"], width=1300, height=300, compact=False)
    compact = charts.day_chart(days, by_day, cover, cur["per_day"], was["per_day"], width=358, height=210, compact=True)
    selected = next((d for d in reversed(days) if by_day[d]["works"]), None)

    return {
        "cur": cur,
        "was": was,
        "kpis": kpis,
        "lab_text": st.nf(cur["lab"]),
        "client_text": st.nf(cur["client"]),
        "chart_wide": wide,
        "chart_compact": compact,
        "mat_rows": mat_rows,
        "client_rows": client_rows,
        "clients_total": len(ranked),
        "nameless_units": nameless,
        "selected_day": selected,
        "prev_label": period_label(prev_period),
    }


# ── Надходження ────────────────────────────────────────────────────────────
def intake(db: Session, p: Params) -> dict:
    f = p.filters
    days = p.period.working_days()
    seen_days = data.row_seen_days(db)
    row_ready = seen_days >= st.ROW_SEEN_MIN_DAYS
    mode = p.hm if (p.hm == "sum3d" or row_ready) else "sum3d"
    heat = data.row_seen_heat(db, days, f) if mode == "row" else data.sum3d_heat(db, days, f)
    maps = []
    for kind, label in (("lab", "Лаба"), ("client", "Клієнти (пошта)")):
        if (kind == "lab" and f.src == "client") or (kind == "client" and f.src == "lab"):
            continue
        grid = heat["grids"][kind]
        total, off = st.off_hours(grid)
        maps.append(
            {
                "kind": kind,
                "label": label,
                "chart": charts.heatmap(grid, kind),
                "n": heat["n"][kind],
                "total": total,
                "off": off,
                "off_pct": round(off / total * 100) if total else None,
                "total_text": st.nf(total),
                "off_text": st.nf(off),
            }
        )

    # 12 тижнів до кінця періоду — завжди, щоб було видно тренд.
    last_monday = st.week_monday(p.period.end)
    first_monday = last_monday - timedelta(weeks=WEEKS - 1)
    span = st.Period(first_monday, p.period.end).working_days()
    group_of = data._material_group_map(db)
    week_facts = data.facts(db, span, f)
    weeks = []
    have_tabs = {tab_day(d) for d in data.tab_index(db).dated.values()}
    for i in range(WEEKS):
        monday = first_monday + timedelta(weeks=i)
        mats = dict.fromkeys(MATERIAL_GROUPS, 0)
        for fact in week_facts:
            if st.week_monday(fact.day) == monday:
                mats[group_of.get(fact.material_id, "oth")] += fact.units
        week_days = [monday + timedelta(days=k) for k in range(5)]
        in_range = [d for d in week_days if d <= p.period.end]
        partial = len(in_range) < 5 or any(d not in have_tabs for d in in_range)
        weeks.append({"monday": monday, "mats": mats, "partial": partial})

    colors = data.color_units(db, days, f)[:8]
    color_rows = charts.share_rows(
        [{"color": c["color"], "group": c["group"], "units": c["units"], "units_text": st.nf(c["units"])} for c in colors]
    )
    return {
        "mode": mode,
        "row_ready": row_ready,
        "row_seen_days": seen_days,
        "row_qs": p.qs(hm="row"),
        "sum3d_qs": p.qs(hm="sum3d"),
        "maps": maps,
        "weeks_chart": charts.week_chart(weeks),
        "color_rows": color_rows,
        "work_frame": f"{st.DOW[min(st.WORK_WEEKDAYS)]}–{st.DOW[max(st.WORK_WEEKDAYS)]} {st.WORK_HOURS[0]}:00–{st.WORK_HOURS[1]}:00",
    }


# ── Бічна панель ───────────────────────────────────────────────────────────
@dataclass(frozen=True)
class ListRequest:
    day: date | None = None
    cell: tuple[str, int, int] | None = None  # (kind, dow, hour)
    noqty: bool = False
    rework: bool = False

    @classmethod
    def parse(cls, query: Mapping[str, str]) -> ListRequest:
        day = None
        try:
            day = date.fromisoformat(query.get("day") or "")
        except ValueError:
            day = None
        cell = None
        parts = (query.get("cell") or "").split(":")
        if len(parts) == 3 and parts[0] in ("lab", "client") and parts[1].isdigit() and parts[2].isdigit():
            dow, hour = int(parts[1]), int(parts[2])
            if 0 <= dow < 7 and 0 <= hour < 24:
                cell = (parts[0], dow, hour)
        return cls(day=day, cell=cell, noqty=query.get("noqty") == "1", rework=query.get("rework") == "1")

    def query(self) -> dict[str, str]:
        if self.day:
            return {"day": self.day.isoformat()}
        if self.cell:
            return {"cell": f"{self.cell[0]}:{self.cell[1]}:{self.cell[2]}"}
        if self.noqty:
            return {"noqty": "1"}
        if self.rework:
            return {"rework": "1"}
        return {}


def side_list(db: Session, p: Params, req: ListRequest) -> dict:
    f = p.filters
    period_days = p.period.working_days()
    kind = None
    cell = None
    if req.day is not None:
        days = [tab_day(req.day)]
        title = f"{st.DOW[days[0].weekday()]} {days[0].day:02d}.{days[0].month:02d}"
        tag = "пт + сб + нд" if days[0].weekday() == 4 else "вкладка"
    elif req.cell is not None:
        days = period_days
        kind, dow, hour = req.cell
        cell = (dow, hour)
        title = f"{st.DOW[dow]} · {hour:02d}:00–{(hour + 1) % 24:02d}:00"
        tag = ("лаба" if kind == "lab" else "клієнти") + " · " + ("поява рядка" if p.hm == "row" else "Sum3D")
    elif req.noqty:
        days = period_days
        title = "Без кількості"
        tag = period_label(p.period)
    elif req.rework:
        days = period_days
        title = "Брак"
        tag = period_label(p.period)
    else:
        days = []
        title, tag = "—", ""
    works = data.works_list(
        db, days, f, kind=kind, noqty=req.noqty, rework=req.rework, cell=cell, heat_mode=p.hm
    ) if days else []
    groups: list[dict] = []
    for w in works:
        label = w["tab"] or f"{w['tab_date'].day:02d}.{w['tab_date'].month:02d} (без вкладки)"
        if not groups or groups[-1]["tab"] != label:
            d = w["tab_date"]
            groups.append({"tab": label, "title": f"{st.DOW[d.weekday()]} {d.day:02d}.{d.month:02d}", "rows": [], "units": 0})
        groups[-1]["rows"].append(w)
        groups[-1]["units"] += w["units"] or 0
    lab = sum(w["units"] or 0 for w in works if w["kind"] == "lab")
    client = sum(w["units"] or 0 for w in works if w["kind"] == "client")
    blames = data.rework_summary(db, days, f)["blames"] if req.rework and days else []
    q = {**p.query(), **req.query()}
    return {
        "title": title,
        "tag": tag,
        "works_n": len(works),
        "units": st.nf(lab + client),
        "lab": st.nf(lab),
        "client": st.nf(client),
        "noqty": sum(1 for w in works if w["units"] is None),
        "groups": groups,
        "open_all": len(works) <= 60,
        "blames": blames,
        "export_qs": "?" + urlencode(q),
        "limit_hit": len(works) >= data.LIST_LIMIT,
    }
