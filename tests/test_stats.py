"""«Статистика» 2.0 (STATS_BRIEF.md, етап 1): агрегати на засіяній базі в
памʼяті, порівняння, покриття, кошики карти, адмінська вкладка, експорт і
рендер справжніх роутів через MiniClient.

Тиждень засіву: Пн 17.08.26 – Пт 21.08.26 (+ стара суботня вкладка 22.08.26,
що лягає в п'ятницю). Попередній тиждень — 10.08–16.08.
"""

from __future__ import annotations

import io
import re
import zipfile
from datetime import date, datetime, time, timedelta
from urllib.parse import urlencode
from xml.etree import ElementTree

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app import stats as st
from app import web
from app.db import Base
from app.services import section_gate as sg
from app.services import stats_data as data
from app.services import stats_export, stats_view
from app.services.stats_data import Filters
from app.services.xlsx_lite import workbook
from tests.asgi_client import MiniClient

OPERATOR = ("stats-op", "Stats-Op-1")
ADMIN = ("stats-adm", "Stats-Adm-1")

WEEK = st.Period(date(2026, 8, 17), date(2026, 8, 23))
DAYS = WEEK.working_days()
PREV_DAYS = WEEK.previous().working_days()
CUSTOM = {"period": "custom", "from": "2026-08-17", "to": "2026-08-23"}


# ── база ───────────────────────────────────────────────────────────────────
@pytest.fixture
def engine():
    import app.models  # noqa: F401 — реєструє таблиці

    eng = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    data.clear_cache()
    yield eng
    data.clear_cache()


@pytest.fixture
def db(engine):
    with Session(engine, expire_on_commit=False) as session:
        _seed(session)
        yield session


def _materials(session):
    from app.models import Material

    out = {}
    for name, prod, order in (("Цирконій", True, 1), ("ПММА", True, 2), ("Титан", True, 4), ("Віск", True, 5)):
        m = Material(name=name, is_production=prod, sort_order=order)
        session.add(m)
        session.flush()
        out[name] = m.id
    return out


def _order(session, tab, source, qty, mat=None, **kw):
    from app.models import Order

    o = Order(source=source, sheet_tab=tab, quantity=qty, material_id=mat, **kw)
    session.add(o)
    session.flush()
    return o


def _seed(session):
    from app.models import ReworkRecord

    m = _materials(session)
    zr, pmma, ti = m["Цирконій"], m["ПММА"], m["Титан"]
    # Пн 17.08: лаба 3+2, клієнт (таблиця) 5, клієнт (пошта) 4
    _order(session, "17.08.26", "lab", "3", zr, row_number=7, work_order_no="24122", technician_name="с", material_color="моно А3")
    _order(session, "17.08.26", "lab", "2", pmma, row_number=8, work_order_no="24123", technician_name="c", material_color="pmma a2")
    _order(session, "17.08.26", "sheet_client", "5", zr, row_number=9, client_name="Клієнт Альфа", material_color="mono a3", sum3d_id="12-01-45")
    _order(session, "17.08.26", "email", "4", ti, row_number=10, client_name="клієнт альфа ", material_color="титан")
    # Вт 18.08: нечислова кількість не підсумовується; нічний Sum3D
    _order(session, "18.08.26", "lab", "2+1", zr, row_number=7, technician_name="Kira", material_color="a35")
    _order(session, "18.08.26", "lab", None, zr, row_number=8, technician_name="Кира")
    _order(session, "18.08.26", "sheet_client", "6", zr, row_number=9, client_name="Бета", sum3d_id="03-10-00\n03-20-00", material_color="моно a3")
    _order(session, "18.08.26", "sheet_client", "1", pmma, row_number=10, client_name="Бета", sum3d_id="8-34-25")
    # Пт 21.08 + субота 22.08 (окрема стара вкладка) → п'ятниця
    fri = _order(session, "21.08.26", "lab", "4", zr, row_number=7, technician_name="кіра")
    _order(session, "22.08.26", "sheet_client", "10", pmma, row_number=7, client_name="Гамма",
           sum3d_id="11-00-00", row_seen_at=datetime(2026, 8, 22, 10, 5))
    _order(session, "21.08.26", "sheet_client", "2", zr, row_number=8, client_name="Гамма",
           sum3d_id="14-00-00", row_seen_at=datetime(2026, 8, 22, 13, 0))
    # Попередній тиждень: Пн 10.08 і Пт 14.08
    prev = _order(session, "10.08.26", "lab", "6", zr, row_number=7, technician_name="Gенис")
    _order(session, "14.08.26", "sheet_client", "4", zr, row_number=7, client_name="Клієнт Альфа")
    # Брак: у тижні — обладнання 2 од. і технік 1 од.; у попередньому — клієнт 1
    session.add(ReworkRecord(order_id=fri.id, blame="обладнання", redo_quantity="2"))
    session.add(ReworkRecord(order_id=fri.id, blame="технік", redo_quantity="1"))
    session.add(ReworkRecord(order_id=prev.id, blame="клієнт", redo_quantity="1"))
    # Поза обома періодами — не мусить потрапити нікуди
    _order(session, "01.07.26", "lab", "50", zr, row_number=7)
    session.commit()


