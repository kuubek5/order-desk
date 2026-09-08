"""Жоден роут не блокує event loop дарма — сторож на ВСІ роути застосунку.

CLAUDE.md §14: синхронна робота з мережею, файлами чи БД не має йти на event
loop. `async def` без жодного `await` усередині — саме такий випадок: FastAPI
НЕ віддає такий роут threadpool'у (як віддав би звичайний `def`), тож увесь
синхронний код у ньому виконується прямо на циклі й блокує застосунок ДЛЯ ВСІХ
на час запиту.

Чому список замінено на суцільний обхід. Раніше тут стояли чотири роути,
виписані іменами (пошта + портрет верстата, аудит 06.09.26). Сторож працював —
і саме тому створював хибне відчуття, що правило під наглядом. Аудит 08.09.26
знайшов ЧОТИРИ нові порушення на екрані видачі (`mark_found`, `unmark_found`,
`unissue_order`, `mark_found_group`): жодного `await` у тілі, зате повна
перебудова екрана з послідовними зверненнями до мережевої шари. Кожна галочка
«знайдено» морозила застосунок для обох операторів. Список за іменами такого не
ловить за визначенням — він стереже те, що вже полагодили.

Тому тепер сторож бере роути з самого застосунку і перевіряє кожен. Новий
`async def` без `await` падає ще в CI, а не через півроку в цеху.
"""

import ast
import inspect
import textwrap

import pytest
from fastapi.routing import APIRoute

import app.web as web


def _has_await(fn) -> bool:
    """Чи є в тілі функції хоч одне справжнє очікування.

    `await`, `async for` і `async with` однаково означають «ця функція має
    причину бути асинхронною». Вкладені функції теж рахуються: якщо обробник
    визначає всередині корутину й віддає її кудись, він асинхронний по суті.
    """
    try:
        source = textwrap.dedent(inspect.getsource(fn))
    except (OSError, TypeError):  # pragma: no cover — вбудовані/динамічні
        return True  # не змогли прочитати — не звинувачуємо
    try:
        tree = ast.parse(source)
    except SyntaxError:  # pragma: no cover — декоратор із хитрим відступом
        return True
    return any(
        isinstance(node, (ast.Await, ast.AsyncFor, ast.AsyncWith))
        for node in ast.walk(tree)
    )


# Роути, яким `async def` без `await` дозволений — бо вони не роблять НІЧОГО
# блокуючого. Правило існує проти синхронного I/O на циклі, а не проти
# асинхронності як такої: обробник, що повертає готовий словник, на циклі
# коштує дешевше, ніж стрибок у threadpool.
#
# Наразі порожньо, і це правильний стан. `health` тут був, поки повертав
# готовий словник; у тому ж аудиті 08.09.26 його навчили перевіряти справжній
# стан (SELECT 1 + вік тіків воркерів), і разом із базою в ньому зник `async` —
# рівно як тут і було написано.
NO_BLOCKING_WORK: set[str] = set()


def _async_endpoints():
    seen = {}
    for route in web.app.routes:
        if not isinstance(route, APIRoute):
            continue
        fn = route.endpoint
        if not inspect.iscoroutinefunction(fn):
            continue
        name = f"{fn.__module__}.{fn.__qualname__}"
        seen.setdefault(name, fn)
    return seen


def test_no_async_route_without_await():
    """`async def` дозволений ЛИШЕ там, де є що чекати.

    Якщо цей тест упав на твоєму новому роуті — прибери `async`. FastAPI сам
    виконає звичайний `def` у threadpool, і синхронний код перестане тримати
    цикл. Лишати `async` можна тільки додавши справжній `await`: наприклад
    `await request.form()` або `await await_on_writeback(...)`.
    """
    offenders = sorted(
        name
        for name, fn in _async_endpoints().items()
        if not _has_await(fn) and name not in NO_BLOCKING_WORK
    )
    assert not offenders, (
        "ці роути оголошені async def, але нічого не чекають — вони блокують "
        "event loop для ВСІХ користувачів на час свого синхронного коду: "
        f"{offenders}"
    )


@pytest.mark.parametrize(
    "name",
    [
        "app.routers.handout.mark_found",
        "app.routers.handout.unmark_found",
        "app.routers.handout.unissue_order",
        "app.routers.handout.mark_found_group",
        "app.routers.mail.accept_email",
        "app.routers.mail.reject_email",
        "app.routers.mail.restore_email",
        "app.routers.settings.devices.upload_machine_portrait",
    ],
)
def test_known_offenders_stay_sync(name):
    """Іменний список лишається — але вже не як ЄДИНА перевірка, а як памʼять.

    Кожен із цих роутів колись блокував цикл і був полагоджений. Загальний тест
    вище ловить будь-яке нове порушення; цей каже, ЯКЕ саме повернулось, і не
    дає тихо відкотити конкретну правку.
    """
    module_path, _, attr = name.rpartition(".")
    module = __import__(module_path, fromlist=[attr])
    fn = getattr(module, attr)
    assert not inspect.iscoroutinefunction(fn), (
        f"{name} знову async def — цей роут блокує event loop; "
        "прибери async, FastAPI віддасть його в threadpool"
    )
