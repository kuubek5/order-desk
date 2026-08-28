"""Додавання коментаря до роботи.

Ключове рішення (28.08.26): коментар комітиться в БД одразу, а запис у
Google-таблицю йде ФОНОМ. Раніше роут відкривав таблицю прямо в потоці
запиту, і додавання коментаря зависало на час відповіді Google (до ~40с
холодним на лаб-проксі). Коментар у базі — головне; таблиця best-effort.
"""

import asyncio
from types import SimpleNamespace

from starlette.datastructures import Headers
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.web as web
from app.db import Base
from app.models import Comment, Order, User


def _database():
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return engine


def _user(db: Session) -> User:
    u = User(username="op", password_hash="unused", full_name="Оператор", role="оператор")
    db.add(u)
    db.commit()
    return u


def _request(user_id):
    return SimpleNamespace(
        session={"user_id": user_id},
        client=SimpleNamespace(host="127.0.0.1"),
        headers=Headers({}),
    )


def _lab_order(db: Session) -> Order:
    o = Order(source="lab", sheet_tab="27.08.26", row_number=7,
              status="нове")
    db.add(o)
    db.commit()
    return o


class TestCommentIsInstant:
    def test_request_commits_comment_without_touching_the_sheet_inline(self, monkeypatch):
        engine = _database()
        touched = []
        monkeypatch.setattr(web, "open_spreadsheet",
                            lambda db=None: touched.append("open") or object())
        queued = []
        monkeypatch.setattr(web, "_append_comment_background",
                            lambda order_id, comment_id, line: queued.append((order_id, line)))
        with Session(engine, expire_on_commit=False) as db:
            user = _user(db)
            order = _lab_order(db)
            resp = asyncio.run(web.add_order_comment(
                request=_request(user.id), order_id=order.id, text="  на швидку  ", db=db,
            ))
            assert resp.status_code == 303
            saved = db.scalars(select(Comment)).all()
            assert len(saved) == 1
            assert saved[0].text == "на швидку"   # обрізано
        assert touched == [], "таблицю чіпає фон, а не потік запиту"
        assert queued and queued[0][0] == order.id

    def test_empty_comment_is_rejected_and_nothing_is_queued(self, monkeypatch):
        engine = _database()
        queued = []
        monkeypatch.setattr(web, "_append_comment_background",
                            lambda *a, **k: queued.append(a))
        with Session(engine, expire_on_commit=False) as db:
            user = _user(db)
            order = _lab_order(db)
            import pytest
            from fastapi import HTTPException
            with pytest.raises(HTTPException):
                asyncio.run(web.add_order_comment(
                    request=_request(user.id), order_id=order.id, text="   ", db=db,
                ))
            assert db.scalars(select(Comment)).all() == []
        assert queued == []

    def test_email_order_saves_comment_but_never_queues_a_sheet_write(self, monkeypatch):
        """Тільки lab-роботи мають рядок у таблиці. Поштова робота коментар
        зберігає, але в таблицю не пише."""
        engine = _database()
        queued = []
        monkeypatch.setattr(web, "_append_comment_background",
                            lambda *a, **k: queued.append(a))
        with Session(engine, expire_on_commit=False) as db:
            user = _user(db)
            order = Order(source="email", sheet_tab="27.08.26", status="нове")
            db.add(order); db.commit()
            asyncio.run(web.add_order_comment(
                request=_request(user.id), order_id=order.id, text="привіт", db=db,
            ))
            assert len(db.scalars(select(Comment)).all()) == 1
        assert queued == [], "поштова робота не має рядка в таблиці"
