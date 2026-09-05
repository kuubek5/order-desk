"""Звідки взялось «видано» — і чому це має значення.

Бойовий випадок 01.09.26: адміністратор видалив кілька рядків між блоком
лабораторії та блоком файлів. Дані з'їхали вгору, заливка лишилась на старих
клітинках, і синк прочитав два клієнтські рядки як «немає заливки» = видано.
Роботи зникли з ранкової видачі, і дізнатись про це можна було хіба від
клієнта.

Ці тести стережуть три речі, кожна з яких окремо не рятує:
* «видано» з таблиці ПОМІЧАЄТЬСЯ як здогад (`issued_source="sheet"`);
* рішення оператора (`issue_locked`) б'є заливку НАЗАВЖДИ — інакше наступний
  синк побачив би ту саму порожню клітинку й позначив роботу виданою знову;
* видана робота ЛИШАЄТЬСЯ у вибірці видачі — саме її зникнення робило збій
  невидимим.
"""

from datetime import date

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db import Base
from app.models import Order
from app.parser import OrderRow
from app.services.handout import handout_eligible_orders
from app.sync import sync_tab


def make_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


def client_row(**overrides):
    values = {
        "row_number": 5, "seq_no": "60", "work_order_no": "", "quantity": "1",
        "material_color": "mono a3", "kind": "Кривовид", "due_time": None,
        "job_code": "", "technician_name": "", "cam_comment": "",
        "sum3d_id": "", "calculated": "", "milled": "", "last_milled_date": "",
        "mill_count": "",
    }
    values.update(overrides)
    return OrderRow(**values)


def test_blue_fill_keeps_client_pending():
    with make_session() as session:
        sync_tab(session, "01.09.26", [client_row()], row_fills={5: "blue"})
        order = session.scalars(select(Order)).one()
        assert order.status == "нове"
        assert order.issued_source is None


def test_cleared_fill_is_a_NORMAL_issue_not_a_doubt():
    """Зняте синє — ШТАТНИЙ спосіб відмітити видачу (§2), а не підозра.

    Спершу я помічав такі роботи знаком питання. Це була помилка: сумнів висів
    би на КОЖНОМУ правильно виданому клієнті щодня, оператор перестав би його
    читати, і справжня аварія загубилась би серед шуму (зауваження власника
    05.09.26). Підозрілим зняте синє робить лише зсув рядків — див. наступний
    тест."""
    with make_session() as session:
        sync_tab(session, "01.09.26", [client_row()], row_fills={5: ""})
        order = session.scalars(select(Order)).one()
        assert order.status == "видано"
        assert order.issued_source == "sheet"   # звичайна видача, без сумніву


def test_operator_decision_survives_the_next_sync():
    """Головний тест. Оператор зняв хибне «видано»; у таблиці заливки як не
    було, так і немає. Без замка синк повернув би «видано» — і людина знімала б
    галочку щоранку."""
    with make_session() as session:
        sync_tab(session, "01.09.26", [client_row()], row_fills={5: ""})
        order = session.scalars(select(Order)).one()
        assert order.status == "видано"

        # Оператор: «ми цього не видавали».
        order.status = "відфрезеровано"
        order.issue_locked = True
        order.issued_source = None
        session.commit()

        sync_tab(session, "01.09.26", [client_row()], row_fills={5: ""})
        order = session.scalars(select(Order)).one()
        assert order.status == "відфрезеровано"
        assert order.issued_source is None


def test_issued_client_stays_on_the_handout_screen():
    """Раніше вибірка мала `status != "видано"`, і саме тому збій був
    невидимий: робота просто зникала з екрана."""
    with make_session() as session:
        sync_tab(session, "01.09.26", [client_row()], row_fills={5: ""})
        eligible = handout_eligible_orders(session, date(2026, 9, 30))
        assert [o.client_name for o in eligible] == ["Кривовид"]
        assert eligible[0].status == "видано"


def test_grey_fill_is_not_issued():
    """Сірий — власна позначка лабораторії, не «видано». Регресія вже була."""
    with make_session() as session:
        sync_tab(session, "01.09.26", [client_row()], row_fills={5: "grey"})
        order = session.scalars(select(Order)).one()
        assert order.status == "нове"
        assert order.issued_source is None


