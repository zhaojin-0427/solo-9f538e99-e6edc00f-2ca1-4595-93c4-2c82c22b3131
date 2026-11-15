"""SQLite connection management and schema.

Concurrency model
-----------------
* The database runs in WAL mode: readers never block a writer and a writer
  never blocks readers.
* Every registration takes an ``IMMEDIATE`` transaction, which acquires the
  write lock up front; concurrent registrations therefore serialize instead of
  receiving "database is locked" on commit.
* Reads run inside an explicit deferred transaction against one connection, so
  a query always observes one consistent snapshot: either the state before a
  committed registration or the state after it, never a partial batch.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from collections.abc import Iterator

DEFAULT_DB_PATH = os.environ.get(
    "LINEAGE_DB_PATH", "/data/lineage.db"
)

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS nodes (
    name       TEXT PRIMARY KEY,
    kind       TEXT NOT NULL CHECK (kind IN ('source', 'derived', 'report'))
);

CREATE TABLE IF NOT EXISTS candidate_groups (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    node_name  TEXT NOT NULL REFERENCES nodes(name) ON DELETE CASCADE,
    position   INTEGER NOT NULL,
    UNIQUE (node_name, position)
);

CREATE TABLE IF NOT EXISTS group_inputs (
    group_id    INTEGER NOT NULL
                REFERENCES candidate_groups(id) ON DELETE CASCADE,
    input_name  TEXT NOT NULL REFERENCES nodes(name),
    position    INTEGER NOT NULL,
    PRIMARY KEY (group_id, input_name)
);

CREATE INDEX IF NOT EXISTS idx_groups_node
    ON candidate_groups(node_name);
CREATE INDEX IF NOT EXISTS idx_inputs_group
    ON group_inputs(group_id);
CREATE INDEX IF NOT EXISTS idx_inputs_name
    ON group_inputs(input_name);
"""


def connect(db_path: str | None = None) -> sqlite3.Connection:
    """Open a connection with safe defaults for a multi-threaded web app."""
    path = db_path or DEFAULT_DB_PATH
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(
        path,
        timeout=10.0,
        check_same_thread=False,
        isolation_level=None,  # autocommit; transactions are explicit
    )
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {10_000}")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def init_db(db_path: str | None = None) -> None:
    """Create the schema (idempotent)."""
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


@contextmanager
def snapshot(db_path: str | None = None) -> Iterator[sqlite3.Connection]:
    """Read-only snapshot transaction.

    All reads through one connection see the same committed snapshot, even if
    another thread commits a registration while the request is in progress.
    """
    conn = connect(db_path)
    try:
        conn.execute("BEGIN")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


@contextmanager
def immediate_write(db_path: str | None = None) -> Iterator[sqlite3.Connection]:
    """Serialised write transaction (``BEGIN IMMEDIATE``).

    The lock is acquired before any work, so two concurrent batches cannot
    interleave. Any exception rolls the whole batch back.
    """
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
