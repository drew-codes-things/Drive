import pytest

from drive.sync.local import LocalEntry
from drive.sync.planner import BaseEntry, BinLimits, bin_approval, binned_files, plan
from drive.sync.remote import Doubts, Listing, RemoteEntry


def here(kind: str = "file", node_id: int = 1, version: str = "blob-1") -> LocalEntry:
    return LocalEntry(kind=kind, id=node_id, version=version if kind == "file" else str(node_id))


def there(kind: str = "file", version: str = "etag-1", entry_id: str | None = None) -> RemoteEntry:
    return RemoteEntry(kind=kind, ref=f"ref-{version}", version=version, id=entry_id)


def synced(kind: str = "file", node_id: int = 1, local_version: str = "blob-1", remote_version: str = "etag-1",
           remote_id: str | None = None) -> BaseEntry:
    return BaseEntry(kind, node_id, local_version if kind == "file" else str(node_id), remote_id, remote_version)


def kinds(actions) -> list[tuple[str, str]]:
    return [(action.kind, action.path) for action in actions]


@pytest.mark.parametrize("direction", ["both", "upload", "download"])
def test_a_first_run_never_deletes(direction):
    local = {"a.txt": here(), "docs": here("folder", 2)}
    remote = {"b.txt": there(), "docs": there("folder")}
    planned = plan({}, local, remote, direction)
    assert not [action for action in planned if "trash" in action.kind]


def test_a_first_two_way_run_copies_each_side_across():
    planned = plan({}, {"mine.txt": here(), "same.txt": here(node_id=3)},
                   {"theirs.txt": there(), "same.txt": there(version="e3")}, "both")
    assert kinds(planned) == [("upload", "mine.txt"), ("compare", "same.txt"), ("download", "theirs.txt")]


def test_one_way_runs_leave_the_other_sides_new_files_alone():
    local, remote = {"mine.txt": here()}, {"theirs.txt": there()}
    assert kinds(plan({}, local, remote, "upload")) == [("upload", "mine.txt")]
    assert kinds(plan({}, local, remote, "download")) == [("download", "theirs.txt")]


def test_changes_flow_the_right_way():
    base = {"a.txt": synced(), "b.txt": synced(node_id=2)}
    local = {"a.txt": here(version="blob-new"), "b.txt": here(node_id=2)}
    remote = {"a.txt": there(), "b.txt": there(version="etag-new")}
    assert kinds(plan(base, local, remote, "both")) == [("replace_remote", "a.txt"), ("replace_local", "b.txt")]


def test_deletes_go_to_the_other_sides_bin():
    base = {"gone-here.txt": synced(), "gone-there.txt": synced(node_id=2)}
    local = {"gone-there.txt": here(node_id=2)}
    remote = {"gone-here.txt": there()}
    assert kinds(plan(base, local, remote, "both")) == [("remote_trash", "gone-here.txt"),
                                                         ("local_trash", "gone-there.txt")]


def test_a_delete_never_wins_over_a_change():
    base = {"edited-there.txt": synced(), "edited-here.txt": synced(node_id=2)}
    local = {"edited-here.txt": here(node_id=2, version="blob-new")}
    remote = {"edited-there.txt": there(version="etag-new")}
    assert kinds(plan(base, local, remote, "both")) == [("upload", "edited-here.txt"),
                                                         ("download", "edited-there.txt")]


def test_both_sides_changed_is_compared_not_overwritten():
    base = {"a.txt": synced()}
    planned = plan(base, {"a.txt": here(version="blob-2")}, {"a.txt": there(version="etag-2")}, "both")
    assert kinds(planned) == [("compare", "a.txt")]


def test_upload_only_puts_back_what_the_cloud_lost_and_download_only_does_the_same_here():
    base = {"a.txt": synced()}
    assert kinds(plan(base, {"a.txt": here()}, {}, "upload")) == [("upload", "a.txt")]
    assert kinds(plan(base, {}, {"a.txt": there()}, "download")) == [("download", "a.txt")]


