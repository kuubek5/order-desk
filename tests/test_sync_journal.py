"""Екран «Журнал синку» + кружок здоровʼя синку в рейці (крок 3.1).

Два різні ризики, тому й два блоки тестів:

1. Роут — гейт (це діагностика для адміна), фільтри й групування по днях.
2. Кружок — ПРАВИЛО КОЛЬОРУ. Воно тут єдине місце, де стан стає кольором, і
   найдорожча помилка не «не той відтінок», а зелений там, де стану немає:
   такий кружок стверджує, що синк живий, саме тоді, коли він не запускався
   жодного разу (CLAUDE.md §14, правило печей — хибна цифра гірша за жодну).
"""

from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.web as web
from app.db import Base
from app.models import SyncLog, User
from app.routers import queue as queue_router
from app.routers import sync_journal as sj
from app.routers.deps import templates


def _database():
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return engine


def _admin(db: Session) -> User:
    user = User(username="root", password_hash="x", full_name="Роман", role="адмін")
    db.add(user)
    db.commit()
    return user


def _operator(db: Session) -> User:
    user = User(username="op", password_hash="x", full_name="Оператор", role="оператор")
    db.add(user)
    db.commit()
    return user


def _request(user_id: int | None):
    session = {} if user_id is None else {"user_id": user_id}
    return SimpleNamespace(
        session=session,
        query_params={},
        headers={},
        client=SimpleNamespace(host="127.0.0.1"),
    )


def _log(db: Session, *, direction, status, when, tab=None, message=""):
    row = SyncLog(direction=direction, status=status, sheet_tab=tab, message=message)
    db.add(row)
    db.flush()
    row.occurred_at = when
    db.commit()
    return row


def _pair(sheet_state, mail_state="neutral", *, paused=False):
    return {
        "sheet": {"state": sheet_state, "label": "", "running": False, "paused": paused},
        "mail": {"state": mail_state, "label": "", "running": False},
    }


@pytest.fixture(autouse=True)
def _stub_templates(monkeypatch):
    """Роут перевіряємо за контекстом, а не за HTML — рендер має власні тести."""
    monkeypatch.setattr(
        web.templates, "TemplateResponse", lambda request, template, context: context
    )


@pytest.fixture(autouse=True)
def _stub_live_status(monkeypatch):
    """Живий стан пари бере сесію БД і пульс процесу — для роутних тестів це
    шум; підмінюємо на сталу пару."""
    monkeypatch.setattr(sj, "live_sync_status", lambda db: _pair("success", "success"))


# --- гейт ---------------------------------------------------------------------


def test_sync_journal_requires_login():
    engine = _database()
    with Session(engine) as db:
        response = sj.get_sync_journal(request=_request(None), db=db)
    assert isinstance(response, RedirectResponse)
    assert response.status_code == 303


def test_sync_journal_is_admin_only():
    """Оператору цей екран не потрібен і не корисний: рядки технічні."""
    engine = _database()
    with Session(engine) as db:
        operator = _operator(db)
        with pytest.raises(HTTPException) as exc:
            sj.get_sync_journal(request=_request(operator.id), db=db)
    assert exc.value.status_code == 403


# --- вміст і фільтри ----------------------------------------------------------


def test_entries_are_grouped_by_day_newest_first():
    engine = _database()
    with Session(engine) as db:
        admin = _admin(db)
        _log(db, direction="sheet_to_db", status="ok", when=datetime(2026, 9, 4, 10, 0))
        _log(db, direction="db_to_sheet", status="error", when=datetime(2026, 9, 5, 9, 0))
        _log(db, direction="db_to_sheet", status="ok", when=datetime(2026, 9, 5, 11, 0))
        context = sj.get_sync_journal(request=_request(admin.id), db=db)

    days = [group["day"] for group in context["groups"]]
    assert days == [datetime(2026, 9, 5).date(), datetime(2026, 9, 4).date()]
    # Усередині дня — теж від найновішого.
    assert [row.occurred_at.hour for row in context["groups"][0]["rows"]] == [11, 9]
    assert context["counts"] == {"ok": 2, "skipped": 0, "error": 1}


