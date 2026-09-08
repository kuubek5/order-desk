"""Друга копія знімка бази — на іншому носії.

Навіщо. Усі три механізми копій (місячна, перед оновленням, перед міграцією)
пишуть у теку `backups` ПОРУЧ із самим файлом бази, тобто на той самий диск.
Це рятує від помилки в програмі — відкотився на вчорашній файл, — але не рятує
від жодної події, що вбиває носій цілком: смерть диска, шифрувальник, крадіжка
системного блока. Тоді зникає і база, і всі копії одночасно, і відновлювати
нема з чого (аудит 08.09.26).

Тут — дзеркало: щойно основний знімок створено, його копія лягає ще й у теку,
вказану адміном (інший диск або мережева шара). Шлях порожній = дзеркало
вимкнене, усе працює як раніше.

Три правила, які тут не можна послабити:

1. **Дзеркало НІКОЛИ не ламає основну копію.** Мережева шара недоступна — це
   нормальний стан, а не помилка застосунку. Тому всі винятки гасяться тут, а
   назовні йде результат, який видно в Налаштуваннях. Основний знімок уже
   створено до того, як ми сюди зайшли.
2. **Копія перевіряється ДО того, як стати останньою.** Файл, що доїхав битим
   по мережі, гірший за відсутній: він виглядає як копія і підводить рівно тоді,
   коли з нього доведеться відновлюватись. Тому `PRAGMA quick_check` на самому
   дзеркалі, і лише потім перейменування на місце.
3. **Ніякого `resolve()` на шляху дзеркала.** Він може вести на мертву мережеву
   шару, а канонізація такого шляху блокує потік на SMB-таймаут. Шлях беремо як
   написано.
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from app.settings_store import get_setting, set_setting

logger = logging.getLogger(__name__)

# Тека, куди дублювати знімки. Порожньо = дзеркало вимкнене.
MIRROR_DIR_KEY = "backup_mirror_dir"
# Коли дзеркало востаннє СПРАЦЮВАЛО. Саме успіх, не спроба: плита розділу має
# зеленіти лише з підтвердженого сигналу (CLAUDE.md §14).
MIRROR_LAST_OK_KEY = "backup_mirror_last_ok"
# Чому не спрацювало минулого разу. Порожньо = останній результат був успішним.
MIRROR_LAST_ERROR_KEY = "backup_mirror_last_error"

# Скільки знімків тримати в дзеркалі. Те саме число, що й локально
# (app/monthly_backup.MAX_SNAPSHOTS), але імпорту звідти немає навмисно: цей
# модуль нічого не знає про місячні знімки й дзеркалить будь-який файл.
MAX_MIRRORED = 12


def get_mirror_dir(session: Session) -> str:
    return (get_setting(session, MIRROR_DIR_KEY) or "").strip()


def mirror_enabled(session: Session) -> bool:
    return bool(get_mirror_dir(session))


def _record(session: Session, *, error: str | None) -> None:
    """Записати результат спроби. Успіх стирає попередню помилку і навпаки —
    інакше плита показувала б обидва стани одночасно."""
    if error is None:
        set_setting(session, MIRROR_LAST_OK_KEY, datetime.now().isoformat(timespec="seconds"))
        set_setting(session, MIRROR_LAST_ERROR_KEY, "")
    else:
        set_setting(session, MIRROR_LAST_ERROR_KEY, error[:500])


def _verify_sqlite(path: Path) -> None:
    """`PRAGMA quick_check` на копії. Кидає, якщо файл не читається як база."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        result = conn.execute("PRAGMA quick_check").fetchone()
    finally:
        conn.close()
    if not result or result[0] != "ok":
        raise RuntimeError(f"копія не проходить перевірку цілісності: {result}")


def _prune(folder: Path, *, keep: int = MAX_MIRRORED) -> None:
    """Прибрати найстаріші дзеркальні файли. Помилка тут не є провалом
    дзеркалення — копія вже на місці, а місце звільниться наступного разу."""
    try:
        files = sorted(
            (p for p in folder.iterdir() if p.is_file() and p.suffix == ".db"),
            key=lambda p: p.name,
        )
    except OSError:
        return
    for path in files[: max(0, len(files) - keep)]:
        try:
            path.unlink()
        except OSError:
            logger.warning("Не вдалося прибрати старе дзеркало %s", path.name)


def mirror_snapshot(session: Session, snapshot: Path, *, subdir: str = "") -> str | None:
    """Скопіювати готовий знімок у теку дзеркала. Повертає текст помилки або None.

    НЕ кидає: викликач уже зробив основну копію, і недоступна шара не повинна
    перетворювати вдалу резервну копію на збій. Результат осідає в налаштуваннях
    і видно в розділі «Резервна копія».
    """
    target_root = get_mirror_dir(session)
    if not target_root:
        return None

    try:
        folder = Path(target_root)
        if subdir:
            folder = folder / subdir
        folder.mkdir(parents=True, exist_ok=True)

        target = folder / snapshot.name
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.unlink(missing_ok=True)

        shutil.copy2(snapshot, tmp)
        _verify_sqlite(tmp)
        tmp.replace(target)

        _prune(folder)
        logger.info("Знімок продубльовано в дзеркало: %s", target)
        _record(session, error=None)
        return None
    except Exception as exc:  # noqa: BLE001 — див. докстрінг: дзеркало не ламає основну копію
        message = f"{type(exc).__name__}: {exc}"
        logger.warning("Дзеркалення знімка %s не вдалося: %s", snapshot.name, message)
        try:
            tmp.unlink(missing_ok=True)  # type: ignore[possibly-undefined]
        except Exception:  # noqa: BLE001 — прибирання найкращим зусиллям
            pass
        _record(session, error=message)
        return message


def mirror_status(session: Session) -> dict:
    """Стан дзеркала для плити налаштувань: чи ввімкнене, коли востаннє вдалось,
    і чим скінчилась остання спроба."""
    return {
        "dir": get_mirror_dir(session),
        "last_ok": (get_setting(session, MIRROR_LAST_OK_KEY) or "").strip(),
        "last_error": (get_setting(session, MIRROR_LAST_ERROR_KEY) or "").strip(),
    }
