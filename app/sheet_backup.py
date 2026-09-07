"""Сирі знімки вкладок Google-таблиці (CSV), для аварійного відновлення.

CLAUDE.md §7/§14 і рішення власника 05.09.26: адміністратори періодично чистять
старі рядки/вкладки з Google, щоб таблиця не роздувалась. База CRM їх не втрачає
(синк архівує, не видаляє — див. app/sync.py), але ВІДНОВИТИ саму Google-таблицю
з бази нічим: Order тримає розібрані поля, не рядок байт-у-байт. Цей модуль
закриває саме це — тримає дослівну CSV-копію кожної **датованої** вкладки, яку
можна залити назад у Google (File → Import → CSV).

Чому CSV, а не окремий формат: та сама копія одночасно є і дослівним архівом, і
готовим до заливу файлом — нема кроку «експорт», знімок і Є експортом.

ПРАВИЛО «не пусте» (вимога власника): знімок вкладки без робочих рядків не
пишеться взагалі. Вкладка дня наповнюється поступово (робота йде до ночі,
видача вранці наступного дня), тому свіжі дні ПЕРЕЗНІМАЮТЬСЯ щопрохід і копія
збігається до найповнішого баченого стану; старий, уже незмінний день не
перечитується — економія квоти Google (60 запитів/хв, читання вкладки коштує
кілька викликів).

Знімки лежать поряд з місячними бекапами БД — `<db dir>/backups/sheets/`, ім'я
файлу в ISO-даті (`2026-07-22.csv`), щоб лексичний порядок = хронологічний і
місяць групувався тривіально (`2026-07`). Маніфест `manifest.json` тримає
метадані (оригінальна назва вкладки, коли знято, скільки рядків) і фіксує, коли
вкладка ЗНИКЛА з Google — саме той сигнал, заради якого існує страховка.

Читання — ОДИН `get_all_values` на вкладку. Чанковий читач писався проти
«проксі обрізає велику відповідь», але той діагноз виявився хибним (роботи
зникали через сірі СЛМ-рядки, фікс 0.6.5), а ціна лишалась: десятки запитів на
вкладку × десятки вкладок палили квоту читань і давали 429 живому синку.
Запобіжник від обрізаного читання нікуди не дівся — він нижче, у звірці з
попередньою копією за кількістю рядків.

Прохід іде ПІД тим самим локом, що й синк таблиці (`sheet_sync_service`), і
пропускається, коли квота читань уже на межі: страховка не має ставати
причиною аварії, від якої страхує.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import threading
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session

from app.business_day import business_today
from app.parser import HEADER_ROWS
from app.settings_store import get_google_sheet_id

logger = logging.getLogger(__name__)

SHEETS_SUBDIR = "sheets"
MANIFEST_NAME = "manifest.json"

# Скільки останніх днів перезнімати щопрохід. Вкладка дня живе й змінюється не
# один день (робота до ночі, видача наступного ранку, дописування «на швидку»),
# тож копія має наздоганяти. 4 дні покривають і те, що лаба часто працює на
# день-два позаду поточної дати.
RESNAPSHOT_RECENT_DAYS = 4

# Серіалізуємо проходи: авто-воркер і ручна кнопка адміна можуть накластися
# (різні потоки), а обидва пишуть ті самі файли й маніфест.
_lock = threading.Lock()


def sheets_backup_dir(db_path: str | Path) -> Path:
    """Тека знімків вкладок — поряд з базою і місячними бекапами, потрапляє в
    будь-яку копію папки, яку адмін уже робить."""
    return Path(db_path).expanduser().resolve().parent / "backups" / SHEETS_SUBDIR


def _parse_tab_date(title: str) -> Optional[date]:
    """Назва датованої вкладки `дд.мм.рр` → date, або None для недатованих
    (легенда, шаблони) — їх авто-знімок свідомо не чіпає."""
    try:
        return datetime.strptime(title.strip(), "%d.%m.%y").date()
    except (ValueError, AttributeError):
        return None


def _row_has_work(row: list[str]) -> bool:
    """Робочий рядок несе наряд (кол. 1) або вид/ім'я клієнта (кол. 4) —
    той самий критерій, що і в парсері черги."""
    return (len(row) > 1 and row[1].strip() != "") or (len(row) > 4 and row[4].strip() != "")


def _tab_has_data(rows: list[list[str]]) -> bool:
    """Чи є у вкладці бодай один робочий рядок під заголовками. Порожню
    (лише шапка / зовсім чисту) вкладку не зберігаємо — вимога власника."""
    return any(_row_has_work(r) for r in rows[HEADER_ROWS:])


def _trim_trailing_empty(rows: list[list[str]]) -> list[list[str]]:
    """Прибрати хвіст цілком порожніх рядків, щоб CSV був охайний. Порожній
    рядок = жодної непорожньої клітинки."""
    end = len(rows)
    while end > 0 and not any((c or "").strip() for c in rows[end - 1]):
        end -= 1
    return rows[:end]


def _rows_to_csv_bytes(rows: list[list[str]]) -> bytes:
    """2D-масив рядків → CSV у utf-8-sig. BOM, бо Excel інакше показує кирилицю
    мохібейком; Google Sheets import BOM ігнорує. csv.writer сам екранує коми
    й переноси всередині клітинки."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    for row in rows:
        writer.writerow(row)
    return buf.getvalue().encode("utf-8-sig")


