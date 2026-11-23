"""Підказка диска при прийнятті листа (власник 12.11.26).

Правила власника: запас мінімум 1 мм; найменша висота, що фізично є; ПММА —
саме вона; мультиколор — вона або ще одна вище; Monolith — завжди на крок
вище; спершу початий диск, у який робота влізе; доступ — поіменно від адміна.
"""

from __future__ import annotations

import json
import math
import os
import struct
import time
from datetime import datetime
from types import SimpleNamespace

import pytest

from app.services import disc_suggest
from tests.asgi_client import MiniClient
from tests.test_settings_slabs_render import ADMIN, OPERATOR, app_db  # noqa: F401 — фікстура
from tests.test_sum3d_disc import _blank, _square

HEIGHTS = [10, 12, 14, 16, 18, 20, 22, 25]


@pytest.fixture(autouse=True)
def _fresh_fit_cache():
    """Відповіді розстановки живуть у пам'яті процесу — між тестами чисто."""
    disc_suggest._fit_cache.clear()
    yield
    disc_suggest._fit_cache.clear()


@pytest.mark.parametrize("material, thickness, expected", [
    # ПММА (власник 22.11.26): будь-який диск не нижчий за потрібну висоту —
    # для 8 мм годиться й 25. Раніше тут була лише найменша ([16], [12]).
    ("пмма A2", 14.7, [16, 18, 20, 22, 25]),  # 14,7 + 0,5 = 15,2 → від 16, бо 15 немає
    ("пмма A2", 11.0, [12, 14, 16, 18, 20, 22, 25]),
    ("воск", 11.0, [12]),  # віск і далі — саме ця висота
    ("емо A2", 12.5, [14, 16]),  # мультиколор: ця або ще одна вище
    ("моно A2", 12.5, [16]),  # 14 впритул (1,5 мм < 2) → Monolith на крок вище
    ("моно A2", 11.6, [14, 16]),  # до 14 запас 2,4 мм — кроку немає, як мультиколор
    ("моно A2", 23.5, [25]),  # впритул, а вищого за 25 немає
    ("пмма A2", 24.5, [25]),  # 25,0 — запас 0,5 мм (власник 14.11.26; було 1 мм)
    ("пмма A2", 24.6, []),  # 25,1 — такого диска немає
])
def test_target_height_rules(material, thickness, expected):
    heights, why = disc_suggest.target_heights(thickness, HEIGHTS, disc_suggest.want_of(material))
    assert heights == expected, why


def test_want_reads_line_family_and_shade():
    w = disc_suggest.want_of("моно А3.5")
    assert (w.line, w.family, set(w.shades), w.multilayer) == ("monolith", "Цирконій", {"a3.5"}, True)
    p = disc_suggest.want_of("pmma a2")
    assert (p.line, p.family, p.multilayer) == (None, "ПММА", False)


def _stl(path, w, d, h):
    """Двійковий STL-коробка w×d×h мм (лише габарит важливий)."""
    tris = [((0, 0, 0), (w, 0, 0), (w, d, h)), ((0, 0, 0), (0, d, 0), (w, d, h))]
    data = b"\0" * 80 + struct.pack("<I", len(tris))
    for a, b, c in tris:
        data += struct.pack("<12fH", 0, 0, 0, *a, *b, *c, 0)
    path.write_bytes(data)
    return path


def _disc(root, material, height, name, contours):
    folder = root / material / str(height)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.blk").write_bytes(_blank(contours))


@pytest.fixture
def shop(tmp_path):
    root = tmp_path / "discs"
    disc_suggest._scan_cache.clear()
    used = [_square(x, y, 6) for x in (-25, 0, 25) for y in (-25, 0, 25)]  # 9 вирізів
    _disc(root, "zr", 14, "zr14_14-Monolith-A2-x30", used[:2])
    _disc(root, "zr", 16, "zr16_16-Monolith-A2-x37", used[:3])  # початий, місця досить
    _disc(root, "zr", 16, "zr16_16-Monolith-A2-x40", [])  # новий у теці
    _disc(root, "zr", 16, "zr16_16-Monolith-A2-x41", [_square(0, 0, 44, n=48)])  # вирізано ~88 %
    _disc(root, "zr", 16, "zr16_16-Monolith-A3-x5", [])  # чужий колір
    _disc(root, "pmma", 16, "pmma16_16-a2-x200", [])
    stl = tmp_path / "mail"
    stl.mkdir()
    files = [_stl(stl / f"case_{t}-anatomy_cad.stl", 9, 10, 10.0) for t in (11, 12, 21)]
    atts = [SimpleNamespace(filename=f.name, saved_path=str(f)) for f in files]
    return root, atts


def test_monolith_gets_started_disc_one_step_higher(shop):
    root, atts = shop
    hint = disc_suggest.suggest("моно A2", atts, str(root))
    # 10 мм × 1.25 = 12,5 + 1 = 13,5 → 14; Monolith → 16.
    assert hint.heights == [16]
    assert hint.best is not None and hint.best.name == "zr16_16-Monolith-A2-x37"
    assert hint.best.started
    names = {a.name for a in hint.alts}
    assert "zr16_16-Monolith-A3-x5" not in names, "чужий колір не пропонується"
    assert hint.new_disc == "новий диск 16 мм Monolith A2"


def test_nearly_cut_disc_is_not_offered(shop):
    root, atts = shop
    hint = disc_suggest.suggest("моно A2", atts, str(root))
    assert "zr16_16-Monolith-A2-x41" not in {hint.best.name, *(a.name for a in hint.alts)}


def test_without_exocad_height_says_so(shop):
    root, _ = shop
    other = [SimpleNamespace(filename="scan.stl", saved_path="")]
    hint = disc_suggest.suggest("моно A2", other, str(root))
    assert hint.best is None and "висоту не визначено" in hint.note


def test_access_is_per_person_and_off_by_default(app_db, shop):  # noqa: F811
    from app.models import Attachment, EmailMessage, User
    from app.settings_store import set_setting

    root, atts = shop
    app, factory = app_db
    with factory() as db:
        set_setting(db, "cam_blanks_path", str(root))
        mail = EmailMessage(uid="1", status="нове", attachments_status="ready",
                            received_at=datetime(2026, 11, 12, 9))
        db.add(mail)
        db.flush()
        for a in atts:
            db.add(Attachment(email_message_id=mail.id, filename=a.filename, saved_path=a.saved_path))
        db.commit()
        mid = mail.id
        admin_id = db.query(User).filter_by(username=ADMIN[0]).one().id

    admin = MiniClient(app)
    admin.login(*ADMIN)
    assert admin.get(f"/mail/{mid}/disc-hint?material_color=%D0%BC%D0%BE%D0%BD%D0%BE%20A2")[2] == "", "вимкнено за замовчуванням"

    assert admin.post("/settings/disc-suggest", {"enabled": "1", f"user_{admin_id}": "1"})[0] == 303
    with factory() as db:
        from app.settings_store import get_setting
        assert json.loads(get_setting(db, disc_suggest.SETTINGS_USERS_KEY)) == [admin_id]
    body = admin.get(f"/mail/{mid}/disc-hint?material_color=%D0%BC%D0%BE%D0%BD%D0%BE%20A2")[2]
    assert "zr16_16-Monolith-A2-x37" in body and "вільно" in body

    operator = MiniClient(app)
    operator.login(*OPERATOR)
    assert operator.get(f"/mail/{mid}/disc-hint?material_color=%D0%BC%D0%BE%D0%BD%D0%BE%20A2")[2] == "", "оператору не дали"
    assert operator.post("/settings/disc-suggest", {"enabled": "1"})[0] == 403


