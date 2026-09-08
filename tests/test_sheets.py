"""app/sheets.py — транспорт до Google Таблиці.

Тут стережеться не логіка синку (вона в test_sync.py), а сам канал: дедлайни
запитів і класифікація помилок. Мережі в тестах немає — адаптер підмінюється.
"""

# ── Дедлайн на кожен запит до Google ───────────────────────────────────────
# Аудит 08.09.26. gspread будує сесію без тайм-ауту, а requests без нього чекає
# ВІЧНО. Обрив без RST (так рве TLS-проксі цеху) лишав потік у read() назавжди:
# `_sync_lock` не звільнявся, а єдиний потік пулу запису вішав усі галочки
# операторів. Перезапуск воркера не лікує — воркер живий, він чекає.


def test_adapter_injects_a_deadline_when_the_caller_gave_none():
    from app.sheets import SHEETS_TIMEOUT_SECONDS, _LegacyRenegotiationAdapter

    seen = {}

    class _Probe(_LegacyRenegotiationAdapter):
        def send(self, request, *args, **kwargs):
            # Перехоплюємо ПІСЛЯ підстановки, до справжньої відправки.
            captured = dict(kwargs)
            def _super_send(_req, *_a, **kw):
                seen.update(kw)
                return "sent"
            import requests.adapters as _adapters
            original = _adapters.HTTPAdapter.send
            _adapters.HTTPAdapter.send = _super_send
            try:
                return super().send(request, *args, **captured)
            finally:
                _adapters.HTTPAdapter.send = original

    _Probe().send(object(), timeout=None)
    assert seen["timeout"] == SHEETS_TIMEOUT_SECONDS


def test_adapter_keeps_an_explicit_deadline():
    """Свій тайм-аут викликача не перетирається — інакше довгі операції, яким
    навмисно дали більше часу, різались би нашим дефолтом."""
    from app.sheets import _LegacyRenegotiationAdapter

    seen = {}

    class _Probe(_LegacyRenegotiationAdapter):
        def send(self, request, *args, **kwargs):
            import requests.adapters as _adapters
            original = _adapters.HTTPAdapter.send
            _adapters.HTTPAdapter.send = lambda _s, _r, *a, **kw: seen.update(kw) or "sent"
            try:
                return super().send(request, *args, **kwargs)
            finally:
                _adapters.HTTPAdapter.send = original

    _Probe().send(object(), timeout=(1, 2))
    assert seen["timeout"] == (1, 2)


def test_timeout_counts_as_transient_so_retries_still_apply():
    """Спрацювання дедлайну має лягати в наявний ланцюг повторів, а не вилітати
    нагору як фатальна помилка."""
    import requests

    from app.sheets import is_transient_sheet_error

    assert is_transient_sheet_error(requests.exceptions.Timeout("too slow"))
    assert is_transient_sheet_error(requests.exceptions.ConnectTimeout("no route"))


def test_both_google_sessions_mount_the_deadline_adapter():
    """Токен оновлюється ОКРЕМОЮ сесією. Якщо дедлайн стоїть лише на одній,
    зависання просто переїде на другу."""
    import inspect

    from app import sheets

    source = inspect.getsource(sheets._build_client)
    # Обидві сесії — token_session і session — мусять монтувати наш адаптер.
    assert source.count('mount("https://", _LegacyRenegotiationAdapter())') == 2
