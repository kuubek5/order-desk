"""One-off CLI to create an operator account — there's no signup screen,
accounts are provisioned by hand until the admin "Налаштування" screen exists.

Usage: python -m app.create_user_cli <username> <password> [full_name] [role]
"""

import sys

from app.auth import hash_password
from app.db import Base, engine, get_session
from app.models import User


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if len(sys.argv) < 3:
        print("Використання: python -m app.create_user_cli <логін> <пароль> [ім'я] [роль]")
        return

    username = sys.argv[1]
    password = sys.argv[2]
    full_name = sys.argv[3] if len(sys.argv) > 3 else None
    role = sys.argv[4] if len(sys.argv) > 4 else "оператор"

    Base.metadata.create_all(engine)

    with get_session() as session:
        if session.query(User).filter_by(username=username).first() is not None:
            print(f"Користувач '{username}' вже існує.")
            return
        session.add(
            User(username=username, password_hash=hash_password(password), full_name=full_name, role=role)
        )

    print(f"Створено користувача '{username}' (роль: {role}).")


if __name__ == "__main__":
    main()
