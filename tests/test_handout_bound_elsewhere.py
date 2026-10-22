"""Тека, закріплена за іншою роботою, не пропонується нечітким збігом (22.10.26).

Бойовий випадок 20.10.26: лист клієнта на 5 одиниць прийнято в теку
`<клієнт>/20.10.26/mono a2` (`Order.export_folder_path`), а рядок на 2 одиниці,
вписаний у таблицю руками, нечітким збігом діставав ту саму теку — у прев'ю
та сама коронка показувалась двічі. Сторож «закріплена — не пропонувати»
стояв за вимикачем ПРИЗУПИНЕНОЇ прив'язки тек (`folder_binding.ENABLED`), хоча
закріплює й прийняття листа, яке працює завжди.

Імена клієнтів тут вигадані.
"""

from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace

from app.export_scanner import SubBatch
from app.services import folder_binding
from app.services.handout import folder_taken, pick_export_folders, pins_by_folder

DAY = date(2026, 10, 20)
ALPHA = "Клієнт Альфа"


def _entry(batch="20.10.26", material="mono a2", created=datetime(2026, 10, 20, 15, 44, 12),
           files=(), parts=(), client=ALPHA):
    return SimpleNamespace(
        client_folder_name=client, batch_folder_name=batch,
        material_color_folder_name=material, created_at=created,
        files=list(files), parts=tuple(parts), subfolders=0,
        folder_path=f"X:/{client}/{batch}/{material}",
    )


def _order(oid, quantity="1", path=None, material="mono a2"):
    return SimpleNamespace(id=oid, quantity=quantity, export_folder_path=path,
                           material_color=material)


FIVE_TEETH = ["2026-10-20_99999-002.constructionInfo",
              "2026-10-20_99999-002-11-12-21-22-23-bridge_cad.stl"]


def test_folder_of_a_letter_is_not_offered_to_a_typed_row():
    """20.10.26: тека листа на 5 одиниць — лише його; рядок на 2 одиниці каже
    «за 20.10 теки немає» (день — від чужої теки), а не показує ту саму коронку."""
    entry = _entry(files=FIVE_TEETH)
    letter = _order(1, "5", f"{ALPHA}/20.10.26/mono a2")
    typed = _order(2, "2")
    pins = pins_by_folder([letter, typed])

    shown, stale, taken = pick_export_folders("mono a2", [entry], DAY, (), typed.id, pins)

    assert shown == [] and taken == [entry]
    assert stale == DAY


def test_without_pins_nothing_changes():
    entry = _entry(files=FIVE_TEETH)
    shown, stale, taken = pick_export_folders("mono a2", [entry], DAY, (), 2, {})
    assert shown == [entry] and stale is None and taken == []


def test_guard_does_not_depend_on_the_paused_binding_switch():
    assert folder_binding.ENABLED is False  # пауза лишається, сторож — ні
    entry = _entry(files=FIVE_TEETH)
    pins = pins_by_folder([_order(1, "5", f"{ALPHA}/20.10.26/mono a2")])
    assert folder_taken(entry, pins, 2)


def test_own_pin_does_not_hide_the_folder_from_itself():
    """Робота, чия тека зникла з диска, шукає нечітко — свою закріплену не ховаємо."""
    entry = _entry(files=FIVE_TEETH)
    letter = _order(1, "5", f"{ALPHA}/20.10.26/mono a2")
    assert not folder_taken(entry, pins_by_folder([letter]), letter.id)


def test_letter_split_into_several_rows_keeps_the_folder_for_the_rest():
    """08.10.26: технік зменшив кількість поштового рядка до 1 і дописав рядок на
    решту — у теці 3 зуби, закріплена робота claimed 1, тож теку лишаємо."""
    entry = _entry(files=["a-11-crown_cad.stl", "b-21-crown_cad.stl", "c-22-crown_cad.stl"])
    pins = pins_by_folder([_order(1, "1", f"{ALPHA}/20.10.26/mono a2")])
    shown, stale, taken = pick_export_folders("mono a2", [entry], DAY, (), 2, pins)
    assert shown == [entry] and taken == []


def test_folder_shared_by_two_letters_is_theirs_together():
    """Дві поштові роботи в одній теці (зведення / вибір наявної теки): разом
    вони покривають усі 3 зуби — третій роботі теку не пропонуємо, а одна одній
    вони її не ховають (кожна бачить свою теку напряму, а без неї — через
    сусідку, що лишилась у переліку власників)."""
    path = f"{ALPHA}/20.10.26/mono a2"
    entry = _entry(files=["a-11-crown_cad.stl", "b-21-22-bridge_cad.stl"])
    first, second = _order(1, "1", path), _order(2, "2", path)
    pins = pins_by_folder([first, second])
    assert folder_taken(entry, pins, 3)
    # Для самих власників тека «чужа» лише через сусідку, і екран їй це не
    # показує: вони беруть теку напряму (`_direct_export_entry`).
    assert pins[folder_binding.rel_key(path)] == [first, second]


