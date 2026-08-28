"""Сторож фронтенду — страхувальна сітка для розбиття app.js (Крок 5).

Бекенд розбивався під 1168 тестами; у фронтенду тестів немає взагалі, а
ламається він тихіше за бекенд: файл із синтаксичною помилкою браузер просто
не виконує — сторінка малюється, кнопки мовчать, у консоль ніхто не дивиться.

Три речі, які ловить цей файл (рівно ті, якими ламається розбиття):

1. **Синтаксис.** Кожен наш `.js` має парситись (`node --check`).
2. **Порядок і склад завантаження.** base.html вантажить скрипти як КЛАСИЧНІ
   (не модулі) з `defer` — вони ділять один глобальний простір і виконуються
   в порядку тегів. Загубився тег або переїхав вище за свою залежність —
   екран мовчить. Тому список звіряється зі знімком.
3. **Подвійні оголошення.** Найпідступніше при розбитті: `const X` у двох
   файлах — це SyntaxError на ДРУГОМУ файлі, і він не виконується цілком.
   Ловиться склеюванням усіх глобальних скриптів в один і `node --check`.

Коли скрипт додають/прибирають СВІДОМО — оновити BASE_HTML_SCRIPTS нижче.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

TEMPLATES_DIR = Path(__file__).parent.parent / "app" / "templates"
STATIC_JS = Path(__file__).parent.parent / "app" / "static" / "js"
BASE_HTML = Path(__file__).parent.parent / "app" / "templates" / "base.html"

# Сторонні бібліотеки — не наш код, не наша відповідальність за стиль; але
# синтаксис перевіряємо й у них, бо зіпсований vendored-файл ламає сторінку
# так само.
VENDORED = {"three-0.128.0.min.js", "STLLoader-0.128.0.js", "htmx-1.9.10.min.js"}

# Скрипти, які base.html вантажить на КОЖНУ сторінку, у порядку тегів.
# Порядок значущий: класичні скрипти з defer виконуються саме так, і пізніший
# бачить те, що оголосив попередній.
BASE_HTML_SCRIPTS = [
    "/static/js/app.js",
    "/static/js/queue.js",
    "/static/js/mail.js",
    "/static/js/handout.js",
    "/static/js/clients.js",
    "/static/js/settings.js",
    "/static/js/three-0.128.0.min.js",
    "/static/js/STLLoader-0.128.0.js",
    "/static/js/stl-preview.js",
    "/static/js/stl-gallery.js",
    "/static/js/htmx-1.9.10.min.js",
]

# З них — наші, що ділять один глобальний простір. Саме їх склеюємо, щоб
# зловити подвійне оголошення після розбиття.
OUR_GLOBAL_SCRIPTS = [s for s in BASE_HTML_SCRIPTS if Path(s).name not in VENDORED]


def _node() -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node не знайдено — перевірку JS пропущено")
    return node


def _check_syntax(node: str, path: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [node, "--check", str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _script_srcs() -> list[str]:
    """Шляхи скриптів з base.html, у порядку тегів, без ?v= кеш-бастера."""
    html = BASE_HTML.read_text(encoding="utf-8")
    head = html.split("</head>", 1)[0]
    srcs = re.findall(r'<script\s+src="([^"?]+)', head)
    return srcs


def test_every_javascript_file_parses():
    """Будь-який .js у static/js має бути синтаксично валідним."""
    node = _node()
    broken = []
    for path in sorted(STATIC_JS.glob("*.js")):
        result = _check_syntax(node, path)
        if result.returncode != 0:
            broken.append(f"{path.name}: {result.stderr.strip().splitlines()[0]}")
    assert not broken, "JS не парситься:\n" + "\n".join(broken)


def test_base_html_loads_the_expected_scripts_in_order():
    """Склад і ПОРЯДОК скриптів на кожній сторінці — зі знімка.

    Загублений тег після розбиття app.js не впаде: сторінка намалюється, а
    частина кнопок просто перестане реагувати."""
    assert _script_srcs() == BASE_HTML_SCRIPTS, (
        "Список скриптів у base.html змінився.\n"
        f"  зараз:  {_script_srcs()}\n"
        f"  знімок: {BASE_HTML_SCRIPTS}\n"
        "Якщо зміна свідома — онови BASE_HTML_SCRIPTS у цьому файлі."
    )


def test_global_scripts_have_no_duplicate_top_level_declarations(tmp_path):
    """Наші глобальні скрипти не повинні оголошувати одне ім'я двічі.

    Класичні скрипти ділять глобальний лексичний простір: `const X` у двох
    файлах — SyntaxError на другому, і він не виконується ЦІЛКОМ. Склеювання
    в один файл відтворює саме цю колізію."""
    node = _node()
    parts = []
    for src in OUR_GLOBAL_SCRIPTS:
        path = STATIC_JS / Path(src).name
        assert path.exists(), f"base.html вантажить {src}, а файлу немає"
        parts.append(f"// ── {path.name} ──\n{path.read_text(encoding='utf-8')}")
    merged = tmp_path / "merged.js"
    merged.write_text("\n".join(parts), encoding="utf-8")

    result = _check_syntax(node, merged)
    assert result.returncode == 0, (
        "Склеєні глобальні скрипти не парсяться — найімовірніше одне ім'я "
        "оголошено у двох файлах:\n" + result.stderr.strip()
    )


# Розділи екрана «Налаштування», у порядку показу. Кожен — окремий партіал
# (Крок 5). Порядок тут і є порядком на екрані, тож він частина поведінки.
SETTINGS_SECTIONS = [
    "_settings_state.html",
    "_settings_notifications.html",
    "_settings_google.html",
    "_settings_operators.html",
    "_settings_backup.html",
    "_settings_imap.html",
    "_settings_paths.html",
    "_settings_mail_download.html",
    "_settings_mail_filters.html",
    "_settings_about.html",
]


def test_every_template_parses():
    """Кожен шаблон має бути валідним Jinja.

    Include резолвиться в РАНТАЙМІ, тому розбиття екрана на партіали не
    перевіряється відкриттям головного шаблону: помилка в партіалі спливе
    лише тоді, коли оператор відкриє сторінку. Тому парсимо всі окремо."""
    from jinja2 import TemplateSyntaxError

    from app.routers.deps import templates

    broken = []
    for path in sorted(TEMPLATES_DIR.glob('*.html')):
        try:
            templates.env.parse(path.read_text(encoding='utf-8'), filename=path.name)
        except TemplateSyntaxError as exc:
            broken.append(f'{path.name}:{exc.lineno}: {exc.message}')
    assert not broken, 'Шаблони не парсяться:' + chr(10) + chr(10).join(broken)


def test_settings_screen_includes_its_sections_in_order():
    """Склад і порядок розділів налаштувань — зі знімка.

    Загублений include не впаде: сторінка відкриється, просто без цілого
    розділу (напр. без IMAP), і помітять це не одразу."""
    html = (TEMPLATES_DIR / 'settings.html').read_text(encoding='utf-8')
    found = re.findall(r'\{%\s*include\s+"(_settings_[^"]+)"', html)
    assert found == SETTINGS_SECTIONS, (
        'Набір розділів у settings.html змінився.' + chr(10)
        + f'  зараз:  {found}' + chr(10)
        + f'  знімок: {SETTINGS_SECTIONS}' + chr(10)
        + 'Якщо зміна свідома — онови SETTINGS_SECTIONS у цьому файлі.'
    )
