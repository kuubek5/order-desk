"""Tests for app/export_scanner.py — scanning the physical export folder tree."""

from datetime import datetime, timedelta

from app.export_scanner import scan_export_folder, ExportEntry


class TestWellFormedExportTree:
    """Test scanning a realistic export folder tree with multiple clients and batches."""

    def test_well_formed_tree_returns_expected_entries(self, tmp_path):
        """Scan a well-formed tree and verify entry count, names, and files."""
        # Client 1 with 1 batch and 1 material folder
        client1_path = tmp_path / "Іваненко Петро"
        batch1_path = client1_path / "Новая папка"
        mat1_path = batch1_path / "mono a3"
        mat1_path.mkdir(parents=True)
        (mat1_path / "file1.stl").write_text("x")
        (mat1_path / "file2.stl").write_text("x")

        # Client 2 with 1 batch and 2 material folders
        client2_path = tmp_path / "Козак Василь"
        batch2_path = client2_path / "Новая папка (2)"
        mat2a_path = batch2_path / "pmma a2"
        mat2a_path.mkdir(parents=True)
        (mat2a_path / "crown.stl").write_text("x")

        mat2b_path = batch2_path / "titanum"
        mat2b_path.mkdir(parents=True)
        (mat2b_path / "abutment.stl").write_text("x")

        # Client 1 again with another batch
        batch1b_path = client1_path / "Новая папка (3)"
        mat3_path = batch1b_path / "zirconia"
        mat3_path.mkdir(parents=True)
        (mat3_path / "frame.stl").write_text("x")

        # Scan and verify
        result = scan_export_folder(tmp_path)

        assert len(result) == 4, "Should find 4 material-color folders"

        # Verify first entry (Іваненко Петро, Новая папка, mono a3)
        entry1 = next(e for e in result if e.client_folder_name == "Іваненко Петро" and e.material_color_folder_name == "mono a3")
        assert entry1.batch_folder_name == "Новая папка"
        assert set(entry1.files) == {"file1.stl", "file2.stl"}
        assert entry1.folder_path == mat1_path

        # Verify second entry (Козак Василь, Новая папка (2), pmma a2)
        entry2 = next(e for e in result if e.client_folder_name == "Козак Василь" and e.material_color_folder_name == "pmma a2")
        assert entry2.batch_folder_name == "Новая папка (2)"
        assert entry2.files == ["crown.stl"]
        assert entry2.folder_path == mat2a_path

        # Verify third entry (Козак Василь, Новая папка (2), titanum)
        entry3 = next(e for e in result if e.client_folder_name == "Козак Василь" and e.material_color_folder_name == "titanum")
        assert entry3.batch_folder_name == "Новая папка (2)"
        assert entry3.files == ["abutment.stl"]
        assert entry3.folder_path == mat2b_path

        # Verify fourth entry (Іваненко Петро, Новая папка (3), zirconia)
        entry4 = next(e for e in result if e.client_folder_name == "Іваненко Петро" and e.material_color_folder_name == "zirconia")
        assert entry4.batch_folder_name == "Новая папка (3)"
        assert entry4.files == ["frame.stl"]
        assert entry4.folder_path == mat3_path

    def test_multiple_files_in_one_folder(self, tmp_path):
        """Verify that files are correctly listed (multiple files in one folder)."""
        client_path = tmp_path / "Client A"
        batch_path = client_path / "Новая папка"
        mat_path = batch_path / "material1"
        mat_path.mkdir(parents=True)

        # Create multiple files
        for i in range(5):
            (mat_path / f"part_{i}.stl").write_text("content")

        result = scan_export_folder(tmp_path)

        assert len(result) == 1
        entry = result[0]
        assert len(entry.files) == 5
        assert set(entry.files) == {f"part_{i}.stl" for i in range(5)}

    def test_deeply_nested_folders_only_three_levels_scanned(self, tmp_path):
        """Verify that we only go 3 levels deep; level 4+ folders are ignored."""
        # Create structure: client / batch / material / subfolder / file
        client_path = tmp_path / "Client"
        batch_path = client_path / "Batch"
        mat_path = batch_path / "Material"
        mat_path.mkdir(parents=True)

        # File directly in material folder (should be included)
        (mat_path / "file.stl").write_text("x")

        # Subfolder in material folder (should be skipped)
        subfolder = mat_path / "subfolder"
        subfolder.mkdir()
        (subfolder / "nested_file.stl").write_text("x")

        result = scan_export_folder(tmp_path)

        assert len(result) == 1
        entry = result[0]
        # Should only have the top-level file, not the nested one
        assert entry.files == ["file.stl"]


