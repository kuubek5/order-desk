# -*- coding: utf-8 -*-
"""Видима назва: «Order Desk» → «KuubMill».

Міняється ЛИШЕ те, що бачить людина: заголовки вкладок, шапка рейки,
сторінка входу, ліцензія, майстер налаштування, трей, діалоги інсталятора,
повідомлення про помилки.

НЕ чіпається технічний шар — `OrderDesk.exe`, тека `%LOCALAPPDATA%\\OrderDesk`,
`order_desk.db`, `order-desk.log`, ключ автозапуску, м'ютекси,
`ORDER_DESK_*`, імена ассетів релізу, репозиторії GitHub. Там живе
встановлений прод із даними, і перейменування = міграція + розрив ланцюга
оновлень. Тому правило просте: рядок із пробілом («Order Desk») — видимий,
злите слово («OrderDesk») — технічне.
"""
import pathlib
import re
import sys

ROOT = pathlib.Path(r"P:\AI-Projects\CRM_Laba")
APPLY = "--apply" in sys.argv

# Тільки форма з пробілом. Злите «OrderDesk» лишається недоторканим.
PATTERN = re.compile(r"Order Desk")
NEW = "KuubMill"

TARGETS = []
TARGETS += sorted((ROOT / "app" / "templates").glob("*.html"))
TARGETS += [
    ROOT / "app" / "web.py",
    ROOT / "app" / "backup.py",
    ROOT / "app" / "google_oauth.py",
    ROOT / "app" / "windows_launcher.py",
    ROOT / "app" / "update_check.py",
    ROOT / "app" / "license.py",
    ROOT / "app" / "changelog.py",
    ROOT / "app" / "mail_reader.py",
    ROOT / "installer" / "OrderDesk.iss",
    ROOT / "dev_server.ps1",
]

total = 0
for f in TARGETS:
    if not f.exists():
        print("немає:", f)
        continue
    t = f.read_text(encoding="utf-8")
    new, n = PATTERN.subn(NEW, t)
    if n:
        total += n
        print(f"{f.relative_to(ROOT)}: {n}")
        if APPLY:
            f.write_text(new, encoding="utf-8")

print(("ЗАСТОСОВАНО " if APPLY else "ПРОБНИЙ ХІД ") + f"замін: {total}")
