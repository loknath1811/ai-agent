'''Alter tasks.status to a PostgreSQL enum'''

from alembic import op
import sqlalchemy as sa
import sqlalchemy.dialects.postgresql as pg

# revision identifiers, used by Alembic.
revision = "20240930_alter_task_status_enum"
down_revision = None
branch_labels = None
depends_on = None

def upgrade() -> None:
    # 1️⃣ create the enum type
    taskstatus = pg.ENUM(
        "pending",
        "on-track",
        "blocked",
        "delayed",
        "done",
        name="taskstatus",
    )
    taskstatus.create(op.get_bind())

    # 2️⃣ map any existing invalid values to 'pending'
    op.execute(
        """
        UPDATE tasks
        SET status = 'pending'
        WHERE status NOT IN ('pending','on-track','blocked','delayed','done');
        """
    )

    # 3️⃣ alter column using explicit cast
    op.alter_column(
        "tasks",
        "status",
        type_=taskstatus,
        postgresql_using="status::taskstatus",
        server_default=sa.text("'pending'"),
    )

def downgrade() -> None:
    # revert to plain text column
    op.alter_column(
        "tasks",
        "status",
        type_=sa.String(),
        server_default=None,
    )
    # drop enum type
    taskstatus = pg.ENUM(name="taskstatus")
    taskstatus.drop(op.get_bind())