def test_conveyor_hint_packs_all_letters_of_the_main_colour(app_db, shop, tmp_path):  # noqa: F811
    """Власник 22.11.26: у мультипрорахунку Конвеєра — один диск на роботи ВСІХ
    листів. Мультипрорахунок збирає один колір, тож лист «інший колір» у
    розстановку не йде (лише лічильник), а висота — за найвищою роботою."""
    from app.models import Attachment, EmailMessage, User

    root, atts = shop
    app, factory = app_db
    tall = tmp_path / "tall"
    tall.mkdir()
    # Висока робота в листі іншого кольору: якби її врахували — висота 25 мм.
    big = _stl(tall / "case_36-anatomy_cad.stl", 9, 10, 19.0)
    with factory() as db:
        from app.settings_store import set_setting

        set_setting(db, "cam_blanks_path", str(root))
        ids = []
        for files in (atts[:1], atts[1:], [SimpleNamespace(filename=big.name, saved_path=str(big))]):
            mail = EmailMessage(uid=str(len(ids) + 10), status="нове", attachments_status="ready",
                                received_at=datetime(2026, 11, 22, 9))
            db.add(mail)
            db.flush()
            for a in files:
                db.add(Attachment(email_message_id=mail.id, filename=a.filename, saved_path=a.saved_path))
            ids.append(mail.id)
        db.commit()
        admin_id = db.query(User).filter_by(username=ADMIN[0]).one().id

    admin = MiniClient(app)
    admin.login(*ADMIN)
    form = []
    for eid, material in zip(ids, ("моно A2", "моно A2", "моно A3")):
        form += [("batch_email_id", str(eid)), ("client_name", "К"), ("material_color", material), ("quantity", "1")]
    assert admin.post("/mail/batch-disc-hint", form)[2] == "", "вимкнено за замовчуванням"

    assert admin.post("/settings/disc-suggest", {"enabled": "1", f"user_{admin_id}": "1"})[0] == 303
    body = admin.post("/mail/batch-disc-hint", form)[2]
    assert "Диск для 2 листів разом" in body
    assert "не враховано 1" in body
    assert "zr16_16-Monolith-A2-x37" in body, "той самий диск, що й для трьох конструкцій одного листа"
    assert "→ 16 мм" in body, "висота за роботами кольору більшості, без високої роботи іншого кольору"


def test_pmma_takes_any_taller_disc_and_finishes_the_fullest_started_one(tmp_path):
    """Власник 22.11.26: для ПММА висота не підбирається під роботу — робота 8 мм
    іде й на 25. Серед початих першим той, де менше вільного (добити диск),
    незалежно від висоти; новий — лише запасним, і найнижчий."""
    root = tmp_path / "discs"
    disc_suggest._scan_cache.clear()
    ring = [_square(28 * math.cos(a), 28 * math.sin(a), 12) for a in (0, 1.05, 2.1, 3.15, 4.2, 5.25)]
    _disc(root, "pmma", 10, "pmma10_10-a2-x11", [_square(30, 0, 8)])  # початий, вільно ~97 %
    _disc(root, "pmma", 25, "pmma25_25-a2-x251", ring)  # початий, вільно ~60 %
    _disc(root, "pmma", 12, "pmma12_12-a2-x20", [])  # новий
    _disc(root, "pmma", 20, "pmma20_20-a2-x30", [])  # новий, вищий
    _disc(root, "zr", 25, "zr25_25-Monolith-A2-x9", [])  # чужий матеріал
    stl = tmp_path / "mail"
    stl.mkdir()
    atts = [SimpleNamespace(filename=f.name, saved_path=str(f))
            for f in (_stl(stl / f"case_{t}-anatomy_cad.stl", 9, 10, 8.0) for t in (11, 21))]

    hint = disc_suggest.suggest("pmma a2", atts, str(root))
    assert hint.heights[0] == 10 and 25 in hint.heights and hint.any_higher
    order = [hint.best.name] + [p.name for p in hint.alts]
    assert order[:2] == ["pmma25_25-a2-x251", "pmma10_10-a2-x11"], "початий із меншим вільним — першим"
    assert order[2] == "pmma12_12-a2-x20", "новий — найнижчий, що підходить"
    assert hint.new_disc.startswith("новий диск 10 мм")

    # Цирконій правило не змінилось: Monolith 8 мм — 10 або 12 (мультиколор), не 25.
    mono = disc_suggest.want_of("моно A2")
    assert disc_suggest.target_heights(8.0, [10, 12, 14, 25], mono)[0] == [10, 12]


def test_pmma_card_says_from_height_not_a_list():
    from app.routers.deps import templates

    want = disc_suggest.want_of("pmma a2")
    pick = disc_suggest.Pick(name="pmma25_25-a2-x251", height=25, free_pct=60, started=True, paths=[], fit=True)
    hint = disc_suggest.Suggestion(want=want, thickness=8.0, heights=[10, 12, 25], best=pick,
                                   new_disc="новий диск 10 мм", any_higher=True)
    html = templates.env.get_template("_mail_disc_hint.html").render(hint=hint)
    assert "робота ≈8.0 мм → від 10 мм" in html and "(диск вищий)" not in html


def test_audit_tool_runs_on_empty_shop(app_db):  # noqa: F811
    from app.services.mcp_tools import TOOLS_BY_NAME

    _app, factory = app_db
    with factory() as db:
        report = TOOLS_BY_NAME["kmill_disc_suggest_audit"].run(db, {"days": 3})
    assert report["робіт"] == 0 and report["приклади"] == []


def test_opaque_code_gets_only_its_own_discs(tmp_path, shop):
    """«1000» (A3 опак Doceram) — окремий диск, не будь-який A3 (14.11.26)."""
    root, atts = shop
    _disc(root, "zr", 14, "zr14_14-1000-x37", [_square(-25, -25, 6)])
    _disc(root, "zr", 14, "zr14_14-Monolith-A3-x06", [_square(-25, -25, 6)])
    disc_suggest._scan_cache.clear()
    hint = disc_suggest.suggest("1000", atts, str(root))
    assert hint.best is not None and hint.best.name == "zr14_14-1000-x37"
    assert all("Monolith" not in a.name for a in hint.alts)
    assert hint.new_disc.endswith("1000")
    mono = disc_suggest.suggest("моно A3", atts, str(root))
    assert all("1000" not in p.name for p in [mono.best, *mono.alts] if p)


