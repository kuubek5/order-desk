"""Скільки колонок часу пишуть за Гринвічем — і чому їх не має більшати.

На SQLite `server_default=func.now()` пише UTC, а застосунок скрізь працює в
локальному часі. Різниця три години. Уся система це вже пережила один раз:
`ShiftNote` від `server_default` свідомо відмовився (CLAUDE.md §14), бо час
записів зміни їхав на три години назад.

Але правило не поширили на решту моделей. Аудит 08.09.26 нарахував двадцять
таких колонок у двадцяти класах. Кожна — тиха міна: будь-який НОВИЙ екран, що
покаже цей час, покаже його на три години раніше, ніж було насправді.

ЧОМУ МИ ЇХ НЕ ВИПРАВИЛИ, а поставили сторожа. Виправлення дорожче за баг і
ризикованіше:

  * поміняти дефолт мало — треба ще ПЕРЕСУНУТИ вже записані значення, інакше в
    одній колонці житимуть UTC-рядки поруч із локальними, і це гірше за
    послідовний зсув: зникає навіть можливість порахувати поправку;
  * міграція «+3 години» неправильна сама по собі: зсув Києва не сталий
    (літній/зимовий час), тож частина історії поїде на годину;
  * жоден чинний екран цих значень не показує, тобто сьогодні шкоди немає.

Тому фіксуємо СКЛАД: двадцять колонок, які вже такі, лишаються (їх правитимуть
разом із рештою, свідомо й з міграцією). А кожна НОВА валить цей тест і змушує
автора спинитись.
"""

from pathlib import Path

# Скільки колонок з UTC-дефолтом є ЗАРАЗ. Число не «правильне», воно
# зафіксоване: нове має падати, старе — доживати до свідомої міграції.
KNOWN_UTC_DEFAULT_COLUMNS = 20


def _models_source() -> str:
    return (Path(__file__).resolve().parents[1] / "app" / "models.py").read_text(
        encoding="utf-8"
    )


def test_no_new_utc_default_timestamps():
    """Новий `server_default=func.now()` — це нова колонка з часом на три
    години назад. Якщо цей тест упав на твоїй новій моделі: використай
    `default=datetime.now`, як зроблено в `ShiftNote` і `ActionLog`."""
    found = _models_source().count("server_default=func.now()")
    assert found <= KNOWN_UTC_DEFAULT_COLUMNS, (
        f"колонок з UTC-дефолтом стало {found} замість {KNOWN_UTC_DEFAULT_COLUMNS}. "
        "На SQLite func.now() пише за Гринвічем, тобто -3 години від того, що "
        "показує решта застосунку. Візьми default=datetime.now (див. ShiftNote)."
    )


def test_the_known_count_stays_honest():
    """Якщо колонок стало МЕНШЕ — хтось почав міграцію; тоді треба зменшити
    число тут, щоб сторож не втратив гостроту."""
    found = _models_source().count("server_default=func.now()")
    assert found == KNOWN_UTC_DEFAULT_COLUMNS, (
        f"колонок з UTC-дефолтом тепер {found}, а зафіксовано "
        f"{KNOWN_UTC_DEFAULT_COLUMNS}. Онови число разом із міграцією."
    )


def test_the_journals_we_prune_are_documented_as_utc():
    """`prune_journals` рахує межу для sync_logs у шкалі UTC саме через це.
    Якщо колонка колись стане локальною — там треба виправити разом."""
    source = _models_source()
    sync_log = source[source.index("class SyncLog(Base)"):]
    sync_log = sync_log[: sync_log.index("class ", 10)] if "class " in sync_log[10:] else sync_log
    assert "server_default=func.now()" in sync_log, (
        "SyncLog.occurred_at більше не UTC — виправ шкалу в "
        "app/services/journal_prune.py, інакше межа прибирання поїде на 3 год"
    )
