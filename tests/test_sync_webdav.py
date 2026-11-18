import json
import logging
import os
import shutil
import sqlite3
import time

import pytest

from conftest import GIB, FakeClock
from drive.sync.http import Answer
from drive.sync.local import LocalEntry
from drive.sync.planner import BaseEntry, plan
from drive.sync.remote import RemoteRefused
from drive.sync.webdav import WebDavRemote, parse_multistatus
from sync_helpers import (LONGER, SHORTER, attach_sync, binned_drive_file_names, disk_files, drive_files, find_node,
                          preview, run, run_saying_yes_to_what_is_shown, running_dav, wait_until_idle)

REMOTE_FOLDER = "Phone"
LATER_SECONDS = 10
HOUR_SECONDS = 3600


@pytest.fixture
def cloud(tmp_path):
    with running_dav(tmp_path / "dav-root") as server:
        (server.root / REMOTE_FOLDER).mkdir()
        yield server


@pytest.fixture(params=[False, True], ids=["plain", "file ids"])
def either_cloud(request, tmp_path):
    with running_dav(tmp_path / "dav-either", file_ids=request.param) as server:
        (server.root / REMOTE_FOLDER).mkdir()
        yield server


@pytest.fixture
def syncing(services, tmp_path, cloud):
    owner, sync = attach_sync(services, tmp_path)
    return owner, sync


def remote_dir(cloud):
    return cloud.root / REMOTE_FOLDER


def write_remote(cloud, relative: str, content: bytes) -> None:
    target = remote_dir(cloud) / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    existed = target.exists()
    target.write_bytes(content)
    if existed:
        later = time.time() + LATER_SECONDS
        os.utime(target, (later, later))


def connect(owner, cloud, folder_name: str = "Synced", direction: str = "both", **extra) -> tuple[dict, dict]:
    folder = owner.folder(folder_name)
    body = {"name": f"{folder_name} with WebDAV", "provider": "webdav", "folderId": folder["id"],
            "remotePath": REMOTE_FOLDER, "direction": direction, "serverUrl": cloud.url,
            "username": cloud.username, "password": cloud.password, **extra}
    created = owner.post("/api/sync/connections", json=body)
    assert created.status_code == 201, created.get_json()
    return folder, created.get_json()


def first_sync(owner, connection: dict) -> dict:
    preview(owner, connection["id"])
    return run(owner, connection["id"])


def test_the_first_run_waits_for_a_preview_that_changes_nothing(syncing, cloud):
    owner, _ = syncing
    folder, connection = connect(owner, cloud)
    owner.upload("from-drive.txt", b"drive side", parent_id=folder["id"])
    write_remote(cloud, "from-cloud.txt", b"cloud side")
    assert owner.post(f"/api/sync/connections/{connection['id']}/run").status_code == 409
    shown = preview(owner, connection["id"])
    assert sorted((line["action"], line["path"]) for line in shown["actions"]) == [
        ("download", "from-cloud.txt"), ("upload", "from-drive.txt")]
    assert disk_files(remote_dir(cloud)) == {"from-cloud.txt": b"cloud side"}
    assert drive_files(owner, folder["id"]) == {"from-drive.txt": b"drive side"}
    view = run(owner, connection["id"])
    assert view["lastStatus"] == "ok", view["lastErrors"]
    assert view["lastCounts"]["uploaded"] == 1 and view["lastCounts"]["downloaded"] == 1
    expected = {"from-cloud.txt": b"cloud side", "from-drive.txt": b"drive side"}
    assert drive_files(owner, folder["id"]) == expected
    assert disk_files(remote_dir(cloud)) == expected


def test_a_first_run_keeps_one_copy_of_matching_files_and_both_of_different_ones(syncing, cloud):
    owner, _ = syncing
    folder, connection = connect(owner, cloud)
    owner.upload("same.txt", b"identical", parent_id=folder["id"])
    owner.upload("differs.txt", b"drive words", parent_id=folder["id"])
    write_remote(cloud, "same.txt", b"identical")
    write_remote(cloud, "differs.txt", b"cloud words!")
    view = first_sync(owner, connection)
    assert view["lastCounts"]["alreadyTheSame"] == 1 and view["lastCounts"]["conflicts"] == 1
    local, remote = drive_files(owner, folder["id"]), disk_files(remote_dir(cloud))
    assert local == remote
    assert local["same.txt"] == b"identical" and local["differs.txt"] == b"drive words"
    copies = [name for name in local if name.startswith("differs (conflict, cloud copy ")]
    assert len(copies) == 1 and local[copies[0]] == b"cloud words!"


def test_changes_flow_both_ways_and_replaced_drive_files_go_to_the_bin(syncing, cloud):
    owner, _ = syncing
    folder, connection = connect(owner, cloud)
    write_remote(cloud, "notes.txt", b"version one")
    first_sync(owner, connection)
    owner.upload("new-here.txt", b"made in Drive", parent_id=folder["id"])
    write_remote(cloud, "notes.txt", b"version two, longer")
    view = run(owner, connection["id"])
    assert view["lastStatus"] == "ok", view["lastErrors"]
    assert disk_files(remote_dir(cloud))["new-here.txt"] == b"made in Drive"
    assert drive_files(owner, folder["id"])["notes.txt"] == b"version two, longer"
    assert [item["name"] for item in owner.get("/api/bin").get_json()["items"]] == ["notes.txt"]


def test_deletes_go_to_the_other_sides_bin(syncing, cloud):
    owner, _ = syncing
    folder, connection = connect(owner, cloud)
    write_remote(cloud, "drop-here.txt", b"a")
    write_remote(cloud, "drop-there.txt", b"b")
    first_sync(owner, connection)
    owner.post("/api/nodes/trash", json={"ids": [find_node(owner, folder["id"], "drop-there.txt")["id"]]})
    (remote_dir(cloud) / "drop-here.txt").unlink()
    view = run(owner, connection["id"])
    assert view["lastCounts"]["binnedInCloud"] == 1 and view["lastCounts"]["binnedInDrive"] == 1
    assert drive_files(owner, folder["id"]) == {}
    assert disk_files(remote_dir(cloud)) == {}
    everything = disk_files(remote_dir(cloud), skip_bin=False)
    binned_in_cloud = [path for path in everything if path.endswith("drop-there.txt")]
    assert len(binned_in_cloud) == 1 and binned_in_cloud[0].startswith(".drive-sync-bin/")
    assert sorted(item["name"] for item in owner.get("/api/bin").get_json()["items"]) == ["drop-here.txt",
                                                                                          "drop-there.txt"]


def test_a_deleted_folder_goes_to_the_cloud_bin_in_one_piece(syncing, cloud):
    owner, _ = syncing
    folder, connection = connect(owner, cloud)
    write_remote(cloud, "Trip/day1.jpg", b"one")
    write_remote(cloud, "Trip/day2.jpg", b"two")
    first_sync(owner, connection)
    owner.post("/api/nodes/trash", json={"ids": [find_node(owner, folder["id"], "Trip")["id"]]})
    view = run(owner, connection["id"])
    assert view["lastCounts"]["binnedInCloud"] == 1, view
    assert disk_files(remote_dir(cloud)) == {}
    binned = disk_files(remote_dir(cloud), skip_bin=False)
    assert any(path.endswith("/Trip/day2.jpg") for path in binned)


def test_both_sides_changed_keeps_both_copies(syncing, cloud):
    owner, _ = syncing
    folder, connection = connect(owner, cloud)
    write_remote(cloud, "plan.txt", b"first")
    first_sync(owner, connection)
    owner.post("/api/nodes/trash", json={"ids": [find_node(owner, folder["id"], "plan.txt")["id"]]})
    owner.upload("plan.txt", b"drive edit", parent_id=folder["id"])
    write_remote(cloud, "plan.txt", b"cloud edit, longer")
    view = run(owner, connection["id"])
    assert view["lastCounts"]["conflicts"] == 1, view
    local, remote = drive_files(owner, folder["id"]), disk_files(remote_dir(cloud))
    assert local == remote
    assert local["plan.txt"] == b"drive edit"
    assert [content for name, content in local.items() if "conflict, cloud copy" in name] == [b"cloud edit, longer"]
    assert run(owner, connection["id"])["lastCounts"].get("conflicts") is None


