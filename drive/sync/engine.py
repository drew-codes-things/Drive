import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..db import Database
from ..disk import DiskFull
from ..errors import DriveError
from ..names import MAX_NAME_BYTES, split_extension
from .local import LocalEntry, LocalSide, file_md5
from .planner import (MOVES, PULLING, PUSHING, Action, BaseEntry, BinLimits, bin_approval, binned_files, plan,
                      synced_file_count)
from .remote import (FolderMissing, Listing, Remote, RemoteEntry, SignInNeeded, StopRun, SyncError, is_under, join,
                     name_of, parent_of, shown)

PREVIEW_LINES = 500
KEPT_MESSAGES = 100
CONFLICT_STAMP = "%Y-%m-%d %H%M"
MOST_CONFLICT_COPIES = 100
UNLISTED_FILE_INSIDE = ("it still holds a file that is not in the list of files to bin, so it was not binned; the "
                        "next run looks again")
COUNT_NAMES = {
    "upload": "uploaded", "replace_remote": "uploaded", "download": "downloaded", "replace_local": "downloaded",
    "remote_trash": "binnedInCloud", "local_trash": "binnedInDrive", "remote_move": "moved", "local_move": "moved",
    "remote_folder": "foldersMade", "local_folder": "foldersMade",
}

log = logging.getLogger("drive.sync.engine")


@dataclass
class Report:
    counts: dict = field(default_factory=dict)
    errors: list = field(default_factory=list)
    skipped: list = field(default_factory=list)
    listed_skips: list = field(default_factory=list)
    actions: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    stopped: str | None = None
    sign_in_needed: bool = False
    held_bins: int = 0
    held: dict | None = None

    def count(self, name: str, amount: int = 1) -> None:
        self.counts[name] = self.counts.get(name, 0) + amount

    def error(self, path: str, error: Exception) -> None:
        self.count("errors")
        if len(self.errors) < KEPT_MESSAGES:
            self.errors.append(f"{shown(path)}: {error}")

    def hold(self, bins: "BinsHeld") -> None:
        self.held_bins = len(bins.files)
        self.held = {"bins": len(bins.files), "approval": bins.approval,
                     "files": [{"side": side, "path": path} for side, path, _ in bins.files]}

    def skip(self, message: str) -> None:
        self.count("skipped")
        if len(self.skipped) < KEPT_MESSAGES:
            self.skipped.append(message)

    def list_skips(self, lines: list[str]) -> None:
        self.counts["skipped"] = self.counts.get("skipped", 0) - len(self.listed_skips) + len(lines)
        self.listed_skips = list(lines)

    def shown_skips(self) -> list[str]:
        lines = self.skipped + self.listed_skips
        total = self.counts.get("skipped", len(lines))
        if total <= KEPT_MESSAGES:
            return lines[:KEPT_MESSAGES]
        shown_lines = lines[:KEPT_MESSAGES - 1]
        return shown_lines + [f"and {total - len(shown_lines)} more skipped names not listed here ({total} in all)"]


class SyncState:
    def __init__(self, database: Database, connection_id: int):
        self.database = database
        self.connection_id = connection_id

    def load(self) -> dict[str, BaseEntry]:
        with self.database.connect() as connection:
            rows = connection.execute("SELECT * FROM sync_entries WHERE connection_id = ?",
                                      (self.connection_id,)).fetchall()
        return {row["path"]: BaseEntry(row["kind"], row["local_id"], row["local_version"], row["remote_id"],
                                       row["remote_version"]) for row in rows}

    def save(self, path: str, local: LocalEntry, remote: RemoteEntry) -> None:
        with self.database.connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO sync_entries (connection_id, path, kind, local_id, local_version, remote_id,"
                " remote_version) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (self.connection_id, path, local.kind, local.id, local.version, remote.id or remote.ref,
                 remote.version))

    def forget(self, path: str, below: bool = False) -> None:
        with self.database.connect() as connection:
            connection.execute("DELETE FROM sync_entries WHERE connection_id = ? AND path = ?",
                               (self.connection_id, path))
            if below:
                connection.execute("DELETE FROM sync_entries WHERE connection_id = ? AND substr(path, 1, ?) = ?",
                                   (self.connection_id, len(path) + 1, path + "/"))

    def rename(self, old: str, new: str) -> None:
        with self.database.connect() as connection:
            connection.execute("UPDATE sync_entries SET path = ? WHERE connection_id = ? AND path = ?",
                               (new, self.connection_id, old))
            connection.execute(
                "UPDATE sync_entries SET path = ? || substr(path, ?)"
                " WHERE connection_id = ? AND substr(path, 1, ?) = ?",
                (new, len(old) + 1, self.connection_id, len(old) + 1, old + "/"))


