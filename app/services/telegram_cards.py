"""Екрани бота картинками: дані, підпис і кнопки (власник 12.11.26).

Кожен екран бота — одна картка в стилі CRM (`telegram_pictures`). Цей модуль
збирає для неї дані ТИМИ САМИМИ функціями, що текстові екрани
(`telegram_bot`, `telegram_views`), тож картинка й текст не можуть розійтись, а
текст лишається запасним: будь-який збій малювання → `None`, і бот показує
старий текстовий екран.

Навігація: усі екрани — фото, тому перехід між ними Telegram робить на місці
(`editMessageMedia`), без «стерли й надіслали наново». Кнопки екрана — зверху
(групи пошти, сторінки, «не здано», день), знизу завжди «Оновити» й «Меню».
Дзвіночок сповіщень — лише на головному.

`p:<вид>:<аргумент>` — кнопка з параметром картинки (група й сторінка пошти,
розгорнутий список лабораторії). Старі кнопки `e:…` далі відкривають текст.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional

from sqlalchemy.orm import Session

from app.services import telegram_pictures as tp
from app.services.telegram_draw import ACCENT, GREEN, INK, INK_2, RED, Span, sub

logger = logging.getLogger(__name__)

PICTURE_CALLBACK = "p"
SETTING_KEY = "telegram_bot_pictures"

#: Назва розділу в підписі до фото (як у текстових екранах).
TITLES = {
    "home": "",
    "furnaces": "🔥 Пічки",
    "mail": "📬 Пошта",
    "lab": "🧪 Лабораторія · сьогодні",
    "lab_y": "🧪 Лабораторія · вчора",
    "handout": "📦 Видача · сьогодні",
    "handout_y": "📦 Видача · вчора",
    "orders": "🧾 Роботи · сьогодні",
    "orders_y": "🧾 Роботи · вчора",
    "sisma": "🖨 Sisma",
}
_DAY_PAIRS = {"lab": "lab_y", "handout": "handout_y", "orders": "orders_y"}
_GROUP_OF_SYMBOL = {"Zr": "zr", "PMMA": "pm", "Ti": "ti", "Wax": "wx"}
_GROUP_OF_NAME = {"Цирконій": "zr", "ПММА": "pm", "Титан": "ti", "Віск": "wx"}


def enabled(db: Session) -> bool:
    """Нові картки ввімкнено (типово так); «0» — старий текстовий бот."""
    from app.settings_store import get_setting

    return (get_setting(db, SETTING_KEY) or "1") != "0"


@dataclass
class Card:
    png: bytes
    summary: str  # тіло підпису (HTML), коротко: картинка вже все показує
    rows: list[list[dict]] = field(default_factory=list)  # кнопки екрана
    refresh: str = ""  # куди веде «Оновити»


def _btn(text: str, data: str) -> dict:
    return {"text": text, "callback_data": data}


def _e(value: Any) -> str:
    import html

    return html.escape(str(value), quote=False)


# ── Збирачі екранів ─────────────────────────────────────────────────────────


def _mail(db: Session, now: datetime, arg: Optional[str]) -> Card:
    from app.services import telegram_views as tv

    group, _, raw_page = (arg or "").partition(".")
    group = group if group in {key for key, _ in tp.MAIL_TABS} else ""
    letters = tv.mail_letters(db)
    pages = tp.mail_pages(letters, group or None)
    page = max(0, min(int(raw_page) if raw_page.isdigit() else 0, pages - 1))
    png = tp.mail_png(letters, now, group or None, page)

    counts: dict[str, int] = {}
    for letter in letters:
        counts[letter.group] = counts.get(letter.group, 0) + 1
    rows: list[list[dict]] = []
    tabs = [
        _btn(("✓ " if group == key else "") + f"{'інше' if key == 'none' else label} {counts[key]}",
             f"{PICTURE_CALLBACK}:mail:{'' if group == key else key}.0")
        for key, label in tp.MAIL_TABS if counts.get(key)
    ]
    if len(tabs) > 1 or group:
        rows.append(tabs)
    if pages > 1:
        rows.append([
            _btn("‹", f"{PICTURE_CALLBACK}:mail:{group}.{max(0, page - 1)}"),
            _btn(f"{page + 1} / {pages}", f"{PICTURE_CALLBACK}:mail:{group}.{page}"),
            _btn("›", f"{PICTURE_CALLBACK}:mail:{group}.{min(pages - 1, page + 1)}"),
        ])
    if letters:
        rows.append([_btn("📋 Листи текстом", f"{tv.EXPAND_CALLBACK}:mail:{tv.MAIL_ALL}")])

    if letters:
        board = tv.mail_board(db, now)
        summary = f"📬 <b>{_e(tv.mail_headline(board))}</b>"
        if board.unknown:
            summary += f"\n{_e(tv.no_number_line(board.unknown))}"
    else:
        summary = "📬 <b>Усі листи взято в роботу</b> ✅"
    return Card(png, summary, rows, f"{PICTURE_CALLBACK}:mail:{group}.{page}")


def _furnace_tiles(db: Session) -> tuple[list[tp.FurnaceTile], Any]:
    from app.services.furnace import strip_cards, strip_summary
    from app.services.telegram_bot import _clip, _hm

    cards = strip_cards(db)
    tiles = []
    for card in cards:
        state = card.state
        temp = state.temp_c if state and state.temp_c is not None else None
        if card.has_problem:
            tiles.append(tp.FurnaceTile(card.target.name, "problem", temp, problem=_clip(card.problem_text, 90)))
        elif card.is_running:
            tiles.append(tp.FurnaceTile(
                card.target.name, "run", temp,
                remaining=(state.remaining_text if state and state.done_at else "") or "",
                opens=_hm(state.done_at) if state and state.done_at else "",
            ))
        elif card.is_idle:
            tiles.append(tp.FurnaceTile(card.target.name, "idle", temp))
        else:
            tiles.append(tp.FurnaceTile(card.target.name, "unknown", temp))
    return tiles, (strip_summary(cards) if cards else None)


def _furnaces(db: Session, now: datetime, arg: Optional[str]) -> Card:
    from app.services.telegram_bot import _furnaces_summary

    tiles, summary = _furnace_tiles(db)
    parts: list[Span] = []
    if summary is not None:
        parts = [sub("спікають "), sub(str(summary.running), INK, "s"), sub(f" з {len(tiles)}")]
        if summary.nearest_text:
            parts += [sub(" · відкриття "), sub(summary.nearest_text, ACCENT, "s")]
        if summary.broken:
            parts += [sub(" · "), sub(f"без зв'язку {summary.broken}", RED, "s")]
    return Card(tp.furnaces_png(tiles, now, parts), _furnaces_summary(db))


def _lab_row(order) -> tp.LabRow:
    from app.material_class import mail_material_badge
    from app.services.queue import is_rush_comment
    from app.services.telegram_views import shade_code

    badge = mail_material_badge(order.material_color)
    group = _GROUP_OF_SYMBOL.get((badge or {}).get("symbol") or "", "")
    shade = shade_code(badge)
    return tp.LabRow(
        number=(order.work_order_no or "").strip(),
        group=group,
        shade=shade,
        material=(order.material_color or "").strip(),
        qty=(order.quantity or "").strip(),
        tech=(order.technician_name or "").strip(),
        rush=is_rush_comment(order.cam_comment),
    )


def _lab(offset: int) -> Callable[[Session, datetime, Optional[str]], Card]:
    def build(db: Session, now: datetime, arg: Optional[str]) -> Card:
        from app.services import telegram_views as tv
        from app.services.queue_view import sum_units
        from app.services.telegram_bot import _day_of

        view = "lab" if offset == 0 else "lab_y"
        counts = tv.lab_counts(tv._lab_orders(db, offset))
        total, done = len(counts["all"]), len(counts["done"])
        take, wait = tv._rush_first(counts["take"]), tv._rush_first(counts["wait"])
        opened_wait = arg == "wait"
        png = tp.lab_png(
            _day_of(offset).strftime("%d.%m"), total, done,
            sum_units(counts["all"]) - sum_units(counts["done"]),
            [_lab_row(o) for o in take[:tp.LAB_MAX_ROWS]],
            [_lab_row(o) for o in wait[:tp.LAB_MAX_ROWS]] if opened_wait else [],
            now, opened_wait=opened_wait, take_total=len(take), wait_total=len(wait),
        )
        rows = []
        if wait:
            rows.append([_btn(("✓ " if opened_wait else "⏳ ") + f"Не здано {len(wait)}",
                              f"v:{view}" if opened_wait else f"{PICTURE_CALLBACK}:{view}:wait")])
        if not total:
            summary = "🧪 Лабораторних робіт немає"
        else:
            summary = f"🧪 Зроблено <b>{done}</b> з {total} · лишається {total - done}"
            if take:
                summary += f" · можна брати <b>{len(take)}</b>"
        return Card(png, summary, rows, f"{PICTURE_CALLBACK}:{view}:wait" if opened_wait else "")

    return build


def _handout(offset: int) -> Callable[[Session, datetime, Optional[str]], Card]:
    def build(db: Session, now: datetime, arg: Optional[str]) -> Card:
        from app.services.telegram_bot import _day_of, handout_day

        stats = handout_day(db, offset)
        png = tp.handout_png(_day_of(offset).strftime("%d.%m"), stats.clients, stats.clients_done,
                             stats.works, stats.works_done, stats.units, stats.units_done, now)
        if not stats.works:
            summary = "📦 Клієнтських робіт немає"
        else:
            summary = f"📦 Клієнтів видано <b>{stats.clients_done}</b> з {stats.clients}"
            left = stats.works - stats.works_done
            summary += f" · ще чекає {left} роб." if left else " · усе видано ✅"
        return Card(png, summary)

    return build


def _orders(offset: int) -> Callable[[Session, datetime, Optional[str]], Card]:
    def build(db: Session, now: datetime, arg: Optional[str]) -> Card:
        from app.queue_filters import count_by_readiness, filter_by_source
        from app.services.queue_view import sum_units
        from app.services.telegram_bot import _day_of, _material_breakdown, _orders_of_day, _works

        day = _day_of(offset)
        orders = _orders_of_day(db, day)
        breakdown = _material_breakdown(orders) if orders else []
        bars = [tp.MaterialBar(name, _GROUP_OF_NAME.get(name, ""), units, works) for name, units, works in breakdown]
        boxes = []
        lab = filter_by_source(orders, "lab")
        clients = filter_by_source(orders, "client")
        for title, group, with_not_ready in (("Лабораторія", lab, True), ("Файли (клієнти)", clients, False)):
            if not group:
                continue
            counts = count_by_readiness(group)
            boxes.append(tp.SourceBox(title, len(group), sum_units(group), counts["can_take"], counts["in_work"],
                                      counts["not_ready"] if with_not_ready else None))
        units = sum_units(orders) if orders else 0
        png = tp.orders_png(day.strftime("%d.%m"), len(orders), units, bars, boxes, now)
        summary = f"🧾 {_works(len(orders))} · <b>{units}</b> од." if orders else "🧾 Робіт немає"
        return Card(png, summary)

    return build


def _sisma_tiles(db: Session) -> list[tp.SismaTile]:
    from app.services.machines import snapshot
    from app.services.telegram_bot import _clip, _when

    now = datetime.now()
    tiles = []
    for card in snapshot(db):
        if not card.is_sisma_machine:
            continue
        name = card.target.name
        if card.has_problem:
            tiles.append(tp.SismaTile(name, "problem", text=_clip(card.problem_text, 90)))
        elif card.is_completed:
            tiles.append(tp.SismaTile(name, "done"))
        elif card.layers:
            layer, total = card.layers
            tiles.append(tp.SismaTile(
                name, "print", layer, total, phase=card.phase_text or "",
                started=_when(card.started_at, now), ends=_when(card.ends_at, now), left=card.left_text or "",
            ))
        else:
            tiles.append(tp.SismaTile(name, "idle", text=card.state_word or "даних немає"))
    return tiles


def _sisma(db: Session, now: datetime, arg: Optional[str]) -> Card:
    from app.services.telegram_bot import _sisma_summary

    tiles = _sisma_tiles(db)
    return Card(tp.sisma_png(tiles, now), _sisma_summary(db) or "🖨 Sisma: принтер ще не впізнано")


# ── Головна ─────────────────────────────────────────────────────────────────


def _tile(label: str, fn: Callable[[], Optional[tp.HomeTile]]) -> Optional[tp.HomeTile]:
    """Одне джерело, що впало, не гасить головну: плитка «не прочитано»."""
    try:
        return fn()
    except Exception:  # noqa: BLE001
        logger.exception("telegram-бот: плитку «%s» не зібрано", label)
        return tp.HomeTile(label, "—", "не вдалось прочитати", [("подробиці в лозі KuubMill", INK_2)])


def _home_tiles(db: Session, now: datetime, admin: bool) -> list[tp.HomeTile]:
    def furnaces() -> Optional[tp.HomeTile]:
        tiles, summary = _furnace_tiles(db)
        if not tiles or summary is None:
            return tp.HomeTile("Печі", "—", "даних ще немає")
        note: list[tuple[str, Any]] = []
        if summary.nearest_text:
            note = [("відкриття ", INK_2), (summary.nearest_text, INK)]
        if summary.broken:
            note += [(" · " if note else "", INK_2), (f"без зв'язку {summary.broken}", RED)]
        idle = sum(1 for t in tiles if t.state == "idle")
        if not note and idle:
            note = [(f"вільних {idle}", GREEN)]
        return tp.HomeTile("Печі", str(summary.running), f"з {len(tiles)} спікають", note,
                           tone="alarm" if summary.broken else "", value_color=ACCENT if summary.running else INK,
                           photo="furnace-crowns.jpg" if summary.running else "")

    def sisma() -> Optional[tp.HomeTile]:
        tiles = _sisma_tiles(db)
        if not tiles:
            return None
        tile = tiles[0]
        if tile.state == "print" and tile.total:
            share = tile.layer / tile.total
            note = [("кінець ~ ", INK_2), (tile.ends, INK)] if tile.ends else []
            return tp.HomeTile("Sisma", f"{round(share * 100)}%", f"шар {tile.layer} з {tile.total}", note,
                               share=share, ring_label="")
        if tile.state == "done":
            return tp.HomeTile("Sisma", "готово", "друк закінчився", tone="ok", value_color=GREEN)
        if tile.state == "problem":
            return tp.HomeTile("Sisma", "—", "без зв'язку", [(tile.text, RED)], tone="alarm")
        return tp.HomeTile("Sisma", "—", tile.text)

    if not admin:
        return [t for t in (_tile("Печі", furnaces), _tile("Sisma", sisma)) if t]

    def machines() -> Optional[tp.HomeTile]:
        from app.services import telegram_board
        from app.services.machines import snapshot

        cards = [card for card in snapshot(db) if not card.is_sisma_machine]
        if not cards:
            return None
        board = telegram_board.board_of(cards)
        total = len(board.tiles)
        run = sum(1 for t in board.tiles if t.state == "run")
        done = [t for t in board.tiles if t.state == "done"]
        alarm = [t for t in board.tiles if t.state in ("off", "fault")]
        note: list[tuple[str, Any]] = []
        if done:
            note += [(f"зняти {len(done)}", GREEN), (f" · {done[0].name}" if len(done) == 1 else "", INK_2)]
        if alarm:
            note += [(" · " if note else "", INK_2), (f"тривога {len(alarm)}", RED)]
        return tp.HomeTile("Верстати", str(run), f"з {total} фрезерують", note, tone="alarm" if alarm else "",
                           share=run / total if total else 0.0, ring_label=f"{run}/{total}")

    def mail() -> Optional[tp.HomeTile]:
        from app.services import telegram_views as tv

        board = tv.mail_board(db, now)
        if not board.letters:
            return tp.HomeTile("Пошта", "0", "усе взято в роботу", tone="ok", value_color=GREEN)
        note: list[tuple[str, Any]] = []
        late = bool(board.waited and board.waited.total_seconds() >= 90 * 60)
        if board.waited:
            note.append((f"найдовше {tv.wait_text(board.waited)}", RED if late else INK_2))
        if board.unread:
            note.append((f" · нових {board.unread}", ACCENT))
        word = tp._plural(board.letters, "лист", "листи", "листів")
        return tp.HomeTile("Пошта", str(board.letters), f"{word} · {board.units} од.", note,
                           tone="alarm" if late else "")

    def lab() -> Optional[tp.HomeTile]:
        from app.services import telegram_views as tv

        counts = tv.lab_counts(tv._lab_orders(db, 0))
        total, done = len(counts["all"]), len(counts["done"])
        if not total:
            return tp.HomeTile("Лабораторія", "0", "робіт сьогодні")
        note: list[tuple[str, Any]] = [("зроблено", INK_2)]
        if counts["take"]:
            note += [(" · можна брати ", INK_2), (str(len(counts["take"])), GREEN)]
        return tp.HomeTile("Лабораторія", str(done), f"з {total}", note, tone="ok" if done == total else "",
                           share=done / total, ring_label=f"{round(done * 100 / total)}%", ring_color=GREEN)

    def handout() -> Optional[tp.HomeTile]:
        from app.services.telegram_bot import handout_day

        stats = handout_day(db, -1)
        if not stats.works:
            return None
        left = stats.clients - stats.clients_done
        note: list[tuple[str, Any]] = [("усе видано", GREEN)] if not left else [("лишилось ", INK_2), (str(left), INK)]
        return tp.HomeTile("Видача за вчора", str(stats.clients_done), f"з {stats.clients} клієнтів", note,
                           tone="ok" if not left else "", photo="handout-tray.jpg")

    def orders() -> Optional[tp.HomeTile]:
        from app.queue_filters import count_by_readiness
        from app.services.queue_view import sum_units
        from app.services.telegram_bot import _day_of, _orders_of_day

        items = _orders_of_day(db, _day_of(0))
        if not items:
            return tp.HomeTile("Роботи сьогодні", "0", "робіт")
        can_take = count_by_readiness(items)["can_take"]
        note: list[tuple[str, Any]] = [("можна брати ", INK_2), (str(can_take), GREEN if can_take else INK)]
        word = tp._plural(len(items), "робота", "роботи", "робіт")
        return tp.HomeTile("Роботи сьогодні", str(len(items)), f"{word} · {sum_units(items)} од.", note)

    built = [
        _tile("Верстати", machines), _tile("Пошта", mail), _tile("Печі", furnaces), _tile("Лабораторія", lab),
        _tile("Видача за вчора", handout), _tile("Роботи сьогодні", orders), _tile("Sisma", sisma),
    ]
    return [t for t in built if t]


def _home(admin: bool) -> Callable[[Session, datetime, Optional[str]], Card]:
    def build(db: Session, now: datetime, arg: Optional[str]) -> Card:
        from app.services.power_watch import home_line

        try:
            banner = home_line(db, datetime.now()) or ""
        except Exception:  # noqa: BLE001 — рядок про світло не має гасити головну
            logger.exception("telegram-бот: рядок про світло не зібрано")
            db.rollback()
            banner = ""
        tiles = _home_tiles(db, now, admin)
        return Card(tp.home_png(tiles, now, banner=banner), "")

    return build


def _builder(view: str, admin: bool) -> Optional[Callable[[Session, datetime, Optional[str]], Card]]:
    return {
        "home": _home(admin),
        "mail": _mail,
        "furnaces": _furnaces,
        "lab": _lab(0),
        "lab_y": _lab(-1),
        "handout": _handout(0),
        "handout_y": _handout(-1),
        "orders": _orders(0),
        "orders_y": _orders(-1),
        "sisma": _sisma,
    }.get(view)


def build(db: Session, view: str, now: datetime, *, admin: bool, arg: Optional[str] = None) -> Optional[Card]:
    """Картка екрана або None — тоді бот показує старий текстовий екран."""
    builder = _builder(view, admin)
    if builder is None:
        return None
    try:
        return builder(db, now, arg)
    except Exception:  # noqa: BLE001 — текстовий екран кращий за мовчазну кнопку
        logger.exception("telegram-бот: картку «%s» не намальовано, показую текст", view)
        db.rollback()
        return None


# ── Кнопки ──────────────────────────────────────────────────────────────────


def keyboard(view: str, card: Card, *, admin: bool, notify: bool, report_callback: str) -> dict:
    rows = [*card.rows]
    refresh = _btn("🔄 Оновити", card.refresh or f"v:{view}")
    if view == "home":
        if admin:
            rows += [
                [_btn("⚙️ Верстати", "v:machines"), _btn("📬 Пошта", "v:mail"), _btn("🔥 Печі", "v:furnaces")],
                [_btn("🧪 Лабораторія", "v:lab"), _btn("📦 Видача", "v:handout"), _btn("🧾 Роботи", "v:orders")],
                [_btn("🖨 Sisma", "v:sisma"), {"text": "🩺 Стан системи", "callback_data": report_callback}],
            ]
        else:
            rows.append([_btn("🔥 Печі", "v:furnaces"), _btn("🖨 Sisma", "v:sisma")])
        rows.append([refresh, _btn("🔔 Сповіщення: так" if notify else "🔕 Сповіщення: ні", "n:home")])
        return {"inline_keyboard": rows}
    today = next((t for t, y in _DAY_PAIRS.items() if view in (t, y)), None)
    if today is not None and admin:
        yesterday = _DAY_PAIRS[today]
        rows.append([_btn(("✓ " if view == today else "") + "Сьогодні", f"v:{today}"),
                     _btn(("✓ " if view == yesterday else "") + "Вчора", f"v:{yesterday}")])
    rows.append([refresh, _btn("🏠 Меню", "v:home")])
    return {"inline_keyboard": rows}
