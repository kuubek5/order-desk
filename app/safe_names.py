"""Windows-safe name rules shared by attachment filenames (app/mail_reader.py)
and export folder names (app/mail_export.py).

Both places solve the same problem — "turn text a client typed into a name this
filesystem will actually accept" — and used to do it with two independent regex
sets. Reserved DEVICE names live here so the rule cannot drift apart again: a
letter with a `con.stl` attachment would otherwise fail `write_bytes` forever
and keep the email stuck in `attachments_status="pending"`, re-fetched on every
sync with no visible reason.
"""

import re

# Windows refuses these names with or without an extension, case-insensitively:
# "CON", "con.stl" and "Con.tar.gz" are all rejected by the filesystem.
RESERVED_DEVICE_NAMES = (
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)


def is_reserved_device_name(name: str) -> bool:
    """True when Windows would reject `name` because its stem is a device name."""
    stem = re.split(r"[.]", name.strip(), maxsplit=1)[0]
    return stem.strip().upper() in RESERVED_DEVICE_NAMES


def avoid_reserved_device_name(name: str, prefix: str = "_") -> str:
    """Prefix a reserved device name so it can be written to disk.

    Keeps the original text visible to the operator (`_con.stl`, not `file`) —
    the point is a name that saves, not an anonymous one.
    """
    return f"{prefix}{name}" if is_reserved_device_name(name) else name
