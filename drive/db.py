import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

BUSY_TIMEOUT_SECONDS = 10
FOLDER_MODE = 0o700
FILE_MODE = 0o600

NODES_COLUMNS = """(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_id INTEGER REFERENCES nodes(id),
    name TEXT NOT NULL,
    name_key TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('folder', 'file')),
    size INTEGER NOT NULL DEFAULT 0,
    blob TEXT,
    mime TEXT NOT NULL DEFAULT '',
    starred INTEGER NOT NULL DEFAULT 0,
    in_bin INTEGER NOT NULL DEFAULT 0,
    trashed_at REAL,
    created_at REAL NOT NULL,
    modified_at REAL NOT NULL,
    opened_at REAL,
    content TEXT
)"""
NODES_OLD_COLUMNS = ("id, parent_id, name, name_key, kind, size, blob, mime, starred, in_bin, trashed_at, created_at,"
                     " modified_at, opened_at")
SHARES_COLUMNS = """(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token TEXT NOT NULL UNIQUE,
    node_id INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    created_at REAL NOT NULL,
    expires_at REAL,
    password_hash TEXT,
    revoked_at REAL
)"""

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS nodes {NODES_COLUMNS};
CREATE UNIQUE INDEX IF NOT EXISTS live_names ON nodes (ifnull(parent_id, 0), name_key) WHERE in_bin = 0;
CREATE INDEX IF NOT EXISTS nodes_by_parent ON nodes (parent_id);
CREATE INDEX IF NOT EXISTS nodes_in_bin ON nodes (trashed_at) WHERE trashed_at IS NOT NULL;
CREATE TABLE IF NOT EXISTS shares {SHARES_COLUMNS};
CREATE INDEX IF NOT EXISTS shares_by_node ON shares (node_id);
CREATE TABLE IF NOT EXISTS uploads (
    id TEXT PRIMARY KEY,
    parent_id INTEGER,
    folders TEXT NOT NULL DEFAULT '[]',
    name TEXT NOT NULL,
    size INTEGER NOT NULL,
    modified_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS upload_batches (
    batch TEXT NOT NULL,
    parent_id INTEGER NOT NULL,
    path TEXT NOT NULL,
    folder_id INTEGER NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (batch, parent_id, path)
);
CREATE TABLE IF NOT EXISTS sync_connections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    provider TEXT NOT NULL,
    folder_id INTEGER NOT NULL,
    remote_path TEXT NOT NULL DEFAULT '',
    direction TEXT NOT NULL,
    interval_minutes INTEGER NOT NULL DEFAULT 0,
    paused INTEGER NOT NULL DEFAULT 0,
    server_url TEXT NOT NULL DEFAULT '',
    username TEXT NOT NULL DEFAULT '',
    account TEXT NOT NULL DEFAULT '',
    secret BLOB,
    sign_in_state TEXT,
    sign_in_verifier BLOB,
    sign_in_redirect TEXT,
    sign_in_started_at REAL,
    created_at REAL NOT NULL,
    previewed_at REAL,
    last_preview TEXT,
    last_run_at REAL,
    last_finished_at REAL,
    last_success_at REAL,
    last_status TEXT NOT NULL DEFAULT 'never',
    last_counts TEXT NOT NULL DEFAULT '{{}}',
    last_errors TEXT NOT NULL DEFAULT '[]',
    last_skipped TEXT NOT NULL DEFAULT '[]',
    last_held TEXT,
    last_held_files TEXT
);
CREATE TABLE IF NOT EXISTS sync_entries (
    connection_id INTEGER NOT NULL REFERENCES sync_connections(id) ON DELETE CASCADE,
    path TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('folder', 'file')),
    local_id INTEGER,
    local_version TEXT NOT NULL DEFAULT '',
    remote_id TEXT,
    remote_version TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (connection_id, path)
);
"""

REBUILD_WITH_LASTING_IDS = f"""
CREATE TABLE nodes_rebuilt {NODES_COLUMNS};
INSERT INTO nodes_rebuilt ({NODES_OLD_COLUMNS}) SELECT {NODES_OLD_COLUMNS} FROM nodes;
CREATE TABLE shares_rebuilt {SHARES_COLUMNS};
INSERT INTO shares_rebuilt SELECT id, token, node_id, created_at, expires_at, password_hash, revoked_at FROM shares;
DROP TABLE shares;
DROP TABLE nodes;
ALTER TABLE nodes_rebuilt RENAME TO nodes;
ALTER TABLE shares_rebuilt RENAME TO shares;
"""


def _create_private_file(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_MODE)
    except FileExistsError:
        return
    os.close(descriptor)


ADDED_COLUMNS = (("sync_connections", "last_held", "TEXT"), ("sync_connections", "last_held_files", "TEXT"))


def _add_column(connection: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    try:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    except sqlite3.OperationalError as error:
        if f"duplicate column name: {column}" not in str(error):
            raise


def _add_missing_columns(connection: sqlite3.Connection) -> None:
    for table, column, definition in ADDED_COLUMNS:
        present = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        if column not in present:
            _add_column(connection, table, column, definition)


def _ids_can_be_reused(connection: sqlite3.Connection) -> bool:
    row = connection.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'nodes'").fetchone()
    return row is not None and "AUTOINCREMENT" not in row[0]


def _rebuild_with_lasting_ids(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute("BEGIN IMMEDIATE")
    try:
        if _ids_can_be_reused(connection):
            for statement in REBUILD_WITH_LASTING_IDS.split(";"):
                if statement.strip():
                    connection.execute(statement)
            problems = connection.execute("PRAGMA foreign_key_check").fetchall()
            if problems:
                raise sqlite3.IntegrityError(f"the rebuilt tables have {len(problems)} broken references")
        connection.execute("COMMIT")
    except BaseException:
        connection.execute("ROLLBACK")
        raise


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=FOLDER_MODE)
        _create_private_file(self.path)
        self._migrate()
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(SCHEMA)
            _add_missing_columns(connection)

    def _migrate(self) -> None:
        connection = sqlite3.connect(self.path, timeout=BUSY_TIMEOUT_SECONDS, isolation_level=None)
        try:
            if _ids_can_be_reused(connection):
                _rebuild_with_lasting_ids(connection)
        finally:
            connection.close()

    def _open(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=BUSY_TIMEOUT_SECONDS)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @contextmanager
    def connect(self):
        connection = self._open()
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def is_reachable(self) -> bool:
        try:
            with self.connect() as connection:
                connection.execute("SELECT COUNT(*) FROM nodes").fetchone()
        except sqlite3.Error:
            return False
        return True
