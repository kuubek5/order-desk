"""Кеш «Можливих дублікатів»: показ миттєвий, рахунок у фоні.

На проді відкриття екрана коштувало 15+ с (dup:folders 13.24с при 757 теках,
27.09.26) — тепер запит лише читає знімок.
"""

import pytest

from app.models import Order
from app.services import duplicates_cache


@pytest.fixture(autouse=True)
def _clean_cache():
    duplicates_cache.reset_for_tests()
    yield
    duplicates_cache.reset_for_tests()


def _seed_pair(db):
    db.add_all([
        Order(source="lab", client_name="Кривовид"),
        Order(source="lab", client_name="Євген Кривовид"),
    ])
    db.commit()


def test_cold_screen_does_not_compute_in_request(db_session, monkeypatch):
    """Холодне відкриття: знімка немає, запит НЕ рахує пари сам — лише
    запускає фон і каже «рахується»."""
    kicked = []
    monkeypatch.setattr(duplicates_cache, "kick", lambda reason: kicked.append(reason))

    snapshot, computing = duplicates_cache.snapshot_for_screen()

    assert snapshot is None
    assert kicked == ["screen"]


def test_refresh_publishes_and_screen_reads_it(db_session, monkeypatch):
    monkeypatch.setattr(
        duplicates_cache, "list_export_client_names_cached",
        lambda root: ["Ніколаєв", "Іван Ніколаєв"],
    )
    monkeypatch.setattr(duplicates_cache, "get_export_folder_path", lambda db: "x:/export")
    _seed_pair(db_session)

    duplicates_cache.refresh_now(db_session)
    monkeypatch.setattr(duplicates_cache, "kick", lambda reason: (_ for _ in ()).throw(AssertionError("свіжий знімок не мусить запускати фон")))
    snapshot, _ = duplicates_cache.snapshot_for_screen()

    assert snapshot is not None
    assert {(c.a_name, c.b_name) for c in snapshot.clients} == {("Євген Кривовид", "Кривовид")} or {(c.a_name, c.b_name) for c in snapshot.clients} == {("Кривовид", "Євген Кривовид")}
    assert [c.a_name for c in snapshot.folders] == ["Іван Ніколаєв"]
    assert snapshot.folder_names == ["Іван Ніколаєв", "Ніколаєв"]


def test_owner_decision_removes_pair_immediately(db_session, monkeypatch):
    """Після «злити»/«не дублі» redirect показує список уже БЕЗ пари — ще до
    того, як фоновий перерахунок завершився."""
    monkeypatch.setattr(
        duplicates_cache, "list_export_client_names_cached",
        lambda root: ["Ніколаєв", "Іван Ніколаєв"],
    )
    monkeypatch.setattr(duplicates_cache, "get_export_folder_path", lambda db: "x:/export")
    _seed_pair(db_session)
    duplicates_cache.refresh_now(db_session)

    duplicates_cache.drop_client_pair("Кривовид", "Євген Кривовид")
    duplicates_cache.drop_folder_pair("Іван Ніколаєв", "Ніколаєв")

    snapshot = duplicates_cache.peek()
    assert snapshot.clients == []
    assert snapshot.folders == []
    # Назви тек для ручного вибору лишаються — рішення їх не прибирає.
    assert snapshot.folder_names == ["Іван Ніколаєв", "Ніколаєв"]


def test_stale_snapshot_is_served_and_refresh_kicked(db_session, monkeypatch):
    monkeypatch.setattr(
        duplicates_cache, "list_export_client_names_cached", lambda root: [],
    )
    monkeypatch.setattr(duplicates_cache, "get_export_folder_path", lambda db: "")
    _seed_pair(db_session)
    duplicates_cache.refresh_now(db_session)

    kicked = []
    monkeypatch.setattr(duplicates_cache, "kick", lambda reason: kicked.append(reason))
    monkeypatch.setattr(
        duplicates_cache.time, "monotonic",
        lambda: duplicates_cache.peek().computed_at + duplicates_cache.TTL_SECONDS + 1,
    )

    snapshot, _ = duplicates_cache.snapshot_for_screen()

    assert snapshot is not None and snapshot.clients  # старе віддали одразу
    assert kicked == ["screen"]
