"""Скріншоти записок передачі зміни: запис на диск і межа безпеки.

Перевіряється не «чи зберігся файл», а рівно ті місця, де довіра до клієнта
коштувала б дорого: ім'я файлу з клієнта, розмір, підроблений формат, шлях у
колонці БД після відновлення з бекапу.
"""

import io
from datetime import datetime, timedelta

import pytest
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app import shift_images
from app.db import Base
from app.models import ShiftNoteImage, User
from app.services.shift import create_note
from app.shift_images import (
    MAX_IMAGES_PER_NOTE,
    ShiftImageError,
    delete_image,
    media_type_for,
    resolve_image_file,
    save_image,
)


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(shift_images, "SHIFT_IMAGES_PATH", str(tmp_path / "shift_images"))
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


@pytest.fixture()
def note(db):
    user = User(username="op", password_hash="x", full_name="Оп", role="оператор")
    db.add(user)
    db.commit()
    created = create_note(
        db, kind="info", text="піч 2 о 9:00", author=user, now=datetime(2026, 8, 28, 2, 0)
    )
    db.commit()
    return created


def _png_bytes(width=40, height=30, fmt="PNG") -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (18, 24, 33)).save(buffer, format=fmt)
    return buffer.getvalue()


def _add(db, note, data: bytes, filename="image.png"):
    return save_image(db, note, stream=io.BytesIO(data), filename=filename)


# 10 — ім'я на диску наше, не клієнтське


def test_file_lands_under_month_and_note_and_ignores_the_client_name(db, note):
    row = _add(db, note, _png_bytes(), filename="../../evil.png")
    db.commit()

    path = shift_images.resolve_image_file(row)
    assert path is not None
    assert path.parent.name == str(note.id)
    assert path.parent.parent.name == "2026-08"
    assert path.name == "01.png"
    # Жоден фрагмент клієнтського імені не потрапив у шлях…
    assert "evil" not in str(path)
    # …але санітизований оригінал лишився для показу.
    assert row.filename == "evil.png"


def test_second_image_gets_the_next_number_and_a_freed_number_is_not_reused(db, note):
    first = _add(db, note, _png_bytes())
    second = _add(db, note, _png_bytes())
    db.commit()
    assert shift_images.resolve_image_file(first).name == "01.png"
    assert shift_images.resolve_image_file(second).name == "02.png"

    delete_image(db, first)
    db.commit()
    third = _add(db, note, _png_bytes())
    db.commit()
    # len(images)+1 дав би «02» — і тихо затер би чужий файл.
    assert shift_images.resolve_image_file(third).name == "03.png"


# 11 — завеликий файл не лишає слідів


def test_oversized_upload_is_refused_and_leaves_nothing_on_disk(db, note, monkeypatch):
    monkeypatch.setattr(shift_images, "MAX_IMAGE_BYTES", 1024)

    with pytest.raises(ShiftImageError):
        _add(db, note, b"\x00" * 4096)

    folder = shift_images.images_root() / "2026-08" / str(note.id)
    assert list(folder.glob("*")) == []
    assert db.query(ShiftNoteImage).count() == 0


# 12 — формат визначає Pillow, а не клієнт


def test_svg_disguised_as_png_is_refused(db, note):
    payload = b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"></svg>'

    with pytest.raises(ShiftImageError):
        _add(db, note, payload, filename="x.png")

    folder = shift_images.images_root() / "2026-08" / str(note.id)
    assert list(folder.glob("*")) == []
    assert db.query(ShiftNoteImage).count() == 0


def test_empty_upload_is_refused(db, note):
    with pytest.raises(ShiftImageError):
        _add(db, note, b"")
    assert db.query(ShiftNoteImage).count() == 0


# 13 — ліміт кількості спрацьовує ДО запису на диск


def test_limit_per_note_is_enforced_before_touching_the_disk(db, note):
    for _ in range(MAX_IMAGES_PER_NOTE):
        _add(db, note, _png_bytes())
    db.commit()

    with pytest.raises(ShiftImageError):
        _add(db, note, _png_bytes())

    folder = shift_images.images_root() / "2026-08" / str(note.id)
    assert len(list(folder.glob("*"))) == MAX_IMAGES_PER_NOTE
    assert db.query(ShiftNoteImage).count() == MAX_IMAGES_PER_NOTE


