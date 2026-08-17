from unittest.mock import patch

import pytest

from app.mail_export import sanitize_folder_name, save_attachments_to_export


def test_sanitize_folder_name_replaces_illegal_chars():
    assert sanitize_folder_name('О"Коннор: тест/файл') == "О_Коннор_ тест_файл"


def test_sanitize_folder_name_empty_falls_back():
    assert sanitize_folder_name("   ") == "без_імені"


def test_sanitize_folder_name_rejects_dot_segments():
    assert sanitize_folder_name(".") == "без_імені"
    assert sanitize_folder_name("..") == "без_імені"


def test_moves_single_attachment_into_client_batch_material_tree(tmp_path):
    export_root = tmp_path / "export"
    src = tmp_path / "incoming.stl"
    src.write_bytes(b"data")

    new_paths = save_attachments_to_export(export_root, "Тестовий Клієнт", "моно а3", [src])

    assert len(new_paths) == 1
    assert new_paths[0].name == "incoming.stl"
    assert new_paths[0].parent.name == "моно а3"
    assert new_paths[0].parent.parent.name == "нова папка"
    assert new_paths[0].parent.parent.parent.name == "Тестовий Клієнт"
    assert new_paths[0].read_bytes() == b"data"
    assert not src.exists()


def test_second_batch_for_same_client_gets_next_number(tmp_path):
    """A second order for a material the latest batch already has must not
    land inside that same material folder alongside the first order's files
    — it collides, so it gets its own new batch instead."""
    export_root = tmp_path / "export"
    (export_root / "Клієнт" / "нова папка" / "пмма").mkdir(parents=True)

    src = tmp_path / "file.stl"
    src.write_bytes(b"x")

    new_paths = save_attachments_to_export(export_root, "Клієнт", "пмма", [src])

    assert new_paths[0].parent.parent.name == "нова папка (2)"


def test_different_material_reuses_latest_batch_instead_of_new_one(tmp_path):
    """A different material for the same client's latest batch has no
    collision to resolve, so it's added straight into that batch rather than
    forking off a new one."""
    export_root = tmp_path / "export"
    src1 = tmp_path / "first.stl"
    src1.write_bytes(b"first")
    first_paths = save_attachments_to_export(export_root, "Клієнт", "емо а3", [src1])

    src2 = tmp_path / "second.stl"
    src2.write_bytes(b"second")
    second_paths = save_attachments_to_export(export_root, "Клієнт", "емо а2", [src2])

    assert first_paths[0].parent.parent.name == "нова папка"
    assert second_paths[0].parent.parent.name == "нова папка"
    assert second_paths[0].parent.name == "емо а2"
    batch_dir = export_root / "Клієнт" / "нова папка"
    assert sorted(p.name for p in batch_dir.iterdir()) == ["емо а2", "емо а3"]


def test_vova_three_order_batching_scenario(tmp_path):
    """Exact scenario confirmed with the user (CLAUDE.md task): same
    material twice in a row collides and starts a new batch; a third,
    different material then lands in that new batch without forking again.

    Замовлення 1 (емо а3) -> нема батчів -> "нова папка/емо а3".
    Замовлення 2 (емо а3) -> колізія в "нова папка" -> "нова папка (2)/емо а3".
    Замовлення 3 (емо а2) -> "нова папка (2)" вільна -> "нова папка (2)/емо а2".
    """
    export_root = tmp_path / "export"

    src1 = tmp_path / "order1.stl"
    src1.write_bytes(b"1")
    paths1 = save_attachments_to_export(export_root, "Вова", "емо а3", [src1])

    src2 = tmp_path / "order2.stl"
    src2.write_bytes(b"2")
    paths2 = save_attachments_to_export(export_root, "Вова", "емо а3", [src2])

    src3 = tmp_path / "order3.stl"
    src3.write_bytes(b"3")
    paths3 = save_attachments_to_export(export_root, "Вова", "емо а2", [src3])

    assert paths1[0].parent.parent.name == "нова папка"
    assert paths1[0].parent.name == "емо а3"

    assert paths2[0].parent.parent.name == "нова папка (2)"
    assert paths2[0].parent.name == "емо а3"

    assert paths3[0].parent.parent.name == "нова папка (2)"
    assert paths3[0].parent.name == "емо а2"

    client_dir = export_root / "Вова"
    assert sorted(p.name for p in client_dir.iterdir()) == ["нова папка", "нова папка (2)"]
    assert sorted(p.name for p in (client_dir / "нова папка").iterdir()) == ["емо а3"]
    assert sorted(p.name for p in (client_dir / "нова папка (2)").iterdir()) == [
        "емо а2",
        "емо а3",
    ]