class NotLanded(SyncError):
    pass


def _what_was_done(renamed: int) -> str:
    if not renamed:
        return "nothing was changed"
    return f"it stopped after {renamed} rename{'s' if renamed != 1 else ''} and changed nothing else"


@dataclass(frozen=True)
class BinsHeld:
    files: list[tuple[str, str, str]]
    synced_files: int

    @property
    def approval(self) -> str:
        return bin_approval(self.files)


class HeldBack(SyncError):
    def __init__(self, bins: BinsHeld, renamed: int = 0):
        super().__init__(f"this run would move {len(bins.files)} of the {bins.synced_files} synced files to a bin, "
                         f"more than the bin brake allows, so {_what_was_done(renamed)}; look at the list and "
                         "confirm if those are the files to bin")
        self.bins = bins


def _bin_brake_note(bins: int, synced_files: int) -> str:
    return (f"running this would move {bins} of the {synced_files} synced files to a bin, more than the bin brake "
            "allows, so Drive asks before doing it")


def conflict_name(path: str, side: str, taken, stamp: str) -> str:
    stem, extension = split_extension(name_of(path))
    for number in range(1, MOST_CONFLICT_COPIES):
        suffix = f" (conflict, {side} copy {stamp}{'' if number == 1 else f' {number}'})"
        while len((stem + suffix + extension).encode("utf-8")) > MAX_NAME_BYTES and stem:
            stem = stem[:-1]
        candidate = join(parent_of(path), f"{stem}{suffix}{extension}")
        if not taken(candidate):
            return candidate
    raise SyncError(f"too many conflict copies of {shown(path)}")


def _known_remote_ids(base: dict[str, BaseEntry]) -> dict[str, str | None]:
    return {path: entry.remote_id for path, entry in base.items()}


def _is_listed_cloud_file(path: str, entry: RemoteEntry, listed: dict[str, str]) -> bool:
    return path in listed and (not entry.id or entry.id == listed[path])


def _check_arrival(entry: RemoteEntry, incoming: Path) -> None:
    arrived = incoming.stat().st_size
    if arrived != entry.size:
        raise SyncError(f"the download was {arrived} bytes but the cloud listed {entry.size}, so it was not kept")
    if entry.md5 and file_md5(incoming) != entry.md5:
        raise SyncError("the download did not match the checksum the cloud listed, so it was not kept")


