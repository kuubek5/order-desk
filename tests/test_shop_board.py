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
          layers=None, word="", note="", machine_id=None, last_orders=(),
          iso_name=None):
    return SimpleNamespace(
        key=name,
        target=SimpleNamespace(name=name, machine_id=machine_id, portrait_model="",
                               show_on_board=True),
        state_key=state_key, state_word=word, state_note=note,
        percent=percent, sum3d_id=sum3d, orders=list(orders),
        last_orders=list(last_orders),
        is_sisma_machine=sisma, layers=layers, iso_name=iso_name,
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


def test_two_works_on_one_sum3d_are_both_named():
    """Дві роботи на один Sum3D називаються ОБИДВІ, а не «перша + 1».

    Доти другий клієнт лишався безіменним саме на екрані, куди дивляться,
    щоб зрозуміти, чия це заготовка (власник 15.09.26).
    """
    view = _view([_card("A", "run", percent=10, sum3d="12-01-45", orders=[
        _order("Дента Люкс", "mono A3", "6", "анатомія"),
        _order("Ортос", "pmma A2", "2"),
    ])])
    m = view.machines[0]
    assert [(w.name, w.material) for w in m.works] == [
        ("Дента Люкс", "mono A3 · 6 · анатомія"),
        ("Ортос", "pmma A2 · 2"),
    ]
    assert m.extra == "", "дві роботи вміщаються поіменно — рахувати нічого"
    assert (m.client, m.material) == ("Дента Люкс", "mono A3 · 6 · анатомія")


def test_third_work_is_counted_not_named():
    """Третя вже не вміщається — за нею «ще N», а не вигаданий спільний клієнт."""
    view = _view([_card("A", "run", percent=10, sum3d="12-01-45", orders=[
        _order("Дента Люкс", "mono A3", "6"),
        _order("Ортос", "pmma A2", "2"),
        _order("Смайл", "tit", "1"),
        _order("Люмі", "wax", "4"),
    ])])
    m = view.machines[0]
    assert [w.name for w in m.works] == ["Дента Люкс", "Ортос"]
    assert m.extra == "ще 2"


def test_work_without_a_name_and_material_is_not_a_line():
    """Порожня робота не має робити картку «роботою»: доти вона малювала «—»
    замість пояснення стану."""
    view = _view([_card("A", "run", percent=10, sum3d="X",
                        orders=[_order(None, None, None)])])
    assert view.machines[0].works == []


def test_finished_machine_shows_the_work_it_just_milled():
    """Два завершені верстати в одному ряду мусять виглядати однаково.

    На RemiCORE назва програми лишається в заголовку й після кінця, а екран
    SUMMARY нового покоління її не несе — і там картка лишалась порожньою
    (скарга з цеху 15.09.26). Тепер вона називає останню роботу, але ЗАВЖДИ
    підписану: без підпису це читалось би як робота, що йде просто зараз.
    """
    view = _view([_card("A", "done", word="завершено",
                        note="програма завершена · зняти",
                        last_orders=[_order("Дента Люкс", "mono A3", "6", "анатомія")])])
    m = view.machines[0]
    assert [w.name for w in m.works] == ["Дента Люкс"]
    assert m.works_are_last is True, "щойно знята робота мусить бути підписана"


def test_finished_machine_says_so_even_when_its_work_is_still_matched():
    """Завершений верстат мусить казати це РЯДКОМ, а не лише словом у кутку.

    Бойовий випадок 17.09.26: три верстати стояли завершені, але їхні роботи
    ще зчеплювались із чергою за Sum3D — і картка малювала клієнта, точнісінько
    як у машини під фрезою. Власник прочитав це як «різні статуси на верстатах»
    і мав рацію: слово «ЗАВЕРШЕНО» дрібним у кутку з проходу не видно.

    Заклик до дії при цьому рівно один: підпис каже «завершено», а «ЗНЯТИ»
    велике їде вниз замість смуги — тому хвіст «· зняти» з пояснення зрізаний.
    """
    view = _view([_card("A", "done", percent=100, sum3d="12-00-54",
                        word="завершено", note="програма завершена · зняти",
                        orders=[_order("Дента Люкс", "mono A3", "6")])])
    m = view.machines[0]
    assert [w.name for w in m.works] == ["Дента Люкс"], "клієнта не ховаємо"
    assert m.action == "завершено", "завершений верстат мусить мати підпис"
    assert m.note == "програма завершена", "«зняти» лишається одне, внизу"


def test_running_machine_has_no_action_label():
    """Підпис — сигнал «до мене треба підійти», а не оздоба кожної картки."""
    view = _view([_card("A", "run", percent=40, sum3d="X",
                        orders=[_order("Ортос", "pmma A2", "2")])])
    assert view.machines[0].action == ""


def test_current_work_wins_over_the_last_one():
    """Поки програма на екрані є — показуємо саме її, без підпису «остання»."""
    view = _view([_card("A", "run", percent=40, sum3d="X",
                        orders=[_order("Ортос", "pmma A2", "2")],
                        last_orders=[_order("Дента Люкс", "mono A3", "6")])])
    m = view.machines[0]
    assert [w.name for w in m.works] == ["Ортос"]
    assert m.works_are_last is False


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
    assert [w.name for w in straight.machines[0].works] ==            [w.name for w in flipped.machines[0].works], "і порядок обох імен теж"


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


def test_lab_work_shows_the_technician_next_to_the_order_number():
    """Власник 29.09.26: у лабораторної роботи — ще й імʼя техніка. У
    клієнтської — ні (там уже імʼя замовника, техніка в листі немає)."""
    lab = _order(None, "mono A3", "6", "анатомія")
    lab.work_order_no = "24122"
    lab.technician_name = "Коваленко"
    view = _view([_card("A", "run", percent=10, sum3d="X", orders=[lab])])
    work = view.machines[0].works[0]
    assert (work.name, work.tech) == ("24122", "Коваленко")
    assert view.machines[0].client == "24122", "наряд лишається головним"

    client = _order("Дента Люкс", "mono A3", "6")
    client.technician_name = "Коваленко"
    view = _view([_card("A", "run", percent=10, sum3d="X", orders=[client])])
    assert view.machines[0].works[0].tech == ""


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
    assert (m.client, m.material, m.extra, m.works) == ("", "", "", [])
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


def test_sisma_fills_the_free_cells_of_the_last_row():
    """Власник 05.10.26: 8 верстатів + SISMA = 9 карток у 5 колонках — одна
    клітинка вільна, SISMA займає дві. Додали верстат (10 карток) — вільних
    немає, SISMA знову одна. Без SISMA — нічого не тягнеться."""
    sis = _card("SISMA", "run", sisma=True, layers=(10, 100))
    nine = _view([_card(str(i), "run") for i in range(8)] + [sis])
    assert (nine.columns, nine.sisma_span) == (5, 2)
    ten = _view([_card(str(i), "run") for i in range(9)] + [sis])
    assert (ten.columns, ten.sisma_span) == (5, 1)
    seven = _view([_card(str(i), "run") for i in range(6)] + [sis])
    assert (seven.columns, seven.sisma_span) == (4, 2)
    assert _view([_card(str(i), "run") for i in range(8)]).sisma_span == 1


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


def test_fault_calls_a_human_as_loudly_as_done(app_db, monkeypatch):  # noqa: F811
    """Помилка на верстаті — на телевізорі так само голосно, як «ЗНЯТИ».

    Власник 18.09.26: коли на верстаті помилка (350i Loader уночі: «Reference
    run required!»), це має бути видно на табло цеху. Доти `fault` мав лише
    червону рамку — ту саму, що й «немає звʼязку», і з проходу обидва стани
    читались однаково. Тепер у помилки своя картка (`is-fault`, пульсує) і
    велике слово замість смуги; «немає звʼязку» лишилось як було."""
    from app.routers.furnace_board import create_board_app
    from app.services import furnace_board as fb
    from app.services import machines as machines_mod
    from tests.test_furnace_board import _enable

    _, factory = app_db
    token = _enable(factory)
    monkeypatch.setattr("app.db.SessionLocal", factory)
    monkeypatch.setattr(machines_mod, "snapshot", lambda db: [
        _card("350i Loader", "fault", percent=40, word="помилка",
              note="верстат показує помилку · підійдіть"),
        _card("250i", "off", word="немає зв'язку", note="мовчить порт агента"),
    ])
    monkeypatch.setattr(fb, "board_view", lambda db, now=None, cards=None: SimpleNamespace(
        cards=[], nearest_name="", nearest_at="", nearest_day="", nearest_iso=""))

    status, _, frag = MiniClient(create_board_app()).get(f"/t/{token}/shop/cards")
    assert status == 200
    loader, offline = frag.split("250i", 1)[0], frag.split("250i", 1)[1]
    assert "is-fault" in loader and "підійти" in loader
    # Смуга відсотка на картці з помилкою не малюється: «40 %» читалось би як
    # «працює», хоча верстат стоїть і чекає людину.
    assert 'class="bar"' not in loader
    assert 'class="disc"' not in loader, "диск із відсотком на помилці теж читався б як «працює»"
    assert "is-fault" not in offline and "підійти" not in offline


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


def test_machine_rustdesk_id_is_saved_cleared_and_validated(app_db):  # noqa: F811
    """ID RustDesk іде тією самою формою рядка: зберігається без пробілів,
    порожнє поле прибирає посилання, недопустимі символи — помилка, і тоді
    НЕ зберігається нічого з рядка (як з невалідною адресою)."""
    from datetime import datetime as dt

    from app.models import Machine

    app, factory = app_db
    with factory() as db:
        m = Machine(name="350i", host="10.0.0.9", port=5900, created_at=dt(2026, 9, 30))
        db.add(m)
        db.commit()
        mid = m.id

    def rid():
        with factory() as db:
            return db.get(Machine, mid).rustdesk_id

    client = MiniClient(app)
    client.login(*ADMIN)
    form = {"name": "350i", "host": "10.0.0.9", "port": "5900", "enabled": "1", "show_on_board": "1"}
    assert client.post(f"/settings/machines/{mid}", {**form, "rustdesk_id": "123 456 789"})[0] == 303
    assert rid() == "123456789"
    client.post(f"/settings/machines/{mid}", {**form, "name": "змінено", "rustdesk_id": "12?password=x"})
    assert rid() == "123456789"
    with factory() as db:
        assert db.get(Machine, mid).name == "350i", "невалідний ID не зберіг і решту рядка"
    client.post(f"/settings/machines/{mid}", {**form, "rustdesk_id": ""})
    assert rid() == ""


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


def test_dancing_cat_shows_over_100_percent_only(app_db, monkeypatch):  # noqa: F811
    """Котик (власник 02.10.26): танцює лише над числом «100%». Над «зняти» —
    ні («не над зняти, а над 100%»), на інших відсотках і на помилці — теж ні.

    Два котики накладені в одній картці (по черзі перемикає клас на body), а
    `data-cat` — ключ верстата, за яким живого котика переносять при
    оновленні шматка. Обидві картинки мусять реально віддаватись табло —
    інакше над числом був би порожній квадрат."""
    from app.routers.furnace_board import create_board_app
    from app.services import furnace_board as fb
    from app.services import machines as machines_mod
    from tests.test_furnace_board import _enable

    _, factory = app_db
    token = _enable(factory)
    monkeypatch.setattr("app.db.SessionLocal", factory)
    monkeypatch.setattr(machines_mod, "snapshot", lambda db: [
        _card("M-done", "done", percent=100),
        _card("M-100", "run", percent=100),
        _card("M-63", "run", percent=63),
        _card("M-bad", "fault", percent=100, word="помилка"),
    ])
    monkeypatch.setattr(fb, "board_view", lambda db, now=None, cards=None: SimpleNamespace(
        cards=[], nearest_name="", nearest_at="", nearest_day="", nearest_iso=""))

    board = MiniClient(create_board_app())
    status, _, frag = board.get(f"/t/{token}/shop/cards")
    assert status == 200
    parts = {name: frag.split(name, 1)[1].split('class="mc ', 1)[0]
             for name in ("M-done", "M-100", "M-63", "M-bad")}
    assert 'class="mc-cat"' in parts["M-100"]
    assert 'class="mc-cat"' not in parts["M-done"], "над «зняти» котика немає"
    assert 'class="mc-cat"' not in parts["M-63"]
    assert 'class="mc-cat"' not in parts["M-bad"], "на помилці котик не танцює"
    # Один котик у картці — одна картинка; другого підставляє скрипт по черзі
    # (два накладені на телевізорі показувались обидва, власник 03.10.26).
    cat = parts["M-100"].split('class="mc-cat"', 1)[1].split("</span>", 1)[0]
    assert cat.count("<img") == 1 and "shop-cat-a.webp" in cat

    for name in ("shop-cat-a.webp", "shop-cat-b.webp"):
        status, headers, _ = board.get(f"/static/img/{name}")
        assert status == 200, name
        assert headers.get("content-type", "").startswith("image/webp"), name


# ── Моушн ТВ 3/4/5 (власник 11.10.26) ───────────────────────────────────────


def test_disc_units_only_when_every_quantity_is_a_number():
    """Коронки на диску — лише з відомої кількості. Одна робота без числа —
    і диск лишається порожнім (дуга без коронок), а не вгадує."""
    assert sb.disc_units([_order(quantity="4"), _order(quantity="3")]) == 7
    assert sb.disc_units([_order(quantity="4"), _order(quantity="")]) is None
    assert sb.disc_units([_order(quantity="2+1")]) is None
    assert sb.disc_units([]) is None
    assert sb.disc_units([_order(quantity=str(sb.DISC_MAX_UNITS + 1))]) is None


def test_disc_crowns_fit_the_disc_without_overlap_and_stay_put():
    import math

    for units in range(1, sb.DISC_MAX_UNITS + 1):
        crowns = sb.disc_crowns(units, 50)
        assert len(crowns) == units
        for c in crowns:
            assert math.hypot(c.x - 50, c.y - 50) + c.r <= 42, (units, c)
        for i, a in enumerate(crowns):
            for b in crowns[i + 1:]:
                assert math.hypot(a.x - b.x, a.y - b.y) >= a.r + b.r, (units, a, b)
        # Розкладка не залежить від відсотка: коронки не стрибають по диску.
        assert [(c.x, c.y) for c in sb.disc_crowns(units, 90)] == [(c.x, c.y) for c in crowns]


def test_disc_cuts_whole_units_and_arc_follows_percent():
    assert [c.cut for c in sb.disc_crowns(7, 99)].count(True) == 6, "на 99 % остання ще не вирізана"
    assert all(c.cut for c in sb.disc_crowns(7, 100))
    assert not any(c.cut for c in sb.disc_crowns(7, 0))
    assert sb.disc_crowns(None, 50) == [] and sb.disc_crowns(7, None) == []
    full = _view([_card("A", "run", percent=100, orders=[_order("К", "pmma", "3")])]).machines[0]
    empty = _view([_card("A", "run", percent=0, orders=[_order("К", "pmma", "3")])]).machines[0]
    assert full.arc_offset == 0 and empty.arc_offset == full.arc_dash
    # Щойно зняті роботи на диск не потрапляють: він про ту, що на шпинделі.
    done = _view([_card("A", "done", last_orders=[_order("К", "pmma", "3")])]).machines[0]
    assert done.units is None


def test_board_draws_the_disc_and_alarm_layer(app_db, monkeypatch):  # noqa: F811
    """Справжній рендер: диск із лунками замість смужки, шар тривоги лише в
    червоних карток і дані для переходів у розмітці."""
    from app.routers.furnace_board import create_board_app
    from app.services import furnace_board as fb
    from app.services import machines as machines_mod
    from tests.test_furnace_board import _enable

    _, factory = app_db
    token = _enable(factory)
    monkeypatch.setattr("app.db.SessionLocal", factory)
    monkeypatch.setattr(machines_mod, "snapshot", lambda db: [
        _card("M-run", "run", percent=43, sum3d="X", orders=[_order("К", "pmma a2", "7")]),
        _card("M-unknown", "run", percent=43, sum3d="Y", orders=[_order("К", "pmma a2", "")]),
        _card("M-fault", "fault", percent=40, word="помилка"),
        _card("M-off", "off", word="немає зв'язку", note="мовчить порт агента"),
    ])
    monkeypatch.setattr(fb, "board_view", lambda db, now=None, cards=None: SimpleNamespace(
        cards=[], nearest_name="", nearest_at="", nearest_day="", nearest_iso=""))

    status, _, frag = MiniClient(create_board_app()).get(f"/t/{token}/shop/cards")
    assert status == 200
    parts = {name: frag.split(f'data-key="{name}"', 1)[1].split('class="mc ', 1)[0]
             for name in ("M-run", "M-unknown", "M-fault", "M-off")}
    run = parts["M-run"]
    assert 'class="disc"' in run and 'data-pct="43"' in run and 'data-cut="3"' in run
    assert run.count('class="crown') == 7 and run.count("is-cut") == 3
    assert 'class="bar"' not in frag, "смужки більше немає"
    unknown = parts["M-unknown"]
    assert 'class="disc"' in unknown and 'class="crown' not in unknown, "невідома кількість — без коронок"
    assert 'class="mc-alarm"' in parts["M-fault"] and 'class="mc-alarm"' in parts["M-off"]
    assert 'class="mc-alarm"' not in run
    assert 'data-tone="bad"' in frag and 'data-tone="run"' in frag


def test_furnace_heat_only_over_a_running_furnace():
    assert sb.furnace_heat("idle", 1400) is None
    assert sb.furnace_heat("bad", None) is None
    assert sb.furnace_heat("run", 1500) == 1.0
    assert sb.furnace_heat("run", 60) == 0.25
    assert 0.25 < sb.furnace_heat("run", 900) < 1.0
    assert sb.furnace_heat("run", None) == 0.35, "не прочитали — тихий жар, не повний"


def _reading(minute, temp):
    from datetime import timedelta

    return SimpleNamespace(captured_at=datetime(2026, 10, 11, 18, 0) + timedelta(minutes=minute),
                           temp_c=temp)


def test_curve_keeps_the_gap_and_shows_a_head_only_when_fresh():
    from datetime import timedelta

    start = datetime(2026, 10, 11, 18, 0)
    end = start + timedelta(hours=sb.TV_CURVE_HOURS)
    # Розігрів, 40 хв тиші (дірка), полиця — і остання точка хвилину тому.
    readings = [_reading(m, 400 + m * 10) for m in range(0, 60)]
    readings += [_reading(m, 1450) for m in range(100, 359)]
    curve = sb.curve_from_readings(readings, started_at=start, ended_at=end)
    assert curve is not None and len(curve.lines) == 2, "розрив лишається розривом"
    assert any(abs(g.x - curve.width * 59 / 360) < 2 for g in curve.gaps)
    assert curve.head_x is not None and curve.head_x > 99
    assert 0 <= curve.head_y < 10, "1450 °C — під самим верхом шкали"

    stale = sb.curve_from_readings(readings[:60], started_at=start, ended_at=end)
    assert stale is not None and stale.head_x is None, "стара точка не вдає «зараз»"
    assert sb.curve_from_readings([_reading(1, None)], started_at=start, ended_at=end) is None


def test_furnace_curve_reads_the_database(app_db):  # noqa: F811
    """Справжній запит до показань: крива — з бази, за ключем печі, і чужа піч
    у неї не потрапляє."""
    from datetime import timedelta

    from app.models import FurnaceReading

    _, factory = app_db
    now = datetime(2026, 10, 11, 23, 0)
    with factory() as db:
        for minute in range(0, 120, 2):
            at = now - timedelta(minutes=minute)
            db.add(FurnaceReading(host="10.0.0.5", captured_at=at, status="RUN", temp_c=1400))
            db.add(FurnaceReading(host="10.0.0.6", captured_at=at, status="RUN", temp_c=200))
        db.add(FurnaceReading(host="10.0.0.5", captured_at=now - timedelta(hours=9),
                              status="RUN", temp_c=50))
        db.commit()
        curve = sb.furnace_curve(db, "10.0.0.5", now=now)
    assert curve is not None and curve.head_x is not None
    ys = [float(p.split(",")[1]) for line in curve.lines for p in line.split()]
    assert ys and max(ys) == min(ys), "лише 1400 °C цієї печі; 200 °C сусідньої й старі 50 °C — поза кривою"


def test_shop_view_passes_heat_and_curve_only_for_running_furnaces():
    curve = sb.TvCurve(lines=["0,10 360,10"], dots=[], gaps=[], width=360, height=110,
                       head_x=100.0, head_y=9.0)
    furnace = SimpleNamespace(
        cards=[
            SimpleNamespace(name="Бочка", kind="run", temp=1452, open_at="01:38", open_day="завтра",
                            done_iso="2026-10-12T01:38:00", remaining="", note=""),
            SimpleNamespace(name="Піч 3", kind="idle", temp=40, open_at="", open_day="",
                            done_iso="", remaining="", note=""),
        ],
        nearest_name="Бочка", nearest_at="01:38", nearest_day="завтра",
        nearest_iso="2026-10-12T01:38:00")
    view = sb.shop_view(None, token="t", now=NOW, cards=[], furnace_view=furnace,
                        curves={"Бочка": curve, "Піч 3": curve})
    run, idle = view.furnaces
    assert run.curve is curve and run.heat == 0.97 and run.done_iso
    assert idle.curve is None and idle.heat is None


def test_board_card_carries_the_furnace_key():
    from app.services import furnace_board as fb

    card = SimpleNamespace(
        key="10.0.0.5", target=SimpleNamespace(name="Бочка"),
        state=SimpleNamespace(temp_c=1452, done_at=None, remaining_text="", captured_at=None),
        is_running=True, is_idle=False, has_problem=False, has_data=True)
    assert fb.board_view(None, NOW, cards=[card]).cards[0].key == "10.0.0.5"


# ── Назва програми поруч із диском (власник 12.10.26, макет B) ─────────────

# Назви, що справді стояли на верстатах цеху 11–12.10.26 (kmill_devices), +
# нове покоління, де назва — лише дата й час.
PROD_ISO_NAMES = [
    ("2_18-Emotions-a1-x193_2026-10-12_09-09-40.iso",
     "2_18-Emotions-a1-x193_2026-10-12_", "09-09-40"),
    ("1--20-Monolith-a3-5-x30_2026-10-11_14-34-50.iso",
     "1--20-Monolith-a3-5-x30_2026-10-11_", "14-34-50"),
    ("26--pmma16_16-a1-x90_2026-10-11_15-37-17.iso",
     "26--pmma16_16-a1-x90_2026-10-11_", "15-37-17"),
    ("3--ti212-5_2026-10-11_14-55-04.iso",
     "3--ti212-5_2026-10-11_", "14-55-04"),
    ("2026-10-12_09-34-59.iso", "2026-10-12_", "09-34-59"),
]


def test_program_line_strips_iso_and_splits_the_sum3d_tail():
    for raw, head, tail in PROD_ISO_NAMES:
        assert sb.program_line(raw) == (head, tail), raw
    # Розширення — у будь-якому регістрі, зайві пробіли по краях не заважають.
    assert sb.program_line("  2026-10-12_09-34-59.ISO \n") == ("2026-10-12_", "09-34-59")


def test_program_line_without_name_gives_nothing():
    for empty in (None, "", "   ", ".iso", " .ISO ", 123, b"x_09-09-40.iso"):
        assert sb.program_line(empty) == ("", ""), repr(empty)


def test_program_line_without_a_time_tail_keeps_the_whole_name():
    # Хвоста-часу немає — це не привід вигадувати межу.
    assert sb.program_line("custom-program.iso") == ("custom-program", "")
    assert sb.program_line("crown_set_3.iso") == ("crown_set_3", "")
    # Дата без часу: `26-10-12` — це кінець РОКУ, а не час. Різати рік не можна.
    assert sb.program_line("job_2026-10-12.iso") == ("job_2026-10-12", "")
    # Час не в кінці назви — теж не хвіст.
    assert sb.program_line("job_09-09-40_final.iso") == ("job_09-09-40_final", "")


def test_machine_card_takes_the_program_name_from_the_card():
    named = _view([
        _card("M-a", "run", percent=10, sum3d="09-09-40",
              iso_name="2_18-Emotions-a1-x193_2026-10-12_09-09-40.iso"),
    ]).machines[0]
    assert named.has_prog
    assert (named.prog_head, named.prog_tail) == ("2_18-Emotions-a1-x193_2026-10-12_", "09-09-40")
    assert named.sum3d == "09-09-40", "Sum3D лишається в даних — це запасний рядок"

    plain = _view([_card("M-b", "run", percent=10, sum3d="09-09-40")]).machines[0]
    assert not plain.has_prog and plain.sum3d == "09-09-40"

    # Картка-підробка без самого поля (як SimpleNamespace у інших тестах)
    # не падає: `iso_name` читається через getattr.
    bare = _card("M-c", "run", percent=10, sum3d="09-09-40")
    del bare.iso_name
    assert not _view([bare]).machines[0].has_prog


def test_finished_machine_keeps_a_program_name_only_while_the_screen_has_it():
    # `done`: екран ще тримає назву — вона називає диск, який треба зняти.
    held = _view([_card("M-d1", "done", percent=100,
                        iso_name="3--ti212-5_2026-10-11_14-55-04.iso",
                        last_orders=[_order("Дельта", "титан", "5")])]).machines[0]
    assert held.works_are_last and held.has_prog and held.prog_tail == "14-55-04"
    # Назву вже стерто (`MachineCard.iso_name` = None) — звичайний рядок, а
    # якщо немає й Sum3D, то порожньо: стара назва не показується.
    gone = _view([_card("M-d2", "done", percent=100,
                        last_orders=[_order("Дельта", "титан", "5")])]).machines[0]
    assert gone.works_are_last and not gone.has_prog and gone.sum3d == ""


def _card_html(frag, key):
    """Розмітка однієї картки: від її відкривального тега до наступної."""
    at = frag.index(f'data-key="{key}"')
    start = frag.rindex('<div class="mc ', 0, at)
    end = frag.find('<div class="mc ', at)
    return frag[start:end if end != -1 else len(frag)]


def test_board_shows_the_program_instead_of_the_sum3d_digits(app_db, monkeypatch):  # noqa: F811
    """Справжній рендер: з назвою програми рядок біля диска — «програма» + вся
    назва без `.iso` і Sum3D-хвіст у `<b>`; без назви — як було («sum3d» +
    шість цифр). Клієнт і матеріал стоять в обох варіантах (лабораторна й
    поштова робота однаково), а клас `has-cat` — лише на 100 % не-«зняти».
    Назва — дані з екрана верстата, тож екранується, а не вставляється як є."""
    from app.routers.furnace_board import create_board_app
    from app.services import furnace_board as fb
    from app.services import machines as machines_mod
    from tests.test_furnace_board import _enable

    _, factory = app_db
    token = _enable(factory)
    monkeypatch.setattr("app.db.SessionLocal", factory)
    lab = SimpleNamespace(id=1, client_name=None, material_color="mono a3", quantity="4",
                          kind="анатомія", work_order_no="24122", technician_name="Іван")
    monkeypatch.setattr(machines_mod, "snapshot", lambda db: [
        _card("M-prog", "run", percent=74, sum3d="09-09-40",
              orders=[_order("Новодент", "emo a1", "2")],
              iso_name="2_18-Emotions-a1-x193_2026-10-12_09-09-40.iso"),
        _card("M-lab", "run", percent=20, sum3d="09-34-59", orders=[lab],
              iso_name="2026-10-12_09-34-59.iso"),
        _card("M-plain", "run", percent=43, sum3d="12-01-45",
              orders=[_order("Дента Люкс", "mono A3", "6")]),
        _card("M-cat", "run", percent=100, sum3d="14-55-04",
              orders=[_order("Дельта", "титан", "5")],
              iso_name="3--ti212-5_2026-10-11_14-55-04.iso"),
        _card("M-done", "done", percent=100, sum3d="15-37-17",
              orders=[_order("Смайл", "pmma a1", "90")],
              iso_name="26--pmma16_16-a1-x90_2026-10-11_15-37-17.iso"),
        _card("M-bad", "fault", percent=100, word="помилка", sum3d="10-10-10",
              iso_name="2026-10-12_10-10-10.iso"),
        _card("M-xss", "run", percent=5, iso_name="<img src=x onerror=alert(1)>_09-09-40.iso"),
        _card("M-notail", "run", percent=5, sum3d="10-10-10", iso_name="custom-program.iso"),
    ])
    monkeypatch.setattr(fb, "board_view", lambda db, now=None, cards=None: SimpleNamespace(
        cards=[], nearest_name="", nearest_at="", nearest_day="", nearest_iso=""))

    status, _, frag = MiniClient(create_board_app()).get(f"/t/{token}/shop/cards")
    assert status == 200

    prog = _card_html(frag, "M-prog")
    assert "<em>програма</em>" in prog and "has-prog" in prog
    assert ('<span class="prog"><span class="ph">2_18-Emotions-a1-x193_2026-10-12_</span>'
            '<b>09-09-40</b></span>') in prog
    assert ".iso" not in prog
    assert "<em>sum3d</em>" not in prog
    assert "Новодент" in prog and "emo a1" in prog, "клієнт і матеріал лишились"

    lab_html = _card_html(frag, "M-lab")
    assert '<span class="prog"><span class="ph">2026-10-12_</span><b>09-34-59</b></span>' in lab_html
    assert "24122" in lab_html and "mono a3" in lab_html, "лабораторна робота: наряд і матеріал"

    plain = _card_html(frag, "M-plain")
    assert "<em>sum3d</em>12-01-45" in plain
    assert "програма" not in plain and "has-prog" not in plain and 'class="prog"' not in plain
    assert "Дента Люкс" in plain and "mono A3" in plain

    # Котик: has-cat рівно там, де є сам котик (100 %, не «зняти», не помилка).
    for key, expect in (("M-cat", True), ("M-prog", False), ("M-plain", False),
                        ("M-done", False), ("M-bad", False)):
        html = _card_html(frag, key)
        opening = html.split(">", 1)[0]
        assert ("has-cat" in opening) is expect, key
        assert ('class="mc-cat"' in html) is expect, key
    # На картці з котиком програма теж стоїть, хвіст у `<b>`.
    assert "<b>14-55-04</b>" in _card_html(frag, "M-cat")

    # Назва з екрана екранується; `<b>` лише той, що ставить шаблон.
    xss = _card_html(frag, "M-xss")
    assert "<img src=x" not in xss
    assert '<span class="ph">&lt;img src=x onerror=alert(1)&gt;_</span><b>09-09-40</b>' in xss

    # Назва без Sum3D-хвоста: показується вся, без `<b>`, і ще не «sum3d».
    notail = _card_html(frag, "M-notail")
    assert '<span class="prog no-tail"><span class="ph">custom-program</span></span>' in notail
    id_row = notail.split('class="mc-id', 1)[1].split('class="mc-bot"', 1)[0]
    assert "<b>" not in id_row and "<em>sum3d</em>" not in id_row
