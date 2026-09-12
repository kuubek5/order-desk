"""Кадри й зони пристроїв для MCP — лише читання.

**Навіщо.** Вузьке місце навчання читача екранів — не алгоритм, а доступ: щоб
зрозуміти, ЧОМУ число не прочиталось, треба мати сам кадр і знати, що читач на
ньому побачив. Досі і те, і те діставалось із цехового ПК руками — скриншот у
месенджер, опис словами, — і кожне «донавчи цифру» коштувало поїздки або
дзвінка. Через MCP те саме віддається запитом: список пристроїв із віком
кадру, кадр картинкою (весь або виріз зони) і розбір зон із причиною відмови.

**Що тут НЕ робиться.** Нічого не пишеться й нічого не вчиться. Еталони
додаються лише скриптами (`scripts/furnace_glyphs.py learn`,
`scripts/newgen_glyphs.py learn`) з тестом на тому ж кадрі: один кривий еталон
псує читання назовсім, і видно це не одразу.

**Сховище кадрів — один файл на пристрій.** `save_frame` перезаписує
`<ключ>.png`, історії немає (виміряно 12.09.26: печі ≈20 КБ на кадр, верстати
до 271 КБ). Тому будь-яка відповідь звідси — про НАЙСВІЖІШИЙ кадр, і вік кадру
їде в кожній відповіді: без нього легко розбирати вчорашню картинку, думаючи,
що вона зараз.

**Ключ пристрою звіряється з налаштованими** (`configured_targets`), а не
підставляється у шлях: аргумент приходить ззовні, а `frames_root()` — звичайна
тека на диску.
"""

from __future__ import annotations

import base64
import io
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from sqlalchemy.orm import Session

from app import furnace_ocr
from app.services import furnace, machines

# Скільки байтів картинки віддаємо щонайбільше. Кадр верстата буває 271 КБ, а
# base64 додає третину; межа з запасом, і все, що більше, ЗМЕНШУЄТЬСЯ, а не
# обрізається — обрізаний кадр гірший за зменшений, бо виглядає цілим.
MAX_IMAGE_BYTES = 700_000

KIND_FURNACE = "піч"
KIND_MACHINE = "верстат"


def _local(moment: Optional[datetime]) -> Optional[str]:
    return moment.strftime("%Y-%m-%d %H:%M:%S") if moment else None


def _age_seconds(moment: Optional[datetime]) -> Optional[int]:
    if moment is None:
        return None
    return int((datetime.now() - moment).total_seconds())


def _file_facts(path: Optional[Path]) -> dict[str, Any]:
    """Час і розмір кадру з ФАЙЛУ, а не лише з памʼяті процесу.

    Стан живе в процесі й після перезапуску порожній, а файл лишається — і
    тоді «кадру немає» було б неправдою саме тоді, коли кадр найпотрібніший.
    """
    if path is None or not path.exists():
        return {"є": False}
    stat = path.stat()
    captured = datetime.fromtimestamp(stat.st_mtime)
    return {
        "є": True,
        "шлях": str(path),
        "байтів": stat.st_size,
        "знято": _local(captured),
        "вік_с": _age_seconds(captured),
    }


def _furnace_targets(db: Session) -> dict[str, Any]:
    return {target.key: target for target in furnace.configured_targets(db)}


def _machine_targets(db: Session) -> dict[str, Any]:
    return {target.key: target for target in machines.configured_targets(db)}


def _unread_screens() -> list[dict[str, Any]]:
    """Кадри «екран JOBS видно, а назву не прочитано» (`_report_unread_screen`).

    Лежать окремою текою по одному на верстат і оновлюються не частіше раза на
    годину — саме ті кадри, заради яких донавчають шрифт нового покоління.
    """
    folder = machines.frames_root() / "newgen_unread"
    if not folder.exists():
        return []
    out = []
    for png in sorted(folder.glob("*.png")):
        facts = _file_facts(png)
        facts["ключ"] = png.stem
        out.append(facts)
    return out