def _summary(db, days, f=Filters()):
    return data.summarize(data.facts(db, days, f), days, data._material_group_map(db))


# ── чисті функції ──────────────────────────────────────────────────────────
class TestParseIntSafe:
    def test_valid_number(self):
        assert st.parse_int_safe("5") == 5

    def test_strips_whitespace(self):
        assert st.parse_int_safe(" 12 ") == 12

    def test_none_and_text(self):
        assert st.parse_int_safe(None) is None
        assert st.parse_int_safe("абц") is None


class TestPeriod:
    def test_default_is_thirty_days_ending_today(self):
        key, p = st.resolve_period(None, None, None, date(2026, 10, 12))
        assert key == "30" and p == st.Period(date(2026, 9, 13), date(2026, 10, 12))

    def test_custom_swapped_bounds_and_garbage(self):
        key, p = st.resolve_period("custom", "2026-08-23", "2026-08-17", date(2026, 10, 12))
        assert key == "custom" and p == WEEK
        key, _ = st.resolve_period("custom", "вчора", "2026-08-17", date(2026, 10, 12))
        assert key == "30"

    def test_previous_is_adjacent_and_same_length(self):
        assert WEEK.previous() == st.Period(date(2026, 8, 10), date(2026, 8, 16))

    def test_working_days_skip_weekend(self):
        assert DAYS == [date(2026, 8, 17 + i) for i in range(5)]


class TestNames:
    def test_homoglyph_c_variants_are_one_technician(self):
        assert st.tech_key("с") == st.tech_key("c") == st.tech_key("С")

    def test_latin_g_in_cyrillic_name(self):
        assert st.tech_key("Gенис") == st.tech_key("генис")

    def test_case_folds(self):
        assert st.tech_key("Даня") == st.tech_key("ДАня")

    def test_kira_alias_from_owner(self):
        assert st.tech_key("Kira") == st.tech_key("Кира") == st.tech_key("кіра") == "кіра"

    def test_dim_stays_separate(self):
        assert st.tech_key("dim") == "dim"
        assert st.tech_key("dim") != st.tech_key("Денис")

    def test_group_shows_most_frequent_spelling(self):
        groups = st.group_spellings([("кіра", 44), ("Кира", 4), ("Kira", 1), ("с", 173), ("c", 17)], st.tech_key)
        assert groups["кіра"]["label"] == "кіра" and groups["кіра"]["n"] == 49
        assert set(groups["кіра"]["spellings"]) == {"кіра", "Кира", "Kira"}
        assert groups[st.tech_key("c")]["label"] == "с"

    def test_color_normalisation(self):
        assert st.norm_color("моно А3") == st.norm_color("mono a3") == "mono a3"
        assert st.norm_color("a35") == "a3.5"


class TestSum3dMoment:
    def test_before_rollover_is_the_next_calendar_day(self):
        moment = st.sum3d_moment(date(2026, 8, 18), time(3, 10), time(7, 30))
        assert moment == datetime(2026, 8, 19, 3, 10)

    def test_daytime_stays_on_the_tab_day(self):
        assert st.sum3d_moment(date(2026, 8, 18), time(12, 1, 45), time(7, 30)).date() == date(2026, 8, 18)

    def test_first_id_of_a_multiline_cell(self):
        assert st.first_sum3d_time("03-10-00\n03-20-00") == time(3, 10)
        assert st.first_sum3d_time("8-34-25") == time(8, 34, 25)
        assert st.first_sum3d_time("немає папки") is None

    def test_friday_tab_uses_the_weekend_day_when_known(self):
        assert st.work_day_for_sum3d(date(2026, 8, 21), date(2026, 8, 22)) == date(2026, 8, 22)
        assert st.work_day_for_sum3d(date(2026, 8, 21), None) == date(2026, 8, 21)
        # Інший тиждень — не «вихідні цієї п'ятниці»
        assert st.work_day_for_sum3d(date(2026, 8, 21), date(2026, 8, 29)) == date(2026, 8, 21)

    def test_off_hours(self):
        grid = st.empty_grid()
        grid[0][10] = 5  # Пн 10:00 — робочий
        grid[0][20] = 3  # Пн 20:00 — ні
        grid[5][10] = 2  # Сб — ні
        assert st.off_hours(grid) == (10, 5)


