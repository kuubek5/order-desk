"""Збережені вигляди черги: приватність, канонічний формат, живучість.

Стережемо рівно ті три властивості, заради яких вигляд і став рядком у БД,
а не записом у localStorage:

1. **Приватність.** Вигляд належить операторові. Ані список, ані
   перейменування, ані видалення не мають дотягуватись до чужого рядка —
   навіть коли id підставили в адресу руками.
2. **Один формат із чергою.** Вигляд зберігає ТОЙ САМИЙ рядок GET-параметрів,
   яким фільтрується черга. Другий, паралельний формат розійшовся б із чергою
   на першій же правці фільтрів.
3. **Застарілий параметр не валить екран.** Фільтр приберуть — збережений
   торік вигляд мусить відкрити чергу, а не 500-ту.
"""

from datetime import datetime

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from app.business_day import business_today
from app.db import Base
from app.models import Order, SavedQueueView, User
from app.routers import queue as queue_router
from app.services.queue_view import (
    MAX_SAVED_VIEWS,
    SavedViewError,
    build_queue_view,
    delete_view,
    normalize_view_query,
    own_view,
    rename_view,
    save_view,
    saved_views,
)


def _database():
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture()
def db():
    with Session(_database()) as session:
        yield session


def _user(db, username="op"):
    user = User(username=username, password_hash="x", full_name=username, role="оператор")
    db.add(user)
    db.commit()
    return user


def _request(user_id):
    """Мінімальний Request із сесією — рівно те, що читає get_current_user."""
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/queue/views",
            "headers": [],
            "query_string": b"",
            "session": {"user_id": user_id} if user_id is not None else {},
        }
    )


# ── Канонічний формат ──────────────────────────────────────────────────────


def test_known_filters_survive_in_the_canonical_order():
    got = normalize_view_query("source=lab&ready=can_take&period=yesterday")
    assert got == "period=yesterday&ready=can_take&source=lab"


def test_unknown_keys_are_dropped_not_kept():
    """Ключ, якого черга не знає, не має доїхати до адреси: інакше вигляд
    возив би сміття, а колись — і чужий параметр із зовсім іншого екрана."""
    got = normalize_view_query("period=today&partial=rows&focus=17&hocus=pocus")
    assert "partial" not in got
    assert "focus" not in got
    assert "hocus" not in got


def test_stale_values_degrade_to_defaults_instead_of_raising():
    """Прибрали фільтр — збережений вигляд просто відкриє чергу за
    замовчуванням. Саме це й мало статися замість 500-ї."""
    got = normalize_view_query("period=позаминулого&ready=вигадка&source=марс&sort=колір")
    assert got == "period=today&ready=all&source=all"


def test_flags_and_sort_are_carried_when_valid():
    got = normalize_view_query("overdue=1&mine=1&sort=material&dir=desc&period=earlier")
    assert got == "overdue=1&period=earlier&ready=all&source=all&mine=1&sort=material&dir=desc"


def test_broken_day_is_dropped_but_a_real_one_is_kept():
    assert "date" not in normalize_view_query("date=32.13.99")
    got = normalize_view_query("date=22.07.26&date_page=2")
    assert "date=22.07.26" in got and "date_page=2" in got


def test_empty_query_still_yields_a_usable_view():
    assert normalize_view_query("") == "period=today&ready=all&source=all"


def test_saved_query_matches_what_the_queue_screen_reports(db):
    """Формат вигляду і формат черги — один. Якби вони розійшлись, підсвітка
    «цей вигляд зараз застосований» брехала б з першого ж дня."""
    user = _user(db)
    db.add(
        Order(
            source="lab",
            sheet_tab=business_today().strftime("%d.%m.%y"),
            row_number=7,
            work_order_no="24122",
            status="нове",
        )
    )
    db.commit()

    view = build_queue_view(db, user, period="yesterday", ready="can_take", source="lab")
    assert view.context["view_qs"] == normalize_view_query(view.context["rows_qs"])

    saved = save_view(db, user, "Ранкова", view.context["rows_qs"])
    assert saved.query == view.context["view_qs"]


# ── Приватність ────────────────────────────────────────────────────────────


def test_views_are_private_to_their_owner(db):
    mine, other = _user(db, "рома"), _user(db, "костя")
    save_view(db, mine, "Моя ранкова", "period=today")
    save_view(db, other, "Чужа", "period=earlier")

    assert [v.name for v in saved_views(db, mine)] == ["Моя ранкова"]
    assert [v.name for v in saved_views(db, other)] == ["Чужа"]


def test_foreign_view_is_invisible_by_id(db):
    """Підставлений в адресу чужий id не має нічого знайти."""
    mine, other = _user(db, "рома"), _user(db, "костя")
    theirs = save_view(db, other, "Чужа", "period=earlier")

    assert own_view(db, mine, theirs.id) is None


def test_foreign_view_cannot_be_renamed(db):
    mine, other = _user(db, "рома"), _user(db, "костя")
    theirs = save_view(db, other, "Чужа", "period=earlier")

    with pytest.raises(SavedViewError):
        rename_view(db, mine, theirs.id, "Тепер моя")
    db.refresh(theirs)
    assert theirs.name == "Чужа"


def test_foreign_view_cannot_be_deleted(db):
    mine, other = _user(db, "рома"), _user(db, "костя")
    theirs = save_view(db, other, "Чужа", "period=earlier")

    assert delete_view(db, mine, theirs.id) is False
    assert db.get(SavedQueueView, theirs.id) is not None