def test_names_without_teeth_prove_no_room():
    entry = _entry(files=["model.stl"])
    pins = pins_by_folder([_order(1, "5", f"{ALPHA}/20.10.26/mono a2")])
    assert folder_taken(entry, pins, 2)


def test_partly_bound_multi_part_folder_stays_offered():
    """Тека кольору з двома партіями: закріплено лише корінь — підтека нічия,
    тож теку пропонуємо й далі (вирішує оператор за STL)."""
    entry = _entry(
        files=["root-36-crown_cad.stl"],
        parts=(SubBatch(datetime(2026, 10, 20, 15, 44, 20), "root-36-crown_cad.stl"),
               SubBatch(datetime(2026, 10, 20, 18, 2, 0), "Новая папка/x-46-crown_cad.stl")),
    )
    pins = pins_by_folder([_order(1, "1", f"{ALPHA}/20.10.26/mono a2")])
    assert not folder_taken(entry, pins, 2)
    both = pins_by_folder([_order(1, "1", f"{ALPHA}/20.10.26/mono a2"),
                           _order(4, "1", f"{ALPHA}/20.10.26/mono a2/Новая папка")])
    assert folder_taken(entry, both, 2)


def test_guard_only_removes_it_never_brings_a_later_folder():
    """Чужа тека дня прибирається, але «найраніша пізніша» на її місце НЕ
    приходить: це була б теж чужа робота (теки дня просто немає)."""
    today = _entry(files=FIVE_TEETH)
    later = _entry(batch="22.10.26", created=datetime(2026, 10, 22, 15, 8, 14),
                   files=["z-14-crown_cad.stl"])
    pins = pins_by_folder([_order(1, "5", f"{ALPHA}/20.10.26/mono a2")])
    shown, stale, taken = pick_export_folders("mono a2", [today, later], DAY, (), 2, pins)
    assert shown == [] and taken == [today] and stale == DAY


def test_other_day_pins_of_the_same_client_do_not_touch_this_day():
    """Закріплені теки інших днів не впливають: їх і так не показують цьому дню."""
    mine = _entry(files=["q-11-crown_cad.stl"])
    yesterday = _entry(batch="19.10.26", created=datetime(2026, 10, 19, 20, 3, 20),
                       files=FIVE_TEETH)
    pins = pins_by_folder([_order(1, "5", f"{ALPHA}/19.10.26/mono a2")])
    shown, stale, taken = pick_export_folders("mono a2", [mine, yesterday], DAY, (), 2, pins)
    assert shown == [mine] and taken == []


# ── Справжній рендер /handout ────────────────────────────────────────────────


def _render(app_db, monkeypatch, tmp_path, orders, scanned):  # noqa: F811
    from sqlalchemy import select

    import app.web as web
    from app.models import Order, User
    from app.routers import handout as handout_router_mod
    from tests.asgi_client import MiniClient
    from tests.test_settings_slabs_render import OPERATOR

    app, factory = app_db
    with factory() as db:
        user = db.scalars(select(User).where(User.username == OPERATOR[0])).one()
        user.handout_layout = "nav"
        for kw in orders:
            db.add(Order(**kw))
        db.commit()

    for e in scanned:  # нечіткий збіг дає посилання на справжню теку тесту
        e.folder_path = tmp_path / e.client_folder_name / e.batch_folder_name / e.material_color_folder_name
    names = sorted({e.client_folder_name for e in scanned} | {ALPHA})
    monkeypatch.setattr(handout_router_mod, "get_export_folder_path", lambda db: str(tmp_path))
    monkeypatch.setattr(handout_router_mod, "list_export_client_names_cached", lambda root: names)
    monkeypatch.setattr(web, "list_export_client_names_cached", lambda root: names)
    monkeypatch.setattr(handout_router_mod, "scan_export_for_clients",
                        lambda root, folders, nb: {ALPHA: list(scanned)})
    monkeypatch.setattr(handout_router_mod, "scan_export_latest_for_clients", lambda root, folders: {})
    monkeypatch.setattr(handout_router_mod, "scan_export_client_cached", lambda root, f, nb: list(scanned))
    monkeypatch.setattr(handout_router_mod, "scan_export_client_latest_cached", lambda root, f: [])

    client = MiniClient(app)
    client.login(*OPERATOR)
    status, _, html = client.get("/handout?source=all&day=20.10.26")
    assert status == 200, html[:300]
    return html


def _folder(tmp_path, rel, files):
    path = tmp_path.joinpath(*rel.split("/"))
    path.mkdir(parents=True)
    for name in files:
        (path / name).write_bytes(b"solid\n")
    return path


