from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from ..errors import DriveError
from ..names import clean_name, name_key

MAX_ENTRIES = 200_000
MAX_DEPTH = 64
SHOWN_NAME_LENGTH = 80
SYNC_BIN_NAME = ".drive-sync-bin"


class SyncError(Exception):
    pass


class StopRun(SyncError):
    pass


class RemoteUnreachable(StopRun):
    pass


class SignInNeeded(StopRun):
    pass


class FolderMissing(SyncError):
    pass


class RemoteRefused(SyncError):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class RemoteEntry:
    kind: str
    ref: str
    version: str
    size: int = 0
    modified_at: float | None = None
    id: str | None = None
    md5: str | None = None


def shown(raw: str) -> str:
    text = ascii(raw)
    return text if len(text) <= SHOWN_NAME_LENGTH else text[:SHOWN_NAME_LENGTH] + "...'"


def join(parent: str, name: str) -> str:
    return f"{parent}/{name}" if parent else name


def parent_of(path: str) -> str:
    return path.rpartition("/")[0]


def name_of(path: str) -> str:
    return path.rpartition("/")[2]


def depth(path: str) -> int:
    return path.count("/") + 1 if path else 0


def is_under(path: str, folder: str) -> bool:
    return path == folder or path.startswith(folder + "/")


def clean_remote_path(raw) -> str:
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise DriveError("the cloud folder must be text like Photos/Phone")
    parts = [part for part in raw.strip().strip("/").split("/") if part.strip()]
    if len(parts) > MAX_DEPTH // 4:
        raise DriveError(f"the cloud folder can be at most {MAX_DEPTH // 4} folders deep")
    return "/".join(clean_name(part) for part in parts)


def path_key(path: str) -> str:
    return "/".join(name_key(part) for part in path.split("/")) if path else ""


def keys_from_top(path: str) -> list[str]:
    parts = path_key(path).split("/") if path else []
    return [""] + ["/".join(parts[:count]) for count in range(1, len(parts) + 1)]


def paths_from_top(path: str) -> list[str]:
    parts = path.split("/") if path else []
    return ["/".join(parts[:count]) for count in range(1, len(parts) + 1)]


@dataclass
class Doubts:
    unsure: set[str] = field(default_factory=set)
    skipped_ids: set[str] = field(default_factory=set)
    folders_with_skips: set[str] = field(default_factory=set)
    skipped_folder_without_id: bool = False
    skipped_file_sizes_without_ids: set[int | None] = field(default_factory=set)
    folders_with_skipped_files_without_ids: set[str] = field(default_factory=set)

    def is_unsure(self, path: str) -> bool:
        return any(key in self.unsure for key in keys_from_top(path))

    def has_skips_under(self, folder: str) -> bool:
        folder_key = path_key(folder)
        return any(is_under(key, folder_key) for key in self.folders_with_skips | self.unsure)

    def may_have_become_a_skipped(self, path: str, kind: str, size: int) -> bool:
        if self.skipped_folder_without_id:
            return True
        if kind != "file":
            return False
        sizes = self.skipped_file_sizes_without_ids
        return (None in sizes or size in sizes
                or path_key(parent_of(path)) in self.folders_with_skipped_files_without_ids)


@dataclass
class Listing:
    known: dict[str, str | None] = field(default_factory=dict)
    entries: dict[str, RemoteEntry] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)
    doubts: Doubts = field(default_factory=Doubts)
    _names: dict[str, dict[str, tuple[str, str | None]]] = field(default_factory=dict)

    def accept(self, parent: str, raw_name: str, kind: str, entry_id: str | None = None,
               size: int | None = None) -> str | None:
        try:
            name = clean_name(raw_name)
        except DriveError as error:
            self.skip(parent, raw_name, str(error), entry_id, kind, size)
            return None
        if not parent and name == SYNC_BIN_NAME:
            return None
        path = join(parent, name)
        if depth(path) > MAX_DEPTH:
            self.doubt(parent, name, f"more than {MAX_DEPTH} folders deep", entry_id, kind, size)
            return None
        names_here = self._names.setdefault(parent, {})
        held = names_here.get(name_key(name))
        if held is not None:
            return self._settle_clash(names_here, held, path, kind, entry_id, size)
        if len(self.entries) >= MAX_ENTRIES:
            raise SyncError(f"the cloud folder holds more than {MAX_ENTRIES} items; pick a smaller folder")
        names_here[name_key(name)] = (path, entry_id)
        return path

    def _is_the_synced_one(self, path: str, entry_id: str | None) -> bool:
        if path not in self.known:
            return False
        synced_id = self.known[path]
        return entry_id is None or synced_id is None or synced_id == entry_id

    def _settle_clash(self, names_here: dict, held: tuple[str, str | None], path: str, kind: str,
                      entry_id: str | None, size: int | None) -> str | None:
        held_path, held_id = held
        self.doubts.unsure.add(path_key(path))
        reason = (f"another {kind} in this folder has the same name apart from capital letters, and Drive cannot "
                  "hold both")
        if self._is_the_synced_one(held_path, held_id) or not self._is_the_synced_one(path, entry_id):
            self.skip(parent_of(path), name_of(path), reason, entry_id, kind, size)
            return None
        evicted = self.entries.pop(held_path, None)
        self.skip(parent_of(held_path), name_of(held_path), reason, held_id,
                  evicted.kind if evicted else kind, evicted.size if evicted else None)
        names_here[name_key(name_of(path))] = (path, entry_id)
        return path

    def skip(self, parent: str, name: str, reason: str, entry_id: str | None = None, kind: str = "file",
             size: int | None = None) -> None:
        self.skipped.append(f"{shown(join(parent, name))}: {reason}")
        self.doubts.folders_with_skips.add(path_key(parent))
        if entry_id:
            self.doubts.skipped_ids.add(entry_id)
        elif kind == "folder":
            self.doubts.skipped_folder_without_id = True
        else:
            self.doubts.skipped_file_sizes_without_ids.add(size)
            self.doubts.folders_with_skipped_files_without_ids.add(path_key(parent))

    def doubt(self, parent: str, name: str, reason: str, entry_id: str | None = None, kind: str = "file",
              size: int | None = None) -> None:
        self.doubts.unsure.add(path_key(join(parent, name)))
        self.skip(parent, name, reason, entry_id, kind, size)

    def distrust(self, folder: str) -> None:
        self.doubts.unsure.add(path_key(folder))


class Remote(Protocol):
    label: str

    def prepare(self, create_missing: bool) -> None: ...

    def list_tree(self, known: dict[str, str | None] | None = None, start: str = "") -> Listing: ...

    def download(self, entry: RemoteEntry, target: Path, max_bytes: int) -> None: ...

    def upload_new(self, path: str, source: Path, size: int) -> RemoteEntry: ...

    def replace(self, entry: RemoteEntry, path: str, source: Path, size: int) -> RemoteEntry: ...

    def make_folder(self, path: str) -> RemoteEntry: ...

    def move(self, entry: RemoteEntry, old_path: str, new_path: str) -> RemoteEntry: ...

    def trash(self, entry: RemoteEntry, path: str) -> None: ...
