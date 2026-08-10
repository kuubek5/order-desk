# Розгортання Order Desk у Docker

## 1. Підготовка

Скопіюйте `.env.example` у `.env` і згенеруйте два різні ключі. Команди працюють у
PowerShell, CMD і Linux shell:

```console
python -c "import secrets; print(secrets.token_urlsafe(48))"
python -c "import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"
```

Перший результат запишіть у `SESSION_SECRET_KEY`, другий — у
`DB_ENCRYPTION_KEY`. Не комітьте `.env`. Збережіть резервну копію
`DB_ENCRYPTION_KEY` у захищеному місці: без нього секрети в БД неможливо
розшифрувати.

Створіть каталоги bind mounts до першого запуску:

```console
mkdir credentials export technician_files mail_attachments
```

На Linux контейнер працює як UID/GID `10001`. Каталоги, куди він записує дані,
мають бути доступні цьому користувачу:

```console
sudo chown -R 10001:10001 export mail_attachments
```

`technician_files` монтується лише для читання. Якщо використовується JSON-файл
сервісного акаунта замість введення JSON через екран налаштувань, переконайтеся,
що UID `10001` може прочитати файл у `credentials`.

## 2. Шляхи хоста і контейнера

У `docker-compose.yml` шлях ліворуч — реальний шлях на Docker-хості; його можна
замінити на Windows share або Linux mount. Шлях праворуч змінювати не потрібно:

| Призначення | Шлях у контейнері |
|---|---|
| Готові роботи / export | `/app/export` |
| Файли техніків | `/app/technician_files` |
| Тимчасові вкладення пошти | `/app/mail_attachments` |
| SQLite БД | `/app/data/order_desk.db` |

В адмінському екрані налаштувань вказуйте саме container-side шляхи
`/app/export` та `/app/technician_files`. Windows-шляхи на кшталт `C:\export`
всередині Linux-контейнера не працюють.

## 3. Перший запуск і адміністратор

Зберіть образ і один раз створіть першого адміністратора. Пароль у команді може
потрапити в історію shell, тому використайте тимчасовий сильний пароль і одразу
змініть його через адмінський екран:

```console
docker compose build
docker compose run --rm order-desk python -m app.create_user_cli admin "TEMP_STRONG_PASSWORD" "Адміністратор" "адмін"
docker compose up -d
```

Перевірка стану:

```console
docker compose ps
docker compose logs --tail=100 order-desk
```

Healthcheck звертається до `http://127.0.0.1:8000/health` усередині контейнера і
не повертає секретів та не змінює БД. Інтерфейс доступний на порту `8000`
Docker-хоста.

## 4. Міграції та існуюча БД

Перед кожним звичайним запуском entrypoint перевіряє БД і виконує
`alembic upgrade head`. Для нової/порожньої БД baseline створюється автоматично.
Для БД, яка вже має `alembic_version`, застосовуються тільки ще не виконані
міграції.

Існуючу БД без таблиці `alembic_version` entrypoint **ніколи не stamp-ить
автоматично**. Він порівнює набір таблиць і колонок із baseline та зупиняє запуск,
щоб помилкова схема не була позначена актуальною.

Для одноразового підключення legacy БД:

1. Зупиніть застосунок і зробіть копію SQLite-файлу в тому самому volume:

   ```console
   docker compose stop order-desk
   docker compose run --rm --entrypoint sh order-desk -c "cp /app/data/order_desk.db /app/data/order_desk.db.before-alembic"
   ```

   Після цього скопіюйте backup із Docker volume на інший носій. Не видаляйте
   захищену резервну копію `DB_ENCRYPTION_KEY`.

2. Запустіть read-only структурну перевірку. Код `3` тут означає «схема сумісна,
   потрібен explicit stamp»; код `4` — схема відрізняється, stamp робити не можна:

   ```console
   docker compose run --rm --entrypoint python order-desk /app/scripts/migration_guard.py
   ```

3. Лише якщо перевірка повідомила `legacy schema matches baseline`, позначте
   baseline і запустіть сервіс:

   ```console
   docker compose run --rm --entrypoint alembic order-desk stamp 0001_initial
   docker compose up -d
   ```

`stamp` не змінює робочі таблиці й дані; він лише додає версію схеми. Якщо guard
повернув код `4`, потрібна окрема міграція після ручного порівняння, а не `stamp`.

## 5. Persistence і резервні копії

БД зберігається в named volume `order_desk_data`; `docker compose down` її не
видаляє, а `docker compose down -v` — видаляє. Для резервної копії зупиніть запис
у застосунок і копіюйте SQLite-файл разом із захищеною копією
`DB_ENCRYPTION_KEY`.
