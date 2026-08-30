"""Вигляд списків під оператора — спільна логіка шестерні (черга і пошта).

Шестерня стоїть на двох екранах і має однакові правила: числа підтягуються до
меж, нуль означає «як було» (а не мінімум), а пресет чи крок поза переліком —
це помилка розмітки, яку краще побачити, ніж тихо проковтнути.

Межі живуть ТУТ, в одному екземплярі. Їх дублює lookgear.js, щоб кнопка гасла
на краю одразу, не чекаючи мережі; розійтись тихо вони не можуть — за цим
стежить tests/test_mail_look_prefs.py.
"""

from dataclasses import dataclass

from app.models import User

#: Крок, яким оператор підкручує числа. 0 = «не задано» → клієнт бере 2.
UI_STEPS = (0, 1, 2, 4, 8)

#: Пресети щільності черги. Порожній рядок — канон («звичайний»), і саме тому
#: окремого "normal" тут немає: два значення з одним змістом розійшлися б.
QUEUE_DENSITIES = ("", "compact", "spacious")
#: Вигляд колонки «Матеріал / Колір»: канон — маркування, "code" — техкод.
QUEUE_MAT_STYLES = ("", "code")


@dataclass(frozen=True)
class Bounds:
    low: int
    high: int

    def clamp_or_zero(self, value: int) -> int:
        """Нуль проходить повз межі: він означає не «найменше», а «як було»."""
        return max(self.low, min(self.high, value)) if value else 0


#: Вертикальний відступ рядка. Нижня межа — та, під якою текст злипається;
#: верхня — та, за якою в екран влазить менше половини списку.
ROW_PAD = Bounds(2, 28)
#: Ширина панелі списку листів. Нижня — та, під яку розрахований двоповерховий
#: рядок; верхня — стеля, за якою рядок перестає читатись як рядок.
LIST_WIDTH = Bounds(340, 1180)


class LookError(ValueError):
    """Значення, яке не могло прийти від кнопок — тобто помилка розмітки."""


def apply_mail_look(user: User, *, row_pad: int, list_width: int, step: int) -> None:
    if step not in UI_STEPS:
        raise LookError("невідомий крок")
    user.mail_row_pad = ROW_PAD.clamp_or_zero(row_pad)
    user.mail_list_width = LIST_WIDTH.clamp_or_zero(list_width)
    user.mail_ui_step = step


def apply_queue_look(
    user: User, *, density: str, row_pad: int, mat_style: str, step: int
) -> None:
    if step not in UI_STEPS:
        raise LookError("невідомий крок")
    if density not in QUEUE_DENSITIES:
        raise LookError("невідомий пресет щільності")
    if mat_style not in QUEUE_MAT_STYLES:
        raise LookError("невідомий вигляд колонки кольору")
    user.queue_density = density
    user.queue_row_pad = ROW_PAD.clamp_or_zero(row_pad)
    user.queue_mat_style = mat_style
    user.queue_ui_step = step
