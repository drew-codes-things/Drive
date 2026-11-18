import multiprocessing
import os
import sqlite3
import stat
from pathlib import Path

from drive.auth import load_or_create_secret
from drive.blobs import BlobStore
from drive.db import Database
from drive.store import DriveStore

RACE_ROUNDS = 40
RACE_PROCESSES = 4
OLD_SCHEMA = """
CREATE TABLE nodes (
    id INTEGER PRIMARY KEY,
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
    opened_at REAL
);
CREATE UNIQUE INDEX live_names ON nodes (ifnull(parent_id, 0), name_key) WHERE in_bin = 0;
CREATE TABLE shares (
    id INTEGER PRIMARY KEY,
    token TEXT NOT NULL UNIQUE,
    node_id INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    created_at REAL NOT NULL,
    expires_at REAL,
    password_hash TEXT,
    revoked_at REAL
);
CREATE TABLE uploads (
    id TEXT PRIMARY KEY,
    parent_id INTEGER,
    folders TEXT NOT NULL DEFAULT '[]',
    name TEXT NOT NULL,
    size INTEGER NOT NULL,
    modified_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
"""


def mode_of(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def test_the_app_makes_its_data_folder_and_database_private(tmp_path):
    data = tmp_path / "fresh" / "data"
    load_or_create_secret(data, "")
    Database(data / "drive.db")
    assert mode_of(data) == 0o700
    assert mode_of(data / "drive.db") == 0o600
    assert mode_of(data / "secret_key") == 0o600


def test_an_old_database_keeps_its_data_and_stops_reusing_ids(tmp_path):
    path = tmp_path / "drive.db"
    blobs = BlobStore(tmp_path / "blobs")
    original = blobs.new_id()
    blobs.path(original).parent.mkdir(parents=True, exist_ok=True)
    blobs.path(original).write_bytes(b"hello")
    linked = blobs.duplicate(original)
    with sqlite3.connect(path) as connection:
        connection.executescript(OLD_SCHEMA)
        connection.execute("INSERT INTO nodes (id, name, name_key, kind, created_at, modified_at)"
                           " VALUES (1, 'Folder', 'folder', 'folder', 1, 1)")
        connection.execute("INSERT INTO nodes (id, parent_id, name, name_key, kind, size, blob, mime, created_at,"
                           " modified_at) VALUES (2, 1, 'a.txt', 'a.txt', 'file', 5, ?, 'text/plain', 1, 1)",
                           (original,))
        connection.execute("INSERT INTO nodes (id, parent_id, name, name_key, kind, size, blob, mime, created_at,"
                           " modified_at) VALUES (3, 1, 'b.txt', 'b.txt', 'file', 5, ?, 'text/plain', 1, 1)",
                           (linked,))
        connection.execute("INSERT INTO nodes (id, name, name_key, kind, created_at, modified_at)"
                           " VALUES (4, 'Last', 'last', 'folder', 1, 1)")
        connection.execute("INSERT INTO shares (id, token, node_id, created_at) VALUES (7, ?, 1, 1)", ("t" * 43,))
    database = Database(path)
    store = DriveStore(database, blobs)
    assert [item["name"] for item in store.list_folder(1)["items"]] == ["a.txt", "b.txt"]
    with database.connect() as connection:
        assert connection.execute("SELECT node_id FROM shares WHERE id = 7").fetchone()[0] == 1
        assert "AUTOINCREMENT" in connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'nodes'").fetchone()[0]
    store.trash([4])
    store.delete_forever([4])
    assert store.create_folder(0, "New")["id"] == 5
    expected = 10 if blobs.copies_take_space() else 5
    assert store.usage()["usedBytes"] == expected
    Database(path)
    assert store.create_folder(0, "Newer")["id"] == 6


def test_an_older_sync_table_gains_the_held_bins_columns(tmp_path):
    path = tmp_path / "drive.db"
    Database(path)
    with sqlite3.connect(path) as connection:
        connection.execute("ALTER TABLE sync_connections DROP COLUMN last_held_files")
        connection.execute("ALTER TABLE sync_connections DROP COLUMN last_held")
    database = Database(path)
    with database.connect() as connection:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(sync_connections)")}
    assert {"last_held", "last_held_files"} <= columns


def open_database(path: Path, answers) -> None:
    try:
        Database(path)
        answers.put("ok")
    except Exception as error:
        answers.put(f"{type(error).__name__}: {error}")


def test_several_processes_opening_an_older_database_at_once_all_start(tmp_path):
    older = tmp_path / "older.db"
    Database(older)
    connection = sqlite3.connect(older)
    connection.execute("ALTER TABLE sync_connections DROP COLUMN last_held")
    connection.commit()
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    connection.close()
    context = multiprocessing.get_context("forkserver")
    context.set_forkserver_preload(["drive.db", "test_db"])
    failures = []
    for attempt in range(RACE_ROUNDS):
        path = tmp_path / f"round-{attempt}.db"
        path.write_bytes(older.read_bytes())
        answers = context.Queue()
        workers = [context.Process(target=open_database, args=(path, answers)) for _ in range(RACE_PROCESSES)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        failures += [answer for answer in (answers.get() for _ in workers) if answer != "ok"]
    assert failures == []
