"""Дані для глобальної палітри команд (Ctrl+K) — аудит 05.09.26, крок 3.3.

Малює палітру `app/static/js/palette.js`; тут лише те, що вона не має права
вирішувати сама.

**Чому перелік екранів будує сервер, а не JS.** Роль вирішує, що людина взагалі
має бачити: «Журнал синку» і «Вхідні» — адмінські. Показати оператору пункт і
дати 403 після Enter гірше, ніж не показати зовсім: рядок у списку — це
обіцянка, а 403 читається як поламаний застосунок. Роль знає лише сервер, тому
список фільтрує він.

**Чому пошук робіт окремим ендпойнтом, а не через `/search`.** `/search` — це
ЕКРАН: він тягне ВСІ роботи в память і фільтрує їх у Python (повний перебір
`orders`, таблиця росте на ~92 рядки за кожен робочий день). Смикати таке на
кожну натиснуту літеру не можна. Тут — вузький SQL із `LIMIT`, а сам `/search`
лишається за палітрою як «показати все»: останній рядок веде на `/search?q=…`,
той самий екран і той самий результат, лише без дублювання логіки.

Обидва роути віддають JSON і на «не увійшов» відповідають 401, а не 303 на
`/login`: палітру відкриває `fetch`, і редирект вона мовчки проковтнула б —
список просто лишився б порожнім без пояснення.
"""

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import or_, select
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.models import Order
from app.routers.deps import get_current_user, get_db

router = APIRouter()

ADMIN_ROLE = "адмін"

# Скільки робіт показувати інлайном. Вісім — стеля, за якою список перестає
# читатись оком за один погляд; решту оператор бачить на /search.
SEARCH_LIMIT = 8

# Один символ збігається майже з усім і коштує сканування таблиці задарма.
SEARCH_MIN_LEN = 2

# Екрани палітри. Порядок = порядок у рейці (_topbar_nav.html), щоб палітра не
# вигадувала власну карту застосунку: людина, яка вже знає меню, знаходить
# пункт там, де очікує.
#
# `keywords` — синоніми пошуку, зокрема латиниця: оператор часто друкує в
# англійській розкладці й не помічає цього до першого «нічого не знайдено».
# `admin` — пункт видно лише адміну (гейт самого екрана лишається на місці;
# це не заміна гейта, а те, що людина не б'ється в зачинені двері).
COMMANDS: list[dict] = [
    {"label": "Черга", "group": "Робота", "href": "/",
     "keywords": "queue cherga роботи головна", "admin": False},
    {"label": "Нові з пошти", "group": "Робота", "href": "/mail",
     "keywords": "mail pochta тріаж листи вкладення", "admin": False},
    {"label": "Ранкова видача", "group": "Робота", "href": "/handout",
     "keywords": "handout vydacha видати коронки лоток", "admin": False},
    {"label": "Зміна", "group": "Робота", "href": "/shift",
     "keywords": "shift zmina передача нотатки", "admin": False},
    {"label": "Пічки", "group": "Цех", "href": "/furnaces",
     "keywords": "furnaces pichky печі спікання синтеризація", "admin": False},
    {"label": "Верстати", "group": "Цех", "href": "/machines",
     "keywords": "machines verstaty фрезери imes icore", "admin": False},
    {"label": "Клієнти", "group": "Довідники", "href": "/clients",
     "keywords": "clients kliienty замовники", "admin": False},
    {"label": "Архів", "group": "Довідники", "href": "/archive",
     "keywords": "archive arkhiv хроніка старі роботи", "admin": False},
    {"label": "Пошук робіт", "group": "Довідники", "href": "/search",
     "keywords": "search poshuk знайти наряд sum3d", "admin": False},
    {"label": "Журнал дій", "group": "Історія", "href": "/journal",
     "keywords": "journal zhurnal хто що зробив скасувати", "admin": False},
    {"label": "Журнал синку", "group": "Історія", "href": "/journal/sync",
     "keywords": "sync journal синхронізація таблиця помилки", "admin": True},
    {"label": "Статистика", "group": "Облік", "href": "/stats",
     "keywords": "stats statystyka брак графіки", "admin": False},
    {"label": "Виробіток", "group": "Облік", "href": "/vyrobitok",
     "keywords": "vyrobitok табель зарплата одиниці місяць", "admin": False},
    {"label": "Вхідні (зворотний звʼязок)", "group": "Система",
     "href": "/feedback/inbox",
     "keywords": "feedback vkhidni звернення скарги", "admin": True},
    {"label": "Мій акаунт", "group": "Система", "href": "/account",
     "keywords": "account akaunt кабінет пароль тема", "admin": False},
    {"label": "Налаштування", "group": "Система", "href": "/settings",
     "keywords": "settings nalashtuvannia секрети шляхи оператори", "admin": False},
]


