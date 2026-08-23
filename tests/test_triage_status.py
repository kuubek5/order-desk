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


def test_incomplete_when_material_missing():
    rd = triage_readiness(_email(material_color_guess=None))
    assert rd["state"] == "incomplete"
    assert rd["missing"] == ["матеріал"]


def test_blank_material_counts_as_missing():
    rd = triage_readiness(_email(material_color_guess="   "))
    assert rd["state"] == "incomplete"


def test_3d_takes_precedence_over_completeness():
    # Even with a material guessed, a 3D-print hint wins — it isn't the lab's work.
    rd = triage_readiness(_email(service_type_guess="3d_print", material_color_guess="temp"))
    assert rd["state"] == "3d"
    assert rd["missing"] == []


def test_missing_attributes_are_safe():
    # A bare object (no guess attributes at all) must not crash.
    rd = triage_readiness(SimpleNamespace())
    assert rd["state"] == "incomplete"
