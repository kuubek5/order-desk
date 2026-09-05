"""Admin-configurable settings, stored encrypted in the DB instead of .env.

Only the DB encryption key itself stays in .env (CLAUDE.md section 7) — every
other secret (IMAP password, service-account JSON, ...) lives here so it can
be changed from the Налаштування screen without editing files on disk.
"""

import json
import logging
import re
from dataclasses import dataclass
from typing import Optional

from cryptography.fernet import InvalidToken
from sqlalchemy.orm import Session

from app.config import EXPORT_FOLDER_PATH, GOOGLE_SHEET_ID
from app.crypto import decrypt_value, encrypt_value
from app.models import AppSetting

logger = logging.getLogger(__name__)


@dataclass
class SettingField:
    key: str
    label: str
    secret: bool = False
    multiline: bool = False
    help_text: str = ""
    # Everything defaults admin-only (Google Sheets/IMAP credentials, sync).
    # Only the two filesystem paths are operator-editable — they're a
    # per-machine detail ("which drive is the export folder mounted on
    # today"), not a credential, and whoever is actually at the workstation
    # needs to fix a moved/renamed folder without waiting on an admin.
    operator_editable: bool = False


SETTING_FIELDS = [
    SettingField(
        key="google_sheet_id",
        label="Google Sheet ID",
        help_text="ID таблиці з адресного рядка Google Sheets",
    ),
    SettingField(
        key="google_service_account_json",
        label="Google Service Account JSON",
        secret=True,
        multiline=True,
        help_text="Вміст JSON-ключа сервісного акаунта Google",
    ),
    SettingField(key="imap_login", label="Логін пошти (IMAP)"),
    SettingField(
        key="imap_password",
        label="Пароль пошти (пароль для програм)",
        secret=True,
    ),
    SettingField(
        key="export_folder_path",
        label="Шлях до папки export",
        help_text="Готові роботи для клієнтів з пошти",
        operator_editable=True,
    ),
    SettingField(
        key="technician_files_path",
        label="Шлях до папки робіт техніків",
        help_text="Куди лабораторія скидає файли на сервер",
        operator_editable=True,
    ),
    SettingField(
        key="license_key",
        label="Ліцензійний ключ",
        secret=True,
        help_text="Видає власник продукту для цього комп'ютера",
    ),
    # Appended after the original six so existing settings.html template
    # indices (fields[0]..fields[5]) stay stable — these new fields are
    # looked up by key, not position.
    SettingField(
        key="google_auth_mode",
        label="Спосіб авторизації Google",
        help_text="service_account або oauth",
    ),
    SettingField(
        key="google_oauth_client_json",
        label="Google OAuth Client JSON",
        secret=True,
        multiline=True,
        help_text="Вміст JSON «Desktop app» OAuth-клієнта з Google Cloud Console",
    ),
    SettingField(
        key="google_oauth_refresh_token",
        label="Google OAuth Refresh Token",
        secret=True,
        help_text="Заповнюється автоматично після входу через Google — не редагувати вручну",
    ),
    # Спільний пароль VNC пічок — заводський пароль моделі, не особистий.
    # Пічка може мати власний (колонка в таблиці furnaces); цей діє для всіх
    # решти. Секрет, бо ним відкривається екран обладнання в цеху; у код і в
    # git він не потрапляє (CLAUDE.md §7).
    SettingField(
        key="furnace_vnc_password",
        label="Спільний пароль VNC пічок",
        secret=True,
        help_text="Пароль екрана пічки Austromat (той самий, що в RealVNC)",
    ),
    # Спільний view-only пароль VNC верстатів — UltraVNC на ПК верстатів.
    # Окремий від пічного: різні сервери, різні паролі.
    SettingField(
        key="machine_vnc_password",
        label="Спільний пароль VNC верстатів",
        secret=True,
        help_text="View-only пароль UltraVNC на ПК верстатів (не повний!)",
    ),
    # Коли починається робочий день цеху («HH:MM», типово 07:30). Межа НЕ
    # опівночі: нічна зміна після 00:00 ще опрацьовує вчорашній день, і
    # календарна північ перекидала чергу на новий день посеред зміни
    # (див. app/business_day.py).
    SettingField(
        key="day_rollover_time",
        label="Коли починається робочий день",
        help_text="Нічна зміна після півночі ще на вчорашньому дні (типово 07:30)",
        operator_editable=True,
    ),
    # Тека проєктів Sum3D (у цеху зветься «Cam-work»). Звідти CRM ловить нові
    # проєкти й показує їх Sum3D ID у шапці черги (хід 1 ROADMAP_IDEAS.md).
    # Назва проєкту = ДАТА_ЧАС (YYYY-MM-DD_HH-MM-SS); беремо хвіст HH-MM-SS —
    # той самий ідентифікатор, який оператори вписують вручну. Читання-лише:
    # CRM у цю теку нічого не пише. operator_editable — це шлях, не секрет.
    SettingField(
        key="sum3d_projects_path",
        label="Шлях до папки проєктів Sum3D (Cam-work)",
        help_text="Звідки CRM ловить нові проєкти й підказує Sum3D ID",
        operator_editable=True,
    ),
]

