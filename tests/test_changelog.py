"""Changelog parsing for the «Про застосунок» screen.

CHANGELOG.md is the source of truth and ships with the build; the parser must
be tolerant (a broken line is skipped, never raised) so a typo in the changelog
can't take down the settings page.
"""

import app.web as web
from app.routers import deps
from app.changelog import parse_changelog, load_changelog

SAMPLE = """# Журнал змін

Преамбула, яку треба ігнорувати.

## [Незалежно від версії]

### Додано
- Новий екран **Стан системи**.
- Рядок, що переноситься
  на другий рядок у файлі.

### Виправлено
- Помилка доступу.

## [0.3.0] — 2026-08-17

### Додано
- Двосторонній запис.

## [0.0.1]

### Порожньо
"""


def test_parses_versions_newest_first_with_dates():
    releases = parse_changelog(SAMPLE)
    versions = [(r.version, r.date) for r in releases]
    assert versions == [
        ("Незалежно від версії", None),
        ("0.3.0", "2026-08-17"),
    ]  # the empty [0.0.1] shell is dropped


def test_unreleased_flag_is_dateless_section():
    releases = parse_changelog(SAMPLE)
    assert releases[0].is_unreleased is True
    assert releases[1].is_unreleased is False


def test_groups_and_items_captured():
    unreleased = parse_changelog(SAMPLE)[0]
    labels = {g.label: g.items for g in unreleased.groups}
    assert set(labels) == {"Додано", "Виправлено"}
    assert labels["Виправлено"] == ["Помилка доступу."]


def test_wrapped_continuation_line_is_folded_onto_item():
    unreleased = parse_changelog(SAMPLE)[0]
    added = next(g for g in unreleased.groups if g.label == "Додано").items
    assert added[0] == "Новий екран **Стан системи**."
    assert added[1] == "Рядок, що переноситься на другий рядок у файлі."


def test_empty_or_broken_input_never_raises():
    assert parse_changelog("") == []
    assert parse_changelog("суцільний текст без заголовків") == []


def test_real_changelog_file_loads_and_has_current_version():
    releases = load_changelog()
    assert releases, "CHANGELOG.md має парситись у щонайменше один реліз"
    versions = {r.version for r in releases}
    assert web.VERSION in versions, "поточна версія має бути в журналі змін"


def test_changelog_md_filter_bolds_and_escapes():
    # bold renders...
    assert "<strong>Стан</strong>" in str(deps.changelog_md("**Стан** системи"))
    # ...but injected HTML is escaped, not executed
    out = str(deps.changelog_md("<script>alert(1)</script> **ок**"))
    assert "<script>" not in out
    assert "&lt;script&gt;" in out
    assert "<strong>ок</strong>" in out


def test_whats_new_splits_bold_title_and_skips_unreleased(monkeypatch):
    """«Що нового» (власник 11.10.26): жирний початок пункту — заголовок картки,
    решта — опис; розділ без дати ще ніхто не встановив — його немає."""
    import app.changelog as changelog

    md = (
        "## [Незалежно від версії]\n### Додано\n- **Ще не вийшло.** Секрет.\n\n"
        "## [0.21.65] — 2026-10-12\n### Додано\n- **Лист летить у док.** Видно, куди\n"
        "  пішла робота, `код` без лапок.\n### Виправлено\n- Без жирного початку\n\n"
        "## [0.21.64] — 2026-10-11\n### Змінено\n- **Плитки прибрано**\n"
    )
    monkeypatch.setattr(changelog, "load_changelog", lambda: changelog.parse_changelog(md))
    out = changelog.whats_new_releases()
    assert [r["version"] for r in out] == ["0.21.65", "0.21.64"]
    first = out[0]["items"]
    assert first[0] == {"group": "Додано", "title": "Лист летить у док", "text": "Видно, куди пішла робота, код без лапок."}
    assert first[1] == {"group": "Виправлено", "title": "Без жирного початку", "text": ""}
    assert out[1]["items"][0]["title"] == "Плитки прибрано"
    assert len(changelog.whats_new_releases(limit=1)) == 1


def test_whats_new_json_is_safe_inside_the_script_tag():
    """JSON у <script type=application/json> — через |tojson: «</script>» у
    тексті журналу не має закрити тег."""
    tpl = deps.templates.env.from_string("{{ data|tojson }}")
    html = tpl.render(data=[{"title": "</script><b>x</b>"}])
    assert "</script>" not in html
