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
    for mult in (1, 2, 2.5, 3, 4, 5, 6, 8, 10):
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
) -> dict:
    left, right, top, bottom = (34, 6, 22, 30) if compact else (46, 10, 30, 46)
    pw, ph = width - left - right, height - top - bottom
    totals = [by_day[d]["lab"] + by_day[d]["client"] for d in days]
    peak = max([*totals, avg or 0, prev_avg or 0, 1])
    vmax, step = nice_scale(peak)
    base = top + ph

    def y(v: float) -> float:
        return base - v / vmax * ph

    n = max(len(days), 1)
    slot = pw / n
    bw = min(slot * (0.66 if compact else 0.62), 46)
    many = n > 35
    peak_total = max(totals) if totals else 0
    bars = []
    for i, d in enumerate(days):
        cx = left + slot * i + slot / 2
        x = cx - bw / 2
        lab, client = by_day[d]["lab"], by_day[d]["client"]
        total = lab + client
        has = bool(by_day[d]["works"]) or cover[i]
        segs, top_y = _stack(x, bw, base, [("lab", lab / vmax * ph), ("cli", client / vmax * ph)])
        if compact:
            label = f"{d.day:02d}.{d.month:02d}" if (d.weekday() == 0 or i == 0) else ""
            sub = ""
        elif many:
            label = f"{d.day:02d}.{d.month:02d}" if (d.weekday() == 0 or i == 0) else ""
            sub = ""
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
                "has": has,
                "empty": not by_day[d]["works"],
                "label": label,
                "sub": sub,
                "tip": tip,
                "show_total": (not compact) and total > 0 and total == peak_total,
            }
        )
    hatch_h = 30 if compact else 46
    return {
        "w": width,
        "h": height,
        "left": left,
        "right": width - right,
        "top": top,
        "base": base,
        "label_y": base + (16 if compact else 17),
        "sub_y": base + 32,
        "ticks": [{"v": st.nf(step * k), "y": round(y(step * k), 1)} for k in range(4)],
        "bars": bars,
        "hatch_y": base - hatch_h,
        "hatch_h": hatch_h,
        "avg": None if avg is None else {"y": round(y(avg), 1), "v": st.nf(avg)},
        "prev": None if prev_avg is None else {"y": round(y(prev_avg), 1), "v": st.nf(prev_avg)},
    }


def week_chart(weeks: list[dict], *, width: int = 1080, height: int = 330) -> dict:
    """Стос по тижнях: цирконій · ПММА · титан · інше (знизу вгору)."""
    left, right, top, bottom = 46, 10, 22, 40
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
        "sub_y": base + 32,
        "ticks": [{"v": st.nf(step * k), "y": round(base - step * k / vmax * ph, 1)} for k in range(4)],
        "bars": bars,
        "hatch_y": base - 40,
    }


def heat_level(value: int, peak: int) -> int:
    """Номер кроку прозорості 0..4 для ненульової клітинки."""
    if value <= 0 or peak <= 0:
        return -1
    return sum(1 for b in _HEAT_BINS if value / peak > b)


def heatmap(grid: list[list[int]], kind: str, *, width: int = 860, height: int = 340) -> dict:
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
                    "tip": f"{st.DOW[dow]} · {hour:02d}:00–{(hour + 1) % 24:02d}:00 · {st.nf(value)} од.",
                }
            )
    w0, w1 = st.WORK_HOURS
    return {
        "kind": kind,
        "w": width,
        "h": height,
        "cells": cells,
        "peak": peak,
        "hours": [{"x": round(left + cw * h + 2, 1), "t": f"{h:02d}"} for h in range(0, 24, 3)],
        "rows": [{"y": round(top + ch * d + ch / 2 + 4, 1), "t": st.DOW[d], "weekend": d >= 5} for d in range(7)],
        "frame": {
            "x": round(left + cw * w0, 1),
            "y": top - 1,
            "w": round(cw * (w1 - w0), 1),
            "h": round(ch * len(st.WORK_WEEKDAYS) + 2, 1),
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
