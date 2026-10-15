"""SQLite access for the maritime ground-truth store."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from applications.maritime_threat_detection import DB_PATH

SCHEMA_PATH: Path = Path(__file__).resolve().parent / "schema.sql"


def connect(db_path: Path = DB_PATH, *, read_only: bool = False) -> sqlite3.Connection:
    """Open the database with row access by column name.

    Agent tools open it read-only: the ground truth an auditor checks against must not be
    mutable by the system being audited.
    """
    if read_only:
        conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    else:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def apply_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
