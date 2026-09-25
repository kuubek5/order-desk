"""Розкладка тексту листа для читання (app/mail_body_view.py).

Бойовий лист 25.09.26 (cadcamlab): пересилання, три рядки по суті, підпис
центру, дев'ять файлів ukr.net по три рядки кожен (ім'я, розмір, посилання).
"""

from app.mail_body_view import inline_parts, letter_segments, useful_text

TOKEN = "AwHRf2PsB-f7wbn1LkQ72K_ZQZ5-ehj4a3D4IIA_Q8XX2Oo2YGn0y92b:lHaKxUo4W5POddkz"
BODY = f"""--- Повідомлення, що пересилається ---

Від кого: Iris <irisdent.ua@gmail.com>

Кому: <cadcamlab@ukr.net>

Тема: .

Дата: 17 вересня 2026, 00:01:05

Monolith кольору денис, юшков а3.5, роман а3, лідія а 1

Данні для відправки: Грунський Сергій Миколайович 0683992978
Нова пошта:5 м.Фастів

--

Ваш найбільший CAD CAM Центр в Україні «Sтаханівець»

тел. (095) 435 20 08; (067) 407 69 07

Цей лист містить файли, які будуть видалені автоматично 09.10.2026. Збережіть їх, якщо вони вам потрібні.

16.09.2026-Проєкт деніс цр.constructionInfo

82Kb

https://files.ukr.net/package/item/download?item=1265149288&token={TOKEN}

16.09.2026-Проєкт деніс цр-35-crown_cad.stl

2Mb

https://files.ukr.net/package/item/download?item=1265149293&token={TOKEN}
"""


def _kinds(segments):
    return [s.kind for s in segments]


def test_real_letter_is_split_into_its_parts():
    segments = letter_segments(BODY)
    assert _kinds(segments) == ["forward", "text", "signature", "files"]


def test_forward_header_becomes_fields():
    forward = letter_segments(BODY)[0]
    assert ("Від кого", "Iris <irisdent.ua@gmail.com>") in forward.fields
    assert ("Дата", "17 вересня 2026, 00:01:05") in forward.fields


def test_customer_words_are_kept_whole():
    text = letter_segments(BODY)[1]
    assert text.lines[0] == "Monolith кольору денис, юшков а3.5, роман а3, лідія а 1"
    assert "Нова пошта:5 м.Фастів" in text.lines


def test_files_lose_their_token_links_but_keep_name_and_size():
    files = letter_segments(BODY)[-1]
    assert [(f.name, f.size) for f in files.files] == [
        ("16.09.2026-Проєкт деніс цр.constructionInfo", "82Kb"),
        ("16.09.2026-Проєкт деніс цр-35-crown_cad.stl", "2Mb"),
    ]
    assert "09.10.2026" in files.note


def test_signature_is_separate_from_customer_words():
    signature = letter_segments(BODY)[2]
    assert any("Стаханівець" in line or "Sтаханівець" in line for line in signature.lines)
    assert not any("Sтаханівець" in line for line in letter_segments(BODY)[1].lines)


def test_nothing_is_lost_except_file_links():
    # Кожен непорожній рядок оригіналу, крім посилань файлів, десь показаний.
    segments = letter_segments(BODY)
    shown = set()
    for s in segments:
        shown.update(line.strip() for line in s.lines)
        shown.update(f"{k}: {v}".strip() for k, v in s.fields)
        shown.update(f.name for f in s.files)
        shown.update(f.size for f in s.files)
        if s.note:
            shown.add(s.note)
    missing = [
        line.strip() for line in BODY.splitlines()
        if line.strip() and not line.strip().startswith("https://")
        and not line.strip().startswith("---") and line.strip() != "--"
        and line.strip() not in shown
    ]
    assert missing == []


def test_card_preview_is_only_the_customer_words():
    preview = useful_text(letter_segments(BODY))
    assert preview.startswith("Monolith кольору денис")
    assert "https" not in preview and "Sтаханівець" not in preview


def test_plain_letter_is_one_text_segment():
    segments = letter_segments("Добрий день!\nМоно а2, 3 одиниці.\n\nДякую")
    assert _kinds(segments) == ["text"]


def test_shades_are_highlighted_and_links_folded():
    parts = inline_parts("юшков а3.5, роман а3, лідія а 1 — https://drive.google.com/file/d/x")
    assert ("shade", "а3.5") in parts and ("shade", "а3") in parts and ("shade", "а 1") in parts
    assert ("link", "drive.google.com") in parts


def test_preposition_and_dates_are_not_shades():
    parts = inline_parts("роботи в 2 екземплярах, 17 вересня 2026, тел 095 435")
    assert not [p for p in parts if p[0] == "shade"]


def test_empty_body():
    assert letter_segments(None) == []
    assert useful_text([]) == ""


PROTON = """Sent with [Proton Mail](https://proton.me/mail/home) secure email.

------- Forwarded Message -------
От: shevchukr <shevchukr@proton.me>
Дата: четверг, 24 сентября 2026 г., 23:07
Тема: Переслано: pmma a3
Кому: kyivletter@ukr.net <kyivletter@ukr.net>

> Sent with [Proton Mail](https://proton.me/mail/home) secure email.
>
> ------- Forwarded Message -------
> От: shevchukr <shevchukr@proton.me>
> Дата: четверг, 24 сентября 2026 г., 22:19
> Тема: Переслано: pmma a3
>
>> pmma a3, 2 одиниці
"""


def test_proton_nested_forwards_are_recognised_through_quotes():
    # Лист 39 (25.09.26): прев'ю картки показувало «Sent with Proton Mail > >».
    segments = letter_segments(PROTON)
    assert _kinds(segments) == ["mailer", "forward", "mailer", "forward", "text"]
    assert segments[0].lines == ["Sent with Proton Mail secure email."]
    assert ("Дата", "четверг, 24 сентября 2026 г., 22:19") in segments[3].fields


def test_preview_skips_mailer_lines_and_quote_marks():
    assert useful_text(letter_segments(PROTON)) == "pmma a3, 2 одиниці"
