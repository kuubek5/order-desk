r"""Чи є файл — з трьома відповідями, а не двома.

`Path.exists()` знає лише «так» і «ні», а на мережевій шарі є третя
відповідь: «не можу подивитись». І Windows віддає її в найгіршій формі —
недоступна шара (`\\host\share`, WinError 53/67) приходить як
`FileNotFoundError`, тобто рівно як видалений файл, і `exists()` чесно каже
False (перевірено 05.10.26 на Python 3.11 цеху). Для переносу коронок це
різні світи: «файла немає» дозволяє викреслити його з переліку, «не бачу» —
ні, бо файл, найімовірніше, лежить там, і без запису про нього він
загубиться (MULTICALC_SAFETY_BRIEF.md, п.3).

Відсутність визнається лише за WinError 2/3 (або без коду — не Windows) І при
живому корені шляху: відключений диск чи шара теж буває «шлях не знайдено».
"""

from __future__ import annotations

from pathlib import Path

PRESENT = "present"
ABSENT = "absent"
UNREACHABLE = "unreachable"

# ERROR_FILE_NOT_FOUND, ERROR_PATH_NOT_FOUND — справжня відсутність. Решта
# FileNotFoundError на Windows (53 ERROR_BAD_NETPATH, 67 ERROR_BAD_NET_NAME…)
# — мережа.
_GONE_WINERRORS = (None, 2, 3)


def path_state(path: str | Path) -> str:
    """PRESENT / ABSENT / UNREACHABLE для одного шляху. Не кидає."""
    target = Path(path)
    try:
        target.stat()
        return PRESENT
    except FileNotFoundError as exc:
        if getattr(exc, "winerror", None) not in _GONE_WINERRORS:
            return UNREACHABLE
    except OSError:
        return UNREACHABLE
    except ValueError:
        return ABSENT  # шлях, якого не буває (нульовий байт тощо)
    anchor = target.anchor
    if anchor:
        try:
            Path(anchor).stat()
        except OSError:
            return UNREACHABLE
    return ABSENT
