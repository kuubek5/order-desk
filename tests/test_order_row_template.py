"""Rendering checks for app/templates/_order_row.html.

Most of this codebase's tests exercise backend logic by calling web.py
functions directly (see tests/test_mail_queue_backend.py's pattern of
monkeypatching TemplateResponse to capture the context dict). Items 2/3/5 of
this change are pure template/markup changes with no new backend logic, so
there's nothing to unit-test at that layer — instead render the partial
directly through the app's real Jinja2Templates instance (web.templates),
which already has the `is_overdue`/`material_color_css_class` globals
registered, and assert on the resulting HTML.
"""

from datetime import datetime
from types import SimpleNamespace

import app.web as web

_TEMPLATE = web.templates.get_template("_order_row.html")


def _order(**overrides):
    base = dict(
        id=1,
        source="lab",
        sheet_tab="01.01.20",
        status="нове",
        material_color="пмма A2",
        kind="анатомія",
        quantity="3",
        job_code="2026-07-21_00016-007",
        job_code_folder_uri=None,
        job_code_folder_preview_token=None,
        sum3d_id="",
        export_folder_uri=None,
        export_folder_preview_token=None,
        technician_name="Іван",
        cam_comment=None,
        client_name=None,
        work_order_no="24122",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _render(order, **extra):
    return _TEMPLATE.render(order=order, statuses=["нове", "видано"], sync_error=None, **extra)


def test_cam_comment_is_editable_input_prefilled_when_set():
    html = _render(_order(cam_comment="покрити опаком, я сам закрию"))

    assert '<td class="comment-cell">' in html
    assert "cam-comment-text" in html
    # inline-editable textarea (grows to show long text) that saves to the sheet
    assert 'name="cam_comment"' in html
    assert 'hx-post="/orders/1/cam-comment"' in html
    assert "<textarea" in html
    # textarea holds its value as inner text, not a value attribute
    assert ">покрити опаком, я сам закрию</textarea>" in html


def test_cam_comment_column_is_empty_editable_input_when_no_comment():
    html = _render(_order(cam_comment=None))

    assert '<td class="comment-cell">' in html
    # empty but still editable — a blank textarea, not a dash
    assert 'name="cam_comment"' in html
    assert "></textarea>" in html


def test_export_folder_icon_is_a_clickable_anchor_not_a_span():
    """Regression guard for item 3: the export folder icon used to be a
    bare <span> with no href — clicking it did nothing. It must now be a
    real <a href="..."> like handout.html's already-working folder link."""
    order = _order(
        export_folder_uri="file:///C:/export/client/folder",
        export_folder_preview_token="preview-token",
    )

    html = _render(order)

    assert '<a href="file:///C:/export/client/folder" class="folder-link"' in html
    assert 'data-stl-preview-token="preview-token"' in html
    # The old broken markup must be gone, not just supplemented.
    assert '<span class="folder-link"' not in html


def test_export_folder_icon_absent_when_no_folder_resolved():
    html = _render(_order(export_folder_uri=None))

    assert "folder-link" not in html


def test_job_code_button_exposes_folder_uri_for_dblclick_handler():
    """Item 5: app/static/js/app.js listens for dblclick on
    [data-folder-uri] and navigates to it — the attribute must be present
    whenever a real folder was resolved, and absent otherwise (no-op)."""
    order = _order(
        job_code="2026-07-21_00016-007",
        job_code_folder_uri="file:///C:/export/tech/job",
    )

    html = _render(order)

    assert 'data-folder-uri="file:///C:/export/tech/job"' in html
    # Single-click copy-to-clipboard behavior (CLAUDE.md "level 1" design)
    # must be completely unchanged.
    assert 'data-copy="2026-07-21_00016-007"' in html


def test_job_code_button_has_no_folder_uri_attribute_when_unresolved():
    html = _render(_order(job_code_folder_uri=None))

    assert "data-folder-uri" not in html


def test_lab_row_shows_dash_for_empty_kind_job_tech():
    """A lab row keeps the «—» placeholder in the fields it simply hasn't filled
    yet — the operator needs to see the column is empty, not missing."""
    html = _render(_order(source="lab", kind=None, job_code=None, technician_name=None))
    # kind + job-code render a dash span; technician renders a bare dash
    assert html.count("—") >= 3


def test_client_row_leaves_kind_job_tech_blank_not_dash():
    """Client rows (email / вписаний клієнт) have no work-type, path or technician
    — those are lab-process fields. Their cells stay empty rather than «—», so the
    row doesn't read as 'data missing'."""
    html = _render(_order(
        source="sheet_client", kind=None, job_code=None, technician_name=None,
        client_name="Басараб",
    ))
    # No dash placeholder in kind / job-code / technician cells for a client row
    assert '<span class="text-muted">—</span>' not in html   # kind dash gone
    assert '<span class="mono">—</span>' not in html         # job-code dash gone
    assert '<td class="technician"></td>' in html            # technician left blank


# ── Смуга матеріалу на краю рядка ────────────────────────────────────────


def _with_material(name, **overrides):
    """Рядок із розпізнаним матеріалом каталогу."""
    return _order(material=SimpleNamespace(name=name), **overrides)


def test_row_carries_the_material_class_from_the_catalog():
    """Смуга і ключ маркування беруть клас з ОДНОГО джерела (material_badge →
    каталог). Якби рядок вигадував собі клас окремо, два сигнали розійшлись би
    на першому ж новому матеріалі."""
    for material, expected in (
        ("Титан", "mat-ti"),
        ("ПММА", "mat-pmma"),
        ("СЛМ", "mat-slm"),
        ("Віск", "mat-wax"),
        ("Цирконій", "mat-zr"),
    ):
        html = _render(_with_material(material))
        assert f'data-mat="{expected}"' in html, material


def test_unresolved_material_is_marked_unknown_never_guessed():
    """Матеріал не зіставлено — рядок каже «не знаю» (і смуги в CSS для цього
    стану немає). Вгадувати колір за текстом не можна: саме через це смуга не
    ходить через регулярку."""
    html = _render(_order(material=None, material_color="щось незрозуміле"))
    assert 'data-mat="mat-unknown"' in html


def test_row_without_any_material_text_gets_no_material_attribute():
    html = _render(_order(material=None, material_color=None))
    assert "data-mat=" not in html


def test_material_stripe_does_not_disturb_the_state_classes():
    """Сторож каналів: матеріал живе в атрибуті, стани — у класах. Титановий
    рядок, який ще й прострочений і змінений техніком, зберігає ОБИДВА сигнали."""
    html = _render(
        _with_material(
            "Титан",
            sheet_tab="01.01.20",
            sheet_changed_at=datetime(2026, 8, 29, 14, 5),
            sheet_changed_fields="колір",
            sum3d_id="",
        )
    )
    assert 'data-mat="mat-ti"' in html
    assert "queue-row-overdue" in html
    assert "queue-row-changed" in html
