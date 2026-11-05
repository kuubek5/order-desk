"""Queue readiness badge logic (готово / перевірити:поле / 3D-друк)."""

from types import SimpleNamespace

from app.triage_status import triage_readiness


def _email(**kw):
    kw.setdefault("service_type_guess", None)
    kw.setdefault("material_color_guess", None)
    return SimpleNamespace(**kw)


def test_ready_when_material_present():
    rd = triage_readiness(_email(material_color_guess="моно а3"))
    assert rd["state"] == "ready"
    assert rd["missing"] == []


def test_letter_with_undownloaded_files_is_not_ready():
    """Рядок казав «не скачано» і показував зелену галочку (власник 05.11.26)."""
    rd = triage_readiness(_email(material_color_guess="моно а3", attachments_status="skipped"))
    assert rd == {"state": "not_downloaded", "missing": ["файли не скачано"]}


def test_undownloaded_and_no_material_names_both():
    rd = triage_readiness(_email(attachments_status="skipped"))
    assert rd["state"] == "incomplete"
    assert rd["missing"] == ["файли не скачано", "матеріал"]


def test_letter_still_downloading_is_not_ready():
    rd = triage_readiness(_email(material_color_guess="моно а3", attachments_status="pending"))
    assert rd == {"state": "incomplete", "missing": ["файли ще качаються"]}


def test_downloaded_letter_is_ready():
    rd = triage_readiness(_email(material_color_guess="моно а3", attachments_status="ready"))
    assert rd["state"] == "ready"


def test_incomplete_when_material_missing():
    rd = triage_readiness(_email(material_color_guess=None))
    assert rd["state"] == "incomplete"
    assert rd["missing"] == ["матеріал"]


def test_blank_material_counts_as_missing():
    rd = triage_readiness(_email(material_color_guess="   "))
    assert rd["state"] == "incomplete"


def test_material_found_by_canon_counts_as_recognised():
    """Здогад порожній (записаний до фіксу розпізнавання), але роутер знайшов
    матеріал каноном у темі — «Эмоушен а3» → `emo a3` (25.09.26). Чіп і бейдж
    не мусять суперечити один одному."""
    rd = triage_readiness(_email(material_color_guess=None, material_known=True))
    assert rd["state"] == "ready"
    assert rd["missing"] == []


def test_3d_takes_precedence_over_completeness():
    # Even with a material guessed, a 3D-print hint wins — it isn't the lab's work.
    rd = triage_readiness(_email(service_type_guess="3d_print", material_color_guess="temp"))
    assert rd["state"] == "3d"
    assert rd["missing"] == []


def test_missing_attributes_are_safe():
    # A bare object (no guess attributes at all) must not crash.
    rd = triage_readiness(SimpleNamespace())
    assert rd["state"] == "incomplete"