class TestEmptyMaterialFolders:
    """Test handling of empty material-color folders."""

    def test_empty_material_folder_still_produces_entry(self, tmp_path):
        """An empty material-color folder should produce an ExportEntry with files=[]."""
        client_path = tmp_path / "Client"
        batch_path = client_path / "Batch"
        mat_path = batch_path / "Material"
        mat_path.mkdir(parents=True)
        # No files created

        result = scan_export_folder(tmp_path)

        assert len(result) == 1
        entry = result[0]
        assert entry.client_folder_name == "Client"
        assert entry.material_color_folder_name == "Material"
        assert entry.files == []
        assert entry.folder_path == mat_path

    def test_mixed_empty_and_nonempty_folders(self, tmp_path):
        """Mix of empty and non-empty material folders should both produce entries."""
        client_path = tmp_path / "Client"
        batch_path = client_path / "Batch"

        # Empty material folder
        mat_empty = batch_path / "empty_material"
        mat_empty.mkdir(parents=True)

        # Non-empty material folder
        mat_full = batch_path / "full_material"
        mat_full.mkdir(parents=True)
        (mat_full / "file.stl").write_text("x")

        result = scan_export_folder(tmp_path)

        assert len(result) == 2
        empty_entry = next(e for e in result if e.material_color_folder_name == "empty_material")
        full_entry = next(e for e in result if e.material_color_folder_name == "full_material")

        assert empty_entry.files == []
        assert full_entry.files == ["file.stl"]


class TestNonexistentRoot:
    """Test behavior when root path doesn't exist."""

    def test_nonexistent_root_returns_empty_list(self, tmp_path):
        """Scanning a non-existent root should return [], not raise."""
        nonexistent = tmp_path / "does" / "not" / "exist"
        result = scan_export_folder(nonexistent)
        assert result == []

    def test_nonexistent_root_string_path(self):
        """Scanning a non-existent root given as a string path should also return []."""
        result = scan_export_folder("/this/path/definitely/does/not/exist/12345")
        assert result == []


class TestCreatedAtTimestamp:
    """Test that created_at field is properly set to batch folder's creation time."""

    def test_created_at_is_datetime_instance(self, tmp_path):
        """created_at should be a datetime instance (not None, not a string)."""
        client_path = tmp_path / "Client"
        batch_path = client_path / "Batch"
        mat_path = batch_path / "Material"
        mat_path.mkdir(parents=True)
        (mat_path / "file.stl").write_text("x")

        result = scan_export_folder(tmp_path)

        assert len(result) == 1
        entry = result[0]
        assert isinstance(entry.created_at, datetime)
        assert entry.created_at is not None

    def test_created_at_is_reasonable_value(self, tmp_path):
        """created_at should be a reasonable timestamp (recent, not epoch zero)."""
        before = datetime.now()

        client_path = tmp_path / "Client"
        batch_path = client_path / "Batch"
        mat_path = batch_path / "Material"
        mat_path.mkdir(parents=True)
        (mat_path / "file.stl").write_text("x")

        result = scan_export_folder(tmp_path)
        after = datetime.now()

        assert len(result) == 1
        entry = result[0]
        # Windows' file-time clock and datetime.now() aren't read from the
        # same timer, so they can disagree by a sub-millisecond hair even
        # though the folder was genuinely created during this test — allow
        # a small margin instead of an exact before/after bracket.
        margin = timedelta(milliseconds=50)
        assert before - margin <= entry.created_at <= after + margin


