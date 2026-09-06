"""F15 (аудит 06.09.26): жоден роут-обробник не блокує event loop дарма.

CLAUDE.md §14 — «Запис у таблицю» / загальне правило: синхронна робота з
мережею, файлами чи БД не має йти на event loop. `async def` без жодного
`await` усередині — саме такий випадок: FastAPI НЕ віддає такий роут
threadpool'у (як віддав би звичайний `def`), тож весь синхронний код у
ньому (тут — файлові операції спулу/export, робота з БД) виконується прямо
на loop і блокує решту застосунку на час запиту.

Чотири роути були `async def` без жодного `await` у тілі: два з них
(`accept_email`, `restore_email`) до того ж рухають файли між спулом і
export на мережевій шарі (UNC-шлях лаби) — саме той дорогий I/O, заради
якого §14 і існує. П'ятий кандидат (`post_settings`) лишається `async def`
навмисно, бо йому потрібен `await request.form()` — його `sync_google_sheets`
винесено в `run_in_threadpool` окремо (перевірено в
`tests/test_sync_pause_gate.py`), тут він не сторожиться.
"""

import inspect

from app.routers.mail import accept_email, reject_email, restore_email
from app.routers.settings.devices import upload_machine_portrait


def test_mail_and_portrait_handlers_are_plain_sync_functions():
    handlers = {
        "app.routers.mail.accept_email": accept_email,
        "app.routers.mail.reject_email": reject_email,
        "app.routers.mail.restore_email": restore_email,
        "app.routers.settings.devices.upload_machine_portrait": upload_machine_portrait,
    }
    still_async = {
        name: fn for name, fn in handlers.items() if inspect.iscoroutinefunction(fn)
    }
    assert not still_async, (
        f"ці роути лишились async def без await — блокують event loop: "
        f"{sorted(still_async)}"
    )