def test_disc_on_machine_comes_first(shop):
    """Цех кладе кілька робіт на диск, що вже стоїть у верстаті — його першим,
    навіть коли інший початий диск майже добитий (14.11.26)."""
    root, atts = shop
    _disc(root, "zr", 16, "zr16_16-Monolith-A2-x52", [])  # новий, але стоїть у верстаті
    disc_suggest._scan_cache.clear()
    seen = datetime(2026, 11, 14, 9)
    hint = disc_suggest.suggest("моно A2", atts, str(root), active={"16-monolith-a2-x52": seen})
    assert hint.best is not None and hint.best.name == "zr16_16-Monolith-A2-x52"
    assert hint.best.on_machine
    assert "zr16_16-Monolith-A2-x37" in {a.name for a in hint.alts}, "початий лишається запасним"
    # Диск з верстата іншої висоти правил висоти не ламає.
    other = disc_suggest.suggest("моно A2", atts, str(root), active={"14-monolith-a2-x30": seen})
    assert other.best is not None and other.best.name == "zr16_16-Monolith-A2-x37"


def test_active_discs_reads_programs_of_recent_days(app_db):  # noqa: F811
    from app.models import MachineReading

    _app, factory = app_db
    now = datetime(2026, 11, 14, 12)
    with factory() as db:
        db.add_all([
            MachineReading(host="h1", captured_at=datetime(2026, 11, 14, 8),
                           iso_name="1zr14_14-Monolith-A3-5-x51_2026-11-14_08-01-02.iso"),
            MachineReading(host="h1", captured_at=datetime(2026, 11, 13, 8),
                           iso_name="2_14-MONOLITH-A2-X47_2026-11-13_08-01-02.iso"),
            MachineReading(host="h2", captured_at=datetime(2026, 11, 5, 8),
                           iso_name="1_16-Monolith-A2-x60_2026-11-05_08-01-02.iso"),  # давно
            MachineReading(host="h2", captured_at=datetime(2026, 11, 14, 13),
                           iso_name="1_18-Monolith-A2-x53_2026-11-14_13-00-00.iso"),  # після
            MachineReading(host="h3", captured_at=datetime(2026, 11, 14, 9),
                           iso_name="2026-11-14_09-00-00.iso"),  # без диска в назві
        ])
        db.commit()
        active = disc_suggest.active_discs(db, until=now)
    # h1 13.11 фрезерував x47, а 14.11 — уже x51: x47 знято, «на верстаті» лише
    # x51 (власник 18.11.26). h2: його останній диск — після `until`, а давніший
    # поза вікном; h3 — назва без диска.
    assert set(active) == {"14-monolith-a3-5-x51"}


def test_active_discs_name_agent_machines_by_key(app_db):  # noqa: F811
    """Покази пишуть ключ «адреса-порт» (агент на 8765): назва верстата мусить
    знаходитись і так — картка писала безіменне «на верстаті» (18.11.26)."""
    from app.models import Machine, MachineReading

    _app, factory = app_db
    with factory() as db:
        db.add(Machine(name="250i-Sec", host="192.168.1.113", port=8765, created_at=datetime(2026, 11, 1)))
        db.add(Machine(name="250i-Tolik", host="192.168.1.81", port=8765, created_at=datetime(2026, 11, 1)))
        db.add_all([
            MachineReading(host="192.168.1.113-8765", captured_at=datetime(2026, 11, 18, 8),
                           iso_name="1_16-Monolith-A1-x26_2026-11-18_08-01-02.iso"),
            MachineReading(host="192.168.1.81-8765", captured_at=datetime(2026, 11, 18, 7),
                           iso_name="2_16-Monolith-A1-x26_2026-11-18_07-00-00.iso"),
        ])
        db.commit()
        machines: dict[str, str] = {}
        active = disc_suggest.active_discs(db, until=datetime(2026, 11, 18, 12), machines=machines)
    assert set(active) == {"16-monolith-a1-x26"}
    assert machines == {"16-monolith-a1-x26": "250i-Sec, 250i-Tolik"}


def test_hint_row_swaps_only_itself():
    """Рядок підказки стоїть у формі з `hx-target="#mail-detail"`; без власної
    цілі htmx успадковував ту й відповідь підказки замінювала всю картку листа
    (власник 14.11.26)."""
    import re
    from pathlib import Path

    html = (Path(__file__).resolve().parents[1] / "app/templates/_mail_detail_panel.html").read_text(encoding="utf-8")
    tag = re.search(r'<div class="mc-disc"[^>]*>', html, re.S)
    assert tag is not None and 'hx-target="this"' in tag.group(0)


def test_disc_on_machine_may_be_higher_than_the_rule(shop):
    """Власник 14.11.26: диск із верстата — і вищий за правило висоти, якщо
    робота влазить. Нижчий за правило — ні (Monolith +1 крок лишається)."""
    root, atts = shop
    _disc(root, "zr", 18, "zr18_18-Monolith-A2-x53", [])
    _disc(root, "pmma", 18, "pmma18_18-a2-x283", [])
    disc_suggest._scan_cache.clear()
    seen = datetime(2026, 11, 14, 9)
    hint = disc_suggest.suggest("моно A2", atts, str(root), active={"18-monolith-a2-x53": seen})
    assert hint.heights == [16]
    assert hint.best is not None and hint.best.name == "zr18_18-Monolith-A2-x53" and hint.best.on_machine
    # Без верстата 18 мм не пропонується — правило висоти як було.
    plain = disc_suggest.suggest("моно A2", atts, str(root))
    assert all(p.height == 16 for p in [plain.best, *plain.alts] if p)
    # ПММА: правило «найменша висота» (12), але на верстаті стоїть 18 — його.
    pmma = disc_suggest.suggest("пмма A2", atts, str(root), active={"18-a2-x283": seen})
    assert pmma.best is not None and pmma.best.name == "pmma18_18-a2-x283"


def test_disc_on_machine_in_rule_beats_higher_one_on_machine(shop):
    """Два диски в верстатах: 16 мм (за правилом) і 18 мм (вищий, але
    свіжіший) — першим за правилом (14.11.26, прод: робота 9 мм → «18 мм»)."""
    root, atts = shop
    _disc(root, "zr", 18, "zr18_18-Monolith-A2-x53", [])
    disc_suggest._scan_cache.clear()
    active = {"16-monolith-a2-x37": datetime(2026, 11, 14, 8),
              "18-monolith-a2-x53": datetime(2026, 11, 14, 9)}
    hint = disc_suggest.suggest("моно A2", atts, str(root), active=active)
    assert hint.best is not None and hint.best.name == "zr16_16-Monolith-A2-x37"
    assert "zr18_18-Monolith-A2-x53" in {a.name for a in hint.alts}