def test_status_and_direction_filters_narrow_the_list():
    engine = _database()
    with Session(engine) as db:
        admin = _admin(db)
        _log(db, direction="sheet_to_db", status="ok", when=datetime(2026, 9, 5, 9, 0))
        _log(db, direction="db_to_sheet", status="error", when=datetime(2026, 9, 5, 10, 0))
        _log(db, direction="db_to_sheet", status="skipped", when=datetime(2026, 9, 5, 11, 0))

        by_status = sj.get_sync_journal(request=_request(admin.id), db=db, status="error")
        by_direction = sj.get_sync_journal(
            request=_request(admin.id), db=db, direction="db_to_sheet"
        )

    assert [r.status for g in by_status["groups"] for r in g["rows"]] == ["error"]
    assert by_status["selected_status"] == "error"
    assert len([r for g in by_direction["groups"] for r in g["rows"]]) == 2


def test_unknown_filter_values_are_ignored_not_applied():
    """Підроблений параметр не має давати порожній екран: порожньо в цьому
    журналі читається як «синк мовчить», а це вже діагноз."""
    engine = _database()
    with Session(engine) as db:
        admin = _admin(db)
        _log(db, direction="sheet_to_db", status="ok", when=datetime(2026, 9, 5, 9, 0))
        context = sj.get_sync_journal(
            request=_request(admin.id), db=db,
            direction="хтозна", status="хтозна", day="вчора",
        )

    assert context["selected_direction"] == ""
    assert context["selected_status"] == ""
    assert context["selected_day"] == ""
    assert sum(len(g["rows"]) for g in context["groups"]) == 1


def test_day_filter_keeps_only_that_day():
    engine = _database()
    with Session(engine) as db:
        admin = _admin(db)
        _log(db, direction="sheet_to_db", status="ok", when=datetime(2026, 9, 4, 23, 59))
        _log(db, direction="sheet_to_db", status="ok", when=datetime(2026, 9, 5, 0, 1))
        context = sj.get_sync_journal(request=_request(admin.id), db=db, day="2026-09-05")

    assert context["selected_day"] == "2026-09-05"
    assert [g["day"] for g in context["groups"]] == [datetime(2026, 9, 5).date()]


def test_window_is_capped_and_says_so():
    engine = _database()
    with Session(engine) as db:
        admin = _admin(db)
        for minute in range(sj.PAGE_LIMIT + 5):
            db.add(SyncLog(direction="db_to_sheet", status="ok", message=str(minute)))
        db.commit()
        context = sj.get_sync_journal(request=_request(admin.id), db=db)

    assert context["truncated"] is True
    assert sum(len(g["rows"]) for g in context["groups"]) == sj.PAGE_LIMIT


# --- правило кольору ----------------------------------------------------------


def _dot(state, **kwargs) -> str:
    macro = templates.env.get_template("_sync_dot.html").module.sync_dot
    return str(macro(state, **kwargs))


def test_no_state_is_neutral_never_green():
    """Найважливіший тест файлу. Пульс не переживає рестарт, тому «стану ще
    немає» — звичайна ситуація, і саме там зелений кружок брехав би."""
    assert "is-none" in _dot(None)
    assert "is-none" in _dot(_pair("neutral", "neutral"))
    assert "is-ok" not in _dot(None)


def test_fresh_success_is_green():
    assert "is-ok" in _dot(_pair("success", "success"))


def test_stale_or_paused_is_yellow():
    # «warning» приходить із heartbeat_status: остання спроба старша за
    # interval × STALE_HEARTBEAT_MULTIPLIER (3 хв для таблиці, 6 хв для пошти).
    assert "is-warn" in _dot(_pair("warning", "success"))
    # Пауза синку — теж жовтий: цикл живий, але нічого не читає й не пише.
    assert "is-warn" in _dot(_pair("success", "success", paused=True))


def test_error_beats_everything():
    assert "is-error" in _dot(_pair("error", "success"))
    assert "is-error" in _dot(_pair("success", "error"))
    assert "is-error" in _dot(_pair("error", "success", paused=True))


