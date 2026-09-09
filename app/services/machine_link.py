"""Розбір обриву зв'язку з верстатом: клас поломки, стук у порти, пояснення.

Навіщо окремий модуль. `app/services/machines.py` уміє СКАЗАТИ, що сталось —
три різні тексти для трьох різних поломок. Але сказане там живе лише в
памʼяті процесу і зникає на рестарті, а на питання «чому воно рветься
регулярно» відповідає не одне повідомлення, а їхня послідовність. Тут
зібрано те, що потрібно для розбору ПІСЛЯ факту: як упізнати клас поломки за
текстом, як зібрати доказ (відповіді портів), і як перекласти доказ у
пояснення, за яким видно, куди йти руками.

ПОЯСНЕННЯ БУДУЄТЬСЯ ПРИ ПОКАЗІ, а не зберігається. У базі лежать самі факти:
клас, сирий текст, відповіді портів із часом. Тому виправлене формулювання
лікує і вже записані обриви, а записаний колись текст не може стати
неправдою після зміни коду.

ЧЕСНІСТЬ ВАЖЛИВІША ЗА ВПЕВНЕНІСТЬ. Кожне пояснення має не лише «що це
доводить», а й «чого це НЕ доводить»: мовчання всіх портів однаково дає і
вимкнений ПК, і брандмауер у профілі «Загальнодоступна», і вибрати між ними
ми не можемо. Правило §14 «хибне число гірше за жодне» діє й для слів —
вигаданий однозначний вирок гірший за чесно названі два пояснення.
"""

from __future__ import annotations

import json
import socket
import time
from dataclasses import dataclass, field
from typing import Any, Optional


# ── Класи поломок ───────────────────────────────────────────────────────────
# Плоскі рядки, а не Enum: вони лягають у колонку `machine_link_events.cause`,
# по них групують у SQL і їх читає шаблон. Значення не міняти — вони вже в
# базі; додавати нові можна вільно.
CAUSE_AGENT_DOWN = "agent_down"      # RST: ПК живий, агента немає
CAUSE_PORT_SILENT = "port_silent"    # SYN пішов, тиша
CAUSE_NETWORK = "network"            # мережевий рівень не довіз пакет
CAUSE_SLOW = "slow"                  # агент відповів, але не встиг віддати кадр
CAUSE_TOKEN = "token"                # агент відхилив токен
CAUSE_FRAME = "frame"                # кадр прийшов, але непридатний
CAUSE_VNC = "vnc"                    # бік VNC (не HTTP-агент)
CAUSE_OTHER = "other"

# Обгортка, якою `machines._grab_machine_frame` і `poll_all.grab` загортають
# будь-який виняток. Саме через неї впізнавання за РІВНІСТЮ не працювало:
# повідомлення до порівняння доїжджало з префіксом (знайдено 09.09.26).
WRAP_PREFIX = "Знімок не вдався: "


def strip_wrap(error: str) -> str:
    """Текст без службової обгортки — для показу людині."""
    text = (error or "").strip()
    return text[len(WRAP_PREFIX):].strip() if text.startswith(WRAP_PREFIX) else text


def classify(error: Optional[str], host: str, port: int) -> str:
    """Клас поломки за текстом помилки.

    Впізнаємо за ВХОДЖЕННЯМ, а не за рівністю. Рівність тут уже коштувала
    цілої фічі: перевірка досяжності стояла на `error in (msg_a, msg_b)`, а
    обидва шляхи опитування віддають текст із префіксом `Знімок не вдався: `,
    тож умова не збігалась НІКОЛИ і стук у порти не робився жодного разу
    (тести годували poll_target голим повідомленням і лишались зелені).

    Тексти будуються тими самими функціями, що й показуються, тож розійтись
    вони не можуть — на відміну від зашитих тут копій рядків.
    """
    from app.services import machines as _m

    text = strip_wrap(error or "")
    if not text:
        return CAUSE_OTHER
    if _m.msg_agent_not_listening(host, port) in text:
        return CAUSE_AGENT_DOWN
    if _m.msg_host_silent(host, port) in text:
        return CAUSE_PORT_SILENT
    if _m.msg_host_no_answer(host) in text:
        return CAUSE_NETWORK
    low = text.lower()
    if "токен" in low or "403" in low:
        return CAUSE_TOKEN
    if "не віддав кадр" in low or "надто повільно" in low:
        return CAUSE_SLOW
    if "завеликий кадр" in low or "не png" in low:
        return CAUSE_FRAME
    if "vnc" in low:
        return CAUSE_VNC
    return CAUSE_OTHER


