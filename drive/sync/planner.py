import hashlib
import json
from dataclasses import dataclass

from .remote import Doubts, depth, is_under, path_key, paths_from_top, shown

DIRECTIONS = ("download", "upload", "both")
PUSHING = ("upload", "both")
PULLING = ("download", "both")

FILE_RULES = {
    "both": {
        ("new", "absent"): "upload", ("absent", "new"): "download",
        ("new", "new"): "compare", ("changed", "changed"): "compare",
        ("changed", "same"): "replace_remote", ("same", "changed"): "replace_local",
        ("changed", "gone"): "upload", ("gone", "changed"): "download",
        ("gone", "same"): "remote_trash", ("same", "gone"): "local_trash",
        ("gone", "gone"): "forget",
    },
    "upload": {
        ("new", "absent"): "upload",
        ("new", "new"): "compare", ("changed", "changed"): "compare",
        ("changed", "same"): "replace_remote", ("same", "changed"): "replace_remote",
        ("changed", "gone"): "upload", ("same", "gone"): "upload",
        ("gone", "same"): "remote_trash", ("gone", "changed"): "forget",
        ("gone", "gone"): "forget",
    },
    "download": {
        ("absent", "new"): "download",
        ("new", "new"): "compare", ("changed", "changed"): "compare",
        ("same", "changed"): "replace_local", ("changed", "same"): "replace_local",
        ("gone", "changed"): "download", ("gone", "same"): "download",
        ("same", "gone"): "local_trash", ("changed", "gone"): "forget",
        ("gone", "gone"): "forget",
    },
}
FOLDER_RULES = {
    "both": {
        ("new", "absent"): "remote_folder", ("absent", "new"): "local_folder", ("new", "new"): "record",
        ("gone", "same"): "local_folder", ("same", "gone"): "remote_folder", ("gone", "gone"): "forget",
    },
    "upload": {
        ("new", "absent"): "remote_folder", ("new", "new"): "record",
        ("gone", "same"): "forget", ("same", "gone"): "remote_folder", ("gone", "gone"): "forget",
    },
    "download": {
        ("absent", "new"): "local_folder", ("new", "new"): "record",
        ("gone", "same"): "local_folder", ("same", "gone"): "forget", ("gone", "gone"): "forget",
    },
}
DEFAULT_BIN_LIMIT_FILES = 100
DEFAULT_BIN_LIMIT_PERCENT = 50
HANDFUL_OF_FILES = 3
MOVES = ("remote_move", "local_move")
TRASHES = ("remote_trash", "local_trash")
FOLDER_MAKERS = ("remote_folder", "local_folder")
DRIVE_SIDE = "drive"
CLOUD_SIDE = "cloud"
HOLD_ALL = "all"
HOLD_BINS = "bins"
HELD_NOTE = ("kept in Drive and left alone: the cloud listing skipped or doubted it, so it may have been renamed in "
             "the cloud to a name Drive cannot take")
KEPT_NOTE = ("gone from the cloud but kept in Drive, because the cloud folder holds a skipped name it may have been "
             "renamed to; rename that one in the cloud to sync it")
FOLDER_KEPT_NOTE = ("gone from the cloud but kept in Drive and not made again in the cloud, because it holds files the "
                    "cloud may have renamed to a skipped name; rename those in the cloud to sync them")
KEPT_IN_CLOUD_NOTE = ("deleted in Drive, but its cloud folder holds items Drive does not sync, so the folder stays in "
                      "the cloud with those items and is not made again in Drive")


@dataclass(frozen=True)
class BaseEntry:
    kind: str
    local_id: int | None
    local_version: str
    remote_id: str | None
    remote_version: str


@dataclass(frozen=True)
class BinLimits:
    files: int = DEFAULT_BIN_LIMIT_FILES
    percent: int = DEFAULT_BIN_LIMIT_PERCENT

    def exceeded(self, bins: int, synced_files: int) -> bool:
        if bins > self.files:
            return True
        return bins > HANDFUL_OF_FILES and bins * 100 > self.percent * synced_files


@dataclass(frozen=True)
class Action:
    kind: str
    path: str
    local: object = None
    remote: object = None
    target: str | None = None
    note: str = ""
    files_inside: tuple[tuple[str, str], ...] = ()

    @property
    def entry_kind(self) -> str:
        entry = self.local or self.remote
        return entry.kind if entry is not None else "file"


def synced_file_count(base: dict) -> int:
    return sum(1 for entry in base.values() if entry.kind == "file")


def _synced_identity(side: str, entry: BaseEntry) -> str:
    if side == DRIVE_SIDE:
        return str(entry.local_id)
    return entry.remote_id or entry.remote_version


def _listed_identity(side: str, action: Action) -> str:
    if side == DRIVE_SIDE:
        return str(action.local.id)
    return action.remote.id or action.remote.version


