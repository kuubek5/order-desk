"""Мутації налаштувань лишають слід у журналі дій (K.6).

CLAUDE.md §5 вимагає точної історії «хто що зробив» — але вона обривалась саме
там, де ціна помилки найвища: пароль пошти, шляхи до шар, видалення верстата,
відновлення з копії. Ці дії не мали жодного сліду, крім тоста, який зникає.

Друга половина правила: у журнал іде ЩО змінили, ніколи ЗНАЧЕННЯ. Серед
налаштувань є пароль IMAP і service-account JSON, і розшифровані секрети не
залишають settings_store.
"""

import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from starlette.datastructures import Headers

from app.db import Base
from app.models import ActionLog, Machine, User
from app.routers.settings import devices as devices_mod
from app.routers.settings import overview as overview_mod


def _database():
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return engine


def _admin(db: Session) -> User:
    user = User(username="root", password_hash="x", full_name="Адмін", role="адмін")
    db.add(user)
    db.commit()
    return user


def _request(user_id: int, form: dict | None = None, host: str = "127.0.0.1"):
    async def _form():
        return form or {}

    return SimpleNamespace(
        session={"user_id": user_id},
        client=SimpleNamespace(host=host),
        form=_form,
        headers=Headers({}),
    )


def test_deleting_a_machine_is_logged():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        admin = _admin(db)
        machine = Machine(name="Coritec 250i", host="10.0.0.7", created_at=datetime.now())
        db.add(machine)
        db.commit()

        devices_mod.delete_machine(request=_request(admin.id), machine_id=machine.id, db=db)

        entry = db.scalar(select(ActionLog).where(ActionLog.field == "machine.delete"))
    assert entry is not None
    assert "Coritec 250i" in entry.note
    assert entry.operator_id == admin.id


def test_calibration_path_change_is_logged_and_operator_is_refused():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        admin = _admin(db)
        operator = User(username="op", password_hash="x", full_name="Оп", role="оператор")
        db.add(operator)
        db.commit()

        devices_mod.save_machine_calibration_path(
            request=_request(admin.id), path=r"\host\share\frames", db=db
        )
        entry = db.scalar(
            select(ActionLog).where(ActionLog.field == "machine_calibration_path")
        )
        assert entry is not None and "frames" in entry.note

        # Оператор редагує «Обладнання», але НЕ цю теку: це запис у довільну
        # мережеву шару, а не налаштування верстата (CLAUDE.md §14).
        with pytest.raises(HTTPException) as exc:
            devices_mod.save_machine_calibration_path(
                request=_request(operator.id), path="", db=db
            )
        assert exc.value.status_code == 403


def test_settings_save_logs_keys_never_values():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        admin = _admin(db)
        secret = "super-secret-imap-password"

        asyncio.run(overview_mod.post_settings(
            request=_request(admin.id, {"action": "save", "imap_password": secret}),
            db=db,
        ))

        entries = db.scalars(select(ActionLog).where(ActionLog.field == "settings")).all()

    assert entries, "збереження налаштувань має лишати слід"
    note = entries[0].note or ""
    assert "imap_password" in note, "у журналі має бути ЩО змінили"
    assert secret not in note, "значення секрету в журнал потрапити не сміє"