class TestErrorHandling:
    """Test graceful error handling: permission errors, non-directory entries, etc."""

    def test_non_directory_at_level_1_skipped(self, tmp_path):
        """Non-directory entries at level 1 should be skipped silently."""
        # Create a file at level 1 (not a directory)
        (tmp_path / "file_at_level_1.txt").write_text("x")

        # Create a valid client folder
        client_path = tmp_path / "ValidClient"
        batch_path = client_path / "Batch"
        mat_path = batch_path / "Material"
        mat_path.mkdir(parents=True)
        (mat_path / "file.stl").write_text("x")

        result = scan_export_folder(tmp_path)

        # Should still find the valid entry, not crash on the file
        assert len(result) == 1
        assert result[0].client_folder_name == "ValidClient"

    def test_non_directory_at_level_2_skipped(self, tmp_path):
        """Non-directory entries at level 2 should be skipped silently."""
        client_path = tmp_path / "Client"
        client_path.mkdir()

        # Create a file at level 2 (not a batch directory)
        (client_path / "file_at_level_2.txt").write_text("x")

        # Create a valid batch folder
        batch_path = client_path / "ValidBatch"
        mat_path = batch_path / "Material"
        mat_path.mkdir(parents=True)
        (mat_path / "file.stl").write_text("x")

        result = scan_export_folder(tmp_path)

        # Should still find the valid entry, not crash on the file
        assert len(result) == 1
        assert result[0].batch_folder_name == "ValidBatch"

    def test_non_directory_at_level_3_skipped(self, tmp_path):
        """Non-directory entries at level 3 should be skipped silently."""
        client_path = tmp_path / "Client"
        batch_path = client_path / "Batch"
        batch_path.mkdir(parents=True)

        # Create a file at level 3 (not a material directory)
        (batch_path / "file_at_level_3.txt").write_text("x")

        # Create a valid material folder
        mat_path = batch_path / "Material"
        mat_path.mkdir()
        (mat_path / "file.stl").write_text("x")

        result = scan_export_folder(tmp_path)

        # Should still find the valid entry, not crash on the file
        assert len(result) == 1
        assert result[0].material_color_folder_name == "Material"


class TestExportEntryDataclass:
    """Test ExportEntry dataclass properties."""

    def test_export_entry_creation(self, tmp_path):
        """ExportEntry should be creatable with all fields and accessible as attributes."""
        mat_path = tmp_path / "material"
        mat_path.mkdir()

        now = datetime.now()
        entry = ExportEntry(
            client_folder_name="Test Client",
            batch_folder_name="Batch",
            created_at=now,
            material_color_folder_name="Material",
            files=["a.stl", "b.stl"],
            folder_path=mat_path,
        )

        assert entry.client_folder_name == "Test Client"
        assert entry.batch_folder_name == "Batch"
        assert entry.created_at == now
        assert entry.material_color_folder_name == "Material"
        assert entry.files == ["a.stl", "b.stl"]
        assert entry.folder_path == mat_path


class TestSpecialCharactersInFolderNames:
    """Test handling of special characters and Cyrillic names."""

    def test_cyrillic_folder_names(self, tmp_path):
        """Cyrillic folder names (Ukrainian) should be handled correctly."""
        # Create structure with Cyrillic names
        клієнт = tmp_path / "Іванопуло Сергій"
        партія = клієнт / "Нова папка"
        матеріал = партія / "цирконій A3.5"
        матеріал.mkdir(parents=True)
        (матеріал / "коронка1.stl").write_text("x")
        (матеріал / "коронка2.stl").write_text("x")

        result = scan_export_folder(tmp_path)

        assert len(result) == 1
        entry = result[0]
        assert entry.client_folder_name == "Іванопуло Сергій"
        assert entry.batch_folder_name == "Нова папка"
        assert entry.material_color_folder_name == "цирконій A3.5"
        assert set(entry.files) == {"коронка1.stl", "коронка2.stl"}

    def test_mixed_cyrillic_and_latin_names(self, tmp_path):
        """Mix of Cyrillic and Latin characters should work."""
        client_path = tmp_path / "Клієнт A123"
        batch_path = client_path / "Batch_Папка"
        mat_path = batch_path / "mono_A3_моно"
        mat_path.mkdir(parents=True)
        (mat_path / "file_001.stl").write_text("x")

        result = scan_export_folder(tmp_path)

        assert len(result) == 1
        entry = result[0]
        assert entry.client_folder_name == "Клієнт A123"
        assert entry.batch_folder_name == "Batch_Папка"
        assert entry.material_color_folder_name == "mono_A3_моно"


class TestFolderPathField:
    """Test that folder_path field contains the correct full path."""

    def test_folder_path_points_to_material_folder(self, tmp_path):
        """folder_path should be the full path to the level-3 (material-color) folder."""
        client_path = tmp_path / "Client"
        batch_path = client_path / "Batch"
        mat_path = batch_path / "Material"
        mat_path.mkdir(parents=True)
        (mat_path / "file.stl").write_text("x")

        result = scan_export_folder(tmp_path)

        assert len(result) == 1
        entry = result[0]
        assert entry.folder_path == mat_path
        assert entry.folder_path.is_absolute()
        assert entry.folder_path.name == "Material"


