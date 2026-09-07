"""Архів для переїзду: що в ньому мусить бути, а чого не має бути ніколи.

Копія бази свідомо не несе байтів файлів, тож переїзд ПК→ПК складається з
двох файлів. Другий — цей архів. Помилка тут тиха: людина переїжджає, все
начебто на місці, а фото зі зміни зникли — і зрозуміти це можна лише через
тиждень, коли хтось відкриє стару записку.
"""

import zipfile
from io import BytesIO

from app import migration_files


def _wire(monkeypatch, tmp_path):
    """Три теки з файлами + спул пошти поруч, якого в архіві бути не повинно."""
    shift = tmp_path / "shift_images"
    feedback = tmp_path / "feedback_images"
    portraits = tmp_path / "machine_portraits"
    spool = tmp_path / "mail_attachments"
    for folder in (shift, feedback, portraits, spool):
        folder.mkdir()
    (shift / "note-1.jpg").write_bytes(b"shift photo")
    (shift / "2026-09" / "note-2.jpg").parent.mkdir()
    (shift / "2026-09" / "note-2.jpg").write_bytes(b"nested photo")
    (feedback / "bug-1.png").write_bytes(b"screenshot")
    (portraits / "1.jpg").write_bytes(b"portrait")
    (spool / "huge.stl").write_bytes(b"x" * 1024)

    monkeypatch.setattr(migration_files, "BUNDLED_FOLDERS", {
        "shift_images": str(shift),
        "feedback_images": str(feedback),
        "machine_portraits": str(portraits),
    })
    return tmp_path


def test_the_bundle_carries_the_photos_a_backup_leaves_behind(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path)

    with zipfile.ZipFile(BytesIO(migration_files.build_zip())) as archive:
        names = set(archive.namelist())
        assert "shift_images/note-1.jpg" in names
        assert "shift_images/2026-09/note-2.jpg" in names      # вкладені теки теж
        assert "feedback_images/bug-1.png" in names
        assert "machine_portraits/1.jpg" in names
        assert archive.read("shift_images/note-1.jpg") == b"shift photo"


def test_the_mail_spool_never_travels(monkeypatch, tmp_path):
    """Спул — сотні мегабайтів, і ті самі файли лежать у скриньці: після
    переїзду їх повертає «Скачати ще раз». Класти їх сюди означало б зробити
    архів непереносним заради даних, які є у двох місцях."""
    _wire(monkeypatch, tmp_path)

    with zipfile.ZipFile(BytesIO(migration_files.build_zip())) as archive:
        assert not [n for n in archive.namelist() if "mail_attachments" in n]
        assert not [n for n in archive.namelist() if n.endswith("huge.stl")]


def test_the_bundle_explains_itself(monkeypatch, tmp_path):
    """Інструкція їде всередині: архів переживе цей чат, памʼять і людину, яка
    його робила."""
    _wire(monkeypatch, tmp_path)

    with zipfile.ZipFile(BytesIO(migration_files.build_zip())) as archive:
        text = archive.read("ЯК-ПЕРЕНЕСТИ.txt").decode("utf-8")
    assert "Резервна копія" in text
    assert "Ліцензія" in text                    # нове залізо — новий ключ
    assert "Скачати ще раз" in text              # що робити з листами без файлів


def test_an_empty_folder_still_produces_a_valid_archive(monkeypatch, tmp_path):
    """Порожня тека в архіві означає «тут нічого не було». Її ВІДСУТНІСТЬ
    читалась би як «збір не спрацював» — а це різні речі."""
    for name in ("shift_images", "feedback_images", "machine_portraits"):
        (tmp_path / name).mkdir()
    monkeypatch.setattr(migration_files, "BUNDLED_FOLDERS", {
        name: str(tmp_path / name)
        for name in ("shift_images", "feedback_images", "machine_portraits")
    })

    with zipfile.ZipFile(BytesIO(migration_files.build_zip())) as archive:
        assert archive.testzip() is None
        assert "shift_images/" in archive.namelist()


def test_a_missing_folder_does_not_break_the_collection(monkeypatch, tmp_path):
    """Теки може не бути взагалі — на свіжій інсталяції її ще ніхто не створив."""
    monkeypatch.setattr(migration_files, "BUNDLED_FOLDERS", {
        "shift_images": str(tmp_path / "ніколи-не-існувала"),
    })

    with zipfile.ZipFile(BytesIO(migration_files.build_zip())) as archive:
        assert archive.testzip() is None


def test_summary_counts_what_will_travel(monkeypatch, tmp_path):
    """Число видно ДО кліку — інакше незрозуміло, чи качати архів узагалі."""
    _wire(monkeypatch, tmp_path)

    by_name = {folder.name: folder for folder in migration_files.summarize()}
    assert by_name["shift_images"].files == 2
    assert by_name["feedback_images"].files == 1
    assert by_name["machine_portraits"].bytes_total == len(b"portrait")


def test_folders_have_human_labels():
    """У тексті для оператора мають стояти речі, а не імена тек на диску.

    Перша версія показувала «shift_images: 1 файл(ів)» — це мова файлової
    системи, а не людини, яка переносить застосунок.
    """
    labels = {folder: migration_files.FOLDER_LABELS.get(folder) for folder in migration_files.BUNDLED_FOLDERS}
    assert all(labels.values()), f"тека без людського підпису: {labels}"

    summary = migration_files.FolderSummary(name="shift_images", files=2, bytes_total=10)
    assert summary.label == "фото до записок зміни"
