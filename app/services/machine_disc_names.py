"""Повна назва програми верстата нового покоління — не лише з літер на екрані.

Власник 23.11.26: «це вже дістало, можемо один раз налаштувати і щоб працювало
стабільно». Софт CORiTEC пише назву тільки на екрані JOBS, і дотепер вона бралася
звідти літера за літерою за правилом «цілком або ніяк»: одне нове накреслення (Z на
відстані 21 при межі 20, хоч наступна літера аж на 102) — і замість диска лишалась
дата. Тепер назва, у якій з екрана є лише дата й час, доповнюється з двох
незалежних джерел:

  1. сам файл програми: Sum3D кладе `.iso` у мережеву теку `Jobs` верстата, і слід
     (`program_tracks`) пам'ятає його повне ім'я;
  2. перелік дисків теки CAM: прочитане з екрана мусить збігтися рівно з одним
     справжнім диском (`machine_newgen_job.name_by_discs`).

Обидва дають або точну назву, або нічого. На Sum3D ID, дату й прив'язку до черги
не впливають ніяк.
"""

from __future__ import annotations

import dataclasses
import logging
import threading
import time
from typing import Callable, Optional

from PIL import Image

from app import log_throttle
from app.machine_ocr import MillingProgram

logger = logging.getLogger(__name__)

#: Перелік дисків перечитується у фоні: 19 тисяч файлів — це секунда на шарі,
#: а опитування верстатів іде по черзі й чекати не може.
DISCS_TTL_SECONDS = 600.0
#: Не вийшло — пробуємо знову за стільки: новий диск міг щойно з'явитись.
MISS_TTL_SECONDS = 30.0
_CACHE_MAX = 200

_lock = threading.Lock()
_stems: list[str] = []
_stems_root = ""
_stems_at = 0.0
_loading = False
_found: dict[tuple[str, str], str] = {}
_missed: dict[tuple[str, str], float] = {}


def reset_for_tests() -> None:
    global _stems, _stems_root, _stems_at, _loading
    with _lock:
        _stems, _stems_root, _stems_at, _loading = [], "", 0.0, False
        _found.clear()
        _missed.clear()


def _load(root: str) -> None:
    global _stems, _stems_root, _stems_at, _loading
    stems: list[str] = []
    try:
        from app.services.disc_suggest import disc_files

        stems = [d.stem for d in disc_files(root)]
    except Exception:  # noqa: BLE001 — перелік дисків не валить опитування верстатів
        if log_throttle.due("machine-disc-names:load") is not None:
            logger.exception("Перелік дисків для назв програм не прочитано")
    with _lock:
        if stems or root != _stems_root:
            _stems = stems
        _stems_root, _stems_at, _loading = root, time.monotonic(), False


def disc_stems(root: Optional[str]) -> list[str]:
    """Назви файлів дисків з пам'яті; застаріло — перечитуємо у фоні."""
    global _loading
    root = (root or "").strip()
    if not root:
        return []
    with _lock:
        known = list(_stems) if root == _stems_root else []
        fresh = root == _stems_root and _stems_at and time.monotonic() - _stems_at < DISCS_TTL_SECONDS
        start = not fresh and not _loading
        if start:
            _loading = True
    if start:
        threading.Thread(target=_load, args=(root,), name="kuubmill-disc-names", daemon=True).start()
    return known


def complete(program: MillingProgram, key: str, frame: Image.Image, root: Optional[str],
             *, machine: str = "", now: Callable[[], float] = time.monotonic) -> MillingProgram:
    """Програма з екрана, у назві якої лише дата й час, — з повною назвою, коли її
    знає файл програми або перелік дисків. Інакше та сама програма."""
    from app.services import program_tracks, sum3d_names

    if not program.sum3d_id or sum3d_names.bare_parts(program.iso_name) is None:
        return program
    tail = f"{program.date}_{program.sum3d_id}"
    slot = (key, tail)
    with _lock:
        known = _found.get(slot)
        waiting = now() < _missed.get(slot, 0.0)
    if known is None and not waiting:
        known, source = _resolve(program, tail, frame, root, program_tracks.program_of(program.iso_name))
        with _lock:
            if len(_found) > _CACHE_MAX or len(_missed) > _CACHE_MAX:
                _found.clear()
                _missed.clear()
            if known is None:
                _missed[slot] = now() + MISS_TTL_SECONDS
            else:
                _found[slot] = known
        if known is not None:
            logger.info("Верстат %s: повна назва програми %s — %s", machine or key, known, source)
    return dataclasses.replace(program, iso_name=known) if known else program


def _resolve(program: MillingProgram, tail: str, frame: Image.Image, root: Optional[str],
             traced: Optional[tuple[str, str]]) -> tuple[Optional[str], str]:
    from app.machine_newgen_job import name_by_discs

    if traced is not None and traced[0].lower().endswith(f"_{tail}.iso"):
        return traced[0], "з файлу програми"
    stems = disc_stems(root)
    if not stems:
        return None, ""
    try:
        head, _why = name_by_discs(frame, stems)
    except Exception:  # noqa: BLE001 — добір назви не валить опитування
        if log_throttle.due("machine-disc-names:match") is not None:
            logger.exception("Назву програми %s за переліком дисків не звірено", program.iso_name)
        return None, ""
    return (f"{head}_{tail}.iso", "з екрана, звірено з переліком дисків") if head else (None, "")
