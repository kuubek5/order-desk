

# --- K.9: ПІН зберігається хешем -------------------------------------------


def test_pin_matches_accepts_hash_and_legacy_plaintext():
    """Шифрування налаштувань тут не рятує: сенс ПІНа — сховати зарплатні
    цифри від операторів, а вони мають доступ до тієї ж машини й тієї ж бази,
    отже й до ключа розшифрування. Тому новий код лежить хешем.

    Старі, відкриті коди приймаються далі: інакше оновлення замкнуло б розділ
    для того, хто цей код і задавав."""
    from app.auth import hash_password
    from app.routers.vyrobitok import _pin_matches

    hashed = hash_password("2468")
    assert _pin_matches("2468", hashed) is True
    assert _pin_matches("1111", hashed) is False

    # Legacy: збережений відкритим текстом.
    assert _pin_matches("2468", "2468") is True
    assert _pin_matches("1111", "2468") is False

    assert _pin_matches("", hashed) is False
    assert _pin_matches("2468", "") is False


def test_saving_a_pin_stores_a_hash_not_the_code(monkeypatch):
    """Найдорожча помилка тут — покласти код у базу як є."""
    import asyncio
    from types import SimpleNamespace

    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from sqlalchemy.pool import StaticPool

    from app.db import Base
    from app.models import User
    from app.routers.settings import sections as sections_mod
    from app.settings_store import get_setting

    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)

    async def _form():
        return {"vyrobitok_pin": "2468"}

    with Session(engine, expire_on_commit=False) as db:
        admin = User(username="root", password_hash="x", role="адмін")
        db.add(admin)
        db.commit()
        request = SimpleNamespace(
            session={"user_id": admin.id},
            client=SimpleNamespace(host="127.0.0.1"),
            form=_form,
        )
        asyncio.run(sections_mod.save_vyrobitok_pin(request=request, db=db))

        stored = get_setting(db, "vyrobitok_pin")

    assert stored and stored != "2468", "код не має лежати в базі відкритим"
    assert stored.startswith("$")
