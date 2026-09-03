"""Traversal-safety tests for the hover STL preview token scheme.

app/stl_preview.py is the security boundary for a route that streams raw
file bytes back to the browser (app/web.py `/stl-preview/{token}` and
`/stl-preview/{token}/{filename}`) — a token or filename that isn't
rejected correctly here is a directory-traversal read-arbitrary-file bug.
"""

import base64

import pytest

from app import stl_preview
from app.stl_preview import (
    build_preview_token,
    list_stl_files,
    resolve_preview_folder,
    resolve_stl_file,
)


class _FakeDb:
    """resolve_preview_folder only ever passes `db` through to the
    root resolvers, which we monkeypatch below — the object itself is
    never touched."""


def _encode(payload: str) -> str:
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")


# --- build_preview_token / resolve_preview_folder round trip -------------


def test_round_trip_for_folder_under_export_root(tmp_path, monkeypatch):
    root = tmp_path / "export"
    folder = root / "Клієнт" / "нова папка" / "моно а3"
    folder.mkdir(parents=True)
    monkeypatch.setattr(stl_preview, "get_export_folder_path", lambda db: str(root))

    token = build_preview_token(folder, {"export": str(root)})
    assert token is not None

    resolved = resolve_preview_folder(_FakeDb(), token)
    assert resolved == folder.resolve()


def test_round_trip_picks_matching_root_among_several(tmp_path, monkeypatch):
    export_root = tmp_path / "export"
    mail_root = tmp_path / "mail"
    export_root.mkdir()
    mail_root.mkdir()
    folder = mail_root / "42"
    folder.mkdir()

    monkeypatch.setattr(stl_preview, "get_export_folder_path", lambda db: str(export_root))
    monkeypatch.setattr(stl_preview, "MAIL_ATTACHMENTS_PATH", str(mail_root))

    token = build_preview_token(folder, {"export": str(export_root), "mail": str(mail_root)})
    assert token is not None

    resolved = resolve_preview_folder(_FakeDb(), token)
    assert resolved == folder.resolve()


def test_build_preview_token_none_when_folder_outside_all_roots(tmp_path):
    root = tmp_path / "export"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    assert build_preview_token(outside, {"export": str(root)}) is None


def test_build_preview_token_none_for_symlink_escape(tmp_path):
    root = tmp_path / "export"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = root / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks not supported without elevated privileges here")

    assert build_preview_token(link, {"export": str(root)}) is None


def test_build_preview_token_none_when_root_missing(tmp_path):
    folder = tmp_path / "somewhere"
    folder.mkdir()

    assert build_preview_token(folder, {"export": None}) is None
    assert build_preview_token(folder, {"export": ""}) is None
    assert build_preview_token(folder, {"export": str(tmp_path / "does-not-exist")}) is None


# --- resolve_preview_folder: hostile/tampered tokens ----------------------


def test_resolve_preview_folder_rejects_parent_traversal(tmp_path, monkeypatch):
    root = tmp_path / "export"
    root.mkdir()
    (tmp_path / "outside").mkdir()
    monkeypatch.setattr(stl_preview, "get_export_folder_path", lambda db: str(root))

    token = _encode("export:../outside")
    assert resolve_preview_folder(_FakeDb(), token) is None


def test_resolve_preview_folder_rejects_windows_traversal_style(tmp_path, monkeypatch):
    root = tmp_path / "export"
    root.mkdir()
    monkeypatch.setattr(stl_preview, "get_export_folder_path", lambda db: str(root))

    for payload in [
        "export:../../windows/system32",
        "export:..\\..\\windows\\system32",
        "export:sub/../../etc/passwd",
    ]:
        token = _encode(payload)
        assert resolve_preview_folder(_FakeDb(), token) is None, payload


def test_resolve_preview_folder_rejects_absolute_drive_segment(tmp_path, monkeypatch):
    root = tmp_path / "export"
    root.mkdir()
    monkeypatch.setattr(stl_preview, "get_export_folder_path", lambda db: str(root))

    token = _encode("export:C:/Windows/System32")
    assert resolve_preview_folder(_FakeDb(), token) is None


def test_resolve_preview_folder_rejects_unknown_root_key(tmp_path, monkeypatch):
    root = tmp_path / "export"
    folder = root / "sub"
    folder.mkdir(parents=True)
    monkeypatch.setattr(stl_preview, "get_export_folder_path", lambda db: str(root))

    token = _encode("evil:sub")
    assert resolve_preview_folder(_FakeDb(), token) is None


def test_resolve_preview_folder_rejects_malformed_base64(monkeypatch):
    assert resolve_preview_folder(_FakeDb(), "not-valid-base64!!!") is None


def test_resolve_preview_folder_rejects_empty_and_missing_token():
    assert resolve_preview_folder(_FakeDb(), "") is None


def test_resolve_preview_folder_rejects_payload_without_separator(tmp_path, monkeypatch):
    root = tmp_path / "export"
    root.mkdir()
    monkeypatch.setattr(stl_preview, "get_export_folder_path", lambda db: str(root))

    token = _encode("exportsub")  # no ':'
    assert resolve_preview_folder(_FakeDb(), token) is None


def test_resolve_preview_folder_rejects_nonexistent_subfolder(tmp_path, monkeypatch):
    root = tmp_path / "export"
    root.mkdir()
    monkeypatch.setattr(stl_preview, "get_export_folder_path", lambda db: str(root))

    token = _encode("export:never-created")
    assert resolve_preview_folder(_FakeDb(), token) is None


