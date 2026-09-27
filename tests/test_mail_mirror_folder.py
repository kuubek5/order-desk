"""Тека роботи в дзеркалі черги на екрані пошти.

Бойовий випадок 27.09.26: у блоці «Прийняте з пошти» іконка теки стояла не
біля кожного клієнта. Причина — спільний `attach_export_folder_uris` шукає
лист через `EmailMessage.order_id`, а той ставиться лише для ПЕРШОЇ роботи
листа; багатопартійний лист (кілька клієнтів/кольорів одним листом) лишав
решту робіт без теки, а першій міг віддати теку чужої партії.
"""

from app.models import Attachment, EmailMessage, Order
from app.order_folder import attach_mail_mirror_folder_uris


def _email_with_two_works(db, tmp_path):
    email = EmailMessage(uid="1", uid_validity="v", status="прийнято")
    db.add(email)
    db.flush()

    first = Order(source="email", client_name="LekaLab", source_email_id=email.id)
    second = Order(source="email", client_name="Viktor", source_email_id=email.id)
    db.add_all([first, second])
    db.flush()
    # Як у `mail_accept`: legacy-місток дістається лише першій роботі.
    email.order_id = first.id

    folders = {}
    for order, name in ((first, "LekaLab"), (second, "Viktor")):
        folder = tmp_path / "export" / name / "25.09.26" / "mono a3"
        folder.mkdir(parents=True)
        (folder / "crown.stl").write_text("x")
        folders[order.client_name] = folder

    # Порядок вкладень навмисно ЗВОРОТНИЙ до порядку робіт: саме на ньому
    # старий резолвер віддавав першій роботі теку другої.
    db.add_all([
        Attachment(
            email_message_id=email.id, filename="crown.stl",
            saved_path=str(folders["Viktor"] / "crown.stl"), order_id=second.id,
        ),
        Attachment(
            email_message_id=email.id, filename="crown.stl",
            saved_path=str(folders["LekaLab"] / "crown.stl"), order_id=first.id,
        ),
    ])
    db.commit()
    return email, first, second


def test_every_work_of_one_email_gets_its_own_folder(db_session, tmp_path):
    db = db_session
    _email, first, second = _email_with_two_works(db, tmp_path)

    attach_mail_mirror_folder_uris(db, [first, second])

    assert first.export_folder_uri is not None
    assert second.export_folder_uri is not None, "друга робота листа лишилась без теки"
    assert "LekaLab" in first.export_folder_uri
    assert "Viktor" in second.export_folder_uri


def test_work_without_own_files_falls_back_to_spool(db_session, tmp_path):
    """Лист прийняли без переїзду файлів — відкриваємо спул, не чужий export."""
    db = db_session
    email = EmailMessage(uid="2", uid_validity="v", status="прийнято")
    db.add(email)
    db.flush()
    order = Order(source="email", client_name="Бойко", source_email_id=email.id)
    db.add(order)
    db.flush()

    spool = tmp_path / "mail_attachments" / "2"
    spool.mkdir(parents=True)
    (spool / "crown.stl").write_text("x")
    db.add(Attachment(
        email_message_id=email.id, filename="crown.stl",
        saved_path=str(spool / "crown.stl"), order_id=None,
    ))
    db.commit()

    attach_mail_mirror_folder_uris(db, [order])

    assert order.export_folder_uri is not None
    assert "mail_attachments" in order.export_folder_uri


