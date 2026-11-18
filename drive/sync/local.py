import hashlib
import secrets
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from ..blobs import BlobStore
from ..disk import DiskGuard
from ..errors import NotFound
from ..filetypes import HEAD_BYTES, detect_type
from ..store import DriveStore
from .remote import SyncError, join, name_of, parent_of, shown

HASH_BLOCK_BYTES = 1024 * 1024
TEMP_ID_BYTES = 12


@dataclass(frozen=True)
class LocalEntry:
    kind: str
    id: int
    version: str
    size: int = 0
    modified_at: float | None = None
    blob: str | None = None


def file_md5(path: Path) -> str:
    digest = hashlib.md5(usedforsecurity=False)
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(HASH_BLOCK_BYTES), b""):
            digest.update(block)
    return digest.hexdigest()


def _entry(row: dict) -> LocalEntry:
    version = row["blob"] if row["kind"] == "file" else str(row["id"])
    return LocalEntry(kind=row["kind"], id=row["id"], version=version, size=row["size"],
                      modified_at=row["modifiedAt"], blob=row["blob"])


class LocalSide:
    def __init__(self, store: DriveStore, blobs: BlobStore, disk: DiskGuard, root_id: int, temp_dir: Path):
        self.store = store
        self.blobs = blobs
        self.disk = disk
        self.root_id = root_id
        self.temp_dir = Path(temp_dir)
        self.temp_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._folder_ids: dict[str, int] = {"": root_id}

    def tree(self) -> dict[str, LocalEntry]:
        try:
            rows = self.store.sync_tree(self.root_id)
        except NotFound as error:
            raise SyncError("the Drive folder for this connection is not there any more (deleted or in the bin)") \
                from error
        entries = {row["path"]: _entry(row) for row in rows}
        self._folder_ids = {"": self.root_id}
        self._folder_ids.update({path: entry.id for path, entry in entries.items() if entry.kind == "folder"})
        return entries

    def blob_path(self, entry: LocalEntry) -> Path:
        return self.blobs.path(entry.blob)

    def md5(self, entry: LocalEntry) -> str:
        return file_md5(self.blob_path(entry))

    def temp_file(self) -> Path:
        return self.temp_dir / f"incoming-{secrets.token_hex(TEMP_ID_BYTES)}"

    @contextmanager
    def room_for(self, size: int):
        key = f"sync-{secrets.token_hex(TEMP_ID_BYTES)}"
        self.disk.promise(key, size)
        try:
            yield
        finally:
            self.disk.release(key)

    def _parent_id(self, path: str) -> int:
        parent = parent_of(path)
        if parent not in self._folder_ids:
            raise SyncError(f"the Drive folder for {shown(path)} is not there")
        return self._folder_ids[parent]

    def name_taken(self, path: str) -> bool:
        return self.store.name_taken(self._parent_id(path), name_of(path))

    def add_file(self, path: str, incoming: Path, size: int, modified_at: float | None) -> tuple[str, LocalEntry]:
        name = name_of(path)
        with open(incoming, "rb") as handle:
            head = handle.read(HEAD_BYTES)
        parent_id = self._parent_id(path)
        blob = self.blobs.adopt(incoming)
        try:
            node = self.store.add_file(parent_id, name, size, blob, detect_type(name, head), modified_at)
        except BaseException:
            self.blobs.remove(blob)
            raise
        landed = join(parent_of(path), node["name"])
        return landed, LocalEntry(kind="file", id=node["id"], version=blob, size=size, modified_at=modified_at,
                                  blob=blob)

    def replace_file(self, path: str, old: LocalEntry, incoming: Path, size: int,
                     modified_at: float | None) -> tuple[str, LocalEntry]:
        self.store.trash([old.id])
        return self.add_file(path, incoming, size, modified_at)

    def make_folder(self, path: str) -> LocalEntry:
        node = self.store.create_folder(self._parent_id(path), name_of(path))
        self._folder_ids[path] = node["id"]
        return LocalEntry(kind="folder", id=node["id"], version=str(node["id"]))

    def inside(self, folder: LocalEntry) -> list[LocalEntry]:
        try:
            return [_entry(row) for row in self.store.sync_tree(folder.id)]
        except NotFound as error:
            raise SyncError("the Drive folder is not there any more") from error

    def trash(self, entry: LocalEntry) -> None:
        self.store.trash([entry.id])

    def move(self, entry: LocalEntry, old_path: str, new_path: str) -> LocalEntry:
        new_parent = self._parent_id(new_path)
        if parent_of(old_path) != parent_of(new_path):
            self.store.move([entry.id], new_parent)
        if name_of(old_path) != name_of(new_path):
            self.store.rename(entry.id, name_of(new_path))
        if entry.kind == "folder":
            self._folder_ids[new_path] = entry.id
        return entry