def visible_commands(user) -> list[dict]:
    """Пункти, які цій ролі МОЖНА показати. Адмінські для оператора не
    ховаються стилями й не дають 403 — їх просто немає у відповіді."""
    is_admin = getattr(user, "role", None) == ADMIN_ROLE
    return [
        {k: v for k, v in item.items() if k != "admin"}
        for item in COMMANDS
        if is_admin or not item["admin"]
    ]


def _like(term: str) -> str:
    """Шаблон LIKE, у якому `%` і `_` із запиту — літерали.

    Sum3D ID оператор друкує з дефісами й підкресленнями (`12-01-45`,
    `12_01_45`), а `_` у LIKE означає «будь-який символ»: без екранування
    запит «12_01» мовчки знаходив би і «12-01», і «12x01». Тихо неправильний
    результат гірший за порожній."""
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


@router.get("/palette/commands")
def palette_commands(request: Request, db: Session = Depends(get_db)) -> JSONResponse:
    user = get_current_user(request, db)
    if user is None:
        return JSONResponse({"items": []}, status_code=401)
    return JSONResponse({"items": visible_commands(user)})


@router.get("/palette/search")
def palette_search(
    request: Request,
    q: str = "",
    db: Session = Depends(get_db),
) -> JSONResponse:
    user = get_current_user(request, db)
    if user is None:
        return JSONResponse({"items": []}, status_code=401)

    term = (q or "").strip()
    if len(term) < SEARCH_MIN_LEN:
        return JSONResponse({"items": []})

    pattern = _like(term)
    orders = db.scalars(
        select(Order)
        .where(
            or_(
                Order.work_order_no.ilike(pattern, escape="\\"),
                Order.sum3d_id.ilike(pattern, escape="\\"),
                Order.job_code.ilike(pattern, escape="\\"),
                Order.client_name.ilike(pattern, escape="\\"),
            )
        )
        # Свіже зверху: оператор шукає майже завжди сьогоднішню роботу, а
        # однакові наряди з різних днів інакше вишиковувались би найстарішим
        # уперед — тобто рівно навпаки до потрібного.
        .order_by(Order.id.desc())
        .limit(SEARCH_LIMIT)
    ).all()

    items = []
    for order in orders:
        # Наряд-less рядки — норма (клієнтські роботи з пошти й лабораторні, де
        # наряд забули), тож заголовком стає те, що заповнене. Клієнт у підписі
        # не дублюється, коли він уже став заголовком: повтор з'їдає рядок, у
        # якому мали б поміститись матеріал і Sum3D.
        label = order.work_order_no or order.client_name or f"Робота #{order.id}"
        detail = [
            part
            for part in (
                order.client_name if order.client_name != label else None,
                order.material_color,
                f"Sum3D {order.sum3d_id}" if order.sum3d_id else None,
            )
            if part
        ]
        items.append(
            {
                "label": label,
                "sub": " · ".join(detail),
                "group": order.status or "",
                "href": f"/orders/{order.id}",
            }
        )
    return JSONResponse({"items": items})