def test_a_rename_in_drive_is_a_move_in_the_cloud(syncing, cloud):
    owner, _ = syncing
    folder, connection = connect(owner, cloud)
    write_remote(cloud, "draft.txt", b"words")
    write_remote(cloud, "Old name/inside.txt", b"inside")
    first_sync(owner, connection)
    owner.patch(f"/api/nodes/{find_node(owner, folder['id'], 'draft.txt')['id']}", json={"name": "final.txt"})
    owner.patch(f"/api/nodes/{find_node(owner, folder['id'], 'Old name')['id']}", json={"name": "New name"})
    view = run(owner, connection["id"])
    assert view["lastCounts"]["moved"] == 2, view
    assert disk_files(remote_dir(cloud)) == {"final.txt": b"words", "New name/": b"", "New name/inside.txt": b"inside"}
    binned = [path for path in disk_files(remote_dir(cloud), skip_bin=False) if path.startswith(".drive-sync-bin")]
    assert not [path for path in binned if path.endswith(("draft.txt", "inside.txt"))]
    assert run(owner, connection["id"])["lastCounts"].get("moved") is None


def test_a_rename_in_the_cloud_arrives_without_losing_anything(syncing, cloud):
    owner, _ = syncing
    folder, connection = connect(owner, cloud)
    write_remote(cloud, "draft.txt", b"words")
    first_sync(owner, connection)
    (remote_dir(cloud) / "draft.txt").rename(remote_dir(cloud) / "final.txt")
    run(owner, connection["id"])
    assert drive_files(owner, folder["id"]) == {"final.txt": b"words"}
    assert [item["name"] for item in owner.get("/api/bin").get_json()["items"]] == ["draft.txt"]


def test_a_dropped_connection_stops_cleanly_and_the_next_run_finishes(syncing, cloud, tmp_path):
    owner, _ = syncing
    folder, connection = connect(owner, cloud)
    for number in range(3):
        write_remote(cloud, f"photo-{number}.jpg", bytes([number]) * 2000)
    preview(owner, connection["id"])
    cloud.faults.drop_downloads_after = 1
    view = run(owner, connection["id"])
    assert view["lastStatus"] == "failed"
    assert "lost the connection" in view["lastErrors"][0]
    assert len(drive_files(owner, folder["id"])) == 1
    assert list((tmp_path / "sync-work").rglob("incoming-*")) == []
    cloud.faults.heal()
    view = run(owner, connection["id"])
    assert view["lastStatus"] == "ok", view["lastErrors"]
    assert drive_files(owner, folder["id"]) == disk_files(remote_dir(cloud))
    assert owner.get("/api/bin").get_json()["items"] == []


@pytest.mark.parametrize("change", [SHORTER, LONGER])
def test_a_download_without_a_length_that_differs_from_the_listing_is_not_kept(syncing, cloud, change, tmp_path):
    owner, _ = syncing
    folder, connection = connect(owner, cloud)
    write_remote(cloud, "one.bin", b"1" * 2000)
    write_remote(cloud, "two.bin", b"2" * 2000)
    preview(owner, connection["id"])
    cloud.faults.download_change = change
    view = run(owner, connection["id"])
    assert view["lastStatus"] == "errors" and view["lastCounts"]["errors"] == 2, view
    assert view["lastCounts"].get("downloaded") is None
    assert drive_files(owner, folder["id"]) == {}
    assert list((tmp_path / "sync-work").rglob("incoming-*")) == []
    cloud.faults.heal()
    view = run(owner, connection["id"])
    assert view["lastStatus"] == "ok" and view["lastCounts"]["downloaded"] == 2, view
    assert drive_files(owner, folder["id"]) == disk_files(remote_dir(cloud))


def bin_names(owner) -> list[str]:
    return sorted(item["name"] for item in owner.get("/api/bin").get_json()["items"])


@pytest.mark.parametrize("direction", ["both", "upload"])
def test_a_renamed_cloud_folder_stops_the_run_instead_of_binning_drive(syncing, cloud, direction):
    owner, _ = syncing
    folder, connection = connect(owner, cloud, direction=direction)
    for number in range(5):
        owner.upload(f"photo-{number}.jpg", bytes([number]) * 100, parent_id=folder["id"])
    first_sync(owner, connection)
    remote_dir(cloud).rename(cloud.root / "Phone renamed")
    shown = preview(owner, connection["id"])
    assert "renamed" in shown["stopped"]
    view = run(owner, connection["id"])
    assert view["lastStatus"] == "failed" and "renamed" in view["lastErrors"][0], view
    assert len(drive_files(owner, folder["id"])) == 5 and bin_names(owner) == []
    assert sorted(path.name for path in cloud.root.iterdir()) == ["Phone renamed"]


def approve(owner, connection_id: int, approval) -> object:
    return owner.post(f"/api/sync/connections/{connection_id}/run", json={"approveBins": approval})


def shown_bins(held: dict) -> list[tuple[str, str]]:
    return sorted((line["side"], line["path"]) for line in held["files"])


def test_a_run_that_would_bin_most_files_waits_for_a_yes_to_the_files_shown(syncing, cloud):
    owner, _ = syncing
    folder, connection = connect(owner, cloud)
    for number in range(6):
        write_remote(cloud, f"photo-{number}.jpg", bytes([number]) * 100)
    first_sync(owner, connection)
    for number in range(5):
        (remote_dir(cloud) / f"photo-{number}.jpg").unlink()
    expected = [("drive", f"photo-{number}.jpg") for number in range(5)]
    shown = preview(owner, connection["id"])
    assert shown["binsHeld"] == 5 and shown["stopped"] is None
    assert sorted((line["side"], line["path"]) for line in shown["binFiles"]) == expected
    view = run(owner, connection["id"])
    assert view["lastStatus"] == "held" and view["lastCounts"]["binsHeld"] == 5, view
    assert "5 of the 6" in view["lastErrors"][0]
    assert view["held"]["bins"] == 5 and shown_bins(view["held"]) == expected
    assert view["held"]["approval"] == shown["binApproval"]
    assert len(drive_files(owner, folder["id"])) == 6 and bin_names(owner) == []
    for refused in ("lots", -1, True, 5, "a" * 63, "g" * 64, ["x"]):
        assert approve(owner, connection["id"], refused).status_code == 400, refused
    agreed = approve(owner, connection["id"], view["held"]["approval"])
    assert agreed.status_code == 202, agreed.get_json()
    view = wait_until_idle(owner, connection["id"])
    assert view["lastStatus"] == "ok" and view["lastCounts"]["binnedInDrive"] == 5, view
    assert view["held"] is None
    assert drive_files(owner, folder["id"]) == {"photo-5.jpg": bytes([5]) * 100}


def test_a_count_alone_is_no_longer_a_yes(syncing, cloud):
    owner, _ = syncing
    folder, connection = connect(owner, cloud)
    for number in range(6):
        write_remote(cloud, f"photo-{number}.jpg", bytes([number]) * 100)
    first_sync(owner, connection)
    for number in range(5):
        (remote_dir(cloud) / f"photo-{number}.jpg").unlink()
    assert run(owner, connection["id"])["lastCounts"]["binsHeld"] == 5
    for count in (5, 6):
        answer = owner.post(f"/api/sync/connections/{connection['id']}/run", json={"allowBins": count})
        assert answer.status_code == 400 and "approveBins" in answer.get_json()["error"], answer.get_json()
    assert len(drive_files(owner, folder["id"])) == 6 and bin_names(owner) == []


def test_a_yes_to_forty_files_never_bins_a_different_forty(syncing, cloud):
    owner, _ = syncing
    folder, connection = connect(owner, cloud)
    for number in range(70):
        write_remote(cloud, f"photo-{number:02}.jpg", bytes([number]) * 100)
    first_sync(owner, connection)
    for number in range(40):
        (remote_dir(cloud) / f"photo-{number:02}.jpg").unlink()
    first_view = run(owner, connection["id"])
    first_set = first_view["held"]
    assert first_view["lastStatus"] == "held" and first_set["bins"] == 40, first_view
    for number in range(40):
        write_remote(cloud, f"photo-{number:02}.jpg", bytes([number]) * 100)
    for number in range(30, 70):
        (remote_dir(cloud) / f"photo-{number:02}.jpg").unlink()
    assert approve(owner, connection["id"], first_set["approval"]).status_code == 202
    view = wait_until_idle(owner, connection["id"])
    assert view["lastStatus"] == "held" and view["held"]["bins"] == 40, view
    assert shown_bins(view["held"]) == [("drive", f"photo-{number:02}.jpg") for number in range(30, 70)]
    assert view["held"]["approval"] != first_set["approval"]
    assert len(drive_files(owner, folder["id"])) == 70 and bin_names(owner) == []
    stale = approve(owner, connection["id"], first_set["approval"])
    assert stale.status_code == 409 and "changed" in stale.get_json()["error"], stale.get_json()
    assert approve(owner, connection["id"], view["held"]["approval"]).status_code == 202
    view = wait_until_idle(owner, connection["id"])
    assert view["lastStatus"] == "ok" and view["lastCounts"]["binnedInDrive"] == 40, view
    assert sorted(drive_files(owner, folder["id"])) == [f"photo-{number:02}.jpg" for number in range(30)]


