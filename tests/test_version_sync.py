"""Guards against app/__version__.py and installer/OrderDesk.iss drifting
apart — the installer filename and #define are hand-maintained, and there
is no automated way to have Inno Setup read the Python constant directly,
so this test is the safety net instead."""

from pathlib import Path
import re

from app.__version__ import VERSION

ISS_PATH = Path(__file__).resolve().parents[1] / "installer" / "OrderDesk.iss"


def test_iss_version_matches_python_version():
    content = ISS_PATH.read_text(encoding="utf-8")
    match = re.search(r'#define\s+MyAppVersion\s+"([^"]+)"', content)
    assert match is not None, "MyAppVersion define not found in OrderDesk.iss"
    assert match.group(1) == VERSION
