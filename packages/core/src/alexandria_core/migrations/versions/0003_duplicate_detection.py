"""Duplicate detection: the content digest of a document's pages, and the document a duplicate copies.

Revision ID: 0003_duplicate_detection
Revises: 0002_document_metadata
Create Date: 2026-11-16 09:25:43.348417
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_duplicate_detection"
down_revision: str | None = "0002_document_metadata"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Postgres's default name, which databases from before migrations have too, so the downgrade can drop it there.
DUPLICATE_OF_FOREIGN_KEY = "documents_duplicate_of_fkey"


def upgrade() -> None:
    """Apply this revision."""
    op.add_column("documents", sa.Column("content_digest", sa.Text(), nullable=True))
    op.add_column("documents", sa.Column("duplicate_of", sa.Integer(), nullable=True))
    op.create_index(op.f("ix_documents_content_digest"), "documents", ["content_digest"], unique=False)
    op.create_index(op.f("ix_documents_duplicate_of"), "documents", ["duplicate_of"], unique=False)
    op.create_foreign_key(
        DUPLICATE_OF_FOREIGN_KEY, "documents", "documents", ["duplicate_of"], ["id"], ondelete="SET NULL"
    )


def downgrade() -> None:
    """Revert this revision."""
    op.drop_constraint(DUPLICATE_OF_FOREIGN_KEY, "documents", type_="foreignkey")
    op.drop_index(op.f("ix_documents_duplicate_of"), table_name="documents")
    op.drop_index(op.f("ix_documents_content_digest"), table_name="documents")
    op.drop_column("documents", "duplicate_of")
    op.drop_column("documents", "content_digest")