def devices(db: Session, args: Optional[dict] = None) -> dict[str, Any]:
    """Усі налаштовані пристрої з віком кадру й тим, що з нього прочиталось."""
    furnaces = []
    for card in furnace.snapshot(db):
        state = card.state
        furnaces.append(
            {
                "ключ": card.key,
                "назва": card.target.name,
                "адреса": f"{card.target.host}:{card.target.port}",
                "статус": card.status,
                "температура_c": state.temp_c if state else None,
                "лишилось": state.remaining_text if state else "",
                "помилка": card.problem_text if card.has_problem else None,
                "попередження": state.warnings if state else [],
                "опитано": _local(state.attempted_at) if state else None,
                "кадр": _file_facts(furnace.frame_path(card.key)),
            }
        )

    states = machines.states_snapshot()
    mills = []
    for target in machines.configured_targets(db):
        state = states.get(target.key)
        mills.append(
            {
                "ключ": target.key,
                "назва": target.name,
                "адреса": f"{target.host}:{target.port}",
                "спосіб": "агент" if target.is_agent else "VNC",
                "відсоток": state.percent if state else None,
                "завершено": bool(state and state.completed),
                "перевірка_програми": bool(state and state.validating),
                "sum3d_id": state.sum3d_id if state else None,
                "програма": state.iso_name if state else None,
                "помилка": state.error if state else None,
                "невдач_поспіль": state.fail_streak if state else None,
                "кадр": _file_facts(machines.frame_path(target.key)),
            }
        )

    return {
        "печі": furnaces,
        "верстати": mills,
        "нерозпізнані_екрани": _unread_screens(),
        "підказка": (
            "Кадр картинкою — kmill_frame, розбір зон із причиною відмови — "
            "kmill_zones. Історії кадрів немає: на кожен пристрій зберігається "
            "лише найсвіжіший, тому дивись на «вік_с»."
        ),
    }


def _resolve(db: Session, key: str) -> tuple[Optional[str], Optional[Any], Optional[Path]]:
    """Ключ → (вид пристрою, ціль, шлях до кадру). Невідомий ключ — усе None."""
    target = _furnace_targets(db).get(key)
    if target is not None:
        return KIND_FURNACE, target, furnace.frame_path(key)
    target = _machine_targets(db).get(key)
    if target is not None:
        return KIND_MACHINE, target, machines.frame_path(key)
    return None, None, None


def _unknown_key(db: Session, key: str) -> dict[str, Any]:
    return {
        "помилка": f"пристрою з ключем {key!r} серед налаштованих немає",
        "відомі_ключі": list(_furnace_targets(db)) + list(_machine_targets(db)),
    }


def _no_frame(key: str, kind: str, target: Any) -> dict[str, Any]:
    return {
        "пристрій": {"ключ": key, "вид": kind, "назва": target.name},
        "помилка": "кадру на диску ще немає — пристрій жодного разу не знявся",
    }