def held_after_yes(owner, connection_id: int, approval: str) -> dict:
    assert approve(owner, connection_id, approval).status_code == 202
    view = wait_until_idle(owner, connection_id)
    assert view["lastStatus"] == "held", view
    return view["held"]


def test_one_file_more_fewer_or_different_than_the_yes_keeps_the_run_held(syncing, cloud):
    owner, _ = syncing
    folder, connection = connect(owner, cloud)
    for number in range(10):
        write_remote(cloud, f"photo-{number}.jpg", bytes([number]) * 100)
    first_sync(owner, connection)
    for number in range(6):
        (remote_dir(cloud) / f"photo-{number}.jpg").unlink()
    six = run(owner, connection["id"])["held"]
    (remote_dir(cloud) / "photo-6.jpg").unlink()
    seven = held_after_yes(owner, connection["id"], six["approval"])
    assert seven["bins"] == 7
    write_remote(cloud, "photo-6.jpg", bytes([6]) * 100)
    back_to_six = held_after_yes(owner, connection["id"], seven["approval"])
    assert back_to_six["bins"] == 6 and back_to_six["approval"] == six["approval"]
    write_remote(cloud, "photo-0.jpg", bytes([0]) * 100)
    (remote_dir(cloud) / "photo-7.jpg").unlink()
    swapped = held_after_yes(owner, connection["id"], back_to_six["approval"])
    assert swapped["bins"] == 6 and swapped["approval"] != six["approval"]
    assert ("drive", "photo-7.jpg") in shown_bins(swapped) and ("drive", "photo-0.jpg") not in shown_bins(swapped)
    assert len(drive_files(owner, folder["id"])) == 10 and bin_names(owner) == []


def test_a_brake_that_trips_after_renames_says_the_renames_were_done(syncing, cloud, monkeypatch):
    from drive.sync.engine import BinsHeld, SyncEngine
    owner, _ = syncing
    folder, connection = connect(owner, cloud)
    write_remote(cloud, "draft.txt", b"words")
    first_sync(owner, connection)
    owner.patch(f"/api/nodes/{find_node(owner, folder['id'], 'draft.txt')['id']}", json={"name": "final.txt"})
    looks = iter([None, BinsHeld([("drive", f"photo-{number}.jpg", str(number)) for number in range(5)], 6)])
    monkeypatch.setattr(SyncEngine, "_bins_beyond_the_brake", lambda self, actions: next(looks))
    view = run(owner, connection["id"])
    assert view["lastStatus"] == "held", view
    assert "nothing was changed" not in view["lastErrors"][0] and "1 rename" in view["lastErrors"][0], view
    assert disk_files(remote_dir(cloud)) == {"final.txt": b"words"}


def album_with_a_rename_still_to_be_done(owner, cloud, root_files: int, deleted_root_files: int) -> tuple[dict, dict]:
    folder, connection = connect(owner, cloud)
    for number in range(4):
        write_remote(cloud, f"Album/Sub/in-sub-{number}.jpg", bytes([number]) * 50)
    for number in range(root_files):
        write_remote(cloud, f"root-{number}.jpg", bytes([number]) * 60)
    first_sync(owner, connection)
    owner.patch(f"/api/nodes/{find_node(owner, folder['id'], 'Album')['id']}", json={"name": "Album 2024"})
    shutil.rmtree(remote_dir(cloud) / "Album" / "Sub")
    for number in range(deleted_root_files):
        (remote_dir(cloud) / f"root-{number}.jpg").unlink()
    return folder, connection


def refuse_cloud_moves(monkeypatch) -> None:
    def refused(self, entry, old_path, new_path):
        raise RemoteRefused("WebDAV answered 423 to moving it, as a locked folder would", 423)

    monkeypatch.setattr(WebDavRemote, "move", refused)


IN_SUB = [("drive", f"Album 2024/Sub/in-sub-{number}.jpg") for number in range(4)]


def test_a_folder_bin_under_a_rename_still_to_be_done_is_held_and_listed(syncing, cloud, monkeypatch):
    owner, _ = syncing
    folder, connection = album_with_a_rename_still_to_be_done(owner, cloud, root_files=1, deleted_root_files=0)
    shown = preview(owner, connection["id"])
    assert shown["binsHeld"] == 4, shown
    assert sorted((line["side"], line["path"]) for line in shown["binFiles"]) == IN_SUB
    refuse_cloud_moves(monkeypatch)
    view = run(owner, connection["id"])
    assert view["lastStatus"] == "held" and shown_bins(view["held"]) == IN_SUB, view
    assert bin_names(owner) == []


def test_a_yes_never_bins_a_file_outside_the_list_when_a_cloud_move_fails(syncing, cloud, monkeypatch):
    owner, _ = syncing
    folder, connection = album_with_a_rename_still_to_be_done(owner, cloud, root_files=10, deleted_root_files=8)
    shown = preview(owner, connection["id"])
    listed = sorted((line["side"], line["path"]) for line in shown["binFiles"])
    refuse_cloud_moves(monkeypatch)
    assert approve(owner, connection["id"], shown["binApproval"]).status_code == 202
    wait_until_idle(owner, connection["id"])
    names = bin_names(owner)
    binned = [("drive", name) for name in names if name != "Sub"] + (IN_SUB if "Sub" in names else [])
    assert sorted(set(binned) - set(listed)) == [], listed
    assert listed == sorted(IN_SUB + [("drive", f"root-{number}.jpg") for number in range(8)]), listed
    assert sorted(binned) == listed


def all_held_pages(owner, connection_id: int, held: dict) -> list[tuple[str, str]]:
    lines = [(line["side"], line["path"]) for line in held["files"]]
    start = len(lines)
    while start is not None:
        answer = owner.get(f"/api/sync/connections/{connection_id}/held?from={start}")
        assert answer.status_code == 200, answer.get_json()
        page = answer.get_json()
        assert page["approval"] == held["approval"], page
        lines += [(line["side"], line["path"]) for line in page["files"]]
        start = page["next"]
    return lines


def test_every_file_a_yes_covers_can_be_listed_past_the_first_page(syncing, cloud):
    owner, _ = syncing
    folder, connection = connect(owner, cloud)
    total, deleted = 530, 510
    for number in range(total):
        write_remote(cloud, f"photo-{number:03}.jpg", b"x" * 10)
    first_sync(owner, connection)
    for number in range(deleted):
        (remote_dir(cloud) / f"photo-{number:03}.jpg").unlink()
    held = run(owner, connection["id"])["held"]
    assert held["bins"] == deleted and len(held["files"]) == 500, held["bins"]
    listed = all_held_pages(owner, connection["id"], held)
    assert sorted(listed) == [("drive", f"photo-{number:03}.jpg") for number in range(deleted)]
    assert owner.get(f"/api/sync/connections/{connection['id']}/held?from=-1").status_code == 400
    assert approve(owner, connection["id"], held["approval"]).status_code == 202
    assert wait_until_idle(owner, connection["id"])["lastCounts"].get("binnedInDrive") == deleted
    assert sorted(("drive", name) for name in bin_names(owner)) == sorted(listed)


def held_long_list(owner, cloud, total: int, deleted: int) -> tuple[dict, dict]:
    folder, connection = connect(owner, cloud)
    for number in range(total):
        write_remote(cloud, f"photo-{number:03}.jpg", b"x" * 10)
    first_sync(owner, connection)
    for number in range(deleted):
        (remote_dir(cloud) / f"photo-{number:03}.jpg").unlink()
    return connection, run(owner, connection["id"])["held"]


def stored_card_list(sync, connection_id: int) -> list:
    with sync.database.connect() as connection:
        row = connection.execute("SELECT last_held FROM sync_connections WHERE id = ?", (connection_id,)).fetchone()
    return json.loads(row["last_held"])["files"]


def test_the_card_reads_only_the_first_page_of_a_long_held_list(syncing, cloud):
    owner, sync = syncing
    connection, held = held_long_list(owner, cloud, 530, 510)
    assert len(stored_card_list(sync, connection["id"])) == 500
    assert len(all_held_pages(owner, connection["id"], held)) == 510


def test_a_held_list_cut_short_by_an_older_version_says_to_sync_again(syncing, cloud):
    owner, sync = syncing
    connection, held = held_long_list(owner, cloud, 530, 510)
    older = {"bins": held["bins"], "approval": held["approval"], "files": held["files"]}
    with sync.database.connect() as database:
        database.execute("UPDATE sync_connections SET last_held = ?, last_held_files = NULL WHERE id = ?",
                         (json.dumps(older), connection["id"]))
    held_address = f"/api/sync/connections/{connection['id']}/held"
    assert len(owner.get(f"{held_address}?from=0").get_json()["files"]) == 500
    answer = owner.get(f"{held_address}?from=500")
    assert answer.status_code == 409, answer.get_json()
    assert "Sync now" in answer.get_json()["error"]


