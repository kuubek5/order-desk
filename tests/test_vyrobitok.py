"""Правила підрахунку «Виробітку» — відкалібровані проти реальної відомості.

Ловить саме те, на чому легко помилитись у грошах: джерело→колонка,
виключення переробок і архівних, пріоритет правки над авто, і математику
підков (одиниці входять у цирконій, оплата вдвічі).
"""

from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import Material, Order, VyrobitokCell
from app.services.vyrobitok import compute_month, save_month_settings, set_cell


AUG = "%d.08.26"


def _db() -> Session:
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return Session(engine)


def _materials(db: Session) -> dict[str, int]:
    ids = {}
    for name in ("Цирконій", "ПММА", "Віск", "СЛМ", "Титан"):
        m = Material(name=name, is_production=True)
        db.add(m)
        db.flush()
        ids[name] = m.id
    db.commit()
    return ids


def _order(db, *, source, material_id, qty, day=5, mill_count=None, archived=False):
    o = Order(
        source=source,
        sheet_tab=AUG % day,
        row_number=day,
        material_id=material_id,
        quantity=str(qty),
        mill_count=mill_count,
        status="відфрезеровано",
    )
    if archived:
        o.archived_at = date(2026, 9, 1)
    db.add(o)
    db.commit()
    return o


def _totals(db):
    return compute_month(db, 2026, 8).totals


def test_units_sum_into_source_and_material_columns():
    db = _db()
    mat = _materials(db)
    _order(db, source="lab", material_id=mat["Цирконій"], qty=30)
    _order(db, source="lab", material_id=mat["Цирконій"], qty=12)
    _order(db, source="email", material_id=mat["Цирконій"], qty=50)
    _order(db, source="sheet_client", material_id=mat["Цирконій"], qty=5)
    _order(db, source="lab", material_id=mat["ПММА"], qty=7)
    _order(db, source="lab", material_id=mat["Титан"], qty=3)

    totals = _totals(db)
    assert totals["lab_zr"] == 42
    # email + sheet_client обидва йдуть у «Пошта».
    assert totals["mail_zr"] == 55
    assert totals["lab_pmma"] == 7
    assert totals["lab_ti"] == 3


def test_rework_is_excluded():
    db = _db()
    mat = _materials(db)
    _order(db, source="lab", material_id=mat["Цирконій"], qty=20)
    # «Який раз фрезерується» >= 2 → переробка, у виробіток не входить.
    _order(db, source="lab", material_id=mat["Цирконій"], qty=99, mill_count="2")
    assert _totals(db)["lab_zr"] == 20


def test_archived_is_excluded():
    db = _db()
    mat = _materials(db)
    _order(db, source="lab", material_id=mat["Цирконій"], qty=20)
    _order(db, source="lab", material_id=mat["Цирконій"], qty=99, archived=True)
    assert _totals(db)["lab_zr"] == 20


def test_slm_not_counted_from_orders():
    db = _db()
    mat = _materials(db)
    # СЛМ у чергу (Orders) не потрапляє; навіть якщо Order з матеріалом СЛМ є,
    # табель його НЕ рахує з Orders — число СЛМ приходить лише зі синку (клітинки).
    _order(db, source="lab", material_id=mat["СЛМ"], qty=40)
    assert _totals(db)["lab_slm"] == 0


def _row(*, naryad="", client=False, kind="", mat="", qty=""):
    from types import SimpleNamespace
    return SimpleNamespace(
        work_order_no=naryad, is_client_row=client, kind=kind,
        material_color=mat, quantity=qty,
    )


