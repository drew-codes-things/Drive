import pytest

from drive.errors import Conflict, DriveError, NotFound
from drive.names import clean_name, numbered_name
from drive.store import BIN_DAYS, DAY_SECONDS, MAX_FOLDER_DEPTH


def add_file(services, name: str, content: bytes = b"hello", folder_id: int = 0) -> dict:
    part = services.blobs.root / "incoming"
    part.write_bytes(content)
    blob = services.blobs.adopt(part)
    return services.store.add_file(folder_id, name, len(content), blob, "text/plain")


def names_in(store, folder_id: int = 0) -> list[str]:
    return [item["name"] for item in store.list_folder(folder_id)["items"]]


@pytest.mark.parametrize("bad", ["", "   ", ".", "..", "a/b", "a\\b", "nul\x00here", "tab\there", "line\nbreak",
                                 "x" * 256, None, 5])
def test_names_that_could_escape_or_confuse_are_refused(bad):
    with pytest.raises(DriveError):
        clean_name(bad)


def test_names_are_trimmed_and_unicode_kept():
    assert clean_name("  holiday été.jpg ") == "holiday été.jpg"


def test_numbered_names_keep_the_extension_and_the_length_limit():
    assert numbered_name("photo.jpg", 2, False) == "photo (2).jpg"
    assert numbered_name("archive.tar", 3, True) == "archive.tar (3)"
    assert len(numbered_name("y" * 251 + ".txt", 12, False).encode()) <= 255


def test_folders_list_first_and_sort_by_name_size_and_date(services, clock):
    store = services.store
    add_file(services, "b.txt", b"12345")
    clock.advance(10)
    add_file(services, "a.txt", b"1")
    store.create_folder(0, "Zeta")
    assert names_in(store) == ["Zeta", "a.txt", "b.txt"]
    by_size = store.list_folder(0, "size", "desc")["items"]
    assert [item["name"] for item in by_size] == ["Zeta", "b.txt", "a.txt"]
    by_date = store.list_folder(0, "modified", "desc")["items"]
    assert [item["name"] for item in by_date][1:] == ["a.txt", "b.txt"]
    with pytest.raises(DriveError):
        store.list_folder(0, "name; DROP TABLE nodes", "asc")
    with pytest.raises(DriveError):
        store.list_folder(0, "name", "sideways")


def test_folder_names_are_unique_ignoring_case_but_files_get_numbers(services):
    store = services.store
    store.create_folder(0, "Photos")
    with pytest.raises(Conflict):
        store.create_folder(0, "photos")
    add_file(services, "notes.txt")
    second = add_file(services, "Notes.txt")
    assert second["name"] == "Notes (2).txt"


def test_rename_refuses_a_name_already_used_here(services):
    store = services.store
    first = add_file(services, "one.txt")
    add_file(services, "two.txt")
    with pytest.raises(Conflict):
        store.rename(first["id"], "TWO.txt")
    assert store.rename(first["id"], "uno.txt")["name"] == "uno.txt"


def test_a_folder_cannot_move_into_itself_or_below_itself(store):
    outer = store.create_folder(0, "outer")
    inner = store.create_folder(outer["id"], "inner")
    with pytest.raises(DriveError):
        store.move([outer["id"]], outer["id"])
    with pytest.raises(DriveError):
        store.move([outer["id"]], inner["id"])


def test_moving_into_a_folder_with_the_same_name_numbers_it(services):
    store = services.store
    target = store.create_folder(0, "target")
    add_file(services, "a.txt", folder_id=target["id"])
    loose = add_file(services, "a.txt")
    moved = store.move([loose["id"]], target["id"])
    assert moved[0]["name"] == "a (2).txt"
    assert names_in(store) == ["target"]


def test_moving_into_a_file_is_refused(services):
    file = add_file(services, "a.txt")
    other = add_file(services, "b.txt")
    with pytest.raises(DriveError):
        services.store.move([other["id"]], file["id"])


