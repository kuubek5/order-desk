#!/usr/bin/env python
"""Підняти версію застосунку в УСІХ чотирьох місцях за один крок.

Навіщо: версія зашита руками в app/__version__.py, installer/KuubMill.iss і
.github/workflows/release.yml (6 разів). У минулих сесіях це робилось потрійним
`sed` по чотири рази — легко проґавити одне місце й отримати провал релізу.
Див. SESSIONS_DIAGNOSTICS.md, кластер 10.

Використання:
    python bump_version.py 0.3.1
    python bump_version.py 0.3.1 --check   # лише показати, що змінилось би

Після бампу:
    python -m pytest tests/test_version_sync.py -q   # має пройти
    git commit + git tag v0.3.1 + git push origin v0.3.1  # тригерить CI-реліз
"""

from __future__ import annotations

import datetime
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")

# The heading the changelog collects in-progress work under, between releases.
# Must match CHANGELOG.md and app/changelog.py's "dateless == unreleased" rule.
CHANGELOG_UNRELEASED = "## [Незалежно від версії]"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


class EmptyChangelogError(Exception):
    """Секцію «Незалежно від версії» нема чим штампувати."""


def _pending_is_empty(text: str, body_start: int) -> bool:
    """Чи порожня секція, що зараз стане релізною.

    Порожня = між її заголовком і наступним `## ` немає жодного непорожнього
    рядка. Саме так виглядав розділ 0.4.3, коли бамп проштампував порожнечу.
    """
    body = text[body_start:]
    next_heading = re.search(r"^## ", body, re.MULTILINE)
    if next_heading:
        body = body[: next_heading.start()]
    return not body.strip()


def _roll_changelog(new: str, check: bool) -> None:
    """Stamp the pending «Незалежно від версії» section with the new version +
    today's date, and open a fresh empty pending section above it.

    Makes the release the moment the changelog is versioned: work accumulates
    under the dateless heading during development, and this turns it into
    `## [X.Y.Z] — YYYY-MM-DD` in one step, so the shipped build's «Про
    застосунок» shows exactly what changed. Absent/already-rolled section is a
    warning, not a failure — a release with nothing new to note is legitimate.

    Порожня ж секція — це failure, і саме її ловить `_pending_is_empty`. На
    0.4.3 бамп мовчки проштампував порожнечу: реліз вийшов, а екран «Що
    нового» в ньому порожній, і побачилось це лише коли впав тест журналу
    (`load_changelog` порожні секції не віддає). Дешевше спинити тут.
    """
    path = ROOT / "CHANGELOG.md"
    if not path.exists():
        print("  ! CHANGELOG.md не знайдено — пропускаю")
        return
    text = _read(path)
    # Match the heading ONLY as a real line-start heading — the preamble
    # documents "## [Незалежно від версії]" inside backticks, and a plain
    # substring replace hit THAT first, corrupting the intro. Anchor to line
    # start (MULTILINE) so only the actual heading is rolled.
    heading_re = re.compile("^" + re.escape(CHANGELOG_UNRELEASED) + r"\s*$", re.MULTILINE)
    match = heading_re.search(text)
    if not match:
        print("  ! CHANGELOG.md: секції «Незалежно від версії» немає — пропускаю")
        return

    if _pending_is_empty(text, match.end()):
        print(
            "  ! CHANGELOG.md: секція «Незалежно від версії» порожня.\n"
            "    Спершу опиши, що змінилось для оператора, інакше в релізі\n"
            "    буде порожній екран «Що нового» (так вийшло з 0.4.3)."
        )
        raise EmptyChangelogError

    today = datetime.date.today().isoformat()
    stamped = f"{CHANGELOG_UNRELEASED}\n\n## [{new}] — {today}"
    print(f"  CHANGELOG.md: «Незалежно від версії» -> [{new}] — {today}")
    if not check:
        # Replace only the first heading occurrence (the top, pending one).
        path.write_text(heading_re.sub(stamped, text, count=1), encoding="utf-8")


def bump(new: str, check: bool) -> int:
    if not VERSION_RE.match(new):
        print(f"Невалідна версія: {new!r} (очікую X.Y.Z)")
        return 2

    version_py = ROOT / "app" / "__version__.py"
    iss = ROOT / "installer" / "KuubMill.iss"
    workflow = ROOT / ".github" / "workflows" / "release.yml"

    old_match = re.search(r'VERSION\s*=\s*"([^"]+)"', _read(version_py))
    if not old_match:
        print("Не знайшов VERSION у app/__version__.py")
        return 1
    old = old_match.group(1)
    if old == new:
        print(f"Версія вже {new} — нічого міняти.")
        return 0

    edits = {
        version_py: (f'VERSION = "{old}"', f'VERSION = "{new}"'),
        iss: (f'#define MyAppVersion "{old}"', f'#define MyAppVersion "{new}"'),
    }

    print(f"{old}  ->  {new}")
    for path, (frm, to) in edits.items():
        text = _read(path)
        if frm not in text:
            print(f"  ! {path.relative_to(ROOT)}: не знайдено {frm!r}")
            return 1
        print(f"  {path.relative_to(ROOT)}: 1 заміна")
        if not check:
            path.write_text(text.replace(frm, to), encoding="utf-8")

    # Workflow: усі згадки KuubMill-Setup-<old> -> <new> (їх 6)
    wtext = _read(workflow)
    count = wtext.count(f"KuubMill-Setup-{old}")
    print(f"  .github/workflows/release.yml: {count} замін")
    if not check:
        workflow.write_text(
            wtext.replace(f"KuubMill-Setup-{old}", f"KuubMill-Setup-{new}"), encoding="utf-8"
        )

    try:
        _roll_changelog(new, check)
    except EmptyChangelogError:
        # Версію у файлах уже змінено — але без запису журналу реліз усе одно
        # неповноцінний, тому виходимо з помилкою, а не «майже готово».
        return 1

    if check:
        print("\n(--check: нічого не записано)")
    else:
        print("\nГотово. Далі: pytest test_version_sync -> commit -> tag v" + new + " -> push")
    return 0


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--check"]
    if len(args) != 1:
        print(__doc__)
        sys.exit(2)
    sys.exit(bump(args[0], check="--check" in sys.argv))