def test_hint_row_shows_work_height(app_db, shop):  # noqa: F811
    from app.services.disc_suggest import Pick, Suggestion, Want
    from app.routers.deps import templates

    hint = Suggestion(want=Want(family="Цирконій", line="monolith", shades=frozenset({"a2"})),
                      thickness=9.06, heights=[14], why="…",
                      best=Pick(name="zr18_18-Monolith-A2-x53", height=18, free_pct=51,
                                started=True, paths=[], on_machine=True))
    html = templates.get_template("_mail_disc_hint.html").render(hint=hint)
    assert "робота ≈9.1 мм → 14 мм (диск вищий)" in html and "на верстаті" in html


def test_active_discs_name_the_machine(app_db):  # noqa: F811
    from app.models import Machine, MachineReading

    _app, factory = app_db
    with factory() as db:
        db.add(Machine(name="250i-Tolik", host="192.168.1.58", created_at=datetime(2026, 11, 1)))
        db.add(MachineReading(host="192.168.1.58", captured_at=datetime(2026, 11, 14, 8),
                              iso_name="1zr14_14-Monolith-A2-x47_2026-11-14_08-01-02.iso"))
        db.commit()
        machines: dict[str, str] = {}
        disc_suggest.active_discs(db, until=datetime(2026, 11, 14, 12), machines=machines)
    assert machines == {"14-monolith-a2-x47": "250i-Tolik"}


def test_hint_row_names_machine_and_warns_when_no_place_found():
    from app.routers.deps import templates
    from app.services.disc_suggest import Pick, Suggestion, Want

    pick = Pick(name="zr14_14-Monolith-A2-x47", height=14, free_pct=30, started=True,
                paths=[], on_machine=True, machine="250i-Tolik", fit=False)
    hint = Suggestion(want=Want(family="Цирконій", line="monolith", shades=frozenset({"a2"})),
                      thickness=9.0, heights=[14], why="…", best=pick)
    html = templates.get_template("_mail_disc_hint.html").render(hint=hint)
    assert "стоїть у 250i-Tolik" in html and "на верстаті" not in html
    assert "розстав у Sum3D" in html


def test_started_disc_without_place_is_offered_only_when_much_is_free(shop, monkeypatch):
    """Власник 20.11.26 (лист Павленка: картка «новий диск», а він сам розмістив
    7 зубів на x265): розстановка CRM — наближення, тож початий диск, де вона
    місця не знайшла, але вільно ≥ 30 %, показується з «розстав у Sum3D».
    Майже повний (18.11.26, x37 з 8 %: «тут же очевидно, що не влізе») і новий
    диск без місця — ні."""
    from app.routers.deps import templates

    root, atts = shop
    monkeypatch.setattr(disc_suggest.disc_fit, "fits_on", lambda *a, **k: False)
    hint = disc_suggest.suggest("моно A2", atts, str(root))
    assert hint.best is not None and hint.best.name == "zr16_16-Monolith-A2-x37"
    assert hint.best.fit is False and hint.best.started and hint.best.free_pct >= 30
    shown = {hint.best.name, *(a.name for a in hint.alts)}
    assert "zr16_16-Monolith-A2-x41" not in shown, "вирізано ~88 % — не показуємо"
    assert "zr16_16-Monolith-A2-x40" not in shown, "новий диск, де місця немає, — ні"
    html = templates.get_template("_mail_disc_hint.html").render(hint=hint)
    assert "розстав у Sum3D" in html and "влазить" not in html


def test_nothing_with_place_and_little_free_says_new_disc(shop, monkeypatch):
    from app.routers.deps import templates

    root, atts = shop
    monkeypatch.setattr(disc_suggest, "DOUBTFUL_SHOW_PCT", 101)
    monkeypatch.setattr(disc_suggest.disc_fit, "fits_on", lambda *a, **k: False)
    hint = disc_suggest.suggest("моно A2", atts, str(root))
    assert hint.best is None and hint.alts == [] and hint.rejected >= 1
    html = templates.get_template("_mail_disc_hint.html").render(hint=hint)
    assert "Новий диск" in html and "початого диска з місцем немає" in html
    assert "розстав у Sum3D" not in html


def test_trace_explains_every_disc_and_keeps_the_card(shop):
    """`kmill_disc_hint` (20.11.26, лист Павленка: картка «новий диск», Sum3D —
    x265): по кожному диску потрібного кольору — чому він у картці чи ні."""
    root, atts = shop
    plain = disc_suggest.suggest("моно A2", atts, str(root))
    disc_suggest._fit_cache.clear()
    trace: dict = {}
    traced = disc_suggest.suggest("моно A2", atts, str(root), trace=trace)
    assert traced.best.name == plain.best.name and [p.name for p in traced.alts] == [p.name for p in plain.alts]

    by = {d["диск"]: d for d in trace["диски"]}
    assert "zr16_16-Monolith-A3-x5" not in by, "лише потрібний колір"
    assert by["zr14_14-Monolith-A2-x30"]["рішення"] == "висота не за правилом"
    cut = by["zr16_16-Monolith-A2-x41"]  # вирізано ~88 %
    assert cut["рішення"] == "розстановка не знайшла місця — не показано"
    assert cut["розстановка"]["лягло"] < cut["розстановка"]["з"] and "не_лягла" in cut["розстановка"]
    best = by["zr16_16-Monolith-A2-x37"]
    assert best["рішення"] == "у картці" and best["початий"] is True
    assert best["розстановка"]["влазить"] is True and best["розстановка"]["лягло"] == 3
    letter = trace["лист"]
    assert letter["конструкцій_для_розстановки"] == 3 and len(letter["розміри_мм"]) == 3
    assert letter["висоти"] == [16]


def test_trace_names_the_piece_that_did_not_fit(tmp_path, shop):
    """Конструкція, ширша за диск (зайва далека вершина в STL), не лягає ніде —
    і слід каже, яка саме й якого розміру."""
    root, atts = shop
    huge = _stl(tmp_path / "mail" / "case_31-anatomy_cad.stl", 120, 10, 10.0)
    atts = [*atts, SimpleNamespace(filename=huge.name, saved_path=str(huge))]
    trace: dict = {}
    hint = disc_suggest.suggest("моно A2", atts, str(root), trace=trace)
    assert hint.best is None
    reports = [d["розстановка"] for d in trace["диски"] if "розстановка" in d]
    assert reports and all(r["влазить"] is False for r in reports)
    assert reports[0]["причина"] == "конструкція ширша за диск"
    assert reports[0]["не_лягла"].startswith("150.0×")  # 120 мм × усадка 1.25


