"""Теки програм фрезерування верстатів — ЛИШЕ читання.

Власник 12.11.26: Sum3D кладе прораховану програму `.iso` у два місця. Для
старих верстатів — у розшарену теку на ПК з CRM, звідки верстат сам забирає
файл скриптом. Для нових — одразу в розшарену теку самого верстата. Шлях до
теки кожного верстата задається в Налаштуваннях → Верстати
(`Machine.program_folder`).

**Тут немає жодної операції запису** — ні видалення, ні переносу, ні
створення тек чи файлів. Це вимога власника («щоб випадково нічого зайвого не
видалити») і її стереже `tests/test_program_folders.py`: модуль, у якому
зʼявиться `unlink`, `rename`, `rmtree`, `mkdir`, `write_*` чи `open(..., "w")`,
не пройде тест.

Читаємо лише імена, розмір і час зміни, на дві теки вглиб, з межею на
кількість файлів і час — тека на мережевій шарі буває повільною.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from app.fs_probe import ABSENT, PRESENT, UNREACHABLE, path_state

PROGRAM_EXTS = (".iso", ".nc")
MAX_FILES = 5000
MAX_DEPTH = 2
TIME_BUDGET_S = 8.0


@dataclass(frozen=True)
class ProgramFile:
    name: str
    rel_dir: str
    size: int
    modified: datetime


@dataclass
class FolderScan:
    path: str
    state: str = ABSENT  # PRESENT / ABSENT / UNREACHABLE
    files: list[ProgramFile] = field(default_factory=list)
    other_files: int = 0
    truncated: bool = False
    error: str = ""
    elapsed_ms: int = 0

    @property
    def ok(self) -> bool:
        return self.state == PRESENT and not self.error

    def newest(self, limit: int) -> list[ProgramFile]:
        return sorted(self.files, key=lambda f: f.modified, reverse=True)[:limit]

    def summary(self) -> str:
        """Один рядок для екрана налаштувань."""
        if not self.path:
            return "шлях не задано"
        if self.state == UNREACHABLE:
            return "тека недоступна (мережа чи права)"
        if self.state != PRESENT:
            return "теки не знайдено"
        if self.error:
            return f"не прочитано: {self.error}"
        if not self.files:
            return f"програм немає (інших файлів: {self.other_files})"
        last = self.newest(1)[0]
        more = "+" if self.truncated else ""
        return (f"програм: {len(self.files)}{more}, найновіша {last.name} "
                f"від {last.modified:%d.%m %H:%M}")


def scan(path: Optional[str]) -> FolderScan:
    """Перелік програм у теці (і на `MAX_DEPTH` рівнів нижче). Нічого не змінює."""
    raw = (path or "").strip()
    result = FolderScan(path=raw)
    if not raw:
        return result
    started = time.monotonic()
    result.state = path_state(raw)
    if result.state != PRESENT:
        return result
    stack: list[tuple[str, str, int]] = [(raw, "", 0)]
    try:
        while stack:
            folder, rel, depth = stack.pop()
            if time.monotonic() - started > TIME_BUDGET_S or len(result.files) >= MAX_FILES:
                result.truncated = True
                break
            with os.scandir(folder) as entries:
                for entry in entries:
                    if entry.is_dir(follow_symlinks=False):
                        if depth < MAX_DEPTH:
                            stack.append((entry.path, f"{rel}{entry.name}/", depth + 1))
                        continue
                    if not entry.name.lower().endswith(PROGRAM_EXTS):
                        result.other_files += 1
                        continue
                    info = entry.stat()
                    result.files.append(ProgramFile(
                        name=entry.name, rel_dir=rel, size=info.st_size,
                        modified=datetime.fromtimestamp(info.st_mtime),
                    ))
    except OSError as exc:
        result.error = f"{exc.__class__.__name__}: {exc.strerror or exc}"
    result.elapsed_ms = int((time.monotonic() - started) * 1000)
    return result


__all__ = ["FolderScan", "ProgramFile", "scan", "PRESENT", "ABSENT", "UNREACHABLE"]
