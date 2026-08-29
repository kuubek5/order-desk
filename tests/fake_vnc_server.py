"""Фейкова піч: мінімальний RFB-сервер, який віддає заданий кадр.

Навіщо цілий сервер у тестах. Знімок екрана печі — єдине джерело даних, і між
нами й числами лежить чужа бібліотека (asyncvnc), рукостискання RFB, DES-автен-
тифікація і розбір сирого фреймбуфера. Мок на `capture()` перевіряє все, КРІМ
цього шматка — тобто саме того, що найімовірніше зламається при оновленні
залежностей (окремо гостро: `cryptography` переносить TripleDES у `decrepit`,
а asyncvnc імпортує його зі старого місця; коли приберуть — впаде саме тут).

Сервер уміє ще одну річ, якої мок не вміє: він ЗАПИСУЄ, чи прилетів від нас
хоч один байт вводу. Це перетворює обіцянку «застосунок ніколи не керує піччю»
з коментаря на перевірку.

Реалізовано рівно стільки протоколу, скільки треба (RFB 3.8, VNC-автентифікація,
кодування Raw) — це стенд, а не VNC-сервер.
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field
from typing import Optional

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

# Виклик, який сервер шле клієнту. Фіксований: у тесті випадковість тільки
# заважає відтворити збій.
CHALLENGE = bytes(range(16))

# 32bpp, depth 24, little-endian, true colour, максимуми 255, зсуви r0/g8/b16.
# Саме цей формат asyncvnc знає як «rgba» і тому НЕ шле SetPixelFormat у
# відповідь — байти кадру йдуть так, як ми їх приготували.
PIXEL_FORMAT = b"\x20\x18\x00\x01\x00\xff\x00\xff\x00\xff\x00\x08\x10" + b"\x00\x00\x00"

# Довжини клієнтських повідомлень RFB 3.8 (без байта типу).
_MESSAGE_TAILS = {
    0: 19,  # SetPixelFormat
    4: 7,   # KeyEvent
    5: 5,   # PointerEvent
}


def vnc_response(password: str, challenge: bytes = CHALLENGE) -> bytes:
    """Відповідь на виклик за правилами VNC: DES із «дзеркальним» ключем.

    Біти кожного байта пароля перевертаються — історична особливість VNC, через
    яку звичайний DES тут не підходить. Та сама схема, що в asyncvnc; тримаємо
    її окремо, щоб перевіряти автентифікацію, а не вірити їй на слово.
    """
    key = password.encode("ascii")[:8].ljust(8, b"\x00")
    key = bytes(int(bin(n)[:1:-1].ljust(8, "0"), 2) for n in key)
    encryptor = Cipher(algorithms.TripleDES(key), modes.ECB()).encryptor()
    return encryptor.update(challenge) + encryptor.finalize()


@dataclass
class FakeFurnace:
    """Стенд однієї печі.

    frame_rgba — байти кадру в порядку R,G,B,A. Змінюється на льоту: так тест
    показує «піч перемкнулась з RUN у WAIT», не перепідключаючись.
    """

    width: int
    height: int
    frame_rgba: bytes
    password: str = "DEKEMA"
    # Не відповідати на запит кадру — імітація напівживого сокета, заради якого
    # у знімку взагалі є дедлайн.
    hang: bool = False
    # Відхиляти автентифікацію, навіть якщо пароль правильний.
    reject_auth: bool = False

    connections: int = 0
    shared_flags: list[int] = field(default_factory=list)
    bad_passwords: int = 0
    # Головний свідок: сюди потрапляє тип будь-якого повідомлення вводу.
    input_events: list[int] = field(default_factory=list)

    _server: Optional[asyncio.AbstractServer] = None
    _loop: Optional[asyncio.AbstractEventLoop] = None
    _thread: Optional[threading.Thread] = None
    port: int = 0

    # ── життєвий цикл ───────────────────────────────────────────────────────

    def start(self) -> "FakeFurnace":
        ready = threading.Event()

        def _run() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop

            async def _serve() -> None:
                self._server = await asyncio.start_server(self._client, "127.0.0.1", 0)
                self.port = self._server.sockets[0].getsockname()[1]
                ready.set()
                async with self._server:
                    await self._server.serve_forever()

            try:
                loop.run_until_complete(_serve())
            except asyncio.CancelledError:
                pass
            finally:
                # Прибрати за собою повністю. Інакше клієнт, який «завис»
                # (стенд для перевірки дедлайну), лишається живою корутиною, а
                # транспорти доживають до збирача сміття вже після закритого
                # циклу — pytest показує це як помилку там, де її немає.
                pending = asyncio.all_tasks(loop)
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True)
                    )
                loop.run_until_complete(asyncio.sleep(0.05))
                loop.run_until_complete(loop.shutdown_asyncgens())
                loop.close()

        self._thread = threading.Thread(target=_run, name="fake-furnace", daemon=True)
        self._thread.start()
        if not ready.wait(timeout=5):
            raise RuntimeError("фейкова піч не піднялась")
        return self

    def stop(self) -> None:
        if self._loop is not None and self._server is not None:
            self._loop.call_soon_threadsafe(self._server.close)
        if self._thread is not None:
            self._thread.join(timeout=2)

    def __enter__(self) -> "FakeFurnace":
        return self.start()

    def __exit__(self, *_exc) -> None:
        self.stop()

    # ── протокол ────────────────────────────────────────────────────────────

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        try:
            writer.write(b"RFB 003.008\n")
            await reader.readexactly(12)

            writer.write(bytes([1, 2]))  # один тип безпеки: VNC-автентифікація
            chosen = await reader.readexactly(1)
            if chosen != b"\x02":
                writer.write(b"\x00\x00\x00\x01")
                await writer.drain()
                return

            writer.write(CHALLENGE)
            response = await reader.readexactly(16)
            ok = response == vnc_response(self.password) and not self.reject_auth
            if not ok:
                self.bad_passwords += 1
                writer.write(b"\x00\x00\x00\x01")
                # RFB 3.8: причина відмови текстом, інакше клієнт читає сміття.
                reason = "неправильний пароль".encode("utf-8")
                writer.write(len(reason).to_bytes(4, "big") + reason)
                await writer.drain()
                return
            writer.write(b"\x00\x00\x00\x00")

            shared = await reader.readexactly(1)
            self.shared_flags.append(shared[0])

            name = b"fake furnace"
            writer.write(
                self.width.to_bytes(2, "big")
                + self.height.to_bytes(2, "big")
                + PIXEL_FORMAT
                + len(name).to_bytes(4, "big")
                + name
            )
            await writer.drain()

            while True:
                header = await reader.readexactly(1)
                message = header[0]
                if message == 2:  # SetEncodings
                    padding_and_count = await reader.readexactly(3)
                    count = int.from_bytes(padding_and_count[1:], "big")
                    await reader.readexactly(4 * count)
                elif message == 3:  # FramebufferUpdateRequest
                    await reader.readexactly(9)
                    if self.hang:
                        await asyncio.sleep(3600)
                    self._write_frame(writer)
                    await writer.drain()
                elif message == 6:  # ClientCutText — теж ввід, теж свідчення
                    tail = await reader.readexactly(7)
                    await reader.readexactly(int.from_bytes(tail[3:], "big"))
                    self.input_events.append(message)
                elif message in _MESSAGE_TAILS:
                    await reader.readexactly(_MESSAGE_TAILS[message])
                    if message in (4, 5):
                        self.input_events.append(message)
                else:
                    return
        except (asyncio.IncompleteReadError, ConnectionError, asyncio.CancelledError):
            pass
        finally:
            self.connections -= 1
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass

    def _write_frame(self, writer: asyncio.StreamWriter) -> None:
        writer.write(
            b"\x00\x00"  # FramebufferUpdate + padding
            + (1).to_bytes(2, "big")  # один прямокутник
            + (0).to_bytes(2, "big")
            + (0).to_bytes(2, "big")
            + self.width.to_bytes(2, "big")
            + self.height.to_bytes(2, "big")
            + (0).to_bytes(4, "big")  # кодування Raw
            + self.frame_rgba
        )


def frame_bytes(image) -> bytes:
    """PIL-зображення → сирі байти R,G,B,A для кодування Raw."""
    return image.convert("RGBA").tobytes()