def test_dot_carries_the_oob_target_id_exactly_once():
    """Ціль OOB-свапу мусить бути одна: два однакові id — і htmx оновлює не
    той кружок (або жоден)."""
    html = _dot(_pair("success"))
    assert html.count('id="rail-sync-dot"') == 1
    assert "hx-swap-oob" not in html
    assert 'hx-swap-oob="true"' in _dot(_pair("success"), oob=True)


# --- живість кружка: OOB їде в уже наявному поллі ------------------------------


def test_sheets_state_marks_the_dot_oob_for_admin_only(monkeypatch):
    """Кружок оновлюється тим самим поллом /sheets/state (кожні 3 с), без
    власного запиту. Оператору OOB не шлемо: пункт меню з цим id у нього не
    рендериться, і htmx лаявся б у консоль на ненайдену ціль."""
    seen: dict = {}
    monkeypatch.setattr(
        web.templates, "TemplateResponse",
        lambda request, template, context: seen.update(context) or SimpleNamespace(headers={}),
    )
    monkeypatch.setattr(queue_router, "sheets_configured", lambda db: True)
    monkeypatch.setattr(queue_router, "queue_sync_summary", lambda db: "")
    monkeypatch.setattr(queue_router, "live_sync_status", lambda db: _pair("success"))

    engine = _database()
    with Session(engine) as db:
        admin = _admin(db)
        operator = _operator(db)
        queue_router.sheet_sync_state(_request(admin.id), db)
        assert seen["oob_dot"] is True
        seen.clear()
        queue_router.sheet_sync_state(_request(operator.id), db)
        assert seen["oob_dot"] is False


# --- рейка --------------------------------------------------------------------


@pytest.fixture
def _quiet_globals(monkeypatch):
    """Глобали рейки ходять у справжню БД — для перевірки розмітки це зайве."""
    for name, value in (
        ("shift_pending", 0),
        ("feedback_open_count", 0),
        ("busy_operators", 0),
        ("get_known_update", None),
        ("sync_state", None),
        ("notify_prefs", {}),
        ("ui_prefs", {}),
    ):
        monkeypatch.setitem(templates.env.globals, name, (lambda v: (lambda: v))(value))


def _rail(role: str, active: str) -> str:
    return templates.env.get_template("_topbar_nav.html").render(
        user=SimpleNamespace(role=role, full_name="Роман", username="root"),
        topbar_active=active,
        request=SimpleNamespace(url=SimpleNamespace(path="/settings")),
        sync_status=_pair("success"),
        pending_mail_count=0,
    )


@pytest.mark.parametrize("active", ["queue", "handout", "settings"])
def test_rail_shows_the_dot_in_both_of_its_modes(active, _quiet_globals):
    """Рейка має ДВА режими — головне меню й drill-in налаштувань. Кружок
    мусить бути в обох, інакше він зникає рівно на екранах налаштувань, куди
    йдуть, коли синк зламався."""
    html = _rail("адмін", active)
    assert html.count('id="rail-sync-dot"') == 1
    assert "/journal/sync" in html


@pytest.mark.parametrize("active", ["queue", "settings"])
def test_rail_hides_the_dot_from_operators(active, _quiet_globals):
    """Екран адмінський — пункт меню теж, і в ОБОХ режимах рейки: у drill-in
    налаштувань сусідній «Журнал дій» відкритий усім, тож пункт легко було
    покласти поза адмін-гейтом і привести оператора з меню прямо в 403.
    Заразом це й умова коректності OOB-свапу: у оператора цілі немає, тому
    роут йому OOB і не шле."""
    html = _rail("оператор", active)
    assert "rail-sync-dot" not in html
    assert "/journal/sync" not in html