# ── агрегати ───────────────────────────────────────────────────────────────
class TestAggregates:
    def test_units_by_day_lab_vs_client_and_friday_weekend(self, db):
        s = _summary(db, DAYS)
        by = s["by_day"]
        assert by[date(2026, 8, 17)] == {"lab": 5, "client": 9, "works": 4, "noqty": 0}
        # Субота 22.08 лягла в п'ятницю
        assert by[date(2026, 8, 21)]["lab"] == 4 and by[date(2026, 8, 21)]["client"] == 12
        assert s["lab"] == 9 and s["client"] == 28 and s["units"] == 37

    def test_non_numeric_quantity_is_counted_not_summed(self, db):
        tue = _summary(db, DAYS)["by_day"][date(2026, 8, 18)]
        assert tue["lab"] == 0 and tue["noqty"] == 2 and tue["works"] == 4

    def test_per_working_day_average_uses_tab_days(self, db):
        s = _summary(db, DAYS)
        assert s["tab_days"] == 3
        assert s["per_day"] == pytest.approx(37 / 3)
        was = _summary(db, PREV_DAYS)
        assert was["tab_days"] == 2 and was["per_day"] == pytest.approx(5)
        assert st.delta_pct(s["per_day"], was["per_day"]) == 147

    def test_coverage_counts_tabs_not_works(self, db):
        assert data.coverage(db, DAYS) == [True, True, False, False, True]

    def test_materials_group(self, db):
        mats = _summary(db, DAYS)["materials"]
        assert mats == {"zr": 3 + 5 + 6 + 4 + 2, "pmma": 2 + 1 + 10, "ti": 4, "oth": 0}

    def test_filters_source_and_material(self, db):
        lab_only = _summary(db, DAYS, Filters(src="lab"))
        assert lab_only["client"] == 0 and lab_only["lab"] == 9
        zr_id = next(mid for mid, name, _ in data.materials(db) if name == "Цирконій")
        assert _summary(db, DAYS, Filters(mat=str(zr_id)))["units"] == 20

    def test_technician_filter_gathers_all_spellings(self, db):
        kira = _summary(db, DAYS, Filters(tech="кіра"))
        assert kira["works"] == 3 and kira["units"] == 4 and kira["noqty"] == 2
        c = _summary(db, DAYS, Filters(tech=st.tech_key("c")))
        assert c["works"] == 2 and c["units"] == 5

    def test_clients_fold_spelling_and_compare(self, db):
        now = data.client_units(db, DAYS, Filters())
        alpha = now[st.client_key("Клієнт Альфа")]
        assert alpha["units"] == 9 and alpha["works"] == 2
        was = data.client_units(db, PREV_DAYS, Filters())
        assert was[st.client_key("Клієнт Альфа")]["units"] == 4

    def test_rework_by_blame_in_period_only(self, db):
        rw = data.rework_summary(db, DAYS, Filters())
        assert rw["units"] == 3 and rw["cases"] == 2
        assert [b["blame"] for b in rw["blames"]] == ["обладнання", "технік"]
        assert data.rework_summary(db, PREV_DAYS, Filters())["blames"][0]["blame"] == "клієнт"

    def test_sum3d_heatmap_buckets(self, db):
        heat = data.sum3d_heat(db, DAYS, Filters())
        cli = heat["grids"]["client"]
        assert cli[0][12] == 5  # Пн 12-01-45
        assert cli[2][3] == 6  # Вт-вкладка, 03-10 → нічна зміна, середа 03:00
        assert cli[1][8] == 1  # одноцифрова година
        assert cli[5][11] == 10  # суботній рядок (стара вкладка 22.08)
        assert cli[5][14] == 2  # п'ятнична вкладка, рядок зʼявився в суботу
        assert heat["n"]["client"] == 5

    def test_row_seen_switch_needs_seven_days(self, db):
        assert data.row_seen_days(db) == 1
        from app.models import Order

        for i in range(7):
            db.add(Order(source="lab", sheet_tab="17.08.26", quantity="1", row_seen_at=datetime(2026, 8, 3 + i, 12)))
        db.commit()
        data.clear_cache()
        assert data.row_seen_days(db) >= 7

    def test_lead_time_median(self, db):
        from app.models import Order

        assert data.lead_minutes(db, DAYS, Filters()) == []
        for minutes in (30, 90, 600):
            db.add(Order(source="lab", sheet_tab="19.08.26", quantity="1",
                         row_seen_at=datetime(2026, 8, 19, 9, 0),
                         job_code_at=datetime(2026, 8, 19, 9, 0) + timedelta(minutes=minutes)))
        db.commit()
        data.clear_cache()
        assert st.median(data.lead_minutes(db, DAYS, Filters())) == pytest.approx(90, abs=0.01)

    def test_side_list_for_friday_groups_by_tab(self, db):
        p = _params()
        side = stats_view.side_list(db, p, stats_view.ListRequest(day=date(2026, 8, 21)))
        assert [g["tab"] for g in side["groups"]] == ["21.08.26", "22.08.26"]
        assert side["works_n"] == 3

    def test_side_list_heat_cell_matches_bucket(self, db):
        p = _params()
        side = stats_view.side_list(db, p, stats_view.ListRequest(cell=("client", 2, 3)))
        rows = [w for g in side["groups"] for w in g["rows"]]
        assert [w["client"] for w in rows] == ["Бета"] and rows[0]["sum3d"] == "03-10-00"

    def test_side_list_without_quantity(self, db):
        side = stats_view.side_list(db, _params(), stats_view.ListRequest(noqty=True))
        assert side["works_n"] == 2 and side["noqty"] == 2


