"""Фаза 1 аудиту 05.09.26 — те, що видно операторові на кожному екрані.

Кроки 1.7 (статус як дія рядка, архів у пошуку — лише перегляд), 1.8 (журнал
і стан синку доступні звідусіль) і 1.9 (архів памʼятає, де ти був).
"""

from datetime import datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from starlette.datastructures import Headers

import app.web as web
from app.db import Base
from app.models import Order, User
from app.routers import archive as archive_router_mod

# Через справжній Jinja застосунку — у ньому вже зареєстровані глобали
# (is_overdue, material_color_css_class, status_dot…), як у test_order_row_template.
_ROW = web.templates.get_template("_order_row.html")

STATUSES = ["нове", "прийнято", "проблема", "видано"]


def _order(**overrides):
    base = dict(
        id=1, source="lab", sheet_tab="01.01.20", status="нове",
        material_color="пмма A2", kind="анатомія", quantity="3",
        job_code="2026-07-21_00016-007", job_code_folder_uri=None,
        job_code_folder_preview_token=None, sum3d_id="12-01-45",
        export_folder_uri=None, export_folder_preview_token=None,
        technician_name="Іван", cam_comment="перевір край", client_name=None,
        work_order_no="24122", calculated_raw="Р", archived_at=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _render(order):
    return _ROW.render(order=order, statuses=STATUSES, sync_error=None)


class TestStatusIsARowAction:
    """1.7: колонку статусу приховано на прохання власника, і разом із нею зі
    списку зник спосіб поставити «проблема» — доводилось відкривати картку."""

    def test_live_row_offers_every_status_from_the_row(self):
        html = _render(_order(status="прийнято"))
        assert 'class="rowmenu"' in html
        for status in STATUSES:
            assert f'"status": "{status}"' in html
        assert 'hx-post="/orders/1/status"' in html
        # поточний статус позначено, щоб меню не питало «а який зараз?»
        assert 'rowmenu-item on' in html

    def test_menu_does_not_add_a_seventh_row_fill(self):
        """Сигнальні канали рядка (фон/край/контур) не чіпаємо — меню це
        контрол, а не новий колір (CLAUDE.md §14 «Черга»)."""
        html = _render(_order())
        assert "rowmenu" in html
        assert "queue-row-status" not in html


class TestArchivedRowsAreReadOnly:
    """1.7: архів — це історія. Правити її з пошуку означало б тихо міняти
    минуле; картка роботи для архівних уже read-only, рядок мусив теж."""

    def test_archived_row_has_no_inputs_and_no_actions(self):
        html = _render(_order(archived_at=datetime(2026, 1, 2, 10, 0)))
        assert 'name="sum3d_id"' not in html
        assert 'name="cam_comment"' not in html
        assert 'name="operator"' not in html
        assert 'class="rowmenu"' not in html
        assert "row-delete-form" not in html
        assert "badge arch" in html

    def test_archived_row_still_shows_the_values(self):
        html = _render(_order(archived_at=datetime(2026, 1, 2, 10, 0)))
        assert "12-01-45" in html      # Sum3D
        assert "перевір край" in html  # коментар
        assert ">Р<" in html or "Р" in html

    def test_live_row_keeps_its_inputs(self):
        html = _render(_order())
        assert 'name="sum3d_id"' in html
        assert 'name="cam_comment"' in html
        assert "badge arch" not in html


class TestArchiveRemembersWhereYouWere:
    """1.9: місяць і день живуть в адресі, тож перезавантаження, «назад» і
    надіслане посилання відкривають те саме місце."""

    def _db(self):
        engine = create_engine("sqlite://", poolclass=StaticPool)
        Base.metadata.create_all(engine)
        return engine

    def _request(self):
        return SimpleNamespace(
            session={"user_id": 1},
            client=SimpleNamespace(host="127.0.0.1"),
            headers=Headers({}),
        )

    def _seed(self, db):
        db.add(User(id=1, username="op", password_hash="x", role="адмін"))
        old = datetime.now() - timedelta(days=200)
        for i in range(3):
            db.add(Order(
                source="lab", sheet_tab=old.strftime("%d.%m.%y"), row_number=i + 1,
                work_order_no=f"1{i}", status="видано", archived_at=old,
            ))
        db.commit()
        return old

    def test_month_in_the_url_opens_that_month(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(
            web.templates, "TemplateResponse",
            lambda request, template, context: captured.update(ctx=context) or context,
        )
        engine = self._db()
        with Session(engine, expire_on_commit=False) as db:
            old = self._seed(db)
            wanted = old.strftime("%Y-%m")

            archive_router_mod.get_archive(request=self._request(), month=wanted, db=db)

            assert captured["ctx"]["active_ym"] == wanted

    def test_date_in_the_url_opens_that_day_too(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(
            web.templates, "TemplateResponse",
            lambda request, template, context: captured.update(ctx=context) or context,
        )
        engine = self._db()
        with Session(engine, expire_on_commit=False) as db:
            old = self._seed(db)

            archive_router_mod.get_archive(
                request=self._request(), month=old.strftime("%Y-%m"),
                date=old.strftime("%d.%m.%y"), db=db,
            )

            ctx = captured["ctx"]
            assert ctx["selected_date"] == old.date()
            assert len(ctx["day_orders"]) == 3

    def test_an_unknown_month_falls_back_to_the_newest(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(
            web.templates, "TemplateResponse",
            lambda request, template, context: captured.update(ctx=context) or context,
        )
        engine = self._db()
        with Session(engine, expire_on_commit=False) as db:
            old = self._seed(db)

            archive_router_mod.get_archive(request=self._request(), month="1999-01", db=db)

            assert captured["ctx"]["active_ym"] == old.strftime("%Y-%m")

    def test_bad_month_answers_with_a_readable_notice_not_an_empty_400(self):
        """HTMX не свапає 4xx узагалі — порожня відповідь-400 виглядала як
        «нічого не сталось», і оператор тиснув ще раз."""
        engine = self._db()
        with Session(engine, expire_on_commit=False) as db:
            self._seed(db)
            response = archive_router_mod.get_archive_detail(
                request=self._request(), month="не-місяць", db=db
            )
            assert response.status_code == 200
            assert "не розпізнано" in response.body.decode("utf-8")


class TestGlobalsForEveryScreen:
    """1.8/1.10: бейджі й лічильники, які мусять працювати на кожному екрані —
    той самий патерн, що shift_pending: власна сесія, збій → безпечний дефолт."""

    def test_busy_operators_never_raises(self, monkeypatch):
        from app.routers import deps

        def boom():
            raise RuntimeError("база впала")

        monkeypatch.setattr(deps, "SessionLocal", boom)
        assert deps.busy_operators() == 0

    def test_sync_state_returns_none_when_the_sheet_is_not_set_up(self, monkeypatch):
        from app.routers import deps

        monkeypatch.setattr(
            "app.services.config_state.sheets_configured", lambda db: False
        )
        assert deps.sync_state() is None

    def test_sync_state_never_raises(self, monkeypatch):
        from app.routers import deps

        def boom():
            raise RuntimeError("база впала")

        monkeypatch.setattr(deps, "SessionLocal", boom)
        assert deps.sync_state() is None
