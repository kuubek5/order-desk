"""Геометрія графіків «Статистики» — inline SVG без бібліотек.

Тут лише числа (координати, висоти, підписи); кольори — токени в шаблоні й
stats.css (`--st-*`), тема = токени. Розмітку малює `_stats_*.html`.

Стовпчики — група `<g class="st-bar" data-key>`: JS (stats.js) при зміні
періоду чи фільтра бере стару висоту за `data-key` і плавно «перетікає» до
нової трансформацією (FLIP), а не малює з нуля.
"""

from __future__ import annotations

import math
from datetime import date

from app import stats as st

HEAT_OPACITY = (0.16, 0.34, 0.55, 0.78, 1.0)
_HEAT_BINS = (0.2, 0.4, 0.6, 0.8)


def nice_scale(peak: float, ticks: int = 3) -> tuple[float, float]:
    """(верх осі, крок): верх — кругле число, ≥ піку, рівно `ticks` кроків."""
    if peak <= 0:
        return float(ticks), 1.0
    raw = peak / ticks
    mag = 10 ** math.floor(math.log10(raw))
    for mult in (1, 1.25, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10):
        step = mult * mag
        if step * ticks >= peak:
            return step * ticks, step
    return 10 * mag * ticks, 10 * mag


def _top_rounded(x: float, w: float, top: float, bottom: float, r: float = 4.0) -> str:
    """Стовпчик із заокругленим лише ВЕРХОМ (кінець даних), низ — на осі."""
    r = max(0.0, min(r, (bottom - top), w / 2))
    return (
        f"M{x:.1f},{bottom:.1f} V{top + r:.1f} Q{x:.1f},{top:.1f} {x + r:.1f},{top:.1f} "
        f"H{x + w - r:.1f} Q{x + w:.1f},{top:.1f} {x + w:.1f},{top + r:.1f} V{bottom:.1f} Z"
    )


def _flat(x: float, w: float, top: float, bottom: float) -> str:
    return f"M{x:.1f},{bottom:.1f} V{top:.1f} H{x + w:.1f} V{bottom:.1f} Z"


def _stack(x: float, w: float, base: float, parts: list[tuple[str, float]], gap: float = 2.0) -> tuple[list[dict], float]:
    """Сегменти стосу знизу вгору; між сегментами — зазор кольору тла."""
    segs: list[dict] = []
    visible = [(cls, h) for cls, h in parts if h > 0.05]
    y = base
    for i, (cls, h) in enumerate(visible):
        top = y - h
        last = i == len(visible) - 1
        segs.append({"cls": cls, "d": _top_rounded(x, w, top, y) if last else _flat(x, w, top, y)})
        y = top - (0 if last else gap)
    return segs, (y if segs else base)