def test_disc_untouched_for_half_a_year_is_not_offered(shop):
    """Власник 20.11.26: x136 Emotions A3 востаннє брали у 2024 — «99 % що його
    вже немає». Файл диска, який Sum3D не переписував понад пів року, — поза
    підказкою; той самий диск у верстаті — показується."""
    root, atts = shop
    old = root / "zr" / "16" / "zr16_16-Monolith-A2-x37.blk"
    stamp = time.time() - (disc_suggest.STALE_DAYS + 30) * 86400
    os.utime(old, (stamp, stamp))
    trace: dict = {}
    hint = disc_suggest.suggest("моно A2", atts, str(root), trace=trace)
    shown = {hint.best.name if hint.best else None, *(a.name for a in hint.alts)}
    assert "zr16_16-Monolith-A2-x37" not in shown
    assert hint.best is not None and hint.best.name == "zr16_16-Monolith-A2-x40", "свіжий диск лишився"
    why = {d["диск"]: d for d in trace["диски"]}["zr16_16-Monolith-A2-x37"]["рішення"]
    assert why.startswith(f"не брали понад {disc_suggest.STALE_DAYS} дн")

    # Стоїть у верстаті — отже фізично є, хай файл і старий.
    on_machine = disc_suggest.suggest("моно A2", atts, str(root),
                                      active={"16-monolith-a2-x37": datetime.now()})
    assert on_machine.best is not None and on_machine.best.name == "zr16_16-Monolith-A2-x37"

    # Трохи молодший за поріг — лишається.
    stamp = time.time() - (disc_suggest.STALE_DAYS - 5) * 86400
    os.utime(old, (stamp, stamp))
    assert disc_suggest.suggest("моно A2", atts, str(root)).best.name == "zr16_16-Monolith-A2-x37"


def test_stale_16_gives_way_to_existing_18_for_multilayer(tmp_path):
    """Власник 20.11.26 (лист Павленка, Emotions A3, ≈13,9 мм): x136 16 мм не
    брали з 2024 — «можна розглядати висоту 18». Мультиколор і так допускає
    крок вище; без давнього 16 першим стає наявний 18, а не «новий диск»."""
    root = tmp_path / "discs"
    disc_suggest._scan_cache.clear()
    used = [_square(x, 0, 6) for x in (-25, 0)]
    _disc(root, "zr", 16, "zr16_16-Emotions-A3-x136", used)
    _disc(root, "zr", 18, "zr18_18-Emotions-A3-x200", used)
    stamp = time.time() - 700 * 86400
    os.utime(root / "zr" / "16" / "zr16_16-Emotions-A3-x136.blk", (stamp, stamp))
    (tmp_path / "mail").mkdir()
    files = [_stl(tmp_path / "mail" / f"case_{t}-crown_cad.stl", 9, 10, 10.0) for t in (11, 12)]
    atts = [SimpleNamespace(filename=f.name, saved_path=str(f)) for f in files]
    hint = disc_suggest.suggest("emo a3", atts, str(root))
    assert hint.heights == [16, 18]
    assert hint.best is not None and hint.best.name == "zr18_18-Emotions-A3-x200"
    assert hint.alts == []


def _letter_of(tmp_path, height=10.0):
    stl = tmp_path / "mail"
    stl.mkdir(exist_ok=True)
    files = [_stl(stl / f"case_{t}-crown_cad.stl", 9, 10, height)
             for t in (11, 12)]
    return [SimpleNamespace(filename=f.name, saved_path=str(f)) for f in files]


def _names(hint):
    return [p.name for p in ([hint.best] if hint.best else []) + list(hint.alts)]


USED = [_square(x, 0, 6) for x in (-25, 0)]


def test_started_one_step_higher_goes_before_a_new_disc(tmp_path):
    """Власник 23.11.26: «якщо вище на один пункт, ніж ідеальний, то можна взяти»
    (D3: картка казала «новий 16», цех узяв початий 18-x06)."""
    from app.routers.deps import templates

    root = tmp_path / "discs"
    disc_suggest._scan_cache.clear()
    _disc(root, "zr", 16, "zr16_16-Monolith-D3-x01", USED)
    _disc(root, "zr", 18, "zr18_18-Monolith-D3-x07", [])  # новий потрібної висоти
    _disc(root, "zr", 20, "zr20_20-Monolith-D3-x06", USED)  # початий на крок вищий
    _disc(root, "zr", 20, "zr20_20-Monolith-D3-x08", [])  # новий вищий — ні
    _disc(root, "zr", 22, "zr22_22-Monolith-D3-x09", USED)  # на два кроки — ні
    # 11,6 × 1,25 = 14,5 мм: 16 впритул → Monolith лише 18.
    hint = disc_suggest.suggest("mono d3", _letter_of(tmp_path, 11.6), str(root))
    assert hint.heights == [18]
    assert _names(hint) == ["zr20_20-Monolith-D3-x06", "zr18_18-Monolith-D3-x07"]
    assert hint.best.started and hint.best.fit is True
    html = templates.get_template("_mail_disc_hint.html").render(hint=hint)
    assert "(диск вищий)" in html and "новий диск 18 мм Monolith D3" in html


def test_started_of_the_right_height_hides_the_higher_one(tmp_path):
    root = tmp_path / "discs"
    disc_suggest._scan_cache.clear()
    _disc(root, "zr", 16, "zr16_16-Monolith-D3-x03", USED)
    _disc(root, "zr", 18, "zr18_18-Monolith-D3-x01", USED)
    _disc(root, "zr", 20, "zr20_20-Monolith-D3-x06", USED)
    hint = disc_suggest.suggest("mono d3", _letter_of(tmp_path, 11.6), str(root))
    assert hint.heights == [18]
    assert _names(hint)[0] == "zr18_18-Monolith-D3-x01"


def test_higher_started_disc_must_be_fresh_and_have_room(tmp_path, monkeypatch):
    root = tmp_path / "discs"
    disc_suggest._scan_cache.clear()
    _disc(root, "zr", 16, "zr16_16-Monolith-D3-x01", USED)
    _disc(root, "zr", 18, "zr18_18-Monolith-D3-x07", [])
    _disc(root, "zr", 20, "zr20_20-Monolith-D3-x06", USED)
    atts = _letter_of(tmp_path, 11.6)
    path = root / "zr" / "20" / "zr20_20-Monolith-D3-x06.blk"
    old = time.time() - 300 * 86400
    os.utime(path, (old, old))
    assert _names(disc_suggest.suggest("mono d3", atts, str(root))) == ["zr18_18-Monolith-D3-x07"], "давно не чіпаний"
    os.utime(path, None)
    disc_suggest._fit_cache.clear()
    monkeypatch.setattr(disc_suggest.disc_fit, "fits_on", lambda *a, **k: False)
    assert "zr20_20-Monolith-D3-x06" not in _names(disc_suggest.suggest("mono d3", atts, str(root))), "місця немає"


