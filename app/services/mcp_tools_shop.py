"""Інструменти MCP аналізу цеху (12.11.26): звірка диска з замовленням, старі
диски, теки програм, історія верстатів, Sum3D-кількість, печі, export,
установки Sum3D, прогін підказки диска. Винесено з `mcp_tools.py` без змін.
Лише читання."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.services.mcp_common import Tool, _days_limit_schema, _int_arg

SHOP_TOOLS: tuple[Tool, ...] = ()


def tool_disc_check(db: Session, args: dict) -> dict[str, Any]:
    """Звірка «замовлено ↔ диск програми» заднім числом (`disc_check.retro`)."""
    from app.services import disc_check

    return disc_check.retro(
        db,
        _int_arg(args, "days", default=14, low=1, high=90),
        examples=_int_arg(args, "limit", default=25, low=1, high=200),
    )


def tool_discs_inventory(db: Session, args: dict) -> dict[str, Any]:
    """Старі диски теки CAM з ознаками «фізично вже немає» (`disc_inventory`)."""
    from app.services import disc_inventory
    from app.settings_store import get_setting

    return disc_inventory.inventory(
        db,
        get_setting(db, "cam_blanks_path"),
        sample=_int_arg(args, "limit", default=40, low=1, high=300),
    )


def tool_program_folders(db: Session, args: dict) -> dict[str, Any]:
    """Теки програм верстатів: що лежить, який диск і чи збігається з роботою."""
    from app.models import Machine
    from app.services import disc_check, program_folders

    from sqlalchemy import func

    from app.models import MachineReading

    limit = _int_arg(args, "limit", default=10, low=1, high=50)
    # Коли програму вперше побачили на БУДЬ-ЯКОМУ верстаті: прорахована, але
    # не запущена лежить у теці без цієї позначки.
    on_machine = {
        (name or "").lower(): at.strftime("%Y-%m-%d %H:%M")
        for name, at in db.execute(
            select(MachineReading.iso_name, func.min(MachineReading.captured_at))
            .where(MachineReading.iso_name.is_not(None),
                   MachineReading.captured_at >= datetime.now() - timedelta(days=30))
            .group_by(MachineReading.iso_name)
        ).all()
        if name and at
    }
    out: list[dict[str, Any]] = []
    for machine in db.scalars(select(Machine).order_by(Machine.sort_order, Machine.id)).all():
        folder = (machine.program_folder or "").strip()
        if not folder:
            out.append({"верстат": machine.name, "тека": None})
            continue
        scanned = program_folders.scan(folder)
        files = []
        for item in scanned.newest(limit):
            disc = disc_check.program_disc(item.name)
            entry: dict[str, Any] = {
                "файл": item.rel_dir + item.name,
                "розмір": item.size,
                "змінено": item.modified.strftime("%Y-%m-%d %H:%M:%S"),
                "диск": disc.head if disc else None,
                "на_верстаті": on_machine.get(item.name.lower()),
            }
            if disc is not None:
                orders = disc_check.orders_for(db, disc)
                entry["робіт"] = len(orders)
                checks = []
                for order in orders:
                    verdict = disc_check.compare(disc, order)
                    checks.append({
                        "робота": order.id, "клієнт": order.client_name,
                        "колір_роботи": order.material_color,
                        "розбіжності": verdict.issues or None,
                        "не_порівняно": verdict.skipped or None,
                    })
                entry["звірка"] = checks
            files.append(entry)
        out.append({
            "верстат": machine.name,
            "тека": folder,
            "стан": scanned.summary(),
            "програм": len(scanned.files),
            "обрізано": scanned.truncated,
            "інших_файлів": scanned.other_files,
            "прохід_мс": scanned.elapsed_ms,
            "найновіші": files,
        })
    return {"верстати": out, "примітка": "Лише читання: файли не видаляються й не переносяться."}


SHOP_TOOLS = SHOP_TOOLS + (
    Tool(
        name="kmill_disc_check",
        description=(
            "Звірка «замовлено ↔ диск»: програми з верстатів (назва .iso несе диск: матеріал, висоту, "
            "колір) звʼязуються з роботами черги за Sum3D ID і датою, і для кожної — чи збігся матеріал, "
            "відтінок і висота (STL × усадка). Лічильники, розбіжності за видом, «не порівняно» "
            "(чого не розібрано з одного боку — не помилка), приклади. Для оцінки, скільки було б "
            "попереджень і чи вони справжні. Лише читання."
        ),
        schema={
            "type": "object",
            "properties": {
                "days": {"type": "integer", "minimum": 1, "maximum": 90, "description": "За скільки днів, типово 14."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200, "description": "Скільки прикладів у кожному списку, типово 25."},
            },
            "additionalProperties": False,
        },
        run=tool_disc_check,
    ),
    Tool(
        name="kmill_discs_inventory",
        description=(
            "Старі диски в теці CAM (.blk): скільки всього, коли кожен востаннє був у програмі на "
            "верстаті, скільки вільно (вміст читається з межею часу, найстаріші першими), висновок "
            "«майже_точно_немає / можливо_лежить / в_роботі / невідомо» з причинами, розклад по теках "
            "матеріал/висота. Для рішення, які файли дисків фізично викинуто. Лише читання: нічого не "
            "видаляє й не переносить."
        ),
        schema={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 300, "description": "Скільки прикладів у кожному списку, типово 40."},
            },
            "additionalProperties": False,
        },
        run=tool_discs_inventory,
    ),
    Tool(
        name="kmill_program_folders",
        description=(
            "Теки програм фрезерування верстатів (Налаштування → Верстати → «Теки програм»): чи "
            "доступна тека, скільки .iso/.nc, найновіші файли з диском у назві, роботи черги з тим "
            "самим Sum3D і звірка матеріалу/кольору/висоти. Лише читання."
        ),
        schema={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "description": "Скільки найновіших файлів на верстат, типово 10."},
            },
            "additionalProperties": False,
        },
        run=tool_program_folders,
    ),
)

def tool_machine_history(db: Session, args: dict) -> dict[str, Any]:
    from app.services import shop_history

    return shop_history.machine_history(
        db, _int_arg(args, "days", default=7, low=1, high=60), str(args.get("machine") or ""),
        examples=_int_arg(args, "limit", default=15, low=1, high=100),
    )


def tool_cam_units(db: Session, args: dict) -> dict[str, Any]:
    from app.services import shop_history

    return shop_history.cam_units(
        db, _int_arg(args, "days", default=3, low=1, high=30),
        examples=_int_arg(args, "limit", default=25, low=1, high=200),
    )


def tool_disc_usage(db: Session, args: dict) -> dict[str, Any]:
    from app.services import shop_history

    return shop_history.disc_usage(db, _int_arg(args, "weeks", default=8, low=1, high=52))


def tool_furnace_cycles(db: Session, args: dict) -> dict[str, Any]:
    from app.services import shop_history

    return shop_history.furnace_cycles(
        db, _int_arg(args, "days", default=14, low=1, high=120),
        examples=_int_arg(args, "limit", default=20, low=1, high=200),
    )


def tool_export_overview(db: Session, args: dict) -> dict[str, Any]:
    from app.services import shop_history

    return shop_history.export_overview(db)


SHOP_TOOLS = SHOP_TOOLS + (
    Tool(
        name="kmill_machine_history",
        description=(
            "Історія програм верстатів з показів: скільки програм, медіана роботи до 100 %, простій на "
            "100 % до наступної (окремо вночі 18–8), найдовші простої, повторні запуски тієї самої .iso, "
            "кадри з помилкою, обриви зв'язку, медіана роботи за матеріалом/висотою диска. Лише читання."
        ),
        schema=_days_limit_schema(7, 60, {"machine": {"type": "string", "description": "Частина назви верстата."}}),
        run=tool_machine_history,
    ),
    Tool(
        name="kmill_cam_units",
        description=(
            "Кількість у таблиці проти проєкту Sum3D: для кожної групи «день + Sum3D» сума одиниць рядків "
            "і зуби, пораховані з назв STL у проєкті .cam. Збіг / розбіжність / без зубів у назвах, "
            "приклади розбіжностей з клієнтами. Назв STL не віддає. Лише читання."
        ),
        schema=_days_limit_schema(3, 30),
        run=tool_cam_units,
    ),
    Tool(
        name="kmill_disc_usage",
        description=(
            "Темп витрати дисків: скільки НОВИХ дисків (файлів .blk) зʼявлялось по групах матеріал/висота/"
            "колір по тижнях, середнє на тиждень і скільки файлів групи зараз у теці. Лише читання."
        ),
        schema={"type": "object", "properties": {
            "weeks": {"type": "integer", "minimum": 1, "maximum": 52, "description": "Скільки тижнів, типово 8."},
        }, "additionalProperties": False},
        run=tool_disc_usage,
    ),
    Tool(
        name="kmill_furnace_cycles",
        description=(
            "Цикли печей з показів (суцільний RUN): тривалість, найвища температура, за скільки хвилин "
            "її досягнуто, медіани по кожній печі — щоб бачити, чи нагрів не повільнішає. Лише читання."
        ),
        schema=_days_limit_schema(14, 120),
        run=tool_furnace_cycles,
    ),
    Tool(
        name="kmill_export_overview",
        description=(
            "Теки export (клієнт/партія) і спул пошти: скільки клієнтів і партій, вік партій, партії без "
            "закріпленої роботи, файлів і ГБ; спул — листів, файлів, ГБ. Назв файлів не віддає. Лише читання."
        ),
        schema={"type": "object", "properties": {}, "additionalProperties": False},
        run=tool_export_overview,
    ),
)

def tool_disc_suggest_audit(db: Session, args: dict) -> dict[str, Any]:
    from app.services import disc_suggest

    return disc_suggest.audit(db, _int_arg(args, "days", default=7, low=1, high=60),
                              _int_arg(args, "limit", default=30, low=1, high=200))


def tool_sum3d_install(db: Session, args: dict) -> dict[str, Any]:
    from app.services import sum3d_install

    return sum3d_install.overview(db, limit=_int_arg(args, "limit", default=40, low=1, high=300))


SHOP_TOOLS = SHOP_TOOLS + (
    Tool(
        name="kmill_sum3d_install",
        description=(
            "Встановлення Sum3D (Налаштування → Програма Sum3D: кілька версій) і спільна тека стратегій: "
            "версія exe, теки першого рівня, розширення файлів, файли налаштувань і бібліотек (.xml/.ini/"
            ".cfg/.dat/.lib…) з розміром і часом, журнали (.log), що змінювалось останнім. Назви STL і "
            "вміст файлів не віддає. Лише читання."
        ),
        schema={"type": "object", "properties": {
            "limit": {"type": "integer", "minimum": 1, "maximum": 300,
                      "description": "Скільки файлів налаштувань і журналів показати, типово 40."},
        }, "additionalProperties": False},
        run=tool_sum3d_install,
    ),
    Tool(
        name="kmill_disc_suggest_audit",
        description=(
            "Прогін підказки диска на прийнятих листах за N днів: яку висоту й диск запропонувала б "
            "картка листа (правила власника: +1 мм, наступна наявна висота, Monolith +1 крок, "
            "мультиколор ±1, початий першим) проти диска з назви програми на верстаті. Лічильники "
            "«висота_як_у_цеху / інша», приклади. Назв STL не віддає. Лише читання."
        ),
        schema=_days_limit_schema(7, 60),
        run=tool_disc_suggest_audit,
    ),
)
