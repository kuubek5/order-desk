"""Опційний QC-чеклист на видачі (аудит 05.09.26, крок 3.6).

Три звірки перед відміткою «знайдено». Головне тут — слово «опційний»:
чеклист свідомо порушує правило «один клік на дію» (CLAUDE.md §2) заради
страховки від чужої коронки в чужому пакетику, і це рішення власника, не
наше. Тому за замовчуванням він ВИМКНЕНИЙ, і саме це стережуть перші тести:
нова база, зіпсований ключ шифрування чи невідоме значення в налаштуваннях
не мають раптово додати операторові три кліки на кожну з ~92 робіт дня.

Друге, що стережеться, — несумісність із груповою кнопкою «Усі знайдено»
(крок 3.5): чеклист питає про КОНКРЕТНУ роботу в руках, тож поки він
увімкнений, групової кнопки на картці немає взагалі.
"""

import io
import re
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.services.handout_qc import (
    HANDOUT_QC_ITEMS,
    HANDOUT_QC_KEY,
    qc_checklist_enabled,
    set_qc_checklist,
)
from app.settings_store import set_setting

TEMPLATES = Path(__file__).resolve().parents[1] / "app" / "templates"


def _db():
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return Session(engine)


class TestDefaultIsOff:
    def test_a_fresh_database_does_not_ask_for_the_checklist(self):
        with _db() as db:
            assert qc_checklist_enabled(db) is False

    def test_an_unreadable_setting_degrades_to_off(self, monkeypatch):
        """`get_setting` віддає None, коли ключ шифрування розійшовся (§14).
        Це не привід почати вимагати три галочки."""
        import app.services.handout_qc as qc

        monkeypatch.setattr(qc, "get_setting", lambda db, key: None)
        with _db() as db:
            assert qc_checklist_enabled(db) is False

    def test_an_unknown_value_is_not_treated_as_on(self):
        with _db() as db:
            set_setting(db, HANDOUT_QC_KEY, "yes")
            db.commit()
            assert qc_checklist_enabled(db) is False


class TestToggle:
    def test_it_turns_on_and_off(self):
        with _db() as db:
            set_qc_checklist(db, True)
            db.commit()
            assert qc_checklist_enabled(db) is True

            set_qc_checklist(db, False)
            db.commit()
            assert qc_checklist_enabled(db) is False

    def test_the_service_does_not_commit_on_its_own(self):
        """Коміт лишається за роутом, як у решті налаштувань — інакше
        половина форми зберігалась би при помилці в другій половині."""
        with _db() as db:
            set_qc_checklist(db, True)
            db.rollback()
            assert qc_checklist_enabled(db) is False


class TestItems:
    def test_there_are_exactly_three(self):
        """«Три галочки» — не фігура мови: четверта перетворює звірку на
        клацання не дивлячись."""
        assert len(HANDOUT_QC_ITEMS) == 3

    def test_the_first_one_is_the_shape(self):
        """Форма — те, на що дивляться найдовше, і те, що плутають найчастіше
        (спільного ключа рядок↔коронка не існує, §2)."""
        assert "Форма" in HANDOUT_QC_ITEMS[0]


class TestTemplateWiring:
    def _card_source(self):
        return io.open(TEMPLATES / "_handout_cards.html", encoding="utf-8").read()

    def test_the_group_button_is_hidden_while_the_checklist_is_on(self):
        """Крок 3.5 і крок 3.6 суперечать одне одному свідомо: «Усі знайдено»
        одним кліком просто обійшло б чеклист."""
        source = self._card_source()
        assert re.search(
            r"found_n\s*<\s*total_n\s+and\s+not\s+handout_qc", source
        ), "групова кнопка мусить зникати при увімкненому чеклисті"

    def test_only_the_mark_found_form_carries_the_gate(self):
        """`unmark-found` і `unissue` — зняття відмітки; питати три звірки
        перед ВІДКОТОМ означало б карати за виправлення помилки."""
        source = self._card_source()
        forms = re.findall(r"<form[^>]*action=\"([^\"]+)\"[^>]*>", source, re.S)
        gated = re.findall(r"<form[^>]*data-qc=\"1\"[^>]*action=\"([^\"]+)\"", source, re.S)
        gated += [
            action for action, block in
            [(a, b) for a, b in zip(forms, re.findall(r"<form.*?>", source, re.S))]
            if 'data-qc="1"' in block
        ]
        assert all("mark-found" in action and "unmark" not in action for action in gated), gated

    def test_the_dialog_lives_outside_the_swapped_list(self):
        """`#handout-list` HTMX міняє цілком; діалог усередині зник би рівно в
        мить підтвердження."""
        page = io.open(TEMPLATES / "handout.html", encoding="utf-8").read()
        cards = page.index('_handout_cards.html')
        dialog = page.index('_handout_qc.html')
        assert dialog > cards
        assert "_handout_qc.html" not in self._card_source()

    def test_the_confirm_button_starts_disabled(self):
        source = io.open(TEMPLATES / "_handout_qc.html", encoding="utf-8").read()
        assert re.search(r"data-qc-confirm[^>]*disabled", source)

    def test_the_dialog_is_not_the_apex_button(self):
        """`.btn.apex` — єдина залита кнопка на екран, і вона зарезервована за
        прийняттям листа (§14)."""
        source = io.open(TEMPLATES / "_handout_qc.html", encoding="utf-8").read()
        assert "apex" not in source.replace("`.apex`", "")


class TestGateScript:
    def _js(self):
        return io.open(
            Path(__file__).resolve().parents[1] / "app" / "static" / "js" / "handout.js",
            encoding="utf-8",
        ).read()

    def test_the_listener_captures_and_stops_the_event(self):
        """htmx слухає submit на тілі документа. Без фази перехоплення й
        `stopPropagation` запит пішов би ПАРАЛЕЛЬНО з діалогом — тобто гейта
        не було б узагалі, а виглядало б, що він є."""
        js = self._js()
        assert 'document.addEventListener("submit"' in js
        assert "stopPropagation()" in js
        assert re.search(r'addEventListener\("submit",[\s\S]{0,2000}?\},\s*true\)', js)

    def test_a_missing_dialog_does_not_block_the_operator(self):
        """Якщо шаблон діалогу не вставлено — це НАША помилка, і платити за неї
        непрацюючою галочкою оператор не має."""
        js = self._js()
        assert "if (!qcOpen(form)) return;" in js
