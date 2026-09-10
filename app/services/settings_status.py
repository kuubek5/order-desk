"""Плити стану для розділів «Налаштувань» (макет «Стенд»).

Кожен розділ починається смугою: пігулка стану + до чотирьох метрик. Це та
сама відповідь, по яку адмін приходить у налаштування («воно взагалі
працює?»), тільки винесена нагору замість сірого чіпа в кутку.

ГОЛОВНЕ ПРАВИЛО (те саме, що на печах, CLAUDE.md §14): **хибне число гірше за
жодне**. Метрика або спирається на реальний сигнал, або її тут немає. Тому:

* зелений ставиться лише там, де є ПІДТВЕРДЖЕННЯ роботи (свіжий heartbeat,
  кадр з верстата, активний адмін), а не там, де просто «поле заповнене»;
* «задано» і «працює» — різні написи й різні кольори: заповнений шлях до теки
  дає нейтральний тон, бо існування мережевої теки ми на рендері не перевіряємо
  (це робить окрема проба, і вона коштує похід у мережу);
* невідоме — сірий `none`, ніколи не зелений.

Тони: ``ok`` (зелений) · ``warn`` (жовтий) · ``alarm`` (червоний) ·
``none`` (сірий, «невідомо / не налаштовано»).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.__version__ import VERSION
from app.business_day import utc_now, utc_to_business
from app.models import SyncLog
from app.services import furnace as furnace_service
from app.services import machines as machines_service
from app.sync_heartbeat import last_agreement

TONE_OK = "ok"
TONE_WARN = "warn"
TONE_ALARM = "alarm"
TONE_NONE = "none"

# Стан heartbeat → тон плити. Джерело — app/sync_heartbeat.heartbeat_status,
# єдине місце, де вирішується «живий / протух / помилка»; тут лише переклад.
_HEARTBEAT_TONE = {
    "success": TONE_OK,
    "warning": TONE_WARN,
    "error": TONE_ALARM,
    "neutral": TONE_NONE,
}


@dataclass(frozen=True)
class Meter:
    """Одна клітинка метрики: підпис, велике значення, підрядок."""

    k: str
    v: str
    s: str = ""
    tone: str = ""


@dataclass(frozen=True)
class Slab:
    """Смуга стану розділу."""

    tone: str = TONE_NONE
    label: str = ""
    meters: list[Meter] = field(default_factory=list)


def _pl(n: int, one: str, few: str, many: str) -> str:
    """Українське відмінювання після числа: 1 робота / 2 роботи / 5 робіт."""
    if 11 <= n % 100 <= 14:
        return many
    tail = n % 10
    if tail == 1:
        return one
    if 2 <= tail <= 4:
        return few
    return many


def _heartbeat(pair: dict | None, key: str) -> dict[str, str]:
    if not pair:
        return {"state": "neutral", "label": "невідомо"}
    return pair.get(key) or {"state": "neutral", "label": "невідомо"}


def _clean(label: str) -> str:
    """Прибрати службові значки з тексту heartbeat — тон уже несе колір."""
    return label.replace("⚠", "").replace("✓", "").strip()


# Скільки годин запис у журналі ще вважається «свіжою активністю». Старший —
# не показуємо взагалі: помилка тижневої давності в метриці поруч із живим
# синком читалась як «щойно зламалось», хоча воркер відтоді відпрацював
# сотні тихих тіків (SyncLog навмисно не пише рядка на тихий тік).
SYNC_LOG_FRESH_HOURS = 24


def _last_sync_log(db: Session) -> Optional[SyncLog]:
    """Останній запис журналу — ЛИШЕ якщо він молодший за добу.

    Метрика відповідає на «що робив синк останнім часом». Старий запис на це
    питання не відповідає, а виглядає як відповідь — тому краще порожньо.
    """
    row = db.scalars(select(SyncLog).order_by(SyncLog.id.desc()).limit(1)).first()
    if row is None or row.occurred_at is None:
        return None
    # `occurred_at` — UTC (server_default на SQLite); локальний `now()` робив
    # запис на 3 год старшим, ніж він є.
    age = utc_now() - row.occurred_at
    if age > timedelta(hours=SYNC_LOG_FRESH_HOURS):
        return None
    return row


# ── окремі розділи ──────────────────────────────────────────────────────────


def _slab_state(ctx: dict) -> Slab:
    """Стан системи: конвеєр робіт + найгірший з двох синків."""
    pair = ctx.get("sync_status") or {}
    mail = _heartbeat(pair, "mail")
    sheet = _heartbeat(pair, "sheet")
    tones = [_HEARTBEAT_TONE.get(mail["state"], TONE_NONE), _HEARTBEAT_TONE.get(sheet["state"], TONE_NONE)]
    if TONE_ALARM in tones:
        tone, label = TONE_ALARM, "є помилка синку"
    elif TONE_WARN in tones:
        tone, label = TONE_WARN, "фоновий процес мовчить"
    elif tones == [TONE_OK, TONE_OK]:
        tone, label = TONE_OK, "обидва синки живі"
    elif not (ctx.get("google_configured") or ctx.get("imap_configured")):
        tone, label = TONE_NONE, "джерела не налаштовані"
    else:
        # Налаштовано, але фоновий тік ще не відбувся (щойно стартували).
        # Це НЕ «не налаштовано» — і не привід світити зеленим.
        tone, label = TONE_NONE, "очікує першої перевірки"

    # Метрики тут — про ФОНОВІ ПРОЦЕСИ, а не про лічильники конвеєра: числа
    # конвеєра нижче малює карта зі стрілками (scon-map), і дублювати їх у
    # плиті означало б два погляди на одне значення поруч.
    intervals = ctx.get("sync_intervals") or {}
    meters = [
        Meter(
            k="Пошта (IMAP)",
            v=_clean(mail["label"]) or "невідомо",
            s=f"фоновий, кожні {intervals.get('mail', '—')} хв",
            tone=_HEARTBEAT_TONE.get(mail["state"], TONE_NONE),
        ),
        Meter(
            k="Google Таблиця",
            v=_clean(sheet["label"]) or "невідомо",
            s=f"фоновий, кожні {intervals.get('sheet', '—')} хв",
            tone=_HEARTBEAT_TONE.get(sheet["state"], TONE_NONE),
        ),
    ]
    report = ctx.get("spool_report")
    if report is not None:
        meters.append(
            Meter(
                k="Спул пошти",
                v=f"{getattr(report, 'total_mb', 0)} МБ",
                s=f"{getattr(report, 'total_dirs', 0)} тимчасових тек",
            )
        )
    return Slab(tone=tone, label=label, meters=meters)


def _slab_sheets(db: Session, ctx: dict) -> Slab:
    """Google Таблиця: тон = heartbeat синку таблиці, не «поле заповнене»."""
    hb = _heartbeat(ctx.get("sync_status"), "sheet")
    tone = _HEARTBEAT_TONE.get(hb["state"], TONE_NONE)
    if not ctx.get("google_configured"):
        tone, label = TONE_NONE, "не підключено"
    else:
        label = {
            TONE_OK: "працює",
            TONE_WARN: "немає відповіді",
            TONE_ALARM: "помилка синку",
        }.get(tone, "очікує перевірки")

    meters = [Meter(k="Синк таблиці", v=_clean(hb["label"]) or "невідомо", s="фоновий, кожні 10 хв", tone=tone)]

    last = _last_sync_log(db)
    if last is not None:
        ok = (last.status or "").lower() in {"ok", "успіх", "success"}
        meters.append(
            Meter(
                k="Останній запис",
                v=(
                    utc_to_business(last.occurred_at).strftime("%H:%M")
                    if last.occurred_at else "—"
                ),
                s=(last.sheet_tab or last.direction or "").strip() or (last.status or ""),
                tone=TONE_OK if ok else TONE_WARN,
            )
        )

    meters.append(
        Meter(
            k="Авторизація",
            v="сервісний акаунт" if (ctx.get("values") or {}).get("google_auth_mode", "service_account") != "oauth" else "Google-акаунт",
            s="ключ збережено" if (ctx.get("values_set") or {}).get("google_service_account_json") or (ctx.get("values_set") or {}).get("google_oauth_refresh_token") else "ключ не задано",
            tone=TONE_OK if ((ctx.get("values_set") or {}).get("google_service_account_json") or (ctx.get("values_set") or {}).get("google_oauth_refresh_token")) else TONE_WARN,
        )
    )
    # Звірка «таблиця = база». Зелений ставимо ЛИШЕ коли прохід був
    # достовірний і розбіжностей нуль — «ще не звіряли» лишається сірим, як і
    # решта невідомого в цьому файлі.
    agreement = last_agreement()
    if agreement is None:
        meters.append(
            Meter(k="Звірка з базою", v="ще не було", s="після першого синку", tone=TONE_NONE)
        )
    else:
        differed = int(agreement.get("differed") or 0)
        rows = int(agreement.get("rows") or 0)
        if not agreement.get("trustworthy"):
            a_tone, a_value, a_sub = TONE_NONE, "відкладена", "рядки саме рухались"
        elif differed:
            a_tone, a_value = TONE_WARN, f"{differed} " + _pl(differed, "розбіжність", "розбіжності", "розбіжностей")
            a_sub = f"звірено {rows} " + _pl(rows, "рядок", "рядки", "рядків")
        else:
            a_tone, a_value = TONE_OK, "збігається"
            a_sub = f"звірено {rows} " + _pl(rows, "рядок", "рядки", "рядків")
        meters.append(Meter(k="Звірка з базою", v=a_value, s=a_sub, tone=a_tone))

    total = ctx.get("sheet_snapshot_total") or 0
    meters.append(
        Meter(
            k="Копії вкладок",
            v=str(total),
            s="CSV-знімків збережено" if total else "жодного знімка",
            tone=TONE_OK if total else TONE_NONE,
        )
    )
    return Slab(tone=tone, label=label, meters=meters)


def _slab_imap(ctx: dict) -> Slab:
    hb = _heartbeat(ctx.get("sync_status"), "mail")
    tone = _HEARTBEAT_TONE.get(hb["state"], TONE_NONE)
    if not ctx.get("imap_configured"):
        tone, label = TONE_NONE, "не налаштовано"
    else:
        label = {
            TONE_OK: "працює",
            TONE_WARN: "немає відповіді",
            TONE_ALARM: "помилка входу",
        }.get(tone, "очікує перевірки")

    meters = [
        Meter(k="Синк пошти", v=_clean(hb["label"]) or "невідомо", s="фоновий, кожні 2 хв", tone=tone),
        Meter(
            k="Пароль",
            v="збережено" if (ctx.get("values_set") or {}).get("imap_password") else "не задано",
            s="зашифровано в базі",
            tone=TONE_OK if (ctx.get("values_set") or {}).get("imap_password") else TONE_WARN,
        ),
    ]
    nodes = {n["l"]: n for n in (ctx.get("state_nodes") or [])}
    if "Пошта" in nodes:
        meters.append(Meter(k="У тріажі", v=str(nodes["Пошта"]["n"]), s="листів чекають рішення"))
    meters.append(
        Meter(
            k="Скачування вкладень",
            v="усі підряд" if ctx.get("mail_download_all") else "лише довірені",
            s="глобальний режим",
        )
    )
    return Slab(tone=tone, label=label, meters=meters)


def _slab_paths(ctx: dict) -> Slab:
    """Шляхи: «задано» ≠ «існує». Зелений тут не ставимо ніколи.

    Існування мережевої теки коштує похід у шару, і робити його на кожному
    рендері сторінки не можна (див. CLAUDE.md §14, N+1 на мережевій шарі).
    Тому тон — сірий/жовтий, а перевірку робить кнопка-проба поруч із полем.
    """
    values = ctx.get("values") or {}
    filled = [k for k in ("export_folder_path", "technician_files_path") if (values.get(k) or "").strip()]
    if len(filled) == 2:
        tone, label = TONE_NONE, "обидві теки задано"
    elif filled:
        tone, label = TONE_WARN, "задано лише одну теку"
    else:
        tone, label = TONE_WARN, "теки не задано"

    def mark(key: str) -> Meter:
        titles = {
            "export_folder_path": ("Тека export", "звідки беруться файли на видачу"),
            "technician_files_path": ("Файли техніків", "куди технік кладе роботу"),
            "sum3d_projects_path": ("Проєкти Sum3D", "необов'язково"),
        }
        title, sub = titles[key]
        val = (values.get(key) or "").strip()
        return Meter(k=title, v="задано" if val else "не задано", s=sub, tone=TONE_NONE if val else TONE_WARN)

    meters = [mark("export_folder_path"), mark("technician_files_path"), mark("sum3d_projects_path")]
    meters.append(
        Meter(
            k="Початок робочої доби",
            v=(values.get("day_rollover_time") or "07:30"),
            s="«сьогодні» рахується від цього часу",
        )
    )
    return Slab(tone=tone, label=label, meters=meters)


def _slab_operators(ctx: dict) -> Slab:
    operators = ctx.get("operators") or []
    total = len(operators)
    active = sum(1 for u in operators if getattr(u, "is_active", True))
    admins = sum(1 for u in operators if getattr(u, "role", "") == "адмін" and getattr(u, "is_active", True))
    if admins == 0:
        tone, label = TONE_ALARM, "немає активного адміністратора"
    elif active == 0:
        tone, label = TONE_ALARM, "усі акаунти вимкнені"
    else:
        tone, label = TONE_OK, f"{active} {_pl(active, 'активний', 'активні', 'активних')}"
    meters = [
        Meter(k="Акаунтів", v=str(total), s=_pl(total, "акаунт", "акаунти", "акаунтів")),
        Meter(k="Активних", v=str(active), s="можуть увійти", tone=TONE_OK if active else TONE_ALARM),
        Meter(k="Адміністраторів", v=str(admins), s="бачать секрети", tone=TONE_OK if admins else TONE_ALARM),
        Meter(
            k="ПІН «Виробітку»",
            v="задано" if ctx.get("vyrobitok_pin_set") else "не задано",
            s="код доступу до розділу",
            tone=TONE_OK if ctx.get("vyrobitok_pin_set") else TONE_NONE,
        ),
    ]
    return Slab(tone=tone, label=label, meters=meters)


def _slab_sections(ctx: dict) -> Slab:
    rows = ctx.get("sections_admin") or []
    total = len(rows)
    # `sections_admin()` віддає СЛОВНИКИ, а не обʼєкти: `getattr(s, "is_open")`
    # завжди повертав дефолт `True`, тож плита рахувала нуль закритих і писала
    # «усі відкриті» навіть тоді, коли розділ був зачинений (знайдено 10.09.26).
    closed = sum(1 for s in rows if not s.get("is_open", True))
    if closed:
        tone, label = TONE_WARN, f"{closed} {_pl(closed, 'розділ закрито', 'розділи закрито', 'розділів закрито')}"
    else:
        tone, label = TONE_OK, "усі відкриті"
    return Slab(
        tone=tone,
        label=label,
        meters=[
            Meter(k="Розділів", v=str(total), s="під керуванням"),
            Meter(k="Закрито", v=str(closed), s="видно лише адміну", tone=TONE_WARN if closed else TONE_OK),
            Meter(k="Відкрито", v=str(total - closed), s="видно всім", tone=TONE_OK),
        ],
    )


def _slab_backup(ctx: dict) -> Slab:
    """Тон беремо з ДРУГОЇ копії, а не з кількості знімків.

    Раніше плита зеленіла від того, що в теці лежать файли. Але файли лежать
    там з минулого року й тоді, коли копіювання давно падає, — тобто зелений
    показував «колись працювало», а не «працює». Гірше: усі знімки лежать на
    тому самому диску, що й база, тож у найважливішому сценарії (диск помер)
    їх немає разом із нею. Тому головне питання плити тепер — «чи є копія
    ПОЗА цим диском» (аудит 08.09.26, правило §14: колір лише з
    підтвердженого сигналу).
    """
    snaps = ctx.get("monthly_snapshots") or []
    total = len(snaps)
    size = round(sum(float(s.get("size_mb") or 0) for s in snaps), 1)

    mirror = ctx.get("backup_mirror") or {}
    mirror_on = bool(mirror.get("dir"))
    mirror_error = (mirror.get("last_error") or "").strip()
    mirror_ok = (mirror.get("last_ok") or "").strip()

    if not total:
        tone, label = TONE_WARN, "жодного знімка"
    elif not mirror_on:
        # Не помилка, а незакритий ризик: копії є, але всі на диску бази.
        tone, label = TONE_WARN, "лише на диску бази"
    elif mirror_error:
        tone, label = TONE_WARN, "друга копія не вдалася"
    elif mirror_ok:
        tone, label = TONE_OK, f"{total} {_pl(total, 'знімок', 'знімки', 'знімків')} + друга копія"
    else:
        # Дзеркало щойно ввімкнули, першої копії ще не було.
        tone, label = TONE_WARN, "друга копія ще не робилась"

    if not mirror_on:
        mirror_meter = Meter(k="Друга копія", v="вимкнено", s="усе на диску бази", tone=TONE_WARN)
    elif mirror_error:
        mirror_meter = Meter(k="Друга копія", v="збій", s=mirror_error[:60], tone=TONE_WARN)
    elif mirror_ok:
        mirror_meter = Meter(k="Друга копія", v=mirror_ok[:10], s="останній успіх", tone=TONE_OK)
    else:
        mirror_meter = Meter(k="Друга копія", v="очікує", s="ще не робилась", tone=TONE_WARN)

    return Slab(
        tone=tone,
        label=label,
        meters=[
            Meter(k="Місячних знімків", v=str(total), s="автоматичних", tone=TONE_OK if total else TONE_WARN),
            Meter(k="Разом", v=f"{size} МБ", s="на диску"),
            mirror_meter,
        ],
    )


def _slab_sheet_backup(ctx: dict) -> Slab:
    on = bool(ctx.get("sheet_backup_enabled"))
    total = ctx.get("sheet_snapshot_total") or 0
    gone = ctx.get("sheet_snapshot_disappeared") or 0
    if not on:
        tone, label = TONE_WARN, "автознімання вимкнено"
    elif total:
        tone, label = TONE_OK, "знімається автоматично"
    else:
        tone, label = TONE_WARN, "увімкнено, знімків ще немає"
    meters = [
        Meter(k="Автознімання", v="увімкнено" if on else "вимкнено", s="CSV по вкладках", tone=TONE_OK if on else TONE_WARN),
        Meter(k="Період", v=f"{ctx.get('sheet_backup_interval_hours') or '—'} год", s="між знімками"),
        Meter(k="Знімків", v=str(total), s="усього збережено", tone=TONE_OK if total else TONE_NONE),
    ]
    if gone:
        meters.append(Meter(k="Зниклих вкладок", v=str(gone), s="є лише в копії", tone=TONE_WARN))
    return Slab(tone=tone, label=label, meters=meters)


def _slab_mail_download(ctx: dict) -> Slab:
    report = ctx.get("spool_report")
    all_mode = bool(ctx.get("mail_download_all"))
    dirs = getattr(report, "total_dirs", 0) or 0
    mb = getattr(report, "total_mb", 0) or 0
    prunable = len(getattr(report, "prunable_dirs", []) or [])
    prunable_mb = getattr(report, "prunable_mb", 0) or 0
    tone = TONE_NONE
    label = "усі підряд" if all_mode else "лише довірені"
    meters = [
        Meter(k="Режим", v=label, s="кого качаємо автоматично"),
        Meter(k="Тек у спулі", v=str(dirs), s="тимчасові вкладення"),
        Meter(k="Обсяг", v=f"{mb} МБ", s="можна прибрати"),
    ]
    if prunable:
        meters.append(
            Meter(k="Можна прибрати", v=f"{prunable_mb} МБ", s=f"{prunable} тек без листа", tone=TONE_WARN)
        )
        # Жовтий тут — не про режим (обидва режими нормальні), а про сміття
        # на диску. Тому й підпис міняється: інакше «лише довірені» світилося б
        # попередженням, наче це помилка налаштування.
        tone, label = TONE_WARN, f"є що прибрати · {prunable_mb} МБ"
    return Slab(tone=tone, label=label, meters=meters)


def _slab_mail_filters(ctx: dict) -> Slab:
    rules = ctx.get("filter_rules") or []
    cats = ctx.get("filter_category_rows") or ctx.get("filter_categories") or []
    hits = sum(int(getattr(r, "hits", 0) or 0) for r in rules)
    total = len(rules)
    tone = TONE_OK if total else TONE_NONE
    return Slab(
        tone=tone,
        label=f"{total} {_pl(total, 'правило', 'правила', 'правил')}" if total else "правил немає",
        meters=[
            Meter(k="Правил", v=str(total), s="ловлять «не наші» листи", tone=tone),
            Meter(k="Категорій", v=str(len(cats)), s="куди розкладати"),
            Meter(k="Спрацювань", v=str(hits), s="за весь час"),
        ],
    )


def _slab_furnaces(db: Session, ctx: dict) -> Slab:
    cards = furnace_service.snapshot(db)
    total = len(cards)
    if not total:
        return Slab(tone=TONE_NONE, label="печей не додано", meters=[])
    broken = sum(1 for c in cards if c.has_problem)
    with_data = sum(1 for c in cards if c.has_data)
    running = sum(1 for c in cards if c.is_running)
    if broken:
        tone, label = TONE_ALARM, f"{broken} без зв'язку"
    elif with_data == 0:
        tone, label = TONE_WARN, "жодного кадру"
    else:
        tone, label = TONE_OK, f"{with_data} з {total} на звʼязку"
    return Slab(
        tone=tone,
        label=label,
        meters=[
            Meter(k="Печей", v=f"{with_data} / {total}", s="віддають кадр", tone=TONE_OK if with_data else TONE_WARN),
            Meter(k="У роботі", v=str(running), s="програма йде"),
            Meter(k="Без зв'язку", v=str(broken), s="перевір ПК печі", tone=TONE_ALARM if broken else TONE_OK),
            Meter(
                k="Спільний пароль",
                v="збережено" if ctx.get("furnace_password_set") else "не задано",
                s="для печей без власного",
                tone=TONE_OK if ctx.get("furnace_password_set") else TONE_NONE,
            ),
        ],
    )


def _slab_machines(db: Session, ctx: dict) -> Slab:
    cards = machines_service.snapshot(db)
    total = len(cards)
    if not total:
        return Slab(tone=TONE_NONE, label="верстатів не додано", meters=[])
    broken = sum(1 for c in cards if c.has_problem)
    with_frame = sum(1 for c in cards if c.has_frame and not c.has_problem)
    running = sum(1 for c in cards if c.is_running)
    agents = sum(1 for c in cards if getattr(c.target, "is_agent", False))
    if broken:
        tone, label = TONE_ALARM, f"{broken} без зв'язку"
    elif with_frame == 0:
        tone, label = TONE_WARN, "жодного кадру"
    else:
        tone, label = TONE_OK, f"{with_frame} з {total} на звʼязку"
    calib = ctx.get("calibration") or {}
    meters = [
        Meter(k="Верстатів", v=f"{with_frame} / {total}", s="віддають кадр", tone=TONE_OK if with_frame else TONE_WARN),
        Meter(k="Фрезерують", v=str(running), s="є свіжий відсоток"),
        Meter(k="Без зв'язку", v=str(broken), s="перевір ПК верстата", tone=TONE_ALARM if broken else TONE_OK),
        Meter(k="Через агент", v=f"{agents} / {total}", s="бачать смугу %"),
    ]
    frames = calib.get("frames") if isinstance(calib, dict) else None
    if frames is not None:
        meters.append(Meter(k="Калібрувальних кадрів", v=str(frames), s="зібрано для навчання"))
    return Slab(tone=tone, label=label, meters=meters[:4])


# Коди зберігання → людські підписи. У плиті стоїть те саме, що бачить
# оператор у формі нижче: «glass» і «tc» на екрані нічого не означають.
_NOTIFY_STYLE = {"glass": "скляна", "card": "картка", "aurora": "аврора"}
_NOTIFY_POS = {
    "tc": "зверху по центру",
    "tr": "зверху справа",
    "br": "знизу справа",
    "bl": "знизу зліва",
}


def _slab_notifications(ctx: dict) -> Slab:
    """Тут немає «здоров'я» — лише конфігурація. Тон завжди нейтральний."""
    events = ctx.get("notify_events") or []
    allev = ctx.get("notify_all") or []
    return Slab(
        tone=TONE_NONE,
        label=f"{len(events)} з {len(allev)} подій",
        meters=[
            Meter(k="Стиль", v=_NOTIFY_STYLE.get(ctx.get("notify_style"), "—"), s="вигляд спливайки"),
            Meter(k="Позиція", v=_NOTIFY_POS.get(ctx.get("notify_position"), "—"), s="кут екрана"),
            Meter(k="Подій", v=f"{len(events)} / {len(allev)}", s="про що сповіщати"),
        ],
    )


