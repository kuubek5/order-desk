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
# Розбираємо СПРАВА, бо саме хвіст стабільний: останній блок — номер, перед
# ним — колір, решта — голова з матеріалом і висотою. Голова змінюється від
# машини до машини, хвіст — ні.
_NAME_A_RE = re.compile(
    r"^(?P<height>\d{1,3})\s*-\s*(?P<brand>[^-]+?)\s*-\s*(?P<shade>.+?)\s*-\s*[xX](?P<serial>\d+)$"
)
_NAME_B_RE = re.compile(
    r"^(?P<head>.+?)\s*-\s*(?P<shade>[^-]+?)\s*-\s*[xX]?(?P<serial>\d+)$"
)
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
    """Назва файлу → висота, матеріал, колір, номер. Розуміє обидва формати.

    Не розібралось — повертаємо порожній ParsedBlank, а не None: викликач
    однаково мусить порахувати файл, просто без структурованих полів.

    `brand` для формату B — це КОД МАТЕРІАЛУ з назви (`zr`, `pmma`, `crco`,
    `ti`), бо виробника ці назви не несуть узагалі. Для комірниці рядок
    «Zr a2 25(3)» усе одно кращий за сире імʼя файлу, а справжній виробник
    видно з теки матеріалу.
    """
    stem = name[: -len(BLANK_EXT)] if name.lower().endswith(BLANK_EXT) else name
    stem = stem.strip()

    match = _NAME_A_RE.match(stem)
    if match is not None:
        try:
            height = int(match.group("height"))
            serial = int(match.group("serial"))
        except ValueError:  # pragma: no cover — регулярка вже гарантує цифри
            return ParsedBlank()
        return ParsedBlank(
            height=height,
            brand=_clip(match.group("brand")),
            shade=_clip(match.group("shade")),
            serial=serial,
        )

    match = _NAME_B_RE.match(stem)
    if match is None:
        return ParsedBlank()
    try:
        serial = int(match.group("serial"))
    except ValueError:  # pragma: no cover
        return ParsedBlank()
    head = match.group("head").strip()
    height_match = _HEAD_HEIGHT_RE.search(head)
    return ParsedBlank(
        height=int(height_match.group("height")) if height_match else None,
        brand=_clip(_head_code(head)),
        shade=_clip(match.group("shade")),
        serial=serial,
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

    if result.appeared or result.vanished or result.baseline:
        db.commit()
    return result


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
# Формат списаний з реальних повідомлень власника (Viber, чат з комірницею):
#
#   Mono a2-18+25
#   Mono a3.5 18+20+25
#   Прозора пмма 20(3)+25(3)
#
# Рядок = виробник + колір; ВИСОТИ через «+» (по одному диску кожної);
# кількість у дужках лише коли їх більше однієї. Це читає людина, яка звикла
# саме до такого вигляду, тож формат має бути їй ВПІЗНАВАНИЙ, а не «правильний».


def _brand_label(brand: Optional[str]) -> str:
    """Виробник так, як його пише оператор: `monolith` → `Mono`, `emo` → `Emo`."""
    if not brand:
        return "?"
    lowered = brand.strip().lower()
    if lowered.startswith("mono"):
        return "Mono"
    if lowered.startswith("emo"):
        return "Emo"
    return lowered.capitalize()


def order_text(rows: Iterable[CamBlank]) -> str:
    """Готовий текст замовлення — той, що йде в буфер обміну.

    Нерозібрані файли не ховаємо: вони теж узяті диски. Йдуть окремими
    рядками сирою назвою, щоб оператор вирішив сам.
    """
    groups: dict[tuple[str, str], list[int]] = {}
    unparsed: list[str] = []
    for row in rows:
        if row.height is None or not row.brand:
            unparsed.append(row.file_name or row.rel_path)
            continue
        key = (_brand_label(row.brand), (row.shade or "").strip())
        groups.setdefault(key, []).append(row.height)

    lines: list[str] = []
    for (brand, shade), heights in groups.items():
        parts: list[str] = []
        for height in sorted(set(heights)):
            count = heights.count(height)
            parts.append(f"{height}({count})" if count > 1 else str(height))
        head = f"{brand} {shade}".strip()
        lines.append(f"{head} {'+'.join(parts)}")
    lines.extend(unparsed)
    return "\n".join(lines)
