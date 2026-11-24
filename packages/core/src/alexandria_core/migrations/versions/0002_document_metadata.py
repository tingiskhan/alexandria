"""Document metadata: authors, year, DOI and type, nullable, so existing rows read as unknown.

Revision ID: 0002_document_metadata
Revises: 0001_initial_schema
Create Date: 2026-11-16 09:25:35.972410
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_document_metadata"
down_revision: str | None = "0001_initial_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Apply this revision."""
    op.add_column("documents", sa.Column("authors", postgresql.ARRAY(sa.Text()), nullable=True))
    op.add_column("documents", sa.Column("year", sa.Integer(), nullable=True))
    op.add_column("documents", sa.Column("doi", sa.Text(), nullable=True))
    op.add_column("documents", sa.Column("doc_type", sa.Text(), nullable=True))


def downgrade() -> None:
    """Revert this revision."""
    op.drop_column("documents", "doc_type")
    op.drop_column("documents", "doi")
    op.drop_column("documents", "year")
    op.drop_column("documents", "authors")
