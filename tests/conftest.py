"""Спільні фікстури тестів — одна база в памʼяті замість 48 копій.

**Навіщо.** Половина тестових файлів дослівно повторює той самий блок:

    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)

Копія сама по собі не болить, поки не треба змінити ОДНУ спільну деталь
(прапорець зʼєднання, PRAGMA, спосіб реєстрації моделей) — тоді правку доводиться
рознести по 48 файлах, і один-два неминуче лишаться зі старою поведінкою. Саме так
розходяться «однакові» тести: тут працює, там ні.

**Міграція свідомо поступова.** Переписати всі 48 файлів одним махом — велика
непроглядна правка на код, який і є нашою страховкою; помилка в такій правці
робить тест зеленим і порожнім. Тому тут лише фікстури, а на них переведено
кілька найпростіших файлів як зразок. Решта живе зі своїм локальним `_db()` і
працює далі — фікстури нічого не нав'язують.

**Як перевести наступний файл** (по одному, з прогоном після кожного):

1. Прибрати локальний `_db()` / `_database()` і імпорти `create_engine`,
   `StaticPool`, `Base` (а `Session` — лише якщо більше ніде не потрібен).
2. Додати `db_session` (готова сесія) або `db_engine` (коли тест сам відкриває
   кілька сесій на ту саму базу) в аргументи тест-функції.
3. Прогнати саме цей файл. Якщо тест ловив `expire_on_commit` за замовчуванням
   (тобто СПОДІВАВСЯ, що обʼєкт після `commit()` протухне) — не переводити:
   фікстура дає `expire_on_commit=False`, як переважна більшість наявних копій.

Файл із кількома НЕЗАЛЕЖНИМИ базами в одному тесті переводити не варто — фікстура
дає рівно одну; для таких випадків є `make_memory_engine()`.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
import app.models  # noqa: F401 — імпорт реєструє ВСІ таблиці в Base.metadata


def make_memory_engine(check_same_thread: bool = False):
    """Порожня база в памʼяті зі створеною схемою.

    `StaticPool` обовʼязковий: без нього кожне нове зʼєднання до `sqlite://`
    отримує ВЛАСНУ порожню базу, і друга сесія не бачить записаного першою.

    `check_same_thread=False` типово ввімкнено, бо частина тестів ганяє синк і
    фонові воркери в окремих потоках (`test_handout_routes`,
    `test_sheet_write_safety`), а sqlite інакше кидає «SQLite objects created in
    a thread…». Для однопотокового тесту прапорець нешкідливий — він лише знімає
    перевірку драйвера, а не додає паралелізму.
    """
    engine = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": check_same_thread},
    )
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def db_engine():
    """Движок на порожню базу — коли тест сам вирішує, скільки сесій відкрити."""
    engine = make_memory_engine()
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def db_session(db_engine) -> Session:
    """Готова сесія до порожньої бази — типовий випадок.

    `expire_on_commit=False` повторює те, що вручну прописано в більшості
    наявних копій: інакше після кожного `commit()` звернення до поля робить
    зайвий SELECT, а обʼєкт, узятий до коміту, стає непридатним.
    """
    with Session(db_engine, expire_on_commit=False) as session:
        yield session


@pytest.fixture(autouse=True)
def _fresh_sheets_quota_counter():
    """Лічильник запитів до Sheets живе на ПРОЦЕС, а тести ганяють синк сотні
    разів. Без скидання один файл «витрачає квоту» наступному, і гальмо
    гарячої смуги (app/sheets.quota_is_tight) спрацьовує в тестах, які про
    нього нічого не знають — падіння виглядає як плаваюче й залежне від
    порядку. Скидаємо перед КОЖНИМ тестом: у бою лічильник так само починає
    з нуля при старті процесу.
    """
    from app.sheets import reset_api_call_counter

    reset_api_call_counter()
    yield
    reset_api_call_counter()


@pytest.fixture(autouse=True)
def _fresh_sheet_erase_guard():
    """Стеля стирань рядків теж живе на ПРОЦЕС (app/sheet_erase_guard).
    Без скидання файл, який ганяє стирання, вичерпує вікно наступному, і той
    падає з «спрацював запобіжник» без жодного стосунку до свого предмета.
    """
    from app import sheet_erase_guard

    sheet_erase_guard.reset_for_tests()
    yield
    sheet_erase_guard.reset_for_tests()


@pytest.fixture(autouse=True)
def _fresh_absent_tab_streaks():
    """Лічильник «вкладки немає в листингу N читань поспіль» — теж на ПРОЦЕС
    (app/sheet_sync_service). Без скидання один тест «набиває» три читання
    наступному, і той архівує вкладку з першого тіку.
    """
    from app.sheet_sync_service import _reset_absent_streaks_for_tests

    _reset_absent_streaks_for_tests()
    yield
    _reset_absent_streaks_for_tests()


@pytest.fixture(autouse=True)
def _fresh_log_throttle():
    """Глушник повторів у лозі (app/log_throttle) теж на ПРОЦЕС: без скидання
    тест, який перевіряє попередження, не побачить його через те, що інший
    файл уже «витратив» годинне вікно.
    """
    from app import log_throttle

    log_throttle.reset_for_tests()
    yield
    log_throttle.reset_for_tests()


@pytest.fixture
def weekday_clock(monkeypatch):
    """Пін годинника на БУДНІЙ день — для тестів, що самі будують назву вкладки.

    Цех працює в суботу й неділю, а вкладок за вихідні в таблиці немає: роботи
    цих днів пишуть у вкладку п'ятниці (CLAUDE.md §4). Тому в застосунку
    розійшлись дві речі: `business_today()` — справжня робоча ДАТА (годинник,
    retention), а `business_tab_today()` — вкладка, у яку цех пише зараз.

    Тест, який будує назву вкладки як `business_today().strftime("%d.%m.%y")`,
    у будень має рацію (дата й вкладка збігаються), а в суботу створює роботу
    у вкладці «12.09.26», якої в житті не існує, — і сам же питає про неї чергу,
    що дивиться на п'ятницю. 12.09.26 так падало 23 тести в шести файлах при
    ПРАВИЛЬНІЙ поведінці застосунку, а ворота CI гоняють `pytest` — тобто дві
    доби на тиждень реліз був заблокований тестами, не кодом.

    Фікстура не чіпає `app/` взагалі: вона лише ставить годинник на будній день,
    у якому припущення тесту знову правдиве. Зсув мінімальний (субота → п'ятниця,
    неділя → п'ятниця), час доби зберігається — межа робочого дня 07:30 лишається
    такою ж, як у справжньому прогоні.
    """
    from app import business_day
    from timepin import weekday_moment

    pinned = weekday_moment()
    monkeypatch.setattr(business_day, "business_now", lambda: pinned)
    return pinned


def run_route(result):
    """Викликати роут, не знаючи, синхронний він чи асинхронний.

    Тести кличуть обробники напряму, а не через HTTP-клієнт (httpx у venv
    немає). Раніше вони робили `asyncio.run(route(...))` — і це прив'язувало
    ТЕСТ до того, чи оголошений роут як `async def`. Коли аудит 08.09.26 зняв
    `async` з чотирьох роутів видачі (вони блокували event loop, бо не мали
    жодного `await`, зате ходили по мережевій шарі), одинадцять тестів
    попадали з «a coroutine was expected» — при повністю правильній правці.

    Обгортка прибирає цю крихкість: `run_route(route(...))` працює однаково для
    обох видів, і майбутня зміна асинхронності роута більше не тягне за собою
    правку тестів.
    """
    import asyncio
    import inspect

    if inspect.iscoroutine(result):
        return asyncio.run(result)
    return result