def test_handout_screen_shows_the_letter_folder_once(app_db, monkeypatch, tmp_path):  # noqa: F811
    """Сценарій 20.10.26 цілком: лист на 5 одиниць (своя тека), рядок на 2,
    вписаний руками (без теки), і лист, переписаний з іншого клієнта (тека
    лежить у того клієнта). Тека першого листа — рівно під ним."""
    from app.order_folder import folder_to_file_uri

    own = _folder(tmp_path, f"{ALPHA}/20.10.26/mono a2", FIVE_TEETH)
    other = _folder(tmp_path, "Клієнт Бета/20.10.26/mono a2 (4)", ["k-21-22-bridge_cad.stl"])
    common = dict(sheet_tab="20.10.26", client_name=ALPHA, material_color="mono a2",
                  sum3d_id="15-35-56", status="знайдено при видачі")
    html = _render(app_db, monkeypatch, tmp_path, [
        dict(source="email", row_number=74, quantity="5",
             export_folder_path=f"{ALPHA}/20.10.26/mono a2", **common),
        dict(source="sheet_client", row_number=75, quantity="2", **common),
        dict(source="email", row_number=76, quantity="2",
             export_folder_path="Клієнт Бета/20.10.26/mono a2 (4)", **common),
    ], [_entry(files=FIVE_TEETH)])

    assert html.count(f'href="{folder_to_file_uri(own)}"') == 1
    assert html.count(f'href="{folder_to_file_uri(other)}"') == 1
    assert html.count("за 20.10 теки немає") == 1


def test_handout_screen_two_letters_in_one_folder_both_see_it(app_db, monkeypatch, tmp_path):  # noqa: F811
    from app.order_folder import folder_to_file_uri

    rel = f"{ALPHA}/20.10.26/mono a2"
    files = ["a-11-crown_cad.stl", "b-21-22-bridge_cad.stl"]
    shared = _folder(tmp_path, rel, files)
    common = dict(sheet_tab="20.10.26", client_name=ALPHA, material_color="mono a2",
                  status="знайдено при видачі")
    html = _render(app_db, monkeypatch, tmp_path, [
        dict(source="email", row_number=70, quantity="1", export_folder_path=rel, **common),
        dict(source="email", row_number=71, quantity="2", export_folder_path=rel, **common),
        dict(source="sheet_client", row_number=72, quantity="1", **common),
    ], [_entry(files=files)])

    assert html.count(f'href="{folder_to_file_uri(shared)}"') == 2
    assert html.count("за 20.10 теки немає") == 1


def test_handout_screen_archived_pin_does_not_hide(app_db, monkeypatch, tmp_path):  # noqa: F811
    """Архівна робота у видачу не потрапляє — і її закріплена тека нікого не
    відсікає (рішення 22.10.26: сторож дивиться лише на роботи видачі)."""
    from app.order_folder import folder_to_file_uri

    rel = f"{ALPHA}/20.10.26/mono a2"
    folder = _folder(tmp_path, rel, FIVE_TEETH)
    common = dict(sheet_tab="20.10.26", client_name=ALPHA, material_color="mono a2",
                  status="знайдено при видачі")
    html = _render(app_db, monkeypatch, tmp_path, [
        dict(source="email", row_number=74, quantity="5", export_folder_path=rel,
             archived_at=datetime(2026, 10, 21, 10, 0), **common),
        dict(source="sheet_client", row_number=75, quantity="2", **common),
    ], [_entry(files=FIVE_TEETH)])

    assert html.count(f'href="{folder_to_file_uri(folder)}"') == 1
    assert "за 20.10 теки немає" not in html


def test_diag_names_the_folder_taken_by_another_work(tmp_path, monkeypatch):
    """`kmill_handout_match` каже те саме, що екран: тека дня чужа."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from sqlalchemy.pool import StaticPool

    from app.business_day import business_today
    from app.db import Base
    from app.models import Order
    from app.services import handout_diag

    tab = business_today()
    batch = tab.strftime("%d.%m.%y")
    _folder(tmp_path, f"{ALPHA}/{batch}/mono a2", FIVE_TEETH)
    monkeypatch.setattr(handout_diag, "get_export_folder_path", lambda db: str(tmp_path))
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        common = dict(client_name=ALPHA, material_color="mono a2", sheet_tab=batch,
                      status="прораховано")
        db.add(Order(source="email", quantity="5",
                     export_folder_path=f"{ALPHA}/{batch}/mono a2", **common))
        typed = Order(source="sheet_client", quantity="2", **common)
        db.add(typed)
        db.commit()

        r = handout_diag.diagnose_handout_client(db, str(typed.id))
        row = next(w for w in r["роботи"] if w["id"] == typed.id)
        assert row["результат"].startswith("тека дня закріплена за іншою роботою"), row


from tests.test_settings_slabs_render import app_db  # noqa: E402,F401 — фікстура
