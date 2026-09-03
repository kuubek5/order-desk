"""Екран діагностики швидкодії — `/diag/perf`.

Навіщо окремий екран, а не лог. Прод стоїть на робочому ПК біля верстатів, і
з машини розробки його не видно (та сама причина, з якої в 0.7.x додавали
лічильники кешів прямо в інтерфейс). «Відкрий файл логу й знайди рядок» між
двома роботами ніхто робити не буде, а скарга «перемикання між вкладками ~5
секунд» без чисел не лікується — див. правило «спершу міряти, потім правити».

Що тут видно, чого немає в логу:
- розкладка серверного часу по фазах (SQL, мережева шара, рендер шаблону);
- КЛІЄНТСЬКИЙ час — свап HTMX і перемальовка. Для оператора це та сама
  затримка, але сервер її не бачить узагалі: він давно відповів;
- кнопка «Скопіювати як текст» — щоб віддати зріз одним блоком, не роблячи
  скріншотів таблиці.

Роути свідомо під /diag/: middleware вимірювання їх пропускає, інакше екран
міряв би сам себе й витісняв корисні проби з кільцевого буфера.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from sqlalchemy.orm import Session
from starlette.requests import Request

from app import perf
from app.routers.deps import get_current_user, get_db, templates

router = APIRouter()


def _require_admin(request: Request, db: Session):
    user = get_current_user(request, db)
    if user is None:
        return None, RedirectResponse("/login", status_code=303)
    if user.role != "адмін":
        raise HTTPException(status_code=403, detail="лише для адміністратора")
    return user, None


def _rows(limit: int = 120) -> list[dict]:
    """Проби, найповільніші зверху.

    Сортуємо за ВІДЧУТИМ часом (клієнтський total, якщо він є), а не за
    серверним: саме його чекає оператор. Запит, де сервер віддав за 200 мс, а
    браузер малював 3 с, у серверному сортуванні провалився б у хвіст — а це
    рівно той випадок, який ми шукаємо.
    """
    out: list[dict] = []
    for sample in perf.samples():
        felt = sample.client.get("total", 0.0) or sample.server_seconds
        phases = {k: v for k, v in sample.phases.items() if k != "rows"}
        # Явно показуємо, скільки часу НЕ потрапило в жодну фазу. Мовчазна
        # прогалина читалась би як «все заміряно», а саме в ній минулого разу
        # й ховались секунди: фази покривають відомі шматки, решта — ні.
        measured = sum(v for k, v in phases.items() if k != perf.ENTRY_PHASE)
        rest = sample.server_seconds - measured
        if rest >= perf.MIN_PHASE_SECONDS:
            phases["решта (незаміряне)"] = rest
        out.append(
            {
                "at": datetime.fromtimestamp(sample.at).strftime("%H:%M:%S"),
                "method": sample.method,
                "path": sample.path,
                "query": sample.query,
                "status": sample.status,
                "server": sample.server_seconds,
                "felt": felt,
                "rows": int(sample.phases.get("rows", 0)),
                "phases": sorted(phases.items(), key=lambda kv: -kv[1]),
                "client": sample.client,
                # Скільки часу пішло ПІСЛЯ відповіді сервера. Це і є та
                # частина, якої в логах не було ніколи.
                "after_server": max(0.0, felt - sample.server_seconds) if sample.client else None,
            }
        )
    out.sort(key=lambda r: -r["felt"])
    return out[:limit]


@router.get("/diag/perf", response_class=HTMLResponse)
def get_perf(request: Request, db: Session = Depends(get_db)):
    user, redirect = _require_admin(request, db)
    if redirect is not None:
        return redirect
    rows = _rows()
    total = len(perf.samples())
    return templates.TemplateResponse(
        request,
        "diag_perf.html",
        {
            "page_title": "Швидкодія",
            "user": user,
            "rows": rows,
            "total": total,
            "slow": [r for r in rows if r["felt"] >= 1.0],
        },
    )


@router.get("/diag/perf.txt", response_class=PlainTextResponse)
def get_perf_text(request: Request, db: Session = Depends(get_db)):
    """Той самий зріз простим текстом — щоб надіслати одним блоком."""
    _, redirect = _require_admin(request, db)
    if redirect is not None:
        return redirect

    lines = [f"KuubMill — швидкодія, проб у буфері: {len(perf.samples())}", ""]
    for row in _rows(60):
        felt = f"{row['felt']:.2f}"
        server = f"{row['server']:.2f}"
        head = f"{row['at']}  {felt}с відчутно / {server}с сервер  {row['method']} {row['path']}"
        if row["query"]:
            head += f"?{row['query']}"
        if row["rows"]:
            head += f"  ({row['rows']} рядків)"
        lines.append(head)
        if row["phases"]:
            lines.append(
                "    сервер: "
                + ", ".join(f"{name} {value:.2f}" for name, value in row["phases"])
            )
        if row["client"]:
            lines.append(
                "    клієнт: "
                + ", ".join(
                    f"{name} {value:.2f}"
                    for name, value in sorted(row["client"].items(), key=lambda kv: -kv[1])
                    if name != "total"
                )
            )
    return "\n".join(lines)


@router.post("/diag/perf/clear")
def post_perf_clear(request: Request, db: Session = Depends(get_db)):
    _, redirect = _require_admin(request, db)
    if redirect is not None:
        return redirect
    perf.clear()
    return RedirectResponse("/diag/perf", status_code=303)


@router.post("/diag/perf/client")
async def post_perf_client(request: Request):
    """Клієнтські числа для вже записаної проби.

    БЕЗ гейту на адміна свідомо: це шле сама сторінка будь-якого залогіненого
    оператора, і саме його затримки нас цікавлять. Приймаємо лише числа й
    лише для `request_id`, який ми самі видали — чужого рядка сюди не
    підсунути, а вміст іде в буфер у пам'яті, не в базу.
    """
    if request.session.get("user_id") is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 — некоректне тіло не має шуміти в логах
        raise HTTPException(status_code=400, detail="очікується JSON")

    entries = payload if isinstance(payload, list) else [payload]
    attached = 0
    for entry in entries[:50]:
        if not isinstance(entry, dict):
            continue
        request_id = str(entry.get("id", ""))[:32]
        metrics = entry.get("metrics")
        if not request_id or not isinstance(metrics, dict):
            continue
        clean: dict[str, float] = {}
        for name, value in list(metrics.items())[:12]:
            try:
                clean[str(name)[:24]] = round(float(value), 4)
            except (TypeError, ValueError):
                continue
        if clean and perf.attach_client(request_id, clean):
            attached += 1
    return {"attached": attached}