def test_restoring_the_blue_fill_withdraws_the_sheet_guess():
    """Зворотний напрямок. Логіст помилково зняв заливку — робота стала
    «видано»; повернув заливку — «видано» має зникнути.

    Без цього поломку було видно, а її виправлення — ні: захист статусів
    («видано» не затирається таблицею) блокував відкат, і робота лишалась
    виданою назавжди, хоча в таблиці все вже було правильно."""
    with make_session() as session:
        sync_tab(session, "01.09.26", [client_row()], row_fills={5: ""})
        assert session.scalars(select(Order)).one().status == "видано"

        sync_tab(session, "01.09.26", [client_row()], row_fills={5: "blue"})
        order = session.scalars(select(Order)).one()
        assert order.status == "нове"
        assert order.issued_source is None


def test_sheet_cannot_undo_an_issue_made_by_the_operator():
    """Межа правила: таблиця забирає лише те, що сама ж стверджувала.

    Видачу, яку провів оператор через CRM, повернена заливка скасувати НЕ може —
    інакше будь-яка правка кольору в таблиці відкочувала б реальну видачу."""
    with make_session() as session:
        sync_tab(session, "01.09.26", [client_row()], row_fills={5: "blue"})
        order = session.scalars(select(Order)).one()
        order.status = "видано"
        order.issued_source = "portal"      # натиснули «Видати» в CRM
        session.commit()

        sync_tab(session, "01.09.26", [client_row()], row_fills={5: "blue"})
        order = session.scalars(select(Order)).one()
        assert order.status == "видано"
        assert order.issued_source == "portal"


def test_doubt_is_raised_only_when_rows_shifted_in_the_same_pass():
    """Сумнів («shifted») ставиться ЛИШЕ тоді, коли в тому ж читанні таблиці
    рядки з'їхали — саме тоді колір міг опинитись навпроти чужої роботи.

    Відтворюємо 01.09.26: робота жила в рядку 5, потім хтось видалив рядок
    вище, і та сама робота приїхала в рядок 4 — вже без заливки."""
    with make_session() as session:
        sync_tab(session, "01.09.26", [client_row(row_number=5)], row_fills={5: "blue"})
        assert session.scalars(select(Order)).one().status == "нове"

        # Той самий клієнт і матеріал, але рядком вище + порожня заливка.
        sync_tab(session, "01.09.26", [client_row(row_number=4)], row_fills={4: ""})
        order = session.scalars(select(Order)).one()
        assert order.status == "видано"
        assert order.issued_source == "shifted"


def test_lock_releases_once_the_sheet_shows_blue_again():
    """Повний робочий цикл власника (05.09.26):
      1. таблиця без синього → CRM ставить «видано»;
      2. оператор зняв галочку в CRM → замок + рядок перефарбовано синім;
      3. синк бачить синє → таблиця наздогнала, ЗАМОК ЗНІМАЄТЬСЯ;
      4. знову зняли синє в таблиці → CRM МУСИТЬ показати «видано».
    Довічний замок ламав крок 4: після зняття галочки в CRM таблиця вже
    ніколи не могла позначити цю роботу виданою."""
    with make_session() as session:
        sync_tab(session, "01.09.26", [client_row()], row_fills={5: ""})
        order = session.scalars(select(Order)).one()
        assert order.status == "видано"

        # крок 2: зняли галочку в CRM (те, що робить /unissue)
        order.status = "відфрезеровано"
        order.issue_locked = True
        order.issued_source = None
        session.commit()

        # крок 3: у таблиці вже синє → замок відпускає, статус не рухається
        sync_tab(session, "01.09.26", [client_row()], row_fills={5: "blue"})
        order = session.scalars(select(Order)).one()
        assert order.issue_locked is False
        assert order.status == "відфрезеровано"

        # крок 4: знову зняли синє → «видано» знову працює
        sync_tab(session, "01.09.26", [client_row()], row_fills={5: ""})
        order = session.scalars(select(Order)).one()
        assert order.status == "видано"
        assert order.issued_source == "sheet"


def test_lock_holds_while_the_fill_is_still_cleared():
    """Поки таблиця ще НЕ наздогнала (перефарбування не доїхало, або синк був на
    паузі), замок тримає — інакше «видано» поверталось би щоп'ятнадцять секунд."""
    with make_session() as session:
        sync_tab(session, "01.09.26", [client_row()], row_fills={5: ""})
        order = session.scalars(select(Order)).one()
        order.status = "відфрезеровано"
        order.issue_locked = True
        order.issued_source = None
        session.commit()

        sync_tab(session, "01.09.26", [client_row()], row_fills={5: ""})
        order = session.scalars(select(Order)).one()
        assert order.status == "відфрезеровано"
        assert order.issue_locked is True
