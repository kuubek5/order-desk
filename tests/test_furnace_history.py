"""Історія показань печі за добу — графік, який не має права вигадувати.

Екран печей побудований на одному правилі: хибне число гірше за жодне. Графік
цю вимогу підсилює, бо вміє брехати тихіше за цифру — достатньо провести лінію
через проміжок, де показань не було, і на екрані з'явиться температура, якої
ніхто не бачив. Тому тести нижче перевіряють не «малює», а саме «не домальовує»:

— дірка в даних розриває лінію і лишається дірою;
— нерозпізнана температура не стає точкою;
— порожня історія дає порожній стан, а не нульову лінію;
— проріджування великих вибірок лишає ЛИШЕ реальні виміри, без усереднення;
— вікно історії міряється часом, а не кількістю рядків.
"""

from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import FurnaceReading
from app.routers.deps import templates
from app.services import furnace as service

HOST = "192.168.1.76"
NOW = datetime(2026, 9, 6, 12, 0, 0)
STARTED = NOW - timedelta(hours=24)


def _row(minutes_ago: int, temp=None, status: str = "RUN", error=None) -> FurnaceReading:
    return FurnaceReading(
        host=HOST,
        captured_at=NOW - timedelta(minutes=minutes_ago),
        status=status,
        temp_c=temp,
        error=error,
    )


def _chart(rows) -> service.DayChart:
    return service.build_day_chart(rows, started_at=STARTED, ended_at=NOW)


def _database():
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return engine


# ── Пропуски ────────────────────────────────────────────────────────────────


def test_gap_is_never_bridged_by_an_invented_line():
    """Вісім годин без показань — це два окремі шматки історії, а не одна
    лінія, що плавно спускається з 900 до 300 °C. Прямої через ці вісім годин
    не існувало, і саме вона була б хибним числом на весь екран."""
    rows = [_row(600, 900), _row(599, 905), _row(120, 300), _row(119, 295)]
    chart = _chart(rows)

    assert len(chart.lines) == 2, "шматки історії не з'єднуються"
    # Три дірки: до першого показання, вісім годин посередині, дві години в кінці.
    assert len(chart.gaps) == 3
    middle = chart.gaps[1]
    assert "немає даних 02:01–10:00" in middle.title
    assert "7 год 59 хв" in middle.title


def test_unreadable_temperature_is_a_hole_not_a_point():
    """Рядок без розпізнаного числа (зіпсована цифра, помилка зв'язку) не стає
    точкою графіка. Десять таких хвилин поспіль читаються як дірка."""
    rows = [_row(60 - i, 800) for i in range(5)]
    rows += [_row(55 - i, None, status="?", error="немає зв'язку") for i in range(10)]
    rows += [_row(45 - i, 780) for i in range(5)]
    chart = _chart(rows)

    assert chart.unread == 10
    assert chart.readings == 20
    assert len(chart.lines) == 2
    assert any("немає даних" in gap.title for gap in chart.gaps)


def test_edges_of_the_window_are_shaded_too():
    """Піч почали опитувати посеред доби — ліва частина графіка це не «нуль
    градусів» і не «нічого не відбувалось», а невідомість."""
    chart = _chart([_row(31, 700), _row(30, 705)])
    assert chart.gaps, "порожній край доби мусить бути позначений"
    assert chart.gaps[0].x == 0.0
    assert "немає даних" in chart.gaps[0].title


def test_empty_history_is_an_empty_state_not_a_zero_line():
    """Порожній графік із лінією по нулю сказав би «було 0 °C». Правильна
    відповідь — що даних немає."""
    chart = _chart([])
    assert chart.has_data is False
    assert chart.lines == [] and chart.dots == []
    assert chart.max_c is None


def test_only_errors_still_counts_as_no_data():
    """Піч відповідала, але жодного числа не віддала — це теж «даних немає»,
    а не графік із порожнім полем."""
    chart = _chart([_row(300, None, status="?", error="таймаут") for _ in range(3)])
    assert chart.has_data is False
    assert chart.readings == 3 and chart.unread == 3


def test_lonely_reading_is_a_dot_not_a_segment():
    """Одне показання без сусідів — це одна крапка. Відрізок «звідси досюди»
    із неї малювати нема з чого."""
    chart = _chart([_row(700, 640), _row(100, 120)])
    assert chart.lines == []
    assert len(chart.dots) == 2


