from logging.config import fileConfig
import logging
import os

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.db import Base
import app.models  # noqa: F401 -- registers all tables on Base.metadata


config = context.config

if config.config_file_name is not None and not logging.getLogger().handlers:
    fileConfig(config.config_file_name)

# Дефолт мусить збігатися з app.config._db_path(), інакше `alembic upgrade`
# з командного рядка мігрує НЕ ТУ базу: застосунок уже перейменував файл у
# kuubmill.db, а alembic створював поруч нову порожню order_desk.db і
# «успішно» накатував міграції на неї. Пакетний запуск передає DB_PATH явно
# (windows_launcher._run_migrations), тому там це не спливало.
db_path = os.environ.get("DB_PATH", "kuubmill.db")
config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}".replace("%", "%%"))
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
