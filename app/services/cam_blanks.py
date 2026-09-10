"""Заготовки з теки CAM: що взяли з архіву і що замовити комірниці.

**Навіщо.** Щодня до 17:30 оператор обходив шухляди, дивився, чого бракує, і
писав комірниці у Viber — близько десяти хвилин, кожен день. Але коли він
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
from datetime import datetime
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


def sync_blanks(db: Session, root: Path | str, *, now: Optional[datetime] = None) -> SyncBlanksResult:
    """Звести побачене на диску з тим, що вже знає база.

    Три випадки:
      * файл є на диску, а живого рядка немає → ЗʼЯВИВСЯ (новий диск);
      * живий рядок є, а файлу на диску немає → ЗНИК (дороблено чи підчистили);
      * решта — без змін.

    «Живий» = рядок із порожнім `gone_at`. Саме тому повторена назва після
    підчистки дає новий рядок: старий уже позначений як зниклий.
    """
    now = now or datetime.now()
    found = scan_blanks(root)
    result = SyncBlanksResult(present=len(found))

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


def pending_blanks(db: Session) -> list[CamBlank]:
    """Диски, взяті ПІСЛЯ останнього замовлення — те, що треба замовити.

    Вікно рахується від позначки «замовлено», а НЕ від робочої доби. Причина
    в цеху: комірниця йде о 18:00, далі нічна зміна бере диски з архіву, а у
    вихідні комірниці немає взагалі — межа 07:30 відрізала б саме це.
    Позначка ж переживає і вихідні, і забутий день без календарної логіки.

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