def test_slm_classifier_lab_file_and_ignored():
    from app.services.vyrobitok import slm_totals_from_rows
    rows = [
        # наряд-body СЛМ — ігнорується (наряд могли завести, роботу не зробити).
        _row(naryad="29203", kind="каркас гвинтова", mat="слм", qty="4"),
        # CADCAM Команда — лаб, к-сть у колонці кольору.
        _row(client=True, kind="CADCAM Команда", mat="4", qty=""),
        # клієнти — файловий СЛМ, к-сть у колонці кількості.
        _row(client=True, kind="CadCam Energy", mat="", qty="4"),
        _row(client=True, kind="Zanoviak", mat="", qty="12"),
        # моделі — не наша робота, не СЛМ.
        _row(client=True, kind="моделі", mat="", qty="5"),
        # звичайна клієнтська фрезерна робота (є і матеріал, і к-сть) — не СЛМ.
        _row(client=True, kind="Basarab", mat="mono a3", qty="2"),
    ]
    lab, mail = slm_totals_from_rows(rows)
    assert lab == 4
    assert mail == 16  # 4 + 12
    # Пастка CADCAM Команда (лаб) ≠ CadCam Energy (клієнт) — не переплутано.


def test_slm_totals_flow_into_tally_and_override_wins():
    db = _db()
    _materials(db)
    from app.services.vyrobitok import store_slm_totals
    # Синк записав СЛМ у клітинки за день.
    store_slm_totals(db, date(2026, 8, 5), lab_units=4, mail_units=115)
    db.commit()
    totals = _totals(db)
    assert totals["lab_slm"] == 4
    assert totals["mail_slm"] == 115

    # Правка оператора б'є авто; повторний запис синку її не чіпає.
    set_cell(db, date(2026, 8, 5), "mail_slm", 120)
    store_slm_totals(db, date(2026, 8, 5), lab_units=4, mail_units=115)
    db.commit()
    grid = compute_month(db, 2026, 8)
    cell = next(r for r in grid.rows if r["dayn"] == 5)["cells"]["mail_slm"]
    assert cell["num"] == 120 and cell["auto"] == 115 and cell["edited"] is True


def test_override_wins_over_auto_and_marks_edited():
    db = _db()
    mat = _materials(db)
    _order(db, source="lab", material_id=mat["Цирконій"], qty=30)

    set_cell(db, date(2026, 8, 5), "lab_zr", 42)
    grid = compute_month(db, 2026, 8)
    row = next(r for r in grid.rows if r["dayn"] == 5)
    cell = row["cells"]["lab_zr"]
    assert cell["num"] == 42
    assert cell["auto"] == 30
    assert cell["edited"] is True
    assert grid.totals["lab_zr"] == 42

    # Стерти правку → повертається авто, мітка знята.
    set_cell(db, date(2026, 8, 5), "lab_zr", None)
    grid = compute_month(db, 2026, 8)
    cell = next(r for r in grid.rows if r["dayn"] == 5)["cells"]["lab_zr"]
    assert cell["num"] == 30 and cell["edited"] is False


def test_manual_columns_have_no_edited_marker():
    db = _db()
    _materials(db)
    set_cell(db, date(2026, 8, 5), "disks", 7)
    grid = compute_month(db, 2026, 8)
    cell = next(r for r in grid.rows if r["dayn"] == 5)["cells"]["disks"]
    assert cell["num"] == 7
    assert cell["edited"] is False  # ручна колонка — не «виправлення CRM»


def test_auto_snapshot_survives_archiving():
    db = _db()
    mat = _materials(db)
    o = _order(db, source="lab", material_id=mat["Цирконій"], qty=30)
    # Перегляд поки день «живий» знімає авто у сховище.
    assert _totals(db)["lab_zr"] == 30
    snap = db.query(VyrobitokCell).filter_by(day=date(2026, 8, 5), col_key="lab_zr").one()
    assert snap.auto_value == 30
    # Робота зникла з живої таблиці (архів) — знімок тримає число.
    o.archived_at = date(2026, 9, 1)
    db.commit()
    assert _totals(db)["lab_zr"] == 30


