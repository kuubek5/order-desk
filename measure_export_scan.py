"""Виміряти, чи паралельний обхід `export` справді швидший за послідовний.

Питання, на яке відповідає цей скрипт: 80 секунд на екрані видачі — це
ЗАТРИМКА кожної ходки по SMB (тоді потоки допоможуть у рази) чи ПРОПУСКНА
ЗДАТНІСТЬ сховища (тоді потоки не дадуть майже нічого)? Без цієї цифри
вибір між ними — ставка, а не рішення.

Запускати на робочому ПК, де змонтована шара:

    python measure_export_scan.py "<шлях до export>"

Шлях видно в Налаштуваннях → «Шлях до папки export». Скрипт лише ЧИТАЄ
теки: нічого не створює, не змінює й не видаляє.

Два заміри йдуть по РІЗНИХ клієнтах однакової кількості, бо після першого
проходу SMB тримає теку в кеші й повтор був би нечесно швидким.
"""

import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

from app.export_scanner import list_export_client_names, scan_export_client

WORKERS = 16
SAMPLE = 20          # клієнтів на кожен з двох замірів
WINDOW_DAYS = 37     # те саме вікно, що й на видачі (30 днів + тиждень запасу)


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: python measure_export_scan.py <export-root>")
        return 2

    root = Path(sys.argv[1])
    not_before = datetime.now() - timedelta(days=WINDOW_DAYS)

    started = time.monotonic()
    names = list_export_client_names(root)
    names_seconds = time.monotonic() - started
    print(f"level-1 scandir: {len(names)} client folders, {names_seconds:.2f}s")

    if len(names) < SAMPLE * 2:
        print(f"need at least {SAMPLE * 2} client folders, got {len(names)}")
        return 1

    # Дві непересічні вибірки з різних кінців списку.
    seq_names = names[:SAMPLE]
    par_names = names[-SAMPLE:]

    started = time.monotonic()
    seq_entries = sum(len(scan_export_client(root, n, not_before)) for n in seq_names)
    seq_seconds = time.monotonic() - started

    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        par_entries = sum(
            len(r)
            for r in pool.map(lambda n: scan_export_client(root, n, not_before), par_names)
        )
    par_seconds = time.monotonic() - started

    print(f"sequential: {SAMPLE} clients, {seq_entries} entries, {seq_seconds:.2f}s")
    print(f"parallel x{WORKERS}: {SAMPLE} clients, {par_entries} entries, {par_seconds:.2f}s")

    if par_seconds > 0:
        print(f"speedup: {seq_seconds / par_seconds:.1f}x")
    print(
        f"projected for 262 clients: sequential {seq_seconds / SAMPLE * 262:.0f}s, "
        f"parallel {par_seconds / SAMPLE * 262:.0f}s"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