def _synced_files_inside(folder: str, base: dict, side: str) -> tuple[tuple[str, str], ...]:
    return tuple((path, _synced_identity(side, entry)) for path, entry in base.items()
                 if entry.kind == "file" and is_under(path, folder))


def binned_files(actions: list[Action]) -> list[tuple[str, str, str]]:
    found = []
    for action in actions:
        if action.kind not in TRASHES:
            continue
        side = DRIVE_SIDE if action.kind == "local_trash" else CLOUD_SIDE
        if action.entry_kind == "file":
            found.append((side, action.path, _listed_identity(side, action)))
            continue
        found += [(side, path, identity) for path, identity in action.files_inside]
    return sorted(found)


def bin_approval(files: list[tuple[str, str, str]]) -> str:
    listed = json.dumps(sorted(files), ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(listed.encode("ascii")).hexdigest()


def _state(base: BaseEntry | None, entry, base_version: str | None) -> str:
    if entry is None:
        return "gone" if base else "absent"
    if base is None:
        return "new"
    if entry.kind != base.kind:
        return "changed"
    if entry.kind == "folder" or entry.version == base_version:
        return "same"
    return "changed"


def _rename_keys(mapping: dict, old: str, new: str) -> None:
    for key in [key for key in mapping if is_under(key, old)]:
        mapping[new + key[len(old):]] = mapping.pop(key)


def _unchanged(base: BaseEntry, entry, side: str) -> bool:
    if entry.kind != base.kind:
        return False
    if entry.kind == "folder":
        return True
    return entry.version == (base.local_version if side == "local" else base.remote_version)


def _moved(old: str, base: BaseEntry, mover, other, side: str) -> bool:
    if mover.kind != base.kind or other is None or other.kind != base.kind:
        return False
    if base.kind == "folder":
        return True
    return _unchanged(base, mover, side) and _unchanged(base, other, "remote" if side == "local" else "local")


def _plan_moves(base: dict, local: dict, remote: dict, direction: str) -> list[Action]:
    actions = []
    if direction in PUSHING:
        local_paths = {entry.id: path for path, entry in local.items()}
        for old in sorted(base, key=depth):
            entry = base.get(old)
            new = local_paths.get(entry.local_id) if entry and old not in local else None
            if new is None or new in base or new in remote or not _moved(old, entry, local[new], remote.get(old),
                                                                            "local"):
                continue
            actions.append(Action("remote_move", old, local=local[new], remote=remote[old], target=new))
            _rename_keys(base, old, new)
            _rename_keys(remote, old, new)
    if direction in PULLING:
        remote_paths = {entry.id: path for path, entry in remote.items() if entry.id}
        for old in sorted(base, key=depth):
            entry = base.get(old)
            new = remote_paths.get(entry.remote_id) if entry and entry.remote_id and old not in remote else None
            if new is None or new in base or new in local or not _moved(old, entry, remote[new], local.get(old),
                                                                           "remote"):
                continue
            actions.append(Action("local_move", old, local=local[old], remote=remote[new], target=new))
            _rename_keys(base, old, new)
            _rename_keys(local, old, new)
    return actions


def _subtree_is_unchanged(folder: str, base: dict, side_entries: dict, side: str) -> bool:
    return all(path in base and _unchanged(base[path], entry, side)
               and not (side == "remote" and _is_another_cloud_file(base[path], entry))
               for path, entry in side_entries.items() if is_under(path, folder))


def _hold(path: str, base: dict, here, doubts: Doubts) -> str | None:
    if here is None:
        return None
    if doubts.is_unsure(path):
        return HOLD_ALL
    for synced_path in paths_from_top(path):
        synced = base.get(synced_path)
        if synced is not None and synced.remote_id and synced.remote_id in doubts.skipped_ids:
            return HOLD_ALL
    if doubts.may_have_become_a_skipped(path, here.kind, here.size):
        return HOLD_BINS
    return None


def _holds_something(folder: str, base: dict, local: dict, remote: dict, doubts: Doubts) -> bool:
    return any(_hold(path, base, local.get(path), doubts)
               for path in base if is_under(path, folder) and path not in remote)


def _takes_a_move(folder: str, move_targets: list[str]) -> bool:
    return any(is_under(target, folder) for target in move_targets)


def _is_kept_in_the_cloud(path: str, base: dict, remote: dict, here, doubts: Doubts, move_targets: list[str]) -> bool:
    before, there = base.get(path), remote.get(path)
    return (before is not None and before.kind == "folder" and here is None and there is not None
            and doubts.has_skips_under(path) and _subtree_is_unchanged(path, base, remote, "remote")
            and not _takes_a_move(path, move_targets))


def _plan_folder_trashes(base: dict, local: dict, remote: dict, direction: str, handled: list[str],
                         kept_in_drive: list[str], doubts: Doubts, move_targets: list[str]) -> list[Action]:
    actions = []
    folders = sorted((path for path, entry in base.items() if entry.kind == "folder"), key=depth)
    for path in folders:
        if any(is_under(path, done) for done in handled) or _takes_a_move(path, move_targets):
            continue
        remote_folder, local_folder = remote.get(path), local.get(path)
        if (direction in PUSHING and local_folder is None and remote_folder is not None
                and not doubts.has_skips_under(path) and _subtree_is_unchanged(path, base, remote, "remote")):
            actions.append(Action("remote_trash", path, remote=remote_folder,
                                  files_inside=_synced_files_inside(path, base, CLOUD_SIDE)))
            handled.append(path)
        elif (direction in PULLING and remote_folder is None and local_folder is not None
              and _subtree_is_unchanged(path, base, local, "local")):
            if not _holds_something(path, base, local, remote, doubts):
                actions.append(Action("local_trash", path, local=local_folder,
                                      files_inside=_synced_files_inside(path, base, DRIVE_SIDE)))
                handled.append(path)
            elif direction == "both" and _hold(path, base, local_folder, doubts) != HOLD_ALL:
                actions.append(Action("skip", path, local=local_folder, note=FOLDER_KEPT_NOTE))
                kept_in_drive.append(path)
    return actions


def _order(action: Action) -> int:
    if action.kind in MOVES:
        return 0
    if (action.kind in TRASHES and action.entry_kind == "folder") or action.kind in ("forget", "skip"):
        return 1
    if action.kind in FOLDER_MAKERS:
        return 2
    if action.kind in TRASHES:
        return 4
    return 3


def _is_another_cloud_file(before: BaseEntry, there) -> bool:
    if there is None or there.kind != "file" or before.kind != "file":
        return False
    return bool(there.id and before.remote_id) and there.id != before.remote_id


def _case_twins(base: dict, local: dict, remote: dict) -> dict[str, str]:
    new_here = {path_key(path): path for path in local if path not in base}
    twins = {}
    for path in remote:
        if path in base or path in local:
            continue
        other = new_here.get(path_key(path))
        if other is None or other in remote:
            continue
        note = (f"Drive has {shown(other)} and the cloud has {shown(path)}; names that differ only in capital "
                "letters cannot both be in Drive, so rename one of them")
        twins[path] = twins[other] = note
    return twins


def plan(base: dict, local: dict, remote: dict, direction: str, doubts: Doubts | None = None) -> list[Action]:
    if direction not in DIRECTIONS:
        raise ValueError(f"unknown direction {direction!r}")
    doubts = doubts or Doubts()
    base, local, remote = dict(base), dict(local), dict(remote)
    actions = _plan_moves(base, local, remote, direction)
    move_targets = [action.target for action in actions]
    handled: list[str] = []
    kept_in_drive: list[str] = []
    actions += _plan_folder_trashes(base, local, remote, direction, handled, kept_in_drive, doubts, move_targets)
    twins = _case_twins(base, local, remote)
    for path in sorted(set(base) | set(local) | set(remote), key=lambda key: (depth(key), key)):
        if path in kept_in_drive or any(is_under(path, done) for done in handled):
            continue
        before, here, there = base.get(path), local.get(path), remote.get(path)
        hold = _hold(path, base, here, doubts) if there is None and before is not None else None
        if hold == HOLD_ALL:
            actions.append(Action("skip", path, local=here, note=HELD_NOTE))
            handled.append(path)
            continue
        if direction == "both" and _is_kept_in_the_cloud(path, base, remote, here, doubts, move_targets):
            actions.append(Action("skip", path, remote=there, note=KEPT_IN_CLOUD_NOTE))
            continue
        if before is not None and _is_another_cloud_file(before, there):
            if direction in PULLING and here is not None and _unchanged(before, here, "local"):
                actions.append(Action("replace_local", path, local=here, remote=there))
                continue
            before = None
        if path in twins:
            actions.append(Action("skip", path, local=here, remote=there, note=twins[path]))
            handled.append(path)
            continue
        if here is not None and there is not None and here.kind != there.kind:
            actions.append(Action("skip", path, local=here, remote=there,
                                  note="a file on one side and a folder on the other; rename one of them"))
            handled.append(path)
            continue
        kind = (here or there or before).kind
        rules = (FILE_RULES if kind == "file" else FOLDER_RULES)[direction]
        local_state = _state(before, here, before.local_version if before else None)
        remote_state = _state(before, there, before.remote_version if before else None)
        chosen = rules.get((local_state, remote_state))
        if chosen == "local_trash" and hold == HOLD_BINS:
            actions.append(Action("skip", path, local=here, note=KEPT_NOTE))
        elif chosen:
            actions.append(Action(chosen, path, local=here, remote=there))
    return sorted(actions, key=_order)
