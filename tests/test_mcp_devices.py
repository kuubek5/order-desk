"""Пристрої через MCP: список, кадр картинкою, розбір зон.

`tests/test_mcp.py` тримає протокол і зрізи бази. Тут інше: три інструменти,
які віддають КАРТИНКУ й пояснення, чому число не прочиталось. Що ламається
саме тут, і кожна поломка тиха:

* **«Кадру немає» ≠ «кадр старий».** Сховище — один файл на пристрій, історії
  не існує, тож відповідь мусить нести вік кадру завжди. Розбір учорашньої
  картинки як сьогоднішньої — найдорожча помилка цього інструмента.
* **Картинка не має лізти в текст.** `_image` виймається з навантаження
  (`app/routers/mcp.py::_tool_result`) і їде окремим блоком `image`. Лишився б
  у `structuredContent` — кожен виклик віз би сотні кілобайт base64 ДВІЧІ, і
  помітно це стало б не з тесту, а з розчавленого контексту.
* **Невідомий ключ і невідома зона — відповідь із переліком, а не виняток.**
  Виняток тут означав би «інструмент упав», тобто того, хто питає, відкинуло б
  на початок замість того, щоб показати, що саме написати.
* **Порожнє поле мусить мати ПРИЧИНУ.** Правило §14 «цифра або збігається з
  еталоном, або поле порожнє» лишається за читачем; інструмент зобовʼязаний
  лише сказати, котра з причин спрацювала — інакше кожне «донавчи цифру»
  знову впирається в кадр.

Кадри пишуться тими самими `save_frame`, якими пише опитувач: файл, покладений
руками повз них, доводив би лише сам себе.
"""

from __future__ import annotations

import base64
import io
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import furnace_ocr
from app.models import Furnace, Machine
from app.services import furnace as furnace_service
from app.services import machines as machines_service
from app.services import mcp_tools
from tests.asgi_client import MiniClient
from tests.test_mcp import _call, _json, _tool  # noqa: F401 — той самий шлях до /mcp
from tests.test_settings_slabs_render import app_db  # noqa: F401 — фікстура

FIXTURES = Path(__file__).parent / "fixtures" / "furnace"

FURNACE_HOST = "192.0.2.10"
MACHINE_HOST = "192.0.2.20"
# Порт не типовий — тоді ключ верстата складений (`host-port`), і тест заразом
# доводить, що інструмент бере його з тієї самої властивості `target.key`, а
# не зліплює сам.
MACHINE_PORT = 8765
FURNACE_KEY = FURNACE_HOST
MACHINE_KEY = f"{MACHINE_HOST}-{MACHINE_PORT}"


def _real_frame():
    """Справжній кадр печі 800×600 — той самий, на якому вчиться читач."""
    from PIL import Image

    return Image.open(FIXTURES / "run.png").convert("RGB")


def _blank_panel():
    """Кадр правильного розміру, на якому НЕМАЄ чорнила жодного виду.

    Сірий 128: сума каналів 384 — це ні «світле» (>450), ні «темне» (<300), ні
    «червоне» (`app/furnace_ocr.py::INKS`). Розмір панельний, тож читач не
    відмовиться через роздільність і дійде саме туди, куди треба: до зон, у
    яких нічого не знайдено.
    """
    from PIL import Image

    return Image.new("RGB", furnace_ocr.PANEL_SIZE, (128, 128, 128))


@pytest.fixture
def bench(app_db, tmp_path, monkeypatch):  # noqa: F811
    """Піч і верстат у базі + власні теки кадрів на кожен вид пристрою.

    Теки РІЗНІ навмисно: спільна сховала б переплутані `frames_root()` —
    кадр печі лежав би там, де його шукає верстат, і обидва інструменти
    виглядали б справними.
    """
    app, session_factory = app_db

    furnace_root = tmp_path / "furnace_frames"
    machine_root = tmp_path / "machine_frames"
    furnace_root.mkdir()
    machine_root.mkdir()
    monkeypatch.setattr(furnace_service, "frames_root", lambda: furnace_root)
    monkeypatch.setattr(machines_service, "frames_root", lambda: machine_root)

    # Стан пристроїв живе в памʼяті процесу й переживає тест — піч, «побачена»
    # сусіднім файлом, інакше залишилась би видимою тут.
    furnace_service.reset_state_for_tests()
    machines_service.reset_state_for_tests()

    with session_factory() as db:
        db.add(
            Furnace(
                name="Піч 1",
                host=FURNACE_HOST,
                port=5900,
                enabled=True,
                sort_order=0,
                created_at=datetime.now(),
            )
        )
        db.add(
            Machine(
                name="Coritec 250i",
                host=MACHINE_HOST,
                port=MACHINE_PORT,
                enabled=True,
                sort_order=0,
                created_at=datetime.now(),
            )
        )
        db.commit()

    yield SimpleNamespace(
        app=app,
        client=MiniClient(app),
        session_factory=session_factory,
        furnace_root=furnace_root,
        machine_root=machine_root,
    )

    furnace_service.reset_state_for_tests()
    machines_service.reset_state_for_tests()