def needs_probe(cause: str) -> bool:
    """Чи має сенс стукати в інші порти.

    Тільки коли ми НЕ знаємо, чи живий ПК. Відмова (RST) це вже сказала, і
    платити трьома секундами за відоме не треба — крім детального режиму, де
    оператор свідомо просить повний доказ (див. `Machine.diagnose_link`).
    """
    return cause in (CAUSE_PORT_SILENT, CAUSE_NETWORK)


# ── Стук у порти ────────────────────────────────────────────────────────────
# Результат кожного порту окремо, з часом відповіді. Голого «так/ні» мало:
# «445 відмовив за 14 мс» і «445 мовчав секунду» — різні світи, і саме ця
# різниця відповідає на «ПК живий чи ні». Порти не «зламуємо»: одразу
# закриваємо сокет, нічого не шлемо й не читаємо.

PROBE_OPEN = "open"        # зʼєднання прийнято
PROBE_REFUSED = "refused"  # RST — відповів САМ ПК, отже він у мережі
PROBE_SILENT = "silent"    # тиша до таймауту
PROBE_ERROR = "error"      # щось інше (немає маршруту, DNS)


def probe_ports(
    host: str, ports: tuple[int, ...], timeout: float
) -> list[dict[str, Any]]:
    """Постукати в кожен порт і повернути, що саме відповів кожен.

    Не кидає: діагностика не має права завалити опитування. Порт, на якому
    сталось невідоме, лишається в списку з класом `error` і текстом — це
    теж доказ, а мовчазний пропуск ним не був би.
    """
    rows: list[dict[str, Any]] = []
    for probe in ports:
        started = time.monotonic()
        result = PROBE_SILENT
        detail = ""
        try:
            with socket.create_connection((host, probe), timeout=timeout):
                result = PROBE_OPEN
        except ConnectionRefusedError:
            result = PROBE_REFUSED
        except socket.timeout:
            result = PROBE_SILENT
        except OSError as exc:
            if getattr(exc, "errno", None) == 10061:  # WSAECONNREFUSED
                result = PROBE_REFUSED
            else:
                result = PROBE_ERROR
                detail = type(exc).__name__
        rows.append(
            {
                "port": probe,
                "result": result,
                "ms": int((time.monotonic() - started) * 1000),
                **({"detail": detail} if detail else {}),
            }
        )
    return rows


def answered_from_probe(rows: list[dict[str, Any]]) -> Optional[bool]:
    """True — ПК озвався хоч на одному порту (згодою АБО відмовою).

    Відмова — така сама відповідь, як згода: RST шле лише той, хто отримав
    пакет. Порожній список = не стукали, і тоді відповіді немає, а не «ні».
    """
    if not rows:
        return None
    for row in rows:
        if row.get("result") in (PROBE_OPEN, PROBE_REFUSED):
            return True
    return False


def dump_probe(rows: Optional[list[dict[str, Any]]]) -> Optional[str]:
    if not rows:
        return None
    return json.dumps(rows, ensure_ascii=False)


def load_probe(raw: Optional[str]) -> list[dict[str, Any]]:
    """Розбір JSON із бази. Зіпсований рядок = порожньо, а не 500: журнал
    діагностики не має падати через один кривий запис."""
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return []
    return data if isinstance(data, list) else []


PROBE_WORDS = {
    PROBE_OPEN: "відкрито",
    PROBE_REFUSED: "відмова (RST)",
    PROBE_SILENT: "тиша",
    PROBE_ERROR: "помилка",
}


