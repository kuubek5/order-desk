"""Прив'язка рядка до теки в мить його появи (власник 26.09.26).

Видача три тижні вгадувала теку щоранку заново — за датою, потім за
«найближчим Sum3D» уночі, потім за підпартіями, — і кожна латка ламала сусідній
випадок. Тепер тека закріплюється за роботою ОДИН раз і далі не вгадується.

Головний тест — ПРОГРАВАННЯ бойових даних цеху за 24.09.26 (`kmill_order` —
коли рядок з'явився і його Sum3D; `kmill_handout_match` — коли лягла кожна
тека й підтека): рядки з'являються у свій час, теки лягають у свій, воркер
проходить щохвилини. Очікування — те, що кажуть самі файли (Sum3D за секунди
після теки, номери зубів), а не те, що показувала видача.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.export_scanner import SubBatch, entry_for_folder
from app.models import Order
from app.services import folder_binding
from app.services.folder_binding import bind_pending, entry_fully_bound, rel_key, units_of
from app.sync import _reset_order_for_new_work

KYIV = timedelta(hours=3)  # вересень: UTC+3
D24 = datetime(2026, 9, 24)
D25 = datetime(2026, 9, 25)


@pytest.fixture(autouse=True)
def _fresh_retry_state():
    folder_binding.reset_for_tests()
    yield
    folder_binding.reset_for_tests()


def _utc(kyiv: datetime) -> datetime:
    return kyiv - KYIV


def at(day: datetime, clock: str) -> datetime:
    h, m, s = (int(x) for x in clock.split(":"))
    return day.replace(hour=h, minute=m, second=s)


@dataclass
class Folder:
    """Тека кольору на диску: хто, де і коли лягла кожна партія."""

    client: str
    batch: str
    material: str
    parts: list[tuple[datetime, str]] = field(default_factory=list)  # (коли, перший STL)

    def as_seen_at(self, now: datetime):
        """Якою тека була о `now`: партії, що ляжуть пізніше, ще не існують."""
        present = [(when, stl) for when, stl in self.parts if when <= now]
        if not present:
            return None
        return SimpleNamespace(
            client_folder_name=self.client,
            batch_folder_name=self.batch,
            material_color_folder_name=self.material,
            created_at=present[0][0],
            material_created_at=present[0][0],
            folder_path=Path("X:/export") / self.client / self.batch / self.material,
            parts=tuple(SubBatch(w, s) for w, s in present) if len(present) > 1 else (),
        )


@dataclass
class Row:
    id: int
    client: str
    material: str
    appeared: datetime
    sum3d: str | None
    expected: str | None  # шлях від кореня export, None — не прив'язувати


def replay(db, rows: list[Row], folders: list[Folder], until: datetime) -> dict[int, str | None]:
    """Хвилина за хвилиною: рядок з'являється в базі у свій час, воркер
    проходить щохвилини й бачить лише теки, що вже лягли."""
    now = min(r.appeared for r in rows) - timedelta(minutes=1)
    created: dict[int, Order] = {}
    names = {r.client: r.client for r in rows}
    while now <= until:
        for row in rows:
            if row.id not in created and row.appeared <= now:
                order = Order(
                    id=row.id, source="sheet_client", sheet_tab="24.09.26", row_number=row.id,
                    client_name=row.client, material_color=row.material, quantity="1",
                    status="прораховано", sum3d_id=row.sum3d,
                )
                db.add(order)
                db.flush()
                order.created_at = _utc(row.appeared)
                db.commit()
                created[row.id] = order
        seen = [e for f in folders if (e := f.as_seen_at(now)) is not None]
        bind_pending(
            db, Path("X:/export"), now_utc=_utc(now),
            scan=lambda root, folder, nb, seen=seen: [e for e in seen if e.client_folder_name == folder],
            client_folders=names,
        )
        now += timedelta(minutes=1)
    return {row_id: order.export_folder_path for row_id, order in created.items()}


# ── програвання 24.09.26 ────────────────────────────────────────────────────

SEREDIUK_A35 = Folder("Середюк", "Новая папка (762)", "mono a3.5", [
    (at(D24, "10:08:10"), "2026-09-23_00002-005-36-tooth.stl"),
    (at(D24, "21:46:06"), "Новая папка/2026-09-17_00002-002-46-tooth.stl"),
    (at(D24, "23:04:12"), "Новая папка (2)/2026-09-24_00002-005-11-crown.stl"),
    (at(D25, "00:23:06"), "Новая папка (3)/2026-09-22_00002-005-17-tooth.stl"),
])
SEREDIUK_A3 = Folder("Середюк", "Новая папка (762)", "mono a3", [
    (at(D24, "20:41:03"), "2026-09-24_00002-004-12-11-21-22-bridge.stl"),
    (at(D24, "23:49:22"), "Новая папка/2026-09-22_00002-009-37-tooth.stl"),
    (at(D25, "00:08:01"), "Новая папка (2)/2026-09-23_00002-007-36-crown.stl"),
])
SEREDIUK_BL2 = Folder("Середюк", "Новая папка (762)", "mono bl2", [
    (at(D24, "23:29:50"), "2026-09-23_00002-010-14-crown.stl"),
    (at(D24, "23:40:17"), "Новая папка/2026-09-24_00002-002-34-crown.stl"),
])
SEREDIUK_PMMA = Folder("Середюк", "Новая папка (763)", "pmma a2", [
    (at(D25, "02:55:49"), "bitesplint.stl"),
    (at(D25, "03:30:13"), "Новая папка/2026-09-22_00002-010-16-tooth.stl"),
])
B762 = "Середюк/Новая папка (762)"


def test_replay_serediuk_24_09(db_session):
    """11 робіт одного клієнта за добу, кілька листів одного кольору в одній
    теці; вечір денний, ніч — диски Дениса на кількох клієнтів
    (`03-27-06` — п'ятеро). Було: 10 збігались і 4490 без теки («СТАРІША
    тека»). Стало: усі 11, кожна зі СВОЄЮ партією."""
    rows = [
        Row(4403, "Середюк", "mono a3.5", at(D24, "10:11:02"), "10-08-17", f"{B762}/mono a3.5"),
        Row(4461, "Середюк", "mono a3", at(D24, "20:50:08"), "20-40-01", f"{B762}/mono a3"),
        Row(4463, "Середюк", "mono a3.5", at(D24, "21:45:15"), "21-46-36", f"{B762}/mono a3.5/Новая папка"),
        Row(4468, "Середюк", "mono a3.5", at(D24, "23:08:50"), "23-05-13", f"{B762}/mono a3.5/Новая папка (2)"),
        Row(4469, "Середюк", "mono bl2", at(D24, "23:34:29"), "23-30-39", f"{B762}/mono bl2"),
        Row(4470, "Середюк", "mono bl2", at(D24, "23:43:46"), "23-38-59", f"{B762}/mono bl2/Новая папка"),
        Row(4472, "Середюк", "mono a3", at(D24, "23:54:53"), "23-48-43", f"{B762}/mono a3/Новая папка"),
        Row(4474, "Середюк", "mono a3", at(D25, "00:11:24"), "00-08-50", f"{B762}/mono a3/Новая папка (2)"),
        Row(4475, "Середюк", "mono a3.5", at(D25, "00:30:32"), "00-24-43", f"{B762}/mono a3.5/Новая папка (3)"),
        Row(4482, "Середюк", "pmma a2", at(D25, "03:12:26"), "02-58-04", "Середюк/Новая папка (763)/pmma a2"),
        Row(4490, "Середюк", "pmma a2", at(D25, "03:41:24"), "03-27-06", "Середюк/Новая папка (763)/pmma a2/Новая папка"),
    ]
    got = replay(db_session, rows, [SEREDIUK_A35, SEREDIUK_A3, SEREDIUK_BL2, SEREDIUK_PMMA],
                 until=at(D25, "05:00:00"))
    assert got == {r.id: r.expected for r in rows}


def test_replay_neda_row_written_before_files(db_session):
    """Неда `pmma a3`: рядок 66 з'явився о 13:36 — РАНІШЕ за будь-які файли,
    рядок 67 о 13:43. Лише хронологія віддала б корінь рядку 66. Sum3D
    каже інше, з точністю до секунд: корінь 13:37:37 ↔ `13-37-41` (67),
    підтека 13:42:54 ↔ `13-42-58` (66). Було: так само (видача вгадувала за
    Sum3D). Стало: так само, але закріплено."""
    folder = Folder("Неда", "Новая папка (763)", "pmma a3", [
        (at(D24, "13:37:37"), "2026-09-24_00001-004-25-crown.stl"),
        (at(D24, "13:42:54"), "attachments/2026-09-24_00001-002-15-crown.stl"),
    ])
    rows = [
        Row(4419, "Неда", "pmma a3", at(D24, "13:36:39"), "13-42-58",
            "Неда/Новая папка (763)/pmma a3/attachments"),
        Row(4420, "Неда", "pmma a3", at(D24, "13:43:07"), "13-37-41", "Неда/Новая папка (763)/pmma a3"),
    ]
    got = replay(db_session, rows, [folder], until=at(D24, "15:00:00"))
    assert got == {r.id: r.expected for r in rows}


def test_replay_basarab_and_rytchenka(db_session):
    """Басараб: файли 4449 лягли через 80 с ПІСЛЯ рядка й через 2 хв після
    Sum3D (рахували зі «Завантажень», потім перенесли) — ловить поправка на
    годинник. Ритченка: обидві — тека за секунди до Sum3D. Було й стало
    однаково; тепер закріплено."""
    basarab = Folder("Басараб", "Новая папка (1094)", "mono a3", [
        (at(D24, "17:02:52"), "2026-09-22_00002-012-37-crown.stl"),
        (at(D24, "19:36:14"), "mono a3/2026-09-24_99999-001-14-tooth.stl"),
    ])
    rytchenka = Folder("Стоматологія Ритченка", "Новая папка (461)", "mono a2", [
        (at(D24, "15:07:11"), "lytvynova-12-crown.stl"),
        (at(D24, "17:08:44"), "mono a2/nikolaeva-26-crown.stl"),
    ])
    rows = [
        Row(4449, "Басараб", "mono a3", at(D24, "17:01:32"), "17-00-47", "Басараб/Новая папка (1094)/mono a3"),
        Row(4458, "Басараб", "mono a3", at(D24, "19:43:46"), "19-36-17",
            "Басараб/Новая папка (1094)/mono a3/mono a3"),
        Row(4439, "Стоматологія Ритченка", "mono a2", at(D24, "15:15:12"), "15-07-19",
            "Стоматологія Ритченка/Новая папка (461)/mono a2"),
        Row(4450, "Стоматологія Ритченка", "mono a2", at(D24, "17:09:10"), "17-08-46",
            "Стоматологія Ритченка/Новая папка (461)/mono a2/mono a2"),
    ]
    got = replay(db_session, rows, [basarab, rytchenka], until=at(D24, "21:00:00"))
    assert got == {r.id: r.expected for r in rows}


# ── межі: де свідомо НЕ вгадуємо ────────────────────────────────────────────

def test_row_written_before_its_folder_stays_unbound(db_session):
    """Кривовид 17.09.26: рядок о 01:55, тека о 04:55 — нічний диск, Sum3D
    не поруч з текою. Тека, що лягла ПІСЛЯ рядка, не доказ — рядок лишається
    на старому показі видачі."""
    folder = Folder("Кривовид", "Новая папка (597)", "mono a3.5", [(at(D24, "04:55:00"), "a.stl")])
    rows = [Row(1, "Кривовид", "mono a3.5", at(D24, "01:55:00"), "04-40-00", None)]
    assert replay(db_session, rows, [folder], until=at(D24, "07:00:00")) == {1: None}


def test_two_folders_then_two_rows_keep_their_order(db_session):
    """Дві теки, потім по черзі два рядки, Sum3D — нічний диск (не поруч).
    «Найсвіжіша» переставила б їх; порядок зберігається."""
    folder_x = Folder("Середюк", "P1", "mono a3", [(at(D24, "09:00:00"), "x.stl")])
    folder_y = Folder("Середюк", "P2", "mono a3", [(at(D24, "09:10:00"), "y.stl")])
    rows = [
        Row(1, "Середюк", "mono a3", at(D24, "09:20:00"), "08-00-00", "Середюк/P1/mono a3"),
        Row(2, "Середюк", "mono a3", at(D24, "09:30:00"), "08-00-00", "Середюк/P2/mono a3"),
    ]
    got = replay(db_session, rows, [folder_x, folder_y], until=at(D24, "10:30:00"))
    assert got == {1: "Середюк/P1/mono a3", 2: "Середюк/P2/mono a3"}


def test_orphan_folder_of_an_earlier_row_is_not_taken(db_session):
    """Рядок А з'явився раніше за свої файли й лишився без теки; його тека L
    нічия. Наступний рядок бере свою свіжу теку, а не L — інакше зсунувся б
    увесь день."""
    orphan = Folder("Кривовид", "L", "mono a3", [(at(D24, "04:48:00"), "l.stl")])
    own = Folder("Кривовид", "X", "mono a3", [(at(D24, "09:50:00"), "x.stl")])
    rows = [
        Row(1, "Кривовид", "mono a3", at(D24, "01:55:00"), "03-00-00", None),
        Row(2, "Кривовид", "mono a3", at(D24, "10:00:00"), "03-00-00", "Кривовид/X/mono a3"),
    ]
    got = replay(db_session, rows, [orphan, own], until=at(D24, "11:00:00"))
    assert got == {1: None, 2: "Кривовид/X/mono a3"}


def test_batch_of_rows_gets_folders_in_row_order(db_session):
    """«Додати вручну» пачкою (одна секунда), спільний нічний диск. Старша
    тека — верхньому рядку; тек менше, ніж рядків, — жодного."""
    a = Folder("Середюк", "P1", "mono a3", [(at(D24, "14:00:00"), "a.stl")])
    b = Folder("Середюк", "P2", "mono a3", [(at(D24, "14:30:00"), "b.stl")])
    rows = [
        Row(10, "Середюк", "mono a3", at(D24, "15:00:00"), "12-00-00", "Середюк/P1/mono a3"),
        Row(11, "Середюк", "mono a3", at(D24, "15:00:00"), "12-00-00", "Середюк/P2/mono a3"),
    ]
    assert replay(db_session, rows, [a, b], until=at(D24, "15:30:00")) == {
        10: "Середюк/P1/mono a3", 11: "Середюк/P2/mono a3",
    }


def test_batch_with_fewer_folders_than_rows_binds_none(db_session):
    only = Folder("Середюк", "P1", "mono a3", [(at(D24, "14:00:00"), "a.stl")])
    rows = [
        Row(10, "Середюк", "mono a3", at(D24, "15:00:00"), "12-00-00", None),
        Row(11, "Середюк", "mono a3", at(D24, "15:00:00"), "12-00-00", None),
    ]
    assert replay(db_session, rows, [only], until=at(D24, "15:30:00")) == {10: None, 11: None}


def test_row_without_sum3d_waits_for_it(db_session):
    """Sum3D ще не вписали — хронологія могла б помилитись (випадок Неди),
    тож чекаємо годину, і лише тоді вирішуємо без нього."""
    folder = Folder("Середюк", "P1", "mono a3", [(at(D24, "11:50:00"), "a.stl")])
    rows = [Row(1, "Середюк", "mono a3", at(D24, "12:00:00"), None, None)]
    assert replay(db_session, rows, [folder], until=at(D24, "12:55:00")) == {1: None}
    folder_binding.reset_for_tests()
    order = db_session.get(Order, 1)
    bind_pending(db_session, Path("X:/export"), now_utc=_utc(at(D24, "13:01:00")),
                 scan=lambda *a: [folder.as_seen_at(at(D24, "13:01:00"))],
                 client_folders={"Середюк": "Середюк"})
    assert order.export_folder_path == "Середюк/P1/mono a3"


def test_colour_age_and_taken_folders_are_respected(db_session):
    other_colour = Folder("Середюк", "P1", "mono a2", [(at(D24, "11:50:00"), "a.stl")])
    too_old = Folder("Середюк", "P0", "mono a3", [(at(D24, "00:30:00") - timedelta(days=1), "o.stl")])
    rows = [Row(1, "Середюк", "mono a3", at(D24, "12:00:00"), "11-50-30", None)]
    assert replay(db_session, rows, [other_colour, too_old], until=at(D24, "12:30:00")) == {1: None}


def test_folder_taken_by_mail_work_is_skipped(db_session):
    """Тека вже закріплена прийняттям листа — нікому більше не дістається."""
    mail = Order(source="email", sheet_tab="24.09.26", row_number=1, client_name="Середюк",
                 material_color="mono a3", quantity="1", status="прораховано",
                 export_folder_path="Середюк/P2/mono a3")
    db_session.add(mail)
    db_session.flush()
    mail.created_at = _utc(at(D24, "11:00:30"))
    db_session.commit()
    older = Folder("Середюк", "P1", "mono a3", [(at(D24, "10:00:00"), "a.stl")])
    taken = Folder("Середюк", "P2", "mono a3", [(at(D24, "11:00:00"), "b.stl")])
    rows = [Row(2, "Середюк", "mono a3", at(D24, "12:00:00"), "08-00-00", "Середюк/P1/mono a3")]
    got = replay(db_session, rows, [older, taken], until=at(D24, "12:30:00"))
    # P1 лежить ДО рядка листа (11:00) і старша за його теку — не береться;
    # чесне «не вгадуємо» краще за чужу теку.
    assert got == {2: None}


def test_retry_memory_does_not_grow_forever(db_session):
    """Пам'ять про невдалі спроби живе на ПРОЦЕС, а процес працює місяцями.
    Рядок, що вийшов із вікна спроб, мусить зникати з неї сам — інакше
    ~60 клієнтських рядків на день накопичувались би до перезапуску."""
    # Дві роботи, одна тека — не вгадуємо, отже спроба невдала й лишає слід.
    only = Folder("Середюк", "P1", "mono a3", [(at(D24, "14:00:00"), "a.stl")])
    when = at(D24, "15:00:00")
    rows = [Row(10, "Середюк", "mono a3", when, "12-00-00", None),
            Row(11, "Середюк", "mono a3", when, "12-00-00", None)]
    replay(db_session, rows, [only], until=at(D24, "15:30:00"))
    assert folder_binding._next_try, "невдала спроба мусить лишити слід"
    # Через сім годин рядки вже поза ATTEMPT_WINDOW — слід має зникнути.
    bind_pending(db_session, Path("X:/export"), now_utc=_utc(at(D24, "22:30:00")),
                 scan=lambda *a: [], client_folders={"Середюк": "Середюк"})
    assert folder_binding._next_try == {}
    assert folder_binding._attempts == {}


def test_lab_rows_and_unmatched_clients_are_left_alone(db_session):
    lab = Order(source="lab", sheet_tab="24.09.26", row_number=1, client_name="Середюк",
                material_color="mono a3", quantity="1", status="нове", sum3d_id="11-50-10")
    db_session.add(lab)
    db_session.flush()
    lab.created_at = _utc(at(D24, "12:00:00"))
    db_session.commit()
    folder = Folder("Середюк", "P1", "mono a3", [(at(D24, "11:50:00"), "a.stl")])
    assert bind_pending(db_session, Path("X:/export"), now_utc=_utc(at(D24, "12:10:00")),
                        scan=lambda *a: [folder.as_seen_at(at(D24, "12:10:00"))],
                        client_folders={}) == []
    assert lab.export_folder_path is None


def test_gives_up_after_attempt_window(db_session):
    folder = Folder("Середюк", "P1", "mono a3", [(at(D24, "11:50:00"), "a.stl")])
    rows = [Row(1, "Середюк", "mono a3", at(D24, "12:00:00"), "11-50-20", None)]
    # воркер «лежав» до 19:00 — вікно спроб (6 год) минуло
    order_times = replay(db_session, rows, [folder], until=at(D24, "11:59:00"))
    assert order_times == {}
    order = Order(id=1, source="sheet_client", sheet_tab="24.09.26", row_number=1,
                  client_name="Середюк", material_color="mono a3", quantity="1",
                  status="прораховано", sum3d_id="11-50-20")
    db_session.add(order)
    db_session.flush()
    order.created_at = _utc(at(D24, "12:00:00"))
    db_session.commit()
    assert bind_pending(db_session, Path("X:/export"), now_utc=_utc(at(D24, "19:00:00")),
                        scan=lambda *a: [folder.as_seen_at(at(D24, "19:00:00"))],
                        client_folders={"Середюк": "Середюк"}) == []
    assert bind_pending(db_session, Path("X:/export"), now_utc=_utc(at(D24, "14:00:00")),
                        scan=lambda *a: [folder.as_seen_at(at(D24, "14:00:00"))],
                        client_folders={"Середюк": "Середюк"}) == [(1, "Середюк/P1/mono a3")]


# ── одиниці, видача, синк ───────────────────────────────────────────────────

def test_root_part_is_not_older_than_its_colour_folder():
    """Файли, ПЕРЕНЕСЕНІ зі «Завантажень», несуть давній час створення —
    корінь теки кольору не може бути старшим за саму теку."""
    entry = SimpleNamespace(
        client_folder_name="Середюк", batch_folder_name="B", material_color_folder_name="mono a3",
        created_at=at(D24, "09:00:00"), material_created_at=at(D24, "10:00:00"),
        folder_path=Path("X:/export/Середюк/B/mono a3"),
        parts=(SubBatch(datetime(2026, 9, 20, 8, 0), "old.stl"),
               SubBatch(at(D24, "12:00:00"), "Новая папка/x.stl")),
    )
    root, sub = units_of(entry)
    assert root.created_at == at(D24, "10:00:00")
    assert sub.rel == "Середюк/B/mono a3/Новая папка"


def test_fully_bound_folder_is_not_a_candidate_for_others():
    one = SEREDIUK_PMMA.as_seen_at(at(D25, "03:00:00"))
    two = SEREDIUK_PMMA.as_seen_at(at(D25, "04:00:00"))
    bound = {rel_key("Середюк/Новая папка (763)/pmma a2")}
    assert entry_fully_bound(one, bound)
    # Підтека ще нічия — тека кольору лишається кандидатом.
    assert not entry_fully_bound(two, bound)
    bound.add(rel_key("середюк\\Новая папка (763)\\pmma a2\\Новая папка"))
    assert entry_fully_bound(two, bound)


def test_entry_for_bound_subfolder_and_loose_batch(tmp_path):
    sub = tmp_path / "Середюк" / "Новая папка (763)" / "pmma a2" / "Новая папка"
    sub.mkdir(parents=True)
    (sub / "tooth.stl").write_bytes(b"x")
    entry = entry_for_folder(tmp_path, "Середюк/Новая папка (763)/pmma a2/Новая папка")
    assert entry is not None
    assert entry.material_color_folder_name == "pmma a2"   # колір, не назва підтеки
    assert entry.files == ["tooth.stl"]
    assert entry.folder_path == sub

    loose = tmp_path / "Клієнт" / "kappa"
    loose.mkdir(parents=True)
    (loose / "a.stl").write_bytes(b"x")
    entry = entry_for_folder(tmp_path, "Клієнт/kappa")
    assert entry is not None and entry.material_color_folder_name == "kappa"
    assert entry_for_folder(tmp_path, "Клієнт") is None
    assert entry_for_folder(tmp_path, "a/b/c/d/e") is None


def test_row_taken_over_by_another_work_drops_the_folder():
    """Синк віддав рядок іншій роботі — стара тека з файлами теж була
    старої роботи, і під новою її показувати не можна."""
    order = Order(source="sheet_client", status="прораховано",
                  export_folder_path="Середюк/P1/mono a3")
    _reset_order_for_new_work(order, source="lab", status="нове")
    assert order.export_folder_path is None
