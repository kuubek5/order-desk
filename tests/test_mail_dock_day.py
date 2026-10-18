"""День вкладки в доці «Нові з пошти» (власник 18.10.26).

Док показував лише сьогоднішню вкладку, і порахувати вчорашню чи завтрашню
лабу з екрана пошти було не можна. Тепер у шапці доку ті самі «Вчора /
Сьогодні / Завтра», що в періодах черги, і ‹ › по днях з роботами.

Що ламається тихо:
- типово док мусить лишатись рівно на сьогоднішній вкладці (як було);
- полл, що не несе день, перескочив би на сьогодні під руками оператора —
  день їде в КОЖНОМУ запиті (`day`), а порожнє `day` = «поля не було»;
- субота й неділя — не вкладки: будь-який день вихідних = пʼятниця
  (`business_day.tab_day`), «Вчора» в понеділок — пʼятниця;
- кука з учорашнім вибором уранці не діє — док знову на сьогодні;
- груповий Sum3D з доку на НЕсьогоднішній вкладці пише в ТУ вкладку.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app import business_day
from app.business_day import BUSINESS_TIMEZONE, business_tab_today, next_tab_day, prev_tab_day
from app.db import Base
from app.models import Order, User
from app.services import mail_mirror
from app.services import sheet_writeback as wb
from app.services.focus import toggle
from app.services.mail_mirror import (
    DOCK_DAY_COOKIE,
    dock_day_cookie,
    dock_day_from_cookie,
    dock_day_from_wire,
    mail_dock,
)
from tests.asgi_client import MiniClient
from tests.test_mail_dock_sum3d_batch import _wire_google, captured  # noqa: F401 — фікстура
from tests.test_settings_slabs_render import OPERATOR, app_db  # noqa: F401 — фікстура

HX = {"HX-Request": "true"}


def _pin(monkeypatch, day: date, hour: int = 12) -> None:
    moment = datetime(day.year, day.month, day.day, hour, 0, tzinfo=BUSINESS_TIMEZONE)
    monkeypatch.setattr(business_day, "business_now", lambda: moment)


def _database():
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return engine


def _tab(d: date) -> str:
    return d.strftime("%d.%m.%y")


def _lab(db, no, row, day: date, **kw):
    o = Order(source="lab", work_order_no=no, material_color="моно а3", quantity="2",
              status="нове", sheet_tab=_tab(day), row_number=row,
              job_code=f"2026-10-13_{no}", **kw)
    db.add(o)
    db.commit()
    return o


def _mail(db, client, day: date):
    o = Order(source="email", client_name=client, material_color="pmma a2", quantity="1",
              status="прийнято", sheet_tab=_tab(day))
    db.add(o)
    db.commit()
    return o


# Середа 14.10.26: вчора — вівторок, завтра — четвер; будній «звичайний» день.
WEDNESDAY = date(2026, 10, 14)
FRIDAY = date(2026, 10, 16)
SATURDAY = date(2026, 10, 17)
SUNDAY = date(2026, 10, 18)
MONDAY = date(2026, 10, 19)


# ── 1. Сервіс доку ──────────────────────────────────────────────────────────


def test_default_day_is_today_exactly_as_before(monkeypatch):
    _pin(monkeypatch, WEDNESDAY)
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        _mail(db, "Сьогоднішній", WEDNESDAY)
        _mail(db, "Вчорашній", prev_tab_day(WEDNESDAY))
        dock = mail_dock(db)
        assert [o.client_name for o in dock.orders] == ["Сьогоднішній"]
        assert dock.is_today and dock.wire_day == "today"
        assert dock.day == dock.today == WEDNESDAY
        # Явне «сьогодні» і «нічого не вибрано» — одне й те саме.
        assert [o.id for o in mail_dock(db, day=WEDNESDAY).orders] == [o.id for o in dock.orders]


def test_other_day_shows_that_tabs_lab_rows_in_sheet_order(monkeypatch):
    _pin(monkeypatch, WEDNESDAY)
    yesterday = prev_tab_day(WEDNESDAY)
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        _lab(db, "Y-2", 12, yesterday)
        _lab(db, "Y-1", 5, yesterday)
        _lab(db, "T-1", 3, WEDNESDAY)
        _mail(db, "Вчорашня пошта", yesterday)
        dock = mail_dock(db, scope="labonly", ready="all", day=yesterday)
        assert [o.work_order_no for o in dock.orders] == ["Y-1", "Y-2"], "порядок таблиці, лише вкладка дня"
        assert not dock.is_today and dock.wire_day == _tab(yesterday)
        assert dock.day_counts[yesterday] == 3 and dock.day_counts[WEDNESDAY] == 1
        # Канон на іншому дні — пошта ТІЄЇ вкладки.
        assert [o.client_name for o in mail_dock(db, day=yesterday).orders] == ["Вчорашня пошта"]
        # Порожній інший день не обіцяє «приймеш лист — зʼявиться тут».
        empty = mail_dock(db, day=next_tab_day(WEDNESDAY))
        assert empty.orders == [] and empty.empty_text.startswith("У вкладці 15.10.26")
        assert mail_dock(db, scope="labonly").empty_text != empty.empty_text


def test_arrows_step_over_days_with_work_and_today_is_always_reachable(monkeypatch):
    _pin(monkeypatch, WEDNESDAY)
    engine = _database()
    old = WEDNESDAY - timedelta(days=6)   # четвер минулого тижня
    ancient = WEDNESDAY - timedelta(days=60)  # поза вікном черги — Архів
    with Session(engine, expire_on_commit=False) as db:
        _lab(db, "OLD", 1, old)
        _lab(db, "ANCIENT", 1, ancient)
        _lab(db, "NEXT", 1, next_tab_day(WEDNESDAY))
        dock = mail_dock(db)  # у сьогоднішній вкладці робіт немає
        assert dock.prev_day == old, "стрілка ‹ — найближчий день із роботами, не порожній"
        assert dock.next_day == next_tab_day(WEDNESDAY)
        back = mail_dock(db, day=old)
        assert back.prev_day is None, "старіше за вікно черги стрілка не веде"
        assert back.next_day == WEDNESDAY, "сьогодні — завжди серед днів, навіть порожнє"
        assert back.other_day, "день поза Вчора/Сьогодні/Завтра малюється окремою кнопкою"


def test_weekend_days_are_the_friday_tab(monkeypatch):
    _pin(monkeypatch, MONDAY)
    assert dock_day_from_wire(_tab(SATURDAY)) == FRIDAY
    assert dock_day_from_wire(_tab(SUNDAY)) == FRIDAY
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        _lab(db, "FRI", 4, FRIDAY)
        dock = mail_dock(db, scope="labonly", ready="all", day=SATURDAY)
        assert dock.day == FRIDAY and [o.work_order_no for o in dock.orders] == ["FRI"]
        # «Вчора» в понеділок — пʼятниця, не порожня неділя (як у черзі).
        assert dock.yesterday == FRIDAY and dock.day == dock.yesterday and not dock.other_day


def test_on_saturday_today_is_the_friday_tab(monkeypatch):
    _pin(monkeypatch, SATURDAY)
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        _lab(db, "FRI", 4, FRIDAY)
        dock = mail_dock(db, scope="labonly", ready="all")
        assert dock.today == FRIDAY and dock.is_today
        assert dock.tomorrow == MONDAY and dock.yesterday == date(2026, 10, 15)
        # П'ятниця словами «сьогодні» — це те саме «сьогодні».
        assert dock_day_from_wire(_tab(FRIDAY)) is None


def test_wire_words_and_cookie_validity(monkeypatch):
    _pin(monkeypatch, WEDNESDAY)
    yesterday = prev_tab_day(WEDNESDAY)
    assert dock_day_from_wire("today") is None
    assert dock_day_from_wire("") is None
    assert dock_day_from_wire("сміття") is None
    assert dock_day_from_wire(_tab(yesterday)) == yesterday
    value = dock_day_cookie(yesterday)
    assert value == f"{_tab(yesterday)}_{_tab(WEDNESDAY)}"
    assert dock_day_cookie(None) == "" and dock_day_cookie(WEDNESDAY) == ""
    assert dock_day_from_cookie(value) == yesterday
    # Наступного робочого дня вчорашній вибір не діє — док знову на сьогодні.
    _pin(monkeypatch, next_tab_day(WEDNESDAY))
    assert dock_day_from_cookie(value) is None


# ── 2. Розмітка й JS ────────────────────────────────────────────────────────


def test_day_markup_has_own_word_for_today_and_js_carries_the_day():
    html = Path("app/templates/_mail_dock_controls.html").read_text(encoding="utf-8")
    assert "data-dock-day=" in html
    assert 'data-dock-day=""' not in html, "порожнє значення сервер читає як «поля не було»"
    assert "'today'" in html
    js = Path("app/static/js/mail.js").read_text(encoding="utf-8")
    # Один слухач додає день до КОЖНОГО запиту доку (полл, refresh, клік).
    assert re.search(r'htmx:configRequest[\s\S]{0,400}/mail/queue-mirror[\s\S]{0,200}parameters\.day', js)
    assert "/mail/queue-mirror?pick=1" in js
    page = Path("app/templates/mail_triage.html").read_text(encoding="utf-8")
    assert 'data-dock-day="{{ dock.wire_day }}"' in page


# ── 3. Через справжній застосунок ───────────────────────────────────────────


def _pressed(html: str) -> str:
    """Яка кнопка дня натиснута (стрілки — не вибір, у них свій клас)."""
    found = re.findall(r'<button type="button" data-dock-day="([^"]+)"\s+aria-pressed="true"', html)
    assert len(found) == 1, found
    return found[0]


def _section_day(page: str) -> str:
    found = re.search(r'data-dock-scope-now="[a-z]+" data-dock-day="([^"]+)"', page)
    assert found, "секція доку без дня"
    return found.group(1)


def _login(app) -> MiniClient:
    client = MiniClient(app)
    status, _, _ = client.login(*OPERATOR)
    assert status in (200, 302, 303), status
    return client


def _seed_two_days(session_factory):
    today = business_tab_today()
    yesterday = prev_tab_day(today)
    with session_factory() as db:
        op = db.scalar(select(User).where(User.username == OPERATOR[0]))
        op.mail_dock_scope = "labonly"
        op.mail_dock_ready = "all"
        _lab(db, "СЬОГОДНІ-1", 3, today)
        a = _lab(db, "ВЧОРА-1", 7, yesterday)
        b = _lab(db, "ВЧОРА-2", 8, yesterday)
        db.commit()
        return today, yesterday, a.id, b.id


@pytest.mark.usefixtures("weekday_clock")
def test_poll_with_day_shows_that_tab_and_default_stays_today(app_db):  # noqa: F811
    app, session_factory = app_db
    today, yesterday, _, _ = _seed_two_days(session_factory)
    client = _login(app)

    status, _, html = client.get("/mail/queue-mirror")
    assert status == 200
    assert "СЬОГОДНІ-1" in html and "ВЧОРА-1" not in html, "без дня — сьогодні, як було"
    assert _pressed(html) == "today"

    status, _, html = client.get(f"/mail/queue-mirror?day={_tab(yesterday)}")
    assert status == 200
    assert "ВЧОРА-1" in html and "ВЧОРА-2" in html and "СЬОГОДНІ-1" not in html
    assert html.index("ВЧОРА-1") < html.index("ВЧОРА-2"), "порядок таблиці"
    assert _pressed(html) == _tab(yesterday) and 'qmir-dayflag' in html
    # Полл без pick куку не чіпає: перезавантаження /mail лишається на сьогодні.
    assert DOCK_DAY_COOKIE not in client.cookies
    status, _, page = client.get("/mail")
    assert _section_day(page) == "today"


@pytest.mark.usefixtures("weekday_clock")
def test_picked_day_survives_a_full_reload_and_today_clears_it(app_db):  # noqa: F811
    app, session_factory = app_db
    today, yesterday, _, _ = _seed_two_days(session_factory)
    client = _login(app)

    status, _, _ = client.get(f"/mail/queue-mirror?pick=1&day={_tab(yesterday)}")
    assert status == 200
    assert client.cookies.get(DOCK_DAY_COOKIE) == f"{_tab(yesterday)}_{_tab(today)}"
    status, _, page = client.get("/mail")
    assert status == 200
    assert _section_day(page) == _tab(yesterday), "прийняття з картки перезавантажує /mail"
    assert _pressed(page) == _tab(yesterday)
    assert "ВЧОРА-1" in page
    # Полл без `day` (поля не було) — день із куки, а не сьогодні.
    status, _, html = client.get("/mail/queue-mirror")
    assert "ВЧОРА-1" in html

    status, _, html = client.get("/mail/queue-mirror?pick=1&day=today")
    assert "СЬОГОДНІ-1" in html
    # Starlette стирає куку порожнім `""` з Max-Age=0; MiniClient терміну не
    # читає й лишає `""` — тому перевіряємо наслідок: перезавантаження — сьогодні.
    assert client.cookies.get(DOCK_DAY_COOKIE) in (None, '""'), "«Сьогодні» стирає вибір"
    status, _, page = client.get("/mail")
    assert _section_day(page) == "today"


@pytest.mark.usefixtures("weekday_clock")
def test_group_sum3d_on_yesterdays_dock_rows_writes_into_yesterdays_tab(  # noqa: F811
    app_db, captured, monkeypatch  # noqa: F811
):
    app, session_factory = app_db
    today, yesterday, a_id, b_id = _seed_two_days(session_factory)
    with session_factory() as db:
        op = db.scalar(select(User).where(User.username == OPERATOR[0]))
        for oid in (a_id, b_id):
            toggle(db, db.get(Order, oid), op)
        db.commit()
    google = _wire_google(monkeypatch, session_factory)
    tabs: list[str] = []
    real = wb.get_worksheet_by_name
    monkeypatch.setattr(wb, "get_worksheet_by_name", lambda ss, name: (tabs.append(name), real(ss, name))[1])
    client = _login(app)

    # Док на вчорашній вкладці показує смугу з двома шпильками.
    status, _, html = client.get(f"/mail/queue-mirror?day={_tab(yesterday)}")
    assert "ВЧОРА-1" in html
    status, headers, _ = client.post(
        "/orders/sum3d-batch", {"sum3d_id": "08-15-00", "ctx": "mail"}, headers=HX
    )
    assert status == 200
    assert json.loads(headers["hx-trigger"])["mail-dock-refresh"] is True
    with session_factory() as db:
        assert db.get(Order, a_id).sum3d_id == db.get(Order, b_id).sum3d_id == "08-15-00"

    assert wb.write_fields_bulk(dict(captured.batch)) is None
    assert google["write"] == 1 and google["plans"] == [sorted([a_id, b_id])]
    assert tabs == [_tab(yesterday)], "запис — у вкладку роботи, не в сьогоднішню"
    # Після refresh док лишається на вчорашньому дні й бачить уже взяті роботи.
    status, _, html = client.get(f"/mail/queue-mirror?day={_tab(yesterday)}")
    assert "08-15-00" in html


def test_service_is_the_only_day_parser():
    """Свій strptime поряд з `parse_sheet_tab` — заборонено (§14 «Синк таблиці»)."""
    src = Path(mail_mirror.__file__).read_text(encoding="utf-8")
    assert "strptime(" not in src and "date.today(" not in src
