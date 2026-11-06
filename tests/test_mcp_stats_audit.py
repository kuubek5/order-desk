"""`kmill_stats_audit`: звірка екрана «Статистика» рядок за рядком.

Бойовий випадок 06.11.26: рядок із зсунутими колонками прожив у таблиці 16 секунд,
синк зробив із нього архівну роботу з кількістю 37875 (це номер наряду), і екран
показував 116 443 одиниці за 30 днів. `kmill_queue` архівних не показує, тож знайти
такий рядок було нічим.

Період задано датами, а не словом: вкладки тут сталі, годинник тестам не потрібен.
"""

from __future__ import annotations

from datetime import datetime

from app.models import ActionLog, Order
from app.services import stats_data
from app.services.mail_rework import REWORK_FIELD
from tests.asgi_client import MiniClient
from tests.test_settings_slabs_render import app_db  # noqa: F401 — фікстура

TUE, WED = "03.11.26", "04.11.26"
PERIOD = {"period": "custom", "from": "2026-11-03", "to": "2026-11-04"}


def _call(app, **arguments) -> dict:
    import json

    _, _, body = MiniClient(app).post_json(
        "/mcp",
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "kmill_stats_audit", "arguments": arguments},
        },
        headers={"MCP-Protocol-Version": "2025-06-18"},
    )
    result = json.loads(body)["result"]
    assert not result.get("isError"), result
    return result["structuredContent"]


def _seed(factory) -> dict[str, int]:
    stats_data.clear_cache()
    with factory() as db:
        rows = {
            "real": Order(
                source="lab", sheet_tab=TUE, row_number=2, work_order_no="37875", technician_name="с",
                material_color="пмма прозора", quantity="14", kind="капа", sum3d_id="13-39-49",
            ),
            # Той самий рядок, прочитаний зі зсувом колонок: кількість = наряд.
            "ghost": Order(
                source="lab", sheet_tab=TUE, row_number=23, work_order_no="30",
                technician_name="2026-10-31_00001-004", material_color="14", quantity="37875",
                kind="пмма прозора", sum3d_id="робота за 30.10",
                archived_at=datetime(2026, 11, 3, 10, 10, 27),
            ),
            # Перенесена: у вівторок без Sum3D, у середу зроблена.
            "moved_tue": Order(
                source="lab", sheet_tab=TUE, row_number=3, work_order_no="38124", quantity="4",
                material_color="моно а3.5", archived_at=datetime(2026, 11, 4, 7, 40),
            ),
            "moved_wed": Order(
                source="lab", sheet_tab=WED, row_number=2, work_order_no="38124", quantity="4",
                material_color="моно а3.5", sum3d_id="10-00-00",
            ),
            "rework": Order(
                source="lab", sheet_tab=WED, row_number=3, work_order_no="38200", quantity="2",
                material_color="моно а3", sum3d_id="11-00-00", mill_count="2",
            ),
            "mail": Order(
                source="email", sheet_tab=WED, row_number=50, client_name="Клієнт", quantity="5",
                material_color="mono a3", sum3d_id="12-00-00",
            ),
            "mail_rework": Order(
                source="email", sheet_tab=WED, row_number=51, client_name="Клієнт", quantity="1",
                material_color="mono a3", sum3d_id="12-30-00",
            ),
            "no_qty": Order(source="lab", sheet_tab=WED, row_number=4, work_order_no="38300", material_color="800"),
        }
        db.add_all(rows.values())
        db.flush()
        db.add(ActionLog(order_id=rows["mail_rework"].id, action_type="rework", field=REWORK_FIELD))
        db.commit()
        return {name: order.id for name, order in rows.items()}


def test_overview_matches_screen_and_finds_the_ghost(app_db):  # noqa: F811
    app, factory = app_db
    ids = _seed(factory)
    data = _call(app, **PERIOD)

    assert data["збіг_з_екраном"] is True
    assert data["рядки_тут"] == {"роботи": 8, "одиниць": 37905}
    assert data["екран"]["без_кількості"] == 1

    rules = data["за_правилом"]
    assert rules["архівні"] == {"роботи": 2, "одиниць": 37879}
    assert rules["sum3d_не_час"] == {"роботи": 1, "одиниць": 37875}
    assert rules["sum3d_порожній"] == {"роботи": 2, "одиниць": 4}
    assert rules["переробка_який_раз_2_і_більше"] == {"роботи": 1, "одиниць": 2}
    assert rules["переробка_з_листа"] == {"роботи": 1, "одиниць": 1}
    # Привид не проходить за Sum3D: текст замість часу не рахується часом.
    assert rules["із_sum3d_без_переробок"] == {"роботи": 3, "одиниць": 23}

    assert [row["id"] for row in data["великі_кількості"]] == [ids["ghost"]]
    odd = {row["id"]: row["чому"] for row in data["дивні_рядки"]}
    assert set(odd) == {ids["ghost"]}, "матеріал 800 і звичайні рядки не дивні"
    assert len(odd[ids["ghost"]]) == 4

    tuesday = data["по_днях"][0]
    assert tuesday["день"] == "2026-11-03"
    assert tuesday["лаба"] == 37893 and tuesday["найбільша_кількість"] == 37875
    assert tuesday["архівні"]["одиниць"] == 37879


def test_threshold_is_an_argument(app_db):  # noqa: F811
    app, factory = app_db
    ids = _seed(factory)
    data = _call(app, min_quantity=10, **PERIOD)
    assert [row["id"] for row in data["великі_кількості"]] == [ids["ghost"], ids["real"]]


def test_carryover_lists_works_on_several_tabs(app_db):  # noqa: F811
    app, factory = app_db
    _seed(factory)
    data = _call(app, view="carryover", **PERIOD)
    assert data["нарядів_на_кількох_вкладках"] == 1
    assert data["зайвих_рядків"] == 1 and data["зайвих_одиниць"] == 4
    assert data["нарядів_із_sum3d_кілька_разів"] == 0
    assert data["приклади"][0]["наряд"] == "38124"


def test_day_view_shows_archived_rows(app_db):  # noqa: F811
    app, factory = app_db
    ids = _seed(factory)
    data = _call(app, view="rows", day="2026-11-03")
    assert {row["id"] for row in data["рядки"]} == {ids["real"], ids["ghost"], ids["moved_tue"]}

    data = _call(app, view="rows", day="2026-11-03", only_archived=True)
    assert {row["id"] for row in data["рядки"]} == {ids["ghost"], ids["moved_tue"]}
    ghost = next(row for row in data["рядки"] if row["id"] == ids["ghost"])
    assert ghost["кількість"] == "37875" and ghost["чому_дивний"]


def test_day_view_needs_a_day(app_db):  # noqa: F811
    import json

    app, _ = app_db
    _, _, body = MiniClient(app).post_json(
        "/mcp",
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "kmill_stats_audit", "arguments": {"view": "rows"}},
        },
        headers={"MCP-Protocol-Version": "2025-06-18"},
    )
    result = json.loads(body)["result"]
    assert result["isError"] is True
    assert "day" in result["content"][0]["text"]
