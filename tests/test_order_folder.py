from pathlib import Path

from app.order_folder import (
    attach_email_folder_availability,
    folder_to_file_uri,
    pick_existing_attachment_folder,
    resolve_email_attachment_folder,
    resolve_job_code_folder,
)


class _FakeAttachment:
    """Stand-in for app.models.Attachment — only saved_path is read."""

    def __init__(self, saved_path: str):
        self.saved_path = saved_path


def test_picks_parent_of_first_existing_attachment(tmp_path):
    folder = tmp_path / "Клієнт" / "нова папка" / "моно а3"
    folder.mkdir(parents=True)
    existing_file = folder / "file.stl"
    existing_file.write_bytes(b"data")

    attachments = [_FakeAttachment(str(existing_file))]

    result = pick_existing_attachment_folder(attachments)

    assert result == existing_file.parent


def test_skips_missing_files_and_picks_next_existing(tmp_path):
    folder = tmp_path / "Клієнт" / "нова папка" / "пмма"
    folder.mkdir(parents=True)
    existing_file = folder / "second.stl"
    existing_file.write_bytes(b"data")

    missing_path = str(tmp_path / "gone" / "first.stl")
    attachments = [_FakeAttachment(missing_path), _FakeAttachment(str(existing_file))]

    result = pick_existing_attachment_folder(attachments)

    assert result == existing_file.parent