@pytest.mark.parametrize("start", ["%C2%B2", "%D9%A3", "%EF%BC%91", "9" * 5000, "9" * 10, "-1", "+1", "1e3", "0x1",
                                   "%201", "1%20", ""], ids=lambda start: start[:12])
def test_the_held_list_answers_400_to_a_from_that_is_not_a_plain_number_in_range(syncing, cloud, start):
    owner, _ = syncing
    folder, connection = connect(owner, cloud)
    for number in range(6):
        write_remote(cloud, f"photo-{number}.jpg", bytes([number]) * 100)
    first_sync(owner, connection)
    for number in range(5):
        (remote_dir(cloud) / f"photo-{number}.jpg").unlink()
    assert run(owner, connection["id"])["lastStatus"] == "held"
    held_address = f"/api/sync/connections/{connection['id']}/held"
    assert len(owner.get(f"{held_address}?from=3").get_json()["files"]) == 2
    answer = owner.get(f"{held_address}?from={start}")
    assert answer.status_code == 400, answer.get_json()
    assert "whole number" in answer.get_json()["error"]


def test_a_run_that_fails_for_another_reason_keeps_the_waiting_yes(syncing, cloud):
    owner, _ = syncing
    folder, connection = connect(owner, cloud)
    for number in range(6):
        write_remote(cloud, f"photo-{number}.jpg", bytes([number]) * 100)
    first_sync(owner, connection)
    for number in range(5):
        (remote_dir(cloud) / f"photo-{number}.jpg").unlink()
    held = run(owner, connection["id"])["held"]
    cloud.faults.down = True
    assert run(owner, connection["id"])["lastStatus"] == "failed"
    cloud.faults.heal()
    assert approve(owner, connection["id"], held["approval"]).status_code == 202
    assert wait_until_idle(owner, connection["id"])["lastCounts"].get("binnedInDrive") == 5


def test_a_drive_folder_that_gains_a_file_while_the_cloud_is_listed_is_not_binned(syncing, cloud, monkeypatch):
    owner, _ = syncing
    folder, connection = connect(owner, cloud)
    for number in range(5):
        write_remote(cloud, f"Docs/doc-{number}.txt", bytes([65 + number]) * 20)
    write_remote(cloud, "keep.txt", b"k")
    first_sync(owner, connection)
    shutil.rmtree(remote_dir(cloud) / "Docs")
    held = run(owner, connection["id"])["held"]
    docs = find_node(owner, folder["id"], "Docs")
    listing = WebDavRemote.list_tree

    def listing_while_drive_gains_a_file(self, known=None):
        owner.upload("written-meanwhile.txt", b"never synced, never listed", parent_id=docs["id"])
        return listing(self, known)

    monkeypatch.setattr(WebDavRemote, "list_tree", listing_while_drive_gains_a_file)
    assert approve(owner, connection["id"], held["approval"]).status_code == 202
    view = wait_until_idle(owner, connection["id"])
    assert view["lastCounts"].get("binnedInDrive") is None, view
    assert any(line.startswith("'Docs'") for line in view["lastSkipped"]), view["lastSkipped"]
    assert bin_names(owner) == []
    assert drive_files(owner, folder["id"])["Docs/written-meanwhile.txt"] == b"never synced, never listed"


@pytest.mark.parametrize("docs", [2, 6], ids=["under the brake", "over the brake"])
def test_a_file_moved_in_drive_into_a_folder_deleted_in_the_cloud_ends_in_neither_bin(services, syncing, cloud, docs):
    owner, _ = syncing
    folder, connection = connect(owner, cloud)
    for number in range(docs):
        write_remote(cloud, f"Docs/doc-{number}.txt", bytes([65 + number]) * 20)
    for number in range(2):
        write_remote(cloud, f"keep-{number}.txt", b"k" * (number + 1))
    first_sync(owner, connection)
    shutil.rmtree(remote_dir(cloud) / "Docs")
    mover, docs_folder = find_node(owner, folder["id"], "keep-0.txt"), find_node(owner, folder["id"], "Docs")
    moved = owner.post("/api/nodes/move", json={"ids": [mover["id"]], "targetId": docs_folder["id"]})
    assert moved.status_code == 200, moved.get_json()
    doc_names = [f"doc-{number}.txt" for number in range(docs)]
    shown = preview(owner, connection["id"])
    if shown["binsHeld"]:
        expected = [("drive", f"Docs/{name}") for name in doc_names]
        assert sorted((line["side"], line["path"]) for line in shown["binFiles"]) == expected
        assert approve(owner, connection["id"], shown["binApproval"]).status_code == 202
        first = wait_until_idle(owner, connection["id"])
    else:
        first = run(owner, connection["id"])
    assert first["lastErrors"] in ([], ["'keep-0.txt': WebDAV answered 409 to moving 'keep-0.txt'"]), first
    assert first["lastCounts"].get("binnedInDrive") == docs, first
    for _ in range(2):
        view = run(owner, connection["id"])
        assert view["lastStatus"] == "ok" and not view["lastErrors"], view
    assert drive_files(owner, folder["id"])["Docs/keep-0.txt"] == b"k"
    assert disk_files(remote_dir(cloud))["Docs/keep-0.txt"] == b"k"
    assert binned_drive_file_names(services.store) == doc_names
    assert not [path for path in disk_files(remote_dir(cloud), skip_bin=False)
                if path.startswith(".drive-sync-bin") and path.endswith("keep-0.txt")]


def cloud_bin_files(cloud) -> list[str]:
    return sorted(path.split("/", 2)[2] for path in disk_files(remote_dir(cloud), skip_bin=False)
                  if path.startswith(".drive-sync-bin") and not path.endswith("/"))


@pytest.mark.parametrize("others", [2, 6], ids=["under the brake", "over the brake"])
def test_a_file_moved_out_of_a_folder_is_not_binned_with_it_when_its_cloud_move_fails(syncing, cloud, monkeypatch,
                                                                                     others):
    owner, _ = syncing
    folder, connection = connect(owner, cloud)
    write_remote(cloud, "G/x.txt", b"moved out, never deleted")
    other_names = [f"g-{number}.txt" for number in range(others)]
    for number, name in enumerate(other_names):
        write_remote(cloud, f"G/{name}", bytes([65 + number]) * 20)
    for number in range(2):
        write_remote(cloud, f"keep-{number}.txt", b"k" * (number + 1))
    first_sync(owner, connection)
    g_folder = find_node(owner, folder["id"], "G")
    mover = find_node(owner, g_folder["id"], "x.txt")
    assert owner.post("/api/nodes/move", json={"ids": [mover["id"]], "targetId": folder["id"]}).status_code == 200
    owner.post("/api/nodes/trash", json={"ids": [g_folder["id"]]})

    def refused(self, entry, old_path, new_path):
        raise RemoteRefused("WebDAV answered 423 to moving it, as a locked file would", 423)

    monkeypatch.setattr(WebDavRemote, "move", refused)
    shown = preview(owner, connection["id"])
    if shown["binsHeld"]:
        expected = [("cloud", f"G/{name}") for name in other_names]
        assert sorted((line["side"], line["path"]) for line in shown["binFiles"]) == expected
        assert approve(owner, connection["id"], shown["binApproval"]).status_code == 202
        first = wait_until_idle(owner, connection["id"])
    else:
        first = run(owner, connection["id"])
    assert "x.txt" not in [name.rpartition("/")[2] for name in cloud_bin_files(cloud)], first
    assert disk_files(remote_dir(cloud))["G/x.txt"] == b"moved out, never deleted"
    monkeypatch.undo()
    for _ in range(2):
        view = run_saying_yes_to_what_is_shown(owner, connection["id"])
    assert view["lastStatus"] == "ok" and not view["lastErrors"], view
    assert cloud_bin_files(cloud) == [f"G/{name}" for name in other_names]
    assert disk_files(remote_dir(cloud)) == {"x.txt": b"moved out, never deleted", "keep-0.txt": b"k",
                                             "keep-1.txt": b"kk"}
    assert drive_files(owner, folder["id"]) == disk_files(remote_dir(cloud))


