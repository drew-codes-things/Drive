import logging
import os
import threading
import time

from .blobs import BlobStore
from .db import Database
from .errors import Conflict, DriveError, NotFound
from .filetypes import HEAD_BYTES, detect_type, preview_kind
from .names import clean_name, name_key, numbered_name

ROOT_ID = 0
BIN_DAYS = 30
DAY_SECONDS = 86400
LARGEST_DATABASE_INTEGER = 2 ** 63 - 1
MAX_BATCH = 1000
SQL_BATCH = 500
MAX_FOLDER_DEPTH = 64
MAX_NAME_NUMBER = 10000
RECENT_LIMIT = 50
SEARCH_LIMIT = 200
MAX_QUERY_LENGTH = 200
SORT_COLUMNS = {
    "name": "name_key",
    "modified": "modified_at",
    "size": "size",
}
SORT_ORDERS = ("asc", "desc")
LIKE_ESCAPE = "\\"

log = logging.getLogger("drive.store")

LIVE_SUBTREE = """
WITH RECURSIVE subtree(id) AS (
    SELECT id FROM nodes WHERE id = ?
    UNION ALL
    SELECT nodes.id FROM nodes JOIN subtree ON nodes.parent_id = subtree.id WHERE nodes.in_bin = 0
)
"""
FOLDER_HEIGHT = """
WITH RECURSIVE below(id, level) AS (
    SELECT id, 1 FROM nodes WHERE id = ? AND kind = 'folder'
    UNION ALL
    SELECT nodes.id, below.level + 1 FROM nodes JOIN below ON nodes.parent_id = below.id
    WHERE nodes.kind = 'folder' AND nodes.in_bin = 0 AND below.level <= ?
)
SELECT ifnull(max(level), 0) FROM below
"""
BINNED_SUBTREE = """
WITH RECURSIVE subtree(id) AS (
    SELECT id FROM nodes WHERE id = ?
    UNION ALL
    SELECT nodes.id FROM nodes JOIN subtree ON nodes.parent_id = subtree.id WHERE nodes.trashed_at IS NULL
)
"""


def _parent_value(folder_id: int):
    return None if folder_id == ROOT_ID else folder_id


def _escape_like(text: str) -> str:
    return text.replace(LIKE_ESCAPE, LIKE_ESCAPE * 2).replace("%", LIKE_ESCAPE + "%").replace("_", LIKE_ESCAPE + "_")


def _as_ids(raw) -> list[int]:
    if not isinstance(raw, list) or not raw:
        raise DriveError("send a list of ids")
    if len(raw) > MAX_BATCH:
        raise DriveError(f"at most {MAX_BATCH} items at a time")
    ids = []
    for value in raw:
        if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= LARGEST_DATABASE_INTEGER:
            raise DriveError("ids must be positive whole numbers")
        if value not in ids:
            ids.append(value)
    return ids


def as_folder_id(raw) -> int:
    if raw is None:
        return ROOT_ID
    if isinstance(raw, bool) or not isinstance(raw, int) or not 0 <= raw <= LARGEST_DATABASE_INTEGER:
        raise DriveError("a folder id must be a whole number, 0 for My Drive")
    return raw


def node_view(row) -> dict:
    return {
        "id": row["id"],
        "parentId": row["parent_id"] or ROOT_ID,
        "name": row["name"],
        "kind": row["kind"],
        "size": row["size"],
        "preview": preview_kind(row["mime"]) if row["kind"] == "file" else "none",
        "starred": bool(row["starred"]),
        "createdAt": row["created_at"],
        "modifiedAt": row["modified_at"],
        "openedAt": row["opened_at"],
        "trashedAt": row["trashed_at"],
    }


