"""Табло цеху на телевізор (14.09.26): верстати, принтер і печі за секретом.

Стережемо:
1. Стани, що вимагають людини (`done` — зняти, `fault`/`off` — іти дивитись),
   не зводяться до «не фрезерує»: інакше табло мовчить саме тоді, коли має
   кричати.
2. Один Sum3D може ділитись між кількома роботами — показуємо ПЕРШУ і чесне
   «ще N», а не вигаданого спільного клієнта.
3. SLM-принтер іде окремим місцем, а не рядовою коміркою сітки.
4. Табло цеху живе за тим самим секретом, що й печі, і не відкриває нічого
   іншого; сторінка логістів лишається на своєму місці.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from app.services import shop_board as sb
from tests.asgi_client import MiniClient
from tests.test_settings_slabs_render import ADMIN, app_db  # noqa: F401 — фікстура

NOW = datetime(2026, 9, 14, 14, 22)


def _card(name, state_key, *, percent=None, sum3d=None, orders=(), sisma=False,
          layers=None, word="", note="", machine_id=None):
    return SimpleNamespace(
        key=name,
        target=SimpleNamespace(name=name, machine_id=machine_id, portrait_model="",
                               show_on_board=True),
        state_key=state_key, state_word=word, state_note=note,
        percent=percent, sum3d_id=sum3d, orders=list(orders),
        is_sisma_machine=sisma, layers=layers,
    )


def _order(client=None, material=None, quantity=None, kind=None):
    return SimpleNamespace(id=0, client_name=client, material_color=material,
                           quantity=quantity, kind=kind, work_order_no=None)


def _view(cards):
    furnace = SimpleNamespace(cards=[], nearest_name="", nearest_at="",
                              nearest_day="", nearest_iso="")
    return sb.shop_view(None, token="t", now=NOW, cards=cards, furnace_view=furnace)


def test_states_that_need_a_human_keep_their_own_colour():
    """`done` і `fault` не зливаються з «стоїть»: до них треба підійти."""
    view = _view([
        _card("A", "run", percent=63),
        _card("B", "done", word="завершено", note="програма завершена · зняти"),
        _card("C", "fault", word="помилка"),
        _card("D", "off", word="немає зв'язку"),
        _card("E", "idle", word="стоїть"),
        _card("F", "busy", word="запуск"),
    ])
    tones = [(m.name, m.tone) for m in view.machines]
    assert tones == [
        ("A", "run"), ("B", "done"), ("C", "bad"),
        ("D", "bad"), ("E", "calm"), ("F", "busy"),
    ]


def test_unknown_state_falls_back_to_calm():
    """Новий стан у `machines` не має фарбувати табло навмання."""
    view = _view([_card("A", "щойно-придумали")])
    assert view.machines[0].tone == "calm"


def test_several_works_on_one_sum3d_show_the_first_and_say_so():
    view = _view([_card("A", "run", percent=10, sum3d="12-01-45", orders=[
        _order("Дента Люкс", "mono A3", "6", "анатомія"),
        _order("Ортос", "pmma A2", "2"),
    ])])
    m = view.machines[0]
    assert m.client == "Дента Люкс"
    assert m.material == "mono A3 · 6 · анатомія"
    assert m.extra == "ще 1", "друга робота не зникає й не змішується з першою"


def test_first_work_does_not_jump_between_refreshes():
    """Клієнт на телевізорі не має мінятись, поки на верстаті нічого не змінилось.

    `snapshot()` збирає роботи двома проходами й без сортування — екрану
    «Верстати» це байдуже, бо він показує всі. Табло показує ПЕРШУ, тому
    порядок тут фіксується сам.
    """
    a = _order("Ортос", "pmma A2", "2")
    a.id = 7
    b = _order("Дента Люкс", "mono A3", "6")
    b.id = 3
    straight = _view([_card("A", "run", percent=1, sum3d="X", orders=[a, b])])
    flipped = _view([_card("A", "run", percent=1, sum3d="X", orders=[b, a])])
    assert straight.machines[0].client == flipped.machines[0].client == "Дента Люкс"


def test_lab_work_falls_back_to_the_order_number():
    """У лабораторних робіт клієнта немає — там робота впізнається нарядом.

    Без запасного варіанта рядок лишався порожнім, і на телевізорі було
    видно лише матеріал (скарга з цеху 14.09.26).
    """
    lab = _order(None, "mono A3", "6", "анатомія")
    lab.work_order_no = "24122"
    view = _view([_card("A", "run", percent=10, sum3d="X", orders=[lab])])
    assert view.machines[0].client == "24122"

    client = _order("Дента Люкс", "mono A3", "6")
    client.work_order_no = "24122"
    view = _view([_card("A", "run", percent=10, sum3d="X", orders=[client])])
    assert view.machines[0].client == "Дента Люкс", "ім'я клієнта має перевагу"


def test_printer_shows_the_end_time_the_machine_promised():
    """Час кінця друку — прогноз САМОЇ машини; свого ми не рахуємо."""
    card = _card("SISMA", "run", sisma=True, layers=(412, 780))
    card.ends_at = datetime(2026, 9, 14, 20, 58)
    card.left_text = "лишилось 3 год 55 хв"
    view = _view([card])
    assert view.sisma.ends_at == "20:58"
    assert view.sisma.left_text == "лишилось 3 год 55 хв"


def test_printer_without_a_forecast_says_nothing():
    view = _view([_card("SISMA", "run", sisma=True, layers=(412, 780))])
    assert view.sisma.ends_at == "", "вигаданий час кінця гірший за жоден"


def test_machine_without_queue_match_has_no_invented_work():
    view = _view([_card("A", "run", percent=10, sum3d="12-01-45")])
    m = view.machines[0]
    assert (m.client, m.material, m.extra) == ("", "", "")
    assert m.sum3d == "12-01-45", "ID показуємо навіть без пари в черзі"


def test_printer_is_not_a_regular_cell():
    view = _view([
        _card("A", "run", percent=10),
        _card("SISMA", "run", sisma=True, layers=(412, 780)),
    ])
    assert [m.name for m in view.machines] == ["A"]
    assert view.sisma is not None
    assert (view.sisma.layer, view.sisma.layers_total) == (412, 780)
    assert view.sisma.sisma_percent == 53


def test_printer_without_layers_gives_no_percent():
    """Кадр протух — чисел немає; вигадувати відсоток не можна."""
    view = _view([_card("SISMA", "off", sisma=True, layers=None)])
    assert view.sisma.sisma_percent is None


def test_own_photo_wins_but_card_is_never_left_blank(monkeypatch):
    """Спершу фото з Налаштувань, а без нього — дефолт моделі.

    Половинчаста версія (є фото — показуємо, немає — нічого) лишала б картки
    голими на будь-якому цеху, де фото ще не завантажили.
    """
    import app.machine_portraits as mp

    monkeypatch.setattr(mp, "portrait_version", lambda mid: 7 if mid == 5 else None)
    view = _view([
        _card("350i · 1", "run", machine_id=5),   # своє фото є
        _card("250i dry", "run", machine_id=9),   # фото немає → дефолт моделі
        _card("350i loader", "run"),              # навіть без рядка в базі
    ])
    assert view.machines[0].portrait == "/t/t/portrait/5.jpg?v=7"
    assert view.machines[1].portrait == "/static/img/machine-portrait-250i-dry.jpg"
    assert view.machines[2].portrait == "/static/img/machine-portrait-350i-loader.jpg"


def test_model_chosen_in_settings_beats_the_guess_by_name():
    """Верстат, названий «350i L», здогадом не лоадер — і це лікується
    вибором моделі в Налаштуваннях, а не перейменуванням верстата."""
    guessed = _view([_card("350i L", "run")]).machines[0]
    assert guessed.portrait == "/static/img/machine-portrait-350i.jpg"

    card = _card("350i L", "run")
    card.target.portrait_model = "350i-loader"
    picked = _view([card]).machines[0]
    assert picked.portrait == "/static/img/machine-portrait-350i-loader.jpg"


def test_model_defaults_are_actually_served_by_the_board():
    """Білий список має містити саме ті файли, які підставляє сервіс.

    Інакше картка просить картинку, а табло віддає 404 — і ніхто про це не
    дізнається, бо фон просто лишається порожнім.
    """
    from pathlib import Path

    from app.routers import furnace_board as router
    from app.runtime import resource_path

    wanted = {m.portrait.rsplit("/", 1)[-1]
              for m in _view([_card(n, "run") for n in
                              ("350i · 1", "350i L", "250i", "250i dry")]).machines}
    assert wanted <= router._IMAGES, "дефолт моделі не віддається табло"
    img_dir = Path(resource_path("app/static")) / "img"
    for name in wanted:
        assert (img_dir / name).exists(), f"{name} немає на диску"


def test_machine_hidden_from_the_board_is_still_watched():
    """Галочка «Табло» — про місце на екрані, не про стеження.

    `enabled` означає «на ремонті» й зупиняє опитування; сплутати їх
    означало б разом із карткою втратити історію й обриви звʼязку.
    """
    shown = _card("A", "run", percent=10)
    hidden = _card("B", "run", percent=20)
    hidden.target.show_on_board = False
    view = _view([shown, hidden])
    assert [m.name for m in view.machines] == ["A"]


def test_printer_can_be_hidden_too():
    printer = _card("SISMA", "run", sisma=True, layers=(1, 2))
    printer.target.show_on_board = False
    assert _view([printer]).sisma is None


def test_board_keeps_the_order_it_was_given():
    """Порядок карток — той, у якому їх віддав `snapshot()`, тобто
    `sort_order` із Налаштувань. Табло нічого не пересортовує саме."""
    view = _view([_card(n, "run") for n in ("В", "А", "Б")])
    assert [m.name for m in view.machines] == ["В", "А", "Б"]


def test_grid_columns_follow_the_number_of_machines():
    """Сітка рахується від кількості: у цеху то дев'ять верстатів, то десять."""
    assert _view([_card(str(i), "run") for i in range(10)]).columns == 5
    assert _view([_card(str(i), "run") for i in range(9)]).columns == 5
    assert _view([_card(str(i), "run") for i in range(6)]).columns == 3
    assert _view([_card(str(i), "run") for i in range(3)]).columns == 3


