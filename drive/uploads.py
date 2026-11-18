import json
import logging
import os
import re
import secrets
import threading
import time
from pathlib import Path

from .blobs import BlobStore
from .db import Database
from .disk import DiskGuard
from .errors import Conflict, DriveError, NotFound
from .filetypes import HEAD_BYTES, detect_type
from .names import clean_name
from .store import MAX_FOLDER_DEPTH, DriveStore, as_folder_id

UPLOAD_ID = re.compile(r"[0-9a-f]{32}")
BATCH_ID = re.compile(r"[0-9a-f]{32}")
PARENT_GONE_TEXT = "the folder this upload was going into has been deleted, so the upload was dropped; upload it again"
UPLOAD_ID_BYTES = 16
CHUNK_BYTES = 8 * 1024 * 1024
COPY_BYTES = 1024 * 1024
STALE_SECONDS = 2 * 86400
FILE_MODE = 0o600

log = logging.getLogger("drive.uploads")


def _upload_view(row, received: int) -> dict:
    return {"id": row["id"], "name": row["name"], "size": row["size"], "received": received,
            "chunkBytes": CHUNK_BYTES, "parentId": row["parent_id"] or 0, "folders": json.loads(row["folders"]),
            "updatedAt": row["updated_at"]}


def _folder_names(raw) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > MAX_FOLDER_DEPTH:
        raise DriveError(f"folders must be a list of at most {MAX_FOLDER_DEPTH} names")
    return [clean_name(name) for name in raw]


def _size(raw, largest: int) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        raise DriveError("size must be a whole number of bytes")
    if raw > largest:
        raise DriveError(f"files can be at most {largest // 1024 ** 3} GB here", 413)
    return raw


def _batch(raw) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str) or not BATCH_ID.fullmatch(raw):
        raise DriveError("batch must be 32 lowercase hex characters")
    return raw


def _modified_at(raw):
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not 0 < raw < 2 ** 41:
        raise DriveError("lastModified must be milliseconds since 1970")
    return raw / 1000


