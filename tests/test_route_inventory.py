"""Сторож інвентаря роутів — страхувальна сітка для розбиття app/web.py.

Перенесення роутів у окремі APIRouter ламається ТИХО: загубився роут,
змінився шлях, зник метод. Поведінкові тести цього не бачать — вони
перевіряють, що роут РОБИТЬ, а не що він існує під тим самим шляхом.

Цей тест звіряє повний набір (метод, шлях) застосунку зі знімком у
tests/route_inventory.txt. Будь-яке відхилення — червоний.

Коли роут додають/прибирають СВІДОМО — оновити знімок:
    python -c "import app.web,io; \
rows=sorted({(m+' '+r.path) for r in app.web.app.routes \
if getattr(r,'path',None) for m in (getattr(r,'methods',None) or ['MOUNT']) \
if m not in ('HEAD','OPTIONS')}); \
io.open('tests/route_inventory.txt','w',encoding='utf-8',newline='\\n').write(chr(10).join(rows)+chr(10))"
і переконатись, що зміна навмисна.
"""

from pathlib import Path

import app.web as web

INVENTORY = Path(__file__).parent / "route_inventory.txt"


def _current_routes() -> set[str]:
    rows = set()
    for r in web.app.routes:
        path = getattr(r, "path", None)
        if not path:
            continue
        methods = getattr(r, "methods", None)
        if not methods:
            rows.add(f"MOUNT {path}")
            continue
        for m in methods:
            if m in ("HEAD", "OPTIONS"):
                continue
            rows.add(f"{m} {path}")
    return rows


def _saved_routes() -> set[str]:
    return {line.strip() for line in INVENTORY.read_text(encoding="utf-8").splitlines() if line.strip()}


def test_no_route_was_lost_or_renamed():
    current = _current_routes()
    saved = _saved_routes()
    missing = saved - current
    added = current - saved
    assert not missing and not added, (
        f"Інвентар роутів змінився.\n"
        f"  ЗНИКЛИ (зламає посилання/форми): {sorted(missing)}\n"
        f"  ДОДАНІ (онови знімок, якщо навмисно): {sorted(added)}\n"
        f"Якщо зміна свідома — онови tests/route_inventory.txt (див. докстрінг)."
    )


def test_app_imports_without_side_effects():
    # Підняття застосунку не має падати й має дати непорожній набір роутів —
    # базова перевірка, що розбиття не зламало збірку app.
    assert len(_current_routes()) > 50