def test_money_coefficient_and_pidkovy_double_rate():
    db = _db()
    mat = _materials(db)
    # 200 цирконію (лаб 150 + пошта 50), 20 з них — підкови; диски 100.
    _order(db, source="lab", material_id=mat["Цирконій"], qty=150, day=5)
    _order(db, source="email", material_id=mat["Цирконій"], qty=50, day=6)
    set_cell(db, date(2026, 8, 7), "disks", 100)
    set_cell(db, date(2026, 8, 7), "pidkovy", 20)

    m = compute_month(db, 2026, 8).money
    assert m["zr_total"] == 200
    assert m["disks_total"] == 100
    assert m["coefficient"] == pytest.approx(2.0)  # 200 / 100
    types = {t["name"]: t for t in m["types"]}
    # Цирконій без підков = 180; підкови окремо, ставка вдвічі.
    assert types["Цирконій"]["units"] == 180
    assert types["Підкови"]["units"] == 20
    assert types["Підкови"]["rate"] == pytest.approx(types["Цирконій"]["rate"] * 2)
    # Разом одиниць = 200 (підкови всередині цирконію, не додаються зверху).
    assert m["total_units"] == 200


def test_rate_out_of_band_warns_without_guessing():
    db = _db()
    mat = _materials(db)
    # Коефіцієнт 30 (>26,7) → поза довідником, попередження.
    _order(db, source="lab", material_id=mat["Цирконій"], qty=300, day=5)
    set_cell(db, date(2026, 8, 6), "disks", 10)
    grid = compute_month(db, 2026, 8)
    assert grid.money["rate_out_of_band"] is True
    assert grid.warn is not None

    # Оператор задав ставку → попередження зникло, ставка взята з поля.
    save_month_settings(db, 2026, 8, rate_override="0,7")
    grid = compute_month(db, 2026, 8)
    assert grid.money["rate_out_of_band"] is False
    assert grid.money["rate_zn"] == pytest.approx(0.7)


def test_month_settings_divisor_and_kurs():
    db = _db()
    mat = _materials(db)
    _order(db, source="lab", material_id=mat["Цирконій"], qty=100, day=5)
    save_month_settings(db, 2026, 8, kurs="50", people_count=4)
    m = compute_month(db, 2026, 8).money
    assert m["people_count"] == 4
    assert m["share"] == pytest.approx(100 / 4)


def _orow(row_number, *, work_order_no="", quantity="", material_color="", kind="",
          technician_name="", mill_count=""):
    from app.parser import OrderRow
    return OrderRow(
        row_number=row_number, seq_no=str(row_number), work_order_no=work_order_no,
        quantity=quantity, material_color=material_color, kind=kind, due_time=None,
        job_code="", technician_name=technician_name, cam_comment="", sum3d_id="",
        calculated="", milled="", last_milled_date="", mill_count=mill_count,
    )


def test_offline_gap_then_catchup_fills_all_days():
    # Сценарій: CRM стояла. Спершу синхронізувались дні 3-4, потім (після
    # догону) додались 5-7. Табель — сума всіх, без «дірок»: він читає ЩО Є в
    # базі, а синк-догін підтягує пропущене (див. test_sheet_sync_service:
    # test_catches_up_missed_days_after_being_offline).
    db = _db()
    mat = _materials(db)
    for d in (3, 4):
        _order(db, source="lab", material_id=mat["Цирконій"], qty=10, day=d)
    assert compute_month(db, 2026, 8).totals["lab_zr"] == 20
    for d in (5, 6, 7):
        _order(db, source="lab", material_id=mat["Цирконій"], qty=10, day=d)
    assert compute_month(db, 2026, 8).totals["lab_zr"] == 50


