"""Encrypted full-database backup and restore.

CLAUDE.md section 14 names this an outstanding need: "backup для перенесення
ПК" — moving KuubMill to a new Windows PC or user account. That's exactly
the case the app's normal secret storage can't handle on its own: every
secret in `app_settings` is Fernet-encrypted under a key derived from this
machine's DPAPI store (app/windows_dpapi.py), which by design cannot be
reproduced anywhere else. A raw copy of order_desk.db would carry orders,
clients, and history just fine, but Google Sheet ID / service-account JSON /
IMAP password would come out as undecryptable ciphertext on the new machine.

The backup file sidesteps that by re-encrypting everything — operational
tables and secrets alike — under a password the admin chooses at export
time, independent of DPAPI. On restore, secrets are decrypted with that
password and immediately re-encrypted under whatever DPAPI key the target
machine already has, so the app's normal DPAPI-only-at-rest model picks
back up transparently; nothing outside this module ever has to know a
backup password existed.
"""

import base64
import json
import logging
import os
from datetime import date, datetime
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from sqlalchemy import inspect as sa_inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.business_day import utc_now
from app.crypto import decrypt_value, encrypt_value
from app.models import (
    ActionLog,
    AppSetting,
    Attachment,
    Client,
    ClientNameAlias,
    ClientSenderMemory,
    Comment,
    EmailMessage,
    Feedback,
    FeedbackImage,
    Furnace,
    FurnaceReading,
    CamBlank,
    CamBlankOrder,
    MachineLinkEvent,
    MachineReading,
    Machine,
    MailFilterCategory,
    MailFilterRule,
    Material,
    MaterialAlias,
    Order,
    OrderFocus,
    ReworkRecord,
    SavedQueueView,
    ScreenPuzzle,
    ShiftNote,
    ShiftNoteImage,
    StatusEvent,
    SyncLog,
    TelegramInvite,
    TelegramMember,
    TelegramOutbox,
    TelegramWatch,
    User,
    VyrobitokCell,
    VyrobitokDay,
    VyrobitokMonth,
)

logger = logging.getLogger(__name__)

# 2 — копія охоплює ВСІ таблиці (див. `_TABLE_MODELS`). У версії 1 їх було 13
# із 29: переїзд на новий ПК привозив роботи й операторів, але лишав позаду
# пічки, верстати з паролями, матеріали, фільтри пошти, журнал дій і ВЕСЬ
# Виробіток (зарплатні цифри). Файли версії 1 читаються далі — просто в них
# менше таблиць (ревʼю 07.09.26).
#
# 3 — паролі пічок і верстатів та токен агента (`_ENCRYPTED_COLUMNS`) їдуть
# усередині копії ВІДКРИТО, а не ciphertext'ом старої машини. У версії 2 вони
# формально переносились, але на новому ПК не розшифровувались: пічки й
# верстати довелося б налаштовувати заново — тобто рівно те, від чого копія
# мала рятувати (знайдено прогоном «переїзд на інший компʼютер» 07.09.26).
# Старі файли читаються як були: у них ці колонки лишаються недоторканими,
# інакше вийшло б подвійне шифрування.
FORMAT_VERSION = 3
KDF_ITERATIONS = 480_000