def test_same_name_for_two_operators_is_fine(db):
    """Унікальність — у межах акаунта. Двоє операторів мають право назвати
    свою «Ранкова», не знаючи один про одного."""
    mine, other = _user(db, "рома"), _user(db, "костя")
    save_view(db, mine, "Ранкова", "period=today")
    save_view(db, other, "Ранкова", "period=today")

    assert len(saved_views(db, mine)) == 1
    assert len(saved_views(db, other)) == 1


# ── Збереження, перейменування, видалення ──────────────────────────────────


def test_name_is_trimmed_and_time_is_local(db):
    user = _user(db)
    view = save_view(db, user, "  Ранкова   видача  ", "period=today")
    assert view.name == "Ранкова видача"
    # Локальний час, не UTC: на SQLite server_default=func.now() дав би зсув.
    assert abs((datetime.now() - view.created_at).total_seconds()) < 60


def test_empty_name_is_refused(db):
    user = _user(db)
    with pytest.raises(SavedViewError):
        save_view(db, user, "   ", "period=today")
    assert saved_views(db, user) == []


def test_duplicate_name_is_refused_case_insensitively(db):
    user = _user(db)
    save_view(db, user, "Ранкова", "period=today")
    with pytest.raises(SavedViewError):
        save_view(db, user, "ранкова", "period=earlier")
    assert len(saved_views(db, user)) == 1


def test_new_view_goes_to_the_end_and_never_shuffles_the_rest(db):
    """Смуга — мʼязова памʼять: збереження нового вигляду не має рухати вже
    збережені (той самий закон, що зі шпильками «мої зараз»)."""
    user = _user(db)
    first = save_view(db, user, "Перший", "period=today")
    second = save_view(db, user, "Другий", "period=earlier")
    save_view(db, user, "Третій", "period=tomorrow")

    assert [v.name for v in saved_views(db, user)] == ["Перший", "Другий", "Третій"]
    assert first.position < second.position


def test_the_strip_has_a_ceiling(db):
    user = _user(db)
    for number in range(MAX_SAVED_VIEWS):
        save_view(db, user, f"Вигляд {number}", "period=today")
    with pytest.raises(SavedViewError):
        save_view(db, user, "Ще один", "period=today")


def test_rename_keeps_the_filters(db):
    user = _user(db)
    view = save_view(db, user, "Ранкова", "period=earlier&ready=can_take")
    before = view.query
    rename_view(db, user, view.id, "Вечірня")
    assert view.name == "Вечірня"
    assert view.query == before


def test_rename_to_an_existing_name_is_refused(db):
    user = _user(db)
    save_view(db, user, "Ранкова", "period=today")
    second = save_view(db, user, "Вечірня", "period=earlier")
    with pytest.raises(SavedViewError):
        rename_view(db, user, second.id, "Ранкова")
    assert second.name == "Вечірня"


def test_delete_removes_only_that_view(db):
    user = _user(db)
    keep = save_view(db, user, "Лишається", "period=today")
    drop = save_view(db, user, "Зникає", "period=earlier")

    assert delete_view(db, user, drop.id) is True
    assert [v.id for v in saved_views(db, user)] == [keep.id]
    # Повторне видалення — тихий no-op, а не помилка: рядка вже немає.
    assert delete_view(db, user, drop.id) is False


# ── Роути ──────────────────────────────────────────────────────────────────


def test_routes_create_rename_and_delete(db):
    user = _user(db)

    response = queue_router.create_queue_view(
        _request(user.id), name="Ранкова", current="period=today&ready=can_take", db=db
    )
    assert response.status_code == 200
    views = saved_views(db, user)
    assert [v.name for v in views] == ["Ранкова"]
    assert views[0].query == "period=today&ready=can_take&source=all"

    queue_router.rename_queue_view(
        _request(user.id), view_id=views[0].id, name="Вечірня", current="period=today", db=db
    )
    assert saved_views(db, user)[0].name == "Вечірня"

    queue_router.remove_queue_view(
        _request(user.id), view_id=views[0].id, current="period=today", db=db
    )
    assert saved_views(db, user) == []


def test_route_reports_the_reason_instead_of_falling_over(db):
    """Помилка збереження лишається в смузі текстом — сторінка не падає й не
    перезавантажується, а набране імʼя видно далі."""
    user = _user(db)
    save_view(db, user, "Ранкова", "period=today")

    response = queue_router.create_queue_view(
        _request(user.id), name="Ранкова", current="period=today", db=db
    )
    assert response.status_code == 200
    assert response.context["views_error"]
    assert response.context["views_open_form"] == "new"
    assert response.context["views_form_name"] == "Ранкова"


def test_route_refuses_a_logged_out_caller(db):
    with pytest.raises(HTTPException) as exc:
        queue_router.create_queue_view(_request(None), name="Ранкова", current="", db=db)
    assert exc.value.status_code == 401


def test_route_renders_only_my_views(db):
    """Останній рубіж приватності — те, що реально потрапляє в HTML."""
    mine, other = _user(db, "рома"), _user(db, "костя")
    save_view(db, other, "Чужа", "period=earlier")
    save_view(db, mine, "Моя", "period=today")

    response = queue_router.create_queue_view(
        _request(mine.id), name="Ще моя", current="period=today", db=db
    )
    names = [v.name for v in response.context["saved_views"]]
    assert names == ["Моя", "Ще моя"]
    body = response.body.decode("utf-8")
    assert "Чужа" not in body
    assert "Моя" in body