@pytest.mark.parametrize("change", ["a new file", "an edited file", "a new subfolder"])
def test_a_cloud_folder_that_changes_after_it_was_listed_is_not_binned(syncing, cloud, monkeypatch, change):
    owner, _ = syncing
    folder, connection = connect(owner, cloud)
    for number in range(2):
        write_remote(cloud, f"Docs/doc-{number}.txt", bytes([65 + number]) * 20)
    for number in range(6):
        write_remote(cloud, f"keep-{number}.txt", b"k" * (number + 1))
    first_sync(owner, connection)
    owner.post("/api/nodes/trash", json={"ids": [find_node(owner, folder["id"], "Docs")["id"]]})
    listing, listings = WebDavRemote.list_tree, []

    def listing_then_the_cloud_folder_changes(self, *arguments, **named):
        found = listing(self, *arguments, **named)
        listings.append(found)
        if len(listings) == 1 and change == "a new file":
            write_remote(cloud, "Docs/written-meanwhile.txt", b"never synced, never listed")
        elif len(listings) == 1 and change == "an edited file":
            write_remote(cloud, "Docs/doc-0.txt", b"edited after the listing")
        elif len(listings) == 1:
            (remote_dir(cloud) / "Docs" / "Later").mkdir()
        return found

    monkeypatch.setattr(WebDavRemote, "list_tree", listing_then_the_cloud_folder_changes)
    view = run(owner, connection["id"])
    monkeypatch.undo()
    assert view["lastStatus"] == "ok" and view["lastCounts"].get("binnedInCloud") is None, view
    assert any(line.startswith("'Docs'") and "changed in the cloud" in line for line in view["lastSkipped"]), view
    assert not [path for path in disk_files(remote_dir(cloud), skip_bin=False) if path.startswith(".drive-sync-bin")]
    assert "Docs/doc-1.txt" in disk_files(remote_dir(cloud))


def test_a_few_deletes_pass_the_bin_brake(syncing, cloud):
    owner, _ = syncing
    folder, connection = connect(owner, cloud)
    for number in range(3):
        write_remote(cloud, f"photo-{number}.jpg", bytes([number]) * 100)
    first_sync(owner, connection)
    for number in range(3):
        (remote_dir(cloud) / f"photo-{number}.jpg").unlink()
    view = run(owner, connection["id"])
    assert view["lastStatus"] == "ok" and view["lastCounts"]["binnedInDrive"] == 3, view


def test_a_case_twin_arriving_in_the_cloud_never_bins_the_synced_file(syncing, cloud):
    owner, _ = syncing
    folder, connection = connect(owner, cloud)
    write_remote(cloud, "notes.txt", b"the synced one")
    first_sync(owner, connection)
    write_remote(cloud, "NOTES.txt", b"a newcomer")
    for _ in range(3):
        view = run(owner, connection["id"])
        assert view["lastStatus"] == "ok" and view["lastCounts"]["skipped"] == 1, view
        assert "NOTES.txt" in view["lastSkipped"][0]
    assert drive_files(owner, folder["id"]) == {"notes.txt": b"the synced one"}
    assert bin_names(owner) == []
    assert disk_files(remote_dir(cloud)) == {"NOTES.txt": b"a newcomer", "notes.txt": b"the synced one"}


def test_names_that_differ_only_in_case_across_the_sides_are_skipped_not_doubled(syncing, cloud):
    owner, _ = syncing
    folder, connection = connect(owner, cloud)
    owner.upload("report.txt", b"drive words", parent_id=folder["id"])
    write_remote(cloud, "Report.txt", b"cloud words!")
    for _ in range(3):
        view = first_sync(owner, connection)
        assert view["lastStatus"] == "ok" and view["lastCounts"]["skipped"] == 2, view
    assert drive_files(owner, folder["id"]) == {"report.txt": b"drive words"}
    assert disk_files(remote_dir(cloud)) == {"Report.txt": b"cloud words!"}
    assert bin_names(owner) == []


def test_a_case_only_rename_in_the_cloud_arrives_without_a_numbered_copy(syncing, cloud):
    owner, _ = syncing
    folder, connection = connect(owner, cloud)
    write_remote(cloud, "notes.txt", b"words")
    first_sync(owner, connection)
    (remote_dir(cloud) / "notes.txt").rename(remote_dir(cloud) / "NOTES.txt")
    for _ in range(2):
        run(owner, connection["id"])
    assert drive_files(owner, folder["id"]) == {"NOTES.txt": b"words"}
    assert bin_names(owner) == ["notes.txt"]


def test_a_cloud_folder_name_with_outer_spaces_is_skipped_not_fatal(syncing, cloud):
    owner, _ = syncing
    folder, connection = connect(owner, cloud)
    write_remote(cloud, "ok.txt", b"fine")
    write_remote(cloud, "Holiday /beach.jpg", b"sand")
    shown = preview(owner, connection["id"])
    assert shown["stopped"] is None, shown
    view = run(owner, connection["id"])
    assert view["lastStatus"] == "ok", view
    assert drive_files(owner, folder["id"]) == {"ok.txt": b"fine"}
    assert any("Holiday" in line and "spaces" in line for line in view["lastSkipped"])
    assert run(owner, connection["id"])["lastStatus"] == "ok"
    assert (remote_dir(cloud) / "Holiday " / "beach.jpg").read_bytes() == b"sand"


def test_a_synced_cloud_folder_renamed_to_a_name_drive_rewrites_is_never_binned(services, tmp_path, either_cloud):
    owner, _ = attach_sync(services, tmp_path)
    folder, connection = connect(owner, either_cloud)
    for number in range(2):
        write_remote(either_cloud, f"Trip/photo-{number}.jpg", bytes([number]) * 20)
    first_sync(owner, connection)
    (remote_dir(either_cloud) / "Trip").rename(remote_dir(either_cloud) / "Holiday ")
    for _ in range(2):
        view = run(owner, connection["id"])
        assert view["lastStatus"] == "ok" and view["lastCounts"].get("binnedInDrive") is None, view
        assert any("Holiday" in line for line in view["lastSkipped"]), view
    assert sorted(drive_files(owner, folder["id"])) == ["Trip/", "Trip/photo-0.jpg", "Trip/photo-1.jpg"]
    assert bin_names(owner) == []
    assert sorted(path.name for path in (remote_dir(either_cloud) / "Holiday ").iterdir()) == ["photo-0.jpg",
                                                                                              "photo-1.jpg"]


def test_a_synced_file_renamed_to_a_name_drive_refuses_is_never_binned(services, tmp_path, either_cloud):
    owner, _ = attach_sync(services, tmp_path)
    folder, connection = connect(owner, either_cloud)
    write_remote(either_cloud, "notes.txt", b"words")
    write_remote(either_cloud, "other.txt", b"more words")
    first_sync(owner, connection)
    (remote_dir(either_cloud) / "notes.txt").rename(remote_dir(either_cloud) / "no\\tes.txt")
    view = run(owner, connection["id"])
    assert view["lastStatus"] == "ok" and view["lastCounts"].get("binnedInDrive") is None, view
    assert drive_files(owner, folder["id"]) == {"notes.txt": b"words", "other.txt": b"more words"}
    assert bin_names(owner) == []
    assert sorted(path.name for path in remote_dir(either_cloud).iterdir()) == ["no\\tes.txt", "other.txt"]


def test_binning_a_drive_folder_leaves_cloud_items_drive_never_held(services, tmp_path, either_cloud):
    owner, _ = attach_sync(services, tmp_path)
    folder, connection = connect(owner, either_cloud)
    write_remote(either_cloud, "Trip/a.jpg", b"synced")
    for number in range(2):
        write_remote(either_cloud, f"Trip/Raw /keep-{number}.dng", b"never synced")
    view = first_sync(owner, connection)
    assert drive_files(owner, folder["id"]) == {"Trip/": b"", "Trip/a.jpg": b"synced"}, view
    owner.post("/api/nodes/trash", json={"ids": [find_node(owner, folder["id"], "Trip")["id"]]})
    view = run(owner, connection["id"])
    assert view["lastStatus"] == "ok", view
    trip = remote_dir(either_cloud) / "Trip"
    assert not (trip / "a.jpg").exists()
    assert sorted(path.name for path in (trip / "Raw ").iterdir()) == ["keep-0.dng", "keep-1.dng"]
    assert run(owner, connection["id"])["lastStatus"] == "ok"
    assert sorted(path.name for path in (trip / "Raw ").iterdir()) == ["keep-0.dng", "keep-1.dng"]


