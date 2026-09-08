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

**Ціна проходу.** Читаємо тільки імена: ні вмісту, ні розміру, ні дати файлу.
Тека локальна. Груп близько сотні, файлів у них можуть бути десятки тисяч
(номери доходили до 891, старі не прибирали роками), тому спершу дивимось час
зміни кожної теки висоти й читаємо лише ті, де щось рухалось — сотня перевірок
замість десятків тисяч імен.

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

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CamBlank

logger = logging.getLogger(__name__)

# Розширення файлу-заготовки. Порівняння регістронезалежне (.blk/.BLK).
BLANK_EXT = ".blk"

# `12-monolith-a2-x14` → висота 12, виробник monolith, колір a2, номер 14.
# Колір може містити дефіси й пробіли («прозора», «a3 5»), тому він жадібний
# до останнього блоку `-x<цифри>`. Номер ЗАВЖДИ останній і завжди з `x`.
_NAME_RE = re.compile(
    r"^(?P<height>\d{1,3})\s*-\s*(?P<brand>[^-]+?)\s*-\s*(?P<shade>.+?)\s*-\s*[xX](?P<serial>\d+)$"
)


@dataclass(frozen=True)
class ParsedBlank:
    """Розібрана назва. Будь-яке поле може бути None — формат це конвенція,
    не примус, і нерозібраний файл усе одно лишається взятим диском."""

    height: Optional[int] = None
    brand: Optional[str] = None
    shade: Optional[str] = None
    serial: Optional[int] = None


def parse_blank_name(name: str) -> ParsedBlank:
    """`12-monolith-a2-x14` → ParsedBlank(12, 'monolith', 'a2', 14).

    Не розібралось — повертаємо порожній ParsedBlank, а не None: викликач
    однаково мусить порахувати файл, просто без структурованих полів.
    """
    stem = name[: -len(BLANK_EXT)] if name.lower().endswith(BLANK_EXT) else name
    match = _NAME_RE.match(stem.strip())
    if match is None:
        return ParsedBlank()
    try:
        height = int(match.group("height"))
        serial = int(match.group("serial"))
    except ValueError:  # pragma: no cover — регулярка вже гарантує цифри
        return ParsedBlank()
    return ParsedBlank(
        height=height,
        brand=match.group("brand").strip().lower() or None,
        shade=match.group("shade").strip().lower() or None,
        serial=serial,
    )


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

    alive: dict[str, CamBlank] = {
        row.rel_path: row
        for row in db.scalars(select(CamBlank).where(CamBlank.gone_at.is_(None))).all()
    }
    seen_paths = set()

    for item in found:
        seen_paths.add(item.rel_path)
        if item.rel_path in alive:
            continue
        db.add(
            CamBlank(
                rel_path=item.rel_path,
                material_dir=item.material_dir,
                height_dir=item.height_dir,
                file_name=item.file_name,
                height=item.parsed.height,
                brand=item.parsed.brand,
                shade=item.parsed.shade,
                serial=item.parsed.serial,
                height_mismatch=item.height_mismatch,
                first_seen_at=now,
            )
        )
        result.appeared += 1

    for rel_path, row in alive.items():
        if rel_path not in seen_paths:
            row.gone_at = now
            result.vanished += 1

    if result.appeared or result.vanished:
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


def mark_ordered(db: Session, *, now: Optional[datetime] = None) -> int:
    """Позначити все незамовлене як замовлене. Повертає скільки саме."""
    now = now or datetime.now()
    rows = pending_blanks(db)
    for row in rows:
        row.ordered_at = now
    if rows:
        db.commit()
    return len(rows)


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