def test_catchup_resync_of_same_tab_does_not_double():
    # Догін часто ПЕРЕЧИТУЄ вже імпортовані дні. Проганяємо sync_tab двічі на тих
    # самих рядках (фрезерна робота + блок СЛМ) і перевіряємо, що табель не
    # подвоївся: Orders апсертяться за (вкладка,рядок), СЛМ-клітинка ЗАМІНЮЄ
    # auto_value, а не додає.
    from app.sync import sync_tab
    from app.models import Order
    db = _db()  # ensure_seeded усередині sync_tab насіює каталог матеріалів
    rows = [
        _orow(1, work_order_no="24001", quantity="5", material_color="mono a3", technician_name="Іван"),
        _orow(50, material_color="4", kind="CADCAM Команда"),   # лаб СЛМ 4
        _orow(51, quantity="12", kind="CadCam Energy"),          # файловий СЛМ 12
    ]
    sync_tab(db, "05.08.26", rows)
    db.commit()
    t1 = compute_month(db, 2026, 8).totals
    sync_tab(db, "05.08.26", rows)   # догін тієї самої вкладки
    db.commit()
    t2 = compute_month(db, 2026, 8).totals

    assert t1["lab_zr"] == 5 and t1["lab_slm"] == 4 and t1["mail_slm"] == 12
    assert t2 == t1                               # без подвоєння
    assert db.query(Order).count() == 1           # СЛМ у Orders не потрапляє


def test_override_survives_catchup_resync():
    # Оператор виправив СЛМ; наступний синк-догін не має затерти правку
    # (пише лише auto_value).
    from app.sync import sync_tab
    db = _db()
    rows = [_orow(50, material_color="4", kind="CADCAM Команда"),
            _orow(51, quantity="12", kind="CadCam Energy")]
    sync_tab(db, "05.08.26", rows)
    db.commit()
    set_cell(db, date(2026, 8, 5), "mail_slm", 99)
    sync_tab(db, "05.08.26", rows)
    db.commit()
    cell = next(r for r in compute_month(db, 2026, 8).rows if r["dayn"] == 5)["cells"]["mail_slm"]
    assert cell["num"] == 99 and cell["auto"] == 12 and cell["edited"] is True


def test_today_row_never_marked_off():
    # Сьогодні може випасти на вихідний; тоді порожній off-рядок перебивав би
    # підсвітку today (у шаблоні off має пріоритет). Інваріант: today ≠ off.
    from app.business_day import business_today
    db = _db()
    _materials(db)
    t = business_today()
    grid = compute_month(db, t.year, t.month)
    today_row = next(r for r in grid.rows if r["is_today"])
    assert today_row["is_off"] is False


def _req(session=None):
    from types import SimpleNamespace
    return SimpleNamespace(
        session=session if session is not None else {},
        headers={}, client=SimpleNamespace(host="127.0.0.1"),
    )


def test_pin_unlocked_respects_expiry():
    import time
    from app.routers import vyrobitok as vr
    assert vr._pin_unlocked(_req({vr._PIN_SESSION_KEY: time.time() + 100})) is True
    # Протух — знову під кодом.
    assert vr._pin_unlocked(_req({vr._PIN_SESSION_KEY: time.time() - 1})) is False
    assert vr._pin_unlocked(_req({})) is False


def test_pin_required_only_when_set_and_locked(monkeypatch):
    import time
    from app.routers import vyrobitok as vr
    monkeypatch.setattr(vr, "get_setting", lambda db, k: "2468")
    assert vr._pin_required(_req({}), None) is True
    assert vr._pin_required(_req({vr._PIN_SESSION_KEY: time.time() + 100}), None) is False
    # Код не заданий — розділ відкритий.
    monkeypatch.setattr(vr, "get_setting", lambda db, k: "")
    assert vr._pin_required(_req({}), None) is False


def test_lock_clears_permission(monkeypatch):
    import time
    from app.routers import vyrobitok as vr
    monkeypatch.setattr(vr, "get_current_user", lambda r, db: object())
    req = _req({vr._PIN_SESSION_KEY: time.time() + 100})
    resp = vr.post_vyrobitok_lock(req, None)
    assert vr._PIN_SESSION_KEY not in req.session
    assert resp.status_code == 303


