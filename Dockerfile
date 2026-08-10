FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app app
COPY alembic.ini .
COPY migrations migrations
COPY scripts scripts
COPY docker-entrypoint.sh .
COPY DEPLOYMENT.md .

# Keep a stable UID/GID so Linux bind-mount permissions can be prepared on the
# host.  The named data volume inherits ownership from this directory on its
# first mount.
RUN groupadd --system --gid 10001 orderdesk \
    && useradd --system --uid 10001 --gid orderdesk --no-create-home \
        --home-dir /nonexistent --shell /usr/sbin/nologin orderdesk \
    && mkdir -p /app/data \
    && chown orderdesk:orderdesk /app/data

EXPOSE 8000

USER orderdesk

ENTRYPOINT ["sh", "/app/docker-entrypoint.sh"]
CMD ["uvicorn", "app.web:app", "--host", "0.0.0.0", "--port", "8000"]
