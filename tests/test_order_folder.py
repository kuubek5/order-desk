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