# Parent-first: safe insert order under foreign-key constraints. Restore
# deletes in the reverse of this list (children before parents) and
# inserts in this order (parents before children).
#
# Список ПОВНИЙ і явний, а не зібраний з `Base.metadata`: порядок вставки
# тримає саме людина (цикл orders ↔ email_messages автоматичний сортувальник
# не розвʼязує). Щоб нову таблицю не забули, її відсутність тут валить
# `tests/test_backup_covers_every_table.py` — там же список свідомих
# винятків.
# `list[Any]`, а не `list[type[Base]]`: перевірка типів не бачить
# `__tablename__` на декларативному класі й сипала помилками на кожному
# зверненні до нього — шум, який ховав справжні (07.09.26).
_TABLE_MODELS: list[Any] = [
    # Батьки без залежностей.
    User,
    Client,
    Material,
    MaterialAlias,
    MailFilterCategory,
    MailFilterRule,
    Furnace,
    FurnaceReading,
    # Історія показань верстатів. У копію входить із тієї ж причини, що й
    # пічна: це ЄДИНИЙ у системі запис про те, що і коли фрезерувалось —
    # подій «у фрезеруванні» в базі нуль, а колонка «Відфрезерував» у таблиці
    # містить ініціали людини, не час. Переїзд на новий ПК без неї знищив би
    # рівно ті дані, заради яких її й завели.
    MachineReading,
    # Журнал обривів зв'язку. У копію входить із тієї ж причини, що й історія
    # показань: сенс цієї таблиці — довге вікно, на якому видно закономірність
    # («щодня о тій самій порі», «щоразу разом з усіма»). Переїзд на новий ПК
    # без неї почав би спостереження з нуля саме тоді, коли воно потрібне.
    MachineLinkEvent,
    # Нові диски: замовлення на склад і історія взятих дисків. У копію
    # входять, бо це журнал замовлень — переїзд на новий ПК без них почав би
    # відлік «взято після останнього замовлення» з нуля. Замовлення — ПЕРЕД
    # дисками: диск посилається на своє замовлення (order_id).
    CamBlankOrder,
    CamBlank,
    Machine,
    SyncLog,
    ClientNameAlias,
    ClientSenderMemory,
    VyrobitokMonth,
    VyrobitokCell,
    VyrobitokDay,
    # Telegram-бот. Черга сповіщень — журнал доставки (що й коли пішло Ромі,
    # що не дійшло). Пам'ять переходів після відновлення безпечна: `seen_at`
    # старий, тож перший кадр на новому ПК стає тихою точкою відліку, а не
    # «пічка закрилась»; невідправлене давно прострочене й не піде.
    TelegramOutbox,
    TelegramWatch,
    # Учасники бота (FK на users — тому після User). Переїзд на новий ПК без
    # них мовчки відрізав би всіх, кого власник запросив. Запрошення — разом:
    # невикористане посилання після переїзду має діяти так само.
    TelegramMember,
    TelegramInvite,
    # Роботи й усе, що на них посилається.
    Order,
    EmailMessage,
    Attachment,
    StatusEvent,
    ActionLog,
    Comment,
    ReworkRecord,
    OrderFocus,
    SavedQueueView,
    # Скринька невідомих екранів (FK на users — тому після User). Картинки
    # кадрів у копію НЕ їдуть, як і решта кадрів печей і верстатів: це робочий
    # кеш. А рядки їдуть, бо в них лежить те, чого більше ніде немає — підпис
    # людини («це екран помилки», «тут нічого важливого») і лічильник «бачено
    # N разів». Переїзд на новий ПК без них знищив би саме людську роботу, а не
    # кеш; рядок без картинки далі показує причину, подробиці й підпис — так
    # само, як записка зміни без прибраного фото.
    ScreenPuzzle,
    # Зміна і звернення.
    ShiftNote,
    ShiftNoteImage,
    Feedback,
    FeedbackImage,
]


class BackupPasswordError(Exception):
    """Wrong backup password — Fernet's own auth tag failed to verify."""


class BackupFormatError(Exception):
    """Not a recognizable KuubMill backup file."""


class BackupIncompleteError(Exception):
    """Відновлення поклало в базу не те, що лежало у файлі.

    Копія несе власний перелік «скільки рядків у якій таблиці» (`manifest`).
    Якщо після вставки числа не збіглись, ми НЕ лишаємо напівживу базу мовчки:
    транзакція відкочується, і адмін бачить, чого саме бракує.
    """


_MODEL_BY_TABLE = {model.__tablename__: model for model in _TABLE_MODELS}

# Секрети, які живуть не в `app_settings`, а колонками таблиць: паролі пічок і
# верстатів та токен агента. Вони зашифровані ключем МАШИНИ, тому в копії їх
# не можна везти як є — на новому ПК вони мертві, і саме пічки з верстатами
# довелося б налаштовувати заново після переїзду (знайдено прогоном
# «переїзд на інший компʼютер» 07.09.26).
#
# Усередині копії вони лежать відкрито, але сама копія зашифрована паролем
# адміна, тож рівень захисту не падає — це та сама схема, що для `app_settings`
# від самого початку.
_ENCRYPTED_COLUMNS: dict[str, tuple[str, ...]] = {
    "furnaces": ("password_encrypted",),
    "machines": ("password_encrypted", "agent_token_encrypted"),
}