def day_chart(
    days: list[date],
    by_day: dict,
    cover: list[bool],
    avg: float | None,
    prev_avg: float | None,
    *,
    width: int,
    height: int,
    compact: bool,
    was: dict[date, tuple[date, int]] | None = None,
) -> dict:
    """`was`: для кожного дня той самий день тижня в попередньому періоді (дата, одиниці).

    Порівнюємо понеділок із понеділком, а не «n-й день із n-м»: п'ятниця несе вихідні й
    завжди вища, тож зсув на кілька днів показав би хибну різницю.
    """
    was = was or {}
    left, right, top, bottom = (34, 6, 22, 30) if compact else (46, 86, 30, 52)
    pw, ph = width - left - right, height - top - bottom
    totals = [by_day[d]["lab"] + by_day[d]["client"] for d in days]
    peak = max([*totals, *(v for _, v in was.values()), avg or 0, prev_avg or 0, 1])
    vmax, step = nice_scale(peak)
    base = top + ph

    def y(v: float) -> float:
        return base - v / vmax * ph

    n = max(len(days), 1)
    slot = pw / n
    bw = min(slot * (0.66 if compact else 0.56), 40)
    many = n > 26
    peak_total = max(totals) if totals else 0
    bars = []
    for i, d in enumerate(days):
        cx = left + slot * i + slot / 2
        x = cx - bw / 2
        lab, client = by_day[d]["lab"], by_day[d]["client"]
        total = lab + client
        has = bool(by_day[d]["works"]) or cover[i]
        segs, top_y = _stack(x, bw, base, [("lab", lab / vmax * ph), ("cli", client / vmax * ph)])
        if compact or many:
            # Перший день підписуємо, лише якщо понеділок не стоїть впритул: «27.0831.08».
            monday_close = i == 0 and any(later.weekday() == 0 for later in days[1:4])
            label = f"{d.day:02d}.{d.month:02d}" if (d.weekday() == 0 or (i == 0 and not monday_close)) else ""
            sub = ""
        elif n > 12:
            # Вузький стовпчик: число й день тижня двома ярусами, інакше підписи злипаються.
            label = f"{d.day:02d}"
            sub = st.DOW[d.weekday()] + ("+" if d.weekday() == 4 else "")
        else:
            label = f"{st.DOW[d.weekday()]} {d.day:02d}"
            sub = "+сб нд" if d.weekday() == 4 else ""
        tip = f"{st.DOW[d.weekday()]} {d.day:02d}.{d.month:02d}"
        if d.weekday() == 4:
            tip += " (+сб нд)"
        if by_day[d]["works"]:
            tip += f" · {st.nf(total)} од. · лаба {st.nf(lab)} · клієнти {st.nf(client)}"
            if by_day[d]["noqty"]:
                tip += f" · ? {by_day[d]['noqty']} без кількості"
        elif not has:
            tip += " · немає вкладки"
        else:
            tip += " · 0 робіт"
        was_day, was_units = was.get(d, (None, 0))
        if was_day is not None and was_units:
            tip += f" · було {st.DOW[was_day.weekday()]} {was_day.day:02d}.{was_day.month:02d}: {st.nf(was_units)} од."
        bars.append(
            {
                "key": d.isoformat(),
                "x": round(x, 1),
                "w": round(bw, 1),
                "slot_x": round(left + slot * i, 1),
                "slot_w": round(slot, 1),
                "cx": round(cx, 1),
                "segs": segs,
                "top": round(top_y, 1),
                "base": base,
                "total": total,
                "lab": lab,
                "client": client,
                "works": by_day[d]["works"],
                "date": f"{st.DOW[d.weekday()]} {d.day:02d}.{d.month:02d}",
                "was_units": was_units,
                "was_y": round(y(was_units), 1) if was_units else None,
                "has": has,
                "empty": not by_day[d]["works"],
                "label": label,
                "sub": sub,
                "tip": tip,
                "show_total": (not compact) and total > 0 and total == peak_total,
            }
        )
    hatch_h = 30 if compact else 46
    avg_y = None if avg is None else y(avg)
    prev_y = None if prev_avg is None else y(prev_avg)
    avg_label_y, prev_label_y = _line_label_ys(avg_y, prev_y)
    return {
        "w": width,
        "h": height,
        "left": left,
        "right": width - right,
        "top": top,
        "base": base,
        "label_y": base + (16 if compact else 17),
        "sub_y": base + 36,
        "ticks": [{"v": st.nf(step * k), "y": round(y(step * k), 1)} for k in range(4)],
        "bars": bars,
        "hatch_y": base - hatch_h,
        "hatch_h": hatch_h,
        "avg": None if avg is None else {"y": round(y(avg), 1), "v": st.nf(avg), "label_y": avg_label_y},
        "prev": None if prev_avg is None else {"y": round(y(prev_avg), 1), "v": st.nf(prev_avg), "label_y": prev_label_y},
    }


def _line_label_ys(a: float | None, b: float | None) -> tuple[float | None, float | None]:
    """Підписи ліній «середнє» і «було» справа від графіка: близькі розводимо вгору й униз."""
    if a is None or b is None:
        return (None if a is None else round(a + 4, 1)), (None if b is None else round(b + 4, 1))
    if abs(a - b) >= 22:
        return round(a + 4, 1), round(b + 4, 1)
    mid = (a + b) / 2
    upper, lower = round(mid - 4, 1), round(mid + 15, 1)
    return (upper, lower) if a <= b else (lower, upper)


