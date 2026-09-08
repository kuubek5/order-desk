"""Scanner for the export folder structure — reading physical milled jobs for morning handoff.

Folder structure:
  export/<client_name>/<batch_folder>/<material_color>/<files>

Walks exactly 3 levels deep, collecting ExportEntry objects for each material-color leaf.
"""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import logging
import os
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ExportEntry:
    """Represents a single material-color folder with its files and metadata."""

    client_folder_name: str
    """Level 1: client name folder (may have typos vs. sheet names — fuzzy matching is caller's job)."""

    batch_folder_name: str
    """Level 2: Windows auto-numbered folder name (e.g., 'Новая папка', 'Новая папка (2)').
    The name carries no information, only the filesystem creation timestamp matters."""

    created_at: datetime
    """Level 2 folder's filesystem creation time.
    On Windows, os.stat(path).st_ctime is the genuine creation time.
    On POSIX, st_ctime is metadata-change time, but this project targets Windows (per CLAUDE.md)."""

    material_color_folder_name: str
    """Level 3: material+color folder name (free-text, e.g., 'mono a3', 'pmma a2')."""

    files: list[str]
    """Filenames (just names, not paths) in the material-color folder.
    May be empty if folder exists but has no files (useful signal: files may still be copying)."""

    folder_path: Path
    """Full path to the level-3 (material-color) folder.
    Kept for later use (e.g., building a 'copy path' button), not used by scanning logic."""


def scan_export_folder(root: Path, not_before: datetime | None = None) -> list[ExportEntry]:
    """Scan the export folder tree and return a flat list of ExportEntry objects.

    Walks exactly 3 levels deep: client / batch / material-color.
    Skips silently (does not raise) on:
      - Non-directory entries where a directory is expected
      - Permission errors on a specific subfolder (logs nothing, just skips that branch)
      - Empty material-color folders (still returns them with files=[])

    If root doesn't exist, returns [] without raising.

    Args:
        root: Root export folder path.

    Returns:
        Flat list of ExportEntry, one per material-color leaf folder found.
    """
    root = Path(root)
    return [
        entry
        for name in list_export_client_names(root)
        for entry in scan_export_client(root, name, not_before)
    ]


# ПРОДУКТИВНІСТЬ: `export` — це шара Synology через SMB, де кожен системний
# виклик коштує мережеву ходку. Тому тут два окремих входи:
#
#   list_export_client_names — ОДИН scandir кореня. Дешево, і саме цього
#       достатньо для нечіткого зіставлення імен «таблиця ↔ тека».
#   scan_export_client — глибина (партія / матеріал / файли) для ОДНОГО
#       клієнта. Викликається тільки для тих, хто реально на екрані.
#
# Раніше екран видачі обходив УСЕ дерево, хоча показує 10-20 клієнтів із
# сотень. Тепер робота пропорційна показаному, а не вмісту сховища.


def _dir_entries(path) -> list:
    """os.scandir одним запитом. На Windows тип запису й час створення
    приходять РАЗОМ зі списком теки, тож entry.is_dir()/stat() безкоштовні —
    на відміну від Path.iterdir() + .is_dir(), де кожен запис це окрема ходка."""
    try:
        with os.scandir(path) as it:
            return list(it)
    except (PermissionError, OSError):
        return []


def list_export_client_names(root: Path) -> list[str]:
    """Імена тек клієнтів (рівень 1). Один запит до сховища."""
    root = Path(root)
    if not root.exists():
        return []
    names = []
    for client in _dir_entries(root):
        try:
            if client.is_dir():
                names.append(client.name)
        except OSError:
            continue
    return sorted(names)


def scan_export_client(
    root: Path, client_folder_name: str, not_before: datetime | None = None
) -> list[ExportEntry]:
    """Партії/матеріали/файли ОДНОГО клієнта.

    `not_before` відсікає старі партії, НЕ заходячи в них. Це головний важіль
    швидкодії: у бойовому логу 27.08.26 повний обхід дав
    «46148 записів, 511.42с» — 262 клієнти по ~176 партій кожен, тобто роки
    накопичених папок. Видача показує роботи за останні 30 днів, і файли
    не можуть лежати в партії, створеній ДО появи роботи. Час створення
    scandir віддає безкоштовно разом зі списком теки, тож відсів коштує нуль
    ходок, а економить тисячі.
    """
    client_root = Path(root) / client_folder_name
    entries: list[ExportEntry] = []

    for batch in _dir_entries(client_root):
        try:
            if not batch.is_dir():
                continue
            created_at = datetime.fromtimestamp(batch.stat().st_ctime)
        except (OSError, PermissionError, ValueError, OverflowError):
            # без часу створення партію не відрізнити від сусідньої
            continue

        # Стару партію пропускаємо ДО того, як зазирнути всередину.
        if not_before is not None and created_at < not_before:
            continue

        for material in _dir_entries(batch.path):
            try:
                if not material.is_dir():
                    continue
            except OSError:
                continue

            files_list = []
            for f in _dir_entries(material.path):
                try:
                    if f.is_file():
                        files_list.append(f.name)
                except OSError:
                    continue

            entries.append(
                ExportEntry(
                    client_folder_name=client_folder_name,
                    batch_folder_name=batch.name,
                    created_at=created_at,
                    material_color_folder_name=material.name,
                    files=files_list,
                    folder_path=Path(material.path),
                )
            )
    return entries


