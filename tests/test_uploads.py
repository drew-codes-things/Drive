import os
import threading

from conftest import GIB
from drive.errors import DriveError
from drive.uploads import CHUNK_BYTES, STALE_SECONDS


def start(owner, name="big.bin", size=10, **extra):
    return owner.post("/api/uploads", json={"name": name, "size": size, "parentId": 0, **extra})


def test_a_file_in_several_chunks_arrives_whole(owner):
    content = os.urandom(CHUNK_BYTES * 2 + 123)
    node = owner.upload("big.bin", content)
    assert node["size"] == len(content)
    download = owner.get(f"/api/files/{node['id']}/content")
    assert download.data == content


def test_an_upload_resumes_from_what_the_server_has(owner):
    content = b"abcdefghij"
    upload = start(owner, size=len(content)).get_json()
    assert owner.put(f"/api/uploads/{upload['id']}?offset=0", data=content[:4]).get_json()["received"] == 4
    status = owner.get(f"/api/uploads/{upload['id']}").get_json()
    assert status["received"] == 4
    repeat = owner.put(f"/api/uploads/{upload['id']}?offset=0", data=content[:4])
    assert repeat.status_code == 409
    assert "4 bytes" in repeat.get_json()["error"]
    owner.put(f"/api/uploads/{upload['id']}?offset=4", data=content[4:])
    node = owner.post(f"/api/uploads/{upload['id']}/finish").get_json()
    assert owner.get(f"/api/files/{node['id']}/content").data == content


def test_unfinished_uploads_cannot_be_finished_or_overfilled(owner):
    upload = start(owner, size=5).get_json()
    owner.put(f"/api/uploads/{upload['id']}?offset=0", data=b"abc")
    assert owner.post(f"/api/uploads/{upload['id']}/finish").status_code == 409
    assert owner.put(f"/api/uploads/{upload['id']}?offset=3", data=b"defg").status_code == 400


def test_chunk_offsets_must_be_plain_numbers(owner):
    upload = start(owner).get_json()
    for offset in ("-1", "1e3", "", "١", "9" * 16):
        assert owner.put(f"/api/uploads/{upload['id']}?offset={offset}", data=b"a").status_code == 400


def test_unknown_or_malformed_upload_ids_are_not_found(owner):
    for upload_id in ("0" * 32, "../../etc/passwd", "ABC"):
        assert owner.get(f"/api/uploads/{upload_id}").status_code == 404
        assert owner.put(f"/api/uploads/{upload_id}?offset=0", data=b"a").status_code == 404


def test_folder_uploads_rebuild_the_folders(owner):
    first = owner.upload("a.txt", b"a", folders=["Holiday", "Day 1"])
    second = owner.upload("b.txt", b"b", folders=["Holiday", "Day 1"])
    assert first["parentId"] == second["parentId"]
    top = owner.get("/api/folders/0").get_json()["items"]
    assert [item["name"] for item in top] == ["Holiday"]
    assert start(owner, folders=["..", "x"]).status_code == 400
    assert start(owner, folders=["a/b"]).status_code == 400
    assert start(owner, folders="Holiday").status_code == 400


def test_same_name_uploads_keep_both(owner):
    owner.upload("photo.jpg", b"one")
    second = owner.upload("photo.jpg", b"two")
    assert second["name"] == "photo (2).jpg"


def test_empty_files_upload_without_chunks(owner):
    upload = start(owner, name="empty.txt", size=0).get_json()
    node = owner.post(f"/api/uploads/{upload['id']}/finish").get_json()
    assert node["size"] == 0