def _slab_about(ctx: dict) -> Slab:
    changelog = ctx.get("changelog") or []
    version = VERSION
    nodes = {n["l"]: n for n in (ctx.get("state_nodes") or [])}
    meters = [Meter(k="Версія", v=version, s="встановлена збірка", tone=TONE_OK)]
    if changelog:
        meters.append(Meter(k="Релізів у журналі", v=str(len(changelog)), s="історія змін"))
    if "Черга" in nodes:
        meters.append(Meter(k="У черзі", v=str(nodes["Черга"]["n"]), s="активних робіт"))
    if "Архів" in nodes:
        meters.append(Meter(k="В архіві", v=str(nodes["Архів"]["n"]), s="робіт за весь час"))
    return Slab(tone=TONE_NONE, label=f"v{version}", meters=meters)


def _slab_handout(ctx: dict) -> Slab:
    """Видача: правила процесу. Увімкнений чеклист — факт, а не «зелено»:
    плита не має підтвердженого сигналу, тож тон завжди нейтральний."""
    on = bool(ctx.get("handout_qc"))
    return Slab(
        tone=TONE_NONE,
        label="QC-чеклист увімкнено" if on else "«знайдено» в один клік",
        meters=[
            Meter(
                k="QC перед «знайдено»",
                v="увімкнено" if on else "вимкнено",
                s="три звірки на кожну роботу" if on else "один клік (CLAUDE.md §2)",
            ),
        ],
    )


