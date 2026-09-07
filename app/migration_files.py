"""Другий файл переїзду: те, що лежить на диску поруч із базою.

Копія бази (app/backup.py) везе рядки, але свідомо НЕ везе байти файлів —
так вирішено, і шаблони малюють відсутнє фото так само, як прибране
автоприбиранням (див. коментар до `ShiftNoteImage.pruned_at`). Через це
переїзд на новий ПК досі був двоскладовим: файл копії плюс усна інструкція
«ще скопіюй кілька тек». Усна інструкція — найгірший вид інструкції.

Тут — та сама кількість кліків, що й для копії бази: один архів із теками,
які інакше довелося б шукати руками.

Що ВСЕРЕДИНІ (маленьке й незамінне):
  * `shift_images` — фото до записок зміни;
  * `feedback_images` — скріншоти звернень;
  * `machine_portraits` — фотокартки верстатів.

Чого свідомо НЕМАЄ:
  * `mail_attachments` — спул ще не прийнятих листів. Це сотні мегабайтів, і
    ті самі файли лежать у скриньці: після переїзду їх повертає кнопка
    «Скачати ще раз» у картці листа. Класти їх сюди означало б робити архів
    непереносним заради даних, які й так є в двох місцях.
  * `export` — спільна мережева шара, а не файли цього ПК.
  * кадри пічок і верстатів — робочий кеш, живе своїм життям.
  * сама база й `master.key` — база їде окремою копією під паролем, а ключ
    машини на новому ПК свій і має таким лишитись.
"""

from __future__ import annotations

import io
import logging
import zipfile
from dataclasses import dataclass
from pathlib import Path

from app.config import (
    FEEDBACK_IMAGES_PATH,
    MACHINE_PORTRAITS_PATH,
    SHIFT_IMAGES_PATH,
)

logger = logging.getLogger(__name__)

# Ім'я теки в архіві → шлях на цій машині. Ім'я всередині архіву збігається з
# іменем теки в даних, щоб розпакування на новому ПК було буквальним.
BUNDLED_FOLDERS: dict[str, str] = {
    "shift_images": SHIFT_IMAGES_PATH,
    "feedback_images": FEEDBACK_IMAGES_PATH,
    "machine_portraits": MACHINE_PORTRAITS_PATH,
}


# Людські підписи для екрана: у тексті для оператора мають стояти речі, а не
# імена тек на диску.
FOLDER_LABELS: dict[str, str] = {
    "shift_images": "фото до записок зміни",
    "feedback_images": "скріншоти звернень",
    "machine_portraits": "портрети верстатів",
}


@dataclass
class FolderSummary:
    """Скільки чого поїде — щоб адмін бачив це ДО натискання кнопки."""

    name: str
    files: int
    bytes_total: int

    @property
    def label(self) -> str:
        return FOLDER_LABELS.get(self.name, self.name)


def summarize() -> list[FolderSummary]:
    out: list[FolderSummary] = []
    for name, raw_path in BUNDLED_FOLDERS.items():
        root = Path(raw_path)
        files = 0
        size = 0
        try:
            for item in root.rglob("*"):
                if item.is_file():
                    files += 1
                    size += item.stat().st_size
        except OSError:
            # Тека недоступна (шлях перенесли, диск відпав) — це не привід
            # ховати решту: показуємо нулі й пишемо в лог.
            logger.warning("Тека %s недоступна для збору переїзду", raw_path, exc_info=True)
        out.append(FolderSummary(name=name, files=files, bytes_total=size))
    return out


def build_zip() -> bytes:
    """Архів із трьома теками. Порожні теж потрапляють — порожня тека в архіві
    чесно означає «тут нічого не було», а її відсутність читалась би як «збір
    не спрацював»."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, raw_path in BUNDLED_FOLDERS.items():
            root = Path(raw_path)
            archive.writestr(f"{name}/", b"")
            if not root.exists():
                continue
            for item in sorted(root.rglob("*")):
                if not item.is_file():
                    continue
                try:
                    archive.write(item, f"{name}/{item.relative_to(root).as_posix()}")
                except OSError:
                    # Один нечитабельний файл не має валити весь переїзд.
                    logger.warning("Файл %s не потрапив в архів переїзду", item, exc_info=True)
        archive.writestr("ЯК-ПЕРЕНЕСТИ.txt", INSTRUCTIONS.encode("utf-8"))
    return buffer.getvalue()


INSTRUCTIONS = """Перенесення KuubMill на інший компʼютер
=======================================

Потрібні ДВА файли з цієї машини:
  1) резервна копія бази (Налаштування → Резервна копія → «Створити копію»);
  2) цей архів із файлами.

На новому компʼютері:
  1. Встановити KuubMill і один раз запустити — він створить свою теку даних.
  2. Налаштування → Резервна копія → «Відновити з копії»: завантажити файл
     копії й ввести пароль, яким її створювали. Приїдуть роботи, клієнти,
     історія, оператори, налаштування й паролі пічок і верстатів.
  3. Закрити застосунок. Розпакувати цей архів у теку даних так, щоб теки
     shift_images, feedback_images і machine_portraits лягли поруч із файлом
     kuubmill.db. Запустити застосунок знову.
  4. Ліцензія привʼязана до заліза, тож на новому ПК потрібен новий ключ.
  5. Якщо у файлі копії був перелік «нечитабельні налаштування» — саме їх
     доведеться ввести руками: стару машину вони пережити не могли.

Чого в архіві немає навмисно:
  * вкладення ще не прийнятих листів — вони лишились у поштовій скриньці,
    після переїзду їх повертає кнопка «Скачати ще раз» у картці листа;
  * тека export — вона спільна й лежить на мережевій шарі;
  * кадри пічок і верстатів — це робочий кеш, він набереться сам.
"""
