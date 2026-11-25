"""Store a signed-in user's profile photo.

Revision ID: 0039
Revises: 0038
"""

import sqlalchemy as sa
from alembic import op

revision = "0039"
down_revision = "0038"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("users", sa.Column("profile_photo", sa.LargeBinary(), nullable=True))
    op.add_column(
        "users", sa.Column("profile_photo_content_type", sa.String(32), nullable=True)
    )


def downgrade():
    op.drop_column("users", "profile_photo_content_type")
    op.drop_column("users", "profile_photo")
