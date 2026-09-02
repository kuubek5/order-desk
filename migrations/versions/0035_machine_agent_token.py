"""Верстати: токен HTTP-агента (Go kmill-agent).

Revision ID: 0035_machine_agent_token
Revises: 0034_add_feedback_reports

Крім VNC, верстат можна читати через власний HTTP-агент (agent/main.go): він
віддає кадр екрана на GET /capture з токеном, і на відміну від VNC БАЧИТЬ синю
смугу відсотка RemiCORE. Токен зберігається зашифровано (як паролі). Непорожній
`agent_token_encrypted` → CRM тягне кадр по HTTP замість VNC.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0035_machine_agent_token"
down_revision: str | None = "0034_add_feedback_reports"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "machines",
        sa.Column("agent_token_encrypted", sa.String(length=500), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("machines", "agent_token_encrypted")