def test_copying_a_folder_copies_everything_with_new_blobs(services):
    store = services.store
    folder = store.create_folder(0, "docs")
    sub = store.create_folder(folder["id"], "sub")
    add_file(services, "deep.txt", b"deep", sub["id"])
    copies = store.copy([folder["id"]], 0)
    assert copies[0]["name"] == "docs (2)"
    copied_sub = store.list_folder(copies[0]["id"])["items"][0]
    copied_file = store.list_folder(copied_sub["id"])["items"][0]
    original = store.file_record(store.list_folder(sub["id"])["items"][0]["id"])
    duplicate = store.file_record(copied_file["id"])
    assert duplicate["blob"] != original["blob"]
    assert services.blobs.path(duplicate["blob"]).read_bytes() == b"deep"
    store.trash([folder["id"]])
    store.empty_bin()
    assert services.blobs.path(duplicate["blob"]).read_bytes() == b"deep"


def test_trash_hides_a_whole_folder_and_restore_brings_it_back(services):
    store = services.store
    folder = store.create_folder(0, "trip")
    photo = add_file(services, "beach.jpg", folder_id=folder["id"])
    store.trash([folder["id"]])
    assert names_in(store) == []
    assert [item["name"] for item in store.bin_items()] == ["trip"]
    assert store.search("beach") == []
    with pytest.raises(NotFound):
        store.get(photo["id"])
    store.restore([folder["id"]])
    assert names_in(store, folder["id"]) == ["beach.jpg"]


def test_items_trashed_on_their_own_stay_in_the_bin_when_their_folder_comes_back(services):
    store = services.store
    folder = store.create_folder(0, "trip")
    photo = add_file(services, "beach.jpg", folder_id=folder["id"])
    keep = add_file(services, "keep.jpg", folder_id=folder["id"])
    store.trash([photo["id"]])
    store.trash([folder["id"]])
    store.restore([folder["id"]])
    assert names_in(store, folder["id"]) == ["keep.jpg"]
    assert [item["id"] for item in store.bin_items()] == [photo["id"]]
    assert store.get(keep["id"])["name"] == "keep.jpg"


def test_restoring_into_a_folder_that_is_gone_puts_it_at_the_top(services):
    store = services.store
    folder = store.create_folder(0, "trip")
    photo = add_file(services, "beach.jpg", folder_id=folder["id"])
    store.trash([photo["id"]])
    store.trash([folder["id"]])
    store.delete_forever([folder["id"]])
    restored = store.restore([photo["id"]])
    assert restored[0]["parentId"] == 0
    assert names_in(store) == ["beach.jpg"]


def test_restoring_next_to_a_new_item_with_the_same_name_numbers_it(services):
    store = services.store
    first = add_file(services, "a.txt")
    store.trash([first["id"]])
    add_file(services, "a.txt")
    assert store.restore([first["id"]])[0]["name"] == "a (2).txt"


def test_delete_forever_only_works_on_the_bin_and_removes_the_blob(services):
    store = services.store
    file = add_file(services, "gone.txt")
    blob = store.file_record(file["id"])["blob"]
    with pytest.raises(NotFound):
        store.delete_forever([file["id"]])
    store.trash([file["id"]])
    store.delete_forever([file["id"]])
    assert not services.blobs.path(blob).exists()


def test_the_bin_empties_itself_after_thirty_days(services, clock):
    store = services.store
    old = add_file(services, "old.txt")
    store.trash([old["id"]])
    clock.advance((BIN_DAYS - 1) * DAY_SECONDS)
    newer = add_file(services, "newer.txt")
    store.trash([newer["id"]])
    assert store.purge_expired() == 0
    clock.advance(2 * DAY_SECONDS)
    assert store.purge_expired() == 1
    assert [item["name"] for item in store.bin_items()] == ["newer.txt"]


def test_search_treats_percent_and_underscore_as_plain_text(services):
    add_file(services, "100% done.txt")
    add_file(services, "1000 done.txt")
    add_file(services, "a_b.txt")
    add_file(services, "axb.txt")
    assert [item["name"] for item in services.store.search("100%")] == ["100% done.txt"]
    assert [item["name"] for item in services.store.search("a_b")] == ["a_b.txt"]
    assert services.store.search("") == []


def test_starred_and_recent_lists(services, clock):
    store = services.store
    first = add_file(services, "first.txt")
    clock.advance(5)
    add_file(services, "second.txt")
    store.set_starred(first["id"], True)
    assert [item["name"] for item in store.starred()] == ["first.txt"]
    assert [item["name"] for item in store.recent()] == ["second.txt", "first.txt"]
    clock.advance(5)
    store.mark_opened(first["id"])
    assert [item["name"] for item in store.recent()] == ["first.txt", "second.txt"]
    with pytest.raises(DriveError):
        store.set_starred(first["id"], "yes")


