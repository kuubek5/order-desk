# -*- coding: utf-8 -*-
"""KuubMill-агент для ПК верстата: кадр екрана в мережеву теку.

НАВІЩО ВІН Є. Доступу до софта верстата немає, тому стан читається з екрана
RemiCORE. VNC-сервери (UltraVNC) на Windows 7 знімають екран старим способом
і НЕ БАЧАТЬ смугу відсотка виконання — вона малюється поверх звичайного
шару. RustDesk її бачить, тобто картинка технічно доступна; бракувало лише
захоплювача, який бере кадр так само.

ЩО ВІН РОБИТЬ. Рівно одне: раз на N секунд знімає екран через PrintWindow
(той самий шлях, яким кадр бачить композитор Windows) і кладе PNG у теку,
яку CRM і так читає. Ніякого протоколу, ніякого порту, ніякого пароля.

ЧОГО ВІН НЕ РОБИТЬ — і не вміє за конструкцією:
* не приймає вхідних з'єднань (порт не відкривається взагалі);
* не шле верстату ані байта вводу — у коді немає жодного виклику миші чи
  клавіатури;
* не читає, не пересуває й не видаляє нічого, крім СВОГО кадру;
* нічого не шле в інтернет — тільки файл у теку локальної мережі.

Читається за хвилину, і це навмисно: агент на CNC-ПК має бути таким, щоб
власник цеху міг сам переконатись, що він безпечний.

Запуск на верстаті:
    python kuubmill_agent.py --out \\\\SERVER\\share\\machine_frames --name 350i-1

Без Python на ПК верстата збирається в один .exe (див. agent/README.md).
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import sys
import time
from pathlib import Path

# Значення за замовчуванням: кадр раз на 15 с — рівно з тим тактом, що його
# читає CRM. Частіше не потрібно: відсоток на екрані міняється повільно.
DEFAULT_INTERVAL_SECONDS = 15.0

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

SRCCOPY = 0x00CC0020
# Прапорець PW_RENDERFULLCONTENT: саме він змушує Windows віддати ПОВНИЙ
# вміст вікна, включно з шарами, які звичайний BitBlt не бачить. Це і є та
# відмінність, через яку UltraVNC на Win7 губив смугу відсотка.
PW_RENDERFULLCONTENT = 0x00000002


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
        ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
        ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG),
        ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD),
    ]


def grab_screen():
    """Кадр усього робочого стола як (ширина, висота, BGRA-байти).

    Спершу пробуємо PrintWindow з PW_RENDERFULLCONTENT — він віддає вміст
    вікна робочого стола повністю. Якщо система його не підтримує (старі
    складання Win7), тихо відкочуємось на класичний BitBlt: краще кадр без
    смуги, ніж жодного кадру.
    """
    hwnd = user32.GetDesktopWindow()
    width = user32.GetSystemMetrics(0)
    height = user32.GetSystemMetrics(1)

    window_dc = user32.GetWindowDC(hwnd)
    mem_dc = gdi32.CreateCompatibleDC(window_dc)
    bitmap = gdi32.CreateCompatibleBitmap(window_dc, width, height)
    gdi32.SelectObject(mem_dc, bitmap)

    ok = user32.PrintWindow(hwnd, mem_dc, PW_RENDERFULLCONTENT)
    if not ok:
        gdi32.BitBlt(mem_dc, 0, 0, width, height, window_dc, 0, 0, SRCCOPY)

    header = BITMAPINFOHEADER()
    header.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    header.biWidth = width
    # Від'ємна висота = рядки згори вниз. Інакше кадр приходить перевернутим.
    header.biHeight = -height
    header.biPlanes = 1
    header.biBitCount = 32
    header.biCompression = 0

    buffer = ctypes.create_string_buffer(width * height * 4)
    gdi32.GetDIBits(mem_dc, bitmap, 0, height, buffer, ctypes.byref(header), 0)

    gdi32.DeleteObject(bitmap)
    gdi32.DeleteDC(mem_dc)
    user32.ReleaseDC(hwnd, window_dc)
    return width, height, buffer.raw


def save_png(path: Path, width: int, height: int, bgra: bytes) -> None:
    """Записати кадр атомарно: спершу у тимчасовий файл, потім підміна.

    Без цього CRM іноді читала б напівзаписаний файл — та сама причина, з
    якої кадри печей теж пишуться через tmp + replace.
    """
    from PIL import Image

    image = Image.frombuffer("RGBA", (width, height), bgra, "raw", "BGRA", 0, 1)
    tmp = path.with_suffix(".tmp")
    image.convert("RGB").save(tmp, format="PNG", optimize=True)
    tmp.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description="KuubMill: кадр екрана верстата у теку")
    parser.add_argument("--out", required=True, help="тека для кадрів (мережевий шлях)")
    parser.add_argument("--name", required=True, help="ім'я верстата: буде іменем файлу")
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_SECONDS)
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{args.name}.png"

    print(f"KuubMill-агент: {target}, кожні {args.interval:g} с. Ctrl+C — зупинити.")
    while True:
        started = time.monotonic()
        try:
            width, height, pixels = grab_screen()
            save_png(target, width, height, pixels)
        except Exception as exc:  # noqa: BLE001 — агент не має падати від однієї невдачі
            print(f"кадр не знято: {exc}")
        time.sleep(max(0.5, args.interval - (time.monotonic() - started)))


if __name__ == "__main__":
    sys.exit(main())