# Ключ розділу → як зібрати його плиту. Словник, а не тіло функції, бо плиту
# треба вміти зібрати й ПООДИНЦІ: «Сповіщення» живуть тепер у кабінеті
# (/account), і тягнути туди снапшоти печей заради однієї плити не варто.
_SLAB_BUILDERS = {
    "state": lambda db, ctx: _slab_state(ctx),
    "notifications": lambda db, ctx: _slab_notifications(ctx),
    "sheets": _slab_sheets,
    "operators": lambda db, ctx: _slab_operators(ctx),
    "sections": lambda db, ctx: _slab_sections(ctx),
    "backup": lambda db, ctx: _slab_backup(ctx),
    "sheet-backup": lambda db, ctx: _slab_sheet_backup(ctx),
    "imap": lambda db, ctx: _slab_imap(ctx),
    "paths": lambda db, ctx: _slab_paths(ctx),
    "mail-download": lambda db, ctx: _slab_mail_download(ctx),
    "mail-filters": lambda db, ctx: _slab_mail_filters(ctx),
    "furnaces": _slab_furnaces,
    "machines": _slab_machines,
    "handout": lambda db, ctx: _slab_handout(ctx),
    "update": lambda db, ctx: _slab_about(ctx),
}


def build_slabs(db: Session, ctx: dict) -> dict[str, Slab]:
    """Плити для всіх розділів. Ключ = `data-sec` секції в шаблоні.

    Печі й верстати читають ПАМʼЯТЬ процесу (`snapshot`), а не мережу — до
    пристрою з потоку запиту не ходимо, інакше мовчазний ПК тримав би
    налаштування двадцять секунд.
    """
    return {key: build(db, ctx) for key, build in _SLAB_BUILDERS.items()}


def build_slab(key: str, db: Session, ctx: dict) -> Optional[Slab]:
    """Одна плита за ключем — для екранів поза /settings."""
    build = _SLAB_BUILDERS.get(key)
    return build(db, ctx) if build else None


__all__ = ["Meter", "Slab", "build_slabs", "build_slab", "TONE_OK", "TONE_WARN", "TONE_ALARM", "TONE_NONE"]
