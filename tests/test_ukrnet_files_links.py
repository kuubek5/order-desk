"""Великі файли ukr.net («буде видалено автоматично 09.10») — посилання в HTML.

Бойовий лист 25.09.26 (cadcamlab, 9 STL): HTML-лист без текстової частини,
файли — посилання `files.ukr.net/package/item/download?item=…&token=…`. Доти
HTML→текст викидав href, а шаблон знав лише dl/edisk: лист лежав у тріажі з
іменами файлів і без жодного способу їх скачати.
"""

from types import SimpleNamespace

from app.link_attachments import extract_download_links
from app.mail_reader import _refresh_links_in_body, html_to_plain_text

URL = (
    "https://files.ukr.net/package/item/download?item=1265149288"
    "&token=AwHRf2PsB-f7wbn1LkQ72K_ZQZ5-ehj4a3D4IIA"
)
HTML = (
    "<div>Monolith кольору денис, юшков а3.5</div>"
    '<div><a href="tel:+380683992978">0683992978</a></div>'
    '<div><a href="https://stahanovets.example/">Стаханівець</a></div>'
    "<div>Цей лист містить файли, які будуть видалені автоматично 09.10.2026.</div>"
    f'<div><a href="{URL.replace("&", "&amp;")}">16.09.2026-Проєкт деніс цр-35-crown_cad.stl</a></div>'
)


def test_html_keeps_file_host_links_and_drops_the_rest():
    text = html_to_plain_text(HTML)
    assert URL in text  # &amp; розкодовано
    assert "16.09.2026-Проєкт деніс цр-35-crown_cad.stl" in text
    assert "tel:" not in text and "stahanovets" not in text


def test_files_ukr_net_link_is_recognised_with_short_label():
    links = extract_download_links(html_to_plain_text(HTML))
    assert [link.url for link in links] == [URL]
    assert links[0].kind == "ukrnet"
    assert links[0].display == "ukr.net · файл 1265149288"


def test_old_body_without_links_is_refreshed_on_manual_download():
    email = SimpleNamespace(body_text="16.09.2026-Проєкт деніс цр-35-crown_cad.stl")
    _refresh_links_in_body(email, SimpleNamespace(text="", html=HTML))
    assert URL in email.body_text


def test_parallel_link_marks_do_not_overwrite_each_other(db_engine):
    """Загублене оновлення: обидва запити тримали СТАРИЙ список, і в Python
    останній запис перемагав. Атомарний UPDATE додає до того, що в базі зараз."""
    import json

    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from app.models import EmailMessage
    from app.routers.mail import _mark_link_handled

    with Session(db_engine) as s:
        s.add(EmailMessage(uid="1", status="нове", handled_link_refs=json.dumps(["a"])))
        s.commit()
        email_id = s.scalars(select(EmailMessage.id)).one()

    first, second = Session(db_engine), Session(db_engine)
    # Обидва «прочитали» лист до того, як хтось записав.
    first.get(EmailMessage, email_id)
    second.get(EmailMessage, email_id)
    _mark_link_handled(first, email_id, "b")
    first.commit()
    _mark_link_handled(second, email_id, "c")
    _mark_link_handled(second, email_id, "c")  # повтор не дублює
    second.commit()
    first.close()
    second.close()

    with Session(db_engine) as s:
        refs = json.loads(s.get(EmailMessage, email_id).handled_link_refs)
    assert refs == ["a", "b", "c"]


def test_body_is_left_alone_when_nothing_new():
    email = SimpleNamespace(body_text="текст оператора")
    _refresh_links_in_body(email, SimpleNamespace(text="", html="<p>без посилань</p>"))
    assert email.body_text == "текст оператора"
    # Лист із текстовою частиною будувався НЕ з HTML — не чіпаємо.
    _refresh_links_in_body(email, SimpleNamespace(text="plain", html=HTML))
    assert email.body_text == "текст оператора"
