"""Зібрати калібрувальні кадри верстата — по одному на кожен новий відсоток.

Навіщо. Читання відсотка зараз тримається на ГЕОМЕТРІЇ смуги (частка заливки),
і це вже сім разів ламалось об чергове синє щось на екрані. Надійніший другий
сигнал — сам ПІДПИС усередині смуги («43%»), прочитаний шаблонами цифр, як на
табло печей (app/furnace_ocr.py). Шаблони знімаються з РЕАЛЬНИХ кадрів, а не
малюються: жоден системний шрифт не збігається (перевірено на печах — Arial
Bold розходився на 20% пікселів).

Кадр перезаписується застосунком раз на 15 с, тож зловити руками різні
відсотки важко. Цей скрипт ходить прямо до агента верстата й зберігає кадр
ЛИШЕ коли змінився прочитаний відсоток — за одну програму назбирується майже
весь набір цифр (9 → 23 → 43 → 57 → 81 → 100).

Запуск на ПК, де стоїть CRM (агент слухає в локальній мережі):

    python scripts/machine_collect_frames.py 192.168.1.85 ТОКЕН
    python scripts/machine_collect_frames.py 192.168.1.85 ТОКЕН --minutes 30

Кадри лягають у teky `calibration_frames/<адреса>/pct-XX-N.png`. Нічого не
змінює ні на верстаті, ні в базі — тільки читає й пише файли.
"""

from __future__ import annotations

import argparse
import io
import sys
import time
from pathlib import Path

import requests
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.machine_ocr import find_progress_bar  # noqa: E402

# Консоль Windows за замовчуванням у cp1251/cp866 і падає з UnicodeEncodeError
# на кирилиці й стрілках — а цей скрипт запускає ОПЕРАТОР на робочому ПК, тож
# «впало на друці» = «скрипт не працює». Перемикаємо потоки на UTF-8 з
# заміною нездатних символів; errors="replace", щоб навіть екзотична консоль
# показала текст, а не обірвала роботу.
for _stream in (sys.stdout, sys.stderr):
    reconfigure = getattr(_stream, "reconfigure", None)
    if reconfigure is not None:
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):  # перенаправлений потік — не біда
            pass

DEFAULT_PORT = 8765
DEFAULT_MINUTES = 20
POLL_SECONDS = 10.0
"""Частіше не треба: відсоток на 15-хвилинній програмі рухається повільно, а
кожен знімок — це трафік з ПК верстата."""


def grab(host: str, port: int, token: str, timeout=(3.0, 15.0)) -> Image.Image:
    response = requests.get(
        f"http://{host}:{port}/capture", headers={"X-Agent-Token": token}, timeout=timeout
    )
    if response.status_code == 403:
        raise RuntimeError("агент відмовив: невірний токен")
    response.raise_for_status()
    return Image.open(io.BytesIO(response.content)).convert("RGB")


def main() -> int:
    # НЕ description=__doc__: у docstring є знаки відсотка («43%»), а argparse
    # проганяє довідку через %-форматування і падає на них.
    parser = argparse.ArgumentParser(
        description="Зібрати калібрувальні кадри верстата — по одному на кожен "
                    "новий відсоток. Подробиці — у docstring файлу."
    )
    parser.add_argument("host", help="адреса ПК верстата, напр. 192.168.1.85")
    parser.add_argument("token", help="токен агента (Налаштування → Верстати)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--minutes", type=float, default=DEFAULT_MINUTES)
    parser.add_argument(
        "--interval", type=float, default=POLL_SECONDS,
        help=f"секунд між знімками (типово {POLL_SECONDS:g})",
    )
    parser.add_argument(
        "--out", default="calibration_frames",
        help="куди складати кадри (типово calibration_frames/)",
    )
    args = parser.parse_args()

    out_dir = Path(args.out) / args.host.replace(":", "-")
    out_dir.mkdir(parents=True, exist_ok=True)

    deadline = time.monotonic() + args.minutes * 60
    seen: dict[int, int] = {}
    saved = 0
    misses = 0

    print(f"Збираю кадри з {args.host}:{args.port} протягом {args.minutes:g} хв.")
    print(f"Тека: {out_dir.resolve()}")
    print("Зупинити раніше — Ctrl+C. Уже збережене нікуди не дінеться.\n")

    try:
        while time.monotonic() < deadline:
            try:
                frame = grab(args.host, args.port, args.token)
            except Exception as exc:  # noqa: BLE001 — збирач не має падати від блимання мережі
                print(f"  ! {exc}")
                time.sleep(args.interval)
                continue

            bar = find_progress_bar(frame)
            if bar is None:
                misses += 1
                # Кадр БЕЗ смуги теж цінний — рівно на ньому детектор мовчить,
                # і саме такий кадр потрібен, щоб зрозуміти чому. Але тримаємо
                # лише кілька, інакше вони заб'ють теку.
                if misses <= 3:
                    path = out_dir / f"no-bar-{misses}.png"
                    frame.save(path)
                    print(f"  смуги не знайдено → {path.name}")
                time.sleep(args.interval)
                continue

            pct = bar.percent
            count = seen.get(pct, 0)
            if count >= 2:
                # Два кадри на кожне значення досить: більше — це та сама
                # бітмапа цифр ще раз.
                time.sleep(args.interval)
                continue

            seen[pct] = count + 1
            path = out_dir / f"pct-{pct:03d}-{count + 1}.png"
            frame.save(path)
            saved += 1
            digits = "".join(sorted({c for c in str(pct)}))
            print(f"  {pct:3d}%  (цифри {digits})  → {path.name}")
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nЗупинено вручну.")

    covered = {c for pct in seen for c in str(pct)}
    missing = sorted(set("0123456789") - covered)
    print(f"\nЗбережено {saved} кадрів, різних відсотків: {len(seen)}.")
    if missing:
        print(f"Цифри, яких ще не бачили: {', '.join(missing)} — "
              f"або лови їх на іншій програмі, або цього вистачить.")
    else:
        print("Усі десять цифр трапились — набір повний.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
