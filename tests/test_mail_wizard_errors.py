"""Помилка прийняття не викидає оператора з картки (аудит 05.09.26, UX 1.2).

Картка «Стрічка» живе у фрагменті `#mail-detail` (блок A замінив вкладки+майстер,
25.09.26). Кожна невдача прийняття відповідала б редіректом на `/mail/{id}?error=…`,
htmx ішов би за ним і перемальовував увесь екран — усе, що оператор заповнив,
зникало б. Тепер та сама картка повертається фрагментом (`_mail_detail_panel.html`)
із заповненими значеннями і банером помилки.

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
from app.models import Attachment, EmailMessage, User
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
            # Блок A «Стрічка»: помилка повертає ту саму КАРТКУ (не крок майстра).
            assert captured["template"] == "_mail_detail_panel.html"
            ctx = captured["ctx"]
            assert "за посиланням не скачано" in ctx["error"]
            # Найголовніше: введене НЕ загублено — оператор дотискає кнопку,
            # а не набирає три кроки заново.
            assert ctx["client_name"] == "Люмі-Дент"
            assert ctx["material_color"] == "моно а3"
            assert ctx["quantity"] == "7"
            # Лист лишається в тріажі — нічого не прийнято.
            assert email.status == "нове"

    def test_undownloaded_attachment_blocks_with_a_loud_message(self, monkeypatch):
        """Вкладення листа, якого немає на диску, не пропускається мовчки —
        крок 3 з наполегливим попередженням, лист лишається «нове» (власник
        24.09.26). Без accept_anyway гейт тримає."""
        engine = _database()
        captured = {}
        self._stub_templates(monkeypatch, captured)

        with Session(engine, expire_on_commit=False) as db:
            user = _user(db)
            email = _letter(db)
            db.add(Attachment(
                email_message_id=email.id, filename="crown.stl",
                saved_path="/no/such/dir/crown.stl",
            ))
            db.commit()

            _accept(db, user, email, _request(user.id, htmx=True))

            ctx = captured["ctx"]
            assert captured["template"] == "_mail_detail_panel.html"
            assert "не скачано на диск" in ctx["error"]
            assert email.status == "нове"

    def test_accept_anyway_passes_the_undownloaded_attachment_gate(self, monkeypatch):
        """Галка «прийняти без них» пропускає повз гейт нескачаного вкладення —
        помилка вже НЕ про диск (сервер іде далі). Не блокуємо намертво."""
        engine = _database()
        captured = {}
        self._stub_templates(monkeypatch, captured)

        with Session(engine, expire_on_commit=False) as db:
            user = _user(db)
            email = _letter(db)
            db.add(Attachment(
                email_message_id=email.id, filename="crown.stl",
                saved_path="/no/such/dir/crown.stl",
            ))
            db.commit()

            mail_router_mod.accept_email(
                request=_request(user.id, htmx=True), email_id=email.id,
                client_name="Люмі-Дент", material_color="моно а3", kind="",
                quantity="7", folder_pick="", folder_new="", material_folder="",
                attachment_ids=[], accept_anyway="1", db=db,
            )
            # Гейт диска пройдено: якщо крок повернувся з помилкою, вона вже
            # НЕ про «не скачано на диск».
            if captured.get("ctx"):
                assert "не скачано на диск" not in (captured["ctx"].get("error") or "")

    def test_pending_attachments_also_stay_in_the_wizard(self, monkeypatch):
        engine = _database()
        captured = {}
        self._stub_templates(monkeypatch, captured)

        with Session(engine, expire_on_commit=False) as db:
            user = _user(db)
            email = _letter(db, attachments_status="pending")

            _accept(db, user, email, _request(user.id, htmx=True))

            assert captured["template"] == "_mail_detail_panel.html"
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
