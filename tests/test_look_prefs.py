"""Шестерня «вигляд списку» — спільна для черги і тріажу пошти.

Чотири речі, що ламаються тихо:
- нуль означає «як було», і його НЕ можна підтягувати до мінімуму, інакше
  «Скинути» ставило б 2px замість канону;
- межі в lookgear.js мусять збігатися з серверними, інакше кнопка гасне не
  там, де сервер обрізає число (або навпаки — гасне, а сервер бере більше);
- пресети й крок із розмітки мусять бути тим, що сервер приймає;
- набір мусить пережити вихід і повторний вхід — прямий запит власника.
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
from app.routers import auth as auth_router_mod
from app.routers import deps as deps_mod
from app.services import look_prefs


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
    user = User(username="look", password_hash="x", full_name="Оп", role="оператор")
    db.add(user)
    db.commit()
    return user


def _save(request, db, **kwargs):
    payload = {"row_pad": 0, "list_width": 0, "density": "", "mat_style": "", "step": 2}
    payload.update(kwargs)
    return asyncio.run(
        auth_router_mod.post_account_look(request=request, db=db, **payload)
    )


def test_values_are_clamped_and_zero_stays_zero():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        request = _request(user.id)

        _save(request, db, scope="mail", row_pad=999, list_width=99999, step=4)
        db.refresh(user)
        assert user.mail_row_pad == look_prefs.ROW_PAD.high
        assert user.mail_list_width == look_prefs.LIST_WIDTH.high
        assert user.mail_ui_step == 4

        # Нуль — це «канон», а не «мінімум»: інакше «Скинути» не скидало б.
        _save(request, db, scope="mail", row_pad=0, list_width=0, step=2)
        db.refresh(user)
        assert (user.mail_row_pad, user.mail_list_width) == (0, 0)


def test_queue_scope_saves_its_own_fields():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        _save(_request(user.id), db, scope="queue", density="compact",
              mat_style="code", row_pad=14, step=8)
        db.refresh(user)
        assert user.queue_density == "compact"
        assert user.queue_mat_style == "code"
        assert user.queue_row_pad == 14
        # Скоуп черги не має чіпати налаштування пошти.
        assert user.mail_row_pad == 0


def test_unknown_values_are_refused():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        for bad in ({"step": 7}, {"density": "нема-такого"}, {"mat_style": "хех"}):
            resp = _save(_request(user.id), db, scope="queue", row_pad=6, **bad)
            assert resp.status_code == 422
            db.refresh(user)
            assert user.queue_row_pad == 0, "невалідне значення не має зберігати решту"
        assert _save(_request(user.id), db, scope="вигадка").status_code == 422


def test_prefs_come_back_on_the_next_login(monkeypatch):
    """Прямий запит власника: налаштування прикріплені до акаунта і
    підвантажуються при повторному вході. Нова сесія = новий request."""
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        _save(_request(user.id), db, scope="mail", row_pad=14, list_width=700, step=4)
        _save(_request(user.id), db, scope="queue", density="spacious", step=1)

    monkeypatch.setattr(deps_mod, "SessionLocal", lambda: Session(engine))
    prefs = deps_mod.ui_prefs(_request(user.id))
    assert (prefs["mail_row_pad"], prefs["mail_list_w"], prefs["mail_step"]) == (14, 700, 4)
    assert (prefs["queue_density"], prefs["queue_step"]) == ("spacious", 1)


def test_js_limits_match_the_server():
    """Межі продубльовані в lookgear.js, щоб кнопка гасла ОДРАЗУ, а не після
    відповіді мережі. Розійтись тихо вони не можуть — ось сторож."""
    js = Path("app/static/js/lookgear.js").read_text(encoding="utf-8")
    found = re.search(r"LIMITS = \{ pad: \[(\d+), (\d+)\], width: \[(\d+), (\d+)\] \}", js)
    assert found, "не знайдено таблицю LIMITS у lookgear.js — оновіть і сторожа"
    pad_low, pad_high, width_low, width_high = (int(g) for g in found.groups())
    assert (pad_low, pad_high) == (look_prefs.ROW_PAD.low, look_prefs.ROW_PAD.high)
    assert (width_low, width_high) == (look_prefs.LIST_WIDTH.low, look_prefs.LIST_WIDTH.high)


def test_markup_only_offers_values_the_server_accepts():
    """Розмітка не має пропонувати крок, якого сервер не прийме — інакше клік
    по кроку дає 422 і вигляд тихо не зберігається.

    Читаємо ВІДРЕНДЕРЕНИЙ макрос, а не файл шаблону. Попередня версія цього
    тесту шукала `data-look-step="<цифра>"` у сирому файлі, де стоїть
    `data-look-step="{{ s }}"` — регекс не міг влучити НІКОЛИ, і сторож був
    порожній від народження.
    """
    import re

    from app.routers.deps import templates

    macro = templates.env.get_template("_lookgear.html").module.lookgear
    html = str(macro("queue", "body", 0, 2))
    steps = {int(m) for m in re.findall(r'data-look-step="(\d+)"', html)}
    assert steps, "у відрендереній панелі мають бути кнопки кроку"
    assert steps <= set(look_prefs.UI_STEPS), f"розмітка пропонує {steps - set(look_prefs.UI_STEPS)}"

    queue = Path("app/templates/queue.html").read_text(encoding="utf-8")
    presets = set(re.findall(r"\('([a-z]*)', \d+, '", queue))
    assert presets <= set(look_prefs.QUEUE_DENSITIES), presets


def test_handout_day_totals_counts_the_same_set_the_screen_shows():
    """Лічильник дня — цифри, які диктують керівництву, і в них уже одного разу
    був баг: знаменник ЗМЕНШУВАВСЯ протягом дня, бо рахувався по видимих
    групах, а видане з них випадає.

    Тут перевіряємо головне: чисельник і знаменник беруться з ОДНІЄЇ множини.
    Видали клієнта — «зроблених» більшає, загальне число не рухається.
    """
    import datetime as dt

    from sqlalchemy.orm import Session as _S

    from app.models import Order
    from app.services.handout import handout_day_totals

    engine = _database()
    day = dt.date(2026, 8, 28)
    tab = day.strftime("%d.%m.%y")

    with _S(engine, expire_on_commit=False) as db:
        def _order(client, status, qty="2"):
            row = Order(source="lab", sheet_tab=tab, row_number=7, client_name=client,
                        quantity=qty, status=status)
            db.add(row)
            return row

        shown_a = _order("Vision", "нове")
        shown_b = _order("Vision", "нове")
        shown_c = _order("Неда", "нове")
        issued = _order("Полищук", "видано")
        # Архівне не має рахуватись узагалі: на екран воно не потрапить ніколи,
        # тож знаменник із ним лишався б недосяжним.
        archived = _order("Привид", "видано")
        archived.archived_at = dt.datetime(2026, 8, 29)
        db.commit()

        groups = [
            {"client_name": "Vision", "orders": [shown_a, shown_b], "all_found": False},
            {"client_name": "Неда", "orders": [shown_c], "all_found": False},
        ]
        totals = handout_day_totals(db, day, groups)
        assert totals["clients"] == 3, "два на екрані + один уже виданий"
        assert totals["clients_done"] == 1
        assert totals["works"] == 4
        assert totals["works_done"] == 1
        assert totals["units"] == 8
        assert "Привид" not in str(totals), "архівне не рахується"

        # Клієнта зібрано — «зроблених» більшає, ЗНАМЕННИК СТОЇТЬ.
        groups[1]["all_found"] = True
        shown_c.status = "знайдено при видачі"
        db.commit()
        after = handout_day_totals(db, day, groups)
        assert after["clients"] == totals["clients"], "знаменник не має рухатись"
        assert after["works"] == totals["works"]
        assert after["clients_done"] == 2 and after["works_done"] == 2


def test_letter_without_files_on_disk_is_never_ready():
    """Готовність без файлів неможлива за означенням. Бейдж «ГОТОВО» на листі,
    чиї файли видалили з диска, вів до прийняття роботи без жодного STL —
    оператор веде очима саме по списку."""
    from types import SimpleNamespace

    from app.triage_status import files_on_disk, triage_readiness

    gone = SimpleNamespace(saved_path="/definitely/not/here.stl", order_id=None)
    staged = SimpleNamespace(saved_path="/moved/to/export.stl", order_id=42)

    email = SimpleNamespace(
        service_type_guess=None, material_color_guess="Zr mono a3", attachments=[gone]
    )
    assert triage_readiness(email)["state"] == "no_files"
    assert files_on_disk(email) == 0

    # Прийняте в чергу вкладення рахується наявним: його файл переїхав у export.
    email.attachments = [staged]
    assert triage_readiness(email)["state"] == "ready"
    assert files_on_disk(email) == 1

    # Листа без вкладень взагалі цей стан не стосується.
    email.attachments = []
    assert triage_readiness(email)["state"] == "ready"