def _params(**query) -> stats_view.Params:
    return stats_view.parse_params({**CUSTOM, **query}, date(2026, 10, 12), is_admin=True)


class TestViewAndExport:
    def test_overview_kpis_and_comparison(self, db):
        ov = stats_view.overview(db, _params())
        k = ov["kpis"]
        assert k["per_day"]["text"] == "12" and k["per_day"]["arrow"]["sym"] == "↑"
        assert k["units"]["noqty"] == 2
        assert k["rework"]["units"] == 3
        assert k["lead"]["collecting"] is True
        assert ov["selected_day"] == date(2026, 8, 21)

    def test_intake_week_chart_is_twelve_weeks(self, db):
        it = stats_view.intake(db, _params())
        assert len(it["weeks_chart"]["bars"]) == 12
        assert it["mode"] == "sum3d" and it["row_ready"] is False
        # перемикач «поява рядка» без 7 днів даних не вмикається адресою
        assert stats_view.intake(db, _params(hm="row"))["mode"] == "sum3d"

    def test_overview_export_content(self, db):
        book = stats_export.overview_book(db, _params())
        sheets = _read_xlsx(book)
        assert list(sheets) == ["Огляд", "Дні", "Матеріали", "Клієнти", "Брак"]
        days = sheets["Дні"]
        assert days[0][0] == "Дата"
        assert ["17.08.26", "Пн", "так", "4", "5", "9", "14", "0"] == days[1]
        assert ["обладнання", "1", "2"] in sheets["Брак"]

    def test_list_export_content(self, db):
        book = stats_export.list_book(db, _params(), stats_view.ListRequest(day=date(2026, 8, 17)))
        rows = next(iter(_read_xlsx(book).values()))
        assert rows[0][:4] == ["Вкладка", "Рядок", "Джерело", "Наряд"]
        assert len(rows) == 5

    def test_intake_export_has_maps(self, db):
        sheets = _read_xlsx(stats_export.intake_book(db, _params()))
        assert "Карта Лаба" in sheets and "Кольори" in sheets

    def test_params_roundtrip_is_shareable(self):
        p = _params(tab="intake", src="lab", tech="кіра", cmp="0")
        q = p.query()
        assert q["tab"] == "intake" and q["src"] == "lab" and q["tech"] == "кіра" and q["cmp"] == "0"
        assert q["from"] == "2026-08-17"
        assert "tab" not in stats_view.parse_params({}, date(2026, 10, 12), True).query()

    def test_techs_tab_is_admin_only(self):
        assert stats_view.parse_params({"tab": "techs"}, date(2026, 10, 12), is_admin=False).tab == "overview"
        assert stats_view.parse_params({"tab": "techs"}, date(2026, 10, 12), is_admin=True).tab == "techs"


def test_xlsx_lite_is_well_formed():
    book = workbook([("Аркуш: 1/2", [["Назва", "Число"], ["<&>", 5], ["x\x01y", None]])])
    with zipfile.ZipFile(io.BytesIO(book)) as zf:
        for name in zf.namelist():
            if name.endswith(".xml") or name.endswith(".rels"):
                ElementTree.fromstring(zf.read(name))
        assert 'name="Аркуш  1 2"' in zf.read("xl/workbook.xml").decode()