def _iso(tab_date: date) -> str:
    return tab_date.isoformat()


def _load_manifest(folder: Path) -> dict:
    path = folder / MANIFEST_NAME
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def _save_manifest(folder: Path, manifest: dict) -> None:
    """Атомарний запис маніфесту (tmp + replace), щоб збій посеред запису не
    лишив півфайлу, який зламає наступне читання."""
    path = folder / MANIFEST_NAME
    tmp = path.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2, sort_keys=True)
    tmp.replace(path)


@dataclass
class SnapshotResult:
    written: int = 0          # вкладок записано/оновлено цього проходу
    skipped_empty: int = 0    # пропущено як порожні (нема робочих рядків)
    skipped_cached: int = 0   # старий незмінний день — не перечитувався
    disappeared: int = 0      # вкладок, які зникли з Google цього проходу
    held_shrink: int = 0      # копій НЕ перезаписано через різке падіння рядків
    failed: int = 0           # вкладок, які не вдалося прочитати
    deferred: int = 0         # вкладок, не прочитаних цього проходу через квоту
    error: Optional[str] = None   # фатальна причина (не налаштовано / нема доступу)
    tabs: list[str] = field(default_factory=list)  # оновлені (для логів/тостів)

    @property
    def ok(self) -> bool:
        return self.error is None


