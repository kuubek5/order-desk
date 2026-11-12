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
    ("пмма A2", 14.7, [16]),  # 14,7 + 1 = 15,7 → 16, бо 15 немає
    ("пмма A2", 11.0, [12]),
    ("емо A2", 12.5, [14, 16]),  # мультиколор: ця або ще одна вище
    ("моно A2", 12.5, [16]),  # влазить у 14 → Monolith на крок вище
    ("моно A2", 23.5, [25]),  # вищого за 25 немає
    ("пмма A2", 24.5, []),  # 25,5 — такого диска немає
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