# Скільки таблиць додано ПІСЛЯ того, як копії почали нести прапорець `partial`.
# Потрібне лише для файлів БЕЗ прапорця (формат до нього): у них склад таблиць
# менший за нинішній не тому, що копія часткова, а тому, що тих таблиць тоді
# ще не було. Число росте разом із `_TABLE_MODELS` — і саме тому воно тут,
# поруч зі списком, а не зашите в логіку відновлення.
_NEW_SINCE_FLAG = 1  # screen_puzzles (12.09.26)


def _row_to_dict(obj: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for col in sa_inspect(obj).mapper.column_attrs:
        val = getattr(obj, col.key)
        if isinstance(val, (datetime, date)):
            val = val.isoformat()
        out[col.key] = val
    return out


def _dict_to_row(model: type, data: dict[str, Any]):
    kwargs: dict[str, Any] = {}
    for col in sa_inspect(model).columns:
        name = col.name
        if name not in data:
            continue
        val = data[name]
        if val is not None and isinstance(val, str):
            py_type = col.type.python_type if hasattr(col.type, "python_type") else None
            if py_type is datetime:
                val = datetime.fromisoformat(val)
            elif py_type is date:
                val = date.fromisoformat(val)
        kwargs[name] = val
    return model(**kwargs)


def _derive_key(password: str, salt: bytes, iterations: int = KDF_ITERATIONS) -> bytes:
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=iterations)
    return base64.urlsafe_b64encode(kdf.derive(password.encode("utf-8")))


def create_backup(
    session: Session, password: str, *, only_tables: list[str] | None = None
) -> bytes:
    """Serialize every table plus decrypted settings, encrypt under `password`.

    Returns the full backup file's bytes (a small JSON envelope; the actual
    data sits inside `envelope["payload"]`, a Fernet token).

    `only_tables` робить копію ЧАСТКОВОЮ: у файл ідуть лише названі таблиці, а
    налаштування не йдуть узагалі. Це інший інструмент, ніж повна копія:
    повна відповідає на «перенести все на новий ПК», часткова — на «повернути
    те, що щойно зникло», не відкочуючи разом із ним усю роботу за день
    (app/backup_parts.py).
    """
    selected = None if only_tables is None else set(only_tables)
    tables: dict[str, list[dict[str, Any]]] = {}
    unreadable: list[str] = []
    for model in _TABLE_MODELS:
        table = model.__tablename__
        if selected is not None and table not in selected:
            continue
        rows = session.query(model).all()
        dumped = [_row_to_dict(r) for r in rows]
        for column in _ENCRYPTED_COLUMNS.get(table, ()):
            for row_data in dumped:
                stored = row_data.get(column)
                if not stored:
                    continue
                try:
                    row_data[column] = decrypt_value(stored)
                except InvalidToken:
                    row_data[column] = None
                    unreadable.append(f"{table}.{column}")
        tables[table] = dumped

    # Нечитабельне налаштування НЕ валить копію. Ключ шифрування прив'язаний до
    # машини (DPAPI), і після переїзду теки чи перевстановлення Windows старі
    # рядки перестають розшифровуватись — рівно тоді, коли копію й роблять, щоб
    # перевезти дані. Досі перший же такий рядок давав 500, і людина лишалась
    # без жодного способу забрати роботи (знайдено живим прогоном на стенді
    # 07.09.26).
    #
    # Роботи, клієнти й історія від ключа не залежать і їдуть повністю.
    # Втрачається лише те, що вже нечитабельне на цій машині; його ІМЕНА (не
    # значення) кладемо в конверт, щоб на новому ПК було видно, які саме
    # налаштування доведеться ввести заново.
    # Секрети їдуть лише в ПОВНІЙ копії. Часткова — це «поверни мені
    # операторів»; тягнути в неї пароль пошти й ключ таблиці означало б робити
    # з дрібної операції ще один файл, який страшно загубити.
    settings: dict[str, str] = {}
    for row in (session.query(AppSetting).all() if selected is None else []):
        if row.value_encrypted is None:
            continue
        try:
            settings[row.key] = decrypt_value(row.value_encrypted)
        except InvalidToken:
            unreadable.append(row.key)
    if unreadable:
        logger.warning(
            "Копія: %s налаштувань не розшифровано (ключ цієї машини змінився): %s",
            len(unreadable), ", ".join(sorted(unreadable)),
        )

    # Перелік «таблиця → скільки рядків» їде ВСЕРЕДИНІ копії, під тим самим
    # шифром. Після відновлення ми звіряємо базу з ним: копія сама себе
    # перевіряє, і «відновилось, але половини нема» стає видно одразу.
    manifest = {name: len(rows) for name, rows in tables.items()}
    if selected is None:
        manifest[AppSetting.__tablename__] = len(settings)

    payload = json.dumps(
        {"tables": tables, "settings": settings, "manifest": manifest},
        ensure_ascii=False,
    ).encode("utf-8")

    salt = os.urandom(16)
    key = _derive_key(password, salt)
    token = Fernet(key).encrypt(payload)

    envelope = {
        "format_version": FORMAT_VERSION,
        # Скільки чого всередині — видно ДО введення пароля (сам вміст
        # зашифрований). Це те, що показує екран перед відновленням.
        "manifest": manifest,
        # Імена налаштувань, які ця машина вже не може прочитати (лише імена,
        # ніколи значення). Видно ДО введення пароля — щоб на новому ПК одразу
        # було ясно, що саме доведеться ввести руками.
        "unreadable_settings": sorted(unreadable),
        # Часткова копія мусить сама казати, що вона часткова: інакше екран
        # відновлення обіцяв би замінити все, а замінив би дві таблиці — або
        # навпаки, і людина дізналась би про це вже по наслідках.
        "partial": selected is not None,
        "app": "order-desk",
        "created_at": utc_now().isoformat() + "Z",
        "kdf": "pbkdf2-sha256",
        "kdf_iterations": KDF_ITERATIONS,
        "salt": base64.b64encode(salt).decode("ascii"),
        "payload": token.decode("ascii"),
    }
    return json.dumps(envelope, ensure_ascii=False, indent=2).encode("utf-8")


