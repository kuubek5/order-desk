# Деплой (Proxmox / будь-який хост з Docker)

## 1. Секрети

```bash
cp .env.example .env
```

Згенерувати й вписати в `.env`:

```bash
python -c "import secrets; print(secrets.token_hex(32))"   # SESSION_SECRET_KEY
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"   # DB_ENCRYPTION_KEY
```

`GOOGLE_SHEET_ID`/`GOOGLE_SERVICE_ACCOUNT_JSON` можна лишити пустими — задаються пізніше через `/settings`.

## 2. Запуск

```bash
docker compose up -d --build
```

Створює БД (SQLite у `./data/order_desk.db`), піднімає застосунок на `:8000`.

## 3. Перший адмін

Signup-екрана нема — обліковий запис створюється командою всередині контейнера:

```bash
docker compose exec order-desk python -m app.create_user_cli <логін> <пароль> "<Ім'я>" адмін
```

## 4. Налаштування

Відкрити `http://<ip-сервера>:8000/login`, увійти, перейти в **Налаштування** — задати Google Sheet ID, Service Account JSON, IMAP логін/пароль, шлях до `export`.

## 5. Синхронізація (поки вручну, без планувальника)

```bash
docker compose exec order-desk python -m app.sync_cli        # таблиця -> БД
docker compose exec order-desk python -m app.mail_sync_cli   # пошта -> БД (тріаж)
```

## Дані на диску

- `./data/order_desk.db` — база
- `./mail_attachments/` — вкладення листів
- Реальну папку `export/` з готовими роботами змонтувати окремо (див. коментар у `docker-compose.yml`), або задати шлях до неї через `/settings`, якщо вона змонтована деінде в контейнері.
