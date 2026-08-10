#!/bin/sh
set -u

python /app/scripts/migration_guard.py
guard_status=$?

case "$guard_status" in
  0)
    alembic upgrade head || exit $?
    ;;
  3)
    echo "Startup refused: back up this legacy DB, validate it, then explicitly stamp baseline." >&2
    echo "See /app/DEPLOYMENT.md in the source repository for exact commands." >&2
    exit 78
    ;;
  *)
    echo "Startup refused: database migration guard failed (exit $guard_status)." >&2
    exit "$guard_status"
    ;;
esac

exec "$@"
