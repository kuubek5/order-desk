"""«Виробіток»: вердикт по кожному рядку й нові правила власника (22.11.26).

Правила, які власник назвав дослівно за змістом:
  1. не прорахована робота не рахується — вона не виконана (рядок незробленої
     роботи адміністратори щодня переносять з учора на сьогодні);
  2. переробка буває тричі й уп'ятеро, але рахується ОДИН раз.

Бойовий приклад — наряд 36652: рядок без Sum3D їздив по вкладках 23–30.10 (не
рахувався — правильно), його прорахували 27.10, а 02.11 прорахували ЗНОВУ; у
колонці «Який раз» порожньо в обох, тому стара логіка рахувала роботу двічі.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime

from sqlalchemy.orm import Session

from app.models import ActionLog, Order, VyrobitokCell, VyrobitokDay
from app.services import vyrobitok, vyrobitok_rows as vr
from app.services.order_dates import order_date
from app.services.vyrobitok import compute_month, recount_month, rules_v2, set_rules_v2
from app.stats import has_sum3d_time, parse_int_safe
from tests.test_vyrobitok import _db, _materials

OCT = (date(2026, 10, 1), date(2026, 10, 31))
NOV = (date(2026, 11, 1), date(2026, 11, 30))
KIND = "анатомія глазурована гвинтова(35-37)"


def _row(db: Session, tab: str, *, source="lab", material=None, qty="3", sum3d=None,
         naryad=None, kind=None, color="моно а2", mill_count=None, client=None, row=1,
         archived=False) -> Order:
    order = Order(
        source=source, sheet_tab=tab, row_number=row, material_id=material, quantity=qty,
        sum3d_id=sum3d, work_order_no=naryad, kind=kind, material_color=color,
        mill_count=mill_count, client_name=client, status="нове",
    )
    if archived:
        order.archived_at = datetime(2026, 11, 1, 13, 14)
    db.add(order)
    db.commit()
    return order


def _sum(db: Session, span, *, v2: bool) -> dict[str, int]:
    verdicts, _months = vr.judge(db, *span, v2=v2)
    out: dict[str, int] = defaultdict(int)
    for v in verdicts:
        if v.col_key:
            out[v.col_key] += v.units
    return dict(out)


def _reasons(db: Session, span, *, v2: bool) -> dict[int, str | None]:
    return {v.order_id: v.reason for v in vr.judge(db, *span, v2=v2)[0]}


# ── Старі правила не змінились ──────────────────────────────────────────────


def _old_live_units(db: Session, first: date, last: date):
    """Цикл `vyrobitok._live_units`, яким він був до 22.11.26, — еталон."""
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    orders = db.scalars(
        select(Order).options(selectinload(Order.material)).where(Order.archived_at.is_(None))
    ).all()
    auto: dict[tuple[date, str], int] = defaultdict(int)
    live_days: set[date] = set()
    months: set[tuple[int, int]] = set()
    for order in orders:
        d = order_date(order)
        months.add((d.year, d.month))
        if not (first <= d <= last):
            continue
        n = parse_int_safe(order.mill_count)
        if n is not None and n >= 2:
            continue
        live_days.add(d)
        if order.source == "lab" and not has_sum3d_time(order.sum3d_id):
            continue
        material = order.material
        key = vyrobitok.MATERIAL_KEY_BY_NAME.get(material.name) if material else None
        if key is None or key not in vyrobitok.ORDER_MATERIALS:
            continue
        col_key = f"{'lab' if order.source == 'lab' else 'mail'}_{key}"
        auto[(d, col_key)] += parse_int_safe(order.quantity) or 0
    return dict(auto), live_days, months


def test_old_rules_give_exactly_the_old_numbers():
    """Старий рахунок відтворено рядок у рядок — ним звіряємо «було → стало»."""
    db = _db()
    ids = _materials(db)
    zr, pmma, ti, slm = ids["Цирконій"], ids["ПММА"], ids["Титан"], ids["СЛМ"]
    _row(db, "02.11.26", material=zr, qty="3", sum3d="12-54-26", naryad="36652", kind=KIND, row=1)
    _row(db, "02.11.26", material=zr, qty="2", sum3d=None, naryad="40001", row=2)           # не взяли
    _row(db, "02.11.26", material=zr, qty="4", sum3d="немає папки", naryad="40002", row=3)  # текст, не час
    _row(db, "02.11.26", material=zr, qty="5", sum3d="10-00-00", mill_count="2", row=4)     # «Який раз»
    _row(db, "02.11.26", material=None, qty="8", sum3d="11-00-00", color="загадка", row=5)  # невідомий
    _row(db, "02.11.26", material=slm, qty="9", sum3d="11-10-00", row=6)                    # не колонка робіт
    _row(db, "02.11.26", material=pmma, qty="", sum3d="11-20-00", row=7)                    # порожня к-сть
    _row(db, "02.11.26", material=pmma, qty="2+1", sum3d="11-30-00", row=8)                 # не число
    _row(db, "03.11.26", source="email", material=zr, qty="6", sum3d=None, client="К", row=9)
    _row(db, "03.11.26", source="sheet_client", material=ti, qty="1", sum3d="09-00-00", row=10)
    _row(db, "03.11.26", material=zr, qty="7", sum3d="09-10-00", mill_count="3", row=11)    # день лише з переробки
    _row(db, "04.11.26", material=zr, qty="7", sum3d="09-10-00", mill_count="3", row=12)
    _row(db, "27.10.26", material=zr, qty="3", sum3d="18-53-11", naryad="36652", kind=KIND, row=13)
    _row(db, "05.11.26", material=zr, qty="1", sum3d="08-00-00", archived=True, row=14)

    set_rules_v2(db, False)         # аварійний відкат = старий рахунок рядок у рядок
    db.commit()
    for span in (NOV, OCT, (date(2026, 11, 2), date(2026, 11, 2))):
        auto, live_days, months = vyrobitok._live_units(db, *span)
        want_auto, want_days, want_months = _old_live_units(db, *span)
        assert dict(auto) == want_auto and live_days == want_days and months == want_months


def test_material_keys_are_the_same_in_both_modules():
    assert vr.MATERIAL_KEY_BY_NAME == vyrobitok.MATERIAL_KEY_BY_NAME
    assert vr.ORDER_MATERIALS == vyrobitok.ORDER_MATERIALS


# ── Правило 1: не прораховане не рахується ──────────────────────────────────


def test_carried_over_row_counts_only_on_the_day_it_was_calculated():
    """Адміністратори переносять незроблений рядок з учора на сьогодні: поки
    Sum3D немає, він не рахується ніде — і за старими, і за новими правилами."""
    db = _db()
    zr = _materials(db)["Цирконій"]
    for n, tab in enumerate(("23.10.26", "26.10.26", "28.10.26", "29.10.26", "30.10.26")):
        _row(db, tab, material=zr, naryad="36652", kind=KIND, row=n + 1, archived=True)
    done = _row(db, "27.10.26", material=zr, sum3d="18-53-11", naryad="36652", kind=KIND, row=9)
    for v2 in (False, True):
        assert _sum(db, OCT, v2=v2) == {"lab_zr": 3}
        assert _reasons(db, OCT, v2=v2) == {done.id: None}


def test_mail_row_without_sum3d_is_not_work_done_under_new_rules():
    db = _db()
    zr = _materials(db)["Цирконій"]
    waiting = _row(db, "12.11.26", source="email", material=zr, qty="6", client="К", row=60)
    done = _row(db, "12.11.26", source="email", material=zr, qty="2", sum3d="14-02-07",
                client="Б", row=61)
    assert _sum(db, NOV, v2=False) == {"mail_zr": 8}, "стара логіка рахувала й непрораховане"
    assert _sum(db, NOV, v2=True) == {"mail_zr": 2}
    assert _reasons(db, NOV, v2=True) == {waiting.id: vr.REASON_NO_SUM3D, done.id: None}


# ── Правило 2: переробка — один раз ─────────────────────────────────────────


def test_recalculated_job_is_counted_once_even_with_an_empty_mill_count():
    """Наряд 36652: 27.10 Sum3D 18-53-11, 02.11 Sum3D 12-54-26, «Який раз» порожній."""
    db = _db()
    zr = _materials(db)["Цирконій"]
    first = _row(db, "27.10.26", material=zr, sum3d="18-53-11", naryad="36652", kind=KIND, row=13)
    again = _row(db, "02.11.26", material=zr, sum3d="12-54-26", naryad="36652", kind=KIND, row=4)

    assert _sum(db, OCT, v2=False) == {"lab_zr": 3} and _sum(db, NOV, v2=False) == {"lab_zr": 3}
    assert _sum(db, OCT, v2=True) == {"lab_zr": 3}, "перший прорахунок рахується"
    assert _sum(db, NOV, v2=True) == {"lab_zr": 0}, "переробка — ні"
    verdict = next(v for v in vr.judge(db, *NOV, v2=True)[0] if v.order_id == again.id)
    assert verdict.reason == vr.REASON_REPEAT and "27.10" in verdict.reason_label
    assert _reasons(db, OCT, v2=True)[first.id] is None


def test_third_and_fifth_remake_still_count_once():
    db = _db()
    zr = _materials(db)["Цирконій"]
    days = ("03.11.26", "05.11.26", "09.11.26", "12.11.26", "16.11.26")
    for n, tab in enumerate(days):
        _row(db, tab, material=zr, qty="2", sum3d=f"1{n}-00-00", naryad="40500", kind="анатомія", row=n + 1)
    assert _sum(db, NOV, v2=False) == {"lab_zr": 10}
    assert _sum(db, NOV, v2=True) == {"lab_zr": 2}


def test_first_calculation_already_in_the_archive_still_makes_the_second_a_remake():
    """Лаба чистить старі вкладки: перший прорахунок до часу переробки в архіві."""
    db = _db()
    zr = _materials(db)["Цирконій"]
    _row(db, "27.10.26", material=zr, sum3d="18-53-11", naryad="36652", kind=KIND, row=13, archived=True)
    _row(db, "02.11.26", material=zr, sum3d="12-54-26", naryad="36652", kind=KIND, row=4)
    assert _sum(db, NOV, v2=True) == {"lab_zr": 0}


def test_other_items_of_the_same_work_order_are_not_remakes():
    """«2й етап» (коронки після абатментів) і різні зуби одного наряду — різні вироби."""
    db = _db()
    ids = _materials(db)
    zr, pmma = ids["Цирконій"], ids["ПММА"]
    _row(db, "18.11.26", material=zr, qty="2", sum3d="09-54-25", naryad="40380", kind="абатмент",
         color="1000", row=8)
    _row(db, "19.11.26", material=zr, qty="2", sum3d="12-10-58", naryad="40380",
         kind="анатомія гвинтова", color="моно а3", row=22)
    _row(db, "16.11.26", material=pmma, qty="1", sum3d="11-59-31", naryad="39903", kind="анатомія",
         color="пмма", row=5)
    _row(db, "16.11.26", material=zr, qty="1", sum3d="11-13-20", naryad="39903", kind="анатомія",
         color="моно а3", row=8)
    assert _sum(db, NOV, v2=True) == {"lab_zr": 5, "lab_pmma": 1}
    assert set(_reasons(db, NOV, v2=True).values()) == {None}


def test_same_calculation_seen_on_two_days_is_a_moved_row_not_a_remake():
    """Рядок перенесли вже ПРОРАХОВАНИМ (той самий Sum3D): це не переробка. Його
    не виключаємо — лише позначаємо, щоб звірка бачила можливий подвійний рахунок."""
    db = _db()
    zr = _materials(db)["Цирконій"]
    _row(db, "18.11.26", material=zr, qty="2", sum3d="20-11-01", naryad="40751", kind="анатомія", row=28)
    moved = _row(db, "19.11.26", material=zr, qty="2", sum3d="20-11-01", naryad="40751",
                 kind="анатомія", row=3)
    assert _sum(db, NOV, v2=True) == {"lab_zr": 4}
    verdict = next(v for v in vr.judge(db, *NOV, v2=True)[0] if v.order_id == moved.id)
    assert verdict.counted and verdict.note == "той самий прорахунок стоїть і в 18.11 — пораховано в обох днях"


def test_a_calculated_row_moved_to_the_next_day_says_where_it_came_from():
    """Рядок ВИРІЗАЛИ з учорашньої вкладки (там він тепер в архіві) і вставили
    в сьогоднішню: живий лише один, рахується він. Звірка бачить це окремо від
    випадку, коли рядок стоїть живим у двох вкладках."""
    from app.services import vyrobitok_audit

    db = _db()
    zr = _materials(db)["Цирконій"]
    _row(db, "18.11.26", material=zr, qty="2", sum3d="20-11-01", naryad="40751", kind="анатомія",
         row=28, archived=True)
    moved = _row(db, "19.11.26", material=zr, qty="2", sum3d="20-11-01", naryad="40751",
                 kind="анатомія", row=3)
    _row(db, "18.11.26", material=zr, qty="4", sum3d="21-00-00", naryad="40760", kind="анатомія", row=30)
    _row(db, "19.11.26", material=zr, qty="4", sum3d="21-00-00", naryad="40760", kind="анатомія", row=4)

    assert _sum(db, NOV, v2=True) == {"lab_zr": 10}
    verdict = next(v for v in vr.judge(db, *NOV, v2=True)[0] if v.order_id == moved.id)
    assert verdict.counted and verdict.note == "прорахований рядок перенесено з 18.11"

    report = vyrobitok_audit.audit(db, 2026, 11)
    assert report["прорахований_рядок_перенесено"]["рядків"] == 1
    assert report["пораховано_в_двох_днях_той_самий_прорахунок"]["рядків"] == 1
    assert report["пораховано_в_двох_днях_той_самий_прорахунок"]["одиниць"] == 4


def test_rows_without_a_work_order_number_are_never_matched_as_remakes():
    db = _db()
    zr = _materials(db)["Цирконій"]
    for n, tab in enumerate(("11.11.26", "12.11.26")):
        _row(db, tab, source="email", material=zr, qty="2", sum3d=f"1{n}-00-00", client="К",
             color="mono a3", row=n + 60)
    assert _sum(db, NOV, v2=True) == {"mail_zr": 4}


def test_mail_rework_keeps_counting_until_the_owner_decides():
    """03.11.26 власник вирішив: переробка з листа йде в загальну кількість. Нове
    «рахується один раз» цьому суперечить — мовчки не перевертаємо, лише позначаємо."""
    db = _db()
    zr = _materials(db)["Цирконій"]
    first = _row(db, "10.11.26", source="email", material=zr, qty="2", sum3d="10-00-00", client="О", row=60)
    redo = _row(db, "12.11.26", source="email", material=zr, qty="2", sum3d="11-00-00", client="О", row=61)
    db.add(ActionLog(order_id=redo.id, action_type="accept", field="rework_of", new_value=str(first.id)))
    db.commit()
    assert _sum(db, NOV, v2=True) == {"mail_zr": 4}
    verdict = next(v for v in vr.judge(db, *NOV, v2=True)[0] if v.order_id == redo.id)
    assert verdict.counted and verdict.note == vr.NOTE_MAIL_REWORK


# ── «Не пораховано й чому» ──────────────────────────────────────────────────


def test_every_silent_zero_gets_a_reason_on_the_screen():
    db = _db()
    ids = _materials(db)
    zr, slm = ids["Цирконій"], ids["СЛМ"]
    _row(db, "16.11.26", source="email", material=None, qty="4", sum3d="16-00-00", client="С",
         color="а3 4 одиниці", row=99)
    _row(db, "16.11.26", source="email", material=zr, qty="", sum3d="16-10-00", client="З", row=94)
    _row(db, "16.11.26", material=zr, qty="2", sum3d=None, naryad="40001", row=2)
    _row(db, "16.11.26", material=zr, qty="5", sum3d="10-00-00", naryad="40002", mill_count="2", row=3)
    _row(db, "16.11.26", material=slm, qty="9", sum3d="11-10-00", naryad="40003", row=4)
    _row(db, "16.11.26", material=zr, qty="1", sum3d="12-00-00", naryad="40004", row=5)

    grid = compute_month(db, 2026, 11, persist=False)
    by_row = {item["row"]: item for item in grid.skipped}
    assert sorted(by_row) == [2, 3, 4, 94, 99], "порахований рядок 5 у переліку не стоїть"
    assert "не впізнано" in by_row[99]["reason"] and by_row[99]["bucket"] == "пошта"
    assert "кількість" in by_row[94]["reason"]
    assert "Sum3D" in by_row[2]["reason"] and by_row[2]["who"] == "40001"
    assert "Який раз" in by_row[3]["reason"]
    assert "не фрезерна" in by_row[4]["reason"]
    assert all(item["counted"] is False and item["day"] == "16.11" for item in grid.skipped)


# ── Правила діють одразу; закриті дні чекають перерахунку ───────────────────


def test_rules_apply_by_default_and_closed_days_wait_for_a_recount():
    """Правила власника — не опція: чиста база рахує ними одразу. Але день,
    закритий ЩЕ за старим рахунком, сам числа не міняє — лише перерахунком."""
    db = _db()
    zr = _materials(db)["Цирконій"]
    _row(db, "27.10.26", material=zr, sum3d="18-53-11", naryad="36652", kind=KIND, row=13)
    _row(db, "02.11.26", material=zr, sum3d="12-54-26", naryad="36652", kind=KIND, row=4)
    _row(db, "02.11.26", material=zr, qty="5", sum3d="13-00-00", naryad="40100", row=5)
    day = date(2026, 11, 2)

    assert rules_v2(db) is True, "без жодного перемикання"
    assert compute_month(db, 2026, 11, persist=False).totals["lab_zr"] == 5, "відкритий день — одразу"

    set_rules_v2(db, False)                         # день закрився до релізу, за старим рахунком
    db.commit()
    vyrobitok._snapshot_orders(db, day)
    db.add(VyrobitokDay(day=day, orders_frozen_at=datetime(2026, 11, 3, 7, 40)))
    set_rules_v2(db, True)
    db.commit()

    grid = compute_month(db, 2026, 11, persist=False)
    assert grid.rules_v2 is True
    assert grid.totals["lab_zr"] == 8, "закритий день сам не перераховується"
    assert any("уже пораховано 27.10" in item["reason"] for item in grid.skipped)

    assert recount_month(db, 2026, 11) == {"days": 1, "skipped_empty": 0, "partial": []}
    assert compute_month(db, 2026, 11, persist=False).totals["lab_zr"] == 5
    assert db.get(VyrobitokDay, day).orders_frozen_at is not None, "позначка закриття лишилась"

    set_rules_v2(db, False)
    db.commit()
    recount_month(db, 2026, 11)
    assert compute_month(db, 2026, 11, persist=False).totals["lab_zr"] == 8, "аварійний відкат — тим самим шляхом"


def test_recount_leaves_a_cleaned_out_day_alone():
    """Лаба почистила вкладку: живих робіт немає, і перерахунок зробив би з
    числа нуль. Такий день не чіпаємо."""
    db = _db()
    _materials(db)
    day = date(2026, 11, 4)
    db.add(VyrobitokCell(day=day, col_key="lab_zr", auto_value=48))
    db.add(VyrobitokDay(day=day, orders_frozen_at=datetime(2026, 11, 5, 7, 40)))
    db.commit()
    assert recount_month(db, 2026, 11) == {"days": 0, "skipped_empty": 1, "partial": []}
    assert compute_month(db, 2026, 11, persist=False).totals["lab_zr"] == 48


def test_recount_leaves_a_day_alone_when_a_counted_row_vanished_after_closing():
    """День закрився з 8 одиницями, потім один прорахований рядок зник з
    таблиці (архів). Серед живих лишилось 5 — перерахунок зробив би з 8 п'ять,
    і вимикач назад цього не повернув би. День не чіпаємо й називаємо."""
    from app.routers import vyrobitok as routes
    from app.services import vyrobitok_audit

    db = _db()
    zr = _materials(db)["Цирконій"]
    gone = _row(db, "04.11.26", material=zr, qty="3", sum3d="10-00-00", naryad="40001", row=4)
    _row(db, "04.11.26", material=zr, qty="5", sum3d="11-00-00", naryad="40002", row=5)
    # незроблений рядок, який перенесли на завтра, — зник, але й не рахувався
    moved = _row(db, "05.11.26", material=zr, qty="2", naryad="40003", row=4)
    _row(db, "05.11.26", material=zr, qty="6", sum3d="12-00-00", naryad="40004", row=5)
    for day in (date(2026, 11, 4), date(2026, 11, 5)):
        vyrobitok._snapshot_orders(db, day)
        db.add(VyrobitokDay(day=day, orders_frozen_at=datetime(2026, 11, 6, 7, 40)))
    gone.archived_at = datetime(2026, 11, 7, 9, 0)
    moved.archived_at = datetime(2026, 11, 7, 9, 0)
    db.commit()

    assert vr.vanished_rows(db, *NOV, v2=False) == {date(2026, 11, 4): 1}
    report = vyrobitok_audit.audit(db, 2026, 11)
    assert report["закриті_дні_яких_перерахунок_не_чіпатиме"] == [{"день": "04.11", "зниклих_рядків": 1}]

    assert recount_month(db, 2026, 11) == {"days": 1, "skipped_empty": 0, "partial": [date(2026, 11, 4)]}
    grid = compute_month(db, 2026, 11, persist=False)
    assert {r["dayn"]: r["cells"]["lab_zr"]["num"] for r in grid.rows if r["dayn"] in (4, 5)} == {4: 8, 5: 6}

    resp = routes.post_vyrobitok_recount(_req(_user(db, "адмін")), year=2026, month=11, db=db)
    assert "число впало б: 04.11" in _html(resp)


def test_recount_keeps_the_operators_own_number():
    db = _db()
    zr = _materials(db)["Цирконій"]
    _row(db, "04.11.26", material=zr, qty="5", sum3d="11-00-00", naryad="40002", row=5)
    day = date(2026, 11, 4)
    vyrobitok._snapshot_orders(db, day)
    db.add(VyrobitokDay(day=day, orders_frozen_at=datetime(2026, 11, 5, 7, 40)))
    db.commit()
    vyrobitok.set_cell(db, day, "lab_zr", 9)

    recount_month(db, 2026, 11)
    cell = next(r for r in compute_month(db, 2026, 11, persist=False).rows if r["dayn"] == 4)["cells"]["lab_zr"]
    assert cell["num"] == 9 and cell["edited"]


# ── Перерахунок закритих днів: лише адміністратор ───────────────────────────


def _user(db: Session, role: str):
    from app.models import User

    user = User(username=f"u-{role}", password_hash="x", full_name="Тест", role=role)
    db.add(user)
    db.commit()
    return user


def _req(user):
    from types import SimpleNamespace

    return SimpleNamespace(
        session={"user_id": user.id, "epoch": 0}, headers={},
        client=SimpleNamespace(host="192.168.1.50"),
    )


def _html(response) -> str:
    return response.body.decode("utf-8")


def test_admin_recounts_closed_days_from_the_screen():
    from app.routers import vyrobitok as routes
    from app.routers.deps import templates

    db = _db()
    zr = _materials(db)["Цирконій"]
    _row(db, "27.10.26", material=zr, sum3d="18-53-11", naryad="36652", kind=KIND, row=13)
    _row(db, "02.11.26", material=zr, sum3d="12-54-26", naryad="36652", kind=KIND, row=4)
    day = date(2026, 11, 2)
    set_rules_v2(db, False)                         # день закрився за старим рахунком
    db.commit()
    vyrobitok._snapshot_orders(db, day)
    db.add(VyrobitokDay(day=day, orders_frozen_at=datetime(2026, 11, 3, 7, 40)))
    set_rules_v2(db, True)
    db.commit()
    admin = _user(db, "адмін")

    page = templates.env.get_template("_vyrobitok_body.html").render(
        routes._grid_context(db, admin, 2026, 11, persist=False)
    )
    assert "/vyrobitok/recount" in page and "уже пораховано 27.10" in page
    assert "/vyrobitok/rules" not in page, "вимикача правил на екрані немає"
    assert compute_month(db, 2026, 11, persist=False).totals["lab_zr"] == 3, "закритий день ще чекає"

    resp = routes.post_vyrobitok_recount(_req(admin), year=2026, month=11, db=db)
    assert resp.status_code == 200
    page = _html(resp)
    assert "Перераховано закритих днів: 1." in page
    assert '<details class="vt-skip" open>' in page, "після дії блок лишається розгорнутим"
    assert compute_month(db, 2026, 11, persist=False).totals["lab_zr"] == 0


def test_operator_cannot_recount():
    """Це гроші всього цеху: перерахунок закритих днів — лише адміністратор.
    Оператор кнопки не бачить, а прямий запит дістає 403."""
    import pytest
    from fastapi import HTTPException

    from app.routers import vyrobitok as routes
    from app.routers.deps import templates

    db = _db()
    zr = _materials(db)["Цирконій"]
    _row(db, "02.11.26", material=zr, sum3d="12-54-26", naryad="36652", kind=KIND, row=4)
    operator = _user(db, "оператор")

    with pytest.raises(HTTPException) as denied:
        routes.post_vyrobitok_recount(_req(operator), year=2026, month=11, db=db)
    assert denied.value.status_code == 403

    page = templates.env.get_template("_vyrobitok_body.html").render(
        routes._grid_context(db, operator, 2026, 11, persist=False)
    )
    assert "Не пораховано й чому" in page and "Як рахується" in page
    assert "/vyrobitok/recount" not in page


def test_recount_route_stays_behind_the_section_pin():
    from app.routers import vyrobitok as routes
    from app.settings_store import set_setting

    db = _db()
    zr = _materials(db)["Цирконій"]
    _row(db, "02.11.26", material=zr, qty="5", sum3d="13-00-00", naryad="40100", row=5)
    day = date(2026, 11, 2)
    db.add(VyrobitokCell(day=day, col_key="lab_zr", auto_value=99))
    db.add(VyrobitokDay(day=day, orders_frozen_at=datetime(2026, 11, 3, 7, 40)))
    admin = _user(db, "адмін")
    set_setting(db, "vyrobitok_pin", "2468")
    db.commit()

    resp = routes.post_vyrobitok_recount(_req(admin), year=2026, month=11, db=db)
    assert resp.status_code == 303
    assert compute_month(db, 2026, 11, persist=False).totals["lab_zr"] == 99, "нічого не перераховано"


# ── Звірка «було → стало» (MCP kmill_vyrobitok_audit) ───────────────────────


def test_audit_shows_screen_old_and_new_numbers_without_writing_anything():
    from app.services import vyrobitok_audit

    db = _db()
    mats = _materials(db)
    zr = mats["Цирконій"]
    _row(db, "27.10.26", material=zr, sum3d="18-53-11", naryad="36652", kind=KIND, row=13)
    _row(db, "02.11.26", material=zr, sum3d="12-54-26", naryad="36652", kind=KIND, row=4)
    _row(db, "02.11.26", material=zr, qty="5", sum3d="13-00-00", naryad="40100", row=5)
    _row(db, "02.11.26", source="sheet_client", material=zr, qty="4", client="Петренко Іван", row=40)
    _row(db, "02.11.26", source="sheet_client", material=None, qty="6", sum3d="14-00-00",
         client="Сидоренко Олена", color="vvv", row=41)
    cells_before = db.query(VyrobitokCell).count()

    report = vyrobitok_audit.audit(db, 2026, 11)

    assert db.query(VyrobitokCell).count() == cells_before, "звірка нічого не пише"
    assert report["нові_правила_ввімкнено"] is True
    assert report["разом"]["екран"]["lab_zr"] == 5, "відкритий день уже за чинними правилами"
    assert report["разом"]["живі_старі"]["lab_zr"] == 8
    assert report["разом"]["нові"]["lab_zr"] == 5
    assert report["разом"]["живі_старі"]["mail_zr"] == 4 and report["разом"]["нові"]["mail_zr"] == 0
    day = next(d for d in report["дні_з_різницею"] if d["день"] == "02.11")
    assert day["колонки"]["lab_zr"] == {"екран": 5, "живі_старі": 8, "нові": 5}
    removed = {item["хто"]: item["чому"] for item in report["нові_правила_знімуть"]["приклади"]}
    assert "уже пораховано 27.10" in removed["наряд 36652"]
    assert "клієнт П.І." in removed, "ім'я клієнта — лише ініціалами"
    assert report["мовчазні_нулі"]["рядків"] == 1
    assert "Петренко" not in str(report) and "Сидоренко" not in str(report)