@pytest.mark.parametrize("direction", ["both", "upload", "download"])
def test_a_skipped_name_without_an_id_holds_only_the_bins_it_could_explain(syncing, cloud, direction):
    owner, _ = syncing
    folder, connection = connect(owner, cloud, direction=direction)
    contents = {"a.txt": b"aaa", "b.txt": b"bbbbb", "Docs/c.txt": b"ccccccc"}
    if direction == "upload":
        docs = owner.folder("Docs", folder["id"])
        owner.folder("Other", folder["id"])
        for name, content in contents.items():
            owner.upload(name.rpartition("/")[2], content, parent_id=docs["id"] if "/" in name else folder["id"])
    else:
        (remote_dir(cloud) / "Other").mkdir()
        for name, content in contents.items():
            write_remote(cloud, name, content)
    first_sync(owner, connection)
    (remote_dir(cloud) / "Other" / "odd\\name.txt").write_bytes(b"xyz")
    for name in contents:
        (remote_dir(cloud) / name).unlink()
    (remote_dir(cloud) / "Docs").rmdir()
    view = run(owner, connection["id"])
    assert view["lastStatus"] == "ok", view
    held = [line for line in view["lastSkipped"] if line.startswith("'a.txt'")]
    if direction == "upload":
        assert held == [], view["lastSkipped"]
        assert disk_files(remote_dir(cloud)) == {**contents, "Docs/": b"", "Other/": b"",
                                                 "Other/odd\\name.txt": b"xyz"}
        return
    assert len(held) == 1 and "kept in Drive" in held[0], view["lastSkipped"]
    assert drive_files(owner, folder["id"]) == {"a.txt": b"aaa", "Other/": b""}
    assert sorted(bin_names(owner)) == ["Docs", "b.txt"]
    if direction == "both":
        owner.post("/api/nodes/trash", json={"ids": [find_node(owner, folder["id"], "a.txt")["id"]]})
        owner.upload("a.txt", b"edited in Drive", parent_id=folder["id"])
        assert run(owner, connection["id"])["lastStatus"] == "ok"
        assert (remote_dir(cloud) / "a.txt").read_bytes() == b"edited in Drive"


def test_a_file_renamed_in_place_to_a_refused_name_and_edited_is_never_binned(syncing, cloud):
    owner, _ = syncing
    folder, connection = connect(owner, cloud)
    write_remote(cloud, "report.txt", b"first draft")
    write_remote(cloud, "other.txt", b"stays")
    first_sync(owner, connection)
    (remote_dir(cloud) / "report.txt").unlink()
    (remote_dir(cloud) / "re\\port.txt").write_bytes(b"first draft, now with a second paragraph")
    view = run(owner, connection["id"])
    assert view["lastStatus"] == "ok" and view["lastCounts"].get("binnedInDrive") is None, view
    assert any(line.startswith("'report.txt'") and "kept in Drive" in line for line in view["lastSkipped"]), view
    assert drive_files(owner, folder["id"]) == {"other.txt": b"stays", "report.txt": b"first draft"}
    assert bin_names(owner) == []


def test_an_empty_skipped_file_holds_nothing_it_could_not_be(syncing, cloud):
    owner, _ = syncing
    folder, connection = connect(owner, cloud)
    for name, content in {"a.txt": b"aaa", "b.txt": b"bbbbb", "Deep/c.txt": b"ccccccc", "keep.txt": b"k"}.items():
        write_remote(cloud, name, content)
    (remote_dir(cloud) / "Other").mkdir()
    first_sync(owner, connection)
    (remote_dir(cloud) / "Other" / "lock\\file").write_bytes(b"")
    for name in ["a.txt", "b.txt", "Deep/c.txt"]:
        (remote_dir(cloud) / name).unlink()
    view = run(owner, connection["id"])
    assert view["lastStatus"] == "ok", view
    assert not any("kept in Drive" in line for line in view["lastSkipped"]), view["lastSkipped"]
    assert drive_files(owner, folder["id"]) == {"keep.txt": b"k", "Deep/": b"", "Other/": b""}
    assert bin_names(owner) == ["a.txt", "b.txt", "c.txt"]


def test_the_kept_in_drive_line_shows_past_a_hundred_skipped_names(syncing, cloud):
    owner, _ = syncing
    folder, connection = connect(owner, cloud)
    write_remote(cloud, "a.txt", b"aaa")
    write_remote(cloud, "keep.txt", b"k")
    first_sync(owner, connection)
    skipped_names = 120
    for number in range(skipped_names):
        write_remote(cloud, f"Other/odd\\{number:03}.txt", b"xyz")
    (remote_dir(cloud) / "a.txt").unlink()
    view = run(owner, connection["id"])
    assert view["lastStatus"] == "ok", view
    assert drive_files(owner, folder["id"]) == {"a.txt": b"aaa", "keep.txt": b"k", "Other/": b""}
    lines = view["lastSkipped"]
    assert len(lines) == 100, len(lines)
    assert lines[0].startswith("'a.txt'") and "kept in Drive" in lines[0], lines[0]
    assert str(skipped_names + 1) in lines[-1] and "not listed" in lines[-1], lines[-1]


@pytest.mark.parametrize("change", ["renamed to a refused name", "deleted beside a skipped folder"])
def test_a_folder_gone_from_the_cloud_with_a_held_file_is_not_made_again_there(syncing, cloud, change):
    owner, _ = syncing
    folder, connection = connect(owner, cloud)
    write_remote(cloud, "Docs/c.txt", b"ccccccc")
    write_remote(cloud, "Docs/d.txt", b"dd")
    write_remote(cloud, "keep.txt", b"k")
    if change == "deleted beside a skipped folder":
        write_remote(cloud, "Old\\Mac/x.txt", b"never synced")
    first_sync(owner, connection)
    if change == "renamed to a refused name":
        (remote_dir(cloud) / "Docs").rename(remote_dir(cloud) / "Do\\cs")
    else:
        shutil.rmtree(remote_dir(cloud) / "Docs")
    cloud_after_the_change = disk_files(remote_dir(cloud))
    for _ in range(2):
        view = run(owner, connection["id"])
        assert view["lastStatus"] == "ok" and view["lastCounts"].get("foldersMade") is None, view
        assert any(line.startswith("'Docs'") for line in view["lastSkipped"]), view["lastSkipped"]
    assert disk_files(remote_dir(cloud)) == cloud_after_the_change
    assert drive_files(owner, folder["id"]) == {"Docs/": b"", "Docs/c.txt": b"ccccccc", "Docs/d.txt": b"dd",
                                                "keep.txt": b"k"}
    assert bin_names(owner) == []


def test_a_synced_file_moved_into_another_folder_under_a_refused_name_is_never_binned(syncing, cloud):
    owner, _ = syncing
    folder, connection = connect(owner, cloud)
    write_remote(cloud, "notes.txt", b"words")
    write_remote(cloud, "Sub/other.txt", b"more words")
    first_sync(owner, connection)
    (remote_dir(cloud) / "notes.txt").rename(remote_dir(cloud) / "Sub" / "no\\tes.txt")
    view = run(owner, connection["id"])
    assert view["lastStatus"] == "ok" and view["lastCounts"].get("binnedInDrive") is None, view
    assert drive_files(owner, folder["id"]) == {"notes.txt": b"words", "Sub/": b"", "Sub/other.txt": b"more words"}
    assert bin_names(owner) == []


def test_odd_cloud_names_are_skipped_and_never_leave_the_folder(syncing, cloud):
    owner, _ = syncing
    folder, connection = connect(owner, cloud)
    for name in ("good.txt", "back\\slash.txt", "ctrl\x01.txt", "evil\u202etxt.exe", "Same.txt", "same.txt"):
        write_remote(cloud, name, b"x")
    view = first_sync(owner, connection)
    local = drive_files(owner, folder["id"])
    assert "good.txt" in local and len(local) == 2
    assert len(view["lastSkipped"]) == 4
    assert all(message.isascii() and message.isprintable() for message in view["lastSkipped"])
    assert [item["name"] for item in owner.get("/api/folders/0").get_json()["items"]] == ["Synced"]


def test_listings_that_point_outside_the_folder_are_refused():
    body = ('<?xml version="1.0"?><d:multistatus xmlns:d="DAV:">' + "".join(
        f'<d:response><d:href>{href}</d:href><d:propstat><d:prop><d:resourcetype/><d:getcontentlength>1'
        '</d:getcontentlength><d:getetag>"e"</d:getetag></d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>'
        '</d:response>' for href in (
            "/dav/Phone/", "/dav/Phone/ok.txt", "/dav/Phone/../../etc/passwd", "/dav/Phone/a%2Fb.txt",
            "http://evil.example/dav/Phone/x.txt", "/dav/Other/x.txt", "/dav/Phone/sub/deep.txt",
            "/dav/Phone/%2e%2e")) + "</d:multistatus>").encode()

    class Canned:
        def call(self, method, url, headers=None, body_bytes=None, upload=None, should_retry=None):
            return Answer(207, {}, body)

    remote = WebDavRemote("http://127.0.0.1:1/dav/", "u", "p", "Phone", Canned())
    listing = remote.list_tree()
    assert list(listing.entries) == ["ok.txt"]
    assert len(listing.skipped) == 6


