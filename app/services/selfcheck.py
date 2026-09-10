"""Самоперевірка: ті самі проби, що за кнопками «Перевірити», одним набором.

Навіщо окремий модуль. Проби жили всередині роута
`POST /settings/selfcheck` як вкладені функції, і скористатись ними більше
ніде було не можна. Щойно знадобився другий споживач — «Звіт для розробника»,
який Рома надсилає раз на тиждень, — виявилось, що найцінніше в звіті (чи є
доступ до Google, чи відповідає пошта, чи пишеться в теку export) недосяжне,
бо замкнене в генераторі HTTP-відповіді.

Контракт лишився той самий: нічого не змінюємо, жоден секрет не виходить —
тільки «задано / не налаштовано» і ті самі класифіковані повідомлення, що
показують окремі кнопки. Зелене тут = зелене там, бо код один.

`check_path` приходить ПАРАМЕТРОМ, а не імпортом: перевірка шляху живе в
роутері налаштувань, а сервіс, який імпортує роутер, — це коло і порушення
правила «роутер бачить Request, сервіс не знає про HTTP» (CLAUDE.md §14).
"""

from __future__ import annotations

import logging
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

from sqlalchemy.orm import Session

from app.config import DB_PATH
from app.db import SessionLocal
from app.monthly_backup import list_snapshots
from app.services.config_state import imap_configured, sheets_configured
from app.settings_store import (
    get_export_folder_path,
    get_imap_login,
    get_imap_password,
    get_technician_files_path,
)
from app.sheets import open_spreadsheet
from app.update_check import get_known_update


logger = logging.getLogger(__name__)

# Стеля на ОДНУ пробу. Мовчазна мережева шара або зайнята Google тримають
# зʼєднання довго, а перевірка мусить закінчитись сама — інакше екран
# «перевіряю…» висить, і людина не знає, чи він живий.
STEP_DEADLINE_SECONDS = 20


@dataclass(frozen=True)
class Step:
    """Одна проба: ключ для UI, людська назва, і що саме зробити."""

    key: str
    name: str
    run: Callable[[], tuple[bool, bool, str]]


@dataclass(frozen=True)
class Result:
    """Наслідок проби. `warn` — «працює, але подивись»; це НЕ помилка."""

    key: str
    name: str
    ok: bool
    warn: bool
    detail: str
    ms: int


def build_steps(db: Session, *, check_path, probe_imap) -> list[Step]:
    """Перелік проб у порядку показу.

    ВСІ значення конфігурації читаються ТУТ, наперед: проби виконуються потім
    (у стрімі роута сесія запиту вже закрита), і торкатись `db` вони не мають
    права. Виняток — `_sheets`, якому потрібна жива сесія; він відкриває
    власну коротку й сам її закриває.
    """
    sheets_ready = sheets_configured(db)
    imap_ready = imap_configured(db)
    imap_login = get_imap_login(db)
    imap_password = get_imap_password(db)
    export_path = get_export_folder_path(db)
    technician_path = get_technician_files_path(db)

    def _sheets() -> tuple[bool, bool, str]:
        if not sheets_ready:
            return False, False, "не налаштовано — ID або JSON-ключ порожні"
        # open_spreadsheet() без сесії падає назад на sheet id з оточення, а не
        # на збережений через екран налаштувань — тому потрібна справжня сесія.
        probe_db = SessionLocal()
        try:
            spreadsheet = open_spreadsheet(db=probe_db)
            n = len(spreadsheet.worksheets())
        finally:
            probe_db.close()
        return True, False, f"доступ підтверджено · {n} вкладок"

    def _imap() -> tuple[bool, bool, str]:
        if not imap_ready:
            return False, False, "не налаштовано — логін або пароль порожні"
        res = probe_imap(imap_login, imap_password)
        return res["state"] == "success", False, res["message"]

    def _folder(path_str, *, needs_write: bool) -> tuple[bool, bool, str]:
        """Та сама проба, що й за кнопкою «Перевірити» біля поля шляху.

        Тут була власна коротка перевірка (`exists` + `is_dir`), і рядок
        «Папка export доступна на запис» ставав зеленим, жодного разу нічого
        туди не записавши. Права на мережеву шару видно лише спробою.

        Для теки робіт техніків запис не потрібен — звідти лише читають.
        """
        p = (path_str or "").strip()
        if not p:
            return False, False, "шлях не задано"
        result = check_path(p, write_probe=needs_write)
        state, message = result["state"], result["message"]
        if state == "success":
            return True, False, message
        if state == "warning":
            # Тека є, але писати нікуди. Для export це зламана функція, а не
            # попередження: саме туди їдуть вкладення прийнятих листів.
            return not needs_write, True, message
        return False, False, message

    def _disk() -> tuple[bool, bool, str]:
        usage = shutil.disk_usage(Path(DB_PATH).parent)
        free_gb = usage.free / (1024 ** 3)
        if free_gb < 2:
            return False, False, f"вільно лише {free_gb:.1f} ГБ (потрібно ≥2 ГБ)"
        return True, free_gb < 10, f"вільно {free_gb:.1f} ГБ"

    def _backup() -> tuple[bool, bool, str]:
        snaps = list_snapshots(DB_PATH)
        if not snaps:
            return True, True, "жодної автоматичної копії ще немає"
        newest = max(s.stat().st_mtime for s in snaps)
        age_days = (time.time() - newest) / 86400
        return True, age_days > 40, f"остання копія {age_days:.0f} дн. тому"

    def _update() -> tuple[bool, bool, str]:
        rel = get_known_update()
        if rel:
            return True, True, f"доступне оновлення v{rel.version}"
        return True, False, "встановлена версія найновіша"

    return [
        Step("sheets", "Доступ до Google Таблиці", _sheets),
        Step("imap", "IMAP-зʼєднання зі скринькою", _imap),
        Step("export", "Папка export доступна на запис",
             lambda: _folder(export_path, needs_write=True)),
        Step("technician", "Папка робіт техніків",
             lambda: _folder(technician_path, needs_write=False)),
        Step("disk", "Місце на диску (потрібно ≥2 ГБ)", _disk),
        Step("backup", "Резервна копія свіжа", _backup),
        Step("update", "Наявність оновлення", _update),
    ]


def run_steps(steps: list[Step]) -> Iterator[Result]:
    """Виконати проби по черзі, віддаючи наслідок щойно він є.

    Генератор, а не список: стрім у роуті показує рядок у мить, коли проба
    справді завершилась. Пул на один демонський потік — покинута проба не
    сміє тримати вихід із застосунку; зависла забирає свій пул із собою, і
    решта дістає новий, а не чергу за нею.
    """
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="selfcheck")
    try:
        for step in steps:
            started = time.perf_counter()
            try:
                ok, warn, detail = pool.submit(step.run).result(
                    timeout=STEP_DEADLINE_SECONDS
                )
            except FutureTimeout:
                logger.warning("selfcheck step %s exceeded deadline", step.key)
                ok, warn, detail = False, False, (
                    f"немає відповіді понад {STEP_DEADLINE_SECONDS} с — перевірку скасовано"
                )
                pool.shutdown(wait=False)
                pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="selfcheck")
            except Exception:  # noqa: BLE001 — проба не сміє валити перевірку
                logger.warning("selfcheck step %s failed", step.key, exc_info=True)
                ok, warn, detail = False, False, "перевірка не виконалась"
            yield Result(
                key=step.key,
                name=step.name,
                ok=ok,
                warn=warn,
                detail=detail,
                ms=int((time.perf_counter() - started) * 1000),
            )
    finally:
        pool.shutdown(wait=False)