# ── Пояснення ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Explanation:
    """Розбір одного обриву: що сталось, що це доводить, що робити."""

    headline: str
    # Що ми ЗНАЄМО і з чого саме це випливає.
    proof: list[str] = field(default_factory=list)
    # Чого цей доказ НЕ доводить. Порожній список — теж відповідь.
    doubt: list[str] = field(default_factory=list)
    # Кроки руками, у порядку, у якому їх варто робити.
    actions: list[str] = field(default_factory=list)
    # Розібрані відповіді портів для таблички.
    probe_rows: list[dict[str, Any]] = field(default_factory=list)


_HEADLINES = {
    CAUSE_AGENT_DOWN: "Агент на ПК верстата не працює",
    CAUSE_PORT_SILENT: "Порт агента мовчить",
    CAUSE_NETWORK: "ПК не відповідає в мережі",
    CAUSE_SLOW: "Агент відповідає, але не встигає віддати кадр",
    CAUSE_TOKEN: "Агент відхилив токен",
    CAUSE_FRAME: "Кадр прийшов, але непридатний",
    CAUSE_VNC: "Не вдалось підключитись по VNC",
    CAUSE_OTHER: "Обрив зв'язку",
}


def explain(
    *,
    cause: str,
    error: str,
    host: str,
    port: int,
    probe_json: Optional[str] = None,
    probe_verdict: Optional[str] = None,
    probe_late: bool = False,
    deep: bool = False,
) -> Explanation:
    """Людський розбір обриву за фактами з рядка журналу.

    Тільки з переданих полів, без звернень до мережі й до стану процесу:
    той самий рядок мусить пояснюватись однаково і через тиждень.
    """
    rows = load_probe(probe_json)
    answered = answered_from_probe(rows)
    proof: list[str] = []
    doubt: list[str] = []
    actions: list[str] = []

    if cause == CAUSE_AGENT_DOWN:
        proof.append(
            f"ПК {host} відповів ВІДМОВОЮ на порт {port}. Відмову (RST) може "
            "надіслати лише той, хто отримав пакет, — отже ПК увімкнений і в "
            "мережі, а слухати на цьому порту нема кому."
        )
        actions += [
            f"Перевірити на ПК {host}, чи запущено службу kmill-agent.",
            "Якщо служба стоїть — запустити її й поставити автозапуск.",
            "Якщо служба «запущена», але порт мовчить — перезапустити її.",
        ]
        doubt.append(
            "Це не каже, ЧОМУ агент зупинився: оновлення Windows, вихід "
            "користувача з сесії, падіння самої служби виглядають однаково."
        )
    elif cause == CAUSE_PORT_SILENT:
        proof.append(
            f"Запит на {host}:{port} пішов, але за час очікування не прийшло "
            "нічого — ні згоди, ні відмови. Пакет хтось викинув мовчки."
        )
        if answered is True:
            proof.append(
                f"Стук у сусідні порти: ПК {host} озвався. Отже він у мережі, "
                f"і глушиться саме порт {port} — брандмауер або профіль мережі."
            )
            actions += [
                f"На ПК {host} перевірити профіль мережі: має бути «Приватна». "
                "Після перевизначення на «Загальнодоступна» правило порту "
                "перестає діяти, і виглядає це точно як обрив.",
                f"Перевірити правило вхідних для порту {port} у брандмауері.",
            ]
        elif answered is False:
            proof.append("Стук у сусідні порти: ПК не озвався на жодному.")
            doubt.append(
                "Мовчання всіх портів НЕ доводить, що ПК вимкнено: брандмауер "
                "у профілі «Загальнодоступна» глушить 445 і 3389 так само, як "
                "порт агента. Пояснень рівно два, і вибрати між ними звідси "
                "неможливо."
            )
            actions += [
                f"Подивитись, чи ПК {host} узагалі увімкнений.",
                f"З іншого ПК: Test-NetConnection {host} -Port {port}.",
                "Якщо ПК працює — дивитись профіль мережі й брандмауер.",
            ]
        else:
            doubt.append(
                "Стук у сусідні порти не робився, тож чи живий сам ПК — "
                "невідомо."
                + (
                    ""
                    if deep
                    else " Увімкніть «Детальний журнал зв'язку» на цьому "
                    "верстаті, щоб наступного разу доказ зібрався сам."
                )
            )
            actions.append(f"З іншого ПК: Test-NetConnection {host} -Port {port}.")
    elif cause == CAUSE_NETWORK:
        proof.append(
            f"Мережевий рівень не довіз пакет до {host}: немає маршруту або "
            "адреса не відгукується. Це рівень нижчий за порт."
        )
        if answered is True:
            proof.append(
                "Але на сусідні порти ПК озвався — отже адреса жива, і збій "
                "був короткочасний або стосується саме цього порту."
            )
        actions += [
            f"Перевірити кабель і лінк на ПК {host}.",
            "Перевірити, чи не змінилась адреса ПК (DHCP видав іншу).",
            f"З іншого ПК: ping {host}.",
        ]
    elif cause == CAUSE_SLOW:
        proof.append(
            f"Агент на {host}:{port} відповів — зв'язок є. Але кадр не приїхав "
            "за відведений час: ПК або мережа перевантажені."
        )
        doubt.append("Це не обрив мережі. ПК живий і агент працює.")
        actions += [
            "Подивитись завантаження ПК верстата (CPU, диск).",
            "Перевірити, чи не йде на ньому оновлення або антивірусний скан.",
        ]
    elif cause == CAUSE_TOKEN:
        proof.append(
            f"Агент на {host}:{port} відповів і свідомо відхилив запит: токен "
            "не збігається. Мережа й агент справні."
        )
        actions.append(
            "Звірити токен агента в Налаштуваннях → Обладнання з тим, що "
            "прописаний на ПК верстата."
        )
    elif cause == CAUSE_FRAME:
        proof.append(
            "Кадр приїхав, але прочитати його не вдалось. Зв'язок справний — "
            "справа в самому знімку."
        )
        actions.append("Перевірити версію агента на ПК верстата.")
    elif cause == CAUSE_VNC:
        proof.append(
            f"Підключення до {host}:{port} по VNC не вдалось. Якщо на цьому "
            "верстаті стоїть HTTP-агент, у налаштуваннях бракує його токена — "
            "без токена CRM іде по VNC."
        )
        actions.append("Перевірити токен агента в Налаштуваннях → Обладнання.")
    else:
        proof.append(strip_wrap(error) or "Причина не розпізнана.")
        actions.append("Показати цей текст розробнику — клас поломки новий.")

    if probe_verdict and probe_verdict not in " ".join(proof):
        proof.append(probe_verdict)
    if probe_late:
        doubt.append(
            "Стук завершився вже після того, як зв'язок повернувся, тож його "
            "вирок описує момент ПІСЛЯ обриву, а не сам обрив."
        )

    return Explanation(
        headline=_HEADLINES.get(cause, _HEADLINES[CAUSE_OTHER]),
        proof=proof,
        doubt=doubt,
        actions=actions,
        probe_rows=rows,
    )