# Скільки незамовлених дисків уже виглядає як «забули натиснути «Замовлено».
#
# Поріг за КІЛЬКІСТЮ, а не за часом, і це принципово. Часовий поріг здавався
# природнішим («найстаршому вже дві доби»), але він давав би хибну тривогу
# КОЖНОГО ПОНЕДІЛКА: комірниця не працює у вихідні, а оператори працюють, тож
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
    щоб оператор випадково скопіював комірниці простирадло на сотню рядків.
    """
    if len(rows) < BLANKS_PILEUP:
        return None
    oldest = min(row.first_seen_at for row in rows)
    return (
        f"У списку {len(rows)} дисків, найстарішого взято {oldest:%d.%m}. "
        "Це більше, ніж набирається навіть за довгі вихідні — схоже, "
        "«Замовлено» давно не натискали."
    )


def mark_ordered(db: Session, *, now: Optional[datetime] = None) -> int:
    """Позначити все незамовлене як замовлене. Повертає скільки саме."""
    now = now or datetime.now()
    rows = pending_blanks(db)
    for row in rows:
        row.ordered_at = now
    if rows:
        db.commit()
    return len(rows)


def undo_last_order(db: Session) -> int:
    """Скасувати ОСТАННЄ «Замовлено». Повертає, скільки дисків повернулось.

    Навіщо. Кнопка «Замовлено» починає нове вікно й миттєво спорожняє список —
    а натиснути її випадково легко (натиснуто помилково 08.09.26). Без відкату
    диски, взяті з архіву, зникають із замовлення НАЗАВЖДИ: рядки лишаються, але
    вже позначені замовленими, і комірниця їх не побачить. Це втрата роботи,
    зробленої за день, від одного зайвого кліку.

    ЯК ВІДРІЗНЯЄМО ПАЧКУ. Усі рядки одного натискання ділять точний час
    `ordered_at` — його ставить один виклик `mark_ordered`. Тому відкат бере
    найбільший такий час і чистить рівно його.

    ЧОМУ ЦЕ НЕ ЧІПАЄ ТОЧКУ ВІДЛІКУ. Перший прохід теж проставляє `ordered_at`
    (інакше 19 тисяч давніх дисків потрапили б у перше ж замовлення), і
    скасувати ЙОГО означало б вивалити комірниці всю історію теки. Такі рядки
    видно за ознакою: у них `ordered_at` дорівнює `first_seen_at`, бо їх
    проставили в ту саму мить, коли вперше побачили. Пачка справжнього
    замовлення завжди пізніша за появу диска. Тому умова `ordered_at !=
    first_seen_at` і є захистом, а не косметикою.
    """
    last = db.scalar(
        select(func.max(CamBlank.ordered_at)).where(
            CamBlank.ordered_at.is_not(None),
            CamBlank.ordered_at != CamBlank.first_seen_at,
        )
    )
    if last is None:
        return 0
    rows = list(
        db.scalars(
            select(CamBlank).where(
                CamBlank.ordered_at == last,
                CamBlank.ordered_at != CamBlank.first_seen_at,
            )
        ).all()
    )
    for row in rows:
        row.ordered_at = None
    if rows:
        db.commit()
    return len(rows)


@dataclass(frozen=True)
class PastOrder:
    """Одне натискання «Замовлено» — як воно виглядало.

    Окремої таблиці під це немає й не треба: усі диски одного натискання
    ділять точний `ordered_at`, тож історія ВИВОДИТЬСЯ з наявних рядків.
    Заводити таблицю означало б тримати ту саму правду у двох місцях і
    ризикувати, що вони розійдуться.
    """

    ordered_at: datetime
    count: int
    text: str
    is_latest: bool = False


def order_history(db: Session, *, limit: int = 30) -> list[PastOrder]:
    """Минулі замовлення, найновіші згори.

    30, а не 12 (10.09.26): історія тепер згорнута під списком і місця на
    екрані не забирає, а питають її саме про давнє — «коли замовляли?».

    Навіщо. Кнопка «Замовлено» досі була дією без сліду: натиснув — список
    спорожнів, і що саме пішло комірниці, вже ніде не подивитись. Питання
    «а ми замовляли цирконій цього тижня?» не мало відповіді в застосунку.
    Тепер має, і заразом видно, ЩО саме поверне скасування.

    Точка відліку (перший прохід теки) сюди не потрапляє: у її рядків
    `ordered_at` дорівнює `first_seen_at`, і показувати «замовлення на 19 133
    диски», якого не було, — брехня.
    """
    stamps = list(
        db.scalars(
            select(CamBlank.ordered_at)
            .where(
                CamBlank.ordered_at.is_not(None),
                CamBlank.ordered_at != CamBlank.first_seen_at,
            )
            .group_by(CamBlank.ordered_at)
            .order_by(CamBlank.ordered_at.desc())
            .limit(limit)
        ).all()
    )
    history: list[PastOrder] = []
    for index, stamp in enumerate(stamps):
        # Умова у вибірці вище вже відсікає NULL, але типізатор про це не знає:
        # колонка оголошена нульовою. Пропуск замість `assert` — щоб дивний
        # рядок у базі не валив увесь екран налаштувань.
        if stamp is None:  # pragma: no cover — відсічено запитом вище
            continue
        rows = list(
            db.scalars(
                select(CamBlank)
                .where(CamBlank.ordered_at == stamp)
                .order_by(CamBlank.material_dir, CamBlank.height, CamBlank.file_name)
            ).all()
        )
        history.append(
            PastOrder(
                ordered_at=stamp,
                count=len(rows),
                text=order_text(rows),
                # Скасувати можна лише НАЙНОВІШЕ. Відкат старішого повернув би
                # у поточний список диски, замовлені тижні тому, і комірниця
                # отримала б їх удруге.
                is_latest=index == 0,
            )
        )
    return history


def last_order_at(db: Session) -> Optional[datetime]:
    """Коли натискали «Замовлено» востаннє. None — жодного разу.

    Потрібне екрану: кнопку скасування показуємо лише тоді, коли є що
    скасовувати, і підписуємо часом — щоб не скасувати позавчорашнє замовлення,
    думаючи, що прибираєш свій випадковий клік.
    """
    return db.scalar(
        select(func.max(CamBlank.ordered_at)).where(
            CamBlank.ordered_at.is_not(None),
            CamBlank.ordered_at != CamBlank.first_seen_at,
        )
    )


# ── Текст для комірниці ─────────────────────────────────────────────────────
# Формат задав власник 10.09.26:
#
#   mono a3 18
#   emo a2 16
#   zr a2 25(2)
#
# Рядок = виробник, колір, висота — ОДНА позиція на рядок, малими літерами;
# кількість у дужках лише коли однакових дисків більше одного. Раніше висоти
# склеювались через «+» в один рядок («Mono a2 18+25»): тоді з нього не можна
# було вибрати, що саме копіювати, — а тепер список копіюється вибірково.
# Свіжі позиції згори, як і таблиця під списком.


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


@dataclass(frozen=True)
class OrderLine:
    """Один рядок замовлення — те, що оператор бачить із галочкою і копіює."""

    text: str
    count: int
    # Коли брали диски цього рядка, найсвіжіші першими.
    taken: tuple[datetime, ...]


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


def order_lines(rows: Iterable[CamBlank]) -> list[OrderLine]:
    """Рядки замовлення, свіжі згори.

    Нерозібрані файли не ховаємо: вони теж узяті диски. Кожен іде окремим
    рядком сирою назвою, щоб оператор вирішив сам.
    """
    groups: dict[str, list[datetime]] = {}
    for row in rows:
        height, brand, shade = _line_fields(row)
        if height is None or not brand:
            text = row.file_name or row.rel_path
        else:
            text = " ".join(p for p in (_brand_label(brand), shade_label(shade), str(height)) if p)
        groups.setdefault(text, []).append(row.first_seen_at)

    lines = []
    for text, stamps in groups.items():
        taken = tuple(sorted((s for s in stamps if s is not None), reverse=True))
        count = len(stamps)
        lines.append(OrderLine(
            text=f"{text}({count})" if count > 1 else text,
            count=count,
            taken=taken,
        ))
    # Свіжі згори; нічия — за текстом, щоб порядок не стрибав між рендерами.
    lines.sort(key=lambda line: line.text)
    lines.sort(key=lambda line: line.taken[0] if line.taken else datetime.min, reverse=True)
    return lines


def order_text(rows: Iterable[CamBlank]) -> str:
    """Готовий текст замовлення — той, що йде в буфер обміну."""
    return "\n".join(line.text for line in order_lines(rows))
