"""Parse the repo's CHANGELOG.md into structured releases for the settings
"Про застосунок" screen.

The markdown file is the single source of truth (Keep a Changelog format); it
ships with the build (see OrderDesk.spec) so the changelog renders offline in
the installed app, with no GitHub round-trip. Parsing is deliberately tolerant:
a malformed heading is skipped rather than raising, because a broken changelog
must never take down the settings page.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache

from app.runtime import resource_path

# "## [0.3.0] — 2026-08-17"  or  "## [Незалежно від версії]"
# The date is optional and may be joined by an em dash, en dash or hyphen.
_VERSION_RE = re.compile(
    r"^##\s+\[(?P<version>[^\]]+)\]\s*(?:[—–-]\s*(?P<date>.+))?\s*$"
)
# "### Додано" / "### Виправлено" / "### Змінено"
_GROUP_RE = re.compile(r"^###\s+(?P<label>.+?)\s*$")
# "- item" / "* item"
_ITEM_RE = re.compile(r"^[-*]\s+(?P<text>.+?)\s*$")


@dataclass
class ChangelogGroup:
    label: str
    items: list[str] = field(default_factory=list)


@dataclass
class ChangelogRelease:
    version: str
    date: str | None
    groups: list[ChangelogGroup] = field(default_factory=list)

    @property
    def is_unreleased(self) -> bool:
        # Anything without a date is treated as the pending / "next" section.
        return self.date is None


def parse_changelog(text: str) -> list[ChangelogRelease]:
    """Parse Keep-a-Changelog markdown into releases, newest first (file order).

    Continuation lines of a wrapped list item (indented, no bullet) are folded
    back onto the item so a soft-wrapped entry reads as one sentence.
    """
    releases: list[ChangelogRelease] = []
    release: ChangelogRelease | None = None
    group: ChangelogGroup | None = None

    for raw in text.splitlines():
        line = raw.rstrip()

        version_match = _VERSION_RE.match(line)
        if version_match:
            release = ChangelogRelease(
                version=version_match.group("version").strip(),
                date=(version_match.group("date") or "").strip() or None,
            )
            releases.append(release)
            group = None
            continue

        if release is None:
            continue  # preamble before the first version heading

        group_match = _GROUP_RE.match(line)
        if group_match:
            group = ChangelogGroup(label=group_match.group("label").strip())
            release.groups.append(group)
            continue

        item_match = _ITEM_RE.match(line)
        if item_match and group is not None:
            group.items.append(item_match.group("text").strip())
            continue

        # Indented continuation of the previous bullet → glue onto it.
        if group and group.items and line.strip() and (raw.startswith(" ") or raw.startswith("\t")):
            group.items[-1] = group.items[-1] + " " + line.strip()

    # Drop empty shells (a version heading with no groups/items yet).
    return [r for r in releases if any(g.items for g in r.groups)]


@lru_cache(maxsize=1)
def _load_changelog_cached(mtime: float) -> list[ChangelogRelease]:
    # Keyed by mtime so an edit in dev is picked up without a restart, while a
    # frozen build (stable mtime) parses the file exactly once.
    path = resource_path("CHANGELOG.md")
    return parse_changelog(path.read_text(encoding="utf-8"))


def load_changelog() -> list[ChangelogRelease]:
    """Structured releases from CHANGELOG.md, or [] if the file is missing."""
    path = resource_path("CHANGELOG.md")
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return []
    return _load_changelog_cached(mtime)