# ── Проріджування ───────────────────────────────────────────────────────────


def test_thinning_keeps_real_measurements_and_never_averages():
    """Доба розігріву — це тисячі рядків. Проріджування лишає РЕАЛЬНІ виміри
    (перший, останній, найгарячіший, найхолодніший у колонці), тому пік не
    згладжується, а на графіку не з'являється жодного значення, якого не було
    на табло."""
    rows = [_row(1400 - i, 700 + (i % 3)) for i in range(1300)]
    spike = 1499
    rows[600] = _row(1400 - 600, spike)
    rows.sort(key=lambda r: r.captured_at)
    chart = _chart(rows)

    assert chart.readings == 1300
    assert chart.plotted < 700, "проріджування мусить спрацювати"
    assert chart.max_c == spike

    real = {r.temp_c for r in rows}
    top = chart.top_c
    allowed = {round(service.CHART_HEIGHT - t / top * service.CHART_HEIGHT, 1) for t in real}
    drawn = {
        float(point.split(",")[1])
        for line in chart.lines
        for point in line.split(" ")
    }
    assert drawn <= allowed, "на графіку з'явилось значення, якого не було в показаннях"
    # Пік лишився: саме заради нього проріджування бере максимум колонки.
    assert round(service.CHART_HEIGHT - spike / top * service.CHART_HEIGHT, 1) in drawn


# ── Час і вікно ─────────────────────────────────────────────────────────────


def test_time_labels_are_shop_clock_without_conversion():
    """Підписи осі — те саме, що показує годинник цеху: captured_at пишеться
    локальним київським часом (див. FurnaceReading), і переводити його ще раз
    означало б зсунути історію на три години відносно решти екрана."""
    chart = _chart([_row(60, 500), _row(59, 505)])
    assert [tick.label for tick in chart.x_ticks] == [
        "15:00", "18:00", "21:00", "00:00", "03:00", "06:00", "09:00",
    ]


def test_day_window_is_measured_in_time_not_in_rows():
    """Чому не recent_readings(): там межа — 40 РЯДКІВ, а рядок пишеться на
    кожну зміну температури. Під час розігріву сорок рядків це кілька хвилин,
    і «історія за добу» показувала б їх під виглядом доби."""
    engine = _database()
    with Session(engine) as db:
        for i in range(100):
            db.add(_row(i, 900 + i % 5))
        for i in range(5):
            db.add(_row(25 * 60 + i, 100))  # старші за добу
        db.commit()

        rows = service.day_readings(db, HOST, now=NOW)
        assert len(rows) == 100
        assert rows == sorted(rows, key=lambda r: r.captured_at), "найстаріші спершу"
        assert all(r.captured_at >= NOW - timedelta(hours=24) for r in rows)
        assert len(service.recent_readings(db, HOST)) == 40  # саме тому й нова функція


def test_day_chart_reads_from_the_database_by_furnace_key():
    engine = _database()
    with Session(engine) as db:
        db.add(_row(10, 1200))
        db.add(_row(9, 1210))
        db.add(FurnaceReading(host="інша-піч", captured_at=NOW, status="RUN", temp_c=30))
        db.commit()

        chart = service.day_chart(db, HOST, now=NOW)
        assert chart.max_c == 1210 and chart.readings == 2


# ── Розмітка ────────────────────────────────────────────────────────────────


def _render(chart) -> str:
    tpl = templates.env.get_template("_furnace_history.html")
    return tpl.render(chart=chart, furnace_name="Піч 1")


def test_markup_draws_one_polyline_per_piece_and_shades_the_holes():
    chart = _chart([_row(600, 900), _row(599, 905), _row(120, 300), _row(119, 295)])
    html = _render(chart)
    assert html.count("<polyline") == 2
    assert html.count('class="fh-gap"') == 3
    assert "немає даних" in html
    # Жодного зовнішнього скрипта: SVG малює сервер, застосунок працює офлайн.
    assert "<script" not in html


def test_markup_of_empty_history_says_so_instead_of_drawing_zero():
    html = _render(_chart([]))
    assert "Даних ще немає" in html
    assert "<polyline" not in html and "<svg" not in html
