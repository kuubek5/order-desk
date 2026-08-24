"""bump_version rolls the changelog: the pending «Незалежно від версії» section
becomes a dated version, and a fresh empty pending section opens above it.

This is what makes «Про застосунок» show the right notes for the shipped build —
work collected during development gets versioned at release in one step.
"""

import datetime

import bump_version


SAMPLE = """# Журнал змін

## [Незалежно від версії]

### Додано
- Нова фіча.

## [0.3.0] — 2026-08-17

### Додано
- Стара фіча.
"""


def _roll(tmp_path, monkeypatch, text, new="0.4.0"):
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(text, encoding="utf-8")
    monkeypatch.setattr(bump_version, "ROOT", tmp_path)
    bump_version._roll_changelog(new, check=False)
    return changelog.read_text(encoding="utf-8")


def test_pending_section_is_stamped_with_version_and_today(tmp_path, monkeypatch):
    out = _roll(tmp_path, monkeypatch, SAMPLE)
    today = datetime.date.today().isoformat()
    assert f"## [0.4.0] — {today}" in out
    # The rolled version keeps the work that was pending.
    idx = out.index("## [0.4.0]")
    assert "Нова фіча." in out[idx:]


def test_a_fresh_empty_pending_section_is_opened_above(tmp_path, monkeypatch):
    out = _roll(tmp_path, monkeypatch, SAMPLE)
    # Pending heading still present...
    assert "## [Незалежно від версії]" in out
    # ...and it now sits ABOVE the freshly stamped version.
    assert out.index("## [Незалежно від версії]") < out.index("## [0.4.0]")


def test_rolled_changelog_parses_with_the_app_parser(tmp_path, monkeypatch):
    out = _roll(tmp_path, monkeypatch, SAMPLE)
    from app.changelog import parse_changelog

    releases = parse_changelog(out)
    versions = [r.version for r in releases]
    # The empty pending shell is dropped by the parser; 0.4.0 and 0.3.0 remain.
    assert "0.4.0" in versions
    assert "0.3.0" in versions
    rolled = next(r for r in releases if r.version == "0.4.0")
    assert rolled.date is not None
    assert not rolled.is_unreleased


def test_check_mode_writes_nothing(tmp_path, monkeypatch):
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(SAMPLE, encoding="utf-8")
    monkeypatch.setattr(bump_version, "ROOT", tmp_path)
    bump_version._roll_changelog("0.4.0", check=True)
    assert changelog.read_text(encoding="utf-8") == SAMPLE


def test_missing_pending_section_is_a_warning_not_a_crash(tmp_path, monkeypatch):
    out = _roll(tmp_path, monkeypatch, "# Журнал змін\n\n## [0.3.0] — 2026-08-17\n")
    # Unchanged, no exception.
    assert "## [0.4.0]" not in out