OPERATOR_EDITABLE_KEYS = {field.key for field in SETTING_FIELDS if field.operator_editable}

# Ключі, значення яких НЕ мають потрапляти в Jinja-контекст. Екран показує їх
# лише ознакою «збережено» (placeholder), тож маскування нічого не ламає, але
# прибирає цілий клас багів: випадковий {{ values[...] }} або сторінка помилки
# Jinja з дампом контексту віддала б пароль пошти прямо в HTML.
SECRET_SETTING_KEYS = {
    "imap_password",
    "google_service_account_json",
    "google_oauth_client_json",
    "google_oauth_refresh_token",
    # Токен бота VARTAAIR: ним хто завгодно шле від імені бота. У контекст
    # Jinja не потрапляє — екран показує лише ознаку «збережено».
    "telegram_bot_token",
}

# Ключі, які МОЖНА очистити порожнім значенням. Для секретів порожнє поле
# означає «не міняти» (їх поле рендериться порожнім навмисно), а для шляхів
# і Sheet ID — «прибрати»: без цього помилковий мережевий шлях, що вішає
# видачу, неможливо було зняти з екрана, а тост при цьому казав «Збережено».
CLEARABLE_SETTING_KEYS = {
    "google_sheet_id",
    "export_folder_path",
    "technician_files_path",
    "sum3d_projects_path",
    "day_rollover_time",
}

# Non-secret preference keys stored in the same AppSetting table but NOT part of
# the credentials screen (SETTING_FIELDS) — set from their own settings screens.
# mail_default_material: which material the mail triage assumes when a milling
# letter carries no material signal at all (empty string / unset = off). See
# app/mail_reader.py and the /settings/recognition screen.
# mail_download_all: "1" → auto-download attachments for EVERY incoming letter
# into the spool, not only whitelisted senders; "" / unset → current behaviour
# (only trusted senders auto-download, the rest wait for a manual pull).
# notify_style / notify_position: look and placement of the transient popup
# notifications (see app/static/js/app.js showToast + .toast-zone in base.css).
# notify_events: comma-separated list of system triggers that are allowed to pop
# a toast — an empty value means "none", an absent value means "the defaults".
# last_full_sync_date: iso-дата останнього успішного повного синку таблиці.
# Не секрет і не поле форми — внутрішній штамп, з якого рахується «скільки
# днів простою надолужити» після ввімкнення (app/sheet_sync_service.py).
PREFERENCE_KEYS = {
    "furnace_background",
    "mail_default_material",
    "mail_download_all",
    "notify_style",
    "notify_position",
    "notify_events",
    "last_full_sync_date",
    # Telegram-пуш форми зворотного зв'язку (розділ /settings/feedback).
    # telegram_bot_token — секрет (маскується вище), telegram_chat_id — куди
    # бот шле (приватний чат Роми, ловиться кнопкою «Прив'язати чат» через
    # getUpdates). feedback_telegram_enabled: "1"/"" — вимикач пуша; запис у
    # БД працює завжди незалежно від нього.
    "telegram_bot_token",
    "telegram_chat_id",
    "feedback_telegram_enabled",
}

