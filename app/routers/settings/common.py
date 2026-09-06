"""Спільне для всіх тематичних модулів налаштувань: гейт «це адмін».

Живе окремо, щоб модулі не імпортували один одного по колу.
"""

from fastapi import HTTPException
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.routers.deps import get_current_user, is_loopback_request, require_admin
from app.services.settings_nav import can_edit


def require_settings_admin(request: Request, db: Session):
    """Admin + loopback gate shared by the settings mutation routes.

    Сама перевірка живе в `deps.require_admin` — одна на застосунок (аудит
    05.09.26, крок 2.7). Ця назва лишається, бо на неї спираються ~30 роутів
    і тести; вона тепер лише каже, ЯКИЙ саме варіант гейта тут потрібен.
    """
    return require_admin(request, db, loopback=True)


def require_settings_edit(request: Request, db: Session, key: str, *, loopback: bool = True):
    """Гейт «цей користувач може РЕДАГУВАТИ розділ `key`».

    Права беруться з реєстру меню (`app/services/settings_nav.py`), тому
    відповідь роута й вигляд екрана не можуть розійтися: кнопку малює
    `can_edit`, і POST перевіряє той самий `can_edit`.

    Рішення власника 06.09.26: оператор редагує «Джерела робіт» і
    «Обладнання» нарівні з адміном (включно з секретами пошти й Google) —
    у цеху за верстатом стоїть він, і чекати адміна, щоб змінити адресу печі
    чи пароль скриньки, немає сенсу. Решта розділів (люди, бекап, ліцензія,
    оновлення) лишається адмінською.

    `loopback` зберігає стару поведінку `require_settings_admin`: дія, що
    керує самою машиною або її секретами, доступна лише за фізичним ПК.
    """
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")
    if not can_edit(user, key):
        raise HTTPException(status_code=403, detail="розділ доступний лише адміністратору")
    if loopback and not is_loopback_request(request):
        raise HTTPException(status_code=403, detail="дія доступна лише на цьому комп'ютері")
    return user
