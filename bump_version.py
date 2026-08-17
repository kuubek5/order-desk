#!/usr/bin/env python
"""Підняти версію застосунку в УСІХ чотирьох місцях за один крок.

Навіщо: версія зашита руками в app/__version__.py, installer/OrderDesk.iss і
.github/workflows/release.yml (6 разів). У минулих сесіях це робилось потрійним
`sed` по чотири рази — легко проґавити одне місце й отримати провал релізу.
Див. SESSIONS_DIAGNOSTICS.md, кластер 10.

Використання:
    python bump_version.py 0.3.1
    python bump_version.py 0.3.1 --check   # лише показати, що змінилось би

Після бампу:
    python -m pytest tests/test_version_sync.py -q   # має пройти
    git commit + git tag v0.3.1 + git push origin v0.3.1  # тригерить CI-реліз
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def bump(new: str, check: bool) -> int:
    if not VERSION_RE.match(new):
        print(f"Невалідна версія: {new!r} (очікую X.Y.Z)")
        return 2

    version_py = ROOT / "app" / "__version__.py"
    iss = ROOT / "installer" / "OrderDesk.iss"
    workflow = ROOT / ".github" / "workflows" / "release.yml"

    old_match = re.search(r'VERSION\s*=\s*"([^"]+)"', _read(version_py))
    if not old_match:
        print("Не знайшов VERSION у app/__version__.py")
        return 1
    old = old_match.group(1)
    if old == new:
        print(f"Версія вже {new} — нічого міняти.")
        return 0

    edits = {
        version_py: (f'VERSION = "{old}"', f'VERSION = "{new}"'),
        iss: (f'#define MyAppVersion "{old}"', f'#define MyAppVersion "{new}"'),
    }

    print(f"{old}  ->  {new}")
    for path, (frm, to) in edits.items():
        text = _read(path)
        if frm not in text:
            print(f"  ! {path.relative_to(ROOT)}: не знайдено {frm!r}")
            return 1
        print(f"  {path.relative_to(ROOT)}: 1 заміна")
        if not check:
            path.write_text(text.replace(frm, to), encoding="utf-8")

    # Workflow: усі згадки OrderDesk-Setup-<old> -> <new> (їх 6)
    wtext = _read(workflow)
    count = wtext.count(f"OrderDesk-Setup-{old}")
    print(f"  .github/workflows/release.yml: {count} замін")
    if not check:
        workflow.write_text(
            wtext.replace(f"OrderDesk-Setup-{old}", f"OrderDesk-Setup-{new}"), encoding="utf-8"
        )

    if check:
        print("\n(--check: нічого не записано)")
    else:
        print("\nГотово. Далі: pytest test_version_sync -> commit -> tag v" + new + " -> push")
    return 0


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--check"]
    if len(args) != 1:
        print(__doc__)
        sys.exit(2)
    sys.exit(bump(args[0], check="--check" in sys.argv))