# 14 — велике зображення зменшується, розміри чесні


def test_large_image_is_scaled_down_and_dimensions_match_the_file(db, note):
    row = _add(db, note, _png_bytes(4000, 3000))
    db.commit()

    assert max(row.width, row.height) == shift_images.MAX_LONG_SIDE
    with Image.open(shift_images.resolve_image_file(row)) as saved:
        assert saved.size == (row.width, row.height)
    assert row.size_bytes == shift_images.resolve_image_file(row).stat().st_size


def test_jpeg_keeps_its_own_extension_and_media_type(db, note):
    row = _add(db, note, _png_bytes(fmt="JPEG"), filename="скріншот.jpeg")
    db.commit()

    path = shift_images.resolve_image_file(row)
    assert path.name == "01.jpg"
    assert media_type_for(path) == "image/jpeg"


# 15 — «ніколи не вір шляху»


def test_resolver_rejects_a_path_outside_the_root(db, note, tmp_path):
    row = _add(db, note, _png_bytes())
    db.commit()

    outside = tmp_path / "стороннє.png"
    outside.write_bytes(_png_bytes())
    row.saved_path = str(outside)
    assert resolve_image_file(row) is None


def test_resolver_rejects_a_missing_file_and_a_pruned_row(db, note):
    row = _add(db, note, _png_bytes())
    db.commit()
    path = shift_images.resolve_image_file(row)

    row.pruned_at = datetime(2027, 3, 1)
    assert resolve_image_file(row) is None, "прибране зображення не віддається"

    row.pruned_at = None
    path.unlink()
    assert resolve_image_file(row) is None, "рядок є, файлу немає — 404, не 500"


def test_resolver_rejects_a_symlink_hop_out_of_the_root(db, note, tmp_path):
    """Симлінк усередині кореня не має ставати виходом назовні. Там, де
    створити його не дають права (звичайний користувач Windows), перевірка
    пропускається — вона про поведінку резолвера, а не про ОС."""
    row = _add(db, note, _png_bytes())
    db.commit()

    secret = tmp_path / "secret.png"
    secret.write_bytes(_png_bytes())
    link = shift_images.images_root() / "2026-08" / str(note.id) / "link.png"
    try:
        link.symlink_to(secret)
    except (OSError, NotImplementedError):
        pytest.skip("створення симлінків недоступне в цьому середовищі")

    row.saved_path = str(link)
    assert resolve_image_file(row) is None


def test_media_type_comes_from_a_whitelist(db, note):
    row = _add(db, note, _png_bytes())
    db.commit()

    assert media_type_for(shift_images.resolve_image_file(row)) == "image/png"
    assert media_type_for(shift_images.images_root() / "x.svg") is None
    assert media_type_for(shift_images.images_root() / "x.exe") is None


# 16 — віддача байтів через роут


def _request(user_id=None):
    from types import SimpleNamespace

    return SimpleNamespace(
        session={} if user_id is None else {"user_id": user_id}, query_params={}, headers={}
    )


def test_image_route_serves_bytes_only_with_a_session(db, note):
    from app.routers import shift as shift_router

    row = _add(db, note, _png_bytes())
    db.commit()
    user = db.query(User).first()

    response = shift_router.get_shift_image(request=_request(user.id), image_id=row.id, db=db)
    assert response.media_type == "image/png"
    assert response.headers["x-content-type-options"] == "nosniff"

    with pytest.raises(Exception) as exc:
        shift_router.get_shift_image(request=_request(None), image_id=row.id, db=db)
    assert exc.value.status_code == 401


def test_image_route_404s_on_unknown_id_and_on_a_pruned_row(db, note):
    from app.routers import shift as shift_router

    row = _add(db, note, _png_bytes())
    db.commit()
    user = db.query(User).first()

    with pytest.raises(Exception) as unknown:
        shift_router.get_shift_image(request=_request(user.id), image_id=9999, db=db)
    assert unknown.value.status_code == 404

    row.pruned_at = datetime(2027, 3, 1)
    db.commit()
    with pytest.raises(Exception) as pruned:
        shift_router.get_shift_image(request=_request(user.id), image_id=row.id, db=db)
    assert pruned.value.status_code == 404, "прибране й зникле дають однаковий 404"