class Canned:
    def __init__(self, body: bytes):
        self.body = body

    def call(self, method, url, headers=None, body_bytes=None, upload=None, should_retry=None):
        return Answer(207, {}, self.body)


def multistatus(*hrefs: str) -> bytes:
    return ('<?xml version="1.0"?><d:multistatus xmlns:d="DAV:">' + "".join(
        f'<d:response><d:href>{href}</d:href><d:propstat><d:prop><d:resourcetype/><d:getcontentlength>1'
        '</d:getcontentlength><d:getetag>"e"</d:getetag></d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>'
        '</d:response>' for href in hrefs) + "</d:multistatus>").encode()


def test_a_listing_without_the_folder_itself_is_not_trusted():
    remote = WebDavRemote("http://127.0.0.1:1/dav/", "u", "p", "Phone", Canned(multistatus("/dav/Phone/ok.txt")))
    with pytest.raises(RemoteRefused, match="without the folder itself"):
        remote.list_tree()
    remote = WebDavRemote("http://127.0.0.1:1/dav/", "u", "p", "Phone",
                          Canned(multistatus("/dav/Phone/", "/dav/Phone/ok.txt")))
    assert list(remote.list_tree().entries) == ["ok.txt"]


def test_a_folder_with_an_item_listed_elsewhere_keeps_its_synced_files():
    remote = WebDavRemote("http://127.0.0.1:1/dav/", "u", "p", "Phone",
                          Canned(multistatus("/dav/Phone/", "/dav/Phone/ok.txt", "/files/Phone/kept.txt")))
    listing = remote.list_tree()
    assert list(listing.entries) == ["ok.txt"]
    base = {"kept.txt": BaseEntry("file", 7, "blob-7", None, "e")}
    local = {"kept.txt": LocalEntry("file", 7, "blob-7")}
    planned = plan(base, local, listing.entries, "both", listing.doubts)
    assert [(action.kind, action.path) for action in planned] == [("skip", "kept.txt"), ("download", "ok.txt")]


def test_listings_with_xml_entities_are_refused():
    bomb = (b'<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">]><d:multistatus xmlns:d="DAV:">'
            b'<d:response><d:href>&lol;</d:href></d:response></d:multistatus>')
    with pytest.raises(RemoteRefused, match="entities"):
        parse_multistatus(bomb)


def test_xml_entities_are_refused_anywhere_in_a_listing():
    late = (b'<?xml version="1.0"?><!--' + b"x" * 5000 + b'--><!DOCTYPE lolz [<!ENTITY lol "expanded">]>'
            b'<d:multistatus xmlns:d="DAV:"><d:response><d:href>/dav/&lol;</d:href></d:response></d:multistatus>')
    with pytest.raises(RemoteRefused, match="entities"):
        parse_multistatus(late)


@pytest.mark.parametrize("encoding", ["utf-16", "utf-16-le"])
def test_xml_entities_are_refused_in_any_encoding(encoding):
    text = (f'<?xml version="1.0" encoding="{encoding}"?><!DOCTYPE lolz [<!ENTITY lol "expanded">]>'
            '<d:multistatus xmlns:d="DAV:"><d:response><d:href>/dav/&lol;</d:href></d:response></d:multistatus>')
    body = text.encode("utf-16") if encoding == "utf-16" else ("\ufeff" + text).encode(encoding)
    with pytest.raises(RemoteRefused, match="entities|could not be read"):
        parse_multistatus(body)


def test_a_listing_in_an_unknown_encoding_is_refused():
    body = (b'<?xml version="1.0" encoding="x-bogus"?><d:multistatus xmlns:d="DAV:"><d:response>'
            b'<d:href>/dav/a.txt</d:href></d:response></d:multistatus>')
    with pytest.raises(RemoteRefused, match="could not be read"):
        parse_multistatus(body)


def test_the_disk_floor_stops_downloads(syncing, cloud, free_space):
    owner, _ = syncing
    folder, connection = connect(owner, cloud)
    write_remote(cloud, "big.bin", b"z" * 4096)
    preview(owner, connection["id"])
    free_space.free = 15 * GIB
    view = run(owner, connection["id"])
    assert view["lastStatus"] == "failed" and "free space" in view["lastErrors"][0]
    assert drive_files(owner, folder["id"]) == {}


def test_cloud_files_bigger_than_drive_allows_are_not_fetched(services, tmp_path, cloud):
    owner, _ = attach_sync(services, tmp_path, max_file_bytes=1024)
    folder, connection = connect(owner, cloud)
    write_remote(cloud, "small.txt", b"s")
    write_remote(cloud, "huge.bin", b"h" * 4096)
    view = first_sync(owner, connection)
    assert view["lastStatus"] == "errors" and "bigger than" in view["lastErrors"][0]
    assert drive_files(owner, folder["id"]) == {"small.txt": b"s"}


def test_a_wrong_app_password_stops_the_run(syncing, cloud):
    owner, _ = syncing
    folder, connection = connect(owner, cloud, password="not-the-password")
    shown = preview(owner, connection["id"])
    assert "refused the user name or app password" in shown["stopped"]
    owner.patch(f"/api/sync/connections/{connection['id']}", json={"password": cloud.password})
    assert preview(owner, connection["id"])["stopped"] is None


def test_one_way_connections_only_send_one_way(syncing, cloud):
    owner, _ = syncing
    up_folder, up = connect(owner, cloud, "Up", direction="upload")
    owner.upload("mine.txt", b"m", parent_id=up_folder["id"])
    write_remote(cloud, "theirs.txt", b"t")
    first_sync(owner, up)
    assert drive_files(owner, up_folder["id"]) == {"mine.txt": b"m"}
    assert disk_files(remote_dir(cloud)) == {"mine.txt": b"m", "theirs.txt": b"t"}
    (remote_dir(cloud) / "mine.txt").unlink()
    run(owner, up["id"])
    assert disk_files(remote_dir(cloud))["mine.txt"] == b"m"


def test_download_only_never_uploads(services, tmp_path):
    with running_dav(tmp_path / "other-root") as server:
        (server.root / REMOTE_FOLDER).mkdir()
        owner, _ = attach_sync(services, tmp_path)
        folder, connection = connect(owner, server, direction="download")
        owner.upload("mine.txt", b"m", parent_id=folder["id"])
        write_remote(server, "theirs.txt", b"t")
        first_sync(owner, connection)
        assert disk_files(server.root / REMOTE_FOLDER) == {"theirs.txt": b"t"}
        assert drive_files(owner, folder["id"]) == {"mine.txt": b"m", "theirs.txt": b"t"}


def test_a_missing_cloud_folder_is_made_by_an_upload_connection(syncing, cloud):
    owner, _ = syncing
    folder = owner.folder("Backup")
    owner.upload("a.txt", b"a", parent_id=folder["id"])
    created = owner.post("/api/sync/connections", json={
        "name": "Backup", "provider": "webdav", "folderId": folder["id"], "remotePath": "Backups/Drive",
        "direction": "upload", "serverUrl": cloud.url, "username": cloud.username, "password": cloud.password})
    connection = created.get_json()
    shown = preview(owner, connection["id"])
    assert shown["notes"] and not (cloud.root / "Backups").exists()
    run(owner, connection["id"])
    assert (cloud.root / "Backups" / "Drive" / "a.txt").read_bytes() == b"a"


def test_nextcloud_connections_use_the_files_address(services, tmp_path):
    root = tmp_path / "nextcloud"
    with running_dav(root, mount="/remote.php/dav/files/tester") as server:
        (root / "Photos").mkdir()
        (root / "Photos" / "cat.jpg").write_bytes(b"meow")
        owner, _ = attach_sync(services, tmp_path)
        folder = owner.folder("Photos")
        created = owner.post("/api/sync/connections", json={
            "name": "Nextcloud photos", "provider": "nextcloud", "folderId": folder["id"], "remotePath": "Photos",
            "direction": "download", "serverUrl": server.origin, "username": server.username,
            "password": server.password})
        connection = created.get_json()
        assert first_sync(owner, connection)["lastStatus"] == "ok"
        assert drive_files(owner, folder["id"]) == {"cat.jpg": b"meow"}


def test_a_missing_nextcloud_folder_is_made_under_the_files_address(services, tmp_path):
    root = tmp_path / "nextcloud"
    with running_dav(root, mount="/remote.php/dav/files/tester") as server:
        owner, _ = attach_sync(services, tmp_path)
        folder = owner.folder("Backup")
        owner.upload("a.txt", b"a", parent_id=folder["id"])
        created = owner.post("/api/sync/connections", json={
            "name": "Nextcloud backup", "provider": "nextcloud", "folderId": folder["id"],
            "remotePath": "Backups/Drive", "direction": "upload", "serverUrl": server.origin,
            "username": server.username, "password": server.password})
        connection = created.get_json()
        assert preview(owner, connection["id"])["notes"]
        view = run(owner, connection["id"])
        assert view["lastStatus"] == "ok", view["lastErrors"]
        assert (root / "Backups" / "Drive" / "a.txt").read_bytes() == b"a"


