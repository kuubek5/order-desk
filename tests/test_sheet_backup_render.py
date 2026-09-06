import json
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.routers.settings as S
from app.db import Base
from app.models import User
from app import sheet_backup as sb


def test_iso_to_tab_name_maps_and_survives_garbage():
    # Регресія: _iso_to_tab_name використовує date.fromisoformat — колись
    # `date` не був імпортований у settings.py і скачування дня падало 500.
    assert S._iso_to_tab_name("2026-09-03.csv") == "03.09.26.csv"
    assert S._iso_to_tab_name("not-a-date.csv") == "not-a-date.csv"


def test_admin_settings_renders_sheet_backup_section(tmp_path, monkeypatch):
    dbfile = tmp_path / "kuubmill.db"
    folder = sb.sheets_backup_dir(dbfile)
    folder.mkdir(parents=True)
    (folder / "2026-07-22.csv").write_bytes(b"a,b\n1,2\n")
    (folder / "manifest.json").write_text(
        json.dumps({"2026-07-22": {"tab": "22.07.26", "rows": 1,
                                    "taken_at": "2026-07-22T10:00:00",
                                    "disappeared_at": "2026-08-01T09:00:00"}}),
        encoding="utf-8",
    )
    # DB_PATH читає саме overview (get_settings), тому підміна цілить у нього:
    # на пакеті вона була б тихим no-op.
    monkeypatch.setattr(S.overview, "DB_PATH", str(dbfile))

    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        admin = User(username="admin", password_hash="unused", full_name="Адмін", role="адмін")
        db.add(admin)
        db.commit()
        resp = S.get_settings(request=SimpleNamespace(session={"user_id": admin.id}), db=db)
    body = resp.body.decode("utf-8")
    assert resp.status_code == 200
    assert "Копії Google-таблиці" in body
    assert "22.07.2026" in body
    assert "зникла з Google" in body
    assert "Файл → Імпорт" in body
