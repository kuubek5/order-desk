"""Нові диски з теки CAM: що взяли з архіву і що замовити на складі.

Екран — «Нові диски» (`app/routers/discs.py`); замовлення як записи —
`app/services/disc_orders.py`. Тут — читання теки, розбір назв, формат
рядка замовлення й розкладка по змінах.

**Навіщо.** Щодня до 17:30 оператор обходив шухляди, дивився, чого бракує, і
писав на склад у Viber — близько десяти хвилин, кожен день. Але коли він
створює новий диск у CAM, той сам кладе файл у
`<корінь>/<матеріал>/<висота>/<висота>-<виробник>-<колір>-x<номер>`. Створення
диска майже завжди означає «взяв новий з архіву». Тобто замовлення вже існує
на диску як побічний слід роботи, яку й так роблять — його лишається зібрати.

**Ідентичність — за ПРИСУТНІСТЮ, не за назвою.** Порядковий номер свій на
кожну групу (матеріал + колір + висота) і після підчистки теки починається з
малого (підтверджено власником 08.09.26). Тобто назви повторюються. Якби ми
звіряли самі назви, після першої ж підчистки нові диски виглядали б як давно
бачені й у замовлення не потрапляли. Тому рядок у базі описує присутність
файлу: назва, що виникла ЗНОВУ після зникнення свого файлу, — це новий диск.

**Дата файлу тут не джерело правди.** У назві часу немає (на відміну від
проєктів Sum3D), а `mtime` зсувають копіювання теки, відновлення з копії й
антивірус. Час появи ставимо СВІЙ, у момент, коли вперше побачили файл.
Час теки використовуємо лише як ПІДКАЗКУ «чи варто читати список» — якщо
підказка збреше, ми просто прочитаємо теку зайвий раз і отримаємо той самий
правильний результат.

**Ціна проходу — ВИМІРЯНА, не припущена.** Читаємо тільки імена: ні вмісту,
ні розміру, ні дати файлу. На 7000 файлів (14 груп) вийшло: саме читання імен
32 мс, перший прохід із записом 516 мс, звичайний тік із одним новим файлом
141 мс. Лінійно це дає близько секунди на тік при 50 тисячах файлів — раз на
пʼять хвилин це прийнятно, тож жодних хитрощів на кшталт «дивитись час зміни
теки» тут НЕ треба. Спершу міряти, потім оптимізувати.

Великий ПЕРШИЙ прохід ріжеться на пачки (`_COMMIT_EVERY`): 50 тисяч вставок
однією транзакцією тримали б базу зайнятою кілька секунд, а поруч кожну
хвилину пишуть покази верстатів і печей.

Модуль ЧИТАЄ теку. Видалення (окремий блок) буде свідомим і підтвердженим.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from pathlib import Path
from time import monotonic
from typing import Iterable, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import CamBlank

logger = logging.getLogger(__name__)

# Розширення файлу-заготовки. Порівняння регістронезалежне (.blk/.BLK).
BLANK_EXT = ".blk"

# По скільки рядків комітити на великому першому проході.
_COMMIT_EVERY = 500

# ── ДВА формати назв, і обидва справжні ────────────────────────────────────
#
# Формат A (чекали спочатку): `12-monolith-a2-x14` — висота, виробник, колір,
# номер. Так виглядали зразки, на яких фічу писали.
#
# Формат B (що НАСПРАВДІ пише CAM на робочому ПК, перевірено 08.09.26):
#
#   zr25_25-a1-x29                 матеріал zr, висота 25, колір a1, № 29
#   pmma25_25-a3-x261              матеріал pmma, висота 25, колір a3, № 261
#   D98_zr25_25-a2-x54             те саме плюс префікс діаметра диска
#   crco20_20-hpp-x18              матеріал crco, висота 20, колір hpp, № 18
#   pmmac25_25-pmmaProzrach-281    номер БЕЗ `x`
#   crco25_TRINIA_IVORY-X03        велика `X`, у голові немає висоти
#
# Жодна з цих назв старою регуляркою не розбиралась — тобто на робочому ПК
# розбір не влучав У ЖОДЕН файл із 19 тисяч. Диски рахувались (тотожність іде
# за шляхом, не за назвою), але замовлення для комірниці виходило не списком
# «Mono a2 25(3)», а купою сирих імен файлів. Фіча працювала наполовину, і
# видно це стало лише на справжніх назвах.
#
# Формат C (скрін з робочого ПК, 10.09.26) — формат B зі СЛОВОМ ВИРОБНИКА
# між головою і кольором, а колір і номер бувають складені:
#
#   zr18_18-Monolith-A2-x37        zr, висота 18, виробник monolith, колір a2
#   zr20_20-Monolith-a3-5-x24      колір `a3-5` = A3.5 (крапку файлова назва
#                                  не любить, тож CAM пише дефіс)
#   zr14_14-Emotions-A3-x843PRO    хвіст номера з літерами
#
# Регулярка формату B брала тут голову `zr20_20-Monolith-a3`, колір `5`, і
# висоти в такій голові не знаходила — у замовлення комірниці йшли сирі імена
# файлів (08.09–10.09.26), а рядки з `A3-5` читались як «zr, колір 5».
#
# Розбираємо СПРАВА, бо саме хвіст стабільний: останній блок — номер, перший —
# голова з матеріалом і висотою, між ними — колір (і, якщо блоків між ними
# більше одного й перший із них — слово, — виробник). Голова змінюється від
# машини до машини, хвіст — ні.
_SERIAL_RE = re.compile(r"^[xX]?(?P<serial>\d+)[A-Za-z]*$")
# Слово виробника: лише літери, від трьох. Колір (`a2`, `s1`, `a3`) цю умову
# не проходить, тож `zr25_25-a3-5-x10` лишається кольором `a3-5` без виробника.
_BRAND_WORD_RE = re.compile(r"^[A-Za-zА-Яа-яІіЇїЄєҐґ]{3,}$")
# Голова формату B: `zr25_25`, `D98_zr25_25`, `crco25_TRINIA_IVORY`.
# Висота — останнє число після `_`.
_HEAD_HEIGHT_RE = re.compile(r"_(?P<height>\d{1,3})$")
_CODE_LETTERS_RE = re.compile(r"^(?P<code>[A-Za-zА-Яа-яІіЇїЄєҐґ]+)")


def _head_code(head: str) -> Optional[str]:
    """Код матеріалу з голови назви.

    Беремо сегмент ПЕРЕД хвостовою висотою, а не перший: у `D98_zr25_25`
    перший сегмент — це діаметр диска (98 мм), і матеріалом він не є. Перший
    підхід повертав тут «d», що в замовленні комірниці виглядало б як окремий
    неіснуючий матеріал.

    Голова без хвостової висоти (`crco25_TRINIA_IVORY`) — беремо перший
    сегмент: іншого орієнтира немає.
    """
    parts = [p for p in head.split("_") if p]
    if not parts:
        return None
    # `zr25_25` → сегменти [zr25, 25]; беремо передостанній, якщо останній —
    # це висота, інакше перший.
    if len(parts) >= 2 and parts[-1].isdigit():
        candidate = parts[-2]
    else:
        candidate = parts[0]
    match = _CODE_LETTERS_RE.match(candidate)
    return match.group("code") if match else None


@dataclass(frozen=True)
class ParsedBlank:
    """Розібрана назва. Будь-яке поле може бути None — формат це конвенція,
    не примус, і нерозібраний файл усе одно лишається взятим диском."""

    height: Optional[int] = None
    brand: Optional[str] = None
    shade: Optional[str] = None
    serial: Optional[int] = None


def parse_blank_name(name: str) -> ParsedBlank:
    """Назва файлу → висота, виробник, колір, номер. Розуміє формати A, B і C.

    Не розібралось — повертаємо порожній ParsedBlank, а не None: викликач
    однаково мусить порахувати файл, просто без структурованих полів.

    `brand` — виробник, якщо назва його несе (`monolith`, `emotions`), інакше
    КОД МАТЕРІАЛУ з голови (`zr`, `pmma`, `crco`, `ti`). Для комірниці рядок
    «zr a2 25» усе одно кращий за сире імʼя файлу.
    """
    stem = name[: -len(BLANK_EXT)] if name.lower().endswith(BLANK_EXT) else name
    parts = [part.strip() for part in stem.strip().split("-")]
    # Голова, хоч один блок кольору й номер. Порожній блок (`a--b`) — не наша
    # конвенція, вгадувати не беремось.
    if len(parts) < 3 or not all(parts):
        return ParsedBlank()
    serial_match = _SERIAL_RE.match(parts[-1])
    if serial_match is None:
        return ParsedBlank()

    head, middle = parts[0], parts[1:-1]
    brand_word = None
    if len(middle) >= 2 and _BRAND_WORD_RE.match(middle[0]):
        brand_word, middle = middle[0], middle[1:]

    if head.isdigit():
        # Формат A: голова — сама висота, матеріалу в ній немає.
        height = int(head) if len(head) <= 3 else None
        code = None
    else:
        height_match = _HEAD_HEIGHT_RE.search(head)
        height = int(height_match.group("height")) if height_match else None
        code = _head_code(head)

    return ParsedBlank(
        height=height,
        brand=_clip(brand_word or code),
        shade=_clip("-".join(middle)),
        serial=int(serial_match.group("serial")),
    )


def _clip(value: Optional[str]) -> Optional[str]:
    """Обрізати під ширину колонки й звести регістр.

    SQLite довжину не перевіряє й мовчки проковтне будь-що, але 180-символьний
    «колір» у таблиці на екрані — це вже зламана верстка, а на строгішій базі
    був би збій запису.
    """
    cleaned = (value or "").strip().lower()
    return cleaned[:60] or None


@dataclass(frozen=True)
class FoundBlank:
    """Файл, побачений на диску цього проходу."""

    rel_path: str
    material_dir: str
    height_dir: str
    file_name: str
    parsed: ParsedBlank

    @property
    def height_mismatch(self) -> bool:
        """Висота в назві не збіглася з текою.

        Власник підтвердив, що такого бути не повинно: якщо тека 12, диск має
        починатися з 12. Отже це помилка розкладання, і CRM її ПОКАЗУЄ, а не
        мовчить — мовчання тут прирівняло б помилку до норми.
        """
        if self.parsed.height is None:
            return False
        digits = re.sub(r"\D", "", self.height_dir)
        if not digits:
            return False
        return int(digits) != self.parsed.height


def scan_blanks(root: Path | str) -> list[FoundBlank]:
    """Обійти `<корінь>/<матеріал>/<висота>/*.blk`.

    Рівно два рівні тек — так у цеху: усередині висоти лежать одразу файли.
    Глибші рівні ігноруємо тихо: чужа тека поруч не має ламати прохід.
    """
    base = Path(root)
    if not base.is_dir():
        return []
    found: list[FoundBlank] = []
    try:
        material_dirs = [e for e in os.scandir(base) if e.is_dir()]
    except OSError:
        logger.exception("Не вдалось прочитати теку заготовок %s", base)
        return []
    for material in material_dirs:
        try:
            height_dirs = [e for e in os.scandir(material.path) if e.is_dir()]
        except OSError:
            logger.exception("Не вдалось прочитати теку матеріалу %s", material.path)
            continue
        for height in height_dirs:
            try:
                entries = list(os.scandir(height.path))
            except OSError:
                logger.exception("Не вдалось прочитати теку висоти %s", height.path)
                continue
            for entry in entries:
                if not entry.is_file():
                    continue
                if not entry.name.lower().endswith(BLANK_EXT):
                    continue
                found.append(
                    FoundBlank(
                        rel_path=f"{material.name}/{height.name}/{entry.name}",
                        material_dir=material.name,
                        height_dir=height.name,
                        file_name=entry.name,
                        parsed=parse_blank_name(entry.name),
                    )
                )
    return found


# ── Проба теки: подивитись, нічого не записавши ─────────────────────────────
# Перед тим як вмикати стеження на робочому ПК, треба переконатись, що
# розбір назв узагалі влучає в реальні файли. Проба НІЧОГО не пише в базу:
# вона читає теку й повертає звіт, який можна прочитати очима і переслати.
#
# Це також єдине місце, де ми МІРЯЄМО ціну проходу. Обіцянка «буде дешево»
# без числа нічого не варта, а тека може виявитись більшою, ніж ми думали.


@dataclass
class BlanksProbe:
    """Звіт проби: що знайшли, що зрозуміли, і що не зрозуміли."""

    root: str = ""
    exists: bool = False
    error: str = ""
    files: int = 0
    parsed: int = 0
    unparsed: int = 0
    mismatched: int = 0
    material_dirs: list = field(default_factory=list)
    height_dirs: list = field(default_factory=list)
    brands: list = field(default_factory=list)
    shades: list = field(default_factory=list)
    # Саме це найцінніше для діагностики: назви, які розбір НЕ зрозумів.
    unparsed_samples: list = field(default_factory=list)
    mismatch_samples: list = field(default_factory=list)
    sample_names: list = field(default_factory=list)
    elapsed_ms: int = 0

    def as_text(self) -> str:
        """Звіт одним шматком тексту — щоб скопіювати й переслати."""
        if self.error:
            return f"Тека: {self.root}\nПОМИЛКА: {self.error}"
        if not self.exists:
            return f"Тека: {self.root}\nНЕ ЗНАЙДЕНА (перевірте шлях)"
        lines = [
            f"Тека: {self.root}",
            f"Файлів .blk: {self.files} · розібрано {self.parsed} · НЕ розібрано {self.unparsed}",
            f"Прохід: {self.elapsed_ms} мс",
            f"Теки матеріалів ({len(self.material_dirs)}): {', '.join(self.material_dirs) or '—'}",
            f"Теки висот ({len(self.height_dirs)}): {', '.join(self.height_dirs) or '—'}",
            f"Виробники ({len(self.brands)}): {', '.join(self.brands) or '—'}",
            f"Кольори ({len(self.shades)}): {', '.join(self.shades) or '—'}",
        ]
        if self.mismatched:
            lines.append(f"Висота ≠ тека: {self.mismatched}")
            lines.extend(f"  ! {name}" for name in self.mismatch_samples)
        if self.unparsed:
            lines.append("НЕ РОЗІБРАНІ назви:")
            lines.extend(f"  ? {name}" for name in self.unparsed_samples)
        if self.sample_names:
            lines.append("Приклади розібраних:")
            lines.extend(f"  · {name}" for name in self.sample_names)
        return "\n".join(lines)


# Скільки прикладів показувати. Звіт має лишатись читабельним і на теці з
# десятками тисяч файлів — переслати простирадло на 50 тис. рядків не вийде.
PROBE_SAMPLES = 25


def probe_blanks(root: Path | str) -> BlanksProbe:
    """Прочитати теку й описати, що з неї вийшло. У базу НЕ пише."""
    started = monotonic()
    probe = BlanksProbe(root=str(root))
    base = Path(root)
    if not str(root).strip():
        probe.error = "шлях не задано"
        return probe
    try:
        probe.exists = base.is_dir()
    except OSError as exc:
        probe.error = str(exc)
        return probe
    if not probe.exists:
        return probe

    try:
        found = scan_blanks(base)
    except OSError as exc:  # pragma: no cover — scan_blanks уже ковтає OSError
        probe.error = str(exc)
        return probe

    materials, heights, brands, shades = set(), set(), set(), set()
    for item in found:
        probe.files += 1
        materials.add(item.material_dir)
        heights.add(item.height_dir)
        if item.parsed.height is None or not item.parsed.brand:
            probe.unparsed += 1
            if len(probe.unparsed_samples) < PROBE_SAMPLES:
                probe.unparsed_samples.append(item.rel_path)
            continue
        probe.parsed += 1
        brands.add(item.parsed.brand)
        if item.parsed.shade:
            shades.add(item.parsed.shade)
        if item.height_mismatch:
            probe.mismatched += 1
            if len(probe.mismatch_samples) < PROBE_SAMPLES:
                probe.mismatch_samples.append(
                    f"{item.rel_path} (тека {item.height_dir}, назва {item.parsed.height})"
                )
        elif len(probe.sample_names) < PROBE_SAMPLES:
            probe.sample_names.append(item.rel_path)

    probe.material_dirs = sorted(materials)
    probe.height_dirs = sorted(heights, key=lambda x: (len(x), x))
    probe.brands = sorted(brands)
    probe.shades = sorted(shades)
    probe.elapsed_ms = int((monotonic() - started) * 1000)
    return probe


@dataclass
class SyncBlanksResult:
    appeared: int = 0
    vanished: int = 0
    present: int = 0
    # Скільки рядків цей прохід записав як ВЖЕ ЗАМОВЛЕНІ, бо вони лежали в
    # теці ще до вмикання стеження. Ненульове буває рівно один раз — на
    # першому проході (див. sync_blanks).
    baseline: int = 0
    # Незамовлені рядки, чиї поля переписано новішим розбором назви.
    reparsed: int = 0
    # Теки за шляхом немає (або вона недоступна) — прохід нічого не змінив.
    missing: bool = False


@dataclass
class LastScan:
    """Останній прохід теки в ЦЬОМУ процесі — для смуги внизу екрана.

    Зелена крапка там ставиться лише з підтвердження (CLAUDE.md §14, плити):
    «шлях заповнено» — не сигнал, «прохід щойно побачив теку» — сигнал. У
    памʼяті, а не в базі: після рестарту крапка сіра, доки воркер не пройде
    теку (25 с), — і це правда.
    """

    at: Optional[datetime] = None
    ok: bool = False
    present: int = 0
    root: str = ""


_last_scan = LastScan()


def last_scan() -> LastScan:
    return _last_scan


def sync_blanks(db: Session, root: Path | str, *, now: Optional[datetime] = None) -> SyncBlanksResult:
    """Звести побачене на диску з тим, що вже знає база.

    Три випадки:
      * файл є на диску, а живого рядка немає → ЗʼЯВИВСЯ (новий диск);
      * живий рядок є, а файлу на диску немає → ЗНИК (дороблено чи підчистили);
      * решта — без змін.

    «Живий» = рядок із порожнім `gone_at`. Саме тому повторена назва після
    підчистки дає новий рядок: старий уже позначений як зниклий.

    Теки НЕМАЄ — прохід не робить нічого. Раніше відсутня тека читалась як
    порожня: усі живі рядки ставали зниклими, а коли тека поверталась
    (помилка в шляху, виправлена за хвилину), кожен її файл зʼявлявся
    «новим» — і в список до замовлення падали всі 19 тисяч дисків точки
    відліку. Відсутня тека і порожня тека — різні речі.
    """
    global _last_scan
    now = now or datetime.now()
    try:
        exists = Path(root).is_dir()
    except OSError:
        exists = False
    if not exists:
        _last_scan = LastScan(at=now, ok=False, present=0, root=str(root))
        return SyncBlanksResult(missing=True)
    found = scan_blanks(root)
    result = SyncBlanksResult(present=len(found))
    _last_scan = LastScan(at=now, ok=True, present=len(found), root=str(root))

    # ПЕРШИЙ прохід — це база відліку, а не вантаж замовлень.
    #
    # У теці лежать диски, накопичені РОКАМИ (номери доходили до 891 у групі,
    # старі не прибирали). Якби ми порахували їх як «щойно взяті», перше ж
    # натискання «Перечитати теку» дало б комірниці замовлення на десятки
    # тисяч дисків — перевірено: 900 файлів перетворювались на рядок
    # «Mono a2 12(300)+18(300)+25(300)». Фіча була б непридатна з першої
    # секунди, а надісланий такий список — ще й соромно.
    #
    # Тому все, що вже лежить у теці на момент вмикання, одразу позначаємо
    # замовленим. Узятим рахується лише те, що зʼявилось ПІСЛЯ.
    #
    # Умова саме «таблиця порожня», а не «немає живих рядків»: після повної
    # підчистки теки всі рядки стають зниклими, але база відліку вже є, і
    # другий раз її ставити не можна.
    is_first_run = not db.scalar(select(func.count()).select_from(CamBlank))

    # Ключ у НИЖНЬОМУ регістрі. Windows не розрізняє регістр у назвах, тож
    # перейменування `12-Mono-A2-x1` → `12-mono-a2-x1` це ТОЙ САМИЙ файл.
    # З чутливим до регістру ключем воно виглядало б як «старий зник, новий
    # зʼявився», і в замовлення комірниці потрапляв би диск, якого немає
    # (перевірено наживо 08.09.26: appeared=1, vanished=1 на самому лише
    # перейменуванні регістру).
    alive: dict[str, CamBlank] = {
        row.rel_path.casefold(): row
        for row in db.scalars(select(CamBlank).where(CamBlank.gone_at.is_(None))).all()
    }
    seen_paths = set()

    for item in found:
        key = item.rel_path.casefold()
        seen_paths.add(key)
        if key in alive:
            # Поля розбору пишуться в момент появи диска. Коли розбір
            # навчився новому формату, рядки, що вже чекають замовлення,
            # лишились би з полями старого (08–10.09.26: «zr, колір 5» замість
            # «monolith a3-5») — і в таблиці, і в прапорці «не на місці».
            # Перечитуємо лише НЕЗАМОВЛЕНІ: їх одиниці-десятки, а замовлену
            # історію текст однаково розбирає з назви заново (`_line_fields`).
            if alive[key].ordered_at is None and _refresh_parsed(alive[key], item):
                result.reparsed += 1
            continue
        db.add(
            CamBlank(
                rel_path=item.rel_path[:400],
                material_dir=item.material_dir[:60],
                height_dir=item.height_dir[:20],
                file_name=item.file_name[:200],
                height=item.parsed.height,
                brand=item.parsed.brand,
                shade=item.parsed.shade,
                serial=item.parsed.serial,
                height_mismatch=item.height_mismatch,
                first_seen_at=now,
                # База відліку: те, що вже лежало, замовляти не треба.
                ordered_at=now if is_first_run else None,
            )
        )
        if is_first_run:
            result.baseline += 1
        else:
            result.appeared += 1
        # Перший прохід на робочому ПК може принести десятки тисяч рядків.
        # Однією транзакцією це тримало б базу зайнятою кілька секунд, а
        # поруч щохвилини пишуть покази верстатів і печей.
        if (result.appeared + result.baseline) % _COMMIT_EVERY == 0:
            db.commit()

    for key, row in alive.items():
        if key not in seen_paths:
            row.gone_at = now
            result.vanished += 1

    if result.appeared or result.vanished or result.baseline or result.reparsed:
        db.commit()
    return result


def _refresh_parsed(row: CamBlank, item: FoundBlank) -> bool:
    """Переписати поля розбору, якщо поточний розбір дає інше. True — змінено."""
    fresh = {
        "height": item.parsed.height,
        "brand": item.parsed.brand,
        "shade": item.parsed.shade,
        "serial": item.parsed.serial,
        "height_mismatch": item.height_mismatch,
    }
    changed = False
    for name, value in fresh.items():
        if getattr(row, name) != value:
            setattr(row, name, value)
            changed = True
    return changed


def real_disc_clause():
    """SQL-умова «це взятий диск, а не точка відліку».

    Перше читання теки ставить `ordered_at = first_seen_at` усьому, що вже
    лежало (див. `sync_blanks`) — роки історії, які ніхто не брав сьогодні.
    Такі рядки не показуються ніде: ні в «Усіх створених дисках», ні в
    графіку, ні в лічильниках (рішення власника 10.09.26). Одна функція на всі
    місця — розійдуться, і лічильник вкладки перестане сходитись зі списком.
    """
    return CamBlank.ordered_at.is_(None) | (CamBlank.ordered_at != CamBlank.first_seen_at)


def pending_blanks(db: Session) -> list[CamBlank]:
    """Диски, ще не замовлені на склад, — те, що лежить у лівій колонці.

    Вікно рахується від позначки «замовлено», а НЕ від робочої доби: склад
    закривається о 18:00, далі нічна зміна бере диски з архіву, а у вихідні
    склад не працює взагалі — межа 07:30 відрізала б саме це. Позначка ж
    переживає і вихідні, і забутий день без календарної логіки.

    Зниклі файли тут лишаються: диск усе одно взяли, і замовити його треба,
    навіть якщо його вже дороблено.
    """
    return list(
        db.scalars(
            select(CamBlank)
            .where(CamBlank.ordered_at.is_(None))
            .order_by(CamBlank.first_seen_at, CamBlank.id)
        ).all()
    )


# Скільки незамовлених дисків уже виглядає як «забули замовити».
#
# Поріг за КІЛЬКІСТЮ, а не за часом, і це принципово. Часовий поріг здавався
# природнішим («найстаршому вже дві доби»), але він давав би хибну тривогу
# КОЖНОГО ПОНЕДІЛКА: склад не працює у вихідні, а оператори працюють, тож
# у понеділок найстаршому диску законно 72 години. Правило «хибний сигнал
# гірший за жодного» тут вирішує на користь кількості.
#
# Число взяте з цеху: нових дисків 0-10 на день, отже за довгі вихідні
# набирається до ~30. 45 — це вже більше, ніж будь-який нормальний проміжок
# між замовленнями.
BLANKS_PILEUP = 45


def pileup_note(rows: list[CamBlank]) -> Optional[str]:
    """Підказка, коли список виріс понад будь-який звичайний проміжок.

    Не тривога й не блокування: оператор міг просто не замовляти три дні.
    Просто називаємо число й найстаршу дату, щоб рішення було зрячим — а не
    щоб на склад випадково пішло простирадло на сотню рядків.
    """
    if len(rows) < BLANKS_PILEUP:
        return None
    oldest = min(row.first_seen_at for row in rows)
    return (
        f"У списку {len(rows)} дисків, найстарішого взято {oldest:%d.%m}. "
        "Це більше, ніж набирається навіть за довгі вихідні — схоже, "
        "замовлення давно не робили."
    )


# ── Позиція диска: mono-a2-25(х2) ───────────────────────────────────────────
# Формат задав власник 10.09.26 (бриф «Нові диски»):
#
#   mono-a3-18
#   mono-a2-25(х2)
#
#   pmma-a2-20
#
# Виробник-колір-висота через дефіс, малими; кількість у дужках кириличною
# «х» і лише коли однакових дисків більше одного. Групи матеріалів —
# цирконій, потім ПММА·PEEK, потім решта, — розділені порожнім рядком.
# Той самий текст на екрані, у буфері й у Telegram: склад читає рівно те,
# що бачив оператор. Нерозібраний файл іде сирою назвою без `.blk`.
#
# До 10.09.26 формат був «mono a3 18(2)» — через пробіл і без групування.

# КИРИЛИЧНА «х» (U+0445), не латинська x: так пише власник, і так склад
# звик читати. Окремою константою, щоб її не «виправили» при правці рядка.
COUNT_MARK = "х"

MATERIAL_ZR = "zr"
MATERIAL_PMMA = "pmma"
# Мітка в рядку й заголовок групи. Решта матеріалів (CoCr, титан…) іде
# групою з назвою своєї теки — вгадувати їй людську назву не беремось.
_GROUP_META = {
    MATERIAL_ZR: ("ZR", "Цирконій"),
    MATERIAL_PMMA: ("PMMA", "ПММА · PEEK"),
}
_GROUP_ORDER = (MATERIAL_ZR, MATERIAL_PMMA)


def bare_name(name: str) -> str:
    """Назва файлу без `.blk` — так її показуємо скрізь (рішення власника)."""
    return name[: -len(BLANK_EXT)] if name.lower().endswith(BLANK_EXT) else name


def _brand_label(brand: Optional[str]) -> str:
    """Виробник так, як його пише оператор: `monolith` → `mono`, `emotions` → `emo`."""
    if not brand:
        return "?"
    lowered = brand.strip().lower()
    if lowered.startswith("mono"):
        return "mono"
    if lowered.startswith("emo"):
        return "emo"
    return lowered


def shade_label(shade: Optional[str]) -> str:
    """Колір так, як його пишуть люди: `a3-5` → `a3.5` (дефіс — лише з
    файлової назви, де крапка небажана)."""
    cleaned = (shade or "").strip().lower()
    return re.sub(r"^([a-d])(\d)-(\d)$", r"\1\2.\3", cleaned)


def _line_fields(row: CamBlank) -> tuple[Optional[int], Optional[str], Optional[str]]:
    """Висота, виробник, колір — з НАЗВИ файлу поточним розбором.

    Поля в базі пишуться в момент появи диска, тобто тим розбором, що був
    тоді. Історія замовлень, зроблених до того, як розбір навчився формату C,
    інакше так і лишилась би з «zr 5 14» замість «mono a3.5 14». Рядок без
    назви (або з назвою, що не розбирається) — беремо збережені поля.
    """
    if row.file_name:
        parsed = parse_blank_name(row.file_name)
        if parsed.height is not None and parsed.brand:
            return parsed.height, parsed.brand, parsed.shade
    return row.height, row.brand, row.shade


def material_group(row: CamBlank) -> str:
    """Група матеріалу диска: `zr`, `pmma` або `dir:<тека>` для решти.

    Головна ознака — ТЕКА матеріалу (`ZR`, `PMMA-PEEK`): її ставить CAM, а
    не людина. Код у назві (`zr25_25`, `pmma25_25`) — запасний, коли тека
    порожня.
    """
    folder = (row.material_dir or "").strip()
    probe = folder.casefold()
    if not probe:
        _, brand, _ = _line_fields(row)
        probe = (row.brand or brand or "").casefold()
    if probe.startswith(("zr", "zir", "цир")):
        return MATERIAL_ZR
    if "pmma" in probe or "peek" in probe or probe.startswith("пмма"):
        return MATERIAL_PMMA
    return f"dir:{folder.casefold()}" if folder else "dir:"


def group_meta(group: str, folder: str = "") -> tuple[str, str]:
    """(мітка, заголовок) групи матеріалу."""
    if group in _GROUP_META:
        return _GROUP_META[group]
    name = folder.strip() or group.partition(":")[2] or "—"
    return name.upper()[:8], name


def _natural(text: str) -> tuple:
    """`a3` < `a3.5` < `a10`: числа порівнюються як числа."""
    parts = re.split(r"(\d+)", text or "")
    return tuple(int(p) if i % 2 else p for i, p in enumerate(parts))


@dataclass(frozen=True)
class DiscPos:
    """Позиція диска в замовленні — `mono-a2-25` або сира назва файлу."""

    key: str
    raw: bool
    brand: str = ""
    shade: str = ""
    height: Optional[int] = None
    group: str = ""
    tag: str = ""
    title: str = ""

    @property
    def sort_key(self) -> tuple:
        # Розібрані — за виробником, кольором, висотою; сирі — у кінці групи.
        if self.raw:
            return (1, (), (), 0, self.key.casefold())
        return (0, self.brand, _natural(self.shade), self.height or 0, "")


def disc_position(row: CamBlank) -> DiscPos:
    height, brand, shade = _line_fields(row)
    group = material_group(row)
    tag, title = group_meta(group, row.material_dir or "")
    if height is None or not brand:
        name = row.file_name or row.rel_path.rsplit("/", 1)[-1]
        return DiscPos(key=bare_name(name), raw=True, group=group, tag=tag, title=title)
    brand_l, shade_l = _brand_label(brand), shade_label(shade)
    key = "-".join(p for p in (brand_l, shade_l, str(height)) if p)
    return DiscPos(
        key=key, raw=False, brand=brand_l, shade=shade_l, height=height,
        group=group, tag=tag, title=title,
    )


def disc_needs_look(row: CamBlank) -> bool:
    """Диск просить погляду: назву не розібрано або висота ≠ тека."""
    height, brand, _ = _line_fields(row)
    return height is None or not brand or bool(row.height_mismatch)


@dataclass(frozen=True)
class OrderLine:
    """Одна позиція замовлення: `mono-a2-25(х2)`."""

    pos: DiscPos
    count: int
    ids: tuple[int, ...]
    # Хоч один диск позиції просить погляду (див. `disc_needs_look`).
    warn: bool = False

    @property
    def text(self) -> str:
        return with_count(self.pos.key, self.count)


def with_count(key: str, count: int) -> str:
    return f"{key}({COUNT_MARK}{count})" if count > 1 else key


@dataclass(frozen=True)
class OrderGroup:
    """Позиції одного матеріалу — блок замовлення між порожніми рядками."""

    group: str
    tag: str
    title: str
    lines: tuple[OrderLine, ...]

    @property
    def count(self) -> int:
        return sum(line.count for line in self.lines)


def order_groups(rows: Iterable[CamBlank]) -> list[OrderGroup]:
    """Диски → групи матеріалів → позиції. Цирконій, ПММА, далі решта."""
    by_group: dict[str, dict[str, list[CamBlank]]] = {}
    positions: dict[tuple[str, str], DiscPos] = {}
    for row in rows:
        pos = disc_position(row)
        by_group.setdefault(pos.group, {}).setdefault(pos.key, []).append(row)
        positions.setdefault((pos.group, pos.key), pos)

    def group_rank(group: str) -> tuple:
        if group in _GROUP_ORDER:
            return (_GROUP_ORDER.index(group), "")
        return (len(_GROUP_ORDER), group)

    groups = []
    for group in sorted(by_group, key=group_rank):
        lines = []
        for key, discs in by_group[group].items():
            lines.append(OrderLine(
                pos=positions[(group, key)],
                count=len(discs),
                ids=tuple(sorted(row.id for row in discs if row.id is not None)),
                warn=any(disc_needs_look(row) for row in discs),
            ))
        lines.sort(key=lambda line: line.pos.sort_key)
        first = lines[0].pos
        groups.append(OrderGroup(group=group, tag=first.tag, title=first.title, lines=tuple(lines)))
    return groups


def order_text(rows: Iterable[CamBlank], note: str = "") -> str:
    """Готовий текст замовлення — у буфер, у Telegram і в історію.

    Групи матеріалів розділені порожнім рядком; дописане від руки — окремим
    блоком у кінці. Замовлення може складатися лише з дописаного.
    """
    blocks = ["\n".join(line.text for line in group.lines) for group in order_groups(rows)]
    extra = (note or "").strip()
    if extra:
        blocks.append(extra)
    return "\n\n".join(blocks)


def order_positions(rows: Iterable[CamBlank]) -> int:
    """Скільки позицій (рядків) дасть замовлення з цих дисків."""
    return sum(len(group.lines) for group in order_groups(rows))


# ── Зміни ───────────────────────────────────────────────────────────────────
# Незамовлене розкладено по змінах, а не по календарних днях (бриф «Нові
# диски»): денна 07:30–18:00, нічна 18:00–07:30. Межі підтвердив власник
# 10.09.26 — 18:00 це закриття складу, 07:30 — початок денної зміни. Нічний
# диск о 01:09 належить нічній зміні, що почалась учора о 18:00.

DAY_SHIFT_START = time(7, 30)
NIGHT_SHIFT_START = time(18, 0)
WEEKDAYS_UK = ("пн", "вт", "ср", "чт", "пт", "сб", "нд")


@dataclass(frozen=True)
class Shift:
    kind: str  # "day" | "night"
    start: datetime
    end: datetime

    @property
    def key(self) -> str:
        return f"{self.kind[0]}{self.start:%Y%m%d}"

    @property
    def title(self) -> str:
        return "Денна зміна" if self.kind == "day" else "Нічна зміна"

    @property
    def span(self) -> str:
        return "07:30–18:00" if self.kind == "day" else "18:00–07:30"

    @property
    def when(self) -> str:
        """`чт 10.09` для денної, `ср 09.09 → чт 10.09` для нічної."""
        head = f"{WEEKDAYS_UK[self.start.weekday()]} {self.start:%d.%m}"
        if self.kind == "day":
            return head
        return f"{head} → {WEEKDAYS_UK[self.end.weekday()]} {self.end:%d.%m}"

    @property
    def short(self) -> str:
        """Підпис у колонці історії: `денна 10.09`, `нічна 09.09→10.09`."""
        if self.kind == "day":
            return f"денна {self.start:%d.%m}"
        return f"нічна {self.start:%d.%m}→{self.end:%d.%m}"


def shift_of(moment: datetime) -> Shift:
    day = moment.date()
    clock = moment.time()
    if clock < DAY_SHIFT_START:
        start_day = day - timedelta(days=1)
        return Shift(
            "night",
            datetime.combine(start_day, NIGHT_SHIFT_START),
            datetime.combine(day, DAY_SHIFT_START),
        )
    if clock >= NIGHT_SHIFT_START:
        return Shift(
            "night",
            datetime.combine(day, NIGHT_SHIFT_START),
            datetime.combine(day + timedelta(days=1), DAY_SHIFT_START),
        )
    return Shift(
        "day",
        datetime.combine(day, DAY_SHIFT_START),
        datetime.combine(day, NIGHT_SHIFT_START),
    )


@dataclass(frozen=True)
class ShiftGroup:
    shift: Shift
    rows: tuple[CamBlank, ...]
    live: bool = False


def shift_groups(rows: Iterable[CamBlank], *, now: Optional[datetime] = None) -> list[ShiftGroup]:
    """Диски по змінах; свіжа зміна згори, у зміні свіжі диски згори."""
    now = now or datetime.now()
    by_key: dict[str, list[CamBlank]] = {}
    shifts: dict[str, Shift] = {}
    for row in rows:
        shift = shift_of(row.first_seen_at)
        by_key.setdefault(shift.key, []).append(row)
        shifts.setdefault(shift.key, shift)
    groups = []
    for key, discs in by_key.items():
        shift = shifts[key]
        discs.sort(key=lambda row: (row.first_seen_at, row.id), reverse=True)
        groups.append(ShiftGroup(shift=shift, rows=tuple(discs), live=shift.start <= now < shift.end))
    groups.sort(key=lambda group: group.shift.start, reverse=True)
    return groups


def shifts_label(rows: Iterable[CamBlank]) -> str:
    """З яких змін диски замовлення — підпис у колонці історії.

    Дві зміни пишемо обидві; більше (вихідні, забуте замовлення) — числом і
    межами, інакше колонка перетворилась би на абзац.
    """
    shifts: dict[str, Shift] = {}
    for row in rows:
        shift = shift_of(row.first_seen_at)
        shifts.setdefault(shift.key, shift)
    ordered = sorted(shifts.values(), key=lambda shift: shift.start)
    if not ordered:
        return ""
    if len(ordered) <= 2:
        return " + ".join(shift.short for shift in ordered)
    first, last = ordered[0], ordered[-1]
    n = len(ordered)
    word = "зміни" if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14) else "змін"
    return f"{n} {word} · {first.start:%d.%m}–{last.start:%d.%m}"


# ── Усі створені диски (журнал) ────────────────────────────────────────────


def real_discs(db: Session) -> list[CamBlank]:
    """Усі взяті диски без точки відліку, свіжі першими."""
    return list(
        db.scalars(
            select(CamBlank)
            .where(real_disc_clause())
            .order_by(CamBlank.first_seen_at.desc(), CamBlank.id.desc())
        ).all()
    )
