"""«Спокійне оновлення» — де шкала вмикається і де НІ (01.10.26).

Вигляд і поведінку перевірено в браузері (Edge без вікна, затримка мережі):
клік по фільтру → область черги пригасає, шкала по центру, після заміни
зникає. Тут — сторожі розмітки й логіки, які легко зламати правкою поруч.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_JS = (ROOT / "app/static/js/app.js").read_text(encoding="utf-8")
LOADER_CSS = (ROOT / "app/static/css/loader.css").read_text(encoding="utf-8")


def _tpl(name: str) -> str:
    return (ROOT / "app/templates" / name).read_text(encoding="utf-8")


def test_opt_in_places():
    assert 'data-kload=".queue-panel"' in _tpl("_queue_sidebar.html")
    handout = _tpl("handout.html")
    assert handout.count('data-kload="#handout-list"') == 2  # стрілки/чіпи днів і список днів
    assert 'action="/sheets/sync" data-kload=".queue-panel"' in _tpl("queue.html")
    assert 'data-kload="self"' in _tpl("_sheet_write_failures.html")


def test_background_polls_never_show_the_scale():
    """Полл ходить по таймеру (`every …`) — шкала щопівхвилини тягнула б погляд."""
    start = APP_JS.index('addEventListener("htmx:beforeRequest"')
    block = APP_JS[start:start + 600]
    assert "/every\\s/.test(trigger)" in block
    assert block.index("every") < block.index("kloadStart")


def test_scale_belongs_to_its_request():
    """Чужий полл, що завершився посеред дії, не знімає шкалу: прив'язка до xhr."""
    assert "kloadByXhr" in APP_JS
    assert 'addEventListener("htmx:afterSettle", kloadStopFor)' in APP_JS
    assert 'addEventListener("htmx:afterSettle", kloadStopAll)' not in APP_JS
    # Два кліки підряд: знімає лише останній запит області (рецензія 01.10.26).
    assert "host._kloadLatest === xhr" in APP_JS
    # Запасний вихід — сам xhr: події htmx від відʼєднаного елемента (другий
    # клік по фільтру) до body не доходять, а hx-swap="none" afterSettle не дає.
    assert 'xhr.addEventListener("loadend"' in APP_JS


def test_dim_is_opacity_only_and_delayed():
    """Нічого не зсувається: лише прозорість; поява — із затримкою."""
    rule = LOADER_CSS[LOADER_CSS.index(".kload-host.is-loading > :not(.kload-veil)"):]
    rule = rule[:rule.index("}")]
    assert "opacity" in rule
    for forbidden in ("height", "width", "margin", "padding", "display", "transform"):
        assert forbidden not in rule
    assert "--kload-delay, 300ms" in LOADER_CSS
    assert "prefers-reduced-motion" in LOADER_CSS


def test_finished_download_row_is_not_kept_stale():
    """Пошта 01.10.26: hx-preserve htmx вирішує за НОВОЮ відповіддю — докачаний
    рядок (тепер із hx-preserve) лишав старий DOM із «завантаження…» до F5.
    Рядок, що качається, мічено; mail.js знімає hx-preserve з відповіді для
    таких рядків. Перевірено наживо (Edge + CDP): без цього рядок висить 40+ с."""
    row = _tpl("_mail_triage_list.html")
    assert "data-dl-pending" in row
    mail_js = (ROOT / "app/static/js/mail.js").read_text(encoding="utf-8")
    assert "unpreserveFinishedDownloads(event, target);" in mail_js
    assert 'fresh.removeAttribute("hx-preserve")' in mail_js


def test_colors_come_from_theme_tokens():
    import re

    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", LOADER_CSS), "кольори — лише var() токенів теми"