def _read_xlsx(book: bytes) -> dict[str, list[list[str]]]:
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    out: dict[str, list[list[str]]] = {}
    with zipfile.ZipFile(io.BytesIO(book)) as zf:
        wb = ElementTree.fromstring(zf.read("xl/workbook.xml"))
        for i, sheet in enumerate(wb.findall("m:sheets/m:sheet", ns), start=1):
            root = ElementTree.fromstring(zf.read(f"xl/worksheets/sheet{i}.xml"))
            rows = []
            for row in root.findall("m:sheetData/m:row", ns):
                cells = []
                for c in row.findall("m:c", ns):
                    t = c.find("m:is/m:t", ns)
                    v = c.find("m:v", ns)
                    cells.append(t.text if t is not None else (v.text if v is not None else ""))
                rows.append(cells)
            out[sheet.get("name")] = rows
    return out


# ── справжні роути ─────────────────────────────────────────────────────────
@pytest.fixture
def app_db(monkeypatch, engine):
    from app import license as license_module
    from app.auth import hash_password
    from app.models import User
    from app.routers import deps
    from app.settings_store import set_setting

    def session_factory():
        return Session(engine, expire_on_commit=False)

    monkeypatch.setattr(deps, "SessionLocal", session_factory)
    monkeypatch.setattr(web, "SessionLocal", session_factory, raising=False)
    private = Ed25519PrivateKey.generate()
    monkeypatch.setattr(license_module, "_PUBLIC_KEY_BYTES", private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw))
    with session_factory() as s:
        set_setting(s, "license_key", license_module.encode_license_key(
            {"machine_id": license_module.get_machine_id(), "customer": "tests",
             "issued_at": "2026-01-01T00:00:00", "expires_at": "2099-01-01T00:00:00"}, private))
        for (name, password), role in ((OPERATOR, "оператор"), (ADMIN, "адмін")):
            s.add(User(username=name, password_hash=hash_password(password), full_name=name, role=role))
        _seed(s)
        sg.set_section_state(s, "stats", sg.OPEN)
        s.commit()
    deps.clear_global_badge_cache()
    data.clear_cache()
    yield
    deps.clear_global_badge_cache()


def _client(user) -> MiniClient:
    client = MiniClient(web.app)
    status, _, _ = client.login(*user)
    assert status in (200, 303)
    return client


def test_overview_page_renders(app_db):
    status, _, body = _client(OPERATOR).get("/stats?" + urlencode(CUSTOM))
    assert status == 200
    assert 'id="sx-root"' in body and "Середньо на робочий день" in body
    assert 'data-day="2026-08-21"' in body and "is-sel" in body
    assert "вкладки є за 3 з 5 робочих днів" in body
    assert "? 2 без кількості" in body
    assert ">Техніки<" not in body, "оператор не бачить вкладку «Техніки»"


def test_admin_sees_techs_tab_and_stage_placeholder(app_db):
    client = _client(ADMIN)
    _, _, body = client.get("/stats?" + urlencode(CUSTOM))
    assert re.search(r"Техніки<span class=\"sx-lock\">адмін", body)
    status, _, body = client.get("/stats?" + urlencode({**CUSTOM, "tab": "techs"}))
    assert status == 200 and "етап 3" in body


def test_operator_cannot_open_techs_by_url(app_db):
    _, _, body = _client(OPERATOR).get("/stats?" + urlencode({**CUSTOM, "tab": "techs"}))
    assert "Середньо на робочий день" in body


def test_htmx_gets_a_fragment(app_db):
    status, _, body = _client(OPERATOR).get("/stats?" + urlencode({**CUSTOM, "tab": "intake"}), headers={"HX-Request": "true"})
    assert status == 200 and "<html" not in body and 'id="sx-root"' in body
    assert "Матеріали по тижнях" in body and "збирається з 12.10.26" in body


def test_side_list_route(app_db):
    status, _, body = _client(OPERATOR).get("/stats/list?" + urlencode({**CUSTOM, "day": "2026-08-17"}))
    assert status == 200 and "/orders/" in body and "Список у Excel" in body and "24122" in body


def test_export_routes_answer_xlsx(app_db):
    client = _client(OPERATOR)
    for path in ("/stats/export?", "/stats/list/export?"):
        status, headers, _ = client.get(path + urlencode({**CUSTOM, "day": "2026-08-17"}))
        assert status == 200 and headers["content-type"].startswith("application/vnd.openxmlformats")
        assert "attachment" in headers["content-disposition"]
