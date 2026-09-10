"""Налаштування (адмін): секрети, шляхи, оператори, бекапи, оновлення.

Секрети (Google Sheet ID, сервісний JSON, IMAP-пароль, шлях до export) свідомо
НЕ живуть у .env: їх вводять на цьому екрані й тримають у базі зашифрованими
(CLAUDE.md §7). У .env лишається один ключ — той, яким шифрується сама база.

Дії, що керують МАШИНОЮ (відкрити теку, поставити оновлення), додатково
обмежені loopback: мати валідну сесію мало, запит має прийти з цього ж ПК.

Розділено на тематичні модулі (аудит 05.09.26, крок 2.9) — раніше це був один
файл на 2300 рядків. Тут лишається тільки збірка: спільний `router` і
реекспорт імен, на які спираються сусідні модулі й тести.

ПРО ПОРЯДОК. FastAPI приміряє маршрути в порядку оголошення, тому пари
«літерал проти {параметра}» (`/settings/furnaces/password` проти
`/settings/furnaces/{furnace_id}`) НЕ можна розкладати по різних модулях: тоді
порядок почав би залежати від послідовності include_router() нижче, а це
неочевидно й легко зламати. Усі такі пари живуть в одному модулі (`devices`),
де порядок видно у файлі. Сторожі — tests/test_furnace.py.

Патчі в тестах цілять у МОДУЛЬ, де ім'я справді використовується
(`app.routers.settings.connections.MailBox`, не `...settings.MailBox`):
реекспорт сюди зробив би `patch` тихим no-op. Тому нижче реекспортуються лише
самі роут-функції та спільні помічники, а не імпорти й константи модулів.
"""

from fastapi import APIRouter

from . import (
    backup,
    connections,
    devices,
    feedback,
    handout,
    materials,
    notifications,
    overview,
    sections,
    update,
    users,
)
from .backup import (
    _iso_to_tab_name,
    download_sheet_all,
    download_sheet_month,
    download_sheet_snapshot,
    export_backup,
    import_backup,
    save_sheet_backup_config,
    snapshot_sheets_now,
)
from .common import require_settings_admin
from .connections import (
    disconnect_google_oauth,
    save_imap_settings,
    settings_selfcheck,
    settings_sheet_weight,
    start_google_oauth,
    test_imap_connection,
    test_sheets_connection,
)
from .devices import (
    add_furnace,
    add_machine,
    delete_furnace,
    delete_machine,
    delete_machine_portrait,
    save_furnace_password,
    save_machine_password,
    toggle_furnace_background,
    update_furnace,
    update_machine,
    upload_machine_portrait,
)
from .feedback import (
    bind_feedback_chat,
    get_feedback_settings,
    save_feedback_settings,
    test_feedback_push,
)
from .materials import (
    add_material_alias,
    create_material,
    get_materials_settings,
    probe_material_alias,
    prune_mail_spool,
    reclassify_materials,
    remove_material_alias,
    set_recognition_default_material,
    toggle_mail_download_all,
)
from .notifications import api_notify_state, save_notification_prefs
from .overview import check_path_status, check_settings_path, get_settings, post_settings
from .handout import save_handout_qc
from .sections import save_section_state, save_vyrobitok_pin
from .update import check_update, install_update, update_install_status
from .users import (
    create_operator,
    reset_operator_password,
    set_operator_initial,
    toggle_operator_active,
)

# Реекспорт — публічний контракт пакета, а не випадковий залишок імпортів:
# ruff інакше зніс би ці рядки як «невикористані» (F401).
__all__ = [
    "router",
    "require_settings_admin",
    # overview
    "check_path_status",
    "check_settings_path",
    "get_settings",
    "post_settings",
    # connections
    "disconnect_google_oauth",
    "save_imap_settings",
    "settings_selfcheck",
    "settings_sheet_weight",
    "start_google_oauth",
    "test_imap_connection",
    "test_sheets_connection",
    # materials
    "add_material_alias",
    "create_material",
    "get_materials_settings",
    "probe_material_alias",
    "prune_mail_spool",
    "reclassify_materials",
    "remove_material_alias",
    "set_recognition_default_material",
    "toggle_mail_download_all",
    # devices
    "add_furnace",
    "add_machine",
    "delete_furnace",
    "delete_machine",
    "delete_machine_portrait",
    "save_furnace_password",
    "save_machine_password",
    "toggle_furnace_background",
    "update_furnace",
    "update_machine",
    "upload_machine_portrait",
    # notifications
    "api_notify_state",
    "save_notification_prefs",
    # backup
    "_iso_to_tab_name",
    "download_sheet_all",
    "download_sheet_month",
    "download_sheet_snapshot",
    "export_backup",
    "import_backup",
    "save_sheet_backup_config",
    "snapshot_sheets_now",
    # update
    "check_update",
    "install_update",
    "update_install_status",
    # users
    "create_operator",
    "reset_operator_password",
    "set_operator_initial",
    "toggle_operator_active",
    # feedback
    "bind_feedback_chat",
    "get_feedback_settings",
    "save_feedback_settings",
    "test_feedback_push",
    # sections
    "save_handout_qc",
    "save_section_state",
    "save_vyrobitok_pin",
]

router = APIRouter()

# Порядок = порядок, у якому роути стояли в старому settings.py. Перетинів
# «літерал проти {параметра}» між модулями немає (див. докстрінг), тож він тут
# заради читабельності діффа, а не заради матчингу.
#
# Роути ПЕРЕЛИВАЮТЬСЯ, а не include_router(): з FastAPI 0.141 include_router
# кладе в `routes` лінивий `_IncludedRouter` без `.path`, і будь-яка перевірка
# «у якому порядку стоять маршрути» (tests/test_furnace.py, інвентар роутів)
# перестала б бачити плаский список. Префіксів і тегів тут усе одно немає.
for _module in (
    overview,
    connections,
    materials,
    devices,
    notifications,
    backup,
    update,
    users,
    feedback,
    sections,
    handout,
):
    router.routes.extend(_module.router.routes)
del _module
