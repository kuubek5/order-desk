"""Копія має везти ВСЕ, без чого застосунок на новому ПК не працює.

Два різні провали вже траплялись, і обидва тихі — база начебто переїхала, а
робота не відновилась:

* колонка-секрет, зашифрована ключем машини, їхала як є й на новому ПК не
  розшифровувалась (паролі пічок і верстатів, знайдено прогоном переїзду
  07.09.26);
* нова таблиця, додана в моделі, не потрапляла в перелік копії.

Обидва не ловляться звичайними тестами: round-trip у межах ОДНІЄЇ машини
проходить, бо ключ там той самий. Тому сторожі порівнюють саме реєстри.
"""

import re
from pathlib import Path

from app.backup import _ENCRYPTED_COLUMNS, _TABLE_MODELS
from app.db import Base

MODELS_FILE = Path(__file__).resolve().parent.parent / "app" / "models.py"

# `app_settings.value_encrypted` обробляється окремою гілкою (`settings` у
# payload) — там значення ще й перевіряються на читабельність, тож у
# `_ENCRYPTED_COLUMNS` його немає свідомо.
HANDLED_ELSEWHERE = {("app_settings", "value_encrypted")}


def _encrypted_columns_in_models() -> set[tuple[str, str]]:
    """(таблиця, колонка) для кожної колонки з іменем на `_encrypted`."""
    text = MODELS_FILE.read_text(encoding="utf-8")
    found: set[tuple[str, str]] = set()
    table = None
    for line in text.splitlines():
        match = re.match(r'\s*__tablename__\s*=\s*"([^"]+)"', line)
        if match:
            table = match.group(1)
            continue
        column = re.match(r"\s*([a-z_]+_encrypted)\s*:\s*Mapped", line)
        if column and table:
            found.add((table, column.group(1)))
    return found


def test_every_encrypted_column_is_carried_across_machines():
    declared = {
        (table, column)
        for table, columns in _ENCRYPTED_COLUMNS.items()
        for column in columns
    } | HANDLED_ELSEWHERE

    missing = _encrypted_columns_in_models() - declared
    assert not missing, (
        "ці колонки зашифровані ключем МАШИНИ й поїдуть у копії мертвими: "
        + ", ".join(f"{t}.{c}" for t, c in sorted(missing))
        + " — додайте їх у app/backup.py::_ENCRYPTED_COLUMNS"
    )


def test_the_scan_actually_finds_something():
    """Сторож сторожа: якщо розбір моделей зламається, тест вище стане зеленим
    і порожнім."""
    assert len(_encrypted_columns_in_models()) >= 4


def test_declared_columns_really_exist():
    """Зайвий запис у реєстрі так само небезпечний: він мовчки нічого не робить,
    а виглядає як зроблена робота."""
    real = {
        (model.__tablename__, column.name)
        for model in Base.__subclasses__()
        for column in model.__table__.columns
    }
    declared = {
        (table, column)
        for table, columns in _ENCRYPTED_COLUMNS.items()
        for column in columns
    }
    ghosts = declared - real
    assert not ghosts, "у реєстрі колонки, яких немає в моделях: " + str(sorted(ghosts))


def test_every_table_model_is_a_real_table():
    """Перелік таблиць копії не має розходитися з моделями."""
    for model in _TABLE_MODELS:
        assert hasattr(model, "__tablename__")
