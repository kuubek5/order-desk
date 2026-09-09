"""Еталони ЕКРАНІВ верстата (`app/data/machine_screens.json`).

Навіщо окремий інструмент. Перший еталон («SUMMARY») зняли руками, і поки він
був один, це не заважало. Другий («VALIDATE JOBS») показав ціну: щоб додати
екран, треба знати, з якої смуги береться маска, як пакуються біти й що
покласти в `ones`. Руками це або відтворюють по коду, або помиляються мовчки —
маска з іншої геометрії просто ніколи не збігається, і детектор тихо мовчить.

Тому команда одна, і вона бере маску тією САМОЮ функцією, якою її потім
звіряє розпізнавач (`machine_ocr._title_mask`) — розійтись їм нема як.

    python -m scripts.machine_screens learn validate кадр.png
    python -m scripts.machine_screens show

Кадр — повний знімок екрана верстата (той, що лежить у `machine_frames` або в
zip калібрувальних кадрів). Заголовок мусить бути видимий і не перекритий.
"""
from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

from PIL import Image

from app.machine_ocr import SUMMARY_INK, _title_mask


TEMPLATES = Path(__file__).resolve().parents[1] / "app" / "data" / "machine_screens.json"


def _load() -> dict:
    try:
        return json.loads(TEMPLATES.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def learn(key: str, frame: Path) -> None:
    """Зняти маску заголовка з кадру й покласти її під ключем `key`."""
    image = Image.open(frame)
    got = _title_mask(image)
    if got is None:
        raise SystemExit(
            f"{frame.name}: у смузі заголовка немає жодного світлого пікселя — "
            "це або не той кадр, або заголовок перекрито вікном"
        )
    width, height, bits = got
    packed = bytearray((width * height + 7) // 8)
    for i, bit in enumerate(bits):
        if bit:
            packed[i // 8] |= 1 << (7 - i % 8)
    data = _load()
    data[key] = {
        "w": width,
        "h": height,
        "ink": SUMMARY_INK,
        "ones": sum(bits),
        "bits": base64.b64encode(bytes(packed)).decode("ascii"),
    }
    TEMPLATES.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"{key}: {width}x{height}, чорнила {sum(bits)} px — записано в {TEMPLATES.name}")


def show() -> None:
    for key, tpl in sorted(_load().items()):
        print(f"{key:<10} {tpl['w']}x{tpl['h']}  чорнила {tpl.get('ones', '?')}")


def main(argv: list[str]) -> int:
    if argv[:1] == ["show"]:
        show()
        return 0
    if argv[:1] == ["learn"] and len(argv) == 3:
        learn(argv[1], Path(argv[2]))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