SETTING_KEYS = {field.key for field in SETTING_FIELDS} | PREFERENCE_KEYS


def get_setting(session: Session, key: str) -> Optional[str]:
    row = session.get(AppSetting, key)
    if row is None or row.value_encrypted is None:
        return None
    try:
        return decrypt_value(row.value_encrypted)
    except InvalidToken:
        # Значення збережене іншим ключем шифрування (master.key змінився —
        # напр. переїзд теки даних або нова інсталяція). Розшифрувати
        # неможливо, тож поводимось як «не задано»: застосунок працює далі,
        # а не падає в 500 на кожному роуті. Секрет доведеться ввести заново.
        logger.warning(
            "Не вдалося розшифрувати налаштування «%s» — ключ шифрування "
            "змінився; вважаю значення незаданим",
            key,
        )
        return None


def setting_unreadable(session: Session, key: str) -> bool:
    """Значення в базі Є, але поточним ключем не розшифровується.

    Відрізняє «секрет не заданий» від «заданий, але ключ шифрування змінився» —
    щоб екран активації показав правильне повідомлення (ввести ключ заново), а
    не «не активовано».
    """
    row = session.get(AppSetting, key)
    if row is None or row.value_encrypted is None:
        return False
    try:
        decrypt_value(row.value_encrypted)
        return False
    except InvalidToken:
        return True


def get_all_settings(session: Session) -> dict[str, Optional[str]]:
    return {field.key: get_setting(session, field.key) for field in SETTING_FIELDS}


def set_setting(session: Session, key: str, value: str) -> None:
    if key not in SETTING_KEYS:
        raise ValueError(f"unknown setting key: {key}")
    row = session.get(AppSetting, key)
    encrypted = encrypt_value(value)
    if row is None:
        session.add(AppSetting(key=key, value_encrypted=encrypted))
    else:
        row.value_encrypted = encrypted


_SHEET_URL_ID_RE = re.compile(r"/spreadsheets/d/([A-Za-z0-9_-]{20,})")


def extract_sheet_id(raw: str) -> str:
    """Accept either a bare Sheet ID or the whole URL from the address bar.

    Operators copy the browser URL, not the id buried inside it — pasting
    "https://docs.google.com/spreadsheets/d/<id>/edit#gid=0" used to be saved
    verbatim and every later call failed with an unhelpful "no access". Pull the
    id out when it's a URL, otherwise return the input untouched (trimmed), so a
    plain id keeps working exactly as before.
    """
    value = (raw or "").strip()
    match = _SHEET_URL_ID_RE.search(value)
    return match.group(1) if match else value


def get_google_sheet_id(session: Session) -> str:
    return get_setting(session, "google_sheet_id") or GOOGLE_SHEET_ID


def get_service_account_email(session: Session) -> Optional[str]:
    """`client_email` out of the saved service-account JSON — the address the
    spreadsheet must be shared with. Shown on the settings screen because
    otherwise there is no way for the operator to know what to paste into
    Google's Share dialog. Returns None if nothing is saved or the JSON is
    unparseable; the caller just hides the hint then."""
    content = get_setting(session, "google_service_account_json")
    if not content:
        return None
    try:
        return json.loads(content).get("client_email") or None
    except (ValueError, AttributeError):
        return None


def get_google_service_account_json(session: Session) -> Optional[str]:
    """Returns JSON content if set via the settings screen, else None.

    Callers should fall back to reading GOOGLE_SERVICE_ACCOUNT_JSON as a file
    path (the .env-based bootstrap mode) when this returns None.
    """
    return get_setting(session, "google_service_account_json")