def test_empty_material_uses_placeholder_folder(tmp_path):
    export_root = tmp_path / "export"
    src = tmp_path / "file.stl"
    src.write_bytes(b"x")

    new_paths = save_attachments_to_export(export_root, "Клієнт", "", [src])

    assert new_paths[0].parent.name == "без_матеріалу"


def test_no_attachments_returns_empty_list(tmp_path):
    assert save_attachments_to_export(tmp_path / "export", "Клієнт", "моно", []) == []


def test_duplicate_attachment_names_are_not_overwritten(tmp_path):
    export_root = tmp_path / "export"
    first_dir = tmp_path / "mail-1"
    second_dir = tmp_path / "mail-2"
    first_dir.mkdir()
    second_dir.mkdir()
    first = first_dir / "case.stl"
    second = second_dir / "case.stl"
    first.write_bytes(b"first")
    second.write_bytes(b"second")

    paths = save_attachments_to_export(export_root, "Клієнт", "моно", [first, second])

    assert [path.name for path in paths] == ["case.stl", "case (2).stl"]
    assert [path.read_bytes() for path in paths] == [b"first", b"second"]


def test_partial_move_is_rolled_back(tmp_path):
    export_root = tmp_path / "export"
    first = tmp_path / "first.stl"
    second = tmp_path / "second.stl"
    first.write_bytes(b"first")
    second.write_bytes(b"second")

    real_move = __import__("shutil").move
    calls = 0

    def fail_second_move(source, destination):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("disk unavailable")
        return real_move(source, destination)

    with patch("app.mail_export.shutil.move", side_effect=fail_second_move):
        with pytest.raises(OSError, match="disk unavailable"):
            save_attachments_to_export(export_root, "Клієнт", "моно", [first, second])

    assert first.read_bytes() == b"first"
    assert second.read_bytes() == b"second"


def test_repeat_client_with_retyped_name_reuses_existing_folder(tmp_path):
    """A client emails again weeks later; the operator retypes the name
    slightly differently at accept time (extra space, one-letter slip).
    Their second order (same material, so it collides with the first) should
    land as a new batch under the SAME client folder, not fork off a
    lookalike sibling folder."""
    export_root = tmp_path / "export"
    src1 = tmp_path / "first.stl"
    src1.write_bytes(b"first")
    save_attachments_to_export(export_root, "Литвиненко Олег", "моно а3", [src1])

    src2 = tmp_path / "second.stl"
    src2.write_bytes(b"second")
    new_paths = save_attachments_to_export(export_root, "Литвиненко Олег ", "моно а3", [src2])

    client_dirs = [p for p in export_root.iterdir() if p.is_dir()]
    assert len(client_dirs) == 1
    assert new_paths[0].parent.parent.parent.name == "Литвиненко Олег"
    assert new_paths[0].parent.parent.name == "нова папка (2)"


def test_genuinely_new_client_still_gets_own_folder(tmp_path):
    export_root = tmp_path / "export"
    src1 = tmp_path / "first.stl"
    src1.write_bytes(b"first")
    save_attachments_to_export(export_root, "Литвиненко Олег", "моно", [src1])

    src2 = tmp_path / "second.stl"
    src2.write_bytes(b"second")
    new_paths = save_attachments_to_export(export_root, "Дуже Інший Клієнт", "моно", [src2])

    client_dirs = sorted(p.name for p in export_root.iterdir() if p.is_dir())
    assert client_dirs == ["Дуже Інший Клієнт", "Литвиненко Олег"]
    assert new_paths[0].parent.parent.parent.name == "Дуже Інший Клієнт"


def test_dot_segment_client_cannot_escape_export_root(tmp_path):
    export_root = tmp_path / "export"
    src = tmp_path / "incoming.stl"
    src.write_bytes(b"data")

    [new_path] = save_attachments_to_export(export_root, "..", "моно", [src])

    assert new_path.is_relative_to(export_root.resolve())
    assert new_path.parent.parent.parent.name == "без_імені"


# --- accept wizard: directory preview + override -------------------------------

def _touch(p):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x")
    return p


def test_preview_export_target_new_client(tmp_path):
    from app.mail_export import preview_export_target
    prev = preview_export_target(tmp_path, "Новий Клієнт", "емо а3")
    assert prev["client_folder"] == "Новий Клієнт"
    assert prev["client_folder_existing"] is False
    assert prev["material_folder"] == "емо а3"
    assert prev["rel_path"] == "Новий Клієнт/нова папка/емо а3"