def test_a_deleted_folder_whose_contents_did_not_change_goes_to_the_bin_whole():
    base = {"old": synced("folder", 5, remote_version="f"), "old/a.txt": synced(node_id=6)}
    remote = {"old": there("folder", "f"), "old/a.txt": there()}
    assert kinds(plan(base, {}, remote, "both")) == [("remote_trash", "old")]


def test_a_deleted_folder_with_new_cloud_files_comes_back():
    base = {"old": synced("folder", 5, remote_version="f"), "old/a.txt": synced(node_id=6)}
    remote = {"old": there("folder", "f"), "old/a.txt": there(), "old/new.txt": there(version="n")}
    assert kinds(plan(base, {}, remote, "both")) == [("local_folder", "old"), ("download", "old/new.txt"),
                                                      ("remote_trash", "old/a.txt")]


def test_a_rename_here_is_a_move_there():
    base = {"draft.txt": synced(node_id=7)}
    planned = plan(base, {"final.txt": here(node_id=7)}, {"draft.txt": there()}, "both")
    assert [(action.kind, action.path, action.target) for action in planned] == [
        ("remote_move", "draft.txt", "final.txt")]


def test_a_renamed_folder_moves_once_with_everything_in_it():
    base = {"old": synced("folder", 5, remote_version="f"), "old/a.txt": synced(node_id=6)}
    local = {"new": here("folder", 5), "new/a.txt": here(node_id=6)}
    remote = {"old": there("folder", "f"), "old/a.txt": there()}
    planned = plan(base, local, remote, "both")
    assert [(action.kind, action.path, action.target) for action in planned] == [("remote_move", "old", "new")]


def test_a_rename_in_the_cloud_is_a_move_here_when_the_cloud_keeps_ids():
    base = {"draft.txt": synced(remote_id="g1")}
    planned = plan(base, {"draft.txt": here()}, {"final.txt": there(entry_id="g1")}, "both")
    assert [(action.kind, action.path, action.target) for action in planned] == [
        ("local_move", "draft.txt", "final.txt")]


def test_a_rename_without_ids_is_a_delete_and_a_new_file():
    base = {"draft.txt": synced()}
    planned = plan(base, {"draft.txt": here()}, {"final.txt": there(version="etag-1")}, "both")
    assert kinds(planned) == [("download", "final.txt"), ("local_trash", "draft.txt")]


def test_a_file_on_one_side_and_a_folder_on_the_other_is_skipped_with_everything_below():
    planned = plan({}, {"x": here("folder", 3), "x/a.txt": here(node_id=4)}, {"x": there()}, "both")
    assert kinds(planned) == [("skip", "x")]


def test_folders_are_made_before_the_files_that_go_in_them():
    planned = plan({}, {"a": here("folder", 2), "a/b": here("folder", 3), "a/b/c.txt": here(node_id=4)}, {}, "both")
    assert kinds(planned) == [("remote_folder", "a"), ("remote_folder", "a/b"), ("upload", "a/b/c.txt")]


def test_unknown_directions_are_refused():
    with pytest.raises(ValueError):
        plan({}, {}, {}, "sideways")


def test_the_bin_brake_counts_files_inside_binned_folders():
    base = {"Trip": synced("folder", 1), "Trip/a.jpg": synced(node_id=2), "Trip/b.jpg": synced(node_id=3),
            "c.txt": synced(node_id=4), "d.txt": synced(node_id=5)}
    local = {"Trip": here("folder", 1), "Trip/a.jpg": here(node_id=2), "Trip/b.jpg": here(node_id=3),
             "c.txt": here(node_id=4), "d.txt": here(node_id=5)}
    remote = {"d.txt": there()}
    planned = plan(base, local, remote, "both")
    assert len(binned_files(planned)) == 3


@pytest.mark.parametrize("bins,synced_files,held", [
    (3, 3, False), (4, 6, True), (4, 8, False), (101, 10_000, True), (100, 10_000, False),
])
def test_the_bin_brake_holds_most_of_a_folder_or_very_many_files(bins, synced_files, held):
    assert BinLimits(files=100, percent=50).exceeded(bins, synced_files) is held