def test_missing_file_on_disk_does_not_fall_back_to_spool(db_session, tmp_path):
    """Власні файли зникли з диска — порожня клітинка, а НЕ спул листа.

    Спул тут ще може тримати копію тих самих вкладень, але вона застаріла: файли
    переїхали в export і вже звідти кудись поділись. Відкрити спул означало б
    показати оператору коронку, якої в export давно немає.
    """
    db = db_session
    email = EmailMessage(uid="3", uid_validity="v", status="прийнято")
    db.add(email)
    db.flush()
    order = Order(source="email", client_name="Ніхто", source_email_id=email.id)
    db.add(order)
    db.flush()
    spool = tmp_path / "mail_attachments" / "3"
    spool.mkdir(parents=True)
    (spool / "old.stl").write_text("x")
    db.add_all([
        Attachment(
            email_message_id=email.id, filename="crown.stl",
            saved_path=str(tmp_path / "gone" / "crown.stl"), order_id=order.id,
        ),
        Attachment(
            email_message_id=email.id, filename="old.stl",
            saved_path=str(spool / "old.stl"), order_id=None,
        ),
    ])
    db.commit()

    attach_mail_mirror_folder_uris(db, [order])

    assert order.export_folder_uri is None


def test_sheet_work_is_left_alone(db_session, tmp_path):
    """Лабораторний/табличний рядок дзеркало не показує — і теки не отримує."""
    db = db_session
    order = Order(source="lab", client_name="Лаба")
    db.add(order)
    db.commit()

    attach_mail_mirror_folder_uris(db, [order])

    assert order.export_folder_uri is None
    assert order.export_folder_preview_token is None


def test_bound_path_from_accept_wins_over_attachments(db_session, tmp_path, monkeypatch):
    """Тека, яку прийняття листа записало в `Order.export_folder_path`, — перше
    джерело. Її обрав оператор у майстрі пошти, і саме її читає видача; шукати
    теку по файлах, коли шлях уже відомий, — обхідний маневр (власник 27.09.26).
    """
    from app import order_folder

    db = db_session
    export_root = tmp_path / "export"
    bound = export_root / "Юрій Бойко" / "27.09.26" / "mono b2"
    bound.mkdir(parents=True)
    (bound / "crown.stl").write_text("x")
    monkeypatch.setattr(order_folder, "get_export_folder_path", lambda _db: str(export_root))

    email = EmailMessage(uid="9", uid_validity="v", status="прийнято")
    db.add(email)
    db.flush()
    order = Order(
        source="email", client_name="Юрій Бойко", source_email_id=email.id,
        export_folder_path="Юрій Бойко/27.09.26/mono b2",
    )
    db.add(order)
    db.flush()
    # Файл роботи лежить ІНДЕ — якби резолвер і далі йшов від вкладень, він
    # показав би цю теку замість закріпленої.
    other = tmp_path / "somewhere" / "old"
    other.mkdir(parents=True)
    (other / "crown.stl").write_text("x")
    db.add(Attachment(
        email_message_id=email.id, filename="crown.stl",
        saved_path=str(other / "crown.stl"), order_id=order.id,
    ))
    db.commit()

    attach_mail_mirror_folder_uris(db, [order])

    assert order.export_folder_uri is not None
    assert "mono%20b2" in order.export_folder_uri


def test_bound_path_that_no_longer_exists_falls_through(db_session, tmp_path, monkeypatch):
    """Теку перейменували — беремо власні файли роботи, а не мертве посилання."""
    from app import order_folder

    db = db_session
    export_root = tmp_path / "export"
    export_root.mkdir()
    monkeypatch.setattr(order_folder, "get_export_folder_path", lambda _db: str(export_root))

    email = EmailMessage(uid="10", uid_validity="v", status="прийнято")
    db.add(email)
    db.flush()
    order = Order(
        source="email", client_name="Хтось", source_email_id=email.id,
        export_folder_path="Хтось/01.01.26/зникла",
    )
    db.add(order)
    db.flush()
    real = tmp_path / "export" / "Хтось" / "02.01.26" / "mono a2"
    real.mkdir(parents=True)
    (real / "crown.stl").write_text("x")
    db.add(Attachment(
        email_message_id=email.id, filename="crown.stl",
        saved_path=str(real / "crown.stl"), order_id=order.id,
    ))
    db.commit()

    attach_mail_mirror_folder_uris(db, [order])

    assert order.export_folder_uri is not None
    assert "mono%20a2" in order.export_folder_uri
