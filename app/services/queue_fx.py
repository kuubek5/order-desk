"""Вигляд черги: «живий верстат у рядку» і плавні переходи (власник 12.11.26).

Два вимикачі адміністратора (Налаштування → Робочий процес → «Вигляд черги»):

* `live`   — робота, що зараз фрезерується, має внизу рядка смужку прогресу
  (той самий відсоток, що в чіпі верстата й на табло; джерело одне —
  `machines.milling_now`);
* `motion` — рух у відповідь на подію: новий рядок в'їжджає й один раз
  підсвічується, зниклий — згасає, лічильники вкладок змінюються плавно.

Типово обидва увімкнено; «0» — вимкнено. Це лише вигляд: порядок рядків,
дані й дії черги від них не залежать (CLAUDE.md §2: нічого не стрибає під
руками). Канали рядка не чіпаємо (§14): фон — стани, лівий край — матеріал,
контур — «мої зараз»; прогрес живе на НИЖНЬОМУ краї.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.settings_store import get_setting, set_setting

LIVE_KEY = "queue_fx_live"
MOTION_KEY = "queue_fx_motion"


def flags(db: Session) -> dict[str, bool]:
    return {
        "live": (get_setting(db, LIVE_KEY) or "1") != "0",
        "motion": (get_setting(db, MOTION_KEY) or "1") != "0",
    }


def save(db: Session, *, live: bool, motion: bool) -> None:
    set_setting(db, LIVE_KEY, "1" if live else "0")
    set_setting(db, MOTION_KEY, "1" if motion else "0")