def test_preview_export_target_reuses_existing_batch(tmp_path):
    from app.mail_export import preview_export_target
    (tmp_path / "Клієнт" / "нова папка" / "титан").mkdir(parents=True)
    prev = preview_export_target(tmp_path, "Клієнт", "емо а3")
    assert prev["client_folder_existing"] is True
    assert prev["batch_folder"] == "нова папка"  # material slot free -> reuse
    assert prev["batch_reused"] is True


def test_preview_export_target_override_wins(tmp_path):
    from app.mail_export import preview_export_target
    (tmp_path / "Vision Dental").mkdir()
    prev = preview_export_target(tmp_path, "Vision", "емо а3", client_folder_override="Vision Dental")
    assert prev["client_folder"] == "Vision Dental"
    assert prev["client_folder_existing"] is True


def test_list_client_folders(tmp_path):
    from app.mail_export import list_client_folders
    (tmp_path / "Бета").mkdir()
    (tmp_path / "Альфа").mkdir()
    (tmp_path / "file.txt").write_bytes(b"x")
    assert list_client_folders(tmp_path) == ["Альфа", "Бета"]


def test_save_attachments_override_targets_named_folder(tmp_path):
    src = _touch(tmp_path / "spool" / "a.stl")
    (tmp_path / "export").mkdir()
    new_paths = save_attachments_to_export(
        tmp_path / "export", "Максим Тест", "емо а3", [src],
        client_folder_override="Окрема Папка",
    )
    assert new_paths[0].parts[-4:] == ("Окрема Папка", "нова папка", "емо а3", "a.stl")
    assert new_paths[0].is_file()


def test_preview_material_folder_override(tmp_path):
    from app.mail_export import preview_export_target
    prev = preview_export_target(
        tmp_path, "Клієнт", "моно а3", material_folder_override="спец матеріал"
    )
    assert prev["material_folder"] == "спец матеріал"
    assert prev["rel_path"].endswith("/спец матеріал")


def test_save_attachments_material_override_targets_named_subfolder(tmp_path):
    src = _touch(tmp_path / "spool" / "b.stl")
    (tmp_path / "export").mkdir()
    new_paths = save_attachments_to_export(
        tmp_path / "export", "Клініка", "моно а3", [src],
        client_folder_override="Клініка", material_folder_override="спец матеріал",
    )
    assert new_paths[0].parts[-4:] == ("Клініка", "нова папка", "спец матеріал", "b.stl")


def test_resolve_wizard_overrides_new_folder_wins():
    from app.web import _resolve_wizard_overrides
    # typed new folder beats the dropdown pick
    assert _resolve_wizard_overrides("Стара", "Нова", "мат") == ("Нова", "мат")
    # empty new -> pick is used
    assert _resolve_wizard_overrides("Стара", "  ", "мат") == ("Стара", "мат")
    # nothing picked/typed -> auto (empty client override)
    assert _resolve_wizard_overrides("", "", "") == ("", "")


def test_repeat_same_material_lands_in_new_batch_no_loss(tmp_path):
    """A client sending a SECOND email of the SAME material must not overwrite
    the first — the repeat goes into a fresh numbered batch, both survive."""
    exp = tmp_path / "export"; exp.mkdir()
    spool = tmp_path / "spool"; spool.mkdir()

    def mk(n):
        p = spool / n; p.write_bytes(b"x"); return p

    first = save_attachments_to_export(exp, "Іванов", "моно а3", [mk("crown.stl")])
    second = save_attachments_to_export(exp, "Іванов", "моно а3", [mk("crown.stl")])
    assert first[0].parent != second[0].parent  # different batch folders
    assert first[0].is_file() and second[0].is_file()  # nothing lost
    assert "нова папка (2)" in second[0].parts  # numbered repeat batch


def test_repeat_different_material_reuses_batch(tmp_path):
    """A different material for the same client piles into the latest batch
    (a free material slot), not a brand-new one."""
    exp = tmp_path / "export"; exp.mkdir()
    spool = tmp_path / "spool"; spool.mkdir()

    def mk(n):
        p = spool / n; p.write_bytes(b"x"); return p

    a = save_attachments_to_export(exp, "Іванов", "моно а3", [mk("a.stl")])
    b = save_attachments_to_export(exp, "Іванов", "пмма а2", [mk("b.stl")])
    assert a[0].parent.parent == b[0].parent.parent  # same batch, different material subfolder
