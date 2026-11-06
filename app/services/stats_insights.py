"""«Головне за період» на «Огляді»: кілька речень замість пошуку очима по екрану.

Лише правила над числами, що вже стоять на екрані, без нових запитів і без вгадування
причин: речення каже, що змінилось, а не чому. Рядок з'являється, лише коли зміна
більша за поріг: дрібниця, подана як новина, навчила б не читати цей блок.

`cmp` — рядок існує лише в порівнянні; перемикач «порівняти» ховає його без запиту.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date

from app import stats as st

MAX_LINES = 6
TEMPO_FLAT_PCT = 3.0  # зміна темпу менша за це — «майже так само»
PEAK_MIN_PCT = 10  # пік, ледь вищий за середнє, не новина
CLIENT_MIN_UNITS = 20
CLIENT_MIN_PCT = 15.0
MATERIAL_MIN_POINTS = 2.0
REWORK_MIN_POINTS = 0.2


def plural(n: int, one: str, few: str, many: str) -> str:
    tail = n % 100
    if 11 <= tail <= 14:
        return many
    last = n % 10
    if last == 1:
        return one
    return few if 2 <= last <= 4 else many


def _points(value: float) -> str:
    return f"{abs(value):.1f}".replace(".", ",")


def _line(icon: str, tone: str, lead: str, rest: str, *, cmp: bool = False, qs: str | None = None, side: str | None = None) -> dict:
    return {"icon": icon, "tone": tone, "lead": lead, "rest": rest, "cmp": cmp, "qs": qs, "side": side}


def tempo_line(per_day: float | None, was: float | None, prev_label: str) -> dict | None:
    delta = st.delta_pct(per_day, was)
    if per_day is None or delta is None:
        return None
    lead = f"Темп {st.nf(per_day)} од. на день"
    if abs(delta) < TEMPO_FLAT_PCT:
        return _line("pace", "flat", lead, f"майже як у {prev_label}", cmp=True)
    word = "більше" if delta > 0 else "менше"
    return _line("pace", "good" if delta > 0 else "bad", lead, f"на {abs(round(delta))} % {word}, ніж у {prev_label}", cmp=True)


def peak_line(by_day: Mapping[date, Mapping], per_day: float | None) -> dict | None:
    totals = {d: v["lab"] + v["client"] for d, v in by_day.items() if v["works"]}
    if len(totals) < 2 or not per_day:
        return None
    day, top = max(totals.items(), key=lambda kv: (kv[1], kv[0]))
    above = round((top / per_day - 1) * 100)
    if above < PEAK_MIN_PCT:
        return None
    weekend = " (пт + сб + нд)" if day.weekday() == 4 else ""
    lead = f"Найбільший день — {st.DOW[day.weekday()]} {day.day:02d}.{day.month:02d}{weekend}"
    return _line("peak", "flat", lead, f"{st.nf(top)} од., на {above} % вище за середнє", side=f"day={day.isoformat()}")


def client_lines(now: Mapping[str, Mapping], was: Mapping[str, Mapping], qs_of) -> list[dict]:
    """Хто найбільше додав і хто найбільше просів, в одиницях. Рядки без імені не клієнт."""
    moves = []
    for key in {*now, *was}:
        if not key:
            continue
        current = now.get(key, {}).get("units", 0)
        before = was.get(key, {}).get("units", 0)
        change = current - before
        if abs(change) < CLIENT_MIN_UNITS:
            continue
        if before and abs(change) / before * 100 < CLIENT_MIN_PCT:
            continue
        label = (now.get(key) or was.get(key) or {}).get("label", key)
        moves.append((change, label, key, before))
    if not moves:
        return []
    lines = []
    change, label, key, before = max(moves, key=lambda m: (m[0], m[1]))
    if change > 0:
        rest = f"+{st.nf(change)} од." + (f" (було {st.nf(before)})" if before else ", раніше робіт не було")
        lines.append(_line("up", "good", f"Найбільше додав: {label}", rest, cmp=True, qs=qs_of(key)))
    change, label, key, before = min(moves, key=lambda m: (m[0], m[1]))
    if change < 0:
        lines.append(_line("down", "bad", f"Найбільше просів: {label}", f"−{st.nf(-change)} од. (було {st.nf(before)})", cmp=True, qs=qs_of(key)))
    return lines


def material_line(rows: list[dict]) -> dict | None:
    moved = [r for r in rows if r.get("dpp") is not None and abs(r["dpp"]) >= MATERIAL_MIN_POINTS]
    if not moved:
        return None
    row = max(moved, key=lambda r: abs(r["dpp"]))
    word = "зросла" if row["dpp"] > 0 else "впала"
    return _line("layers", "flat", f"{row['label']}: {row['share']} % одиниць", f"частка {word} на {_points(row['dpp'])} в.п.", cmp=True, qs=row.get("qs"))


def rework_line(pct: float | None, was_pct: float | None) -> dict | None:
    if pct is None or was_pct is None:
        return None
    change = pct - was_pct
    if abs(change) < REWORK_MIN_POINTS:
        return None
    word = "більше" if change > 0 else "менше"
    return _line("alert", "bad" if change > 0 else "good", f"Брак {_points(pct)} %", f"на {_points(change)} в.п. {word}", cmp=True, side="rework=1")


def gap_lines(cover_missing: int, noqty: int) -> list[dict]:
    """Те, що робить решту чисел неповними, стоїть першим."""
    lines = []
    if cover_missing:
        lines.append(_line("alert", "warn", f"Немає вкладок за {cover_missing} {plural(cover_missing, 'робочий день', 'робочі дні', 'робочих днів')}", "сума за період занижена"))
    if noqty:
        lines.append(_line("alert", "warn", f"{noqty} {plural(noqty, 'робота', 'роботи', 'робіт')} без кількості", "у суму не входять", side="noqty=1"))
    return lines


def overview_lines(
    *,
    per_day: float | None,
    was_per_day: float | None,
    by_day: Mapping[date, Mapping],
    clients_now: Mapping[str, Mapping],
    clients_was: Mapping[str, Mapping],
    client_qs,
    mat_rows: list[dict],
    rework_pct: float | None,
    rework_was_pct: float | None,
    cover_missing: int,
    noqty: int,
    prev_label: str,
) -> list[dict]:
    candidates: list[dict | None] = [
        *gap_lines(cover_missing, noqty),
        tempo_line(per_day, was_per_day, prev_label),
        peak_line(by_day, per_day),
        *client_lines(clients_now, clients_was, client_qs),
        rework_line(rework_pct, rework_was_pct),
        material_line(mat_rows),
    ]
    return [line for line in candidates if line is not None][:MAX_LINES]
