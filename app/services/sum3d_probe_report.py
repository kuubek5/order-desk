"""Звіти набору розвідки Sum3D (`tools/sum3d-probe`). ЛИШЕ ЧИТАННЯ готових `report.json`.

Набір (PowerShell) власник запускає РУКАМИ на ПК із ліцензованою Sum3D: він питає в
`Sum3dDll_x64.dll` список верстатів, стратегію й вільне місце по КОПІЇ одного проєкту і кладе
звіт у `C:\\sum3d_probe\\<дата_час>\\`. Цей модуль лише читає ті звіти, щоб їх було видно
через MCP (`kmill_sum3d_probe`) без пересилання файлів.

Інваріанти (сторож — `tests/test_sum3d_probe_kit.py`):

* KuubMill набір НЕ запускає: тут немає запуску процесів, і ніде в `app/` його немає;
* нічого не пишемо — ні в теку звітів, ні в теку Sum3D;
* літери поза латиницею у значеннях зі звіту затираємо (назви тек можуть нести імена).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from app.runtime import resource_path

KIT_RELATIVE = "tools/sum3d-probe"
REPORTS_ROOT = Path(r"C:\sum3d_probe")
_REPORT_DIR = re.compile(r"^\d{8}_\d{6}$")
_MAX_REPORT_BYTES = 2_000_000
_OTHER_CHANGES_SHOWN = 20

_STOPPED = {
    "dry run": "суха перевірка, DLL не завантажувалась",
    "sum3d running": "відкриті вікна Sum3D, а -AllowRunning не дано",
    "sumdir ambiguous": "кілька тек Sum3D, теку не вказано",
    "no dll": "у теці немає Sum3dDll_x64.dll",
    "not x64": "DLL не 64-бітна",
    "no cam": "файл .cam не знайдено",
    "missing exports": "у DLL бракує потрібних функцій (інша версія)",
    "no free seat": "вільних місць ліцензії немає",
    "last seat": "вільне лише останнє місце ліцензії",
    "place input missing": "рівень 2: не вказано STL, диск, верстат чи матеріал",
    "place copy failed": "рівень 2: STL чи диск не вдалось скопіювати",
    "place machine not found": "рівень 2: верстата немає у списку Sum3D або назва неоднозначна",
}


def kit_dir() -> Path:
    return resource_path(KIT_RELATIVE)


def reports_root() -> Path:
    return REPORTS_ROOT


def _scrub(value: Any) -> str:
    """Рядок зі звіту: літери поза латиницею затерто."""
    return "".join(ch if ord(ch) < 128 else "·" for ch in str(value))


def _as_list(value: Any) -> list[Any]:
    # PowerShell згортає масив з одного елемента в сам елемент.
    if value is None:
        return []
    return list(value) if isinstance(value, list) else [value]


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _scrub_list(value: Any, limit: int = 60) -> list[str]:
    return [_scrub(item) for item in _as_list(value)[:limit]]


def _step(raw: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"крок": _scrub(raw.get("step", "")), "секунд": raw.get("seconds")}
    if raw.get("timeout"):
        out["тайм_аут"] = True
    if "exit_code" in raw:
        out["код_виходу"] = raw.get("exit_code")
    result = raw.get("result")
    if isinstance(result, dict):
        out["ok"] = bool(result.get("ok"))
        for source, target in (
            ("init_code", "код_initialize"),
            ("code", "код"),
            ("value", "значення"),
            ("count", "чисел_контурів"),
            ("first_values", "перші_числа"),
            ("obj_size", "розмір_obj"),
            ("cam_size", "розмір_cam"),
        ):
            if source in result:
                out[target] = result[source]
        for source, target in (("machines_raw", "верстати"), ("equipment_raw", "обладнання"),
                               ("strategy", "стратегія"), ("error", "помилка")):
            if result.get(source) not in (None, ""):
                out[target] = _scrub(result[source])[:2000]
    else:
        out["ok"] = False
        if raw.get("raw"):
            out["вивід"] = _scrub(raw["raw"])[:500]
    if raw.get("stderr"):
        out["stderr"] = _scrub(raw["stderr"])[:500]
    return out


def _candidate(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "тека": _scrub(raw.get("dir", "")),
        "є_camwinc": bool(raw.get("has_camwinc")),
        "є_dll_x64": bool(raw.get("has_dll_x64")),
        "версія_dll": raw.get("dll_version"),
        "x64": raw.get("x64"),
        "функцій": raw.get("export_count"),
        "бракує": _scrub_list(raw.get("missing_exports")),
    }


def _summary(folder: str, data: dict[str, Any]) -> dict[str, Any]:
    stopped = data.get("stopped")
    dll = _dict(data.get("dll"))
    lic = _dict(data.get("license"))
    reg = _dict(data.get("registry"))
    snap = _dict(data.get("snapshot"))
    other = _as_list(data.get("sum_dir_changes_other"))
    out: dict[str, Any] = {
        "тека_звіту": folder,
        "коли": data.get("started"),
        "набір_v": data.get("kit_version"),
        "суха_перевірка": bool(data.get("dry_run")),
        "зупинено": _STOPPED.get(str(stopped), _scrub(stopped)) if stopped else None,
        "помилка": _scrub(data["error"])[:500] if data.get("error") else None,
        "тека_sum3d": _scrub(data["sum_dir"]) if data.get("sum_dir") else None,
        "знайдені_теки": [_candidate(c) for c in _as_list(data.get("candidates")) if isinstance(c, dict)],
        "dll": {
            "версія": dll.get("version"),
            "змінено": dll.get("modified"),
            "x64": dll.get("x64"),
            "функцій": dll.get("export_count"),
            "бракує": _scrub_list(dll.get("missing_exports")),
        },
        "camwinc": data.get("camwinc_version"),
        "ліцензія": {"ліміт_сесій": lic.get("max_session"), "відкрито_camwinc": lic.get("camwinc_running")},
        "відкрито_sum3d_до": _scrub_list(data.get("running_before")),
        "кроки": [_step(s) for s in _as_list(data.get("steps")) if isinstance(s, dict)],
    }
    place = _dict(data.get("place"))
    if place:
        # Рівень 2 (набір v4): STL на копію диска. Назви STL у звіті немає — лише розмір.
        out["постановка"] = {
            "диск": _scrub(place.get("blk", "")),
            "верстат": _scrub(place.get("machine", "")),
            "матеріал": _scrub(place.get("material", "")),
            "матеріал_спрацював": _scrub(place.get("material_used", "")),
            "ліцензія_не_пускає": bool(place.get("license_denied")),
            "обладнання": _scrub(place.get("equipment", "")),
            "усадка": place.get("shrink"),
            "вид_роботи": place.get("object_type"),
            "зсув_x_мм": place.get("shift_x"),
            "розмір_stl": place.get("stl_size"),
        }
    if not data.get("dry_run"):
        out.update(
            {
                "робоча_тека_в_sum3d": bool(data.get("cwd_in_sum_dir")),
                "зміни_налаштувань": _scrub_list(data.get("sum_dir_changes_config")),
                "інші_зміни": {"усього": len(other), "перші": _scrub_list(other, _OTHER_CHANGES_SHOWN)},
                "реєстр": {
                    "гілки": _scrub_list(reg.get("keys_found")),
                    "змінені_розділи": _scrub_list(reg.get("changed_sections"), 30),
                },
                "нові_процеси": _scrub_list(data.get("new_processes")),
                "sum3d_закрились": _as_list(data.get("sum3d_closed_during_probe")),
                "sum3d_відкрились": _as_list(data.get("sum3d_started_during_probe")),
                "знімок_неповний": bool(snap.get("truncated_before") or snap.get("truncated_after")),
            }
        )
    return out


def _read(path: Path) -> dict[str, Any]:
    if path.stat().st_size > _MAX_REPORT_BYTES:
        raise ValueError("звіт завеликий")
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("звіт не є об'єктом")
    return data


def report(limit: int = 3) -> dict[str, Any]:
    """Останні звіти проби (новіші вище) і де лежить сам набір."""
    kit = kit_dir()
    kit_files = sorted(p.name for p in kit.iterdir() if p.is_file()) if kit.is_dir() else []
    root = reports_root()
    folders: list[Path] = []
    if root.is_dir():
        folders = sorted(
            (p for p in root.iterdir() if p.is_dir() and _REPORT_DIR.match(p.name)),
            key=lambda p: p.name,
            reverse=True,
        )
    reports: list[dict[str, Any]] = []
    for folder in folders[: max(1, limit)]:
        path = folder / "report.json"
        try:
            reports.append(_summary(folder.name, _read(path)))
        except (OSError, ValueError) as exc:
            reports.append({"тека_звіту": folder.name, "не_читається": type(exc).__name__})
    return {
        "набір": {"тека": str(kit), "на_місці": bool(kit_files), "файли": kit_files},
        "тека_звітів": str(root),
        "звітів_усього": len(folders),
        "звіти": reports,
        "як_запустити": (
            "Лише вручну: 1_dry_run.cmd (суха перевірка), далі перетягнути .cam на 2_probe.cmd. "
            "Рівень 2 (набір v4): перетягнути копію STL на 4_place.cmd — STL на копію диска. "
            "KuubMill набір не запускає."
        ),
    }
