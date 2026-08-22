"""Mail filter rules: keyword/sender → «Відфільтровані», never delete."""

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.web as web
from app.db import Base
from app.mail_filters import apply_filters_to_email, apply_rule_retroactively
from app.models import EmailMessage, MailFilterRule, User


def _database():
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return engine


def _user(db: Session, role: str = "адмін") -> User:
    user = User(username="op", password_hash="x", full_name="Op", role=role)
    db.add(user)
    db.commit()
    return user


def _request(user_id: int | None, hx: bool = False):
    headers = {"HX-Request": "true"} if hx else {}
    return SimpleNamespace(
        session={} if user_id is None else {"user_id": user_id},
        client=SimpleNamespace(host="127.0.0.1"),
        headers=headers,
    )


# --- engine -----------------------------------------------------------------


def test_keyword_rule_matches_subject_and_body_case_insensitive():
    engine = _database()
    with Session(engine) as db:
        db.add(MailFilterRule(kind="keyword", pattern="Рахунок-Фактура", category="бухгалтерія"))
        subj = EmailMessage(uid="s", status="нове", subject="РАХУНОК-ФАКТУРА №5")
        body = EmailMessage(uid="b", status="нове", subject="", body_text="надсилаю рахунок-фактура за липень")
        miss = EmailMessage(uid="m", status="нове", subject="коронки моно а3")
        db.add_all([subj, body, miss])
        db.flush()

        assert apply_filters_to_email(db, subj) is True
        assert apply_filters_to_email(db, body) is True
        assert apply_filters_to_email(db, miss) is False
        assert subj.filter_category == "бухгалтерія"
        assert miss.filter_category is None


def test_sender_rule_matches_address_substring():
    engine = _database()
    with Session(engine) as db:
        db.add(MailFilterRule(kind="sender", pattern="@buh.example.com", category="бухгалтерія"))
        hit = EmailMessage(uid="h", status="нове", from_address="olena@BUH.example.com")
        miss = EmailMessage(uid="m", status="нове", from_address="client@ukr.net")
        db.add_all([hit, miss])
        db.flush()

        assert apply_filters_to_email(db, hit) is True
        assert apply_filters_to_email(db, miss) is False


def test_disabled_rule_never_matches():
    engine = _database()
    with Session(engine) as db:
        db.add(MailFilterRule(kind="keyword", pattern="спам", category="спам", enabled=False))
        email = EmailMessage(uid="e", status="нове", subject="спам і реклама")
        db.add(email)
        db.flush()
        assert apply_filters_to_email(db, email) is False
        assert email.filter_category is None


def test_already_stamped_email_not_restamped():
    """An operator's unfilter (or an earlier rule) must not be overridden."""
    engine = _database()
    with Session(engine) as db:
        db.add(MailFilterRule(kind="keyword", pattern="друк", category="3D-друк"))
        email = EmailMessage(uid="e", status="нове", subject="3д друк", filter_category="інше")
        db.add(email)
        db.flush()
        assert apply_filters_to_email(db, email) is False
        assert email.filter_category == "інше"


def test_first_match_wins_and_hits_counted():
    engine = _database()
    with Session(engine) as db:
        kw = MailFilterRule(kind="keyword", pattern="друк", category="3D-друк")
        snd = MailFilterRule(kind="sender", pattern="@x.com", category="спам")
        db.add_all([kw, snd])
        email = EmailMessage(uid="e", status="нове", subject="3д друк", from_address="a@x.com")
        db.add(email)
        db.flush()

        apply_filters_to_email(db, email)
        # keyword rules win over sender rules regardless of creation order
        assert email.filter_category == "3D-друк"
        assert kw.hits == 1 and snd.hits == 0


def test_retroactive_apply_stamps_only_matching_pending():
    engine = _database()
    with Session(engine) as db:
        rule = MailFilterRule(kind="keyword", pattern="рахунок", category="бухгалтерія")
        db.add(rule)
        match = EmailMessage(uid="a", status="нове", subject="рахунок за серпень")
        other = EmailMessage(uid="b", status="нове", subject="моно а3")
        archived = EmailMessage(uid="c", status="прийнято", subject="рахунок старий")
        db.add_all([match, other, archived])
        db.flush()

        moved = apply_rule_retroactively(db, rule)
        assert moved == 1
        assert match.filter_category == "бухгалтерія"
        assert other.filter_category is None
        assert archived.filter_category is None  # archive untouched
        assert rule.hits == 1