def snapshot_all_tabs(
    db: Session, db_path: str | Path, *, force: bool = False, today: date | None = None
) -> SnapshotResult:
    """Один прохід знімання. Пише CSV для кожної датованої вкладки з даними.

    force=True (ручна кнопка адміна) перечитує ВСІ датовані вкладки, навіть уже
    зняті старі дні — коли оператор свідомо хоче свіжу повну копію. Авто-прохід
    (force=False) перечитує лише останні RESNAPSHOT_RECENT_DAYS, решту бере з
    наявних файлів — щоб не палити квоту Google на незмінних днях.

    Ніколи не кидає — фатальну причину повертає в result.error, аби фоновий
    воркер лишався живим."""
    result = SnapshotResult()

    if not (get_google_sheet_id(db) or "").strip():
        result.error = "Google Sheet ID не вказано в налаштуваннях."
        return result

    # Імпорт тут, а не вгорі: тягне gspread/google-auth, і тримати модуль
    # знімків незалежним від них до першого реального проходу дешевше.
    from app.sheets import call_with_retry, open_spreadsheet, quota_is_tight
    from app.sheet_sync_service import _sync_lock

    # Знімок — страховка, і вона не має заважати живому синку: беремо той самий
    # лок без очікування. Зайнято — просто наступного разу (авто-прохід ходить
    # регулярно, а ручна кнопка чесно скаже «синк зараз працює»).
    if not _sync_lock.acquire(blocking=False):
        result.error = "Синхронізація таблиці зараз працює — знімок відкладено."
        return result

    try:
        try:
            spreadsheet = open_spreadsheet(db)
            worksheets = call_with_retry(spreadsheet.worksheets)
        except Exception as exc:  # noqa: BLE001 — будь-який збій доступу = не наша аварія
            result.error = f"Немає доступу до таблиці: {exc}"
            logger.warning("Знімок вкладок: не вдалося відкрити таблицю: %s", exc)
            return result

        today = today or business_today()
        recent_cutoff = today - _RESNAPSHOT_DELTA

        with _lock:
            folder = sheets_backup_dir(db_path)
            folder.mkdir(parents=True, exist_ok=True)
            manifest = _load_manifest(folder)

            present_isos: set[str] = set()

            for ws in worksheets:
                title = ws.title
                tab_date = _parse_tab_date(title)
                if tab_date is None:
                    continue  # недатована вкладка — поза межами добово-місячного архіву
                iso = _iso(tab_date)
                present_isos.add(iso)
                target = folder / f"{iso}.csv"

                is_recent = tab_date >= recent_cutoff
                if target.exists() and not is_recent and not force:
                    result.skipped_cached += 1
                    continue  # старий день уже знято й він незмінний — не читаємо

                # Квота читань спільна з синком. Дійшли до межі — цю вкладку
                # пропускаємо: недознятий день дознімається наступним проходом, а
                # 429 у синку коштує оператору живої черги.
                if quota_is_tight():
                    result.deferred += 1
                    continue

                try:
                    rows = call_with_retry(ws.get_all_values)
                except Exception as exc:  # noqa: BLE001
                    result.failed += 1
                    logger.warning("Знімок вкладки %s: помилка читання: %s", title, exc)
                    continue

                if not _tab_has_data(rows):
                    result.skipped_empty += 1
                    continue  # порожню вкладку не зберігаємо (вимога власника)

                rows = _trim_trailing_empty(rows)
                data_rows = sum(1 for r in rows[HEADER_ROWS:] if _row_has_work(r))

                # ЗАПОБІЖНИК ВІД ОБРІЗАНОГО ЧИТАННЯ. TLS-проксі лаби іноді віддає
                # КОРОТКУ, але валідну відповідь (100 рядків зі 120 — бойовий випадок,
                # бойовий випадок 30.08.26). Рядки є, тож «порожньо» не ловить,
                # і повна копія перезаписалась би обрізаною. Той самий принцип, що
                # проти масового зникнення в синку: РІЗКЕ падіння (>5 рядків І >25%)
                # проти вже збереженої копії — майже завжди погане читання, а не
                # реальне видалення. Тримаємо стару, повнішу копію й голосно логуємо.
                # Дрібні зміни (±кілька рядків) пишуться нормально; force теж не
                # обходить це — обрізаний ручний знімок так само не має псувати копію.
                prior = manifest.get(iso)
                prior_rows = prior.get("rows") if isinstance(prior, dict) else None
                if (
                    target.exists()
                    and isinstance(prior_rows, int)
                    and data_rows < prior_rows
                    and (prior_rows - data_rows) > _SHRINK_MIN_DROP
                    and data_rows < _SHRINK_RATIO * prior_rows
                ):
                    result.held_shrink += 1
                    logger.warning(
                        "Знімок вкладки %s: нове читання %d рядків проти збережених %d "
                        "(різке падіння — схоже на обрізане читання), копію НЕ перезаписано",
                        title, data_rows, prior_rows,
                    )
                    continue

                _write_csv_atomic(target, _rows_to_csv_bytes(rows))

                manifest[iso] = {
                    "tab": title,
                    "taken_at": datetime.now().isoformat(timespec="seconds"),
                    "rows": data_rows,
                    "last_present": datetime.now().isoformat(timespec="seconds"),
                }
                manifest[iso].pop("disappeared_at", None)
                result.written += 1
                result.tabs.append(title)

            # Вкладки, що були в маніфесті, а тепер відсутні в Google → зафіксувати
            # зникнення (знімок лишається — це і є страховка). Present-set беремо з
            # дешевого worksheets(), без жодного зайвого читання вкладок.
            now_iso = datetime.now().isoformat(timespec="seconds")
            for iso, meta in manifest.items():
                if not isinstance(meta, dict):
                    continue
                if iso in present_isos:
                    continue
                if not (folder / f"{iso}.csv").exists():
                    continue  # немає копії — нічого фіксувати
                if not meta.get("disappeared_at"):
                    meta["disappeared_at"] = now_iso
                    result.disappeared += 1

            _save_manifest(folder, manifest)

        if result.written or result.disappeared or result.held_shrink:
            logger.info(
                "Знімок вкладок: записано %d, порожніх пропущено %d, кеш %d, "
                "зникло %d, притримано (обрізане?) %d, помилок %d",
                result.written, result.skipped_empty, result.skipped_cached,
                result.disappeared, result.held_shrink, result.failed,
            )
        return result
    finally:
        # Лок віддаємо ЗАВЖДИ: інакше один збій знімка зупинив би синк
        # таблиці назавжди — страховка вбила б те, що страхує.
        _sync_lock.release()