class TestExportScanCache:
    """Кеш обходу export — лікує «GET /handout took 65.281s» з бойового логу
    25.08.26: `export` на мережевому диску, повний прохід triрівневого дерева
    йшов на КОЖНЕ відкриття видачі."""

    def test_second_call_does_not_touch_the_disk_again(self, tmp_path, monkeypatch):
        from app import export_scanner

        export_scanner.clear_export_cache()
        (tmp_path / "Люмі" / "17.08.26" / "моно a3").mkdir(parents=True)

        calls = []
        real = export_scanner.scan_export_folder

        def counting(root):
            calls.append(root)
            return real(root)

        monkeypatch.setattr(export_scanner, "scan_export_folder", counting)
        first = export_scanner.scan_export_folder_cached(tmp_path)
        second = export_scanner.scan_export_folder_cached(tmp_path)

        assert len(calls) == 1, "другий виклик мав прийти з кешу"
        assert [e.material_color_folder_name for e in first] == ["моно a3"]
        assert second == first

    def test_clear_cache_makes_the_next_call_hit_the_disk(self, tmp_path):
        from app import export_scanner

        export_scanner.clear_export_cache()
        (tmp_path / "Люмі" / "17.08.26" / "моно a3").mkdir(parents=True)
        assert len(export_scanner.scan_export_folder_cached(tmp_path)) == 1

        # нова тека з'явилась (оператор прийняв лист) — без скидання кешу
        # видача її б не побачила
        (tmp_path / "Ортос" / "17.08.26" / "пмма A2").mkdir(parents=True)
        assert len(export_scanner.scan_export_folder_cached(tmp_path)) == 1, "кеш ще діє"

        export_scanner.clear_export_cache()
        assert len(export_scanner.scan_export_folder_cached(tmp_path)) == 2

    def test_missing_root_is_cached_as_empty_not_an_error(self, tmp_path):
        from app import export_scanner

        export_scanner.clear_export_cache()
        assert export_scanner.scan_export_folder_cached(tmp_path / "немає") == []


class TestBatchDateCutoff:
    """Регрес-гард для «46148 записів, 511.42с» (бойовий лог 27.08.26).

    На Synology у клієнта накопичуються РОКИ партій (~176 на клієнта), а
    видача показує роботи за 30 днів. Стару партію треба пропускати, НЕ
    заходячи в неї: час створення scandir віддає безкоштовно, а кожна тека
    всередині — це окрема мережева ходка."""

    def _batch(self, root, client, name, age_days, materials=("mono a3", "pmma A2")):
        import os
        import time as _t
        for m in materials:
            (root / client / name / m).mkdir(parents=True)
            (root / client / name / m / "crown.stl").write_bytes(b"x")
        stamp = _t.time() - age_days * 86400
        os.utime(root / client / name, (stamp, stamp))

    def test_old_batches_are_not_descended_into(self, tmp_path, monkeypatch):
        """Перевіряємо саме РІШЕННЯ пропустити, а не файлову систему:
        os.utime міняє час зміни, а код дивиться на час СТВОРЕННЯ, який на
        Windows окремий і засобами тесту не підробляється."""
        from datetime import datetime, timedelta
        from types import SimpleNamespace
        from app import export_scanner

        now = datetime.now().timestamp()
        old = now - 200 * 86400

        def fake_batch(name, ctime):
            return SimpleNamespace(
                name=name,
                path=str(tmp_path / name),
                is_dir=lambda: True,
                stat=lambda: SimpleNamespace(st_ctime=ctime),
            )

        batches = [fake_batch(f"стара-{i:02d}", old) for i in range(20)]
        batches.append(fake_batch("свіжа", now - 3 * 86400))

        opened = []

        def fake_entries(path):
            opened.append(str(path))
            # корінь клієнта віддає партії; глибше — порожньо
            return batches if str(path).endswith("Люмі") else []

        monkeypatch.setattr(export_scanner, "_dir_entries", fake_entries)
        export_scanner.scan_export_client(
            tmp_path, "Люмі", datetime.now() - timedelta(days=30)
        )

        assert not any("стара-" in p for p in opened), (
            f"у старі партії заходили: {[p for p in opened if 'стара-' in p][:3]}"
        )
        assert any("свіжа" in p for p in opened), "свіжу партію мали прочитати"

    def test_without_cutoff_everything_is_read(self, tmp_path):
        from app import export_scanner

        export_scanner.clear_export_cache()
        self._batch(tmp_path, "Люмі", "стара", age_days=400)
        self._batch(tmp_path, "Люмі", "свіжа", age_days=1)

        entries = export_scanner.scan_export_client(tmp_path, "Люмі")
        assert {e.batch_folder_name for e in entries} == {"стара", "свіжа"}
