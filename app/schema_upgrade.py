"""Add columns that the code expects but an older database does not have yet.

`Base.metadata.create_all` creates missing TABLES but never adds missing COLUMNS, so after an update that adds a
field the old database crashed with "column ... does not exist". This closes that gap.

It only ever ADDS. It never changes, renames or deletes anything, and it leaves a column alone (with a warning)
if it cannot be added safely - a NOT NULL column with no default would fail on a table that already has rows.
For anything bigger than "add a column", use a real migration tool.
"""
import logging
from typing import List

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.schema import CreateColumn

from .models import Base

log = logging.getLogger(__name__)


def add_missing_columns(conn: Connection) -> List[str]:
    """Run on a (sync) connection. Returns what was added, e.g. ['tasks.status_check']."""
    insp = inspect(conn)
    added: List[str] = []
    for table in Base.metadata.sorted_tables:
        if not insp.has_table(table.name):
            continue                                   # create_all makes whole new tables
        existing = {c["name"] for c in insp.get_columns(table.name)}
        new_cols = []
        for col in table.columns:
            if col.name in existing:
                continue
            if not col.nullable and col.server_default is None and not col.primary_key:
                log.warning("Cannot add %s.%s automatically (NOT NULL without a default); add it by hand.",
                            table.name, col.name)
                continue
            ddl = str(CreateColumn(col).compile(dialect=conn.dialect))
            conn.execute(text(f"ALTER TABLE {conn.dialect.identifier_preparer.format_table(table)} ADD COLUMN {ddl}"))
            new_cols.append(col.name)
            added.append(f"{table.name}.{col.name}")
        for idx in table.indexes:                      # an index on a column we just added
            if any(c.name in new_cols for c in idx.columns):
                idx.create(conn, checkfirst=True)
    if added:
        log.info("Database upgraded: added column(s) %s", ", ".join(added))
    return added
