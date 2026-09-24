"""Діагностика зіставлення видачі для ОДНОГО клієнта.

Для роботи, яка «не показує STL-прев'ю», показує рівно ЧОМУ: як зіставилось
ім'я клієнта з текою в `export`, які партії знайшов сканер (тека, час
СТВОРЕННЯ = дата партії, матеріал, к-сть STL), і по кожній роботі клієнта —
чи зіставилась вона з текою, чи ні і чому (немає партії того дня, старіша
тека, матеріал не збігся, клієнт не зіставлений).

Read-only. Дзеркалить рівно той самий ланцюг, що будує екран видачі
(`app/routers/handout.py::handout_context`), лише для одного клієнта й без
UI-частини — щоб «чому нема STL» була відповідь за один виклик MCP, а не
розкопки (рішення власника 24.09.26). Живить MCP-інструмент `kmill_handout_match`.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.orm import Session

from app.business_day import business_date_of, business_today
from app.export_scanner import (
    list_export_client_names_cached,
    scan_export_client_cached,
)
from app.services.handout import (
    NAMELESS_CLIENT_KEY,
    covered_days,
    entries_for_material,
    handout_client_matches,
    handout_eligible_orders,
    handout_group_key,
    handout_not_before,
    night_claims_of,
    stale_folder_day,
)
from app.services.order_dates import parse_sheet_tab
from app.settings_store import get_export_folder_path
from app.stl_preview import list_stl_files


def _stl_count(folder_path: Path) -> int | None:
    """К-сть .stl у теці; None, якщо шара не відповіла (діагностика не має падати)."""
    try:
        return len(list_stl_files(folder_path))
    except Exception:  # noqa: BLE001 — мережева шара; діагностика терпима до збою
        return None


def diagnose_handout_client(db: Session, query: str) -> dict:
    """Повний розбір зіставлення видачі для клієнта, знайденого за `query`
    (ім'я клієнта — підрядок, або id/наряд роботи)."""
    query = (query or "").strip()
    today = business_today()
    eligible = handout_eligible_orders(db, today)

    # Групи за клієнтом — по ВСІХ днях (нічні клейми рахуються крос-день, як у
    # роутері), ключ той самий, що на екрані (`handout_group_key`).
    groups: dict[str, list] = {}
    for order in eligible:
        groups.setdefault(handout_group_key(order), []).append(order)

    # Знайти цільового клієнта: за id/нарядом роботи, інакше за іменем (підрядок).
    target_key = None
    q_low = query.lower()
    if query.isdigit():
        oid = int(query)
        for key, orders in groups.items():
            if any(o.id == oid or (o.work_order_no or "") == query for o in orders):
                target_key = key
                break
    if target_key is None:
        for key in groups:
            label = "" if key == NAMELESS_CLIENT_KEY else key
            if q_low and q_low in label.lower():
                target_key = key
                break

    if target_key is None:
        return {
            "query": query,
            "знайдено": False,
            "підказка": (
                "клієнта не знайдено серед робіт видачі (вчора+старіші невидані). "
                "Дай ім'я клієнта (підрядок) або id/наряд роботи."
            ),
            "клієнти_на_видачі": sorted(
                ("(без імені)" if k == NAMELESS_CLIENT_KEY else k) for k in groups
            )[:80],
        }

    orders = groups[target_key]
    claims = night_claims_of(orders)

    export_root = Path(get_export_folder_path(db))
    folder_names = list_export_client_names_cached(export_root)
    not_before = handout_not_before(eligible)

    matches = handout_client_matches(db, [target_key], folder_names)
    match = matches.get(target_key)
    matched_folder = match.matched_folder_name if match else None

    entries = (
        list(scan_export_client_cached(export_root, matched_folder, not_before))
        if matched_folder
        else []
    )

    batches = [
        {
            "тека": e.batch_folder_name,
            "матеріал_тека": e.material_color_folder_name,
            "створено": e.created_at.isoformat(sep=" ", timespec="seconds"),
            "робочий_день": business_date_of(e.created_at).strftime("%d.%m.%y"),
            "файлів": len(e.files),
            "підпапок": e.subfolders,
            "stl": _stl_count(e.folder_path),
        }
        for e in entries
    ]

    works = []
    for order in orders:
        work_day = parse_sheet_tab(order.sheet_tab)
        matched = entries_for_material(
            order.material_color, entries, work_day, claims, order.id
        )
        if matched:
            result = "зіставлено з текою: " + ", ".join(
                f"{e.batch_folder_name}/{e.material_color_folder_name}" for e in matched
            )
        elif not matched_folder:
            result = "тека клієнта НЕ зіставлена (ім'я не знайшло теки в export)"
        else:
            stale = stale_folder_day(
                order.material_color, entries, work_day, claims, order.id
            )
            if stale is not None:
                result = (
                    f"партії того дня немає; є СТАРІША тека ({stale.strftime('%d.%m')}) "
                    "— не показуємо (може бути чужа попередня робота)"
                )
            else:
                result = "немає партії цього дня з цим матеріалом"
        works.append(
            {
                "id": order.id,
                "наряд": order.work_order_no,
                "матеріал": order.material_color,
                "день_роботи": order.sheet_tab,
                "covered_days": (
                    [d.strftime("%d.%m") for d in covered_days(work_day)]
                    if work_day
                    else []
                ),
                "статус": order.status,
                "результат": result,
            }
        )

    return {
        "query": query,
        "клієнт": "(без імені)" if target_key == NAMELESS_CLIENT_KEY else target_key,
        "export_root": str(export_root),
        "зіставлення_імені": {
            "тека": matched_folder,
            "впевненість": round(match.confidence, 1) if match else None,
            "підтверджений_аліас": bool(match.is_confirmed_alias) if match else None,
            "кандидати": (
                [[name, round(score, 1)] for name, score in match.candidates[:5]]
                if match
                else []
            ),
        },
        "партій_знайдено": len(batches),
        "партії": batches,
        "робіт": len(works),
        "роботи": works,
    }
