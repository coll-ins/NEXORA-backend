"""Optional registration details on a SACCO

Revision ID: 0033
Revises: 0032

Registration number, SASRA licence number and registration date. All three are
nullable: they are recorded when a SACCO gives them and are not required yet.
Each number is unique among the SACCOs that have one.
"""

import sqlalchemy as sa
from alembic import op

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None

NUMBER = "'^[A-Za-z0-9][A-Za-z0-9 ./-]{1,38}[A-Za-z0-9]$'"


def upgrade():
    op.add_column("saccos", sa.Column("registration_number", sa.String(40)))
    op.add_column("saccos", sa.Column("sasra_licence_number", sa.String(40)))
    op.add_column("saccos", sa.Column("registration_date", sa.Date()))
    op.create_check_constraint(
        "ck_saccos_registration_number", "saccos", f"registration_number ~ {NUMBER}"
    )
    op.create_check_constraint(
        "ck_saccos_sasra_licence_number", "saccos", f"sasra_licence_number ~ {NUMBER}"
    )
    op.create_check_constraint(
        "ck_saccos_registration_date", "saccos", "registration_date >= DATE '1900-01-01'"
    )
    op.create_index(
        "uq_saccos_registration_number",
        "saccos",
        [sa.text("lower(registration_number)")],
        unique=True,
        postgresql_where=sa.text("registration_number IS NOT NULL"),
    )
    op.create_index(
        "uq_saccos_sasra_licence_number",
        "saccos",
        [sa.text("lower(sasra_licence_number)")],
        unique=True,
        postgresql_where=sa.text("sasra_licence_number IS NOT NULL"),
    )


def downgrade():
    op.drop_index("uq_saccos_sasra_licence_number", "saccos")
    op.drop_index("uq_saccos_registration_number", "saccos")
    op.drop_constraint("ck_saccos_registration_date", "saccos", type_="check")
    op.drop_constraint("ck_saccos_sasra_licence_number", "saccos", type_="check")
    op.drop_constraint("ck_saccos_registration_number", "saccos", type_="check")
    op.drop_column("saccos", "registration_date")
    op.drop_column("saccos", "sasra_licence_number")
    op.drop_column("saccos", "registration_number")