# --- routes -----------------------------------------------------------------


def test_get_mail_pending_excludes_filtered_and_counts_split(monkeypatch):
    engine = _database()
    captured = {}
    monkeypatch.setattr(
        web.templates, "TemplateResponse",
        lambda request, template, context: captured.update(ctx=context) or context,
    )
    monkeypatch.setattr(web, "attach_email_preview_tokens", lambda *a, **k: None)
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        db.add_all([
            EmailMessage(uid="p", status="нове"),
            EmailMessage(uid="f", status="нове", filter_category="спам"),
        ])
        db.commit()

        web.get_mail(request=_request(user.id), db=db)
        ctx = captured["ctx"]
        assert [e.uid for e in ctx["emails"]] == ["p"]
        assert ctx["pending_count"] == 1
        assert ctx["filtered_count"] == 1

        web.get_mail(request=_request(user.id), db=db, view="filtered")
        assert [e.uid for e in captured["ctx"]["emails"]] == ["f"]


def test_unfilter_returns_letter_to_pending(monkeypatch):
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        rule = MailFilterRule(kind="keyword", pattern="друк", category="3D-друк")
        db.add(rule)
        db.flush()
        email = EmailMessage(uid="e", status="нове", filter_category="3D-друк", filter_rule_id=rule.id)
        db.add(email)
        db.commit()

        response = web.unfilter_email(request=_request(user.id, hx=True), email_id=email.id, db=db)
        assert response.status_code == 200
        db.refresh(email)
        assert email.filter_category is None
        assert email.filter_rule_id is None


def test_create_rule_requires_admin(monkeypatch):
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        operator = _user(db, role="оператор")
        with pytest.raises(web.HTTPException) as exc:
            web.create_mail_filter(
                request=_request(operator.id), kind="keyword",
                pattern="спам", category="спам", db=db,
            )
        assert exc.value.status_code == 403


def test_create_rule_applies_retroactively(monkeypatch):
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        admin = _user(db)
        email = EmailMessage(uid="e", status="нове", subject="3д друк моделі")
        db.add(email)
        db.commit()

        response = web.create_mail_filter(
            request=_request(admin.id), kind="keyword",
            pattern="друк", category="3D-друк", db=db,
        )
        assert response.status_code == 303
        db.refresh(email)
        assert email.filter_category == "3D-друк"


def test_delete_rule_keeps_letters_filtered(monkeypatch):
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        admin = _user(db)
        rule = MailFilterRule(kind="keyword", pattern="друк", category="3D-друк")
        db.add(rule)
        db.flush()
        email = EmailMessage(uid="e", status="нове", filter_category="3D-друк", filter_rule_id=rule.id)
        db.add(email)
        db.commit()

        web.delete_mail_filter(request=_request(admin.id), rule_id=rule.id, db=db)
        db.refresh(email)
        assert email.filter_category == "3D-друк"  # badge stays (history)
        assert email.filter_rule_id is None
        assert db.scalars(select(MailFilterRule)).all() == []


def test_suggest_banner_after_two_rejections_without_rule(monkeypatch):
    engine = _database()
    captured = {}
    monkeypatch.setattr(
        web.templates, "TemplateResponse",
        lambda request, template, context: captured.update(ctx=context) or context,
    )
    monkeypatch.setattr(web, "attach_email_preview_tokens", lambda *a, **k: None)
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        db.add_all([
            EmailMessage(uid="r1", status="відхилено", from_address="spam@x.com"),
            EmailMessage(uid="r2", status="відхилено", from_address="spam@x.com"),
            EmailMessage(uid="r3", status="відхилено", from_address="once@y.com"),
        ])
        db.commit()

        web.get_mail(request=_request(user.id), db=db)
        suggest = captured["ctx"]["filter_suggest"]
        assert suggest == {"address": "spam@x.com", "count": 2}

        # A sender rule (even DISABLED = "не питати") silences the banner.
        db.add(MailFilterRule(kind="sender", pattern="spam@x.com", category="відхилені", enabled=False))
        db.commit()
        web.get_mail(request=_request(user.id), db=db)
        assert captured["ctx"]["filter_suggest"] is None