def get_export_folder_path(session: Session) -> str:
    return get_setting(session, "export_folder_path") or EXPORT_FOLDER_PATH


def get_technician_files_path(session: Session) -> Optional[str]:
    return get_setting(session, "technician_files_path")


def get_day_rollover_time(session: Session) -> Optional[str]:
    """Межа робочого дня («HH:MM»). Порожньо = типова 07:30."""
    return get_setting(session, "day_rollover_time")


def get_sum3d_projects_path(session: Session) -> Optional[str]:
    """Тека проєктів Sum3D (Cam-work). None/порожньо = функція вимкнена."""
    return get_setting(session, "sum3d_projects_path")


def get_imap_login(session: Session) -> Optional[str]:
    return get_setting(session, "imap_login")


def get_imap_password(session: Session) -> Optional[str]:
    return get_setting(session, "imap_password")


def get_license_key(session: Session) -> Optional[str]:
    return get_setting(session, "license_key")


def get_google_auth_mode(session: Session) -> str:
    """"service_account" (default, JSON key) or "oauth" (personal Google
    account sign-in) — which credentials app/sheets.py should build."""
    return get_setting(session, "google_auth_mode") or "service_account"


def get_google_oauth_client_json(session: Session) -> Optional[str]:
    return get_setting(session, "google_oauth_client_json")


def get_google_oauth_refresh_token(session: Session) -> Optional[str]:
    return get_setting(session, "google_oauth_refresh_token")


def get_mail_default_material(session: Session) -> Optional[str]:
    """Material name the mail triage assumes for a milling letter with no
    material signal (e.g. «Цирконій»), or None/"" when the rule is off."""
    value = get_setting(session, "mail_default_material")
    return value or None


def set_mail_default_material(session: Session, value: str | None) -> None:
    set_setting(session, "mail_default_material", (value or "").strip())


def get_mail_download_all(session: Session) -> bool:
    """True → auto-download attachments for every incoming letter (not only
    whitelisted senders). Default False keeps the current selective behaviour."""
    return get_setting(session, "mail_download_all") == "1"


def set_mail_download_all(session: Session, value: bool) -> None:
    set_setting(session, "mail_download_all", "1" if value else "")


# ── Спливаючі сповіщення ────────────────────────────────────────────────
# Which system triggers may pop a toast. Each entry is (key, label, level,
# default_on) — the settings screen renders straight from this list, so adding a
# trigger here is the only step needed to expose it. `level` drives colour and
# lifetime client-side: "crit" never auto-dismisses.
NOTIFY_EVENTS: tuple[tuple[str, str, str, bool], ...] = (
    ("offline", "Втрачено зв'язок із застосунком", "crit", True),
    ("sheet_error", "Google Таблиця не відповідає", "crit", True),
    ("mail_error", "Пошта (IMAP) не відповідає", "crit", True),
    ("sheet_recovered", "Зв'язок відновлено", "ok", True),
    # «Нові роботи в таблиці» СВІДОМО вимкнено на цьому етапі (рішення власника
    # 05.09.26). Тост рахував приріст РОЗМІРУ ЧЕРГИ (`orders` = status != видано),
    # а не факт створення роботи. Тому будь-яке повернення «видано»→«нове» —
    # логіст повернув синю заливку, оператор зняв галочку — роздувало лічильник
    # і показувало «N нових робіт», яких немає. Прибрано зі списку (а не лишено
    # вимкненою галочкою), щоб фіча, яка дає хибні спрацювання, не була доступна
    # для випадкового ввімкнення, поки лічильник не рахуватиме появу роботи, а
    # не розмір черги. Повернути = додати рядок назад + полагодити підрахунок.
    # ("new_orders", "Нові роботи в таблиці", "info", True),
    # Scrap prevention: a technician correcting a row the operator may already
    # be milling. Defaults ON and is a warning, not info — this is the one the
    # operator must not scroll past.
    ("sheet_changed", "Технік змінив роботу в таблиці", "warn", True),
    ("new_mail", "Нові листи в тріажі", "info", True),
    # Записка передачі зміни. Рівень warn, поруч із «Технік змінив роботу»:
    # це те, що написав колега, який уже пішов, і перепитати нема в кого.
    # Чесна межа: тост ловить лише появу записки при вже відкритому
    # застосунку. Носій фічі — картка на черзі й бейдж у рейці, бо вони
    # правильні й на холодному завантаженні. Див. app.js::initNotifyTriggers.
    ("shift", "Нова записка передачі зміни", "warn", True),
    ("update_available", "Доступне оновлення", "warn", True),
)
NOTIFY_EVENT_KEYS = {key for key, _, _, _ in NOTIFY_EVENTS}
NOTIFY_STYLES = {"glass", "card"}
NOTIFY_POSITIONS = {"tc", "tr", "br", "bl"}
DEFAULT_NOTIFY_STYLE = "glass"
DEFAULT_NOTIFY_POSITION = "tc"


