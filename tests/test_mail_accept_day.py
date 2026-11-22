"""У вихідні оператор вибирає, куди лягає прийнятий лист: у п'ятничну вкладку
чи на понеділок (власник 22.11.26, `app/services/mail_accept_day.py`).

Неділя ввечері: своє закінчено, верстати вантажаться роботами на понеділок.
Досі такий рядок вписували в понеділкову вкладку руками, а лист у CRM лишався
неприйнятим — прийняття вміло писати лише в п'ятницю.
"""

from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

from app.models import Order
from app.services import mail_accept as mail_accept_svc
from app.services import mail_accept_day as mad
from tests.asgi_client import MiniClient
from tests.test_mail_transaction_safety import _database, _letter, _user, _wire
from tests.test_settings_slabs_render import OPERATOR, app_db  # noqa: F401 — фікстура

FRIDAY = date(2026, 11, 20)
SUNDAY = date(2026, 11, 22)
MONDAY = date(2026, 11, 23)
WEDNESDAY = date(2026, 11, 18)


@pytest.fixture
def sunday(monkeypatch):
    monkeypatch.setattr(mad, "business_today", lambda now=None: SUNDAY)


def _monday_tab(db: Session) -> None:
    """Синк уже приніс роботу з понеділкової вкладки — вона є в таблиці."""
    db.add(Order(source="lab", sheet_tab="23.11.26", row_number=1, status="нове"))
    db.flush()


def _operator(uid: int = 7) -> SimpleNamespace:
    return SimpleNamespace(id=uid, username=f"op{uid}")


# ── Коли перемикач узагалі є ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "moment, expected",
    [
        (datetime(2026, 11, 20, 12, 0), None),              # п'ятниця — будень
        (datetime(2026, 11, 21, 12, 0), (FRIDAY, MONDAY)),  # субота
        (datetime(2026, 11, 22, 16, 0), (FRIDAY, MONDAY)),  # неділя
        # Понеділок о 03:00 — нічна зміна ще на неділі (межа 07:30).
        (datetime(2026, 11, 23, 3, 0), (FRIDAY, MONDAY)),
        (datetime(2026, 11, 23, 8, 0), None),               # понеділок, день почався
    ],
)
def test_switch_exists_only_on_weekend_business_days(moment, expected):
    assert mad.window(moment) == expected


def test_weekday_has_no_switch_and_no_choice(monkeypatch, db_session):
    monkeypatch.setattr(mad, "business_today", lambda now=None: WEDNESDAY)
    user = _operator()
    _monday_tab(db_session)
    assert mad.view(db_session, user) is None
    assert mad.choose(db_session, user, mad.NEXT) is False
    assert mad.chosen(db_session, user) is None


# ── Вибір оператора ─────────────────────────────────────────────────────────


def test_default_is_the_friday_tab(sunday, db_session):
    _monday_tab(db_session)
    seen = mad.view(db_session, _operator())
    assert (seen.current_label, seen.next_label) == ("пт 20.11", "пн 23.11")
    assert seen.on_next is False and seen.next_known is True
    assert seen.state == mad.CURRENT and seen.button_suffix == ""
    assert mad.chosen(db_session, _operator()) is None


def test_switch_to_monday_and_back(sunday, db_session):
    user = _operator()
    _monday_tab(db_session)
    assert mad.choose(db_session, user, mad.NEXT) is True
    assert mad.chosen(db_session, user) == MONDAY
    seen = mad.view(db_session, user)
    assert seen.on_next and seen.state == mad.NEXT
    assert seen.button_suffix == " → пн 23.11", "кнопка «Прийняти» каже, куди ляже рядок"

    assert mad.choose(db_session, user, mad.CURRENT) is True
    assert mad.chosen(db_session, user) is None
    assert mad.view(db_session, user).button_suffix == ""


def test_monday_is_not_offered_until_its_tab_exists(sunday, db_session):
    """Вкладки понеділка ще немає в таблиці — рядок не було б куди писати."""
    user = _operator()
    assert mad.view(db_session, user).next_known is False
    assert mad.choose(db_session, user, mad.NEXT) is False
    assert mad.chosen(db_session, user) is None


def test_choice_belongs_to_one_operator(sunday, db_session):
    """Напарник, що доробляє п'ятничне, чужого перемикача не помічає."""
    _monday_tab(db_session)
    mad.choose(db_session, _operator(7), mad.NEXT)
    assert mad.chosen(db_session, _operator(7)) == MONDAY
    assert mad.chosen(db_session, _operator(8)) is None


def test_choice_dies_on_monday_morning_and_next_weekend(monkeypatch, db_session):
    user = _operator()
    _monday_tab(db_session)
    monkeypatch.setattr(mad, "business_today", lambda now=None: SUNDAY)
    mad.choose(db_session, user, mad.NEXT)
    assert mad.chosen(db_session, user) == MONDAY
    # Понеділок: будень — вибору немає, лист лягає у свій день.
    monkeypatch.setattr(mad, "business_today", lambda now=None: MONDAY)
    assert mad.chosen(db_session, user) is None and mad.view(db_session, user) is None
    # Наступна субота: записана вкладка 23.11 — уже не «наступна» (30.11).
    monkeypatch.setattr(mad, "business_today", lambda now=None: date(2026, 11, 28))
    assert mad.chosen(db_session, user) is None
    assert mad.view(db_session, user).on_next is False


def test_unknown_choice_and_missing_user_change_nothing(sunday, db_session):
    _monday_tab(db_session)
    assert mad.choose(db_session, _operator(), "tomorrow") is False
    assert mad.choose(db_session, None, mad.NEXT) is False
    assert mad.chosen(db_session, None) is None and mad.view(db_session, None) is None