@pytest.mark.parametrize("direction", ["both", "upload", "download"])
def test_a_synced_path_the_cloud_listing_skipped_is_left_alone(direction):
    base = {"notes.txt": synced(), "Trip": synced("folder", 2), "Trip/a.jpg": synced(node_id=3)}
    local = {"notes.txt": here(), "Trip": here("folder", 2), "Trip/a.jpg": here(node_id=3)}
    planned = plan(base, local, {}, direction, Doubts(unsure={"notes.txt", "trip"}))
    assert [(action.kind, action.path) for action in planned] == [("skip", "Trip"), ("skip", "notes.txt")]
    assert all("skipped" in action.note for action in planned)


def test_a_held_path_gone_on_both_sides_is_forgotten():
    base = {"notes.txt": synced(), "Trip": synced("folder", 2)}
    doubts = Doubts(unsure={"notes.txt", "trip"}, skipped_file_sizes_without_ids={None})
    assert sorted(kinds(plan(base, {}, {}, "both", doubts))) == [("forget", "Trip"), ("forget", "notes.txt")]


def test_a_hold_found_by_id_follows_the_path_after_a_cloud_move():
    base = {"F": synced("folder", 1, remote_id="id-f"), "F/x.txt": synced(node_id=2, remote_id="id-x"),
            "F/y.txt": synced(node_id=3, local_version="blob-3", remote_id="id-y")}
    local = {"F": here("folder", 1), "F/x.txt": here(node_id=2), "F/y.txt": here(node_id=3, version="blob-3")}
    listing = Listing(known={path: entry.remote_id for path, entry in base.items()})
    for parent, name, kind, entry_id in (("", "G", "folder", "id-f"), ("G", "x\\y.txt", "file", "id-x"),
                                         ("G", "y.txt", "file", "id-y")):
        accepted = listing.accept(parent, name, kind, entry_id)
        if accepted is not None:
            listing.entries[accepted] = RemoteEntry(kind, entry_id, "etag-1", id=entry_id)
    planned = plan(base, local, listing.entries, "both", listing.doubts)
    assert ("local_move", "F") in kinds(planned)
    assert not [action for action in planned if action.kind == "local_trash"], kinds(planned)


def test_a_different_cloud_file_at_a_synced_path_updates_drive_only_when_drive_did_not_change():
    base = {"scan.pdf": synced(remote_id="id-1")}
    planned = plan(base, {"scan.pdf": here(version="blob-2")}, {"scan.pdf": there(version="etag-2", entry_id="id-2")},
                   "both")
    assert kinds(planned) == [("compare", "scan.pdf")]
    planned = plan(base, {"scan.pdf": here()}, {"scan.pdf": there(version="etag-2", entry_id="id-2")}, "both")
    assert kinds(planned) == [("replace_local", "scan.pdf")]
    planned = plan(base, {"scan.pdf": here()}, {"scan.pdf": there(version="etag-2", entry_id="id-2")}, "upload")
    assert kinds(planned) == [("compare", "scan.pdf")]
    planned = plan(base, {"scan.pdf": here()}, {"scan.pdf": there(version="etag-2", entry_id="id-1")}, "both")
    assert kinds(planned) == [("replace_local", "scan.pdf")]


def test_the_files_to_bin_name_the_side_the_path_and_the_file():
    base = {"Trip": synced("folder", 1, remote_id="id-t"), "Trip/a.jpg": synced(node_id=2, remote_id="id-a"),
            "Trip/b.jpg": synced(node_id=3, remote_id="id-b"), "notes.txt": synced(node_id=4, remote_version="e4")}
    local = {"notes.txt": here(node_id=4)}
    remote = {"Trip": there("folder", entry_id="id-t"), "Trip/a.jpg": there(entry_id="id-a"),
              "Trip/b.jpg": there(entry_id="id-b")}
    planned = plan(base, local, remote, "both")
    files = binned_files(planned)
    assert files == [("cloud", "Trip/a.jpg", "id-a"), ("cloud", "Trip/b.jpg", "id-b"), ("drive", "notes.txt", "4")]
    assert bin_approval(files) == bin_approval(list(reversed(files)))
    assert bin_approval(files) != bin_approval(files[:2])
