"""Вигляд списку листів на акаунті оператора (шестерня над списком).

Три речі, що ламаються тихо:
- межі в JS і на сервері мусять збігатися, інакше кнопка гасне не там, де
  сервер обрізає число (або навпаки — гасне, а сервер приймає більше);
- нуль означає «канон», і його НЕ можна підтягувати до мінімуму, інакше
  «Скинути» ставило б 2px замість «як було»;
- набір мусить пережити вихід і повторний вхід — це прямий запит власника.
"""
import asyncio
import re
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import User
from app.routers import deps as deps_mod
from app.routers import mail as mail_router_mod


def _database():
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return engine


def _request(user_id):
    return SimpleNamespace(
        session={"user_id": user_id},
        client=SimpleNamespace(host="127.0.0.1"),
        state=SimpleNamespace(),
    )


def _user(db):
    u = User(username="look", password_hash="x", full_name="Оп", role="оператор")
    db.add(u)
    db.commit()
    return u


def test_values_are_clamped_and_zero_stays_zero():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        req = _request(user.id)

        asyncio.run(_call(req, db, row_pad=999, list_width=99999, step=4))
        db.refresh(user)
        assert user.mail_row_pad == mail_router_mod.MAIL_ROW_PAD_MAX
        assert user.mail_list_width == mail_router_mod.MAIL_LIST_WIDTH_MAX
        assert user.mail_ui_step == 4

        # Нуль — це «канон», а не «мінімум»: інакше «Скинути» не скидало б.
        asyncio.run(_call(req, db, row_pad=0, list_width=0, step=2))
        db.refresh(user)
        assert (user.mail_row_pad, user.mail_list_width) == (0, 0)


def _call(req, db, **kwargs):
    async def _run():
        return mail_router_mod.post_mail_prefs(request=req, db=db, **kwargs)

    return _run()


def test_unknown_step_is_refused():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        resp = asyncio.run(_call(_request(user.id), db, row_pad=6, list_width=0, step=7))
        assert resp.status_code == 422
        db.refresh(user)
        assert user.mail_row_pad == 0, "невалідний крок не має зберігати решту"


def test_prefs_come_back_on_the_next_login():
    """Прямий запит власника: налаштування прикріплені до акаунта і
    підвантажуються при повторному вході. Нова сесія = новий request."""
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        asyncio.run(_call(_request(user.id), db, row_pad=14, list_width=700, step=4))

    deps_mod_session = deps_mod.SessionLocal
    try:
        deps_mod.SessionLocal = lambda: Session(engine)
        prefs = deps_mod.ui_prefs(_request(user.id))
    finally:
        deps_mod.SessionLocal = deps_mod_session
    assert prefs["mail_row_pad"] == 14
    assert prefs["mail_list_w"] == 700
    assert prefs["mail_step"] == 4


def test_js_limits_match_the_server():
    """Межі продубльовані в mail.js, щоб кнопка гасла ОДРАЗУ, а не після
    відповіді мережі. Розійтись тихо вони не можуть — ось сторож."""
    js = Path("app/static/js/mail.js").read_text(encoding="utf-8")
    found = re.search(r"LIMITS = \{ pad: \[(\d+), (\d+)\], width: \[(\d+), (\d+)\] \}", js)
    assert found, "не знайдено таблицю LIMITS у mail.js — оновіть і сторожа"
    pad_low, pad_high, width_low, width_high = (int(g) for g in found.groups())
    assert (pad_low, pad_high) == (
        mail_router_mod.MAIL_ROW_PAD_MIN,
        mail_router_mod.MAIL_ROW_PAD_MAX,
    )
    assert (width_low, width_high) == (
        mail_router_mod.MAIL_LIST_WIDTH_MIN,
        mail_router_mod.MAIL_LIST_WIDTH_MAX,
    )
    steps = {int(m) for m in re.findall(r'data-look-step="(\d+)"',
                                       Path("app/templates/mail_triage.html").read_text(encoding="utf-8"))}
    assert steps <= set(mail_router_mod.MAIL_UI_STEPS), "крок з розмітки сервер не прийме"
