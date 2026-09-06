"""Порівняти два знімки `settings_baseline.py` — що зникло, що зʼявилось.

    python scripts/settings_baseline.py --out C:\\tmp\\after
    python scripts/settings_baseline_diff.py design/settings-redesign C:\\tmp\\after

Порядок пунктів у меню навмисно НЕ порівнюється як текст: редизайн саме й
переставляє групи. Перевірка одна й сувора — **зниклих має бути нуль**:
кожен `href`, `action`, `name`, `id` і `data-sec`, який бачив користувач до
змін, мусить лишитися доступним. Нове показується окремо, щоб очима звірити
зі списком запланованого.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Консоль Windows живе в cp1251 — інакше знімок падає на першому ж «−».
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROLES = ("адмін", "оператор")
KEYS = ("rail_hrefs", "sections", "actions", "names", "ids")


def _load(folder: Path, role: str) -> dict:
    path = folder / f"baseline_{role}.json"
    if not path.exists():
        raise SystemExit(f"немає знімка: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _bag(snapshot: dict) -> dict[str, set]:
    """Усе клікабельне з усіх сторінок ролі, згорнуте у множини."""
    bag: dict[str, set] = {key: set() for key in KEYS}
    bag["status"] = set()
    for path, page in snapshot["pages"].items():
        bag["status"].add(f"{path} → {page['status']}")
        for key in KEYS:
            for value in page.get(key, []):
                bag[key].add(value)
    return bag


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    before, after = Path(sys.argv[1]), Path(sys.argv[2])

    lost_total = 0
    for role in ROLES:
        old = _bag(_load(before, role))
        new = _bag(_load(after, role))
        print(f"\n=== {role} ===")
        for key in ("status", *KEYS):
            lost = sorted(old[key] - new[key])
            added = sorted(new[key] - old[key])
            lost_total += len(lost) if key != "status" else 0
            if not lost and not added:
                print(f"  {key}: без змін ({len(new[key])})")
                continue
            print(f"  {key}: -{len(lost)} / +{len(added)}")
            for item in lost:
                print(f"    ЗНИКЛО: {item}")
            for item in added:
                print(f"    нове:   {item}")

    print()
    if lost_total:
        print(f"ПРОВАЛ: зникло {lost_total} елементів")
        raise SystemExit(1)
    print("OK: нічого не зникло")


if __name__ == "__main__":
    main()
