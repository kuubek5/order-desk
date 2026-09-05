# -*- coding: utf-8 -*-
"""Навантаження системи: CPU і ОЗП всього ПК та окремо нашого процесу.

На відміну від відсотка верстата (здогад з чужого екрана), тут цифри дає сама
ОС — точні. Читаються через psutil. Стан живе в пам'яті процесу; фоновий
семплер (web.py) оновлює його раз на кілька секунд, а роут /system/load лише
віддає — жодного заміру в момент запиту, бо `cpu_percent(interval=…)`
заблокував би обробник на цей інтервал.

Пороги навантаження (за завантаженням CPU всього ПК): <70 спокій, 70–90
бурштин, >90 червоний. Стрічка на клієнті за цим класом оживає й червоніє.

Немає psutil або збій — стан `ok=False`, стрічка чесно показує «—», а не нуль
(той самий принцип, що на печах: хибне число гірше за жодне)."""

from __future__ import annotations

import logging
import os
import threading

try:
    import psutil
except Exception:  # noqa: BLE001 — на будь-якій платформі без psutil не падаємо
    psutil = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

WARN_AT = 70
HOT_AT = 90

_state: dict = {"ok": False}
_lock = threading.Lock()
_proc = None
if psutil is not None:
    try:
        _proc = psutil.Process(os.getpid())
    except Exception:  # noqa: BLE001
        _proc = None


def _level(pc_cpu: int) -> tuple[str, bool, bool]:
    """(клас, рух, пульс) за завантаженням CPU ПК. Рух — з бурштину, пульс —
    лише на червоному (перегрузі), як затвердив власник."""
    if pc_cpu > HOT_AT:
        return "hot", True, True
    if pc_cpu >= WARN_AT:
        return "mid", True, False
    return "ok", False, False


def sample() -> None:
    """Один замір → у стан. Викликається фоновим воркером; перший виклик
    `cpu_percent(interval=None)` віддає 0 (нема попереднього зрізу), далі —
    справжню дельту, тому семплер має тікати регулярно."""
    global _state
    if psutil is None:
        with _lock:
            _state = {"ok": False}
        return
    try:
        pc_cpu = psutil.cpu_percent(interval=None)  # від попереднього виклику
        vm = psutil.virtual_memory()
        ncpu = psutil.cpu_count() or 1

        crm_cpu_raw = 0.0
        crm_rss = 0
        if _proc is not None:
            procs = [_proc]
            try:
                procs += _proc.children(recursive=True)
            except Exception:  # noqa: BLE001 — діти могли завершитись
                pass
            for p in procs:
                try:
                    crm_cpu_raw += p.cpu_percent(interval=None)
                    crm_rss += p.memory_info().rss
                except Exception:  # noqa: BLE001 — процес зник / нема доступу
                    continue
        # cpu_percent процесу сумується по ядрах (може бути >100%); нормуємо
        # до 0–100, щоб не показувати «140%», як домовились.
        crm_cpu = min(100.0, crm_cpu_raw / ncpu)

        pc_cpu_i = int(round(pc_cpu))
        level, flow, pulse = _level(pc_cpu_i)
        with _lock:
            _state = {
                "ok": True,
                "pc_cpu": pc_cpu_i,
                "pc_ram_pct": int(round(vm.percent)),
                "pc_ram_used_gb": round(vm.used / 1073741824, 1),
                "pc_ram_total_gb": round(vm.total / 1073741824, 1),
                "crm_cpu": int(round(crm_cpu)),
                "crm_ram_mb": int(round(crm_rss / 1048576)),
                "level": level,
                "flow": flow,
                "pulse": pulse,
            }
    except Exception:  # noqa: BLE001 — семплер не має валити воркер
        logger.debug("Замір навантаження не вдався", exc_info=True)
        with _lock:
            _state = {"ok": False}


def snapshot() -> dict:
    with _lock:
        return dict(_state)


def reset_for_tests() -> None:
    global _state
    with _lock:
        _state = {"ok": False}