def test_katana_series_are_known_by_their_disc_names(tmp_path):
    """Лист 3973: у теці немає слова katana — лише STML і UTML."""
    root = tmp_path / "discs"
    disc_suggest._scan_cache.clear()
    for name in ("STML-a2-x42", "UTML-a2-x05", "Monolith-A2-x49", "Emotions-a2-x54", "Vivid-stml-a2-x02",
                 "a2-x99", "A2-STML-x04"):
        _disc(root, "zr", 14, f"zr14_14-{name}", USED)
    atts = _letter_of(tmp_path)

    def offered(material):
        return {n.removeprefix("zr14_14-") for n in _names(disc_suggest.suggest(material, atts, str(root)))}

    assert offered("stml а2") == {"STML-a2-x42", "A2-STML-x04"}
    assert offered("katana a2") == {"STML-a2-x42", "A2-STML-x04"}
    assert offered("katana utml a2") == offered("утмл а2") == {"UTML-a2-x05"}
    assert offered("mono a2") == {"Monolith-A2-x49"}
    assert offered("vivid stml a2") == {"Vivid-stml-a2-x02"}
    assert disc_suggest.want_of("stml a2").label() == "Katana STML A2"


@pytest.mark.parametrize("wanted, got, conflict", [
    ("katana", "stml", False), ("katana", "utml", False), ("stml", "katana", False), ("stml", "stml", False),
    ("stml", "utml", True), ("stml", "monolith", True), ("katana", "monolith", True), ("monolith", None, False),
])
def test_guard_knows_katana_series(wanted, got, conflict):
    assert disc_suggest.lines_conflict(wanted, got) is conflict


def test_far_higher_disc_on_machine_goes_after_started_of_the_right_height(tmp_path):
    """Робота 9002 (8,2 мм, mono a2): першим стояв 25 мм із верстата, цех узяв 16."""
    root = tmp_path / "discs"
    disc_suggest._scan_cache.clear()
    _disc(root, "zr", 14, "zr14_14-Monolith-A2-x01", USED)
    _disc(root, "zr", 16, "zr16_16-Monolith-A2-x02", USED)
    _disc(root, "zr", 18, "zr18_18-Monolith-A2-x03", USED)
    _disc(root, "zr", 25, "zr25_25-Monolith-A2-x40", USED)
    seen = datetime(2026, 11, 23, 9)
    atts = _letter_of(tmp_path, 8.0)  # 10 мм → 14 (запас 4) → мультиколор 14/16
    hint = disc_suggest.suggest("mono a2", atts, str(root), active={"25-monolith-a2-x40": seen})
    assert hint.heights == [14, 16]
    assert _names(hint) == ["zr14_14-Monolith-A2-x01", "zr16_16-Monolith-A2-x02", "zr25_25-Monolith-A2-x40"]
    # На один крок вищий — перший, як і був (власник 14.11.26).
    near = disc_suggest.suggest("mono a2", atts, str(root), active={"18-monolith-a2-x03": seen})
    assert _names(near)[0] == "zr18_18-Monolith-A2-x03"


def test_far_higher_disc_on_machine_does_not_hide_the_one_step_higher(tmp_path):
    root = tmp_path / "discs"
    disc_suggest._scan_cache.clear()
    _disc(root, "zr", 16, "zr16_16-Monolith-A2-x09", [])
    _disc(root, "zr", 18, "zr18_18-Monolith-A2-x03", USED)
    _disc(root, "zr", 20, "zr20_20-Monolith-A2-x05", USED)
    _disc(root, "zr", 25, "zr25_25-Monolith-A2-x40", USED)
    (root / "zr" / "18" / "zr18_18-Monolith-A2-x03.blk").write_bytes(_blank([]))
    hint = disc_suggest.suggest("mono a2", _letter_of(tmp_path, 10.0), str(root),
                                active={"25-monolith-a2-x40": datetime(2026, 11, 23, 9)})
    assert hint.heights == [16, 18]
    assert _names(hint)[:2] == ["zr20_20-Monolith-A2-x05", "zr25_25-Monolith-A2-x40"]


def test_pmma_prefers_discs_of_recent_programs_over_old_leftovers(tmp_path):
    """Журнал 22–23.11.26: 0 з 3 — картка радила x188/x07/x33 з 19–28 % вільного."""
    root = tmp_path / "discs"
    disc_suggest._scan_cache.clear()
    ring = [_square(28 * math.cos(a), 28 * math.sin(a), 12) for a in (0, 1.05, 2.1, 3.15, 4.2, 5.25)]
    _disc(root, "pmma", 16, "pmma16_16-a2-x188", ring)  # найповніший, але давній
    _disc(root, "pmma", 14, "pmma14_14-a2-x133", USED)
    _disc(root, "pmma", 18, "pmma18_18-a2-x268", [_square(30, 0, 8)])
    atts = _letter_of(tmp_path, 8.0)
    assert _names(disc_suggest.suggest("pmma a2", atts, str(root)))[0] == "pmma16_16-a2-x188", "без історії — як було"
    hint = disc_suggest.suggest("pmma a2", atts, str(root), recent={"14-a2-x133", "18-a2-x268"})
    assert _names(hint) == ["pmma14_14-a2-x133", "pmma18_18-a2-x268", "pmma16_16-a2-x188"]
    assert not hint.best.on_machine, "знятий із верстата диск — не «на верстаті»"


def test_recent_discs_do_not_change_zirconia_order(shop):
    root, atts = shop
    _disc(root, "zr", 16, "zr16_16-Monolith-A2-x50", USED)
    disc_suggest._scan_cache.clear()
    plain = _names(disc_suggest.suggest("моно A2", atts, str(root)))
    assert _names(disc_suggest.suggest("моно A2", atts, str(root), recent={"16-monolith-a2-x50"})) == plain


def test_active_discs_also_collects_removed_ones(app_db):  # noqa: F811
    from app.models import MachineReading

    _app, factory = app_db
    with factory() as db:
        for day, disc in ((13, "PMMA14_14-A2-X133"), (14, "PMMA18_18-A2-X268"), (5, "PMMA20_20-A2-X07")):
            db.add(MachineReading(host="h1", captured_at=datetime(2026, 11, day, 8),
                                  iso_name=f"{disc}_2026-11-{day:02d}_08-01-02.iso"))
        db.commit()
        recent: set[str] = set()
        active = disc_suggest.active_discs(db, until=datetime(2026, 11, 14, 12), recent=recent)
    assert set(active) == {"18-a2-x268"} and recent == {"18-a2-x268", "14-a2-x133"}


