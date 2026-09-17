"""Форма «Додати роботу» мусить писати в ТУ вкладку, яку видно на екрані.

Скарга власника 17.09.26: обираю «Вчора», додаю роботу руками — рядок
з'являється в СЬОГОДНІШНІЙ вкладці Google (а в інший раз навпаки — у
вчорашній). Причина не в записі, а в розмітці: смуга фільтрів hx-boost'ить
свої посилання й свапає лише `#queue-rows` плюс кілька OOB-шматків
(`_queue_filter_swap.html`), а форма додавання лежить ПОЗА цим блоком —
навмисно, щоб 15-секундний полл не стирав набране. Тож приховане поле
`target_tab` лишалось зі значенням ПЕРШОГО завантаження сторінки, скільки б
вкладок оператор не перемкнув.

Тому день мусить їхати в тому ж OOB-пакеті, що й лічильники. Сторож тримає
обидві половини: повну сторінку і легкий фрагмент перемикання.
"""

from __future__ import annotations

import re

from tests.asgi_client import MiniClient
from tests.test_settings_slabs_render import OPERATOR, app_db  # noqa: F401 — фікстура

TARGET_RE = re.compile(
    r'<input[^>]*id="addwork-target-tab"[^>]*value="([^"]*)"', re.S
)


def _client(app) -> MiniClient:
    client = MiniClient(app)
    status, _, _ = client.login(*OPERATOR)
    assert status in (200, 302, 303)
    return client


def _target_tab(html: str) -> str:
    found = TARGET_RE.search(html)
    assert found is not None, "поля target_tab у відповіді немає зовсім"
    return found.group(1)


def test_the_full_page_carries_the_day_it_shows(app_db):  # noqa: F811
    app, _factory = app_db
    client = _client(app)

    days = {}
    for period in ("yesterday", "today", "tomorrow"):
        status, _, html = client.get(f"/?period={period}&source=all&ready=all")
        assert status == 200, html[:300]
        days[period] = _target_tab(html)

    assert all(days.values()), f"порожній день у повній сторінці: {days}"
    assert len(set(days.values())) == 3, f"три вкладки мають бути різні: {days}"


def test_switching_the_period_tab_moves_the_day_with_it(app_db):  # noqa: F811
    """Головне: легкий фрагмент перемикання теж несе день.

    Саме його віддає сервер на клік по «Вчора» — і саме його бракувало.
    """
    app, _factory = app_db
    client = _client(app)

    status, _, full = client.get("/?period=today&source=all&ready=all")
    assert status == 200
    today_tab = _target_tab(full)

    status, _, partial = client.get(
        "/?period=yesterday&source=all&ready=all",
        headers={"HX-Request": "true", "HX-Boosted": "true"},
    )
    assert status == 200, partial[:300]
    yesterday_tab = _target_tab(partial)

    assert yesterday_tab != today_tab, (
        "фрагмент перемикання лишив день сьогоднішнім — робота піде не в ту "
        f"вкладку ({yesterday_tab!r})"
    )
    assert 'hx-swap-oob="true"' in partial, "день мусить їхати саме OOB"


def test_earlier_writes_into_todays_tab(app_db):  # noqa: F811
    """«Раніше» охоплює багато днів — одного дня в нього немає, і форма раніше
    їхала з порожнім значенням. Тоді запис падав на давнє правило «найновіша
    вкладка не пізніше сьогодні», і робота могла лягти в чужий день: у вихідні
    або коли сьогоднішньої вкладки ще не створили. Рішення власника 17.09.26 —
    писати в СЬОГОДНІШНЮ вкладку, бо саме її оператор і має на увазі."""
    app, _factory = app_db
    client = _client(app)

    status, _, today_page = client.get("/?period=today&source=all&ready=all")
    assert status == 200
    today_tab = _target_tab(today_page)

    status, _, html = client.get("/?period=earlier&source=all&ready=all")
    assert status == 200
    assert _target_tab(html) == today_tab, "«Раніше» мусить писати в сьогоднішній день"
    assert today_tab, "сьогоднішня вкладка не може бути порожньою"