def restore_backup(session: Session, file_bytes: bytes, password: str) -> dict[str, int]:
    """Replace all operational data and secrets with what's in the backup.

    Destructive by design — this is a restore, not a merge. Raises
    `BackupFormatError` for anything that isn't an KuubMill backup file
    and `BackupPasswordError` for a password that doesn't match (Fernet's
    own authentication tag fails to verify rather than silently producing
    garbage, so a wrong password is always caught here, never left to
    surface as corrupted data downstream).
    """
    try:
        envelope = json.loads(file_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackupFormatError("Файл резервної копії пошкоджений або це не бекап KuubMill.") from exc

    if not isinstance(envelope, dict) or envelope.get("app") != "order-desk" or "payload" not in envelope or "salt" not in envelope:
        raise BackupFormatError("Файл резервної копії пошкоджений або це не бекап KuubMill.")

    # Копія з МАЙБУТНЬОЇ версії формату може нести таблиці, яких ця збірка ще
    # не знає: мовчки відновити її означало б тихо їх викинути.
    file_version = envelope.get("format_version", 1)
    if not isinstance(file_version, int) or file_version > FORMAT_VERSION:
        raise BackupFormatError(
            f"Копія зроблена новішою версією KuubMill (формат {file_version}, "
            f"ця збірка розуміє {FORMAT_VERSION}). Онови застосунок і спробуй знову."
        )

    iterations = envelope.get("kdf_iterations", KDF_ITERATIONS)
    try:
        salt = base64.b64decode(envelope["salt"])
    except (ValueError, TypeError) as exc:
        raise BackupFormatError("Файл резервної копії пошкоджений або це не бекап KuubMill.") from exc

    key = _derive_key(password, salt, iterations)

    try:
        payload = Fernet(key).decrypt(envelope["payload"].encode("ascii"))
    except InvalidToken as exc:
        raise BackupPasswordError("Невірний пароль резервної копії.") from exc

    data = json.loads(payload.decode("utf-8"))
    tables: dict[str, list[dict[str, Any]]] = data.get("tables", {})
    settings: dict[str, str] = data.get("settings", {})

    counts: dict[str, int] = {}

    # `orders.email_message_id` і `email_messages.order_id` посилаються одна на
    # одну, тож жоден порядок вставки не задовольнить перевірку зовнішніх
    # ключів по рядку: перша ж таблиця пари посилається на ще порожню другу.
    # Поки `foreign_keys=ON` не було (до K.5), це минало непоміченим; з ним
    # відновлення падало IntegrityError на email_messages.
    # `defer_foreign_keys` переносить перевірку на COMMIT — тобто на момент,
    # коли обидві таблиці вже заповнені. Це не послаблення: биті посилання
    # так само не пройдуть, лише пізніше й уже по цілій картині. SQLite сам
    # скидає прапорець на кожному коміті, тож це діє рівно на цю транзакцію.
    session.execute(text("PRAGMA defer_foreign_keys=ON"))

    # Часткова копія заміщає ЛИШЕ свої таблиці. Це головна різниця між двома
    # інструментами: повна відповідає на «перенести все», часткова — на
    # «поверни те, що зникло», і не має права зачепити роботи, зроблені після
    # неї. Тому і видалення, і вставка йдуть тільки по таблицях із файлу
    # (07.09.26).
    if "partial" in envelope:
        # Файл сам каже, який він. Довіряти саме прапорцю, а не складу таблиць:
        # інакше КОЖНА повна копія, зроблена попередньою збіркою, ставала б
        # «частковою» щоразу, коли ми додаємо нову таблицю (12.09.26 такою
        # таблицею стала `screen_puzzles`). Наслідок був мовчазний і найгірший
        # з можливих: повне відновлення на новому ПК пропускало б секрети —
        # гілка `if not partial` нижче переписує `app_settings`, — тобто копія
        # переставала робити рівно те, заради чого існує.
        partial = bool(envelope["partial"])
    else:
        # Старіший файл прапорця не має — впізнаємо частковість за складом.
        # Таблиця, якої в тій збірці ще не існувало, тут працює проти нас, тож
        # порівнюємо з тим, що файл МІГ мати: повна копія несе всі таблиці,
        # відомі ЙОМУ, а нових не знає.
        known = {model.__tablename__ for model in _TABLE_MODELS}
        partial = bool(set(tables) - known) or len(set(tables)) < len(known) - _NEW_SINCE_FLAG
    touched = [model for model in _TABLE_MODELS if model.__tablename__ in tables]
    if not partial:
        touched = list(_TABLE_MODELS)

    for model in reversed(touched):
        session.query(model).delete()

    for model in touched:
        table = model.__tablename__
        rows = tables.get(table, [])
        secret_columns = _ENCRYPTED_COLUMNS.get(table, ()) if file_version >= 3 else ()
        for row_data in rows:
            if secret_columns:
                # У копії формату 3 ці колонки лежать відкрито (сам файл під
                # паролем) — шифруємо їх ключем ЦІЄЇ машини, як і решту
                # секретів. У старіших копіях там ciphertext чужої машини:
                # чіпати його не можна, інакше вийде подвійне шифрування.
                row_data = dict(row_data)
                for column in secret_columns:
                    value = row_data.get(column)
                    if value:
                        row_data[column] = encrypt_value(value)
            session.add(_dict_to_row(model, row_data))
        counts[table] = len(rows)

    # Секрети переписує лише повна копія. Часткова їх не несе, і стерти
    # налаштування, «відновлюючи операторів», було б найгіршим сюрпризом.
    if not partial:
        session.query(AppSetting).delete()
        for key_name, value in settings.items():
            session.add(AppSetting(key=key_name, value_encrypted=encrypt_value(value)))
        counts["app_settings"] = len(settings)

    # Звірка з перелічником копії ДО коміту: не збіглось — нічого не міняємо.
    manifest = data.get("manifest") or {}
    session.flush()
    missing: list[str] = []
    for table_name, expected in manifest.items():
        model = _MODEL_BY_TABLE.get(table_name)
        actual = (
            session.query(AppSetting).count()
            if table_name == AppSetting.__tablename__
            else (session.query(model).count() if model is not None else None)
        )
        if actual is None:
            missing.append(f"{table_name}: ця збірка такої таблиці не знає ({expected} рядків у копії)")
        elif actual != expected:
            missing.append(f"{table_name}: у копії {expected}, у базі {actual}")
    if missing:
        session.rollback()
        raise BackupIncompleteError(
            "Відновлення скасовано — база не збіглася з копією: " + "; ".join(missing)
        )

    # Перевірка зовнішніх ключів відкладена до цього коміту (див. вище). Биті
    # посилання у файлі спливуть саме тут — і це має читатись як «копія
    # непридатна», а не як 500 без пояснення.
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise BackupIncompleteError(
            "Відновлення скасовано — у копії є посилання на записи, яких у ній немає."
        ) from exc
    return counts