def test_board_app_serves_shop_only_with_the_right_secret(monkeypatch):
    """Секрет той самий, що в печей; чужа адреса — 404, як і раніше."""
    from app.routers import furnace_board as router

    app = router.create_board_app()
    paths = {r.path for r in app.routes}
    assert "/t/{token}/shop" in paths
    assert "/t/{token}/shop/cards" in paths
    assert "/t/{token}/portrait/{machine_id}.jpg" in paths
    assert "/t/{token}" in paths, "сторінка логістів лишилась на місці"


def test_shop_page_really_renders(app_db, monkeypatch):  # noqa: F811
    """Справжній прохід ASGI → сервіс → шаблон.

    Зелений тест на самих даних доводить лише дані: сторінка може падати на
    рендері й ніхто цього не побачить (так у проєкті вже дев'ять днів жив
    зниклий шаблон). Тому тут — живий запит і пошук чисел у HTML.
    """
    from app.routers.furnace_board import create_board_app
    from app.services import furnace_board as fb
    from app.services import machines as machines_mod
    from tests.test_furnace_board import _enable

    _, factory = app_db
    token = _enable(factory)
    monkeypatch.setattr("app.db.SessionLocal", factory)
    # `shop_view` імпортує snapshot усередині функції — підміна модульного
    # атрибута влучає саме тому, що імпорт відбувається при виклику.
    monkeypatch.setattr(machines_mod, "snapshot", lambda db: [
        _card("350i · 1", "run", percent=63, sum3d="12-01-45",
              orders=[_order("Дента Люкс", "mono A3", "6")]),
        _card("350i · 8", "off", word="немає зв'язку", note="мовчить порт агента"),
        _card("SISMA", "run", sisma=True, layers=(412, 780)),
    ])
    monkeypatch.setattr(fb, "board_view", lambda db, now=None, cards=None: SimpleNamespace(
        cards=[SimpleNamespace(name="Піч 2", kind="run", temp=920, open_at="21:15",
                               open_day="", done_iso="", remaining="", note="")],
        nearest_name="Піч 2", nearest_at="21:15", nearest_day="", nearest_iso=""))

    board = MiniClient(create_board_app())
    status, headers, html = board.get(f"/t/{token}/shop")
    assert status == 200, "сторінка табло цеху не відрендерилась"
    assert headers.get("cache-control") == "no-store"
    for needle in ("350i · 1", "Дента Люкс", "12-01-45", "63", "SISMA", "412",
                   "Піч 2", "920", "21:15", "мовчить порт агента"):
        assert needle in html, needle

    status, _, frag = board.get(f"/t/{token}/shop/cards")
    assert status == 200 and 'id="shop-live"' in frag
    # Верстат БЕЗ програми не має підписуватись «фрезерує». У стані `run`
    # власного слова в системі немає (там число), і порожній підпис раніше
    # читався саме так — у демо працює рівно один верстат.
    assert frag.count("фрезерує") == 1, "підпис «фрезерує» дістався не тому верстату"
    assert board.get("/t/wrong/shop")[0] == 404
    assert board.get("/t/wrong/shop/cards")[0] == 404
    # Полотно анімації віддається, а решта теки js — ні.
    assert board.get("/static/js/shop_board_slm.js")[0] == 200
    assert board.get("/static/js/app.js")[0] == 404