def scan_export_client_latest(
    root: Path, client_folder_name: str, count: int = 3
) -> list[ExportEntry]:
    """Найновіші `count` партій клієнта — без межі за датою.

    Запасний шлях для видачі. Основний обхід відсікає партії, старіші за
    вікно роботи, і це правильно для швидкодії — але буває, що клієнт
    надіслав файли задовго до фрезерування, і тоді в вікні немає нічого,
    хоча тека повна (бойовий випадок 28.08.26: «папку знайти не можу, хоча
    вона є»).

    Коштує один scandir теки клієнта плюс захід у `count` найновіших партій:
    час створення scandir віддає безкоштовно разом зі списком, тож вибір
    найновіших не коштує жодної зайвої ходки — на відміну від повного обходу
    всіх ~176 партій, який і робив екран непридатним."""
    client_root = Path(root) / client_folder_name
    batches = []
    for batch in _dir_entries(client_root):
        try:
            if not batch.is_dir():
                continue
            batches.append((datetime.fromtimestamp(batch.stat().st_ctime), batch))
        except (OSError, PermissionError, ValueError, OverflowError):
            continue
    batches.sort(key=lambda pair: pair[0], reverse=True)

    entries: list[ExportEntry] = []
    for created_at, batch in batches[:count]:
        for material in _dir_entries(batch.path):
            try:
                if not material.is_dir():
                    continue
            except OSError:
                continue
            files_list = []
            for f in _dir_entries(material.path):
                try:
                    if f.is_file():
                        files_list.append(f.name)
                except OSError:
                    continue
            entries.append(
                ExportEntry(
                    client_folder_name=client_folder_name,
                    batch_folder_name=batch.name,
                    created_at=created_at,
                    material_color_folder_name=material.name,
                    files=files_list,
                    folder_path=Path(material.path),
                )
            )
    entries.sort(key=lambda e: e.created_at)
    return entries


# ── Кеш ──────────────────────────────────────────────────────────────────
# stale-while-revalidate: свіже віддається одразу, протухле теж віддається
# одразу з фоновим оновленням. Блокує лише найперший запит. Папка змінюється,
# коли оператор приймає лист, і тоді кеш скидають явно — тож TTL тут не про
# свіжість даних, а про те, як часто ми знову йдемо на SMB.
#
# Мусить бути БІЛЬШИМ за EXPORT_WARM_INTERVAL_SECONDS (120с у web.py). Було
# 90с при прогріві раз на 122с: кожен запис протухав за 32с ДО наступного
# прогріву, тож прогрів щоразу заходив на повний обхід сховища заново, а
# кожен перший клік оператора діставав протухле значення й тягнув за собою
# фонове переобхід. 180с лишає запас на затримку самого прогріву (він сам
# триває 2-7с на бойовому сховищі) і не міняє того, коли дані оновлюються:
# приймання листа скидає кеш явно, а не за таймером.
_CACHE_TTL_SECONDS = 180.0
_cache: dict[tuple, tuple[float, Any]] = {}
_cache_lock = threading.Lock()
_refreshing: set[tuple] = set()


_counters = {"hit": 0, "stale": 0, "miss": 0, "kept": 0}
# Локи «один обхід на ключ». Ключів мало (теки клієнтів), тож словник не
# чиститься — його розмір обмежений складом сховища, не трафіком.
_producer_locks: dict[tuple, threading.Lock] = {}
"""Скільки звернень до сховища кеш прийняв на себе, а скільки пропустив.

Це ВИМІРЮВАЛЬНИЙ прилад, а не оптимізація. Бойова скарга 03.09.26: видача
відкривається 3-5 с, а лог каже «Handout export scan: ... 3.86с» — але не
каже, чому. Промах (ключ ніколи не грівся), протухле (прогрів відстає) і
влучання лікуються ЗОВСІМ по-різному, а з домашнього ПК бойове сховище не
видно. Тепер відповідь є в самому логу видачі."""


def cache_counters() -> dict[str, int]:
    """Знімок лічильників. Викликач бере різницю до/після — так число
    стосується саме цього рендера, а не всього часу роботи процесу."""
    with _cache_lock:
        return dict(_counters)


