"""Облікові записи операторів: перший адмін і літера в таблиці.

Літера («Р», «К», «СТ») — це те, що потрапляє в колонку «Прорахував», коли
оператор вписує Sum3D. Тому вона мусить бути унікальною: за нею в таблиці
впізнають, ХТО прорахував роботу, а спільна літера зробила б це неоднозначним.

Правила живуть тут, бо їх застосовують два різні екрани — кабінет оператора
(сам собі) і налаштування (адмін комусь), — і розійтись вони не мають права.
"""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import User


def user_count(db: Session) -> int:
    """Return the number of configured accounts without loading user records."""
    return db.scalar(select(func.count()).select_from(User)) or 0


def validate_first_admin(
    username: str,
    full_name: str,
    password: str,
    password_confirmation: str,
) -> tuple[dict[str, str] | None, str | None]:
    """Normalize and validate the one-time first administrator form."""
    values = {
        "username": username.strip(),
        "full_name": full_name.strip(),
        "password": password,
    }
    if not values["username"] or not values["full_name"]:
        return None, "Вкажіть логін та ім’я адміністратора"
    if len(password) < 10:
        return None, "Пароль має містити щонайменше 10 символів"
    if password != password_confirmation:
        return None, "Паролі не збігаються"
    return values, None


def normalize_initial(raw: str) -> str | None:
    """A sheet initial normalized: trimmed, upper-cased (Р/К/СТ), or None if
    blank. Length is validated separately by validate_initial."""
    cleaned = (raw or "").strip().upper()
    return cleaned or None


def validate_initial(db: Session, initial: str, *, exclude_user_id: int | None) -> str | None:
    """Return a Ukrainian error message if the initial is invalid, else None.
    Rules: 1-2 letters, unique across operators (letters identify who
    calculated, so a shared letter would be ambiguous)."""
    if not (1 <= len(initial) <= 2) or not initial.isalpha():
        return "літера оператора — 1-2 букви"
    query = select(User).where(User.sheet_initial == initial)
    if exclude_user_id is not None:
        query = query.where(User.id != exclude_user_id)
    clash = db.scalar(query)
    if clash is not None:
        return f"літеру «{initial}» вже має {clash.full_name or clash.username}"
    return None