def get_notify_style(session: Session) -> str:
    """Popup look: "glass" (default) or "card"."""
    value = (get_setting(session, "notify_style") or "").strip()
    return value if value in NOTIFY_STYLES else DEFAULT_NOTIFY_STYLE


def get_notify_position(session: Session) -> str:
    """Where popups appear: tc/tr/br/bl (top-centre by default)."""
    value = (get_setting(session, "notify_position") or "").strip()
    return value if value in NOTIFY_POSITIONS else DEFAULT_NOTIFY_POSITION


def get_notify_events(session: Session) -> set[str]:
    """Enabled triggers. Unset → the per-event defaults; an explicitly saved
    empty string → nothing enabled (the operator turned everything off, which
    must not silently fall back to the defaults)."""
    raw = get_setting(session, "notify_events")
    if raw is None:
        return {key for key, _, _, on in NOTIFY_EVENTS if on}
    return {part for part in (p.strip() for p in raw.split(",")) if part in NOTIFY_EVENT_KEYS}


def set_notify_prefs(
    session: Session, *, style: str, position: str, events: set[str] | list[str]
) -> None:
    set_setting(session, "notify_style", style if style in NOTIFY_STYLES else DEFAULT_NOTIFY_STYLE)
    set_setting(
        session,
        "notify_position",
        position if position in NOTIFY_POSITIONS else DEFAULT_NOTIFY_POSITION,
    )
    kept = [key for key, _, _, _ in NOTIFY_EVENTS if key in set(events)]
    set_setting(session, "notify_events", ",".join(kept))


# ── Пічки спікання ──────────────────────────────────────────────────────────
# Самі пічки живуть у таблиці `furnaces` (назва, адреса, порт, вимикач, свій
# пароль). Тут лишається лише СПІЛЬНИЙ пароль: він заводський, один на модель,
# і пічка без власного відкривається саме ним.


def get_furnace_vnc_password(session: Session) -> Optional[str]:
    return get_setting(session, "furnace_vnc_password")


def get_machine_vnc_password(session: Session) -> Optional[str]:
    """Спільний view-only пароль VNC верстатів (окремий від пічного:
    сервери різні — UltraVNC на ПК верстатів проти вбудованого в піч)."""
    return get_setting(session, "machine_vnc_password")


def get_furnace_background(session: Session) -> bool:
    """Чи показувати фотографію пічки фоном екрана «Пічки».

    Увімкнено за замовчуванням: фон приглушений і не заважає. Але вимикач
    існує не для краси — екран стоїть біля верстатів, і якщо комусь картинка
    заважає читати числа, вона має зникати одним кліком, а не обговоренням.
    """
    value = get_setting(session, "furnace_background")
    return True if value is None else value == "1"


def set_furnace_background(session: Session, value: bool) -> None:
    set_setting(session, "furnace_background", "1" if value else "")