class TestRestoreErasedRow:
    """B.8: слід від стертого рядка вже був у журналі текстом — тепер із нього
    росте дія. Найдорожча помилка тут — записати вміст у рядок, який лабораторія
    вже переюзала: це затерло б чужу живу роботу, тобто рівно те, від чого весь
    цей блок і будувався."""

    def _erased_log(self, db, *, restored_at=None):
        row = SyncLog(
            direction="db_to_sheet", status="ok", sheet_tab="07.09.26",
            message="видалено роботу 42: рядок очищено",
            erased_row=13,
            erased_values='["1", "24122", "2", "моно а3", "анатомія"]',
            erased_restored_at=restored_at,
        )
        db.add(row)
        db.commit()
        return row

    def test_operator_cannot_restore(self):
        engine = _database()
        with Session(engine, expire_on_commit=False) as db:
            operator = _operator(db)
            entry = self._erased_log(db)
            with pytest.raises(HTTPException) as exc:
                sj.restore_erased_sheet_row(entry.id, _request(operator.id), db)
            assert exc.value.status_code == 403

    def test_restores_values_and_marks_the_entry(self, monkeypatch):
        engine = _database()
        written: list = []
        monkeypatch.setattr(sj, "open_spreadsheet", lambda db=None: object())
        monkeypatch.setattr(sj, "get_worksheet_by_name", lambda ss, name: name)
        monkeypatch.setattr(
            sj, "restore_erased_row",
            lambda ws, row, values: written.append((ws, row, values)),
        )
        with Session(engine, expire_on_commit=False) as db:
            admin = _admin(db)
            entry = self._erased_log(db)

            response = sj.restore_erased_sheet_row(entry.id, _request(admin.id), db)

            assert response.status_code == 303
            assert response.headers["location"] == "/journal/sync?restored=ok"
            assert written == [("07.09.26", 13, ["1", "24122", "2", "моно а3", "анатомія"])]
            db.refresh(entry)
            assert entry.erased_restored_at is not None
            # Саме відновлення теж лишає слід у журналі.
            messages = [r.message for r in db.query(SyncLog).all()]
            assert any("рядок 13 відновлено" in (m or "") for m in messages)

    def test_occupied_row_is_refused_and_not_marked(self, monkeypatch):
        from app.sheet_writer import RowOccupiedError

        engine = _database()
        monkeypatch.setattr(sj, "open_spreadsheet", lambda db=None: object())
        monkeypatch.setattr(sj, "get_worksheet_by_name", lambda ss, name: name)

        def occupied(ws, row, values):
            raise RowOccupiedError("рядок 13 уже зайнято")

        monkeypatch.setattr(sj, "restore_erased_row", occupied)
        with Session(engine, expire_on_commit=False) as db:
            admin = _admin(db)
            entry = self._erased_log(db)

            response = sj.restore_erased_sheet_row(entry.id, _request(admin.id), db)

            assert response.headers["location"] == "/journal/sync?restored=occupied"
            db.refresh(entry)
            # Не відновили — кнопка мусить лишитись.
            assert entry.erased_restored_at is None

    def test_second_restore_is_refused(self, monkeypatch):
        engine = _database()
        calls: list = []
        monkeypatch.setattr(sj, "open_spreadsheet", lambda db=None: object())
        monkeypatch.setattr(sj, "get_worksheet_by_name", lambda ss, name: name)
        monkeypatch.setattr(
            sj, "restore_erased_row", lambda ws, row, values: calls.append(row)
        )
        with Session(engine, expire_on_commit=False) as db:
            admin = _admin(db)
            entry = self._erased_log(db, restored_at=datetime(2026, 9, 7, 9, 0))

            response = sj.restore_erased_sheet_row(entry.id, _request(admin.id), db)

            assert response.headers["location"] == "/journal/sync?restored=already"
            assert calls == []

    def test_entry_without_saved_content_has_nothing_to_restore(self):
        engine = _database()
        with Session(engine, expire_on_commit=False) as db:
            admin = _admin(db)
            plain = _log(db, direction="db_to_sheet", status="ok",
                         when=datetime(2026, 9, 7, 9, 0), message="звичайний запис")

            response = sj.restore_erased_sheet_row(plain.id, _request(admin.id), db)

            assert response.headers["location"] == "/journal/sync?restored=nothing"
