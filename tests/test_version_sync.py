"""Guards against app/__version__.py and installer/KuubMill.iss drifting
apart — the installer filename and #define are hand-maintained, and there
is no automated way to have Inno Setup read the Python constant directly,
so this test is the safety net instead."""

from pathlib import Path
import re

from app.__version__ import VERSION

ISS_PATH = Path(__file__).resolve().parents[1] / "installer" / "KuubMill.iss"


def test_iss_version_matches_python_version():
    content = ISS_PATH.read_text(encoding="utf-8")
    match = re.search(r'#define\s+MyAppVersion\s+"([^"]+)"', content)
    assert match is not None, "MyAppVersion define not found in KuubMill.iss"
    assert match.group(1) == VERSION


WORKFLOW_PATH = (
    Path(__file__).resolve().parents[1] / ".github" / "workflows" / "release.yml"
)


def test_release_workflow_builds_the_installer_name_from_the_tag():
    """T.1: шість літералів «KuubMill-Setup-<версія>.exe» жили в release.yml і
    мовчки застарівали при кожному bump — реліз падав уже ПІСЛЯ збірки, на
    кроці «Verify installer exists». Тепер імʼя будується з тегу, і цей
    сторож не дає літералу повернутись."""
    content = WORKFLOW_PATH.read_text(encoding="utf-8")

    hardcoded = re.findall(r"KuubMill-Setup-\d[\d.]*\.exe", content)
    assert not hardcoded, (
        "У release.yml знову вписана версія в імені інсталятора: "
        f"{sorted(set(hardcoded))}. Беріть імʼя з ${{{{ steps.ver.outputs.installer }}}}."
    )
    assert "steps.ver.outputs.installer" in content