# ── Читання журналу ─────────────────────────────────────────────────────────
# Екран діагностики питає в базу три різні речі, і всі три — з одного набору
# рядків: перелік обривів, підсумок по кожному верстату і смуга «коли саме».
# Смуга тут головна: вона відповідає на питання, якого не ставить жоден
# окремий рядок — чи рвалось У ВСІХ ОДНОЧАСНО. Одночасно = мережа або
# живлення, поодинці = той конкретний ПК. Без цього розрізнення шукають не там.


@dataclass(frozen=True)
class OutageView:
    """Один обрив, готовий до показу."""

    id: int
    host: str
    name: str
    started_at: Optional[Any]
    detected_at: Any
    ended_at: Optional[Any]
    error: str
    cause: str
    deep: bool
    failed_polls: int
    probe_late: bool
    explanation: Explanation
    trail: list[dict[str, Any]] = field(default_factory=list)

    @property
    def is_open(self) -> bool:
        """Обрив без кінця. Або триває просто зараз, або застосунок
        перезапустили посеред нього — і тоді ми чесно не знаємо, коли він
        скінчився, а вгадувати не будемо."""
        return self.ended_at is None

    @property
    def seconds(self) -> Optional[int]:
        if self.ended_at is None:
            return None
        return max(0, int((self.ended_at - self.detected_at).total_seconds()))