def sparkline(values: list[float], *, prev: list[float] | None = None, width: int = 112, height: int = 36) -> dict | None:
    """Форма ряду для картки: лінія, заливка під нею, остання точка; `prev` — лінія «було»."""
    if len(values) < 2 or max(values) <= 0:
        return None
    pad = 3.0
    top = max([*values, *(prev or []), 1])
    span = max(len(values), len(prev or [])) - 1

    def points(series: list[float]) -> list[tuple[float, float]]:
        return [(pad + (width - pad * 2) * i / span, height - pad - (height - pad * 2) * v / top) for i, v in enumerate(series)]

    now = points(values)
    line = "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in now)
    return {
        "w": width,
        "h": height,
        "d": line,
        "area": f"{line} L{now[-1][0]:.1f},{height - pad:.1f} L{now[0][0]:.1f},{height - pad:.1f} Z",
        "last": {"x": round(now[-1][0], 1), "y": round(now[-1][1], 1)},
        "prev_d": ("M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in points(prev))) if prev and len(prev) > 1 else None,
    }


def running_total(values: list[int]) -> list[int]:
    out: list[int] = []
    total = 0
    for value in values:
        total += value
        out.append(total)
    return out


def forecast_chart(past: list[dict], future: list[dict], *, width: int = 1100, height: int = 290) -> dict | None:
    """Факт проти прогнозу й наступні дні з діапазоном; одна вісь, одна одиниця.

    `past`: day, actual, forecast (може бути None), anomaly, tip.
    `future`: day, value (може бути None), low, high (можуть бути None), tip.
    """
    days = len(past) + len(future)
    if days < 2:
        return None
    left, right, top, bottom = 46, 14, 16, 30
    pw, ph = width - left - right, height - top - bottom
    known = [row["actual"] for row in past] + [row["forecast"] for row in past if row["forecast"] is not None]
    for row in future:
        known += [v for v in (row["value"], row["high"]) if v is not None]
    vmax, step = nice_scale(max([*known, 1]))
    base = top + ph
    slot = pw / days

    def y(v: float) -> float:
        return base - v / vmax * ph

    def x(i: int) -> float:
        return left + slot * i + slot / 2

    def path(points: list[tuple[float, float]]) -> str | None:
        return ("M" + " L".join(f"{a:.1f},{b:.1f}" for a, b in points)) if len(points) > 1 else None

    slots = []
    actual_pts, forecast_pts = [], []
    for i, row in enumerate(past):
        actual_pts.append((x(i), y(row["actual"])))
        if row["forecast"] is not None:
            forecast_pts.append((x(i), y(row["forecast"])))
        slots.append(
            {
                "x": round(left + slot * i, 1),
                "w": round(slot, 1),
                "cx": round(x(i), 1),
                "ya": round(y(row["actual"]), 1),
                "yf": None if row["forecast"] is None else round(y(row["forecast"]), 1),
                "anomaly": row["anomaly"],
                "label": f"{row['day'].day:02d}.{row['day'].month:02d}" if row["day"].weekday() == 0 else "",
                "tip": row["tip"],
                "future": False,
            }
        )
    next_pts, high_pts, low_pts = [], [], []
    if forecast_pts:
        next_pts.append(forecast_pts[-1])
    for j, row in enumerate(future):
        i = len(past) + j
        if row["value"] is not None:
            next_pts.append((x(i), y(row["value"])))
        if row["high"] is not None and row["low"] is not None:
            high_pts.append((x(i), y(row["high"])))
            low_pts.append((x(i), y(row["low"])))
        slots.append(
            {
                "x": round(left + slot * i, 1),
                "w": round(slot, 1),
                "cx": round(x(i), 1),
                "ya": None,
                "yf": None if row["value"] is None else round(y(row["value"]), 1),
                "anomaly": False,
                "label": f"{row['day'].day:02d}.{row['day'].month:02d}" if row["day"].weekday() == 0 else "",
                "tip": row["tip"],
                "future": True,
            }
        )
    band = None
    if len(high_pts) > 1:
        ring = high_pts + low_pts[::-1]
        band = "M" + " L".join(f"{a:.1f},{b:.1f}" for a, b in ring) + " Z"
    return {
        "w": width,
        "h": height,
        "left": left,
        "right": width - right,
        "top": top,
        "base": base,
        "label_y": base + 18,
        "ticks": [{"v": st.nf(step * k), "y": round(y(step * k), 1)} for k in range(4)],
        "slots": slots,
        "actual_d": path(actual_pts),
        "forecast_d": path(forecast_pts),
        "next_d": path(next_pts),
        "band_d": band,
        "today_x": round(left + slot * len(past), 1) if past and future else None,
    }


def week_chart(weeks: list[dict], *, width: int = 800, height: int = 330) -> dict:
    """Стос по тижнях: цирконій · ПММА · титан · інше (знизу вгору)."""
    left, right, top, bottom = 46, 10, 22, 52
    pw, ph = width - left - right, height - top - bottom
    peak = max([sum(w["mats"].values()) for w in weeks] + [1])
    vmax, step = nice_scale(peak)
    base = top + ph
    n = max(len(weeks), 1)
    slot = pw / n
    bw = min(slot * 0.56, 60)
    bars = []
    for i, w in enumerate(weeks):
        cx = left + slot * i + slot / 2
        x = cx - bw / 2
        parts = [(g, w["mats"].get(g, 0) / vmax * ph) for g in ("zr", "pmma", "ti", "oth")]
        segs, top_y = _stack(x, bw, base, parts)
        total = sum(w["mats"].values())
        mon: date = w["monday"]
        tip = f"тиждень {mon.isocalendar()[1]} · з {mon.day:02d}.{mon.month:02d} · {st.nf(total)} од."
        tip += "".join(
            f" · {label} {st.nf(w['mats'].get(g, 0))}"
            for g, label in (("zr", "цирконій"), ("pmma", "ПММА"), ("ti", "титан"), ("oth", "інше"))
            if w["mats"].get(g)
        )
        if w["partial"]:
            tip += " · неповний"
        bars.append(
            {
                "key": mon.isoformat(),
                "x": round(x, 1),
                "w": round(bw, 1),
                "cx": round(cx, 1),
                "slot_x": round(left + slot * i, 1),
                "slot_w": round(slot, 1),
                "segs": segs,
                "top": round(top_y, 1),
                "total": total,
                "partial": w["partial"],
                "mats": dict(w["mats"]),
                "label": f"{mon.day:02d}.{mon.month:02d}",
                "sub": f"тиж. {mon.isocalendar()[1]}",
                "tip": tip,
            }
        )
    return {
        "w": width,
        "h": height,
        "left": left,
        "right": width - right,
        "base": base,
        "label_y": base + 17,
        "sub_y": base + 36,
        "ticks": [{"v": st.nf(step * k), "y": round(base - step * k / vmax * ph, 1)} for k in range(4)],
        "bars": bars,
        "hatch_y": base - 40,
    }


def heat_level(value: int, peak: int) -> int:
    """Номер кроку прозорості 0..4 для ненульової клітинки."""
    if value <= 0 or peak <= 0:
        return -1
    return sum(1 for b in _HEAT_BINS if value / peak > b)


def heatmap(
    grid: list[list[int]],
    kind: str,
    *,
    width: int = 860,
    height: int = 340,
    unit: str = "од.",
    frame: st.WorkFrame | None = None,
) -> dict:
    left, top = 34, 26
    cw = (width - left - 4) / 24
    ch = (height - top - 8) / 7
    peak = max((max(row) for row in grid), default=0)
    cells = []
    for dow in range(7):
        for hour in range(24):
            value = grid[dow][hour]
            level = heat_level(value, peak)
            cells.append(
                {
                    "dow": dow,
                    "hour": hour,
                    "x": round(left + cw * hour + 1, 1),
                    "y": round(top + ch * dow + 1, 1),
                    "w": round(cw - 2, 1),
                    "h": round(ch - 2, 1),
                    "v": value,
                    "op": HEAT_OPACITY[level] if level >= 0 else 0,
                    "tip": f"{st.DOW[dow]} · {hour:02d}:00–{(hour + 1) % 24:02d}:00 · {st.nf(value)} {unit}",
                }
            )
    work = frame or st.WorkFrame()
    return {
        "kind": kind,
        "w": width,
        "h": height,
        "cells": cells,
        "peak": peak,
        "hours": [{"x": round(left + cw * h + 2, 1), "t": f"{h:02d}"} for h in range(0, 24, 3)],
        "rows": [{"y": round(top + ch * d + ch / 2 + 4, 1), "t": st.DOW[d], "weekend": d >= 5} for d in range(7)],
        "frame": {
            "x": round(left + cw * work.start, 1),
            "y": round(top - 1 + ch * work.first_day, 1),
            "w": round(cw * (work.end - work.start), 1),
            "h": round(ch * (work.last_day - work.first_day + 1) + 2, 1),
        },
        "left": left,
        "head_y": top - 9,
    }


def share_rows(items: list[dict], *, value_key: str = "units", prev_key: str = "prev") -> list[dict]:
    """Горизонтальні смуги: ширина від максимуму поточного, риска «було»."""
    peak = max([i[value_key] for i in items] + [i.get(prev_key) or 0 for i in items] + [1])
    out = []
    for item in items:
        row = dict(item)
        row["pct_w"] = round(item[value_key] / peak * 100, 1)
        prev = item.get(prev_key)
        row["prev_w"] = None if prev is None else round(min(prev / peak * 100, 99.5), 1)
        out.append(row)
    return out