def _structured(bench_, name: str, args: dict | None = None) -> dict:
    payload = _tool(bench_.client, name, args)
    assert "error" not in payload, payload
    return payload["result"]["structuredContent"]


def _image_bytes(payload: dict) -> bytes:
    """Картинка з ДРУГОГО блоку відповіді — саме там, де її чекає клієнт."""
    content = payload["result"]["content"]
    assert len(content) == 2, content
    assert content[1]["type"] == "image", content[1]
    return base64.b64decode(content[1]["data"])


def _opened(data: bytes):
    from PIL import Image

    return Image.open(io.BytesIO(data))


# ── kmill_devices ───────────────────────────────────────────────────────────


def test_devices_lists_both_kinds_and_admits_there_is_no_frame(bench):
    """Пристрій без жодного кадру мусить бути В СПИСКУ й чесно казати «немає».

    Зникнути зі списку він не може: саме «піч є в налаштуваннях, а кадру з неї
    нема жодного» — це стан, заради якого в інструмент і йдуть.
    """
    data = _structured(bench, "kmill_devices")

    assert [f["ключ"] for f in data["печі"]] == [FURNACE_KEY]
    assert [m["ключ"] for m in data["верстати"]] == [MACHINE_KEY]
    assert data["печі"][0]["назва"] == "Піч 1"
    assert data["верстати"][0]["адреса"] == f"{MACHINE_HOST}:{MACHINE_PORT}"
    # Кадру на диску немає — жодних «байтів» і «вік_с», лише ознака.
    assert data["печі"][0]["кадр"] == {"є": False}
    assert data["верстати"][0]["кадр"] == {"є": False}
    assert data["нерозпізнані_екрани"] == []


def test_devices_report_frame_size_and_age_from_the_file(bench):
    """Вік і розмір беруться з ФАЙЛУ, а не зі стану процесу.

    Стан порожній (застосунок щойно піднявся), а файл лежить — і саме тоді
    «кадру немає» було б неправдою.
    """
    saved = furnace_service.save_frame(FURNACE_KEY, _real_frame())

    frame_facts = _structured(bench, "kmill_devices")["печі"][0]["кадр"]
    assert frame_facts["є"] is True
    assert frame_facts["байтів"] == saved.stat().st_size
    assert Path(frame_facts["шлях"]) == saved
    # Вік — ціле число секунд, не None і не відʼємне.
    assert isinstance(frame_facts["вік_с"], int)
    assert frame_facts["вік_с"] >= 0
    assert frame_facts["знято"]


# ── kmill_frame ─────────────────────────────────────────────────────────────


def test_frame_with_unknown_key_answers_with_the_list_of_keys(bench):
    """Помилка аргументу — ВІДПОВІДЬ із переліком, а не виняток.

    Виняток приїхав би як «інструмент упав», і з такої відповіді не видно, що
    саме написати замість помилкового ключа.
    """
    payload = _tool(bench.client, "kmill_frame", {"key": "192.0.2.99"})
    assert "error" not in payload, payload
    data = payload["result"]["structuredContent"]
    assert "помилка" in data
    assert data["відомі_ключі"] == [FURNACE_KEY, MACHINE_KEY]
    # Перелік має сенс лише тоді, коли ключі в ньому видно текстом.
    assert MACHINE_KEY in payload["result"]["content"][0]["text"]


def test_frame_returns_the_whole_png_byte_for_byte_readable(bench):
    """Картинка мусить доїхати ЦІЛОЮ й розпакованою в PNG того ж розміру.

    Кадр печі важить ~20 КБ і в межу влазить, тож зменшення тут не повинно
    відбуватись: на повній роздільності тримається піксельна звірка з
    еталонами, а зменшений кадр відповідав би на «що там написано» здогадкою.
    """
    original = _real_frame()
    furnace_service.save_frame(FURNACE_KEY, original)

    payload = _tool(bench.client, "kmill_frame", {"key": FURNACE_KEY})
    image = _opened(_image_bytes(payload))
    assert image.format == "PNG"
    assert image.size == original.size == furnace_ocr.PANEL_SIZE

    data = payload["result"]["structuredContent"]
    assert data["показано"] == "весь кадр"
    assert data["розмір"] == "800×600"
    assert data["примітки"] == []  # нічого не зменшували


