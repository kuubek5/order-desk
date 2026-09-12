"""Доступ до `/mcp` по мережі — перемикач і токен на екрані налаштувань.

Сам слухач і його стан живуть у `app/services/mcp_gateway.py` (окремий
сторож піднімає/гасить порт 8011 за цим перемикачем — дивись докстрінг
модуля). Тут лише дві дії: увімкнути/вимкнути й перевипустити токен — той
самий посадковий прийом, що в табло печей (`toggle_furnace_board` /
`regenerate_furnace_board_link`, `app/routers/settings/feedback.py`): адмін +
loopback (ця дія керує МАШИНОЮ — відкриває порт назовні), редирект на
`/settings#mcp` із флеш-повідомленням.

Токен сам по собі екран не показує — показує ГОТОВИЙ рядок «адреса + токен»
(`mcp_gateway.connect_links`), який лишається скопіювати й передати. Перший
варіант показував токен рівно один раз і окремо від адреси; на практиці це
означало «запиши зараз, бо більше не побачиш», а власник мусив зліпити рядок
сам. Чому видимий щоразу — у докстрінзі `connect_links`: екран і так лише для
адміна й лише з цього компʼютера, рівно як посилання табло печей.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.routers.deps import get_db
from app.settings_store import set_setting
from .common import require_settings_admin

router = APIRouter()


def _flash(request: Request, kind: str, message: str) -> RedirectResponse:
    request.session["settings_flash"] = {"kind": kind, "message": message}
    return RedirectResponse("/settings#mcp", status_code=303)


@router.post("/settings/mcp/toggle")
def toggle_mcp_gateway(request: Request, db: Session = Depends(get_db)):
    """Увімкнути/вимкнути слухача `/mcp`. Вимкнений не відкриває порт
    узагалі — сторож (той самий прийом, що в табло печей) закриває його за
    кілька секунд. Увімкнення без токена створює його одразу — інакше
    перемикач стоятиме «увімкнено», а зайти ніхто не зможе."""
    require_settings_admin(request, db)
    from app.services import mcp_gateway

    on = not mcp_gateway.gateway_enabled(db)
    if on and not mcp_gateway.has_token(db):
        mcp_gateway.regenerate_token(db)
    set_setting(db, mcp_gateway.ENABLED_KEY, "1" if on else "")
    db.commit()
    if on:
        message = (
            f"Доступ вмикається на порту {mcp_gateway.GATEWAY_PORT}. Рядок для передачі — нижче; "
            "якщо запит ззовні не доходить, лишилась команда брандмауера (вона там же)."
        )
        return _flash(request, "success", message)
    return _flash(request, "success", "Доступ вимкнено — порт закрито повністю, жоден запит ззовні не пройде.")


@router.post("/settings/mcp/token")
def regenerate_mcp_token(request: Request, db: Session = Depends(get_db)):
    """Новий токен; старий одразу перестає працювати — той самий контракт,
    що в «Змінити посилання» табло печей."""
    require_settings_admin(request, db)
    from app.services import mcp_gateway

    mcp_gateway.regenerate_token(db)
    db.commit()
    return _flash(
        request, "success", "Новий рядок готовий — старий більше не працює, передай новий."
    )
