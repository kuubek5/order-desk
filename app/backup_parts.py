"""Частковий бекап: набори даних, які можна зберегти й повернути окремо.

Повна копія відповідає на питання «перенести все на інший ПК». Але буденна
біда інша й дрібніша: хтось видалив оператора, стерли перелік клієнтів,
загубились синоніми матеріалів. Заливати заради цього повну копію означає
відкотити РАЗОМ з тим і всі роботи, зроблені після неї, — тобто вилікувати
подряпину ампутацією.

Тут — набори: людські назви для груп таблиць, які мають сенс окремо. Кожен
набір відновлюється сам по собі й НЕ чіпає нічого поза собою.

Чого тут свідомо немає: робіт з історією. Роботи звʼязані з листами,
вкладеннями, подіями статусів, коментарями й переробками; «повернути тільки
роботи» — це і є повна копія, для неї є повна копія. Набір, який вдає, що вміє
менше, ніж насправді робить, гірший за його відсутність.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BackupPart:
    """Один набір: що це людською мовою і з яких таблиць складається."""

    key: str
    label: str
    hint: str
    tables: tuple[str, ...]


# Порядок — як на екрані. Найчастіша втрата згори.
PARTS: tuple[BackupPart, ...] = (
    BackupPart(
        key="users",
        label="Оператори",
        hint="Облікові записи, ролі, літери в таблиці. Паролі теж — у тому ж вигляді, що в базі.",
        tables=("users",),
    ),
    BackupPart(
        key="clients",
        label="Клієнти",
        hint="Картки клієнтів, їхні написання в таблиці й памʼять «від кого який клієнт».",
        tables=("clients", "client_name_aliases", "client_sender_memory"),
    ),
    BackupPart(
        key="materials",
        label="Матеріали",
        hint="Перелік матеріалів і синоніми, за якими розпізнаються назви з таблиці й листів.",
        tables=("materials", "material_aliases"),
    ),
    BackupPart(
        key="mail_filters",
        label="Фільтри пошти",
        hint="Правила «не наша робота» й категорії, яких навчали руками.",
        tables=("mail_filter_categories", "mail_filter_rules"),
    ),
    BackupPart(
        key="devices",
        label="Пічки й верстати",
        hint="Адреси, порти, паролі й токени агента — усе, що інакше довелось би вбивати заново.",
        tables=("furnaces", "machines"),
    ),
    BackupPart(
        key="vyrobitok",
        label="Виробіток",
        hint="Місяці, дні й клітинки обліку одиниць — цифри, з яких рахують зарплату.",
        tables=("vyrobitok_months", "vyrobitok_cells", "vyrobitok_days"),
    ),
)

PART_BY_KEY = {part.key: part for part in PARTS}


def tables_for(keys: list[str] | tuple[str, ...]) -> list[str]:
    """Таблиці для обраних наборів, без повторів, у порядку оголошення.

    Невідомий ключ ігнорується: форма приходить з браузера, і чужий рядок у ній
    не має валити збереження — набори, які ми впізнали, збережуться.
    """
    wanted = [key for key in keys if key in PART_BY_KEY]
    tables: list[str] = []
    for key in wanted:
        for table in PART_BY_KEY[key].tables:
            if table not in tables:
                tables.append(table)
    return tables


def parts_for_tables(tables: list[str] | tuple[str, ...]) -> list[BackupPart]:
    """Які набори лежать у копії — за переліком її таблиць.

    Потрібно на боці відновлення: у файлі є лише імена таблиць, а людині треба
    сказати «оператори і клієнти», а не перелік із семи слів.
    """
    present = set(tables)
    return [part for part in PARTS if present.issuperset(part.tables)]