def test_body_partial_renders():
    """Тіло-партіал рендериться без Jinja-помилок (макрос клітинки, підсумок,
    гроші, опаки) — парсинг шаблону цього не ловить, лише рендер."""
    from app.routers.deps import templates
    from app.services.vyrobitok import HUE, MATERIAL_COLS, OPAK_PEOPLE

    db = _db()
    mat = _materials(db)
    _order(db, source="lab", material_id=mat["Цирконій"], qty=30)
    grid = compute_month(db, 2026, 8)
    html = templates.env.get_template("_vyrobitok_body.html").render(
        grid=grid, material_cols=MATERIAL_COLS, opak_people=OPAK_PEOPLE, hue=HUE
    )
    assert 'id="vyrobitok-body"' in html
    assert "Разом одиниць" in html


# ── Добір СЛМ за минулий місяць ──────────────────────────────────────────────
# СЛМ пише синк, а він читає лише сьогодні±1: дні, чиї вкладки вийшли з вікна
# до появи цього коду на машині, лишились із порожніми СЛМ-колонками. Добір
# перечитує вкладки ОДНОГО місяця й дописує тільки їх.

class _FakeWorksheet:
    def __init__(self, title, raw):
        self.title = title
        self._raw = raw
        self.reads = 0

    def get_all_values(self):
        self.reads += 1
        return self._raw


class _FakeSpreadsheet:
    def __init__(self, worksheets):
        self._worksheets = worksheets

    def worksheets(self):
        return self._worksheets


def _full_raw(marker):
    """Сире читання «з даними»: 6 рядків шапки + один рядок-маркер."""
    return [["h"]] * 6 + [[marker]]


def _patch_backfill(monkeypatch, spreadsheet, totals_by_marker):
    import app.services.vyrobitok_backfill as bf

    monkeypatch.setattr(bf, "open_spreadsheet", lambda db=None: spreadsheet)
    monkeypatch.setattr(bf, "call_with_retry", lambda fn, *a, **kw: fn(*a, **kw))
    monkeypatch.setattr(bf, "quota_is_tight", lambda: False)
    monkeypatch.setattr(bf, "header_mismatches", lambda raw: [])
    monkeypatch.setattr(bf, "parse_rows", lambda raw: raw[6:])
    monkeypatch.setattr(
        bf, "slm_totals_from_rows", lambda rows: totals_by_marker[rows[0][0]]
    )
    return bf


def test_slm_backfill_reads_only_that_month_and_fills_cells(monkeypatch):
    db = _db()
    _materials(db)
    sheets = [
        _FakeWorksheet("31.07.26", _full_raw("july")),   # інший місяць
        _FakeWorksheet("01.08.26", _full_raw("d1")),
        _FakeWorksheet("31.08.26", _full_raw("d31")),
        _FakeWorksheet("01.09.26", _full_raw("sept")),   # інший місяць
        _FakeWorksheet("Довідник", _full_raw("skip")),   # не датована вкладка
    ]
    bf = _patch_backfill(
        monkeypatch,
        _FakeSpreadsheet(sheets),
        {"july": (9, 9), "d1": (2, 150), "d31": (4, 115), "sept": (7, 7), "skip": (0, 0)},
    )

    result = bf.backfill_slm_month(db, 2026, 8)

    assert result.tabs == 2
    assert (result.lab_units, result.mail_units) == (6, 265)
    # Прочитані лише вкладки серпня — сусідні місяці не чіпаємо.
    assert [w.title for w in sheets if w.reads] == ["01.08.26", "31.08.26"]

    grid = compute_month(db, 2026, 8)
    day1 = next(r for r in grid.rows if r["dayn"] == 1)["cells"]
    day31 = next(r for r in grid.rows if r["dayn"] == 31)["cells"]
    assert (day1["lab_slm"]["num"], day1["mail_slm"]["num"]) == (2, 150)
    assert (day31["lab_slm"]["num"], day31["mail_slm"]["num"]) == (4, 115)


