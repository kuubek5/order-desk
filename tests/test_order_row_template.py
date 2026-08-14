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
