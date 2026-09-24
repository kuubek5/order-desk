"""Діагностика зіставлення видачі (kmill_handout_match / diagnose_handout_client).

Показує ЧОМУ робота на видачі має чи не має STL-теки. Тут — що для роботи, чия
тека фізично є (клієнт/Новая папка/матеріал/.stl), діагностика каже «зіставлено»,
а для невідомого клієнта — «не знайдено».
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.business_day import business_today
from app.db import Base
from app.models import Order
from app.services import handout_diag


def _db():
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return Session(engine, expire_on_commit=False)


def _client_order(db, **kw):
    d = dict(source="sheet_client", status="прораховано")
    d.update(kw)
    o = Order(**d)
    db.add(o)
    db.commit()
    return o


def test_diag_reports_match_when_folder_and_material_line_up(tmp_path, monkeypatch):
    # export/<клієнт>/Новая папка (1)/emo a3.5/x.stl
    client_dir = tmp_path / "Маріанна Голій"
    mat = client_dir / "Новая папка (1)" / "emo a3.5"
    mat.mkdir(parents=True)
    (mat / "2026_00002-003-waxup_slm_cad.stl").write_bytes(b"solid\n")

    monkeypatch.setattr(handout_diag, "get_export_folder_path", lambda db: str(tmp_path))

    with _db() as db:
        # робота клієнта з тим самим матеріалом, за сьогодні (щоб covered збігся)
        day = business_today().strftime("%d.%m.%y")
        _client_order(db, client_name="Голій", material_color="emo a3.5", sheet_tab=day)

        r = handout_diag.diagnose_handout_client(db, "Голій")

        assert r["клієнт"] == "Голій"
        assert r["зіставлення_імені"]["тека"] == "Маріанна Голій"
        assert r["зіставлення_імені"]["впевненість"] == 100.0
        assert r["партій_знайдено"] == 1
        assert r["партії"][0]["матеріал_тека"] == "emo a3.5"
        assert r["партії"][0]["stl"] == 1  # один .stl у теці
        assert r["роботи"][0]["результат"].startswith("зіставлено з текою")


def test_diag_reports_no_folder_when_name_does_not_match(tmp_path, monkeypatch):
    (tmp_path / "Хтось Інший" / "Новая папка (1)" / "mono a2").mkdir(parents=True)
    monkeypatch.setattr(handout_diag, "get_export_folder_path", lambda db: str(tmp_path))

    with _db() as db:
        day = business_today().strftime("%d.%m.%y")
        _client_order(db, client_name="Незіставний Клієнт", material_color="mono a2",
                      sheet_tab=day)

        r = handout_diag.diagnose_handout_client(db, "Незіставний")
        assert r["зіставлення_імені"]["тека"] is None
        assert "НЕ зіставлена" in r["роботи"][0]["результат"]


def test_diag_unknown_client_lists_candidates():
    with _db() as db:
        day = business_today().strftime("%d.%m.%y")
        _client_order(db, client_name="Голій", material_color="emo a3.5", sheet_tab=day)

        r = handout_diag.diagnose_handout_client(db, "НемаєТакого")
        assert r["знайдено"] is False
        assert "Голій" in r["клієнти_на_видачі"]