def test_frame_zone_crop_matches_the_rectangle_exactly(bench):
    """Виріз зони — РІВНО прямокутник із `ZONES`, без полів і масштабування.

    Зсунутий чи розтягнутий виріз виглядав би правдоподібно, а відповідав би
    на питання «чому число не прочиталось» картинкою іншої ділянки табло.
    """
    furnace_service.save_frame(FURNACE_KEY, _real_frame())
    left, top, right, bottom = furnace_ocr.ZONES["temp"].rect

    payload = _tool(bench.client, "kmill_frame", {"key": FURNACE_KEY, "zone": "temp"})
    got = _opened(_image_bytes(payload)).convert("RGB")
    assert got.size == (right - left, bottom - top)
    # І це та сама ділянка того самого файлу піксель-у-піксель, а не картинка
    # потрібного розміру: розмір сам по собі підтвердив би й порожній кроп.
    assert got.tobytes() == _real_frame().crop((left, top, right, bottom)).tobytes()

    data = payload["result"]["structuredContent"]
    assert data["показано"] == "temp"
    # Розмір лишається розміром УСЬОГО кадру: інакше «800×600» поїхало б услід
    # за вирізом, і зона виглядала б цілим екраном.
    assert data["розмір"] == "800×600"
    assert str(list(furnace_ocr.ZONES["temp"].rect)) in data["примітки"][0]


def test_frame_refuses_a_bogus_zone_and_a_zone_on_a_machine(bench):
    """Дві різні відмови, і плутати їх не можна.

    Невідома зона лікується правильною назвою — тому в відповіді перелік.
    Зона для ВЕРСТАТА не лікується ніяк: іменованих зон там немає, кадр
    віддається цілим, і сказати це треба прямо, а не перелічити зони печі.
    """
    furnace_service.save_frame(FURNACE_KEY, _real_frame())
    machines_service.save_frame(MACHINE_KEY, _real_frame())

    bogus = _structured(bench, "kmill_frame", {"key": FURNACE_KEY, "zone": "температура"})
    assert "помилка" in bogus
    assert bogus["зони"] == sorted(furnace_ocr.ZONES)
    assert "temp" in bogus["зони"] and "button" in bogus["зони"]

    on_machine = _structured(bench, "kmill_frame", {"key": MACHINE_KEY, "zone": "temp"})
    assert "іменовані зони є лише в печей" in on_machine["помилка"]
    assert on_machine["пристрій"]["вид"] == "верстат"
    assert "зони" not in on_machine


def test_image_travels_in_its_own_block_and_not_in_the_text(bench):
    """Заради цього `_image` і виймається з навантаження.

    Base64 кадру — сотні кілобайт. Якби він лишився в `structuredContent` або
    потрапив у текстовий блок, кожен виклик віз би картинку двічі, і побачили
    б це не з тесту, а з того, що контекст закінчився посеред розбору.
    """
    furnace_service.save_frame(FURNACE_KEY, _real_frame())
    result = _tool(bench.client, "kmill_frame", {"key": FURNACE_KEY})["result"]

    content = result["content"]
    assert [block["type"] for block in content] == ["text", "image"]
    assert content[1]["mimeType"] == "image/png"

    data = content[1]["data"]
    assert data, "картинка мусить бути непорожньою"
    head = data[:64]
    assert head not in content[0]["text"]
    assert "_image" not in result["structuredContent"]
    assert head not in json.dumps(result["structuredContent"], ensure_ascii=False)
    # У текстовому блоці лишається те, заради чого його читають: вік кадру.
    assert "вік_с" in content[0]["text"]


# ── kmill_zones ─────────────────────────────────────────────────────────────


def test_zones_explain_every_empty_field_instead_of_guessing(bench):
    """Порожнє поле мусить нести причину — інакше інструмент не має сенсу.

    Кадр правильного розміру, але без чорнила: читач не знайде жодного
    символу, і кожна зона зобовʼязана сказати саме це, а не «не прочиталось».
    Мовчазне порожнє поле щоразу повертало б розбір до «надішли кадр».
    """
    furnace_service.save_frame(FURNACE_KEY, _blank_panel())

    data = _structured(bench, "kmill_zones", {"key": FURNACE_KEY})
    assert data["пристрій"]["вид"] == "піч"
    assert data["розмір"] == "800×600"

    zones = {zone["зона"]: zone for zone in data["зони"]}
    assert set(zones) == {"temp", "remaining", "command", "step"}
    for name, zone in zones.items():
        assert zone["прийнято"] is False, name
        assert zone["значення"] is None, name
        assert zone["чому_ні"], name
        assert zone["прямокутник"] == list(furnace_ocr.ZONES[name].rect), name
    # Причина конкретна, а не спільна відписка: на порожньому кадрі це саме
    # «символів не знайдено», і воно ЛІКУЄТЬСЯ не тим, чим «немає еталона».
    assert "не знайдено жодного символу" in zones["temp"]["чому_ні"]
    assert data["як_рахується_статус"]


def test_tools_list_offers_all_nine_tools(bench):
    """Інструмент, не оголошений у `tools/list`, для клієнта не існує."""
    _, body = _call(bench.client, "tools/list")
    names = {tool["name"] for tool in _json(body)["result"]["tools"]}
    assert {"kmill_devices", "kmill_frame", "kmill_zones"} <= names
    assert names == set(mcp_tools.TOOLS_BY_NAME)
    assert len(names) == 9