@pytest.mark.parametrize("stem, line", [
    ("zr25_25-Znature-A3-x06a", "z-nature"), ("zr18_18-Nat-a2-x03", "z-nature"), ("zr14_14-Nature-A2-x01", "z-nature"),
    ("zr16_16-Z-Nature-A3-x03", "z-nature"), ("zr20_20-Z-Nature--a2-x12", "z-nature"), ("zr22_22-ZNat-A2-x1", "z-nature"),
    ("zr18_18-Znature-MS-A3-x35", None), ("zr14_14-Nat_MS-A3-x03", None), ("zr22_22-a2-Nature-MS-x01", None),
    ("zr18_18-Z-Nature-MS-A2-x12", "z-nature"),  # як і було до 23.11.26
    ("zr20_20-pearl natural a2-x01", None), ("zr14_14-a2-x99", None), ("zr22_22-Z-Sm-MS-A2-x11", None),
    ("zr14_14-Emotions-a2-x54", "emotions"), ("zr14_14-STML-a2-x42", "stml"),
])
def test_disc_line_by_file_name(stem, line):
    from app.services.cam_blanks import parse_blank_name

    assert disc_suggest._line_of_disc((parse_blank_name(stem).brand or "").lower(), stem.lower()) == line


def test_disc_hint_tool_needs_a_real_letter(db_session):
    from app.services.mcp_common import ToolError
    from app.services.mcp_tools_shop import tool_disc_hint

    with pytest.raises(ToolError, match="email_id"):
        tool_disc_hint(db_session, {})
    with pytest.raises(ToolError, match="листа 999 немає"):
        tool_disc_hint(db_session, {"email_id": 999})


def test_new_disc_beats_unchecked_started_one_when_limit_runs_out(shop, monkeypatch):
    """18.11.26, x06 після 0.21.151: перевірки скінчились на майже повних, і
    першим став неперевірений початий диск «місце не перевірено». Тепер, коли
    жоден перевірений не підійшов, НОВИЙ диск перевіряється поза межею й іде
    першим — на ньому місце є напевно."""
    root, atts = shop
    seen: list[int] = []

    def fits_on(radius, blocked, masks):
        seen.append(int(blocked.sum()))
        return len(seen) > 1  # перший (початий) — не влазить, далі влазить

    monkeypatch.setattr(disc_suggest.disc_fit, "fits_on", fits_on)
    monkeypatch.setattr(disc_suggest, "MAX_FIT_TRIES", 1)
    hint = disc_suggest.suggest("моно A2", atts, str(root))
    assert len(seen) == 2, "межа спроб + одна перевірка нового диска"
    assert hint.best.fit is True and not hint.best.started
    assert all(p.fit is True for p in [hint.best, *hint.alts]), "неперевірених не показуємо"


def test_fit_answer_is_remembered_for_the_same_disc_and_letter(shop, monkeypatch):
    root, atts = shop
    calls: list[int] = []

    def fits_on(radius, blocked, masks):
        calls.append(1)
        return True

    monkeypatch.setattr(disc_suggest.disc_fit, "fits_on", fits_on)
    disc_suggest.suggest("моно A2", atts, str(root))
    first = len(calls)
    disc_suggest.suggest("моно A2", atts, str(root))
    assert first >= 1 and len(calls) == first, "повторне відкриття листа не перераховує"


def test_checked_fit_wins_over_unchecked(shop, monkeypatch):
    root, atts = shop
    monkeypatch.setattr(disc_suggest.disc_fit, "fits_on", lambda *a, **k: True)
    monkeypatch.setattr(disc_suggest, "MAX_FIT_TRIES", 1)
    hint = disc_suggest.suggest("моно A2", atts, str(root))
    assert hint.best.fit is True
    assert hint.alts == [], "за межею спроб — не показуємо зовсім (власник 18.11.26)"


def test_unchecked_started_disc_is_not_labelled_fitting():
    from app.routers.deps import templates
    from app.services.disc_suggest import Pick, Suggestion, Want

    def render(fit):
        pick = Pick(name="zr18_18-Monolith-A1-x06", height=18, free_pct=22, started=True,
                    paths=[], fit=fit)
        hint = Suggestion(want=Want(family="Цирконій", line="monolith", shades=frozenset({"a1"})),
                          thickness=15.1, heights=[18], why="…", best=pick)
        return templates.get_template("_mail_disc_hint.html").render(hint=hint)

    assert "влазить" in render(True)
    # Без перевірки (немає STL exocad) — про місце ні слова (власник 18.11.26).
    assert "влазить" not in render(None) and "не перевірено" not in render(None)
    assert "розстав у Sum3D" in render(False)


def test_hint_deck_shows_up_to_three_candidates():
    """Власник 14.11.26: кандидати гортаються стрілкою, до 3; видно першого."""
    from app.routers.deps import templates
    from app.services.disc_suggest import Pick, Suggestion, Want

    def pick(n):
        return Pick(name=f"zr14_14-Monolith-A2-x{n}", height=14, free_pct=30, started=True, paths=[])

    hint = Suggestion(want=Want(family="Цирконій", line="monolith", shades=frozenset({"a2"})),
                      thickness=11.0, heights=[14], why="…", best=pick(1), alts=[pick(2), pick(3)])
    html = templates.get_template("_mail_disc_hint.html").render(hint=hint)
    assert html.count('class="mdh" data-i=') == 3
    assert html.count(" hidden title=") == 2, "видно лише першого"
    assert "mdh-next" in html and "/3" in html
    single = Suggestion(want=hint.want, thickness=11.0, heights=[14], why="…", best=pick(1))
    assert "mdh-next" not in templates.get_template("_mail_disc_hint.html").render(hint=single)


def test_pick_label_splits_line_shade_and_code():
    """Варіант A (власник 16.11.26): висота окремо, лінія й колір звичайним
    текстом, код партії окремо — назва більше не обрізається «…»."""
    from app.services.disc_suggest import Pick

    def label(name):
        return Pick(name=name, height=14, free_pct=30, started=True, paths=[]).label

    assert label("zr16_16-Monolith-A1-X52") == ("Monolith A1", "X52")
    assert label("zr14_14-Monolith-A3-5-x51") == ("Monolith A3.5", "x51")
    assert label("pmma18_18-a2-x283") == ("PMMA A2", "x283")
    assert label("zr14_14-1000-x37") == ("1000 опак", "x37")
    assert label("дивна назва") == ("дивна назва", "")


def test_hint_card_keeps_full_name_and_marks_candidates():
    from app.routers.deps import templates
    from app.services.disc_suggest import Pick, Suggestion, Want

    def pick(n, **kw):
        return Pick(name=f"zr14_14-Monolith-A2-x{n}", height=14, free_pct=30, started=True, paths=[], **kw)

    hint = Suggestion(want=Want(family="Цирконій", line="monolith", shades=frozenset({"a2"})),
                      thickness=11.0, heights=[14], why="…",
                      best=pick(1), alts=[pick(2, fit=False), Pick(name="zr14_14-Monolith-A2-x3", height=14,
                                                                    free_pct=100, started=False, paths=[])])
    html = templates.get_template("_mail_disc_hint.html").render(hint=hint)
    assert 'title="zr14_14-Monolith-A2-x1 ·' in html, "повна назва — у підказці при наведенні"
    assert '<span class="mdh-name">Monolith A2</span>' in html and ">x1</span>" in html
    assert html.count('<i class="on"></i>') == 3 and html.count("<i></i>") == 6, "у кожній картці свої три крапки"
    assert 'data-state="bad"' in html and 'data-state="fresh"' in html
    new = Suggestion(want=hint.want, thickness=11.0, heights=[14], why="…", new_disc="новий диск 14 мм Monolith A2")
    assert "Новий диск Monolith A2" in templates.get_template("_mail_disc_hint.html").render(hint=new)


