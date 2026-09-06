"""Помилка прийняття не викидає оператора з візарда (аудит 05.09.26, UX 1.2).

Візард живе у фрагменті `#mail-wizard`. Кожна невдача прийняття відповідала
редіректом на `/mail/{id}?error=…`, htmx ішов за ним і перемальовував увесь
екран — усе, що оператор заповнив у трьох кроках, зникало, а лист доводилось
шукати заново. Тепер той самий крок повертається фрагментом із поясненням.

Тут же — бейдж готовності тріажу (UX 1.4): він мусить рахувати нескачані
файли за посиланням тим самим способом, що й серверний гейт прийняття,
інакше список каже «ГОТОВО» на листі, який прийняти не можна.
"""

from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from starlette.datastructures import Headers

import app.web as web
from app.db import Base
from app.models import EmailMessage, User
from app.routers import mail as mail_router_mod
from app.triage_status import triage_readiness

DRIVE_LINK = "https://drive.google.com/file/d/1AbCdEfGhIjKlMnOpQrStUvWxYz012345/view"


def _database():
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return engine


def _user(db):
    user = User(username="op", password_hash="x", full_name="Оп", role="оператор")
    db.add(user)
    db.commit()
    return user


def _request(user_id, *, htmx: bool):
    headers = {"HX-Request": "true"} if htmx else {}
    return SimpleNamespace(
        session={"user_id": user_id},
        client=SimpleNamespace(host="127.0.0.1"),
        headers=Headers(headers),
    )


def _letter(db, **kw):
    values = dict(uid="u1", status="нове", from_address="lab@ukr.net",
                  subject="моно а3", attachments_status="ready",
                  material_color_guess="моно а3")
    values.update(kw)
    email = EmailMessage(**values)
    db.add(email)
    db.commit()
    return email


def _accept(db, user, email, request):
    return mail_router_mod.accept_email(
        request=request, email_id=email.id,
        client_name="Люмі-Дент", material_color="моно а3", kind="", quantity="7",
        folder_pick="", folder_new="", material_folder="",
        attachment_ids=[], accept_anyway="", db=db,
    )


class TestAcceptErrorStaysInTheWizard:
    def _stub_templates(self, monkeypatch, captured):
        monkeypatch.setattr(
            web.templates, "TemplateResponse",
            lambda request, template, context: captured.update(
                template=template, ctx=context
            ) or SimpleNamespace(status_code=200, headers={}),
        )

    def test_undownloaded_link_returns_the_step_with_the_values_kept(self, monkeypatch):
        engine = _database()
        captured = {}
        self._stub_templates(monkeypatch, captured)

        with Session(engine, expire_on_commit=False) as db:
            user = _user(db)
            email = _letter(db, body_text=f"Файли: {DRIVE_LINK}")

            response = _accept(db, user, email, _request(user.id, htmx=True))

            assert response.status_code == 200
            assert captured["template"] == "_mail_wizard.html"
            ctx = captured["ctx"]
            assert ctx["wizard_step"] == 3
            assert "за посиланням не скачано" in ctx["error"]
            # Найголовніше: введене НЕ загублено — оператор дотискає кнопку,
            # а не набирає три кроки заново.
            assert ctx["client_name"] == "Люмі-Дент"
            assert ctx["material_color"] == "моно а3"
            assert ctx["quantity"] == "7"
            # Лист лишається в тріажі — нічого не прийнято.
            assert email.status == "нове"

    def test_pending_attachments_also_stay_in_the_wizard(self, monkeypatch):
        engine = _database()
        captured = {}
        self._stub_templates(monkeypatch, captured)

        with Session(engine, expire_on_commit=False) as db:
            user = _user(db)
            email = _letter(db, attachments_status="pending")

            _accept(db, user, email, _request(user.id, htmx=True))

            assert captured["template"] == "_mail_wizard.html"
            assert "завантажуються" in captured["ctx"]["error"]

    def test_without_htmx_the_old_redirect_is_kept(self, monkeypatch):
        """Форма без JS не має куди вставити фрагмент — там редірект доречний."""
        engine = _database()
        captured = {}
        self._stub_templates(monkeypatch, captured)

        with Session(engine, expire_on_commit=False) as db:
            user = _user(db)
            email = _letter(db, body_text=f"Файли: {DRIVE_LINK}")

            response = _accept(db, user, email, _request(user.id, htmx=False))

            assert response.status_code == 303
            assert "error=" in response.headers["location"]
            assert captured == {}


class TestReadinessKnowsAboutLinks:
    def test_badge_is_not_ready_while_a_link_is_unfetched(self):
        email = SimpleNamespace(
            service_type_guess=None, material_color_guess="моно а3",
            attachments=[], body_text=f"Файли: {DRIVE_LINK}",
            handled_link_refs=None,
        )
        result = triage_readiness(email)
        assert result["state"] == "incomplete"
        assert any("за посиланням" in item for item in result["missing"])

    def test_badge_is_ready_once_the_link_is_handled(self):
        email = SimpleNamespace(
            service_type_guess=None, material_color_guess="моно а3",
            attachments=[], body_text=f"Файли: {DRIVE_LINK}",
            handled_link_refs='["1AbCdEfGhIjKlMnOpQrStUvWxYz012345"]',
        )
        assert triage_readiness(email)["state"] == "ready"

    def test_letter_without_links_is_unaffected(self):
        email = SimpleNamespace(
            service_type_guess=None, material_color_guess="моно а3",
            attachments=[], body_text="жодних посилань", handled_link_refs=None,
        )
        assert triage_readiness(email)["state"] == "ready"
