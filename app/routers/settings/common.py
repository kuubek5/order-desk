"""Спільне для всіх тематичних модулів налаштувань: гейт «це адмін».

Живе окремо, щоб модулі не імпортували один одного по колу.
"""

from sqlalchemy.orm import Session
from starlette.requests import Request
from app.routers.deps import require_admin


def require_settings_admin(request: Request, db: Session):
    """Admin + loopback gate shared by the settings mutation routes.

    Сама перевірка живе в `deps.require_admin` — одна на застосунок (аудит
    05.09.26, крок 2.7). Ця назва лишається, бо на неї спираються ~30 роутів
    і тести; вона тепер лише каже, ЯКИЙ саме варіант гейта тут потрібен.
    """
    return require_admin(request, db, loopback=True)
