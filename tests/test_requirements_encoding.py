"""requirements-файли мусять лишатись ASCII.

Живий випадок: кириличний коментар у `requirements.txt` завалив збірку 0.3.32
цілком. pip читає requirements локальним кодуванням машини, якщо у файлі немає
BOM, а на CI-раннері це cp1252 — байт 0x81 (половинка «П») там не існує, і pip
падає з UnicodeDecodeError ще до встановлення PyInstaller. Далі все валиться
доміно: немає PyInstaller → немає dist → Inno не знаходить файлів → релізу
немає. Помилка виглядає як проблема збірки, хоча причина — один коментар.

Тому: пояснення в цих файлах пишемо англійською.
"""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
REQUIREMENTS = sorted(ROOT.glob("requirements*.txt"))


def test_requirements_files_exist():
    assert REQUIREMENTS, "не знайдено жодного requirements*.txt"


@pytest.mark.parametrize("path", REQUIREMENTS, ids=lambda p: p.name)
def test_requirements_file_is_ascii(path):
    raw = path.read_bytes()
    offenders = [(index, byte) for index, byte in enumerate(raw) if byte > 127]
    if not offenders:
        return
    index, byte = offenders[0]
    context = raw[max(0, index - 60) : index + 60].decode("utf-8", "replace")
    pytest.fail(
        f"{path.name}: не-ASCII байт {hex(byte)} на позиції {index} "
        f"(усього {len(offenders)}).\n"
        f"  контекст: {context!r}\n"
        "  pip читає цей файл кодуванням машини — на CI (cp1252) збірка впаде. "
        "Коментарі тут пишемо англійською."
    )
