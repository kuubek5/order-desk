# -*- coding: utf-8 -*-
"""Табло цеху для телевізора — сторінка `/t/<token>/shop` на порту 8010.

Живе поруч із табло пічок і в тому самому окремому застосунку
(`app/routers/furnace_board.py`): той самий секрет у посиланні, той самий
вимикач у налаштуваннях, ті самі «жодних інших маршрутів». Різниця в
аудиторії — логісти дивляться печі, цех дивиться верстати, — тому це
ОКРЕМА адреса, а не заміна: стара сторінка лишається там, де була.

Власних даних сервіс не збирає. Верстати приходять із `machines.snapshot()`
(той самий збирач, що годує екран «Верстати»), печі — з
`furnace_board.board_view()`. Інакше на табло рано чи пізно з'явилось би
друге, розбіжне джерело правди про той самий верстат.

ЧОМУ станів більше, ніж «працює / не працює». `MachineCard.state_key` знає
дев'ять станів, і два з них вимагають людини просто зараз: `done` —
програма завершена, треба зняти, і `fault` — на екрані помилка. Табло, яке
звело б їх до «не фрезерує», мовчало б саме тоді, коли має кричати.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from app.business_day import business_now

# Стани, які вимагають людини, — і те, як їх бачить око з проходу.
# Ключ — `MachineCard.state_key`, значення — канал кольору на табло.
_TONES = {
    "run": "run",          # фрезерує — бурштин, шестерня крутиться
    "done": "done",        # завершено, зняти — вимагає дії
    "fault": "bad",        # помилка на екрані
    "off": "bad",          # немає зв'язку
    "check": "busy",       # перевіряє програму перед стартом
    "busy": "busy",        # програма пішла, відсотка ще не видно
    "idle": "calm",        # стоїть
    "wait": "calm",        # кадру ще не було
    "unreadable": "calm",  # екран цього верстата читати не навчені
}


@dataclass
class ShopMachine:
    """Один верстат на табло. Усе вже готове до показу — шаблон не рахує."""

    key: str
    name: str
    state: str                       # state_key як є, для налагодження
    tone: str                        # канал кольору (див. _TONES)
    word: str = ""                   # слово стану; для «run» порожнє — там число
    note: str = ""                   # пояснення під словом
    percent: Optional[int] = None
    sum3d: str = ""
    client: str = ""
    material: str = ""
    extra: str = ""                  # «ще 2 роботи» — коли Sum3D ділять кілька
    portrait: str = ""               # адреса фото верстата, якщо воно є
    # SLM-принтер: шар N з M — числа з екрана машини, не оцінка.
    is_sisma: bool = False
    layer: Optional[int] = None
    layers_total: Optional[int] = None
    # Коли машина обіцяє закінчити — ЇЇ прогноз із екрана, не наша оцінка.
    ends_at: str = ""
    left_text: str = ""

    @property
    def sisma_percent(self) -> Optional[int]:
        if not (self.layer and self.layers_total):
            return None
        return round(self.layer / self.layers_total * 100)


@dataclass
class ShopView:
    now: datetime
    machines: list[ShopMachine] = field(default_factory=list)
    sisma: Optional[ShopMachine] = None
    # Печі приходять готовими картками з табло логістів — один збирач на два
    # екрани, щоб числа на них не могли розійтись.
    furnaces: list = field(default_factory=list)
    nearest_name: str = ""
    nearest_at: str = ""
    nearest_day: str = ""
    nearest_iso: str = ""

    @property
    def columns(self) -> int:
        """Скільки колонок сітки. Рахується від КІЛЬКОСТІ верстатів, а не
        зашито п'ятіркою: у цеху їх то дев'ять, то десять, і жорстка сітка
        або лишала діру, або давила останній ряд."""
        total = len(self.machines) + (1 if self.sisma else 0)
        if total <= 4:
            return max(total, 1)
        if total <= 6:
            return 3
        if total <= 12:
            return (total + 1) // 2
        return (total + 2) // 3


def shop_links(db: Session) -> list[str]:
    """Посилання на табло цеху — ті самі адреси й секрет, що в печей.

    Окрема функція, а не параметр до `furnace_board.board_links`: ту читають
    ще й налаштування з MCP, і зміна її сигнатури зачепила б місця, які до
    телевізора не мають стосунку.
    """
    from app.services import furnace_board

    token = furnace_board.board_token(db)
    if not token:
        return []
    return [
        f"http://{address}:{furnace_board.BOARD_PORT}/t/{token}/shop"
        for address in furnace_board.lan_addresses()
    ]


def _work_of(card) -> tuple[str, str, str]:
    """Клієнт, матеріал і «ще N» для картки.

    Один проєкт Sum3D може містити кілька робіт (див. `MachineCard.orders`).
    Табло показує першу й чесно каже, що вона не одна, — вигадувати спільного
    клієнта для трьох різних робіт не можна.
    """
    orders = list(getattr(card, "orders", None) or [])
    if not orders:
        return "", "", ""
    # Порядок ФІКСУЄМО за id. `snapshot()` збирає роботи двома проходами
    # (звичайні й переробки) і без сортування, бо екран «Верстати» показує
    # їх усі — там черговість байдужа. Тут показується ПЕРША, і без цього
    # рядка ім'я клієнта на телевізорі могло б мінятись між оновленнями,
    # хоча на верстаті нічого не змінилось.
    orders.sort(key=lambda o: (getattr(o, "id", 0) or 0))
    first = orders[0]
    # Клієнт є не в кожної роботи: у лабораторних його немає взагалі, там
    # робота впізнається НОМЕРОМ НАРЯДУ. Той самий порядок, що на екрані
    # «Верстати». Без запасного варіанта рядок лишався порожнім, і на
    # телевізорі було видно лише матеріал (скарга з цеху 14.09.26).
    client = (getattr(first, "client_name", "") or "").strip()
    if not client:
        client = (getattr(first, "work_order_no", "") or "").strip()
    bits = [
        (getattr(first, "material_color", "") or "").strip(),
        (getattr(first, "quantity", "") or "").strip(),
        (getattr(first, "kind", "") or "").strip(),
    ]
    material = " · ".join(b for b in bits if b)
    extra = f"ще {len(orders) - 1}" if len(orders) > 1 else ""
    return client, material, extra


def _portrait_of(card, token: str) -> str:
    """Адреса картинки верстата: спершу ЙОГО фото, інакше дефолт моделі.

    Той самий порядок, що на екрані «Верстати»: фото, завантажене в
    Налаштуваннях (`app/machine_portraits.py`), має перевагу, бо реальні
    машини виглядають інакше, ніж згенерований портрет. Але коли фото ще
    не завантажили — картка не лишається порожньою: дефолт моделі краще за
    голий фон, і саме так поводиться решта застосунку.

    Адреса фото своя, а не `/machines/portrait/...`: у табло немає маршрутів
    головного застосунку, і бути не повинно.
    """
    from app.machine_portraits import portrait_version
    from app.services.machines import machine_model_key

    machine_id = getattr(card.target, "machine_id", None)
    if machine_id is not None:
        version = portrait_version(machine_id)
        if version is not None:
            return f"/t/{token}/portrait/{machine_id}.jpg?v={version}"
    key = machine_model_key(
        getattr(card.target, "name", "") or "",
        getattr(card.target, "portrait_model", "") or "",
    )
    return f"/static/img/machine-portrait-{key}.jpg"


def _machine(card, token: str) -> ShopMachine:
    state = card.state_key
    item = ShopMachine(
        key=card.key,
        name=card.target.name,
        state=state,
        tone=_TONES.get(state, "calm"),
        word=card.state_word,
        note=card.state_note,
        percent=card.percent,
        sum3d=card.sum3d_id or "",
        portrait=_portrait_of(card, token),
    )
    item.client, item.material, item.extra = _work_of(card)
    return item


def _sisma(card, token: str) -> ShopMachine:
    item = _machine(card, token)
    item.is_sisma = True
    layers = card.layers
    if layers:
        item.layer, item.layers_total = layers
    # Час кінця друку — прогноз САМОЇ машини (`ends_at`), тому й показуємо
    # його як є. Свій ми б не порахували: швидкість шару не стала.
    ends = getattr(card, "ends_at", None)
    if ends is not None:
        item.ends_at = ends.strftime("%H:%M")
    item.left_text = (getattr(card, "left_text", "") or "").strip()
    return item


def shop_view(
    db: Session,
    *,
    token: str = "",
    now: Optional[datetime] = None,
    cards=None,
    furnace_view=None,
) -> ShopView:
    """Зібрати все, що показує табло цеху. `cards`/`furnace_view` — для тестів."""
    from app.services import furnace_board
    from app.services.machines import snapshot

    now = now or business_now()
    source = snapshot(db) if cards is None else list(cards)
    view = ShopView(now=now)

    for card in source:
        # Верстат, прибраний із табло в Налаштуваннях, сюди не потрапляє —
        # але опитуватись не перестає: `show_on_board` про місце на екрані,
        # а не про те, стежимо ми за ним чи ні.
        if not getattr(card.target, "show_on_board", True):
            continue
        if card.is_sisma_machine:
            # Принтер лишається на табло і тоді, коли зв'язок обірвався:
            # саме тоді він найпотрібніший (та сама причина, що у віджеті).
            view.sisma = _sisma(card, token)
        else:
            view.machines.append(_machine(card, token))

    fv = furnace_board.board_view(db, now=now) if furnace_view is None else furnace_view
    view.furnaces = list(fv.cards)
    view.nearest_name = fv.nearest_name
    view.nearest_at = fv.nearest_at
    view.nearest_day = fv.nearest_day
    view.nearest_iso = fv.nearest_iso
    return view