def _encode(image, note: list[str]) -> dict[str, str]:
    """PNG у base64, зменшений, ЯКЩО не влазить у межу.

    Зменшуємо після першої спроби, а не «про всяк випадок»: кадр печі важить
    20 КБ і мусить їхати в повній роздільності — на ній тримається піксельна
    звірка з еталонами, і зменшена картинка відповідала б на питання «що там
    написано» здогадкою.
    """
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    data = buffer.getvalue()
    while len(data) > MAX_IMAGE_BYTES and min(image.size) > 80:
        image = image.resize((image.size[0] // 2, image.size[1] // 2))
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        data = buffer.getvalue()
        note.append(
            f"кадр зменшено до {image.size[0]}×{image.size[1]} — оригінал не влазив "
            f"у межу {MAX_IMAGE_BYTES} Б"
        )
    return {"data": base64.b64encode(data).decode("ascii"), "mimeType": "image/png"}


def frame(db: Session, args: dict) -> dict[str, Any]:
    """Найсвіжіший кадр пристрою картинкою — весь або виріз названої зони."""
    from PIL import Image

    key = str(args.get("key") or "").strip()
    zone_name = str(args.get("zone") or "").strip()
    kind, target, path = _resolve(db, key)
    if kind is None:
        return _unknown_key(db, key)
    facts = _file_facts(path)
    if not facts["є"]:
        return _no_frame(key, kind, target)

    note: list[str] = []
    with Image.open(path) as opened:
        image = opened.convert("RGB")
    full_size = image.size
    if zone_name:
        if kind != KIND_FURNACE:
            return {
                "пристрій": {"ключ": key, "вид": kind, "назва": target.name},
                "помилка": "іменовані зони є лише в печей; кадр верстата віддається цілим",
            }
        zone = furnace_ocr.ZONES.get(zone_name)
        if zone is None:
            return {
                "помилка": f"зони {zone_name!r} немає",
                "зони": sorted(furnace_ocr.ZONES),
            }
        image = image.crop(zone.rect)
        note.append(f"виріз зони «{zone.title}» {list(zone.rect)}")

    return {
        "пристрій": {"ключ": key, "вид": kind, "назва": target.name},
        "кадр": facts,
        "розмір": f"{full_size[0]}×{full_size[1]}",
        "показано": zone_name or "весь кадр",
        "примітки": note,
        "_image": _encode(image, note),
    }


def _zone_report(name: str, field) -> dict[str, Any]:
    """Одна зона: що прочиталось, і якщо нічого — котра з трьох причин.

    Причини розрізняються навмисно: «обрізано» лікується переобведенням зони,
    «не впізнано» — новим еталоном, «не той шаблон» — здебільшого тим, що на
    екрані зовсім інша сторінка. Один спільний текст «не прочиталось» щоразу
    вимагав би кадру, тобто з`їдав би те, заради чого інструмент і робиться.
    """
    zone = furnace_ocr.ZONES[name]
    accepted = field.text is not None
    why = None
    if not accepted:
        if field.clipped:
            why = (
                "крайній символ торкається межі зони — число неповне; зону треба "
                "переобвести ширше"
            )
        elif field.unknown:
            why = (
                f"{field.unknown} символ(ів) не збіглися з еталоном піксель-у-піксель "
                "(у сирому рядку вони позначені «?») — донавчити: "
                "scripts/furnace_glyphs.py learn"
            )
        elif field.raw:
            why = f"рядок {field.raw!r} не тієї форми, якої вимагає шаблон {zone.pattern}"
        else:
            why = "у зоні не знайдено жодного символу (порожньо або інший екран)"
    return {
        "зона": name,
        "назва": zone.title,
        "прямокутник": list(zone.rect),
        "чорнило": zone.ink,
        "шаблон": zone.pattern,
        "сире": field.raw,
        "сегментів": field.segments,
        "не_впізнано": field.unknown,
        "обрізано": field.clipped,
        "прийнято": accepted,
        "значення": field.text,
        "чому_ні": why,
    }


def zones(db: Session, args: dict) -> dict[str, Any]:
    """Що читається з найсвіжішого кадру ЗАРАЗ і на чому читач спіткнувся."""
    from PIL import Image

    key = str(args.get("key") or "").strip()
    kind, target, path = _resolve(db, key)
    if kind is None:
        return _unknown_key(db, key)
    facts = _file_facts(path)
    if not facts["є"]:
        return _no_frame(key, kind, target)

    with Image.open(path) as opened:
        image = opened.convert("RGB")

    head: dict[str, Any] = {
        "пристрій": {"ключ": key, "вид": kind, "назва": target.name},
        "кадр": facts,
        "розмір": f"{image.size[0]}×{image.size[1]}",
    }

    if kind == KIND_FURNACE:
        reading = furnace_ocr.read_panel(image)
        head.update(
            {
                "статус": reading.status,
                "голоси": reading.signals,
                "як_рахується_статус": (
                    "потрібні ДВА згодні сигнали (слово вгорі + кнопка внизу); один "
                    "сигнал або розбіжність — «?»"
                ),
                "температура_c": reading.temp_c,
                "лишилось_с": reading.remaining_seconds,
                "попередження": reading.warnings,
                "зони": [
                    _zone_report(name, field) for name, field in reading.fields.items()
                ],
                "еталони_цифр": sorted(furnace_ocr.load_glyphs()),
            }
        )
        return head

    from app.machine_newgen_job import load_newgen_glyphs, read_newgen_program_explained

    program, why = read_newgen_program_explained(image)
    head.update(
        {
            "екран_jobs": {
                "прочитано": (
                    {"sum3d_id": program.sum3d_id, "дата": program.date} if program else None
                ),
                "чому_ні": why,
                "як_читається": (
                    "назва береться з рядка ▶ екрана JOBS і лише тоді, коли заголовок "
                    "вікна її не дав; порожнє «чому_ні» означає, що читати не було чого "
                    "(інший екран), а не відмову"
                ),
            },
            "еталони_цифр": sorted(load_newgen_glyphs()),
        }
    )
    return head