def test_usage_counts_live_and_binned_bytes(services):
    store = services.store
    add_file(services, "a.txt", b"12345")
    gone = add_file(services, "b.txt", b"123")
    store.trash([gone["id"]])
    assert store.usage() == {"usedBytes": 5, "binBytes": 3, "fileCount": 1}


def test_folders_stop_at_the_depth_limit(store):
    parent = 0
    for level in range(MAX_FOLDER_DEPTH):
        parent = store.create_folder(parent, f"level{level}")["id"]
    with pytest.raises(DriveError):
        store.create_folder(parent, "too deep")


def test_is_within_walks_only_live_parents(services):
    store = services.store
    outer = store.create_folder(0, "outer")
    inner = store.create_folder(outer["id"], "inner")
    file = add_file(services, "x.txt", folder_id=inner["id"])
    other = add_file(services, "y.txt")
    assert store.is_within(file["id"], outer["id"])
    assert not store.is_within(other["id"], outer["id"])
    assert not store.is_within(outer["id"], inner["id"])
    store.trash([inner["id"]])
    assert not store.is_within(file["id"], outer["id"])


def test_batches_refuse_bad_ids(store):
    for bad in ([], "1", [True], [-1], [1.5], list(range(1, 1002)), [2 ** 63]):
        with pytest.raises(DriveError):
            store.trash(bad)


@pytest.mark.parametrize("name", ["Screenshot 2026-11-13 at 10.00.00 AM.png", "café menu.txt",
                                  "日本語　ファイル.txt",
                                  "family \U0001F468‍\U0001F469‍\U0001F467.jpg", "soft­hyphen.txt"])
def test_everyday_unicode_names_are_accepted(name):
    assert clean_name(name) == name


@pytest.mark.parametrize("bad", ["evil‮txt.exe", "a⁦b", "a⁩b", "half\ud800surrogate", "bell\x07"])
def test_direction_overrides_surrogates_and_controls_are_refused(bad):
    with pytest.raises(DriveError):
        clean_name(bad)


def chain(store, parent: int, levels: int, prefix: str) -> tuple[int, int]:
    top = current = store.create_folder(parent, f"{prefix}0")["id"]
    for level in range(1, levels):
        current = store.create_folder(current, f"{prefix}{level}")["id"]
    return top, current


def test_move_and_copy_cannot_build_folders_past_the_depth_limit(services):
    store = services.store
    _, deepest = chain(store, 0, MAX_FOLDER_DEPTH - 1, "a")
    two_deep, _ = chain(store, 0, 2, "b")
    with pytest.raises(DriveError, match="deep"):
        store.move([two_deep], deepest)
    with pytest.raises(DriveError, match="deep"):
        store.copy([two_deep], deepest)
    one_deep = store.create_folder(0, "single")["id"]
    store.move([one_deep], deepest)
    file = add_file(services, "leaf.txt", folder_id=one_deep)
    assert store.path(one_deep)[-1]["name"] == "single"
    assert store.get(file["id"])["parentId"] == one_deep


def test_renaming_a_file_works_out_its_type_again(services):
    from conftest import make_png
    store = services.store
    part = services.blobs.root / "incoming"
    part.write_bytes(make_png(4, 4))
    blob = services.blobs.adopt(part)
    node = store.add_file(0, "IMG_0001", 100, blob, "application/octet-stream")
    assert store.get(node["id"])["preview"] == "none"
    assert store.rename(node["id"], "IMG_0001.png")["preview"] == "image"
    assert store.file_record(node["id"])["mime"] == "image/png"
    assert store.rename(node["id"], "IMG_0001.bin")["preview"] == "none"


def test_usage_counts_hard_linked_copies_once(services):
    store = services.store
    original = add_file(services, "a.txt", b"12345")
    store.copy([original["id"]], 0)
    expected = 10 if services.blobs.copies_take_space() else 5
    assert store.usage()["usedBytes"] == expected
    assert store.usage()["fileCount"] == 2
    store.trash([original["id"]])
    assert store.usage()["usedBytes"] == 5
    assert store.usage()["binBytes"] == expected - 5


def test_ids_are_never_handed_out_again(services):
    store = services.store
    first = store.create_folder(0, "first")["id"]
    store.trash([first])
    store.delete_forever([first])
    assert store.create_folder(0, "second")["id"] > first
