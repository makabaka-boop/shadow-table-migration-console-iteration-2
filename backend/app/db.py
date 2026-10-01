from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import sqlite3


# SQLite stores integers as signed 64-bit values.  Values outside this range
# would be promoted to TEXT by SQLite and therefore must not pass an "integer"
# validation rule.
INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1

LEGACY_TABLE = "legacy_records"
CURRENT_TABLE = "records"
HISTORY_TABLE = "record_versions"
MIGRATIONS_TABLE = "migrations"
PREVIEW_META_TABLE = "migration_preview_meta"

TARGET_COLUMNS = ("id", "code", "label")

SEED_LEGACY_ROWS = [
    (1, "A-001", "  Alpha  ", "legacy note 1"),
    (2, "B-002", "Beta", "legacy note 2"),
    (3, "C-003", "  Gamma", "legacy note 3"),
]


class ServiceError(Exception):
    """Application error that maps to an HTTP response."""

    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def default_db_path() -> str:
    return os.environ.get("MIGRATION_DB", str(Path(__file__).resolve().parents[1] / "data" / "app.db"))


def _connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or default_db_path()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


@contextmanager
def db_session(db_path: str | None = None) -> Iterator[sqlite3.Connection]:
    conn = _connect(db_path)
    try:
        yield conn
    finally:
        conn.close()


