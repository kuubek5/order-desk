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
from app.queue_filters import READY_FILTERS

#: Крок, яким оператор підкручує числа. 0 = «не задано» → клієнт бере 2.
UI_STEPS = (0, 1, 2, 4, 8)

#: Пресети щільності черги. Порожній рядок — канон («звичайний»), і саме тому
#: окремого "normal" тут немає: два значення з одним змістом розійшлися б.
QUEUE_DENSITIES = ("", "compact", "spacious")
#: Вигляд колонки «Матеріал / Колір»: канон — маркування, "code" — техкод.
QUEUE_MAT_STYLES = ("", "code")
#: Розкладка видачі: канон — один стовпець карток, "nav" — плюс покажчик дня.
#: "list" — той самий канон, названий уголос (див. коментар до HANDOUT_FLOWS):
#: без власного слова покажчик вмикався б і вже не вимикався.
HANDOUT_LAYOUTS = ("", "list", "nav")
#: Порядок списку видачі: канон — картки по клієнтах, "sheet" — рядки
#: таблиці підряд, як їх бачить той, хто веде видачу за самою таблицею.
#: "clients" — той самий канон, названий уголос: порожнє поле у формі не
#: доходить до сервера відрізнено від «поля не було» (обидва читаються як
#: None), тож повернення до карток мусить мати власне слово.
HANDOUT_FLOWS = ("", "clients", "sheet")


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
#: Вигляд екрана пошти: канон — доведений класичний екран (V1), "conversation"
#: — опційний вигляд A (MAIL_V1_BRIEF.md, 28.09.26; вигляд C «Фокус» власник
#: відхилив після живої перевірки 28.09.26 — прибраний, не лишати мертвим
#: значенням). "" тут — це і канон, і «нічого не збережено» (як у
#: HANDOUT_FLOWS): перемикач ніколи не шле порожній рядок сам, лише
#: "classic"/"conversation" — інакше скидання на канон і «поля не було»
#: злились би в одне значення.
MAIL_VIEWS = ("", "classic", "conversation")
#: Док черги внизу екрана пошти (MAIL_LAB_DOCK_BRIEF.md, 07.10.26, варіант A).
#: Канон "" — лише прийняте з пошти, як було; "mail" — той самий канон,
#: названий уголос на дроті (та сама пастка, що "classic"/"clients"); "lab" —
#: плюс лабораторія; "all" — уся вкладка дня.
MAIL_DOCK_SCOPES = ("", "mail", "lab", "all")
#: Фільтр готовності для лабораторних і табличних рядків доку. Слова — з
#: queue_filters.READY_FILTERS; канон "" у базі = "can_take" (те, що
#: CAM-оператор бере між листами), і "can_take" на дроті кладеться як "".
MAIL_DOCK_READY_DEFAULT = "can_take"
#: Висота доку (тіла таблиці). Нижня — три рядки з шапкою, верхня — стеля, за
#: якою список листів над доком перестає вміщати хоч щось.
DOCK_HEIGHT = Bounds(160, 720)


class LookError(ValueError):
    """Значення, яке не могло прийти від кнопок — тобто помилка розмітки."""


def mail_dock_ready_of(user: User | None) -> str:
    """Збережений фільтр готовності доку, з канону в слово."""
    saved = (getattr(user, "mail_dock_ready", "") or "") if user is not None else ""
    return saved or MAIL_DOCK_READY_DEFAULT


def apply_mail_look(
    user: User,
    *,
    row_pad: int | None = None,
    list_width: int | None = None,
    step: int | None = None,
    view: str | None = None,
    dock: str | None = None,
    dock_ready: str | None = None,
    dock_height: int | None = None,
) -> None:
    """Кожне поле необовʼязкове, і None означає «не чіпати» (як у
    apply_handout_look): шестерня шле відступ, ширину й крок разом, кнопки
    вигляду й доку — лише своє. До 07.10.26 відступ/ширина/крок читались
    безумовно, і клік по вигляду екрана мовчки скидав їх у канон; перемикач
    доку тиснуть десятки разів на день — із тим правилом він стирав би
    налаштування на кожному кліку."""
    if step is not None:
        if step not in UI_STEPS:
            raise LookError("невідомий крок")
        user.mail_ui_step = step
    if view is not None:
        if view not in MAIL_VIEWS:
            raise LookError("невідомий вигляд пошти")
        # Канон у базі лишається порожнім рядком — так його читає ui_prefs і
        # шаблони; "classic" це лише слово на дроті (§ пастка «порожнє поле»).
        user.mail_view = "" if view == "classic" else view
    if dock is not None:
        if dock not in MAIL_DOCK_SCOPES:
            raise LookError("невідоме джерело доку пошти")
        user.mail_dock_scope = "" if dock == "mail" else dock
    if dock_ready is not None:
        if dock_ready not in READY_FILTERS:
            raise LookError("невідомий фільтр готовності доку")
        user.mail_dock_ready = "" if dock_ready == MAIL_DOCK_READY_DEFAULT else dock_ready
    if dock_height is not None:
        user.mail_dock_height = DOCK_HEIGHT.clamp_or_zero(dock_height)
    if row_pad is not None:
        user.mail_row_pad = ROW_PAD.clamp_or_zero(row_pad)
    if list_width is not None:
        user.mail_list_width = LIST_WIDTH.clamp_or_zero(list_width)


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


def apply_handout_look(
    user: User, *, layout: str | None = None, flow: str | None = None
) -> None:
    """Вигляд видачі: розкладка екрана і порядок списку.

    Кожне поле необовʼязкове, і None означає «не чіпати». У шапці стоять дві
    незалежні кнопки-іконки, кожна шле лише СВОЄ значення; якби пропущене поле
    означало «порожньо», клік по одній мовчки скидав би другу.
    """
    if layout is not None:
        if layout not in HANDOUT_LAYOUTS:
            raise LookError("невідома розкладка видачі")
        user.handout_layout = "" if layout == "list" else layout
    if flow is not None:
        if flow not in HANDOUT_FLOWS:
            raise LookError("невідомий порядок списку видачі")
        # У базі канон лишається порожнім рядком — так його читають шаблони й
        # дзеркало в сесії; "clients" це лише слово на дроті.
        user.handout_flow = "" if flow == "clients" else flow