def test_resolve_preview_folder_rejects_symlink_hop(tmp_path, monkeypatch):
    root = tmp_path / "export"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = root / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks not supported without elevated privileges here")
    monkeypatch.setattr(stl_preview, "get_export_folder_path", lambda db: str(root))

    token = _encode("export:link")
    assert resolve_preview_folder(_FakeDb(), token) is None


def test_resolve_preview_folder_rejects_when_root_disappears(tmp_path, monkeypatch):
    root = tmp_path / "export"
    root.mkdir()
    monkeypatch.setattr(stl_preview, "get_export_folder_path", lambda db: str(tmp_path / "gone"))

    token = _encode("export:sub")
    assert resolve_preview_folder(_FakeDb(), token) is None


# --- list_stl_files --------------------------------------------------------


def test_list_stl_files_returns_only_stl_case_insensitive_sorted(tmp_path):
    folder = tmp_path / "material"
    folder.mkdir()
    (folder / "b.STL").write_bytes(b"x")
    (folder / "a.stl").write_bytes(b"x")
    (folder / "notes.txt").write_bytes(b"x")
    (folder / "subdir").mkdir()

    assert list_stl_files(folder) == ["a.stl", "b.STL"]


def test_list_stl_files_empty_for_missing_folder(tmp_path):
    assert list_stl_files(tmp_path / "missing") == []


# --- resolve_stl_file --------------------------------------------------------


def test_resolve_stl_file_accepts_plain_stl_in_folder(tmp_path):
    folder = tmp_path / "material"
    folder.mkdir()
    target = folder / "case.stl"
    target.write_bytes(b"mesh")

    assert resolve_stl_file(folder, "case.stl") == target.resolve()


def test_resolve_stl_file_rejects_path_separators(tmp_path):
    folder = tmp_path / "material"
    folder.mkdir()
    (folder / "case.stl").write_bytes(b"mesh")
    outside_file = tmp_path / "case.stl"
    outside_file.write_bytes(b"mesh")

    assert resolve_stl_file(folder, "../case.stl") is None
    assert resolve_stl_file(folder, "sub/case.stl") is None
    assert resolve_stl_file(folder, "..\\case.stl") is None


def test_resolve_stl_file_rejects_non_stl_extension(tmp_path):
    folder = tmp_path / "material"
    folder.mkdir()
    (folder / "secret.txt").write_bytes(b"data")

    assert resolve_stl_file(folder, "secret.txt") is None


def test_resolve_stl_file_rejects_missing_file(tmp_path):
    folder = tmp_path / "material"
    folder.mkdir()

    assert resolve_stl_file(folder, "ghost.stl") is None


def test_resolve_stl_file_rejects_symlinked_file(tmp_path):
    folder = tmp_path / "material"
    folder.mkdir()
    outside = tmp_path / "outside.stl"
    outside.write_bytes(b"mesh")
    link = folder / "case.stl"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks not supported without elevated privileges here")

    assert resolve_stl_file(folder, "case.stl") is None


class TestLexicalTokenStaysSafe:
    """Дешевий токен не дає доступу нікуди, куди не дав би дорогий.

    build_preview_token_lexical не ходить на диск — саме тому екран черги
    перестав коштувати 18 звернень до мережевої шари на рядок (вимір
    03.09.26). Оптимізація тримається на одному: токен НЕ Є ПЕРЕПУСТКОЮ,
    resolve_preview_folder перевіряє все заново від кореня з налаштувань.
    Тут перевіряється саме ця опора — якщо вона колись зникне, дешевий токен
    миттєво стане дірою.
    """

    def test_folder_outside_the_root_gets_no_token(self, tmp_path):
        from app.stl_preview import build_preview_token_lexical, validate_preview_roots

        root = tmp_path / "export"
        root.mkdir()
        outside = tmp_path / "секрет"
        outside.mkdir()

        roots = {"export": str(root)}
        validated = validate_preview_roots(roots)
        assert build_preview_token_lexical(outside, roots, validated) is None

    def test_traversal_segments_get_no_token(self, tmp_path):
        from app.stl_preview import build_preview_token_lexical, validate_preview_roots

        root = tmp_path / "export"
        (root / "клієнт").mkdir(parents=True)
        roots = {"export": str(root)}
        validated = validate_preview_roots(roots)
        assert build_preview_token_lexical(root / "клієнт" / ".." / "..", roots, validated) is None

    def test_serve_side_refuses_a_token_whose_folder_is_gone(self, tmp_path, monkeypatch):
        """Опора всієї оптимізації: перевірка диском живе на видачі байтів.

        Токен збирається лексично, тека потім зникає — роут мусить відмовити,
        а не віддати вміст."""
        from sqlalchemy import create_engine
        from sqlalchemy.orm import Session
        from sqlalchemy.pool import StaticPool

        import app.stl_preview as sp
        from app.db import Base
        from app.settings_store import set_setting

        root = tmp_path / "export"
        folder = root / "клієнт" / "03.09.26"
        folder.mkdir(parents=True)

        roots = {"export": str(root)}
        token = sp.build_preview_token_lexical(folder, roots, sp.validate_preview_roots(roots))
        assert token, "лексичний токен мав зібратись для теки під коренем"

        engine = create_engine("sqlite://", poolclass=StaticPool)
        Base.metadata.create_all(engine)
        with Session(engine) as db:
            set_setting(db, "export_folder_path", str(root))
            db.commit()
            assert sp.resolve_preview_folder(db, token) == folder.resolve()

            folder.rmdir()
            assert sp.resolve_preview_folder(db, token) is None, (
                "роут віддав теку, якої вже немає — перевірка диском на видачі зникла"
            )