class SyncEngine:
    def __init__(self, state: SyncState, local: LocalSide, remote: Remote, direction: str, max_file_bytes: int,
                 clock=time.time, bin_limits: BinLimits = BinLimits(), synced_before: bool = False):
        self.state = state
        self.local = local
        self.remote = remote
        self.direction = direction
        self.max_file_bytes = max_file_bytes
        self.clock = clock
        self.bin_limits = bin_limits
        self.synced_before = synced_before
        self._local_now: dict[str, LocalEntry] = {}
        self._remote_now: dict[str, RemoteEntry] = {}
        self._base: dict[str, BaseEntry] = {}

    @property
    def pushes(self) -> bool:
        return self.direction in PUSHING

    @property
    def pulls(self) -> bool:
        return self.direction in PULLING

    def _open_cloud(self, report: Report, base: dict, for_preview: bool) -> Listing:
        may_make_folder = self.pushes and not self.synced_before and not base
        try:
            self.remote.prepare(create_missing=may_make_folder and not for_preview)
            return self.remote.list_tree(_known_remote_ids(base))
        except FolderMissing as missing:
            if may_make_folder:
                report.notes.append("the cloud folder does not exist yet; the first sync makes it")
                return Listing()
            if self.synced_before or base:
                raise FolderMissing(f"{missing}; it held synced files before, so it may have been renamed, moved or "
                                    "deleted in the cloud. Nothing was changed: put it back, or make a new connection "
                                    "for its new place") from missing
            raise

    def _look(self, report: Report, for_preview: bool) -> list[Action]:
        local = self.local.tree()
        base = self.state.load()
        listing = self._open_cloud(report, base, for_preview)
        report.list_skips(listing.skipped)
        self._local_now, self._remote_now, self._base = local, listing.entries, base
        return plan(base, local, listing.entries, self.direction, listing.doubts)

    def _bins_beyond_the_brake(self, actions: list[Action]) -> BinsHeld | None:
        files, synced_files = binned_files(actions), synced_file_count(self._base)
        return BinsHeld(files, synced_files) if self.bin_limits.exceeded(len(files), synced_files) else None

    def _check_bin_brake(self, actions: list[Action], approval: str | None, renamed: int = 0) -> None:
        beyond = self._bins_beyond_the_brake(actions)
        if beyond is not None and beyond.approval != approval:
            raise HeldBack(beyond, renamed)

    def preview(self) -> Report:
        report = Report()
        try:
            actions = self._look(report, for_preview=True)
        except SignInNeeded as error:
            report.stopped, report.sign_in_needed = str(error), True
            return report
        except SyncError as error:
            report.stopped = str(error)
            return report
        beyond = self._bins_beyond_the_brake(actions)
        if beyond is not None:
            report.hold(beyond)
            report.notes.append(_bin_brake_note(len(beyond.files), beyond.synced_files))
        for action in actions:
            report.count(action.kind)
            if action.kind == "skip":
                report.skip(f"{shown(action.path)}: {action.note}")
            if len(report.actions) < PREVIEW_LINES:
                report.actions.append(self._describe(action))
        return report

    @staticmethod
    def _describe(action: Action) -> dict:
        entry = action.local or action.remote
        return {"action": action.kind, "path": action.path, "target": action.target, "kind": action.entry_kind,
                "size": getattr(entry, "size", 0) if action.entry_kind == "file" else 0}

    def run(self, approval: str | None = None) -> Report:
        report = Report()
        try:
            actions = self._look(report, for_preview=False)
            self._check_bin_brake(actions, approval)
            moves = [action for action in actions if action.kind in MOVES]
            if moves:
                self._execute(moves, report)
                actions = self._look(report, for_preview=False)
                self._check_bin_brake(actions, approval, report.counts.get("moved", 0))
            self._execute([action for action in actions if action.kind not in MOVES], report)
        except HeldBack as held:
            report.stopped = str(held)
            report.hold(held.bins)
            report.counts["binsHeld"] = report.held_bins
        except DiskFull as full:
            report.stopped = f"stopped to keep the disk's free space: {full}"
        except SignInNeeded as error:
            report.stopped, report.sign_in_needed = str(error), True
        except SyncError as error:
            report.stopped = str(error)
        return report

    def _execute(self, actions: list[Action], report: Report) -> None:
        for action in actions:
            try:
                getattr(self, f"_do_{action.kind}")(action, report)
            except (StopRun, DiskFull):
                raise
            except NotLanded as skipped:
                report.skip(f"{shown(action.path)}: {skipped}")
            except (SyncError, DriveError, OSError, ValueError) as error:
                log.info("sync step %s failed: %s", action.kind, type(error).__name__)
                report.error(action.path, error)
            else:
                if action.kind in COUNT_NAMES:
                    report.count(COUNT_NAMES[action.kind])

    def _do_skip(self, action: Action, report: Report) -> None:
        report.skip(f"{shown(action.path)}: {action.note}")

    def _do_forget(self, action: Action, report: Report) -> None:
        self.state.forget(action.path)

    def _do_record(self, action: Action, report: Report) -> None:
        self.state.save(action.path, action.local, action.remote)
        report.count("alreadyTheSame")

    def _do_remote_move(self, action: Action, report: Report) -> None:
        moved = self.remote.move(action.remote, action.path, action.target)
        self.state.rename(action.path, action.target)
        self.state.save(action.target, action.local, moved)

    def _do_local_move(self, action: Action, report: Report) -> None:
        moved = self.local.move(action.local, action.path, action.target)
        self.state.rename(action.path, action.target)
        self.state.save(action.target, moved, action.remote)

    def _check_the_cloud_folder_before_binning(self, action: Action) -> None:
        seen = {(path, entry.kind, entry.version) for path, entry in self._remote_now.items()
                if is_under(path, action.path) and path != action.path}
        relisted = self.remote.list_tree(_known_remote_ids(self._base), start=action.path)
        if relisted.skipped or any((path, entry.kind, entry.version) not in seen
                                   for path, entry in relisted.entries.items()):
            raise NotLanded("it changed in the cloud after it was listed, so it was not binned; the next run looks "
                            "again")
        listed = dict(action.files_inside)
        if any(not _is_listed_cloud_file(path, entry, listed)
               for path, entry in relisted.entries.items() if entry.kind == "file"):
            raise NotLanded(UNLISTED_FILE_INSIDE)

    def _do_remote_trash(self, action: Action, report: Report) -> None:
        if action.entry_kind == "folder":
            self._check_the_cloud_folder_before_binning(action)
        self.remote.trash(action.remote, action.path)
        self.state.forget(action.path, below=True)

    def _check_the_drive_folder_before_binning(self, action: Action) -> None:
        seen = {(entry.id, entry.version) for path, entry in self._local_now.items()
                if is_under(path, action.path) and path != action.path}
        inside = self.local.inside(action.local)
        if any((entry.id, entry.version) not in seen for entry in inside):
            raise NotLanded("it changed in Drive while the cloud was being listed, so it was not binned; the next run "
                            "looks again")
        listed = {identity for _, identity in action.files_inside}
        if any(str(entry.id) not in listed for entry in inside if entry.kind == "file"):
            raise NotLanded(UNLISTED_FILE_INSIDE)

    def _do_local_trash(self, action: Action, report: Report) -> None:
        if action.entry_kind == "folder":
            self._check_the_drive_folder_before_binning(action)
        self.local.trash(action.local)
        self.state.forget(action.path, below=True)

    def _do_remote_folder(self, action: Action, report: Report) -> None:
        self.state.save(action.path, action.local, self.remote.make_folder(action.path))

    def _do_local_folder(self, action: Action, report: Report) -> None:
        self.state.save(action.path, self.local.make_folder(action.path), action.remote)

    def _do_upload(self, action: Action, report: Report) -> None:
        uploaded = self.remote.upload_new(action.path, self.local.blob_path(action.local), action.local.size)
        self.state.save(action.path, action.local, uploaded)

    def _do_replace_remote(self, action: Action, report: Report) -> None:
        uploaded = self.remote.replace(action.remote, action.path, self.local.blob_path(action.local),
                                       action.local.size)
        self.state.save(action.path, action.local, uploaded)

    def _fetch(self, entry: RemoteEntry, path: str) -> Path:
        if entry.size > self.max_file_bytes:
            raise SyncError(f"it is bigger than the {self.max_file_bytes // 1024 ** 3} GB a Drive file can be")
        incoming = self.local.temp_file()
        try:
            self.remote.download(entry, incoming, entry.size)
            _check_arrival(entry, incoming)
        except BaseException:
            incoming.unlink(missing_ok=True)
            raise
        return incoming

    def _land(self, path: str, entry: RemoteEntry, replacing: LocalEntry | None = None) -> None:
        if replacing is None and self.local.name_taken(path):
            raise NotLanded("Drive already has a name here that differs only in capital letters; it waits until "
                            "that one is gone or renamed")
        with self.local.room_for(entry.size):
            incoming = self._fetch(entry, path)
            try:
                if replacing is not None and file_md5(incoming) == self.local.md5(replacing):
                    self.state.save(path, replacing, entry)
                    return
                if replacing is None:
                    landed, stored = self.local.add_file(path, incoming, entry.size, entry.modified_at)
                else:
                    landed, stored = self.local.replace_file(path, replacing, incoming, entry.size,
                                                             entry.modified_at)
            finally:
                incoming.unlink(missing_ok=True)
        if landed == path:
            self.state.save(path, stored, entry)

    def _do_download(self, action: Action, report: Report) -> None:
        self._land(action.path, action.remote)

    def _do_replace_local(self, action: Action, report: Report) -> None:
        self._land(action.path, action.remote, replacing=action.local)

    def _taken(self, path: str) -> bool:
        return path in self._local_now or path in self._remote_now

    def _same_content(self, action: Action) -> bool:
        local, remote = action.local, action.remote
        if local.size != remote.size:
            return False
        if remote.md5:
            return remote.md5 == self.local.md5(local)
        with self.local.room_for(remote.size):
            incoming = self._fetch(remote, action.path)
            try:
                return file_md5(incoming) == self.local.md5(local)
            finally:
                incoming.unlink(missing_ok=True)

    def _do_compare(self, action: Action, report: Report) -> None:
        if self._same_content(action):
            self.state.save(action.path, action.local, action.remote)
            report.count("alreadyTheSame")
            return
        stamp = time.strftime(CONFLICT_STAMP, time.localtime(self.clock()))
        if self.direction == "download":
            self._keep_drive_copy(action, stamp)
        else:
            self._keep_cloud_copy(action, stamp)
        report.count("conflicts")

    def _keep_cloud_copy(self, action: Action, stamp: str) -> None:
        copy_path = conflict_name(action.path, "cloud", self._taken, stamp)
        moved = self.remote.move(action.remote, action.path, copy_path)
        self._remote_now[copy_path] = moved
        if self.pulls:
            with self.local.room_for(moved.size):
                incoming = self._fetch(moved, copy_path)
                try:
                    landed, stored = self.local.add_file(copy_path, incoming, moved.size, moved.modified_at)
                finally:
                    incoming.unlink(missing_ok=True)
            self.state.save(landed, stored, moved)
        uploaded = self.remote.upload_new(action.path, self.local.blob_path(action.local), action.local.size)
        self.state.save(action.path, action.local, uploaded)

    def _keep_drive_copy(self, action: Action, stamp: str) -> None:
        copy_path = conflict_name(action.path, "Drive", self._taken, stamp)
        self.local.move(action.local, action.path, copy_path)
        self._local_now[copy_path] = action.local
        self._land(action.path, action.remote)