def view_of(event: Any) -> OutageView:
    """ORM-рядок → готовий до показу обрив, разом із поясненням."""
    host = event.host or ""
    # Порт для пояснення беремо з ключа (`host-port`), як його склав
    # MachineTarget.key. Немає хвоста — верстат стоїть на типовому порту.
    port = 0
    if "-" in host:
        head, _, tail = host.rpartition("-")
        if head and tail.isdigit():
            port = int(tail)
            host = head
    return OutageView(
        id=event.id,
        host=event.host or "",
        name=event.name or event.host or "",
        started_at=event.started_at,
        detected_at=event.detected_at,
        ended_at=event.ended_at,
        error=event.error or "",
        cause=event.cause or CAUSE_OTHER,
        deep=bool(event.deep),
        failed_polls=int(event.failed_polls or 0),
        probe_late=bool(event.probe_late),
        explanation=explain(
            cause=event.cause or CAUSE_OTHER,
            error=event.error or "",
            host=host,
            port=port,
            probe_json=event.probe_json,
            probe_verdict=event.probe_verdict,
            probe_late=bool(event.probe_late),
            deep=bool(event.deep),
        ),
        trail=load_probe(event.error_trail),
    )


def load_outages(db: Any, *, since: Any, limit: int = 400) -> list[OutageView]:
    """Обриви від `since`, найновіші зверху.

    Стеля на кількість рядків тут не косметична: мертвий верстат за місяць
    дає сотні обривів, а екран, який намагається намалювати їх усі, стає
    непридатним саме тоді, коли він найпотрібніший.
    """
    from sqlalchemy import select

    from app.models import MachineLinkEvent

    rows = db.scalars(
        select(MachineLinkEvent)
        .where(MachineLinkEvent.detected_at >= since)
        .order_by(MachineLinkEvent.detected_at.desc())
        .limit(limit)
    ).all()
    return [view_of(row) for row in rows]


@dataclass
class MachineSummary:
    """Підсумок по одному верстату за вікно."""

    host: str
    name: str
    outages: int = 0
    downtime_seconds: int = 0
    longest_seconds: int = 0
    last_at: Optional[Any] = None
    open_now: bool = False
    deep: bool = False
    # Скільки обривів припало на кожну комірку смуги (див. bucket_grid).
    cells: list[int] = field(default_factory=list)


def summarize(views: list[OutageView]) -> list[MachineSummary]:
    """Підсумки по верстатах, найпроблемніші зверху.

    Порядок саме за кількістю обривів, а не за назвою: екран відкривають з
    питанням «хто рветься», і відповідь має бути в першому рядку.
    """
    by_host: dict[str, MachineSummary] = {}
    for view in views:
        if view.host not in by_host:
            by_host[view.host] = MachineSummary(host=view.host, name=view.name)
        item = by_host[view.host]
        item.outages += 1
        item.deep = item.deep or view.deep
        seconds = view.seconds
        if seconds is not None:
            item.downtime_seconds += seconds
            item.longest_seconds = max(item.longest_seconds, seconds)
        else:
            item.open_now = True
        if item.last_at is None or view.detected_at > item.last_at:
            item.last_at = view.detected_at
            item.name = view.name
    return sorted(by_host.values(), key=lambda s: (-s.outages, s.name))


def bucket_grid(
    views: list[OutageView], summaries: list[MachineSummary], *, edges: list[Any]
) -> None:
    """Розкласти обриви по комірках смуги (на місці, у `summary.cells`).

    `edges` — межі комірок, N+1 позначок на N комірок. Обрив зараховується в
    комірку за ЧАСОМ ВИЯВЛЕННЯ, а не розмазується по тривалості: питання, на
    яке відповідає смуга, — «коли рвалось», і довгий нічний обрив не має
    зафарбувати пів доби так, ніби рвалось увесь час.
    """
    if len(edges) < 2:
        return
    index = {summary.host: summary for summary in summaries}
    for summary in summaries:
        summary.cells = [0] * (len(edges) - 1)
    for view in views:
        target = index.get(view.host)
        if target is None:
            continue
        at = view.detected_at
        for pos in range(len(edges) - 1):
            if edges[pos] <= at < edges[pos + 1]:
                target.cells[pos] += 1
                break