def test_uploads_are_refused_when_the_disk_would_drop_below_the_floor(owner, free_space):
    free_space.free = 16 * GIB
    refused = start(owner, size=2 * GIB)
    assert refused.status_code == 507
    assert "15.0 GB free" in refused.get_json()["error"]
    assert start(owner, size=GIB // 2).status_code == 201


def test_promised_space_counts_for_uploads_still_running(owner, free_space):
    free_space.free = 16 * GIB
    assert start(owner, size=GIB * 3 // 4).status_code == 201
    assert start(owner, size=GIB * 3 // 4).status_code == 507


def test_a_chunk_is_refused_when_the_disk_filled_up_meanwhile(owner, free_space):
    upload = start(owner, size=10).get_json()
    free_space.free = 15 * GIB
    answer = owner.put(f"/api/uploads/{upload['id']}?offset=0", data=b"abc")
    assert answer.status_code == 507
    assert owner.get(f"/api/uploads/{upload['id']}").get_json()["received"] == 0


def test_storage_shows_used_free_and_the_floor(owner, free_space):
    free_space.free = 20 * GIB
    owner.upload("a.txt", b"12345")
    storage = owner.get("/api/storage").get_json()
    assert storage["usedBytes"] == 5
    assert storage["freeBytes"] == 20 * GIB
    assert storage["minFreeBytes"] == 15 * GIB
    assert storage["uploadRoomBytes"] == 5 * GIB


def test_files_larger_than_the_limit_are_refused(owner):
    assert start(owner, size=5 * GIB).status_code == 413
    assert start(owner, size=-1).status_code == 400
    assert start(owner, size="10").status_code == 400


def test_cancel_removes_the_partial_file(owner, services):
    upload = start(owner, size=10).get_json()
    owner.put(f"/api/uploads/{upload['id']}?offset=0", data=b"abc")
    assert owner.delete(f"/api/uploads/{upload['id']}").status_code == 204
    assert list(services.uploads.folder.glob("*.part")) == []
    assert services.disk.promised_bytes() == 0


def test_stale_uploads_are_dropped(owner, services, clock):
    upload = start(owner, size=10).get_json()
    clock.advance(STALE_SECONDS + 1)
    assert services.uploads.purge_stale() == 1
    assert owner.get(f"/api/uploads/{upload['id']}").status_code == 404


def test_promises_come_back_after_a_restart(owner, services):
    upload = start(owner, size=GIB).get_json()
    owner.put(f"/api/uploads/{upload['id']}?offset=0", data=b"x" * 100)
    services.disk.release(upload["id"])
    services.uploads.restore_promises()
    assert services.disk.promised_bytes() == GIB - 100


def test_uploading_into_a_trashed_folder_is_refused(owner):
    folder = owner.folder("old")
    owner.post("/api/nodes/trash", json={"ids": [folder["id"]]})
    assert start(owner, parentId=folder["id"]).status_code == 404


def test_the_last_modified_time_is_kept(owner):
    upload = start(owner, name="t.txt", size=1, lastModified=1_600_000_000_000).get_json()
    owner.put(f"/api/uploads/{upload['id']}?offset=0", data=b"x")
    node = owner.post(f"/api/uploads/{upload['id']}/finish").get_json()
    assert node["modifiedAt"] == 1_600_000_000


def test_finishing_the_same_upload_at_once_gives_one_file_and_no_server_errors(owner, services):
    upload = start(owner, name="empty.txt", size=0).get_json()
    outcomes = []
    gate = threading.Barrier(6)

    def finish():
        gate.wait()
        try:
            outcomes.append(services.uploads.finish(upload["id"])["name"])
        except DriveError as error:
            outcomes.append(error.status)

    workers = [threading.Thread(target=finish) for _ in range(6)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    assert sorted(outcomes, key=str) == sorted(["empty.txt"] + [404] * 5, key=str)
    assert [item["name"] for item in owner.get("/api/folders/0").get_json()["items"]] == ["empty.txt"]


def test_a_folder_upload_next_to_a_file_with_the_same_name_makes_one_folder(owner):
    owner.upload("docs", b"a file called docs")
    batch = "ab" * 16
    for name in ("one.txt", "two.txt", "three.txt"):
        upload = start(owner, name=name, size=1, folders=["docs", "inner"], batch=batch).get_json()
        owner.put(f"/api/uploads/{upload['id']}?offset=0", data=b"x")
        assert owner.post(f"/api/uploads/{upload['id']}/finish").status_code == 200
    top = owner.get("/api/folders/0").get_json()["items"]
    assert sorted((item["kind"], item["name"]) for item in top) == [("file", "docs"), ("folder", "docs (2)")]
    assert start(owner, folders=["x"], batch="not-hex").status_code == 400


def test_an_upload_whose_folder_was_deleted_is_dropped_not_put_somewhere_else(owner, services):
    folder = owner.folder("going")
    upload = start(owner, name="late.txt", size=1, parentId=folder["id"]).get_json()
    owner.put(f"/api/uploads/{upload['id']}?offset=0", data=b"x")
    owner.post("/api/nodes/trash", json={"ids": [folder["id"]]})
    owner.post("/api/nodes/delete", json={"ids": [folder["id"]]})
    newcomer = owner.folder("unrelated")
    assert newcomer["id"] != folder["id"]
    finished = owner.post(f"/api/uploads/{upload['id']}/finish")
    assert finished.status_code == 404
    assert "deleted" in finished.get_json()["error"]
    assert owner.get(f"/api/uploads/{upload['id']}").status_code == 404
    assert owner.get(f"/api/folders/{newcomer['id']}").get_json()["items"] == []
    assert services.disk.promised_bytes() == 0


def test_tidying_drops_uploads_whose_folder_is_gone(owner, services):
    folder = owner.folder("going")
    upload = start(owner, size=5, parentId=folder["id"]).get_json()
    kept = start(owner, size=5).get_json()
    owner.post("/api/nodes/trash", json={"ids": [folder["id"]]})
    owner.post("/api/nodes/delete", json={"ids": [folder["id"]]})
    assert services.uploads.purge_stale() == 1
    assert owner.get(f"/api/uploads/{upload['id']}").status_code == 404
    assert owner.get(f"/api/uploads/{kept['id']}").status_code == 200
