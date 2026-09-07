"""LOW-список код-ревʼю 07.09.26 — дрібниці, які тихо перестають працювати.

Спільна риса всіх трьох: нічого не падає, помилки не видно, а функція вже не
працює. Саме тому кожна отримала тест — інакше повернення непомітне.
"""

import time
from types import SimpleNamespace

from app.routers import machines as machines_router
from app.services.attempt_limit import AttemptLimiter


def test_stale_login_attempts_do_not_pile_up_forever():
    """Ключ обмежувача — «логін + IP», тобто його вигадує той, хто стукає.

    Перебір неіснуючих логінів лишав по запису на кожну спробу, і словник ріс
    усе життя процесу. Прибирання відбувалось лише при зверненні до ТОГО
    САМОГО ключа — тобто для перебору не відбувалось ніколи.
    """
    limiter = AttemptLimiter(free_attempts=5, block_seconds=1.0, reset_after_seconds=0.05)

    for i in range(50):
        limiter.register_failure(f"привид-{i}@10.0.0.1")
    assert len(limiter._buckets) == 50

    time.sleep(0.06)                       # спроби «застаріли»
    limiter.register_failure("хтось-живий@10.0.0.1")

    assert len(limiter._buckets) == 1      # лишився тільки свіжий ключ


def test_a_blocked_key_survives_the_cleanup():
    """Чистка не має знімати активне блокування — інакше пауза обходилась би
    простим очікуванням чужих спроб."""
    limiter = AttemptLimiter(free_attempts=1, block_seconds=60.0, reset_after_seconds=0.05)

    limiter.register_failure("зловмисник@10.0.0.2")
    blocked_for = limiter.register_failure("зловмисник@10.0.0.2")
    assert blocked_for > 0

    time.sleep(0.06)
    limiter.register_failure("інший@10.0.0.3")

    assert limiter.retry_after("зловмисник@10.0.0.2") > 0


class _StubTemplates:
    def TemplateResponse(self, request, name, context):
        return context


def test_manual_machine_refresh_has_a_cooldown(monkeypatch):
    """«Оновити зараз» одразу йде в мережу до кожного верстата.

    Без паузи затиснута кнопка перетворювала застосунок на генератор запитів
    до цехових ПК і відбирала потоки в планового опитування.
    """
    polls = {"n": 0}
    monkeypatch.setattr(machines_router, "poll_all", lambda db: polls.__setitem__("n", polls["n"] + 1))
    monkeypatch.setattr(machines_router, "templates", _StubTemplates())
    monkeypatch.setattr(machines_router, "_context", lambda request, db, user: {})
    monkeypatch.setattr(
        machines_router, "get_current_user", lambda request, db: SimpleNamespace(id=1, role="оператор")
    )
    monkeypatch.setattr(machines_router, "_last_manual_refresh", 0.0, raising=False)

    request = SimpleNamespace(session={"user_id": 1}, client=SimpleNamespace(host="127.0.0.1"))
    machines_router.machines_refresh(request=request, db=None)
    machines_router.machines_refresh(request=request, db=None)
    machines_router.machines_refresh(request=request, db=None)

    assert polls["n"] == 1                 # три кліки поспіль — один обхід


def test_header_complaint_disappears_with_the_tab():
    """Скарга на зсунуті заголовки знімалась лише при успішному перечитуванні
    ТІЄЇ САМОЇ вкладки. Перейменовану або прибрану вкладку банер показував
    назавжди — до перезапуску застосунку."""
    from app import sheet_sync_service as sync

    sync._record_header_mismatch("01.09.26", ["зсув колонки D"])
    sync._record_header_mismatch("02.09.26", ["зсув колонки D"])

    sync._forget_header_mismatch_for_absent_tabs({"02.09.26", "03.09.26"})

    pending = sync.header_mismatch_pending()
    assert "01.09.26" not in pending        # вкладки більше немає — скарги теж
    assert "02.09.26" in pending            # ця на місці, скарга лишається

    sync._record_header_mismatch("02.09.26", [])


def test_an_empty_listing_is_not_taken_as_proof(monkeypatch):
    """Порожній листинг — це збій читання, а не «вкладок не стало»."""
    from app import sheet_sync_service as sync

    sync._record_header_mismatch("01.09.26", ["зсув колонки D"])
    sync._forget_header_mismatch_for_absent_tabs(set())
    assert "01.09.26" in sync.header_mismatch_pending()
    sync._record_header_mismatch("01.09.26", [])