class UploadManager:
    def __init__(self, database: Database, store: DriveStore, blobs: BlobStore, disk: DiskGuard, folder: Path,
                 max_file_bytes: int, clock=time.time):
        self.database = database
        self.store = store
        self.blobs = blobs
        self.disk = disk
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.max_file_bytes = max_file_bytes
        self.clock = clock
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _part(self, upload_id: str) -> Path:
        return self.folder / f"{upload_id}.part"

    def _lock_for(self, upload_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(upload_id, threading.Lock())

    def _forget_lock(self, upload_id: str) -> None:
        with self._locks_guard:
            self._locks.pop(upload_id, None)

    def _row(self, upload_id: str):
        if not isinstance(upload_id, str) or not UPLOAD_ID.fullmatch(upload_id):
            raise NotFound("that upload is not here, start it again")
        with self.database.connect() as connection:
            row = connection.execute("SELECT * FROM uploads WHERE id = ?", (upload_id,)).fetchone()
        if row is None:
            raise NotFound("that upload is not here, start it again")
        return row

    def _received(self, upload_id: str) -> int:
        try:
            return self._part(upload_id).stat().st_size
        except FileNotFoundError:
            return 0

    def start(self, body: dict) -> dict:
        name = clean_name(body.get("name"))
        size = _size(body.get("size"), self.max_file_bytes)
        folders = _folder_names(body.get("folders"))
        parent_id = as_folder_id(body.get("parentId"))
        modified_at = _modified_at(body.get("lastModified"))
        batch = _batch(body.get("batch"))
        self.store.require_folder(parent_id)
        if batch is not None and folders:
            parent_id, folders = self._batch_folder(batch, parent_id, folders), []
        upload_id = secrets.token_hex(UPLOAD_ID_BYTES)
        self.disk.promise(upload_id, size)
        try:
            descriptor = os.open(self._part(upload_id), os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_MODE)
            os.close(descriptor)
            now = self.clock()
            with self.database.connect() as connection:
                connection.execute(
                    "INSERT INTO uploads (id, parent_id, folders, name, size, modified_at, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (upload_id, parent_id, json.dumps(folders), name, size, modified_at, now, now))
                row = connection.execute("SELECT * FROM uploads WHERE id = ?", (upload_id,)).fetchone()
        except BaseException:
            self.disk.release(upload_id)
            self._part(upload_id).unlink(missing_ok=True)
            raise
        return _upload_view(row, 0)

    def _batch_folder(self, batch: str, parent_id: int, folders: list[str]) -> int:
        current = parent_id
        with self.store.lock:
            for depth in range(1, len(folders) + 1):
                path = json.dumps(folders[:depth])
                with self.database.connect() as connection:
                    row = connection.execute(
                        "SELECT folder_id FROM upload_batches WHERE batch = ? AND parent_id = ? AND path = ?",
                        (batch, parent_id, path)).fetchone()
                if row is not None and self._is_live_folder(row["folder_id"]):
                    current = row["folder_id"]
                    continue
                current = self.store.ensure_folders(current, [folders[depth - 1]])
                with self.database.connect() as connection:
                    connection.execute(
                        "INSERT OR REPLACE INTO upload_batches (batch, parent_id, path, folder_id, created_at)"
                        " VALUES (?, ?, ?, ?, ?)", (batch, parent_id, path, current, self.clock()))
        return current

    def _is_live_folder(self, folder_id: int) -> bool:
        try:
            self.store.require_folder(folder_id)
        except NotFound:
            return False
        return True

    def status(self, upload_id: str) -> dict:
        row = self._row(upload_id)
        return _upload_view(row, self._received(upload_id))

    def unfinished(self) -> list[dict]:
        with self.database.connect() as connection:
            rows = connection.execute("SELECT * FROM uploads ORDER BY updated_at DESC, id").fetchall()
        return [_upload_view(row, self._received(row["id"])) for row in rows]

    def append(self, upload_id: str, offset: int, stream, length: int) -> dict:
        self._row(upload_id)
        if length <= 0 or length > CHUNK_BYTES:
            raise DriveError(f"send chunks of 1 to {CHUNK_BYTES} bytes")
        with self._lock_for(upload_id):
            row = self._row(upload_id)
            received = self._received(upload_id)
            if offset != received:
                raise Conflict(f"the server has {received} bytes of this file; send from there")
            if received + length > row["size"]:
                raise DriveError("that chunk goes past the end of the file")
            self.disk.check_write(length)
            self.disk.update_promise(upload_id, row["size"] - received)
            written = self._write(upload_id, stream, length)
            if written != length:
                raise DriveError(f"the chunk stopped after {written} of {length} bytes; send the rest again")
            with self.database.connect() as connection:
                connection.execute("UPDATE uploads SET updated_at = ? WHERE id = ?", (self.clock(), upload_id))
            self.disk.update_promise(upload_id, row["size"] - received - written)
            return _upload_view(row, received + written)

    def _write(self, upload_id: str, stream, length: int) -> int:
        written = 0
        with open(self._part(upload_id), "ab") as handle:
            while written < length:
                piece = stream.read(min(COPY_BYTES, length - written))
                if not piece:
                    break
                handle.write(piece)
                written += len(piece)
            handle.flush()
            os.fsync(handle.fileno())
        return written

    def finish(self, upload_id: str) -> dict:
        self._row(upload_id)
        with self._lock_for(upload_id):
            row = self._row(upload_id)
            received = self._received(upload_id)
            if received != row["size"]:
                raise Conflict(f"the server has {received} of {row['size']} bytes; send the rest first")
            parent_id = row["parent_id"] or 0
            if not self.store.folder_exists(parent_id):
                self._drop(upload_id)
                raise NotFound(PARENT_GONE_TEXT)
            part = self._part(upload_id)
            with open(part, "rb") as handle:
                head = handle.read(HEAD_BYTES)
            folder_id = self.store.ensure_folders(parent_id, json.loads(row["folders"]))
            blob = self.blobs.adopt(part)
            try:
                node = self.store.add_file(folder_id, row["name"], row["size"], blob, detect_type(row["name"], head),
                                           row["modified_at"])
            except BaseException:
                self.blobs.remove(blob)
                raise
            self._drop(upload_id)
        self._forget_lock(upload_id)
        return node

    def _drop(self, upload_id: str) -> None:
        with self.database.connect() as connection:
            connection.execute("DELETE FROM uploads WHERE id = ?", (upload_id,))
        self._part(upload_id).unlink(missing_ok=True)
        self.disk.release(upload_id)

    def cancel(self, upload_id: str) -> None:
        self._row(upload_id)
        with self._lock_for(upload_id):
            self._row(upload_id)
            self._drop(upload_id)
        self._forget_lock(upload_id)

    def purge_stale(self) -> int:
        cutoff = self.clock() - STALE_SECONDS
        with self.database.connect() as connection:
            stale = [row["id"] for row in connection.execute(
                "SELECT id FROM uploads WHERE updated_at < ? OR (parent_id > 0"
                " AND parent_id NOT IN (SELECT id FROM nodes WHERE kind = 'folder'))", (cutoff,))]
            known = {row["id"] for row in connection.execute("SELECT id FROM uploads")}
            connection.execute("DELETE FROM upload_batches WHERE created_at < ?", (cutoff,))
        for upload_id in stale:
            with self._lock_for(upload_id):
                self._drop(upload_id)
            self._forget_lock(upload_id)
        for leftover in self.folder.glob("*.part"):
            if leftover.stem not in known and leftover.stat().st_mtime < cutoff:
                leftover.unlink(missing_ok=True)
        return len(stale)

    def restore_promises(self) -> None:
        with self.database.connect() as connection:
            rows = connection.execute("SELECT id, size FROM uploads").fetchall()
        for row in rows:
            remaining = row["size"] - self._received(row["id"])
            self.disk.hold(row["id"], remaining)
