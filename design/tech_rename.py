# -*- coding: utf-8 -*-
"""Технічне перейменування OrderDesk → KuubMill.

Свідомо НЕ чіпається:
  • GITHUB_REPO (kuubek5/order-desk-releases) — це справжній віддалений
    репозиторій. Змінити рядок раніше, ніж перейменують сам репозиторій,
    означає тихо зламати перевірку оновлень.
  • Імена мютекса й події виключення — вони мають окрему обробку
    (нова збірка слухає й старі імена, інакше під час оновлення старий і
    новий процеси не побачать одне одного).
"""
import pathlib, re, sys

ROOT = pathlib.Path(r"P:\AI-Projects\CRM_Laba")
APPLY = "--apply" in sys.argv

# Порядок важливий: довші зразки перші, щоб не зіпсувати вкладені збіги.
RULES = [
    (r"OrderDesk-Setup", "KuubMill-Setup"),
    (r"OrderDeskBuild", "KuubMillBuild"),
    (r"orderdesk\.ico", "kuubmill.ico"),
    (r"OrderDesk\.spec", "KuubMill.spec"),
    (r"OrderDesk\.iss", "KuubMill.iss"),
    (r"OrderDesk\.exe", "KuubMill.exe"),
    (r"ORDER_DESK_NONINTERACTIVE", "KUUBMILL_NONINTERACTIVE"),
    (r"ORDER_DESK_SCHEMA_MANAGED", "KUUBMILL_SCHEMA_MANAGED"),
    (r"order-desk\.log", "kuubmill.log"),
    # Гола назва — лишається останньою, бо входить у всі зразки вище.
    (r"\bOrderDesk\b", "KuubMill"),
]

SKIP_DIRS = {".git", "dist", "build", "dist-installer", ".venv", "node_modules",
             "v2b", "design", "__pycache__", ".impeccable"}
EXTS = {".py", ".iss", ".spec", ".ps1", ".yml", ".yaml", ".md", ".html", ".toml", ".cfg", ".txt"}

# Файли, де назва є частиною зовнішнього контракту.
FREEZE = {"update_check.py"}   # GITHUB_REPO — див. докстрінг

total = 0
touched = []
for f in ROOT.rglob("*"):
    if not f.is_file() or f.suffix.lower() not in EXTS:
        continue
    if any(part in SKIP_DIRS for part in f.parts):
        continue
    try:
        t = f.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        continue
    orig, n = t, 0
    for pat, rep in RULES:
        if f.name in FREEZE and "order-desk-releases" in t:
            # у цьому файлі міняємо все, крім рядка з репозиторієм
            t2, k = re.subn(pat, rep, t)
            t2 = t2.replace("kuubek5/KuubMill-releases", "kuubek5/order-desk-releases")
            t2 = t2.replace("kuubek5/KuubMill", "kuubek5/order-desk")
            t, k2 = t2, k
            n += k2
            continue
        t, k = re.subn(pat, rep, t)
        n += k
    if n and t != orig:
        total += n
        touched.append((f.relative_to(ROOT), n))
        if APPLY:
            f.write_text(t, encoding="utf-8")

for rel, n in sorted(touched):
    print(f"{rel}: {n}")
print(("ЗАСТОСОВАНО " if APPLY else "ПРОБНИЙ ХІД ") + f"замін: {total} у {len(touched)} файлах")