def test_moving_a_machine_reorders_the_list(app_db):  # noqa: F811
    """Стрілки міняють верстат місцями з сусідом — і на рівних `sort_order`.

    У старих базах усі рядки мають нуль, тож «мінус один» нічого б не
    змінив. Роут спершу перенумеровує перелік за поточним порядком, і лише
    потім міняє двох місцями.
    """
    from datetime import datetime as dt

    from app.models import Machine
    from app.services import machines as machines_mod

    app, factory = app_db
    with factory() as db:
        for name in ("А", "Б", "В"):
            db.add(Machine(name=name, host=f"10.0.0.{len(name) + ord(name) % 20}",
                           port=5900, sort_order=0, created_at=dt(2026, 9, 15)))
        db.commit()
        ids = {m.name: m.id for m in machines_mod.list_machines(db)}

    def order():
        with factory() as db:
            return [m.name for m in machines_mod.list_machines(db)]

    client = MiniClient(app)
    client.login(*ADMIN)
    assert client.post(f"/settings/machines/{ids['В']}/move", {"direction": "up"})[0] == 303
    assert order() == ["А", "В", "Б"], "рівні sort_order не завадили обміну"

    client.post(f"/settings/machines/{ids['А']}/move", {"direction": "down"})
    assert order() == ["В", "А", "Б"]

    # Край переліку — не помилка й не перестановка по колу.
    first = order()[0]
    with factory() as db:
        top = next(m.id for m in machines_mod.list_machines(db) if m.name == first)
    assert client.post(f"/settings/machines/{top}/move", {"direction": "up"})[0] == 303
    assert order()[0] == first, "верхній верстат лишився вгорі"