from datetime import timedelta as _timedelta  # noqa: E402 — поряд з константою нижче

_RESNAPSHOT_DELTA = _timedelta(days=RESNAPSHOT_RECENT_DAYS)

# Поріг запобіжника від обрізаного читання: копію не перезаписуємо, якщо нове
# читання втратило БІЛЬШЕ за _SHRINK_MIN_DROP рядків І впало нижче _SHRINK_RATIO
# від збереженого. Дзеркалить поріг масового зникнення в app/sync.py (>5, >25%).
_SHRINK_MIN_DROP = 5
_SHRINK_RATIO = 0.75


def _write_csv_atomic(target: Path, content: bytes) -> None:
    tmp = target.with_suffix(".csv.tmp")
    with tmp.open("wb") as fh:
        fh.write(content)
    tmp.replace(target)


# ── Читання для UI / завантаження ──────────────────────────────────────────

@dataclass
class SnapshotInfo:
    iso: str            # 2026-07-22
    filename: str       # 2026-07-22.csv
    tab: str            # 22.07.26 (оригінальна назва вкладки)
    day_label: str      # 22.07.2026
    month_ym: str       # 2026-07
    size_kb: float
    rows: Optional[int]
    taken_at: Optional[str]
    disappeared_at: Optional[str]


def list_snapshots(db_path: str | Path) -> list[SnapshotInfo]:
    """Наявні CSV-знімки, найновіший день першим. Метадані беруться з маніфесту,
    а розмір — з файлу; знімок без запису в маніфесті (напр. після ручного
    копіювання теки) все одно показується, лише без rows/taken_at."""
    folder = sheets_backup_dir(db_path)
    try:
        files = [p for p in folder.iterdir() if p.is_file() and p.suffix == ".csv"]
    except OSError:
        return []
    manifest = _load_manifest(folder)

    out: list[SnapshotInfo] = []
    for path in files:
        iso = path.stem
        tab_date = _parse_iso(iso)
        if tab_date is None:
            continue
        meta = manifest.get(iso) if isinstance(manifest.get(iso), dict) else {}
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        out.append(
            SnapshotInfo(
                iso=iso,
                filename=path.name,
                tab=meta.get("tab") or tab_date.strftime("%d.%m.%y"),
                day_label=tab_date.strftime("%d.%m.%Y"),
                month_ym=f"{tab_date.year:04d}-{tab_date.month:02d}",
                size_kb=round(size / 1024, 1),
                rows=meta.get("rows"),
                taken_at=meta.get("taken_at"),
                disappeared_at=meta.get("disappeared_at"),
            )
        )
    out.sort(key=lambda s: s.iso, reverse=True)
    return out


def _parse_iso(value: str) -> Optional[date]:
    try:
        return date.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def _safe_snapshot_path(db_path: str | Path, filename: str) -> Optional[Path]:
    """Перевірений шлях до одного CSV-знімка всередині теки знімків. None, якщо
    ім'я намагається вийти за межі теки або файлу нема. Захист від traversal:
    приймаємо лише голе ім'я `YYYY-MM-DD.csv`."""
    if _parse_iso(filename[:-4]) is None or not filename.endswith(".csv"):
        return None
    folder = sheets_backup_dir(db_path)
    candidate = (folder / filename).resolve()
    try:
        candidate.relative_to(folder.resolve())
    except ValueError:
        return None
    if not candidate.is_file():
        return None
    return candidate


def read_snapshot_bytes(db_path: str | Path, filename: str) -> Optional[bytes]:
    path = _safe_snapshot_path(db_path, filename)
    if path is None:
        return None
    try:
        return path.read_bytes()
    except OSError:
        return None


def build_zip(db_path: str | Path, *, month: Optional[str] = None) -> tuple[bytes, int]:
    """ZIP усіх знімків (month=None) або одного місяця (`YYYY-MM`). Повертає
    (байти, кількість файлів). Файли всередині ZIP іменовані як вкладка Google
    (`22.07.26.csv`), щоб заливати назад під тією ж назвою."""
    snapshots = list_snapshots(db_path)
    if month:
        snapshots = [s for s in snapshots if s.month_ym == month]

    buf = io.BytesIO()
    count = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for snap in snapshots:
            data = read_snapshot_bytes(db_path, snap.filename)
            if data is None:
                continue
            zf.writestr(f"{snap.tab}.csv", data)
            count += 1
    return buf.getvalue(), count
