"""Підказка диска при прийнятті листа (власник 12.11.26).

Правила власника: запас мінімум 1 мм; найменша висота, що фізично є; ПММА —
саме вона; мультиколор — вона або ще одна вище; Monolith — завжди на крок
вище; спершу початий диск, у який робота влізе; доступ — поіменно від адміна.
"""

from __future__ import annotations

import json
import struct
from datetime import datetime
from types import SimpleNamespace

import pytest

from app.services import disc_suggest
from tests.asgi_client import MiniClient
from tests.test_settings_slabs_render import ADMIN, OPERATOR, app_db  # noqa: F401 — фікстура
from tests.test_sum3d_disc import _blank, _square

HEIGHTS = [10, 12, 14, 16, 18, 20, 22, 25]


@pytest.mark.parametrize("material, thickness, expected", [
    ("пмма A2", 14.7, [16]),  # 14,7 + 0,5 = 15,2 → 16, бо 15 немає
    ("пмма A2", 11.0, [12]),
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
    assert set(active) == {"14-monolith-a3-5-x51", "14-monolith-a2-x47"}


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
    assert "перевір у Sum3D" in html


def test_disc_without_place_is_kept_as_doubtful(shop, monkeypatch):
    """Розстановка наближена — диск, де вона не знайшла місця, не ховається,
    а йде після тих, де знайшла (власник 14.11.26)."""
    root, atts = shop
    monkeypatch.setattr(disc_suggest.disc_fit, "fits", lambda *a, **k: False)
    hint = disc_suggest.suggest("моно A2", atts, str(root))
    assert hint.best is not None and hint.best.fit is False and hint.rejected >= 1


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
