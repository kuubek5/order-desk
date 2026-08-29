"""Знімок екрана печі по VNC. Тільки читання — жодного події вводу.

Чому це безпечно для оператора коло печі:

* RFB-хендшейк asyncvnc шле ClientInit з прапорцем shared=1, тобто наша сесія
  НЕ вибиває нікого. Перевірено наживо: піч тримає щонайменше десяток
  одночасних підключень.
* Ми ніколи не торкаємось `client.keyboard` / `client.mouse` / `client.clipboard`.
  Клавіатура й миша не «вимкнені налаштуванням» — код просто не вміє їх слати,
  і це єдина гарантія, яку не можна випадково перемкнути. Керування піччю —
  свідомо поза цим застосунком: скасувати програму на 1500 °C одним кліком це
  аварія, а не зручність.

Пароль VNC у код не потрапляє: він лежить зашифрованим у налаштуваннях
(CLAUDE.md §7) і передається сюди аргументом.

Пастка на майбутнє: VNC-автентифікація тримається на DES, а `cryptography`
переносить `TripleDES` у `decrepit` і збирається прибрати зі старого місця.
asyncvnc імпортує його зі старого. Коли колись зникне — впаде саме тут, з
AttributeError на підключенні; тому будь-яка помилка автентифікації
перекладається в зрозуміле повідомлення, а не лишається трейсбеком у логах.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from PIL import Image

logger = logging.getLogger(__name__)

DEFAULT_PORT = 5900
# Скільки чекати на весь цикл «підключитись → автентифікуватись → отримати
# кадр». Урок поштового синку: напіввідкритий сокет вішає фоновий потік
# назавжди, тож дедлайн тут обов'язковий, а не бажаний.
DEFAULT_TIMEOUT_SECONDS = 20.0


class FurnaceVncError(Exception):
    """Кадр не знято. Повідомлення призначене оператору, не логам."""


async def _grab(host: str, port: int, password: Optional[str]) -> Image.Image:
    import asyncvnc  # локальний імпорт: без екрана печей залежність не потрібна

    async with asyncvnc.connect(host, port, password=password) as client:
        pixels = await client.screenshot()
    return Image.fromarray(pixels).convert("RGB")


async def capture_async(
    host: str,
    port: int = DEFAULT_PORT,
    password: Optional[str] = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> Image.Image:
    try:
        return await asyncio.wait_for(_grab(host, port, password), timeout=timeout)
    except asyncio.TimeoutError as exc:
        raise FurnaceVncError(f"Піч {host} не відповіла за {timeout:.0f} с") from exc
    except PermissionError as exc:
        raise FurnaceVncError(f"Піч {host}: пароль VNC не підійшов") from exc
    except (OSError, ConnectionError) as exc:
        raise FurnaceVncError(f"Піч {host} недоступна: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 — фоновий воркер не має падати
        logger.exception("Знімок екрана печі %s не вдався", host)
        raise FurnaceVncError(f"Піч {host}: {exc}") from exc


def capture(
    host: str,
    port: int = DEFAULT_PORT,
    password: Optional[str] = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> Image.Image:
    """Синхронна обгортка для фонового потоку й ручної кнопки «Оновити зараз».

    Власний event loop на виклик: воркер печей — звичайний daemon-потік поруч
    із синками пошти й таблиці, а не корутина в циклі FastAPI. Так знімок ніколи
    не займає цикл, який обслуговує запити оператора.
    """
    return asyncio.run(capture_async(host, port, password, timeout))