# ── Прийняття: вибраний день доходить до вкладки ────────────────────────────


def _tab_stubs(monkeypatch, tabs: dict[date, str]):
    """Таблиця з датованими вкладками: «найсвіжіша ≤ дня», як справжня."""
    calls: list[date] = []

    def _listing(spreadsheet, day):
        calls.append(day)
        found = [d for d in tabs if d <= day]
        return SimpleNamespace(title=tabs[max(found)]) if found else None

    monkeypatch.setattr(mail_accept_svc, "business_today", lambda: SUNDAY)
    monkeypatch.setattr(mail_accept_svc, "open_spreadsheet", lambda db=None: object())
    monkeypatch.setattr(mail_accept_svc, "latest_worksheet_on_or_before", _listing)
    monkeypatch.setattr(
        mail_accept_svc, "get_worksheet_by_name", lambda ss, name: SimpleNamespace(title=name)
    )
    mail_accept_svc.reset_tab_cache()
    return calls


def test_resolver_writes_to_monday_when_asked(monkeypatch):
    calls = _tab_stubs(monkeypatch, {FRIDAY: "20.11.26", MONDAY: "23.11.26"})
    email = SimpleNamespace(id=1)
    assert mail_accept_svc._resolve_target_tab(None, email)[0] == "20.11.26", "без вибору — п'ятниця"
    assert mail_accept_svc._resolve_target_tab(None, email, MONDAY)[0] == "23.11.26"
    # Кеш вкладки — за ЦІЛЬОВИМ днем: понеділкова відповідь не підміняє п'ятничну.
    assert mail_accept_svc._resolve_target_tab(None, email)[0] == "20.11.26"
    assert calls == [SUNDAY, MONDAY, SUNDAY]


def test_resolver_falls_back_to_friday_when_monday_tab_is_gone(monkeypatch):
    """Понеділкову вкладку прибрали за цю хвилину — рядок лягає в п'ятницю, як
    і без перемикача, а не на фантомний день."""
    _tab_stubs(monkeypatch, {FRIDAY: "20.11.26"})
    assert mail_accept_svc._resolve_target_tab(None, SimpleNamespace(id=1), MONDAY)[0] == "20.11.26"


def test_accept_letter_puts_the_work_into_the_chosen_tab(tmp_path, monkeypatch):
    engine = _database()
    _wire(monkeypatch, tmp_path)
    monkeypatch.setattr(mail_accept_svc, "business_today", lambda: SUNDAY)
    mail_accept_svc.reset_tab_cache()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        usual, _ = _letter(db, tmp_path / "spool" / "u1")
        result = mail_accept_svc.accept_letter(db, user, usual, client_name="Люмі-Дент",
                                               material_color="mono a3", quantity="2")
        assert result.ok, result.error
        assert result.order.sheet_tab == "22.11.26", "без вибору — як і досі (таблиця тут недоступна)"

    engine = _database()
    mail_accept_svc.reset_tab_cache()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        monday_letter, _ = _letter(db, tmp_path / "spool" / "u1")
        result = mail_accept_svc.accept_letter(db, user, monday_letter, client_name="Люмі-Дент",
                                               material_color="mono a3", quantity="2",
                                               accept_day=MONDAY)
        assert result.ok, result.error
        assert result.order.sheet_tab == "23.11.26"


# ── Екран: перемикач у шапці пошти й маршрут ────────────────────────────────


def test_mail_page_shows_the_switch_only_on_weekends(app_db, monkeypatch):  # noqa: F811
    app, factory = app_db
    with factory() as db:
        _monday_tab(db)
        db.commit()
    client = MiniClient(app)
    client.login(*OPERATOR)

    monkeypatch.setattr(mad, "business_today", lambda now=None: WEDNESDAY)
    status, _headers, body = client.get("/mail")
    assert status == 200 and "acd-mode" not in body, "у будень перемикача немає"

    monkeypatch.setattr(mad, "business_today", lambda now=None: SUNDAY)
    body = client.get("/mail")[2]
    assert 'class="acd-mode"' in body and "Приймаю на:" in body
    assert "пт 20.11" in body and "пн 23.11" in body
    assert 'data-state="current"' in body

    # Клік «пн 23.11»: вибір записано, сторінка перечитується цілком.
    status, headers, _ = client.post("/mail/accept-day", {"day": "next"}, headers={"HX-Request": "true"})
    assert status == 204 and headers.get("hx-refresh") == "true"
    body = client.get("/mail")[2]
    assert 'class="acd-mode is-next"' in body and 'data-state="next"' in body

    # Без HTMX (звичайна форма) — повернення на пошту; і назад на п'ятницю.
    status, headers, _ = client.post("/mail/accept-day", {"day": "current"})
    assert status == 303 and headers.get("location") == "/mail"
    assert 'data-state="current"' in client.get("/mail")[2]


def test_monday_button_is_disabled_without_its_tab(app_db, monkeypatch):  # noqa: F811
    app, _factory = app_db
    monkeypatch.setattr(mad, "business_today", lambda now=None: SUNDAY)
    client = MiniClient(app)
    client.login(*OPERATOR)
    body = client.get("/mail")[2]
    assert "Вкладки пн 23.11 ще немає в таблиці" in body and "disabled" in body
    # Навіть прямий запит нічого не вмикає.
    client.post("/mail/accept-day", {"day": "next"})
    assert 'data-state="current"' in client.get("/mail")[2]


def test_switch_route_needs_a_login(app_db):  # noqa: F811
    app, _factory = app_db
    status, headers, _ = MiniClient(app).post("/mail/accept-day", {"day": "next"})
    assert status in (302, 303) and "/login" in (headers.get("location") or "")