def _is_mass_vanish(previous, value) -> bool:
    """Чи схоже нове значення на «шара відпала», а не на «тек справді не стало».

    Той самий поріг, що на синку таблиці (CLAUDE.md §14): падіння БІЛЬШЕ НІЖ на
    5 позицій І більше ніж на чверть. Причина та сама: мережева тека, що
    відпала, віддає не помилку, а порожній список — і сканер записував цю
    порожнечу в кеш як правильну відповідь на 180 с. На екрані видачі всі
    клієнти ставали «не прив'язані», а прев'ю STL зникало (аудит 08.09.26).

    Порожнеча поверх порожнечі — не подія: перший прогрів на живій, але ще
    порожній теці має закешуватись нормально.
    """
    try:
        was, now_len = len(previous), len(value)
    except TypeError:  # pragma: no cover — на випадок нескінченних ітераторів
        return False
    if was == 0:
        return False
    lost = was - now_len
    return lost > 5 and lost > was * 0.25


def _store(key: tuple, value):
    """Покласти результат у кеш, якщо він не схожий на обрив звʼязку.

    Підозріле значення НЕ затирає попереднє: краще показати трохи застарілий
    список тек, ніж порожній. Штамп часу теж не оновлюємо — інакше наступний
    прохід вважав би дані свіжими й не спробував би ще раз.
    """
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None and _is_mass_vanish(hit[1], value):
            _counters["kept"] += 1
            logger.warning(
                "Сканер export: різке падіння (%d → %d) для %s — схоже на "
                "недоступну шару, лишаю попередній результат",
                len(hit[1]), len(value), key,
            )
            return
        _cache[key] = (time.monotonic(), value)


def _cached(key: tuple, producer):
    """Спільна механіка кешу для всіх трьох входів сканера."""
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None:
            stamped, value = hit
            if now - stamped < _CACHE_TTL_SECONDS:
                _counters["hit"] += 1
                return value
            _counters["stale"] += 1
            if key not in _refreshing:
                _refreshing.add(key)
                threading.Thread(
                    target=_background_refresh, args=(key, producer), daemon=True
                ).start()
            return value            # протухле краще за очікування
        _counters["miss"] += 1
        # Один обхід на ключ, а не по одному на кожного, хто спитав. Двоє
        # операторів плюс фоновий грійник давали ТРИ паралельні обходи шари по
        # 16 потоків кожен. Той самий прийом уже стоїть у app/order_folder.py
        # (`_scan_lock_for`) — тут його бракувало (аудит 08.09.26).
        lock = _producer_locks.setdefault(key, threading.Lock())

    with lock:
        # Поки чекали на лок, попередник міг уже все порахувати.
        with _cache_lock:
            hit = _cache.get(key)
            if hit is not None and time.monotonic() - hit[0] < _CACHE_TTL_SECONDS:
                _counters["hit"] += 1
                return hit[1]
        value = producer()
        _store(key, value)
        with _cache_lock:
            fresh = _cache.get(key)
        # Якщо запобіжник відкинув наше значення, віддаємо те, що лишилось у
        # кеші: показати трохи старе краще, ніж порожнє.
        return fresh[1] if fresh is not None else value


def _background_refresh(key: tuple, producer) -> None:
    try:
        value = producer()
    except Exception:  # noqa: BLE001 — фонове оновлення не валить застосунок
        logger.exception("Фонове сканування export не вдалося: %s", key)
        return
    finally:
        with _cache_lock:
            _refreshing.discard(key)
    _store(key, value)


def list_export_client_names_cached(root: Path) -> list[str]:
    return _cached(("names", str(root)), lambda: list_export_client_names(root))


def scan_export_client_cached(
    root: Path, client_folder_name: str, not_before: datetime | None = None
) -> list[ExportEntry]:
    return _cached(
        ("client", str(root), client_folder_name, not_before),
        lambda: scan_export_client(root, client_folder_name, not_before),
    )


def scan_export_client_latest_cached(
    root: Path, client_folder_name: str, count: int = 3
) -> list[ExportEntry]:
    return _cached(
        ("client-latest", str(root), client_folder_name, count),
        lambda: scan_export_client_latest(root, client_folder_name, count),
    )


def scan_export_folder_cached(root: Path) -> list[ExportEntry]:
    """Повний обхід із кешем. Лишається для екранів, яким справді потрібне
    все дерево; видача натомість ходить ліниво, по клієнту."""
    return _cached(("full", str(root)), lambda: scan_export_folder(root))


def clear_export_cache() -> None:
    """Скинути кеш — після переміщення файлів у/з export.

    Заразом скидаємо кеш токенів прев'ю листів: він теж описує, ДЕ зараз
    лежать файли, і після переїзду бреше так само. Тримати два скидання в
    одному місці надійніше, ніж пам'ятати про друге на кожному новому виклику
    (імпорт локальний — інакше цикл модулів).
    """
    with _cache_lock:
        _cache.clear()
    from app.order_folder import clear_email_preview_token_cache

    clear_email_preview_token_cache()