def test_failed_screenshot_never_takes_the_note_text_with_it(db, note, monkeypatch):
    """Головне правило конвеєра: завеликий скріншот не сміє забрати з собою
    речення «піч №2 відкрити о 9:00»."""
    from app.routers import shift as shift_router

    monkeypatch.setattr(shift_images, "MAX_IMAGE_BYTES", 512)
    upload = type("U", (), {"filename": "big.png", "file": io.BytesIO(b"\x00" * 4096)})()

    problems = shift_router._attach_images(db, note, [upload])

    assert problems and "big.png" in problems[0]
    assert db.query(ShiftNoteImage).count() == 0
    db.refresh(note)
    assert note.text == "піч 2 о 9:00", "текст записки лишився на місці"


def test_delete_removes_both_the_row_and_the_file(db, note):
    row = _add(db, note, _png_bytes())
    db.commit()
    path = shift_images.resolve_image_file(row)

    delete_image(db, row)
    db.commit()

    assert not path.exists()
    assert db.query(ShiftNoteImage).count() == 0


# 17 — прибирання за 6 місяців


def _age(db, row, days):
    """Зістарити зображення й записку на N днів (мітки ставить сервіс, тому
    в тесті їх правимо прямо)."""
    old = datetime.now() - timedelta(days=days)
    row.created_at = old
    row.note.created_at = old
    db.commit()


def test_prune_removes_bytes_after_180_days_but_keeps_the_note_text(db, note):
    from app.shift_images import analyze_shift_images, prune_shift_images

    row = _add(db, note, _png_bytes())
    db.commit()
    path = shift_images.resolve_image_file(row)
    _age(db, row, 200)

    report = analyze_shift_images(db)
    assert report.prunable_rows == 1

    removed, freed = prune_shift_images(db)
    db.commit()

    assert removed == 1 and freed > 0
    assert not path.exists()
    assert row.pruned_at is not None, "лишається видимий слід, а не тихе зникнення"
    assert db.query(ShiftNoteImage).count() == 1, "рядок лишається"
    db.refresh(note)
    assert note.text == "піч 2 о 9:00"


def test_prune_leaves_a_179_day_old_image_alone(db, note):
    from app.shift_images import prune_shift_images

    row = _add(db, note, _png_bytes())
    db.commit()
    _age(db, row, 179)

    removed, _ = prune_shift_images(db)
    db.commit()

    assert removed == 0
    assert row.pruned_at is None
    assert shift_images.resolve_image_file(row) is not None


def test_prune_removes_orphan_files_and_empty_folders(db, note):
    """Покриває відновлення з бекапу (він не несе байтів файлів) і недописані
    .part після аварійного завершення."""
    from app.shift_images import prune_shift_images

    folder = shift_images.images_root() / "2025-01" / "999"
    folder.mkdir(parents=True)
    orphan = folder / "01.png"
    orphan.write_bytes(_png_bytes())
    (folder / "02.part").write_bytes(b"\x00" * 10)

    removed, _ = prune_shift_images(db)
    db.commit()

    assert removed == 2
    assert not orphan.exists()
    assert not folder.exists(), "порожня тека теж прибирається"


def test_prune_is_idempotent(db, note):
    from app.shift_images import prune_shift_images

    row = _add(db, note, _png_bytes())
    db.commit()
    _age(db, row, 200)

    first, _ = prune_shift_images(db)
    db.commit()
    second, freed = prune_shift_images(db)
    db.commit()

    assert first == 1
    assert (second, freed) == (0, 0), "повторний запуск нічого не робить"


def test_prune_route_is_admin_only(db, note):
    from fastapi import HTTPException

    from app.routers import shift as shift_router

    operator = db.query(User).first()
    with pytest.raises(HTTPException) as exc:
        shift_router.prune_shift_images_now(request=_request(operator.id), db=db)
    assert exc.value.status_code == 403

    operator.role = "адмін"
    db.commit()
    response = shift_router.prune_shift_images_now(request=_request(operator.id), db=db)
    assert response.status_code == 204
