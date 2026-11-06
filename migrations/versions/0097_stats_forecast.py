"""Прогноз статистики: forecast_snapshots і stats_anomaly_days.

Revision ID: 0097_stats_forecast
Revises: 0096_mail_dock_retired_filters

Прогноз пишеться наперед, щоб факт звірявся із записаним, а не з перерахованим заднім
числом (STATS_BRIEF.md §6). Нетипові дні позначає адмін, прогноз на них не вчиться.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0097_stats_forecast"
down_revision: str | None = "0096_mail_dock_retired_filters"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "forecast_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("made_on", sa.Date(), nullable=False),
        sa.Column("target_day", sa.Date(), nullable=False),
        sa.Column("series", sa.String(length=20), nullable=False),
        sa.Column("value", sa.Float(), nullable=False),
        sa.Column("low", sa.Float(), nullable=True),
        sa.Column("high", sa.Float(), nullable=True),
        sa.Column("model_version", sa.String(length=40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=False), nullable=False),
        sa.UniqueConstraint("made_on", "target_day", "series", name="uq_forecast_snapshot"),
    )
    op.create_index("ix_forecast_snapshots_made_on", "forecast_snapshots", ["made_on"])
    op.create_index("ix_forecast_snapshots_target_day", "forecast_snapshots", ["target_day"])
    op.create_table(
        "stats_anomaly_days",
        sa.Column("day", sa.Date(), primary_key=True),
        sa.Column("marked_by", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=False), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("stats_anomaly_days")
    op.drop_index("ix_forecast_snapshots_target_day", table_name="forecast_snapshots")
    op.drop_index("ix_forecast_snapshots_made_on", table_name="forecast_snapshots")
    op.drop_table("forecast_snapshots")