class DriveStore:
    def __init__(self, database: Database, blobs: BlobStore, clock=time.time):
        self.database = database
        self.blobs = blobs
        self.clock = clock
        self.lock = threading.RLock()
        self._fill_missing_contents()

    def _content_key(self, blob: str) -> str:
        try:
            details = os.stat(self.blobs.path(blob))
        except (OSError, ValueError):
            return blob
        return f"{details.st_dev}:{details.st_ino}"

    def _fill_missing_contents(self) -> None:
        with self.lock, self.database.connect() as connection:
            rows = connection.execute("SELECT id, blob FROM nodes WHERE kind = 'file' AND content IS NULL").fetchall()
            first_blob_for_key: dict[str, str] = {}
            for row in rows:
                key = self._content_key(row["blob"])
                content = first_blob_for_key.setdefault(key, row["blob"])
                connection.execute("UPDATE nodes SET content = ? WHERE id = ?", (content, row["id"]))
        if rows:
            log.info("worked out shared contents for %d files", len(rows))

    def _row(self, connection, node_id: int, allow_bin: bool = False):
        row = connection.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
        if row is None or (row["in_bin"] and not allow_bin):
            raise NotFound()
        return row

    def _folder_row(self, connection, folder_id: int):
        if folder_id == ROOT_ID:
            return None
        row = self._row(connection, folder_id)
        if row["kind"] != "folder":
            raise DriveError("that is a file, not a folder")
        return row

    def _name_taken(self, connection, folder_id: int, name: str, except_id: int = 0) -> bool:
        row = connection.execute(
            "SELECT id FROM nodes WHERE ifnull(parent_id, 0) = ? AND name_key = ? AND in_bin = 0 AND id != ?",
            (folder_id, name_key(name), except_id)).fetchone()
        return row is not None

    def name_taken(self, folder_id: int, name: str) -> bool:
        with self.database.connect() as connection:
            return self._name_taken(connection, folder_id, name)

    def _free_name(self, connection, folder_id: int, name: str, is_folder: bool) -> str:
        if not self._name_taken(connection, folder_id, name):
            return name
        for number in range(2, MAX_NAME_NUMBER):
            candidate = numbered_name(name, number, is_folder)
            if not self._name_taken(connection, folder_id, candidate):
                return candidate
        raise Conflict(f"too many items called {name!r} in this folder")

    def _insert(self, connection, folder_id: int, name: str, kind: str, size: int = 0, blob=None,
                mime: str = "", modified_at=None, content=None) -> int:
        now = self.clock()
        cursor = connection.execute(
            "INSERT INTO nodes (parent_id, name, name_key, kind, size, blob, mime, created_at, modified_at, content)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (_parent_value(folder_id), name, name_key(name), kind, size, blob, mime, now,
             now if modified_at is None else modified_at, content or blob))
        return cursor.lastrowid

    def get(self, node_id: int, allow_bin: bool = False) -> dict:
        with self.database.connect() as connection:
            return node_view(self._row(connection, node_id, allow_bin))

    def file_record(self, node_id: int) -> dict:
        with self.database.connect() as connection:
            row = self._row(connection, node_id)
        if row["kind"] != "file":
            raise DriveError("that is a folder, not a file")
        return {"name": row["name"], "size": row["size"], "mime": row["mime"], "blob": row["blob"],
                "modifiedAt": row["modified_at"], "id": row["id"]}

    def path(self, folder_id: int, stop_at: int = ROOT_ID) -> list[dict]:
        trail = []
        with self.database.connect() as connection:
            current = folder_id
            while current != ROOT_ID:
                row = self._row(connection, current)
                trail.append({"id": row["id"], "name": row["name"]})
                if current == stop_at:
                    break
                current = row["parent_id"] or ROOT_ID
                if len(trail) > MAX_FOLDER_DEPTH * 4:
                    raise DriveError("this folder is nested too deeply", 500)
        return list(reversed(trail))

    def is_within(self, node_id: int, ancestor_id: int) -> bool:
        with self.database.connect() as connection:
            current = node_id
            for _ in range(MAX_FOLDER_DEPTH * 4):
                row = connection.execute("SELECT parent_id, in_bin FROM nodes WHERE id = ?", (current,)).fetchone()
                if row is None or row["in_bin"]:
                    return False
                if current == ancestor_id:
                    return True
                if row["parent_id"] is None:
                    return False
                current = row["parent_id"]
        return False

    def require_folder(self, folder_id: int) -> None:
        with self.database.connect() as connection:
            self._folder_row(connection, folder_id)

    def folder_exists(self, folder_id: int) -> bool:
        if folder_id == ROOT_ID:
            return True
        with self.database.connect() as connection:
            row = connection.execute("SELECT kind FROM nodes WHERE id = ?", (folder_id,)).fetchone()
        return row is not None and row["kind"] == "folder"

    def list_folder(self, folder_id: int, sort: str = "name", order: str = "asc") -> dict:
        column = SORT_COLUMNS.get(sort)
        if column is None or order not in SORT_ORDERS:
            raise DriveError(f"sort must be one of {', '.join(SORT_COLUMNS)} and order asc or desc")
        with self.database.connect() as connection:
            folder = self._folder_row(connection, folder_id)
            rows = connection.execute(
                f"SELECT * FROM nodes WHERE ifnull(parent_id, 0) = ? AND in_bin = 0"
                f" ORDER BY kind = 'file', {column} {order}, name_key ASC, id ASC", (folder_id,)).fetchall()
        return {
            "folder": {"id": folder_id, "name": folder["name"] if folder else "My Drive"},
            "path": self.path(folder_id),
            "items": [node_view(row) for row in rows],
        }

    def create_folder(self, parent_id: int, raw_name) -> dict:
        name = clean_name(raw_name)
        with self.lock, self.database.connect() as connection:
            self._folder_row(connection, parent_id)
            if self._name_taken(connection, parent_id, name):
                raise Conflict(f"something called {name!r} is already in this folder")
            if self._depth(connection, parent_id) >= MAX_FOLDER_DEPTH:
                raise DriveError(f"folders can be at most {MAX_FOLDER_DEPTH} deep")
            node_id = self._insert(connection, parent_id, name, "folder")
            return node_view(self._row(connection, node_id))

    def _depth(self, connection, folder_id: int) -> int:
        depth = 0
        current = folder_id
        while current != ROOT_ID and depth <= MAX_FOLDER_DEPTH:
            row = connection.execute("SELECT parent_id FROM nodes WHERE id = ?", (current,)).fetchone()
            if row is None:
                break
            depth += 1
            current = row["parent_id"] or ROOT_ID
        return depth

    def ensure_folders(self, parent_id: int, names: list[str]) -> int:
        with self.lock, self.database.connect() as connection:
            self._folder_row(connection, parent_id)
            current = parent_id
            for raw_name in names:
                name = clean_name(raw_name)
                row = connection.execute(
                    "SELECT id, kind FROM nodes WHERE ifnull(parent_id, 0) = ? AND name_key = ? AND in_bin = 0",
                    (current, name_key(name))).fetchone()
                if row is not None and row["kind"] == "folder":
                    current = row["id"]
                    continue
                if self._depth(connection, current) >= MAX_FOLDER_DEPTH:
                    raise DriveError(f"folders can be at most {MAX_FOLDER_DEPTH} deep")
                free = name if row is None else self._free_name(connection, current, name, True)
                current = self._insert(connection, current, free, "folder")
            return current

    def add_file(self, folder_id: int, raw_name, size: int, blob: str, mime: str, modified_at=None) -> dict:
        name = clean_name(raw_name)
        with self.lock, self.database.connect() as connection:
            self._folder_row(connection, folder_id)
            free = self._free_name(connection, folder_id, name, False)
            node_id = self._insert(connection, folder_id, free, "file", size, blob, mime, modified_at)
            return node_view(self._row(connection, node_id))

    def _detect_stored_type(self, name: str, blob: str) -> str:
        with open(self.blobs.path(blob), "rb") as handle:
            return detect_type(name, handle.read(HEAD_BYTES))

    def rename(self, node_id: int, raw_name) -> dict:
        name = clean_name(raw_name)
        with self.lock, self.database.connect() as connection:
            row = self._row(connection, node_id)
            folder_id = row["parent_id"] or ROOT_ID
            if self._name_taken(connection, folder_id, name, except_id=node_id):
                raise Conflict(f"something called {name!r} is already in this folder")
            mime = self._detect_stored_type(name, row["blob"]) if row["kind"] == "file" else row["mime"]
            connection.execute("UPDATE nodes SET name = ?, name_key = ?, mime = ?, modified_at = ? WHERE id = ?",
                               (name, name_key(name), mime, self.clock(), node_id))
            return node_view(self._row(connection, node_id))

    def set_starred(self, node_id: int, starred) -> dict:
        if not isinstance(starred, bool):
            raise DriveError("starred must be true or false")
        with self.database.connect() as connection:
            self._row(connection, node_id)
            connection.execute("UPDATE nodes SET starred = ? WHERE id = ?", (int(starred), node_id))
            return node_view(self._row(connection, node_id))

    def mark_opened(self, node_id: int) -> None:
        with self.database.connect() as connection:
            connection.execute("UPDATE nodes SET opened_at = ? WHERE id = ?", (self.clock(), node_id))

    def _subtree_ids(self, connection, node_id: int) -> list[int]:
        return [row["id"] for row in connection.execute(LIVE_SUBTREE + "SELECT id FROM subtree", (node_id,))]

    def _folder_height(self, connection, node_id: int) -> int:
        return connection.execute(FOLDER_HEIGHT, (node_id, MAX_FOLDER_DEPTH)).fetchone()[0]

    def _check_target(self, connection, ids: list[int], target_id: int) -> None:
        self._folder_row(connection, target_id)
        target_depth = self._depth(connection, target_id)
        for node_id in ids:
            if target_id != ROOT_ID and target_id in self._subtree_ids(connection, node_id):
                raise DriveError("a folder cannot go inside itself")
            if target_depth + self._folder_height(connection, node_id) > MAX_FOLDER_DEPTH:
                raise DriveError(f"that would put folders more than {MAX_FOLDER_DEPTH} deep")

    def move(self, raw_ids, raw_target) -> list[dict]:
        ids, target_id = _as_ids(raw_ids), as_folder_id(raw_target)
        with self.lock, self.database.connect() as connection:
            rows = [self._row(connection, node_id) for node_id in ids]
            self._check_target(connection, ids, target_id)
            moved = []
            for row in rows:
                if (row["parent_id"] or ROOT_ID) == target_id:
                    moved.append(row["id"])
                    continue
                name = self._free_name(connection, target_id, row["name"], row["kind"] == "folder")
                connection.execute("UPDATE nodes SET parent_id = ?, name = ?, name_key = ? WHERE id = ?",
                                   (_parent_value(target_id), name, name_key(name), row["id"]))
                moved.append(row["id"])
            return [node_view(self._row(connection, node_id)) for node_id in moved]

    def copy_size(self, raw_ids) -> int:
        ids = _as_ids(raw_ids)
        with self.database.connect() as connection:
            total = 0
            for node_id in ids:
                self._row(connection, node_id)
                total += connection.execute(
                    LIVE_SUBTREE + "SELECT ifnull(sum(size), 0) FROM nodes WHERE id IN (SELECT id FROM subtree)",
                    (node_id,)).fetchone()[0]
            return total

    def _copy_node(self, connection, row, target_id: int, made_blobs: list[str]) -> int:
        name = self._free_name(connection, target_id, row["name"], row["kind"] == "folder")
        if row["kind"] == "file":
            blob = self.blobs.duplicate(row["blob"])
            made_blobs.append(blob)
            is_hard_link = self._content_key(blob) == self._content_key(row["blob"])
            content = (row["content"] or row["blob"]) if is_hard_link else blob
            return self._insert(connection, target_id, name, "file", row["size"], blob, row["mime"],
                                row["modified_at"], content)
        copy_id = self._insert(connection, target_id, name, "folder")
        children = connection.execute("SELECT * FROM nodes WHERE parent_id = ? AND in_bin = 0 ORDER BY id",
                                      (row["id"],)).fetchall()
        for child in children:
            self._copy_node(connection, child, copy_id, made_blobs)
        return copy_id

    def copy(self, raw_ids, raw_target) -> list[dict]:
        ids, target_id = _as_ids(raw_ids), as_folder_id(raw_target)
        made_blobs: list[str] = []
        try:
            with self.lock, self.database.connect() as connection:
                rows = [self._row(connection, node_id) for node_id in ids]
                self._check_target(connection, ids, target_id)
                copies = [self._copy_node(connection, row, target_id, made_blobs) for row in rows]
                return [node_view(self._row(connection, node_id)) for node_id in copies]
        except BaseException:
            for blob in made_blobs:
                self.blobs.remove(blob)
            raise

    def trash(self, raw_ids) -> int:
        ids = _as_ids(raw_ids)
        now = self.clock()
        with self.lock, self.database.connect() as connection:
            for node_id in ids:
                self._row(connection, node_id)
            for node_id in ids:
                if connection.execute("SELECT in_bin FROM nodes WHERE id = ?", (node_id,)).fetchone()[0]:
                    continue
                connection.execute(LIVE_SUBTREE + "UPDATE nodes SET in_bin = 1 WHERE id IN (SELECT id FROM subtree)",
                                   (node_id,))
                connection.execute("UPDATE nodes SET trashed_at = ? WHERE id = ?", (now, node_id))
        return len(ids)

    def _bin_root(self, connection, node_id: int):
        row = connection.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
        if row is None or row["trashed_at"] is None:
            raise NotFound("that item is not in the bin")
        return row

    def restore(self, raw_ids) -> list[dict]:
        ids = _as_ids(raw_ids)
        with self.lock, self.database.connect() as connection:
            rows = [self._bin_root(connection, node_id) for node_id in ids]
            restored = []
            for row in rows:
                target_id = row["parent_id"] or ROOT_ID
                if target_id != ROOT_ID:
                    parent = connection.execute("SELECT in_bin FROM nodes WHERE id = ?", (target_id,)).fetchone()
                    if parent is None or parent["in_bin"]:
                        target_id = ROOT_ID
                name = self._free_name(connection, target_id, row["name"], row["kind"] == "folder")
                connection.execute("UPDATE nodes SET parent_id = ?, name = ?, name_key = ? WHERE id = ?",
                                   (_parent_value(target_id), name, name_key(name), row["id"]))
                connection.execute(BINNED_SUBTREE + "UPDATE nodes SET in_bin = 0 WHERE id IN (SELECT id FROM subtree)",
                                   (row["id"],))
                connection.execute("UPDATE nodes SET trashed_at = NULL WHERE id = ?", (row["id"],))
                restored.append(row["id"])
            return [node_view(self._row(connection, node_id)) for node_id in restored]

    def _delete_subtree(self, connection, node_id: int) -> list[str]:
        rows = connection.execute(
            BINNED_SUBTREE + "SELECT id, blob FROM nodes WHERE id IN (SELECT id FROM subtree)", (node_id,)).fetchall()
        ids = [row["id"] for row in rows]
        for start in range(0, len(ids), SQL_BATCH):
            self._delete_rows(connection, ids[start:start + SQL_BATCH])
        return [row["blob"] for row in rows if row["blob"]]

    @staticmethod
    def _delete_rows(connection, ids: list[int]) -> None:
        marks = ",".join("?" * len(ids))
        connection.execute(f"DELETE FROM shares WHERE node_id IN ({marks})", ids)
        connection.execute(f"UPDATE nodes SET parent_id = NULL WHERE id IN ({marks})", ids)
        connection.execute(f"UPDATE nodes SET parent_id = NULL WHERE parent_id IN ({marks})", ids)
        connection.execute(f"DELETE FROM nodes WHERE id IN ({marks})", ids)

    def _remove_blobs(self, blobs: list[str]) -> None:
        for blob in blobs:
            try:
                self.blobs.remove(blob)
            except OSError as error:
                log.error("could not remove blob %s: %s", blob, error)

    def delete_forever(self, raw_ids) -> int:
        ids = _as_ids(raw_ids)
        with self.lock:
            with self.database.connect() as connection:
                for node_id in ids:
                    self._bin_root(connection, node_id)
                blobs = []
                for node_id in ids:
                    blobs.extend(self._delete_subtree(connection, node_id))
            self._remove_blobs(blobs)
        return len(ids)

    def _delete_bin_roots(self, where: str, parameters: tuple) -> int:
        with self.lock:
            with self.database.connect() as connection:
                roots = [row["id"] for row in connection.execute(
                    f"SELECT id FROM nodes WHERE trashed_at IS NOT NULL AND {where}", parameters)]
                blobs = []
                for node_id in roots:
                    blobs.extend(self._delete_subtree(connection, node_id))
            self._remove_blobs(blobs)
        return len(roots)

    def empty_bin(self) -> int:
        return self._delete_bin_roots("1 = 1", ())

    def purge_expired(self) -> int:
        return self._delete_bin_roots("trashed_at < ?", (self.clock() - BIN_DAYS * DAY_SECONDS,))

    def bin_items(self) -> list[dict]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM nodes WHERE trashed_at IS NOT NULL ORDER BY trashed_at DESC, id DESC").fetchall()
        return [node_view(row) for row in rows]

    def starred(self) -> list[dict]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM nodes WHERE starred = 1 AND in_bin = 0 ORDER BY kind = 'file', name_key").fetchall()
        return [node_view(row) for row in rows]

    def recent(self) -> list[dict]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM nodes WHERE kind = 'file' AND in_bin = 0"
                " ORDER BY max(modified_at, created_at, ifnull(opened_at, 0)) DESC, id DESC LIMIT ?",
                (RECENT_LIMIT,)).fetchall()
        return [node_view(row) for row in rows]

    def search(self, raw_query) -> list[dict]:
        query = (raw_query or "").strip() if isinstance(raw_query, str) else ""
        if not query:
            return []
        if len(query) > MAX_QUERY_LENGTH:
            raise DriveError(f"a search can be at most {MAX_QUERY_LENGTH} characters")
        pattern = f"%{_escape_like(name_key(query))}%"
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM nodes WHERE in_bin = 0 AND name_key LIKE ? ESCAPE ?"
                " ORDER BY kind = 'file', name_key LIMIT ?", (pattern, LIKE_ESCAPE, SEARCH_LIMIT)).fetchall()
        return [node_view(row) for row in rows]

    def usage(self) -> dict:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT ifnull(sum(CASE WHEN live THEN size END), 0) AS live,"
                " ifnull(sum(CASE WHEN NOT live THEN size END), 0) AS binned FROM"
                " (SELECT max(size) AS size, min(in_bin) = 0 AS live FROM nodes WHERE kind = 'file'"
                "  GROUP BY ifnull(content, blob))").fetchone()
            files = connection.execute("SELECT count(*) FROM nodes WHERE kind = 'file' AND in_bin = 0").fetchone()[0]
        return {"usedBytes": row["live"], "binBytes": row["binned"], "fileCount": files}

    def sync_tree(self, folder_id: int) -> list[dict]:
        found = []
        with self.database.connect() as connection:
            self._folder_row(connection, folder_id)
            pending = [(folder_id, "")]
            while pending:
                parent_id, prefix = pending.pop()
                rows = connection.execute(
                    "SELECT id, name, kind, size, blob, modified_at FROM nodes"
                    " WHERE ifnull(parent_id, 0) = ? AND in_bin = 0", (parent_id,)).fetchall()
                for row in rows:
                    path = f"{prefix}{row['name']}"
                    found.append({"path": path, "id": row["id"], "kind": row["kind"], "size": row["size"],
                                  "blob": row["blob"], "modifiedAt": row["modified_at"]})
                    if row["kind"] == "folder":
                        pending.append((row["id"], path + "/"))
        return found

    def zip_entries(self, folder_id: int) -> tuple[str, list[tuple[str, dict]]]:
        with self.database.connect() as connection:
            folder = self._folder_row(connection, folder_id)
            entries: list[tuple[str, dict]] = []
            self._collect(connection, folder_id, "", entries)
        return (folder["name"] if folder else "My Drive"), entries

    def _collect(self, connection, folder_id: int, prefix: str, entries: list) -> None:
        rows = connection.execute("SELECT * FROM nodes WHERE ifnull(parent_id, 0) = ? AND in_bin = 0 ORDER BY id",
                                  (folder_id,)).fetchall()
        for row in rows:
            relative = f"{prefix}{row['name']}"
            if row["kind"] == "folder":
                entries.append((relative + "/", {"kind": "folder", "modifiedAt": row["modified_at"]}))
                self._collect(connection, row["id"], relative + "/", entries)
                continue
            entries.append((relative, {"kind": "file", "size": row["size"], "blob": row["blob"],
                                       "modifiedAt": row["modified_at"]}))