def test_resolves_when_stored_path_form_differs_from_trusted_root(tmp_path):
    """Mapped-network-drive case: saved_path and the trusted root point at the
    same directory via different forms (UNC vs drive letter in production;
    simulated here with a directory symlink). Lexical relative_to fails, so the
    resolved-form fallback must still find and return the folder."""
    import pytest

    real = tmp_path / "real_spool"
    (real / "14").mkdir(parents=True)
    (real / "14" / "crown.stl").write_bytes(b"x")
    link = tmp_path / "link_spool"
    try:
        link.symlink_to(real, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted on this machine")

    # stored via the symlinked form, trusted root is the real directory
    attachments = [_FakeAttachment(str(link / "14" / "crown.stl"))]
    folder = resolve_email_attachment_folder(attachments, [real])

    assert folder is not None
    assert (folder / "crown.stl").is_file()


def test_returns_none_when_no_attachments_exist_on_disk(tmp_path):
    missing_path = str(tmp_path / "gone" / "file.stl")
    attachments = [_FakeAttachment(missing_path)]

    assert pick_existing_attachment_folder(attachments) is None


def test_returns_none_for_empty_attachment_list():
    assert pick_existing_attachment_folder([]) is None


def test_folder_to_file_uri_none_folder_returns_none():
    assert folder_to_file_uri(None) is None


def test_folder_to_file_uri_produces_valid_file_scheme(tmp_path):
    folder = tmp_path / "Клієнт" / "нова папка" / "моно а3.5"
    folder.mkdir(parents=True)

    uri = folder_to_file_uri(folder)

    assert uri is not None
    assert uri.startswith("file:")


def test_folder_to_file_uri_matches_path_as_uri(tmp_path):
    folder = tmp_path / "space folder" / "кирилиця"
    folder.mkdir(parents=True)

    assert folder_to_file_uri(folder) == folder.as_uri()


def test_folder_to_file_uri_resolves_relative_path(tmp_path, monkeypatch):
    folder = tmp_path / "relative folder"
    folder.mkdir()
    monkeypatch.chdir(tmp_path)

    assert folder_to_file_uri(Path("relative folder")) == folder.as_uri()


def test_resolve_job_code_folder_none_when_technician_files_path_missing(tmp_path):
    job_code = "2026-07-21_00016-007"
    (tmp_path / job_code).mkdir(parents=True)

    assert resolve_job_code_folder(None, job_code) is None
    assert resolve_job_code_folder("", job_code) is None


def test_resolve_job_code_folder_none_when_job_code_missing(tmp_path):
    assert resolve_job_code_folder(str(tmp_path), None) is None
    assert resolve_job_code_folder(str(tmp_path), "") is None


def test_resolve_job_code_folder_none_when_directory_does_not_exist(tmp_path):
    result = resolve_job_code_folder(str(tmp_path), "2026-07-21_00016-007")

    assert result is None


def test_resolve_job_code_folder_returns_path_when_it_exists(tmp_path):
    job_code = "2026-07-21_00016-007"
    expected = tmp_path / job_code
    expected.mkdir(parents=True)

    result = resolve_job_code_folder(str(tmp_path), job_code)

    assert result == expected


def test_resolve_job_code_folder_rejects_parent_traversal(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "technician"
    root.mkdir()

    assert resolve_job_code_folder(str(root), "../outside") is None


def test_resolve_job_code_folder_rejects_absolute_path(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()

    assert resolve_job_code_folder(str(tmp_path / "technician"), str(outside)) is None
    assert resolve_job_code_folder(str(tmp_path / "technician"), r"C:\\Windows") is None


def test_resolve_job_code_folder_rejects_symlink_escape(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "technician"
    root.mkdir()
    link = root / "job"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        return

    assert resolve_job_code_folder(str(root), "job") is None


def test_resolve_email_attachment_folder_accepts_file_under_trusted_root(tmp_path):
    root = tmp_path / "mail"
    folder = root / "42"
    folder.mkdir(parents=True)
    attachment = folder / "case.stl"
    attachment.write_bytes(b"mesh")

    assert resolve_email_attachment_folder(
        [_FakeAttachment(str(attachment))], [root]
    ) == folder.resolve()


def test_resolve_email_attachment_folder_rejects_outside_and_traversal(tmp_path):
    root = tmp_path / "mail"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    file = outside / "case.stl"
    file.write_bytes(b"mesh")
    traversed = root / ".." / "outside" / "case.stl"

    assert resolve_email_attachment_folder([_FakeAttachment(str(file))], [root]) is None
    assert resolve_email_attachment_folder([_FakeAttachment(str(traversed))], [root]) is None


def test_resolve_email_attachment_folder_rejects_missing_file(tmp_path):
    root = tmp_path / "mail"
    root.mkdir()

    assert resolve_email_attachment_folder(
        [_FakeAttachment(str(root / "missing.stl"))], [root]
    ) is None


def test_resolve_email_attachment_folder_rejects_symlink(tmp_path):
    root = tmp_path / "mail"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "case.stl"
    target.write_bytes(b"mesh")
    link = root / "case.stl"
    try:
        link.symlink_to(target)
    except OSError:
        return

    assert resolve_email_attachment_folder([_FakeAttachment(str(link))], [root]) is None


def test_attach_email_folder_availability_exposes_only_boolean(tmp_path):
    root = tmp_path / "mail"
    root.mkdir()
    file = root / "case.stl"
    file.write_bytes(b"mesh")

    class _FakeEmail:
        attachments = [_FakeAttachment(str(file))]

    email = _FakeEmail()
    attach_email_folder_availability([email], [root])

    assert email.folder_available is True


class TestTechnicianFolderIsNotPerRow:
    """Тека техніків лежить на МЕРЕЖЕВІЙ шарі — рахуємо звернення до диска.

    Бойовий випадок 03.09.26: «перемикання між вкладками ~5 секунд». Вимір
    лічильником викликів показав, що рендер черги на 200 рядків робив **600
    звернень до диска** — по три на рядок (два resolve() і один is_dir() у
    resolve_job_code_folder), і ще стільки ж у поллі КОЖНІ 15 СЕКУНД. На SMB
    кожне звернення це окремий round-trip: при 3 мс — 1.8 с на рендер, при
    10 мс — 6 с. Застосунок забивав шару сам собі.

    Тому тут перевіряється не швидкість (мережі в тесті немає), а САМА
    ВЛАСТИВІСТЬ: вартість не залежить від кількості рядків. Виміряти час на
    машині розробки неможливо — порахувати виклики можна завжди.
    """

    def _count_disk_calls(self, monkeypatch, db, orders):
        import os as _os
        from pathlib import Path as _Path

        calls = {"n": 0}
        for owner, name in ((_Path, "resolve"), (_Path, "is_dir"),
                            (_Path, "exists"), (_Path, "iterdir"),
                            (_os, "scandir"), (_os, "stat")):
            orig = getattr(owner, name)

            def wrapper(*a, _orig=orig, **k):
                calls["n"] += 1
                return _orig(*a, **k)

            monkeypatch.setattr(owner, name, wrapper)

        from app.order_folder import attach_job_code_folder_uris

        attach_job_code_folder_uris(db, orders)
        return calls["n"]

    def test_cost_does_not_grow_with_the_number_of_rows(self, monkeypatch, tmp_path):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import Session
        from sqlalchemy.pool import StaticPool

        import app.order_folder as of
        from app.db import Base
        from app.models import Order
        from app.settings_store import set_setting

        root = tmp_path / "tech"
        root.mkdir()
        for i in range(20):
            (root / f"2026-09-02_{i:05d}-007").mkdir()

        engine = create_engine("sqlite://", poolclass=StaticPool)
        Base.metadata.create_all(engine)

        def _orders(db, count):
            made = []
            for i in range(count):
                o = Order(
                    source="lab", sheet_tab="02.09.26", row_number=i + 7,
                    work_order_no=str(24000 + i),
                    job_code=f"2026-09-02_{i % 20:05d}-007", status="прийнято",
                )
                db.add(o)
                made.append(o)
            db.commit()
            return made

        with Session(engine) as db:
            set_setting(db, "technician_files_path", str(root))
            db.commit()

            few = _orders(db, 5)
            with of._tech_listing_lock:
                of._tech_listing.clear()
            cost_few = self._count_disk_calls(monkeypatch, db, few)

            many = _orders(db, 200)
            with of._tech_listing_lock:
                of._tech_listing.clear()
            cost_many = self._count_disk_calls(monkeypatch, db, many)

        # Сорок разів більше рядків — вартість та сама. Допуск на два
        # звернення: сам обхід кореня плюс його resolve().
        assert cost_many <= cost_few + 2, (
            f"вартість росте з рядками: 5 рядків = {cost_few} звернень, "
            f"200 рядків = {cost_many}. Повернувся stat на кожен рядок?"
        )
        # І посилання при цьому мусять реально проставитись — інакше тест
        # хвалив би код, що просто нічого не робить.
        assert sum(1 for o in many if o.job_code_folder_uri) == 200

    def test_missing_folder_gives_no_link(self, monkeypatch, tmp_path):
        """Технік ще не здав роботу — посилання немає, і це не помилка."""
        from sqlalchemy import create_engine
        from sqlalchemy.orm import Session
        from sqlalchemy.pool import StaticPool

        import app.order_folder as of
        from app.db import Base
        from app.models import Order
        from app.settings_store import set_setting

        root = tmp_path / "tech"
        root.mkdir()
        engine = create_engine("sqlite://", poolclass=StaticPool)
        Base.metadata.create_all(engine)

        with Session(engine) as db:
            set_setting(db, "technician_files_path", str(root))
            order = Order(source="lab", sheet_tab="02.09.26", row_number=7,
                          work_order_no="24122", job_code="немає-такої",
                          status="прийнято")
            db.add(order)
            db.commit()
            with of._tech_listing_lock:
                of._tech_listing.clear()
            of.attach_job_code_folder_uris(db, [order])

        assert order.job_code_folder_uri is None
        assert order.job_code_folder_preview_token is None

    def test_traversal_in_job_code_is_still_refused(self, monkeypatch, tmp_path):
        """job_code приходить зі спільної таблиці, тобто ззовні. Пакетний шлях
        мусить різати шлях так само суворо, як поодинокий — інакше швидкий
        варіант тихо став би дірою."""
        from sqlalchemy import create_engine
        from sqlalchemy.orm import Session
        from sqlalchemy.pool import StaticPool

        import app.order_folder as of
        from app.db import Base
        from app.models import Order
        from app.settings_store import set_setting

        root = tmp_path / "tech"
        (root / "справжня").mkdir(parents=True)
        (tmp_path / "секрет").mkdir()

        engine = create_engine("sqlite://", poolclass=StaticPool)
        Base.metadata.create_all(engine)

        with Session(engine) as db:
            set_setting(db, "technician_files_path", str(root))
            bad = [
                Order(source="lab", sheet_tab="02.09.26", row_number=i + 7,
                      work_order_no=str(i), job_code=code, status="прийнято")
                for i, code in enumerate((
                    "..", "../секрет", r"..\секрет", "/etc", r"C:\Windows",
                    r"\host\share", ".",
                ))
            ]
            for o in bad:
                db.add(o)
            db.commit()
            with of._tech_listing_lock:
                of._tech_listing.clear()
            of.attach_job_code_folder_uris(db, bad)

        assert all(o.job_code_folder_uri is None for o in bad), (
            "пакетний шлях пропустив traversal у job_code"
        )


def test_warm_tech_listing_forces_refresh_and_fills_cache(tmp_path):
    """Грійник ПРИМУСОВО оновлює кеш, а не проходить крізь TTL.

    Через `_tech_root_children` на 20-секундному інтервалі кеш (TTL 30с) був би
    ще теплим і не оновився б. warm_tech_listing мусить сканувати завжди."""
    from app import order_folder as of

    (tmp_path / "Іванов").mkdir()
    with of._tech_listing_lock:
        of._tech_listing.clear()

    assert of.warm_tech_listing(str(tmp_path)) == 1
    # У кеші лежить свіже — наступне читання не сканує диск.
    _root, names = of._tech_root_children(str(tmp_path))
    assert names == frozenset({"Іванов"})

    # Додаємо теку й ФОРСУЄМО — warm бачить її попри теплий кеш.
    (tmp_path / "Петров").mkdir()
    assert of.warm_tech_listing(str(tmp_path)) == 2


def test_warm_tech_listing_empty_path_is_noop():
    from app import order_folder as of

    assert of.warm_tech_listing("") == 0


def test_tech_listing_single_flight(tmp_path):
    """5 одночасних запитів на ХОЛОДНОМУ кеші дають ОДИН скан, не п'ять.

    Скан Synology коштує 4-5с; без single-flight кожна відкрита вкладка
    запускала свій (спіймано /diag/perf 04.09.26 — три скани по 5с водночас)."""
    import threading
    import time as _time
    from app import order_folder as of

    (tmp_path / "Іванов").mkdir()
    with of._tech_listing_lock:
        of._tech_listing.clear()

    scans = {"n": 0}
    original = of._scan_tech_root

    def counting(path, now):
        scans["n"] += 1
        _time.sleep(0.2)  # імітуємо повільну шару
        return original(path, now)

    of._scan_tech_root = counting
    try:
        threads = [
            threading.Thread(target=of._tech_root_children, args=(str(tmp_path),))
            for _ in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        of._scan_tech_root = original

    assert scans["n"] == 1, f"мало бути 1 скан на всіх, а було {scans['n']}"