def test_hint_steps_are_timed_for_the_slow_request_log(shop):
    """Прод 16.11.26: підказка 1–4.6 с, а в «Slow request» видно лише render.
    Кроки мусять іти окремими фазами, щоб було видно, що гальмує."""
    from app import perf

    root, atts = shop
    disc_suggest._scan_cache.clear()
    recorder = perf.start("t")
    try:
        disc_suggest.suggest("моно A2", atts, str(root))
    finally:
        perf.finish()
    assert {"disc:scan", "disc:stl", "disc:shapes"} <= set(recorder.phases)


def test_roomy_started_disc_is_reached_behind_many_nearly_full(tmp_path, monkeypatch):
    """22.11.26, лист 4001: початі диски перевіряються від найменш вільного
    («добити диск»), і 47 майже повних з'їли весь бюджет — до x101 з 34 %
    вільного черга не дійшла, хоча оператор поставив коронку саме туди. Тепер
    безнадійні відкидає швидка перевірка, повна розстановка на них не йде, і
    диск із місцем потрапляє в картку."""
    root = tmp_path / "discs"
    disc_suggest._scan_cache.clear()
    def box(cx, cy, w, h):
        return [(cx - w / 2, cy - h / 2), (cx + w / 2, cy - h / 2), (cx + w / 2, cy + h / 2), (cx - w / 2, cy + h / 2)]

    grid = [box(x, y, 12, 12) for x in range(-42, 43, 14) for y in range(-42, 43, 14) if math.hypot(x, y) < 50]
    for n in range(1, 41):  # вирізано решіткою: вільні лише вузькі коридори
        _disc(root, "zr", 16, f"zr16_16-Monolith-A2-x{n:02d}", grid)
    _disc(root, "zr", 16, "zr16_16-Monolith-A2-x99", [box(-24, 0, 48, 100)])  # ліва половина
    stl = tmp_path / "mail"
    stl.mkdir()
    atts = [SimpleNamespace(filename="case_11-anatomy_cad.stl",
                            saved_path=str(_stl(stl / "case_11-anatomy_cad.stl", 9, 10, 10.0)))]
    full: list[int] = []
    real = disc_suggest.disc_fit.place_all

    def counted(blocked, masks, scale=1.0):
        full.append(1)
        return real(blocked, masks, scale)

    monkeypatch.setattr(disc_suggest.disc_fit, "place_all", counted)
    hint = disc_suggest.suggest("моно A2", atts, str(root))
    assert hint.best is not None and hint.best.name == "zr16_16-Monolith-A2-x99"
    assert hint.best.fit is True and hint.best.started
    assert len(full) <= 5, f"повна розстановка на майже повних дисках: {len(full)} разів"


def test_read_limit_drops_the_oldest_discs_not_the_last_by_name(tmp_path):
    """22.11.26, ПММА A3: для ПММА годиться будь-яка вища висота, тож усі висоти
    рівні, і диски читались за назвою — pmma14, pmma16, … до межі 60. Диски
    25 мм (учорашні x266/x267 з купою місця) лишались «не прочитано», а картка
    казала «новий диск, початого з місцем немає». Тепер межа відсікає ті, що
    найдавніше чіпані."""
    root = tmp_path / "discs"
    disc_suggest._scan_cache.clear()

    def box(cx, cy, w, h):
        return [(cx - w / 2, cy - h / 2), (cx + w / 2, cy - h / 2), (cx + w / 2, cy + h / 2), (cx - w / 2, cy + h / 2)]

    grid = [box(x, y, 12, 12) for x in range(-42, 43, 14) for y in range(-42, 43, 14) if math.hypot(x, y) < 50]
    month_ago = time.time() - 30 * 86400
    for n in range(1, 71):  # 70 майже вирізаних, давно не чіпаних
        _disc(root, "pmma", 14, f"pmma14_14-a2-x{n:02d}", grid)
        path = root / "pmma" / "14" / f"pmma14_14-a2-x{n:02d}.blk"
        os.utime(path, (month_ago, month_ago))
    _disc(root, "pmma", 25, "pmma25_25-a2-x266", [box(-24, 0, 48, 100)])  # учорашній, половина вільна
    stl = tmp_path / "mail"
    stl.mkdir()
    atts = [SimpleNamespace(filename="case_45-46-47-bridge_cad.stl",
                            saved_path=str(_stl(stl / "case_45-46-47-bridge_cad.stl", 28, 10, 8.1)))]
    trace: dict = {}
    hint = disc_suggest.suggest("пмма A2", atts, str(root), trace=trace)
    by = {d["диск"]: d for d in trace["диски"]}
    assert "не прочитано" not in by["pmma25_25-a2-x266"]["рішення"], by["pmma25_25-a2-x266"]
    assert hint.best is not None and hint.best.name == "pmma25_25-a2-x266"
    unread = [d for d in by.values() if "не прочитано" in d["рішення"]]
    assert len(unread) == 11 and all(d["висота"] == 14 for d in unread), "за межею — лише давні"


def test_hipc_and_pmma_discs_never_stand_in_for_each_other(shop):
    """Власник 22.11.26: HIPC замість ПММА пропонувати не можна. Диск HIPC лежить
    у теці ПММА й зветься `pmma20_20-hipc-a3-x54`, тож картка «pmma a3» давала
    його запасним (листи 3998 і 4000). Навпаки теж: замовлено HIPC — лише HIPC."""
    root, atts = shop
    _disc(root, "pmma", 16, "pmma16_16-hipc-a2-x54", [])
    disc_suggest._scan_cache.clear()

    def offered(material):
        hint = disc_suggest.suggest(material, atts, str(root))
        return hint, [p.name for p in ([hint.best] if hint.best else []) + list(hint.alts)]

    plain, names = offered("пмма A2")
    assert names == ["pmma16_16-a2-x200"], "диск HIPC у підказці ПММА"
    assert plain.new_disc == "новий диск 16 мм ПММА A2"
    for text in ("hipc a2", "Hips A2", "хіпс а2"):
        hipc, names = offered(text)
        assert names == ["pmma16_16-hipc-a2-x54"], text
        assert hipc.new_disc == "новий диск 16 мм HIPC A2"
    assert disc_suggest.want_of("pmma a2").hipc is False
