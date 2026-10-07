"""`KuubMill.exe --list-snapshots` і `--restore-snapshot [шлях|latest]`.

Прод зібрано без консолі, а Python на машині цеху нема — тому інструмент
відновлення живе в самому exe, а розмову з людиною веде вікно Windows
(`say`/`ask` підміняються в тестах). Нічого з вебу чи шифрування не запускається:
дивись `app/snapshot_tools.py`.
"""
from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

from app.snapshot_tools import (
    SnapshotError,
    app_is_running,
    database_state,
    discover_snapshots,
    newest_valid,
    restore_from_snapshot,
)

MAX_LISTED = 12


def _extra_dirs(from_dirs: list[str] | None) -> list[Path]:
    return [Path(d) for d in (from_dirs or []) if d]


def run(
    *,
    list_only: bool,
    target: str | None,
    data_dir: Path,
    db_file: Path,
    from_dirs: list[str] | None = None,
    assume_yes: bool = False,
    say: Callable[[str], None],
    ask: Callable[[str], bool],
    running: Callable[[], bool] | None = None,
) -> int:
    """0 — зроблено/показано, 1 — не вдалося або людина відмовилась."""
    snapshots = discover_snapshots(data_dir, _extra_dirs(from_dirs))
    if list_only or not snapshots:
        if not snapshots:
            say("Знімків бази не знайдено.\n\nПеревірте теку %s\\backups або вкажіть теку копій: "
                "--from <тека>." % data_dir)
            return 1
        lines = [s.label() for s in snapshots[:MAX_LISTED]]
        more = f"\n… і ще {len(snapshots) - MAX_LISTED}" if len(snapshots) > MAX_LISTED else ""
        say("Знімки бази (найновіші зверху):\n\n" + "\n".join(lines) + more)
        return 0

    if not target or target == "latest":
        chosen = newest_valid(snapshots)
        if chosen is None:
            say("Жоден знімок не пройшов перевірку цілісності. Нічого не змінено.")
            return 1
    else:
        wanted = Path(target)
        chosen = next((s for s in snapshots if s.path.resolve() == wanted.resolve()), None)
        if chosen is None:
            from app.snapshot_tools import inspect_snapshot

            chosen = inspect_snapshot(wanted)
        if not chosen.ok:
            say(f"Знімок непридатний: {chosen.detail}. Нічого не змінено.")
            return 1

    if not assume_yes and not ask(
        f"Відновити базу зі знімка?\n\n{chosen.label()}\n\n"
        "Поточну базу НЕ буде видалено: вона збережеться поруч (*.before-restore-…).\n"
        "Дані, внесені після цього знімка, у відновленій базі будуть відсутні."
    ):
        return 1
    try:
        report = restore_from_snapshot(db_file, chosen.path, running=running)
    except SnapshotError as exc:
        say(str(exc))
        return 1
    say(
        f"Готово. Відновлено {report['orders']} робіт.\n\n"
        f"Стару базу збережено: {report['kept_aside'][0] if report['kept_aside'] else '(її не було)'}\n\n"
        "Тепер запустіть KuubMill як звичайно: він сам доведе базу до поточної версії."
    )
    return 0


def guard_database(
    *,
    db_file: Path,
    data_dir: Path,
    say: Callable[[str], None],
    ask: Callable[[str], bool],
) -> str:
    """Сторож бази на старті: `ok` | `restored` | `declined`.

    Рішення власника 07.10.26 (DR-навчання): база зникла, порожня чи зіпсована
    при наявних знімках → НЕ створювати мовчки порожню й не стартувати на биті,
    а спитати людину. «Так» відновлює найновіший придатний знімок (стару базу
    лишає поруч), «Ні» — вихід без змін.

    Свіжа інсталяція (бази нема І знімків нема) — звичайний старт. Стан
    `unknown` (база заблокована тощо) нічого не відновлює, ніколи.
    """
    state, detail = database_state(db_file)
    if state in ("ok", "unknown"):
        return "ok"
    snap = newest_valid(discover_snapshots(data_dir))
    if snap is None:
        if state == "missing":
            return "ok"  # перша інсталяція: бази й знімків ще нема
        say(
            "База даних KuubMill пошкоджена, а придатного знімка в "
            f"{data_dir}\\backups не знайдено.\n\nЗапуск зупинено, дані не змінено.\n"
            "Якщо копії лежать на іншому диску: KuubMill.exe --restore-snapshot latest --from <тека>.\n\n"
            f"Деталі: {detail}"
        )
        return "declined"
    what = "не знайдена (або порожня)" if state == "missing" else "пошкоджена"
    if not ask(
        f"База даних KuubMill {what}.\n\n"
        f"Знайдено знімок:\n{snap.label()}\n\n"
        "Відновити базу з нього?\n\n"
        "• Поточну базу НЕ буде видалено — вона збережеться поруч (*.before-restore-…).\n"
        "• Дані, внесені після цього знімка, у відновленій базі будуть відсутні.\n"
        "• «Ні» — KuubMill не запуститься, нічого не зміниться."
    ):
        return "declined"
    try:
        restore_from_snapshot(db_file, snap.path, running=lambda: False)
    except SnapshotError as exc:
        say(str(exc))
        return "declined"
    return "restored"


def windows_say(text: str) -> None:
    """Вікно з повідомленням (прод без консолі). Поза Windows/в тестах — у stdout."""
    if os.name != "nt" or os.environ.get("KUUBMILL_NONINTERACTIVE"):
        print(text)
        return
    import ctypes

    ctypes.windll.user32.MessageBoxW(0, text, "KuubMill — відновлення", 0x40 | 0x10000 | 0x40000)


def windows_ask(text: str) -> bool:
    if os.name != "nt" or os.environ.get("KUUBMILL_NONINTERACTIVE"):
        return False  # без людини відновлення мовчки не робимо: потрібен --yes
    import ctypes

    # MB_YESNO | MB_ICONWARNING | MB_DEFBUTTON2 | MB_SETFOREGROUND | MB_TOPMOST
    return ctypes.windll.user32.MessageBoxW(0, text, "KuubMill — відновлення", 0x4 | 0x30 | 0x100 | 0x10000 | 0x40000) == 6


__all__ = ["run", "guard_database", "windows_say", "windows_ask", "app_is_running"]