def test_slm_backfill_skips_empty_read_instead_of_zeroing(monkeypatch):
    """Транзієнтний збій проксі не має затирати реальне число нулем —
    той самий запобіжник, що в sync_tab."""
    db = _db()
    _materials(db)
    from app.services.vyrobitok import store_slm_totals

    store_slm_totals(db, date(2026, 8, 5), lab_units=4, mail_units=115)
    db.commit()

    sheets = [_FakeWorksheet("05.08.26", [["h"]] * 2)]  # обрізане читання
    bf = _patch_backfill(monkeypatch, _FakeSpreadsheet(sheets), {})

    result = bf.backfill_slm_month(db, 2026, 8)

    assert result.tabs == 0 and result.skipped_empty == 1
    totals = _totals(db)
    assert (totals["lab_slm"], totals["mail_slm"]) == (4, 115)


def test_slm_backfill_leaves_operator_override_alone(monkeypatch):
    db = _db()
    _materials(db)
    set_cell(db, date(2026, 8, 5), "mail_slm", 120)

    sheets = [_FakeWorksheet("05.08.26", _full_raw("d5"))]
    bf = _patch_backfill(monkeypatch, _FakeSpreadsheet(sheets), {"d5": (4, 115)})
    bf.backfill_slm_month(db, 2026, 8)

    grid = compute_month(db, 2026, 8)
    cell = next(r for r in grid.rows if r["dayn"] == 5)["cells"]["mail_slm"]
    assert cell["num"] == 120 and cell["auto"] == 115 and cell["edited"] is True


def test_slm_backfill_route_starts_once_and_redirects(monkeypatch):
    from app.routers import vyrobitok as vr

    monkeypatch.setattr(vr, "get_current_user", lambda r, db: object())
    monkeypatch.setattr(vr, "_pin_required", lambda r, db: False)
    calls = []
    monkeypatch.setattr(
        vr, "start_slm_backfill", lambda y, m: calls.append((y, m)) or len(calls) == 1
    )

    req = _req({})
    resp = vr.post_vyrobitok_slm_backfill(req, year=2026, month=8, db=None)
    assert calls == [(2026, 8)]
    assert resp.status_code == 303 and resp.headers["location"] == "/vyrobitok?year=2026&month=8"
    assert "Добір СЛМ почато" in req.session["vyrobitok_flash"]["message"]

    # Друге натискання не запускає другий прогін.
    vr.post_vyrobitok_slm_backfill(_req({}), year=2026, month=8, db=None)
    assert len(calls) == 2 and calls[1] == (2026, 8)


def test_slm_backfill_route_blocked_while_pin_locked(monkeypatch):
    """Кнопка живе на закритому розділі — під замком роут не читає таблицю."""
    from app.routers import vyrobitok as vr

    monkeypatch.setattr(vr, "get_current_user", lambda r, db: object())
    monkeypatch.setattr(vr, "_pin_required", lambda r, db: True)
    started = []
    monkeypatch.setattr(vr, "start_slm_backfill", lambda y, m: started.append(1) or True)

    resp = vr.post_vyrobitok_slm_backfill(_req({}), year=2026, month=8, db=None)
    assert started == []
    assert resp.status_code == 303 and resp.headers["location"] == "/vyrobitok"


def test_vyrobitok_page_renders_backfill_button():
    """Сторінка рендериться цілком і кнопка добору в ній є — парсинг шаблону
    цього не ловить (кнопка сидить у гілці без ПІН-гейта)."""
    import types
    from app.routers.deps import templates
    from app.services.vyrobitok import HUE, MATERIAL_COLS, OPAK_PEOPLE

    db = _db()
    grid = compute_month(db, 2026, 8)
    user = types.SimpleNamespace(role="адмін", username="t", id=1, display_name="t")
    req = types.SimpleNamespace(session={}, scope={"path": "/vyrobitok"}, headers={})
    html = templates.env.get_template("vyrobitok.html").render(
        request=req, user=user, grid=grid, material_cols=MATERIAL_COLS,
        opak_people=OPAK_PEOPLE, hue=HUE, pin_required=False, pin_protected=False,
        slm_backfill_running=False,
        flash={"kind": "success", "message": "готово"},
    )
    assert 'action="/vyrobitok/slm-backfill"' in html
    assert 'name="month" value="8"' in html
    assert "vt-flash" in html