def test_new_machine_appears_on_the_board_at_the_end(app_db):  # noqa: F811
    """Доданий верстат одразу видно на табло, і він стає ОСТАННІМ.

    Два місця, де це могло тихо зламатись: новий рядок мусить отримати
    `show_on_board=True` за замовчуванням (інакше верстат є, а на
    телевізорі його нема й ніхто не розуміє чому), і `sort_order` більший
    за наявні — інакше він уклинився б у середину вже звиклого порядку.
    """
    from datetime import datetime as dt

    from app.models import Machine
    from app.services import machines as machines_mod

    app, factory = app_db
    with factory() as db:
        for i, name in enumerate(("А", "Б")):
            db.add(Machine(name=name, host=f"10.1.0.{i + 1}", port=5900,
                           sort_order=i, created_at=dt(2026, 9, 15)))
        db.commit()

    client = MiniClient(app)
    client.login(*ADMIN)
    status, _, _ = client.post("/settings/machines", {
        "name": "Новий", "host": "10.1.0.9", "port": "5900",
        "password": "", "agent_token": "", "portrait_model": "",
    })
    assert status == 303

    with factory() as db:
        items = machines_mod.list_machines(db)
        assert [m.name for m in items] == ["А", "Б", "Новий"], "новий став у кінець"
        added = items[-1]
        assert added.show_on_board is True, "новий верстат має бути видно на табло"
        # Той самий шлях, яким табло читає верстати.
        target = machines_mod.target_of(added)
        assert target.show_on_board is True


def test_settings_page_shows_the_shop_link(app_db):  # noqa: F811
    """Сторінка налаштувань не падає й називає адресу телевізора.

    Перевіряється саме РЕНДЕР: пропущена змінна в шаблоні дає тихий 500,
    який ніде більше не видно (так у проєкті вже жив зниклий партіал).
    """
    from tests.test_furnace_board import _enable

    app, factory = app_db
    token = _enable(factory)
    client = MiniClient(app)
    client.login(*ADMIN)
    status, _, html = client.get("/settings/feedback")
    assert status == 200, "сторінка налаштувань не відрендерилась"
    # Рахувати кнопки «Скопіювати» не можна: на цій сторінці є ще й
    # запрошення в Telegram, і тест проходив би навіть із невідрендереним
    # посиланням табло. Тому дивимось на самі адреси.
    import re

    copied = set(re.findall(r'data-copy="(http[^"]+)"', html))
    assert any(u.endswith(f"/t/{token}") for u in copied), "посилання логістів зникло"
    assert any(u.endswith(f"/t/{token}/shop") for u in copied), "немає посилання на табло цеху"