def test_two_drives_on_one_nextcloud_folder_pass_edits_without_conflicts(services, tmp_path):
    root = tmp_path / "nextcloud"
    with running_dav(root, mount="/remote.php/dav/files/tester", file_ids=True) as server:
        (root / "Shared").mkdir()
        (root / "Shared" / "notes.txt").write_bytes(b"version one")
        owner, _ = attach_sync(services, tmp_path)
        installs = []
        for name in ("Laptop", "Desktop"):
            folder = owner.folder(name)
            created = owner.post("/api/sync/connections", json={
                "name": name, "provider": "nextcloud", "folderId": folder["id"], "remotePath": "Shared",
                "direction": "both", "serverUrl": server.origin, "username": server.username,
                "password": server.password})
            installs.append((folder, created.get_json()))
            assert first_sync(owner, created.get_json())["lastStatus"] == "ok"
        laptop, desktop = installs
        owner.post("/api/nodes/trash", json={"ids": [find_node(owner, laptop[0]["id"], "notes.txt")["id"]]})
        owner.upload("notes.txt", b"version two from the laptop", parent_id=laptop[0]["id"])
        for _ in range(3):
            for folder, connection in installs:
                view = run(owner, connection["id"])
                assert view["lastStatus"] == "ok" and view["lastCounts"].get("conflicts") is None, view
        expected = {"notes.txt": b"version two from the laptop"}
        assert drive_files(owner, laptop[0]["id"]) == expected
        assert drive_files(owner, desktop[0]["id"]) == expected
        assert disk_files(root / "Shared") == expected


def test_one_sync_per_connection_and_a_busy_answer_beyond_the_cap(services, tmp_path, cloud):
    owner, _ = attach_sync(services, tmp_path, syncs_at_once=1)
    _, first = connect(owner, cloud, "First")
    _, second = connect(owner, cloud, "Second")
    cloud.faults.delay_seconds = 0.5
    assert owner.post(f"/api/sync/connections/{first['id']}/preview").status_code == 202
    assert owner.post(f"/api/sync/connections/{first['id']}/preview").status_code == 409
    busy = owner.post(f"/api/sync/connections/{second['id']}/preview")
    assert busy.status_code == 503 and busy.headers["Retry-After"]
    assert owner.delete(f"/api/sync/connections/{first['id']}").status_code == 409
    wait_until_idle(owner, first["id"])


def test_the_app_password_never_leaves_the_server(syncing, cloud, tmp_path, caplog):
    owner, _ = syncing
    caplog.set_level(logging.DEBUG)
    folder, connection = connect(owner, cloud)
    first_sync(owner, connection)
    for answer in (owner.get("/api/sync"), owner.get(f"/api/sync/connections/{connection['id']}"),
                   owner.get(f"/api/sync/connections/{connection['id']}/preview")):
        assert cloud.password not in answer.get_data(as_text=True)
    assert cloud.password not in caplog.text
    stored = sqlite3.connect(tmp_path / "drive.db").execute("SELECT secret FROM sync_connections").fetchone()[0]
    assert cloud.password.encode() not in stored
    assert (tmp_path / "token.key").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("change", [
    {"serverUrl": "http://example.com/dav/"}, {"serverUrl": "https://user:pw@example.com/dav/"},
    {"serverUrl": "ftp://10.1.2.10/"}, {"remotePath": "../outside"}, {"remotePath": "a\\b"},
    {"direction": "sideways"}, {"intervalMinutes": 7}, {"password": ""}, {"provider": "dropbox"},
    {"name": ""}, {"folderId": 999},
])
def test_bad_connection_settings_are_refused(syncing, cloud, change):
    owner, _ = syncing
    folder = owner.folder("Target")
    body = {"name": "x", "provider": "webdav", "folderId": folder["id"], "remotePath": "Phone", "direction": "both",
            "serverUrl": cloud.url, "username": cloud.username, "password": cloud.password, **change}
    assert owner.post("/api/sync/connections", json=body).status_code in (400, 404)


def test_overlapping_folders_and_risky_edits_are_refused(syncing, cloud):
    owner, _ = syncing
    folder, connection = connect(owner, cloud)
    inner = owner.folder("Inner", parent_id=folder["id"])
    body = {"name": "again", "provider": "webdav", "folderId": inner["id"], "remotePath": "Other", "direction": "both",
            "serverUrl": cloud.url, "username": cloud.username, "password": cloud.password}
    assert owner.post("/api/sync/connections", json=body).status_code == 409
    assert owner.patch(f"/api/sync/connections/{connection['id']}", json={"serverUrl": cloud.url}).status_code == 400
    assert owner.patch(f"/api/sync/connections/{connection['id']}", json={"paused": "yes"}).status_code == 400
    assert owner.patch(f"/api/sync/connections/{connection['id']}", json={"paused": True}).get_json()["paused"]


def test_folders_moved_into_each_other_stop_both_connections(syncing, cloud):
    owner, _ = syncing
    (cloud.root / "Other").mkdir()
    first_folder, first = connect(owner, cloud, "First")
    second_folder = owner.folder("Second")
    second = owner.post("/api/sync/connections", json={
        "name": "second", "provider": "webdav", "folderId": second_folder["id"], "remotePath": "Other",
        "direction": "both", "serverUrl": cloud.url, "username": cloud.username,
        "password": cloud.password}).get_json()
    write_remote(cloud, "one.txt", b"1")
    (cloud.root / "Other" / "two.txt").write_bytes(b"2")
    first_sync(owner, first)
    first_sync(owner, second)
    moved = owner.post("/api/nodes/move", json={"ids": [second_folder["id"]], "targetId": first_folder["id"]})
    assert moved.status_code == 200
    for connection in (first, second):
        view = run(owner, connection["id"])
        assert view["lastStatus"] == "failed" and "overlaps" in view["lastErrors"][0], view
        assert "overlaps" in preview(owner, connection["id"])["stopped"]
    assert disk_files(remote_dir(cloud)) == {"one.txt": b"1"}
    assert bin_names(owner) == []


def test_sync_writes_need_the_csrf_token(syncing, cloud):
    owner, _ = syncing
    folder, connection = connect(owner, cloud)
    answer = owner.client.post(f"/api/sync/connections/{connection['id']}/run", headers=owner.headers)
    assert answer.status_code == 403


def test_the_schedule_runs_only_due_connections_that_have_synced_before(services, tmp_path, cloud):
    clock = FakeClock()
    owner, sync = attach_sync(services, tmp_path, clock=clock)
    _, hourly = connect(owner, cloud, "Hourly", intervalMinutes=60)
    _, never_run = connect(owner, cloud, "Never run", intervalMinutes=15)
    _, paused = connect(owner, cloud, "Paused", intervalMinutes=15)
    _, manual = connect(owner, cloud, "Manual")
    for connection in (hourly, paused, manual):
        first_sync(owner, connection)
    owner.patch(f"/api/sync/connections/{paused['id']}", json={"paused": True})
    assert sync.run_due() == []
    clock.advance(HOUR_SECONDS + 1)
    assert sync.run_due() == [hourly["id"]]
    view = wait_until_idle(owner, hourly["id"])
    assert view["lastStatus"] == "ok" and view["nextRunAt"] == clock() + HOUR_SECONDS
    assert sync.run_due() == []
    assert owner.get(f"/api/sync/connections/{never_run['id']}").get_json()["lastRunAt"] is None


def test_a_scheduled_run_never_bins_even_with_a_yes_waiting(services, tmp_path, cloud):
    clock = FakeClock()
    owner, sync = attach_sync(services, tmp_path, clock=clock)
    folder, connection = connect(owner, cloud, "Hourly", intervalMinutes=60)
    for number in range(6):
        write_remote(cloud, f"photo-{number}.jpg", bytes([number]) * 100)
    first_sync(owner, connection)
    for number in range(5):
        (remote_dir(cloud) / f"photo-{number}.jpg").unlink()
    held = run(owner, connection["id"])["held"]
    for _ in range(2):
        clock.advance(HOUR_SECONDS + 1)
        assert sync.run_due() == [connection["id"]]
        view = wait_until_idle(owner, connection["id"])
        assert view["lastStatus"] == "held" and view["held"]["approval"] == held["approval"], view
    assert len(drive_files(owner, folder["id"])) == 6 and bin_names(owner) == []
