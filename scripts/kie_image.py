"""Згенерувати фонове зображення через kie.ai і покласти його в проєкт.

Навіщо окремий скрипт, а не разова команда: генерацію доведеться повторювати
(перший результат майже ніколи не остаточний), і кожен повтор має бути одним
рядком у консолі, а не збиранням запиту наново.

Ключ НІКОЛИ не потрапляє в командний рядок і в git: скрипт читає його з файлу
`.kie_key` у корені проєкту (він у .gitignore) або зі змінної оточення
KIE_API_KEY. Транскрипти сесій зберігають команди — тому ключ у команді це той
самий витік, лише повільніший.

Приклади:
    python scripts/kie_image.py --prompt "..." --aspect 16:9 --resolution 2K
    python scripts/kie_image.py --prompt "..." --ref https://.../furnace.png
    python scripts/kie_image.py --list-cost      # скільки коштувала остання задача

Документація: https://docs.kie.ai — createTask + recordInfo, обидва асинхронні.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KEY_FILE = ROOT / ".kie_key"
BASE = "https://api.kie.ai/api/v1/jobs"
DEFAULT_MODEL = "nano-banana-2-lite"
# Пастка, яка коштує грошей мовчки: поле референсів зветься по-різному.
# Переплутаєш — модель проігнорує референс і намалює своє за повну ціну.
REF_FIELD = {
    "nano-banana-2-lite": "image_urls",
    "nano-banana-2": "image_input",
    "nano-banana-pro": "image_input",
    "gpt-image-2-image-to-image": "input_urls",
    "kling-3.0/video": "image_urls",
}
REF_FIELD_DEFAULT = "image_input"
# Скільки чекати на картинку. Черга буває довгою, але висіти вічно скрипт не
# має права — інакше він одного дня зависне в чиємусь терміналі назавжди.
DEADLINE_SECONDS = 600
POLL_SECONDS = 5


def read_key() -> str:
    import os

    env = os.environ.get("KIE_API_KEY")
    if env:
        return env.strip()
    if KEY_FILE.exists():
        key = KEY_FILE.read_text(encoding="utf-8").strip()
        if key:
            return key
    raise SystemExit(
        f"Немає ключа. Поклади його в {KEY_FILE} (файл у .gitignore) "
        f"або в змінну оточення KIE_API_KEY."
    )


def call(path: str, key: str, payload: dict | None = None, query: dict | None = None) -> dict:
    url = f"{BASE}/{path}"
    if query:
        url += "?" + urllib.parse.urlencode(query)
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        method="POST" if data is not None else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        raise SystemExit(f"kie.ai відповів {exc.code}: {body}") from exc


def generate(args) -> int:
    key = read_key()
    if args.video:
        # Відеомоделі мають іншу форму входу: один стартовий кадр, тривалість,
        # звук. Фон у застосунку німий, тому sound завжди false — за звук ще й
        # платити довелося б.
        if not args.ref:
            raise SystemExit("Для відео потрібен стартовий кадр: --ref <URL>")
        # Kling вимагає ВСІ ці поля, а `duration` у нього РЯДОК, не число:
        # надішлеш 5 числом — ризикуєш 422. Роздільність задається `mode`,
        # окремого поля немає (std 720p, pro 1080p, 4K).
        payload = {
            "model": args.model,
            "input": {
                "prompt": args.prompt,
                "image_urls": args.ref[:2],
                "sound": False,
                "duration": str(args.duration),
                "aspect_ratio": args.aspect,
                "mode": args.mode,
                "multi_shots": False,
                "multi_prompt": [],
            },
        }
    else:
        payload = {
            "model": args.model,
            "input": {"prompt": args.prompt, "aspect_ratio": args.aspect},
        }
        # У Lite немає ні resolution, ні output_format — розмір там задає лише
        # співвідношення сторін. Зайві поля моделі, яка їх не знає, — прямий
        # шлях до 422 замість картинки.
        if args.model != "nano-banana-2-lite":
            payload["input"]["resolution"] = args.resolution
            payload["input"]["output_format"] = "png"
        if args.ref:
            # Референс мусить бути публічним URL — kie.ai тягне його сам.
            payload["input"][REF_FIELD.get(args.model, REF_FIELD_DEFAULT)] = args.ref

    created = call("createTask", key, payload)
    if created.get("code") != 200:
        raise SystemExit(f"Задачу не створено: {created}")
    task_id = created["data"]["taskId"]
    print(f"Задача {task_id} у черзі…")
    # taskId на диск НЕГАЙНО, ще до очікування. Кредити списуються за
    # створення задачі, і якщо процес обірветься (таймаут консолі, Ctrl+C),
    # без збереженого id результат не дістати ніяк: списку задач у kie немає.
    # Одного такого обриву вистачило, щоб втратити оплачену картинку.
    args.out.parent.mkdir(parents=True, exist_ok=True)
    pending = args.out.parent / (args.out.stem + ".task.txt")
    pending.write_text(task_id + "\n" + args.model + "\n", encoding="utf-8")

    started = time.monotonic()
    while True:
        if time.monotonic() - started > DEADLINE_SECONDS:
            raise SystemExit(f"Задача {task_id} не завершилась за {DEADLINE_SECONDS} с.")
        time.sleep(POLL_SECONDS)
        info = call("recordInfo", key, query={"taskId": task_id})
        data = info.get("data") or {}
        state = data.get("state")
        if state in (None, "waiting", "queuing", "generating"):
            print(f"  {state or '—'} {data.get('progress', '')}")
            continue
        if state != "success":
            raise SystemExit(f"Не вдалось: {data.get('failMsg') or data}")

        result = data.get("resultJson")
        if isinstance(result, str):
            result = json.loads(result)
        urls = (result or {}).get("resultUrls") or []
        if not urls:
            raise SystemExit(f"Готово, але без картинки: {data}")

        args.out.parent.mkdir(parents=True, exist_ok=True)
        # URL друкуємо ПЕРШИМ ділом: картинка вже згенерована й оплачена, і
        # якщо скачування впаде (CDN віддає 403 без User-Agent), результат не
        # має загубитись разом зі списаними кредитами.
        for url in urls:
            print(f"URL: {url}")
        (args.out.parent / (args.out.stem + ".urls.txt")).write_text(
            "\n".join(urls), encoding="utf-8"
        )
        for index, url in enumerate(urls):
            target = args.out if index == 0 else args.out.with_stem(f"{args.out.stem}-{index + 1}")
            request = urllib.request.Request(
                url, headers={"User-Agent": "Mozilla/5.0 (order-desk asset fetch)"}
            )
            try:
                with urllib.request.urlopen(request, timeout=180) as response:
                    target.write_bytes(response.read())
            except urllib.error.HTTPError as exc:
                print(f"  не скачалось ({exc.code}) — візьми файл за URL вище")
                continue
            print(f"Збережено: {target}  ({target.stat().st_size // 1024} КБ)")
        # Лог поруч із файлом — вимога скіла /generate. Без промпту й моделі
        # вдалий кадр через місяць не відтворити, а перегенерувати наосліп
        # означає платити ще раз.
        params = (
            f"відео {args.duration}с"
            if args.video
            else f"{args.aspect} {args.resolution}"
        )
        lines = [
            f"# {args.out.name}",
            f"- модель: {args.model} (Kie AI)",
            f"- задача: {task_id}",
            f"- кредитів списано: {data.get('creditsConsumed')}",
            f"- референс: {', '.join(args.ref) if args.ref else '—'}",
            f"- параметри: {params}",
            "- промпт: |",
            "    " + args.prompt.replace("\n", "\n    "),
            "- URL результату:",
            *[f"    {url}" for url in urls],
            "",
        ]
        log = args.out.with_suffix(args.out.suffix + ".log.md")
        log.write_text("\n".join(lines), encoding="utf-8")
        print(f"Витрачено кредитів: {data.get('creditsConsumed')}")
        print(f"Лог: {log}")
        return 0


def resume(args) -> int:
    """Забрати результат уже створеної задачі за її id.

    Рятує саме той випадок, заради якого id пишеться на диск: задача оплачена,
    але процес, що її чекав, помер.
    """
    key = read_key()
    info = call("recordInfo", key, query={"taskId": args.resume})
    data = info.get("data") or {}
    if data.get("state") != "success":
        print(f"стан: {data.get('state')} {data.get('failMsg') or ''}")
        return 1
    result = data.get("resultJson")
    if isinstance(result, str):
        result = json.loads(result)
    for index, url in enumerate((result or {}).get("resultUrls") or []):
        print(f"URL: {url}")
        target = args.out if index == 0 else args.out.with_stem(f"{args.out.stem}-{index + 1}")
        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(request, timeout=180) as response:
            target.write_bytes(response.read())
        print(f"Збережено: {target}")
    print(f"Витрачено кредитів: {data.get('creditsConsumed')}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prompt")
    parser.add_argument("--resume", help="Забрати результат задачі за taskId")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--aspect", default="16:9")
    parser.add_argument("--resolution", default="2K", choices=["1K", "2K", "4K"])
    parser.add_argument(
        "--ref",
        action="append",
        help="Публічний URL референсного зображення; можна вказати кілька разів",
    )
    parser.add_argument("--video", action="store_true", help="Відеомодель замість зображення")
    parser.add_argument("--duration", type=int, default=5, choices=list(range(3, 16)))
    parser.add_argument(
        "--mode", default="std", choices=["std", "pro", "4K"],
        help="Kling: std 720p (чернетки), pro 1080p, 4K",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=ROOT / "design" / "furnace-bg.png",
        help="Куди зберегти результат",
    )
    args = parser.parse_args(argv)
    if args.resume:
        return resume(args)
    if not args.prompt:
        parser.error("потрібен --prompt (або --resume <taskId>)")
    return generate(args)


if __name__ == "__main__":
    raise SystemExit(main())