def init_db(db_path: str | None = None, *, reset: bool = False) -> None:
    with db_session(db_path) as conn:
        if reset:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            dynamic_tables = [
                row[0]
                for row in conn.execute(
                    """
                    SELECT name FROM sqlite_master
                     WHERE type='table'
                       AND (name LIKE 'migration_shadow_%' OR name LIKE 'records_pending_%')
                    """
                )
            ]
            for table_name in dynamic_tables:
                conn.execute(f'DROP TABLE IF EXISTS "{table_name}"')
            for table_name in (
                "legacy_records",
                "records",
                "record_versions",
                "record_version_rows",
                "migrations",
                "migration_preview_meta",
                "revision_meta",
                "fault_injection_state",
            ):
                conn.execute(f"DROP TABLE IF EXISTS {table_name}")
            for trigger_name in (
                "legacy_records_revision_ai",
                "legacy_records_revision_au",
                "legacy_records_revision_ad",
                "record_version_rows_readonly_insert",
                "record_version_rows_readonly_update",
                "record_version_rows_readonly_delete",
                "record_versions_readonly_update",
                "record_versions_readonly_delete",
            ):
                conn.execute(f"DROP TRIGGER IF EXISTS {trigger_name}")

        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(
            f"""
            CREATE TABLE IF NOT EXISTS revision_meta (
                scope TEXT PRIMARY KEY,
                revision INTEGER NOT NULL CHECK (revision >= 0)
            );

            CREATE TABLE IF NOT EXISTS {LEGACY_TABLE} (
                legacy_id INTEGER PRIMARY KEY,
                code TEXT NOT NULL UNIQUE,
                raw_name TEXT,
                note TEXT
            );

            CREATE TABLE IF NOT EXISTS {CURRENT_TABLE} (
                id INTEGER PRIMARY KEY,
                code TEXT NOT NULL UNIQUE,
                label TEXT NOT NULL,
                legacy_id INTEGER NOT NULL UNIQUE
            );

            CREATE TABLE IF NOT EXISTS {MIGRATIONS_TABLE} (
                migration_id INTEGER PRIMARY KEY AUTOINCREMENT,
                preview_id TEXT NOT NULL UNIQUE,
                source_revision INTEGER NOT NULL,
                committed_revision INTEGER NOT NULL,
                row_count INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            );

            CREATE TABLE IF NOT EXISTS {HISTORY_TABLE} (
                version_id INTEGER PRIMARY KEY,
                migration_id INTEGER NOT NULL UNIQUE,
                replaced_table TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                source_revision INTEGER NOT NULL,
                row_count INTEGER NOT NULL,
                locked INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY(migration_id) REFERENCES {MIGRATIONS_TABLE}(migration_id)
            );

            CREATE TABLE IF NOT EXISTS record_version_rows (
                version_id INTEGER NOT NULL,
                id INTEGER NOT NULL,
                code TEXT NOT NULL,
                label TEXT NOT NULL,
                legacy_id INTEGER NOT NULL,
                PRIMARY KEY(version_id, id),
                FOREIGN KEY(version_id) REFERENCES {HISTORY_TABLE}(version_id)
            );

            CREATE TRIGGER IF NOT EXISTS record_versions_readonly_update
            BEFORE UPDATE ON record_versions
            WHEN OLD.locked = 1
            BEGIN
                SELECT RAISE(ABORT, 'retained history metadata is read-only');
            END;

            CREATE TRIGGER IF NOT EXISTS record_versions_readonly_delete
            BEFORE DELETE ON record_versions
            WHEN OLD.locked = 1
            BEGIN
                SELECT RAISE(ABORT, 'retained history metadata is read-only');
            END;

            CREATE TRIGGER IF NOT EXISTS record_version_rows_readonly_insert
            BEFORE INSERT ON record_version_rows
            WHEN EXISTS (
                SELECT 1 FROM record_versions
                 WHERE version_id = NEW.version_id AND locked = 1
            )
            BEGIN
                SELECT RAISE(ABORT, 'retained history versions are read-only');
            END;

            CREATE TRIGGER IF NOT EXISTS record_version_rows_readonly_update
            BEFORE UPDATE ON record_version_rows
            BEGIN
                SELECT RAISE(ABORT, 'retained history versions are read-only');
            END;

            CREATE TRIGGER IF NOT EXISTS record_version_rows_readonly_delete
            BEFORE DELETE ON record_version_rows
            BEGIN
                SELECT RAISE(ABORT, 'retained history versions are read-only');
            END;

            CREATE TABLE IF NOT EXISTS {PREVIEW_META_TABLE} (
                preview_id TEXT PRIMARY KEY,
                source_revision INTEGER NOT NULL,
                row_count INTEGER NOT NULL,
                mappings_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            );

            CREATE TABLE IF NOT EXISTS fault_injection_state (
                name TEXT PRIMARY KEY,
                enabled INTEGER NOT NULL CHECK (enabled IN (0, 1))
            );
            """
        )

        conn.execute(
            "INSERT OR IGNORE INTO revision_meta(scope, revision) VALUES ('legacy_records', 0)"
        )
        conn.executescript(
            """
            CREATE TRIGGER IF NOT EXISTS legacy_records_revision_ai
            AFTER INSERT ON legacy_records
            BEGIN
                UPDATE revision_meta
                   SET revision = revision + 1
                 WHERE scope = 'legacy_records';
            END;

            CREATE TRIGGER IF NOT EXISTS legacy_records_revision_au
            AFTER UPDATE ON legacy_records
            BEGIN
                UPDATE revision_meta
                   SET revision = revision + 1
                 WHERE scope = 'legacy_records';
            END;

            CREATE TRIGGER IF NOT EXISTS legacy_records_revision_ad
            AFTER DELETE ON legacy_records
            BEGIN
                UPDATE revision_meta
                   SET revision = revision + 1
                 WHERE scope = 'legacy_records';
            END;
            """
        )

        existing = conn.execute(f"SELECT COUNT(*) FROM {LEGACY_TABLE}").fetchone()[0]
        if existing == 0:
            conn.executemany(
                f"INSERT INTO {LEGACY_TABLE}(legacy_id, code, raw_name, note) VALUES (?, ?, ?, ?)",
                SEED_LEGACY_ROWS,
            )


def get_revision(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT revision FROM revision_meta WHERE scope='legacy_records'"
    ).fetchone()
    return int(row[0]) if row else 0
