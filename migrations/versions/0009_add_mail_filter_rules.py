"""Mail filter rules + email filter stamp (triage «Відфільтровані» tab).

Revision ID: 0009_add_mail_filter_rules
Revises: 0008_add_email_seen_at

mail_filter_rules: admin-managed keyword/sender rules that route non-milling
mail (3D print requests, accounting, spam) out of the main triage list into a
separate filtered tab. email_messages gains filter_category + filter_rule_id —
a stamped letter keeps status "нове" and is never deleted; unfiltering is just
clearing the stamp. Existing letters start unstamped (NULL) and stay in the
queue, so the migration changes nothing visible until rules are created.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0009_add_mail_filter_rules"
down_revision: str | None = "0008_add_email_seen_at"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "mail_filter_rules",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("pattern", sa.String(300), nullable=False),
        sa.Column("category", sa.String(100), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("hits", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_by", sa.String(100), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=False),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    # SQLite's batch mode requires NAMED constraints; an inline anonymous
    # ForeignKey raises "Constraint must have a name". Add the columns bare,
    # then create the FK explicitly with a name.
    with op.batch_alter_table("email_messages") as batch_op:
        batch_op.add_column(sa.Column("filter_category", sa.String(100), nullable=True))
        batch_op.add_column(sa.Column("filter_rule_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_email_messages_filter_rule_id",
            "mail_filter_rules",
            ["filter_rule_id"],
            ["id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("email_messages") as batch_op:
        batch_op.drop_constraint("fk_email_messages_filter_rule_id", type_="foreignkey")
        batch_op.drop_column("filter_rule_id")
        batch_op.drop_column("filter_category")
    op.drop_table("mail_filter_rules")
