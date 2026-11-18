import logging
import sqlite3
import unicodedata
from urllib.parse import parse_qs, urlsplit

import pytest

from conftest import FakeClock
from drive.sync.google import APP_FILES_SCOPE, FULL_SCOPE, GoogleDriveRemote, Grant, GoogleSession, OAuthClient
from drive.sync.local import LocalSide
from drive.sync.remote import SyncError
from google_stub import (CUT_SHORT_OF_ITS_LENGTH, CUT_WITHOUT_A_LENGTH, GARBLED_DOWNLOAD, HALF_DOWNLOAD,
                         running_google)
from sync_helpers import (attach_sync, binned_drive_file_names, drive_files, find_node, preview, quick_http, run,
                          run_saying_yes_to_what_is_shown, wait_until_idle)

REMOTE_FOLDER = "Phone"
FIRST_EMAIL = "first.person@example.com"
SECOND_EMAIL = "second.person@example.com"
GOOGLE_DOC_TYPE = "application/vnd.google-apps.document"
SIGN_IN_LIMIT_SECONDS = 600


@pytest.fixture
def google():
    with running_google() as stub:
        yield stub


@pytest.fixture
def syncing(services, tmp_path, google):
    return attach_sync(services, tmp_path, google=google.setup())


def connect(owner, folder_name: str = "Synced", direction: str = "both", remote_path: str = REMOTE_FOLDER) -> dict:
    folder = owner.folder(folder_name)
    body = {"name": f"{folder_name} with Google", "provider": "google", "folderId": folder["id"],
            "remotePath": remote_path, "direction": direction}
    created = owner.post("/api/sync/connections", json=body)
    assert created.status_code == 201, created.get_json()
    return {**created.get_json(), "folder": folder}


def sign_in_url(owner, connection: dict) -> dict:
    answer = owner.post(f"/api/sync/connections/{connection['id']}/google")
    assert answer.status_code == 200, answer.get_json()
    return {key: values[0] for key, values in parse_qs(urlsplit(answer.get_json()["url"]).query).items()}


def sign_in(owner, google, connection: dict, email: str = FIRST_EMAIL, granted_scope: str | None = None):
    query = sign_in_url(owner, connection)
    code = google.authorize(query, email, granted_scope)
    return owner.get(f"/sync/google/callback?state={query['state']}&code={code}")


def signed_in(owner, google, folder_name: str = "Synced", direction: str = "both", email: str = FIRST_EMAIL) -> dict:
    connection = connect(owner, folder_name, direction)
    assert sign_in(owner, google, connection, email).status_code == 303
    return {**owner.get(f"/api/sync/connections/{connection['id']}").get_json(), "folder": connection["folder"]}


def first_sync(owner, connection: dict) -> dict:
    preview(owner, connection["id"])
    view = run(owner, connection["id"])
    assert view["lastStatus"] == "ok", view["lastErrors"]
    return view


def cloud_folder(google) -> str:
    return google.add(REMOTE_FOLDER)


def test_sign_in_uses_pkce_and_the_narrowest_scope_for_the_direction(syncing, google):
    owner, _ = syncing
    both = connect(owner, "Both")
    upload = connect(owner, "Upload", direction="upload")
    both_query, upload_query = sign_in_url(owner, both), sign_in_url(owner, upload)
    assert both_query["scope"] == FULL_SCOPE and upload_query["scope"] == APP_FILES_SCOPE
    assert both_query["code_challenge_method"] == "S256" and both_query["access_type"] == "offline"
    assert "code_verifier" not in both_query and len(both_query["state"]) >= 40
    assert both_query["redirect_uri"].endswith("/sync/google/callback")


def test_signing_in_stores_the_account_and_the_link_works_once(syncing, google, tmp_path):
    owner, _ = syncing
    connection = connect(owner)
    assert connection["needsSignIn"]
    query = sign_in_url(owner, connection)
    code = google.authorize(query, FIRST_EMAIL)
    callback = f"/sync/google/callback?state={query['state']}&code={code}"
    answer = owner.get(callback)
    assert answer.status_code == 303 and answer.headers["Location"].endswith("/#/sync")
    view = owner.get(f"/api/sync/connections/{connection['id']}").get_json()
    assert view["account"] == FIRST_EMAIL and view["hasSecret"] and not view["needsSignIn"]
    assert owner.get(callback).status_code == 400
    assert owner.get("/sync/google/callback?state=made-up&code=x").status_code == 400
    refresh_token = next(iter(google.refresh_tokens))
    stored = sqlite3.connect(tmp_path / "drive.db").execute("SELECT secret FROM sync_connections").fetchone()[0]
    assert refresh_token.encode() not in stored
    assert refresh_token not in owner.get("/api/sync").get_data(as_text=True)


def test_a_sign_in_without_the_drive_permission_is_not_kept(syncing, google):
    owner, _ = syncing
    connection = connect(owner)
    assert sign_in(owner, google, connection, granted_scope="openid").status_code == 303
    view = owner.get(f"/api/sync/connections/{connection['id']}").get_json()
    assert view["needsSignIn"] and "permission was not ticked" in view["lastErrors"][0]
    assert len(google.revoked) == 1


def test_a_cancelled_or_slow_sign_in_is_not_kept(services, tmp_path, google):
    clock = FakeClock()
    owner, _ = attach_sync(services, tmp_path, google=google.setup(), clock=clock)
    connection = connect(owner)
    query = sign_in_url(owner, connection)
    owner.get(f"/sync/google/callback?state={query['state']}&error=access_denied")
    view = owner.get(f"/api/sync/connections/{connection['id']}").get_json()
    assert view["needsSignIn"] and "access_denied" in view["lastErrors"][0]
    query = sign_in_url(owner, connection)
    code = google.authorize(query, FIRST_EMAIL)
    clock.advance(SIGN_IN_LIMIT_SECONDS + 1)
    owner.get(f"/sync/google/callback?state={query['state']}&code={code}")
    view = owner.get(f"/api/sync/connections/{connection['id']}").get_json()
    assert view["needsSignIn"] and "longer than 10 minutes" in view["lastErrors"][0]


def test_a_sync_cannot_start_before_signing_in(syncing, google):
    owner, _ = syncing
    connection = connect(owner)
    assert owner.post(f"/api/sync/connections/{connection['id']}/preview").status_code == 409


def test_the_first_run_waits_for_the_preview_then_copies_both_ways(syncing, google):
    owner, _ = syncing
    parent = cloud_folder(google)
    google.add("from-cloud.txt", parent, b"cloud side")
    trip = google.add("Trip", parent)
    for number in range(3):
        google.add(f"day{number}.jpg", trip, bytes([number]) * 50)
    connection = signed_in(owner, google)
    folder = connection["folder"]
    owner.upload("from-drive.txt", b"drive side", parent_id=folder["id"])
    assert owner.post(f"/api/sync/connections/{connection['id']}/run").status_code == 409
    shown = preview(owner, connection["id"])
    assert ("upload", "from-drive.txt") in [(line["action"], line["path"]) for line in shown["actions"]]
    assert "from-drive.txt" not in google.tree(parent)
    view = first_sync(owner, connection)
    assert view["lastCounts"]["uploaded"] == 1 and view["lastCounts"]["downloaded"] == 4
    assert drive_files(owner, folder["id"]) == google.tree(parent)
    assert google.tree(parent)["from-drive.txt"] == b"drive side"


def test_changes_flow_both_ways_and_deletes_go_to_the_google_bin(syncing, google):
    owner, _ = syncing
    parent = cloud_folder(google)
    notes = google.add("notes.txt", parent, b"version one")
    google.add("drop-there.txt", parent, b"b")
    connection = signed_in(owner, google)
    folder = connection["folder"]
    first_sync(owner, connection)
    google.files[notes]["content"] = b"version two, longer"
    owner.upload("new-here.txt", b"made in Drive", parent_id=folder["id"])
    owner.post("/api/nodes/trash", json={"ids": [find_node(owner, folder["id"], "drop-there.txt")["id"]]})
    view = run(owner, connection["id"])
    assert view["lastStatus"] == "ok", view["lastErrors"]
    assert view["lastCounts"]["binnedInCloud"] == 1
    assert drive_files(owner, folder["id"]) == google.tree(parent)
    assert drive_files(owner, folder["id"])["notes.txt"] == b"version two, longer"
    assert google.find("drop-there.txt", parent, trashed=True) is not None
    google.files[notes]["trashed"] = True
    view = run(owner, connection["id"])
    assert view["lastCounts"]["binnedInDrive"] == 1
    assert sorted(item["name"] for item in owner.get("/api/bin").get_json()["items"]) == [
        "drop-there.txt", "notes.txt", "notes.txt"]


def test_both_sides_changed_keeps_both_copies(syncing, google):
    owner, _ = syncing
    parent = cloud_folder(google)
    plan = google.add("plan.txt", parent, b"first")
    connection = signed_in(owner, google)
    folder = connection["folder"]
    first_sync(owner, connection)
    owner.post("/api/nodes/trash", json={"ids": [find_node(owner, folder["id"], "plan.txt")["id"]]})
    owner.upload("plan.txt", b"drive edit", parent_id=folder["id"])
    google.files[plan]["content"] = b"cloud edit, longer"
    view = run(owner, connection["id"])
    assert view["lastCounts"]["conflicts"] == 1, view
    local = drive_files(owner, folder["id"])
    assert local == google.tree(parent)
    assert local["plan.txt"] == b"drive edit"
    assert [content for name, content in local.items() if "conflict, cloud copy" in name] == [b"cloud edit, longer"]


def test_renames_follow_the_google_file_id_both_ways(syncing, google):
    owner, _ = syncing
    parent = cloud_folder(google)
    draft = google.add("draft.txt", parent, b"words")
    google.add("photo.jpg", parent, b"pixels")
    connection = signed_in(owner, google)
    folder = connection["folder"]
    first_sync(owner, connection)
    google.files[draft]["name"] = "final.txt"
    owner.patch(f"/api/nodes/{find_node(owner, folder['id'], 'photo.jpg')['id']}", json={"name": "holiday.jpg"})
    view = run(owner, connection["id"])
    assert view["lastCounts"]["moved"] == 2, view
    assert drive_files(owner, folder["id"]) == {"final.txt": b"words", "holiday.jpg": b"pixels"}
    assert google.tree(parent) == {"final.txt": b"words", "holiday.jpg": b"pixels"}
    assert owner.get("/api/bin").get_json()["items"] == []
    assert not [item for item in google.files.values() if item["trashed"]]


def test_google_docs_and_bad_names_are_skipped(syncing, google):
    owner, _ = syncing
    parent = cloud_folder(google)
    google.add("good.txt", parent, b"x")
    google.add("Shopping list", parent, None, GOOGLE_DOC_TYPE)
    for name in ("a/b.txt", "..", "ctrl\x01.txt", "evil‮txt.exe"):
        google.add(name, parent, b"x")
    connection = signed_in(owner, google)
    view = first_sync(owner, connection)
    assert drive_files(owner, connection["folder"]["id"]) == {"good.txt": b"x"}
    assert len(view["lastSkipped"]) == 5
    assert any("Google Docs" in message for message in view["lastSkipped"])
    assert all(message.isprintable() for message in view["lastSkipped"])


def test_an_upload_only_connection_makes_its_folder_and_never_downloads(syncing, google):
    owner, _ = syncing
    connection = signed_in(owner, google, direction="upload")
    folder = connection["folder"]
    owner.upload("out.txt", b"going up", parent_id=folder["id"])
    view = first_sync(owner, connection)
    made = google.find(REMOTE_FOLDER)
    assert made is not None and google.tree(made["id"]) == {"out.txt": b"going up"}
    google.add("not-for-drive.txt", made["id"], b"stays")
    view = run(owner, connection["id"])
    assert "not-for-drive.txt" not in drive_files(owner, folder["id"])
    assert view["lastCounts"].get("downloaded") is None


def test_rate_limits_are_waited_out_and_expired_access_is_renewed(syncing, google):
    owner, _ = syncing
    parent = cloud_folder(google)
    google.add("a.txt", parent, b"a")
    connection = signed_in(owner, google)
    google.rate_limit_next = 2
    first_sync(owner, connection)
    assert google.rate_limit_next == 0
    oauth = OAuthClient(google.setup().client_id, google.setup().client_secret, google.endpoints(), quick_http())
    refresh_token = next(iter(google.refresh_tokens))
    stale = Grant("no-longer-valid", oauth.clock() + 3000, refresh_token, frozenset())
    session = GoogleSession(oauth, refresh_token, quick_http(), stale)
    assert session.call("GET", f"{google.base}/drive/v3/about").status == 200


def test_a_withdrawn_sign_in_stops_the_run_and_asks_again(syncing, google):
    owner, _ = syncing
    google.add("a.txt", cloud_folder(google), b"a")
    connection = signed_in(owner, google)
    first_sync(owner, connection)
    google.revoked.extend(google.refresh_tokens)
    view = run(owner, connection["id"])
    assert view["lastStatus"] == "failed" and view["needsSignIn"]
    assert "sign in again" in view["lastErrors"][0]


def test_an_upload_address_off_google_is_never_used(syncing, google, caplog):
    owner, _ = syncing
    cloud_folder(google)
    connection = signed_in(owner, google)
    owner.upload("private.txt", b"stay home", parent_id=connection["folder"]["id"])
    google.upload_location_origin = "http://127.0.0.2:9"
    preview(owner, connection["id"])
    view = run(owner, connection["id"])
    assert view["lastCounts"]["errors"] == 1
    assert "outside its upload service" in view["lastErrors"][0]


def test_two_google_accounts_keep_their_own_sign_ins(syncing, google, caplog):
    owner, _ = syncing
    caplog.set_level(logging.DEBUG)
    first = signed_in(owner, google, "Mine", email=FIRST_EMAIL)
    second = signed_in(owner, google, "Work", email=SECOND_EMAIL)
    shared = signed_in(owner, google, "Mine too", email=FIRST_EMAIL)
    accounts = {view["name"]: view["account"] for view in owner.get("/api/sync").get_json()["connections"]}
    assert accounts == {"Mine with Google": FIRST_EMAIL, "Work with Google": SECOND_EMAIL,
                        "Mine too with Google": FIRST_EMAIL}
    assert owner.delete(f"/api/sync/connections/{first['id']}").status_code == 204
    assert google.revoked == []
    assert owner.delete(f"/api/sync/connections/{second['id']}").status_code == 204
    assert len(google.revoked) == 1
    assert owner.get(f"/api/sync/connections/{shared['id']}").get_json()["hasSecret"]
    for token in google.refresh_tokens:
        assert token not in caplog.text


def test_google_connections_need_the_server_set_up(services, tmp_path):
    owner, _ = attach_sync(services, tmp_path)
    folder = owner.folder("Synced")
    body = {"name": "x", "provider": "google", "folderId": folder["id"], "remotePath": "Phone", "direction": "both"}
    answer = owner.post("/api/sync/connections", json=body)
    assert answer.status_code == 409 and "GOOGLE_CLIENT_ID" in answer.get_json()["error"]
    assert owner.get("/api/sync").get_json()["googleReady"] is False


def bin_names(owner) -> list[str]:
    return sorted(item["name"] for item in owner.get("/api/bin").get_json()["items"])


@pytest.mark.parametrize("cut", [CUT_SHORT_OF_ITS_LENGTH, CUT_WITHOUT_A_LENGTH])
def test_a_cut_off_folder_listing_stops_the_run_and_bins_nothing(syncing, google, cut):
    owner, _ = syncing
    parent = cloud_folder(google)
    for number in range(4):
        google.add(f"photo-{number}.jpg", parent, bytes([65 + number]) * 50)
    connection = signed_in(owner, google)
    folder = connection["folder"]
    first_sync(owner, connection)
    google.cut_listings = cut
    view = run(owner, connection["id"])
    google.cut_listings = None
    assert view["lastStatus"] == "failed", view
    assert len(drive_files(owner, folder["id"])) == 4
    assert bin_names(owner) == []
    assert len(google.tree(parent)) == 4 and len(google.files) == 5
    view = run(owner, connection["id"])
    assert view["lastStatus"] == "ok" and view["lastCounts"].get("downloaded") is None, view


@pytest.mark.parametrize("fault", [HALF_DOWNLOAD, GARBLED_DOWNLOAD])
def test_a_download_that_does_not_match_the_listing_is_not_kept(syncing, google, fault):
    owner, _ = syncing
    parent = cloud_folder(google)
    google.add("album.bin", parent, b"0123456789" * 10)
    connection = signed_in(owner, google)
    folder = connection["folder"]
    preview(owner, connection["id"])
    google.download_fault = fault
    view = run(owner, connection["id"])
    assert view["lastStatus"] == "errors" and view["lastCounts"].get("downloaded") is None, view
    assert "not kept" in view["lastErrors"][0]
    assert drive_files(owner, folder["id"]) == {}
    google.download_fault = None
    view = run(owner, connection["id"])
    assert view["lastStatus"] == "ok" and view["lastCounts"]["downloaded"] == 1, view
    assert drive_files(owner, folder["id"]) == {"album.bin": b"0123456789" * 10}


def test_a_google_folder_that_vanished_after_a_sync_is_not_made_again(syncing, google):
    owner, _ = syncing
    parent = cloud_folder(google)
    connection = signed_in(owner, google)
    folder = connection["folder"]
    for number in range(4):
        owner.upload(f"photo-{number}.jpg", bytes([number]) * 50, parent_id=folder["id"])
    first_sync(owner, connection)
    google.files[parent]["name"] = "Phone renamed"
    view = run(owner, connection["id"])
    assert view["lastStatus"] == "failed" and "renamed" in view["lastErrors"][0], view
    assert len(drive_files(owner, folder["id"])) == 4 and bin_names(owner) == []
    assert google.find(REMOTE_FOLDER) is None


def listed_first(google, file_id: str) -> str:
    item = google.files.pop(file_id)
    item["id"] = "0-listed-first"
    google.files = {item["id"]: item, **google.files}
    return item["id"]


def contents(google) -> dict[str, bytes]:
    return {key: item["content"] for key, item in google.files.items() if item["content"] is not None}


@pytest.mark.parametrize("direction", ["upload", "both"])
def test_a_second_google_file_with_the_same_name_is_never_overwritten(syncing, google, direction):
    owner, _ = syncing
    parent = cloud_folder(google)
    first = google.add("scan.pdf", parent, b"the first scan")
    connection = signed_in(owner, google, direction=direction)
    folder = connection["folder"]
    if direction == "upload":
        owner.upload("scan.pdf", b"the first scan", parent_id=folder["id"])
    first_sync(owner, connection)
    second = listed_first(google, google.add("scan.pdf", parent, b"a different second scan"))
    for _ in range(3):
        view = run(owner, connection["id"])
        assert view["lastStatus"] == "ok" and view["lastCounts"]["skipped"] == 1, view
        assert view["lastCounts"].get("uploaded") is None and view["lastCounts"].get("downloaded") is None
    assert contents(google) == {first: b"the first scan", second: b"a different second scan"}
    assert drive_files(owner, folder["id"]) == {"scan.pdf": b"the first scan"}
    assert bin_names(owner) == []


def replace_in_the_cloud(google, parent: str, old_id: str, content: bytes) -> str:
    google.files[old_id]["trashed"] = True
    return google.add("scan.pdf", parent, content)


def test_a_new_cloud_file_at_a_synced_name_updates_an_unchanged_drive_copy(syncing, google):
    owner, _ = syncing
    parent = cloud_folder(google)
    first = google.add("scan.pdf", parent, b"the first scan")
    connection = signed_in(owner, google)
    folder = connection["folder"]
    first_sync(owner, connection)
    replace_in_the_cloud(google, parent, first, b"a newer scan, saved as a new file")
    view = run(owner, connection["id"])
    assert view["lastStatus"] == "ok" and view["lastCounts"]["downloaded"] == 1, view
    assert view["lastCounts"].get("conflicts") is None
    assert drive_files(owner, folder["id"]) == {"scan.pdf": b"a newer scan, saved as a new file"}
    assert google.tree(parent) == {"scan.pdf": b"a newer scan, saved as a new file"}
    assert bin_names(owner) == ["scan.pdf"]
    assert run(owner, connection["id"])["lastCounts"].get("downloaded") is None


def test_a_new_cloud_file_at_a_name_drive_also_changed_keeps_both(syncing, google):
    owner, _ = syncing
    parent = cloud_folder(google)
    first = google.add("scan.pdf", parent, b"the first scan")
    connection = signed_in(owner, google)
    folder = connection["folder"]
    first_sync(owner, connection)
    owner.post("/api/nodes/trash", json={"ids": [find_node(owner, folder["id"], "scan.pdf")["id"]]})
    owner.upload("scan.pdf", b"edited in Drive", parent_id=folder["id"])
    replace_in_the_cloud(google, parent, first, b"a newer scan, saved as a new file")
    view = run(owner, connection["id"])
    assert view["lastCounts"]["conflicts"] == 1, view
    local = drive_files(owner, folder["id"])
    assert local["scan.pdf"] == b"edited in Drive"
    assert sorted(local.values()) == [b"a newer scan, saved as a new file", b"edited in Drive"]
    assert local == google.tree(parent)


def test_an_upload_only_connection_never_writes_over_a_new_cloud_file(syncing, google):
    owner, _ = syncing
    parent = cloud_folder(google)
    connection = signed_in(owner, google, direction="upload")
    folder = connection["folder"]
    owner.upload("scan.pdf", b"the first scan", parent_id=folder["id"])
    first_sync(owner, connection)
    first = google.find("scan.pdf", parent)["id"]
    second = replace_in_the_cloud(google, parent, first, b"a newer scan, saved as a new file")
    run(owner, connection["id"])
    assert google.files[second]["content"] == b"a newer scan, saved as a new file"
    assert sorted(google.tree(parent).values()) == [b"a newer scan, saved as a new file", b"the first scan"]


NFD_CAFE = unicodedata.normalize("NFD", "Café")
NAMES_DRIVE_WRITES_ANOTHER_WAY = [("Holiday ", "Holiday"), (NFD_CAFE, "Café")]


@pytest.mark.parametrize("cloud_name,drive_name", NAMES_DRIVE_WRITES_ANOTHER_WAY)
def test_google_folders_with_names_drive_writes_another_way_still_sync(syncing, google, cloud_name, drive_name):
    owner, _ = syncing
    parent = cloud_folder(google)
    google.add("beach.jpg", google.add(cloud_name, parent), b"sand")
    connection = signed_in(owner, google)
    view = first_sync(owner, connection)
    assert view["lastSkipped"] == [], view
    assert drive_files(owner, connection["folder"]["id"]) == {f"{drive_name}/": b"", f"{drive_name}/beach.jpg": b"sand"}
    owner.upload("towel.jpg", b"cotton", parent_id=find_node(owner, connection["folder"]["id"], drive_name)["id"])
    view = run(owner, connection["id"])
    assert view["lastStatus"] == "ok" and view["lastCounts"].get("uploaded") == 1, view
    assert google.tree(parent) == {f"{cloud_name}/": b"", f"{cloud_name}/beach.jpg": b"sand",
                                   f"{cloud_name}/towel.jpg": b"cotton"}


@pytest.mark.parametrize("cloud_name,drive_name", NAMES_DRIVE_WRITES_ANOTHER_WAY)
def test_a_google_folder_renamed_to_a_name_drive_writes_another_way_is_moved_not_binned(syncing, google, cloud_name,
                                                                                         drive_name):
    owner, _ = syncing
    parent = cloud_folder(google)
    trip = google.add("Trip", parent)
    for number in range(2):
        google.add(f"photo-{number}.jpg", trip, bytes([65 + number]) * 20)
    connection = signed_in(owner, google)
    first_sync(owner, connection)
    google.files[trip]["name"] = cloud_name
    view = run(owner, connection["id"])
    assert view["lastStatus"] == "ok" and view["lastCounts"].get("binnedInDrive") is None, view
    assert sorted(drive_files(owner, connection["folder"]["id"])) == [f"{drive_name}/", f"{drive_name}/photo-0.jpg",
                                                                     f"{drive_name}/photo-1.jpg"]
    assert bin_names(owner) == []


def test_a_google_file_renamed_into_a_case_clash_is_never_binned(syncing, google):
    owner, _ = syncing
    parent = cloud_folder(google)
    first = google.add("a.pdf", parent, b"the first one")
    google.add("B.pdf", parent, b"another one")
    connection = signed_in(owner, google)
    first_sync(owner, connection)
    google.files[first]["name"] = "b.pdf"
    for _ in range(2):
        view = run(owner, connection["id"])
        assert view["lastStatus"] == "ok" and view["lastCounts"].get("binnedInDrive") is None, view
    assert drive_files(owner, connection["folder"]["id"]) == {"a.pdf": b"the first one", "B.pdf": b"another one"}
    assert bin_names(owner) == []
    assert google.tree(parent) == {"b.pdf": b"the first one", "B.pdf": b"another one"}


def test_a_folder_deleted_in_drive_stays_deleted_while_its_cloud_copy_holds_a_google_doc(syncing, google):
    owner, _ = syncing
    parent = cloud_folder(google)
    trip = google.add("Trip", parent)
    google.add("a.jpg", trip, b"synced")
    google.add("Plan", trip, b"", GOOGLE_DOC_TYPE)
    connection = signed_in(owner, google)
    folder = connection["folder"]
    first_sync(owner, connection)
    owner.post("/api/nodes/trash", json={"ids": [find_node(owner, folder["id"], "Trip")["id"]]})
    for _ in range(2):
        view = run(owner, connection["id"])
        assert view["lastStatus"] == "ok" and view["lastCounts"].get("foldersMade") is None, view
        assert any(line.startswith("'Trip'") and "stays in the cloud" in line for line in view["lastSkipped"]), view
        assert drive_files(owner, folder["id"]) == {}
        assert bin_names(owner) == ["Trip"]
    assert google.tree(parent) == {"Trip/": b"", "Trip/Plan": b""}
    assert google.find("a.jpg", trip, trashed=True) is not None


def test_a_google_folder_that_gains_a_file_after_it_was_listed_is_not_binned(syncing, google, monkeypatch):
    owner, _ = syncing
    parent = cloud_folder(google)
    trip = google.add("Trip", parent)
    google.add("a.jpg", trip, b"synced")
    google.add("Deeper", trip)
    connection = signed_in(owner, google)
    first_sync(owner, connection)
    owner.post("/api/nodes/trash", json={"ids": [find_node(owner, connection["folder"]["id"], "Trip")["id"]]})
    listing, listings = GoogleDriveRemote.list_tree, []

    def listing_then_the_cloud_folder_gains_a_file(self, *arguments, **named):
        found = listing(self, *arguments, **named)
        listings.append(found)
        if len(listings) == 1:
            google.add("later.jpg", google.find("Deeper", trip)["id"], b"added after the listing")
        return found

    monkeypatch.setattr(GoogleDriveRemote, "list_tree", listing_then_the_cloud_folder_gains_a_file)
    view = run(owner, connection["id"])
    monkeypatch.undo()
    assert view["lastStatus"] == "ok" and view["lastCounts"].get("binnedInCloud") is None, view
    assert any(line.startswith("'Trip'") and "changed in the cloud" in line for line in view["lastSkipped"]), view
    assert google.tree(parent) == {"Trip/": b"", "Trip/a.jpg": b"synced", "Trip/Deeper/": b"",
                                   "Trip/Deeper/later.jpg": b"added after the listing"}
    settled_runs(owner, connection, count=2)
    assert drive_files(owner, connection["folder"]["id"]) == {
        "Trip/": b"", "Trip/Deeper/": b"", "Trip/Deeper/later.jpg": b"added after the listing"}
    assert google.tree(parent) == {"Trip/": b"", "Trip/Deeper/": b"",
                                   "Trip/Deeper/later.jpg": b"added after the listing"}


def trip_with_a_doc(owner, google) -> tuple[str, str, str, str, dict]:
    parent = cloud_folder(google)
    trip = google.add("Trip", parent)
    photo = google.add("a.jpg", trip, b"synced")
    google.add("Plan", trip, b"", GOOGLE_DOC_TYPE)
    sub = google.add("Sub", trip)
    google.add("b.jpg", sub, b"synced too")
    connection = signed_in(owner, google)
    first_sync(owner, connection)
    return parent, trip, photo, sub, connection


def bin_trip_in_drive(owner, connection: dict) -> None:
    owner.post("/api/nodes/trash", json={"ids": [find_node(owner, connection["folder"]["id"], "Trip")["id"]]})


def settled_runs(owner, connection: dict, count: int = 3) -> dict:
    for _ in range(count):
        view = run(owner, connection["id"])
        assert view["lastStatus"] == "ok" and not view["lastErrors"], (view["lastErrors"], view["lastCounts"])
    return view


def test_a_cloud_edit_under_a_folder_deleted_in_drive_brings_the_folder_back(syncing, google):
    owner, _ = syncing
    parent, trip, photo, sub, connection = trip_with_a_doc(owner, google)
    google.files[photo]["content"] = b"edited in the cloud after the last sync"
    google.files[photo]["modifiedTime"] = "2031-01-01T00:00:00.000Z"
    bin_trip_in_drive(owner, connection)
    view = settled_runs(owner, connection)
    assert not any("stays in the cloud" in line for line in view["lastSkipped"]), view
    assert drive_files(owner, connection["folder"]["id"]) == {
        "Trip/": b"", "Trip/a.jpg": b"edited in the cloud after the last sync"}


def test_a_new_cloud_file_under_a_folder_deleted_in_drive_brings_the_folder_back(syncing, google):
    owner, _ = syncing
    parent, trip, photo, sub, connection = trip_with_a_doc(owner, google)
    google.add("new.jpg", sub, b"added in the cloud")
    bin_trip_in_drive(owner, connection)
    settled_runs(owner, connection)
    assert drive_files(owner, connection["folder"]["id"]) == {
        "Trip/": b"", "Trip/Sub/": b"", "Trip/Sub/new.jpg": b"added in the cloud"}


def test_files_added_later_to_a_kept_cloud_folder_still_arrive(syncing, google):
    owner, _ = syncing
    parent, trip, photo, sub, connection = trip_with_a_doc(owner, google)
    bin_trip_in_drive(owner, connection)
    view = settled_runs(owner, connection, count=1)
    assert any(line.startswith("'Trip'") and "stays in the cloud" in line for line in view["lastSkipped"]), view
    assert not any("cloud bin" in line for line in view["lastSkipped"]), view
    assert drive_files(owner, connection["folder"]["id"]) == {}
    google.add("later.jpg", trip, b"added in the cloud later")
    google.add("deep.jpg", google.add("Later", trip), b"deep")
    settled_runs(owner, connection, count=2)
    assert drive_files(owner, connection["folder"]["id"]) == {
        "Trip/": b"", "Trip/later.jpg": b"added in the cloud later", "Trip/Later/": b"", "Trip/Later/deep.jpg": b"deep"}


@pytest.mark.parametrize("with_a_doc", [True, False], ids=["kept for a Doc", "no Doc"])
@pytest.mark.parametrize("into", ["Trip", "Trip/Sub"])
def test_a_file_moved_in_the_cloud_into_a_folder_deleted_in_drive_ends_in_neither_bin(services, syncing, google,
                                                                                    with_a_doc, into):
    owner, _ = syncing
    parent = cloud_folder(google)
    trip = google.add("Trip", parent)
    google.add("a.jpg", trip, b"synced")
    if with_a_doc:
        google.add("Plan", trip, b"", GOOGLE_DOC_TYPE)
    sub = google.add("Sub", trip)
    google.add("b.jpg", sub, b"synced too")
    outside = google.add("outside.jpg", parent, b"synced at the top")
    connection = signed_in(owner, google)
    first_sync(owner, connection)
    google.files[outside]["parents"] = [sub if into == "Trip/Sub" else trip]
    bin_trip_in_drive(owner, connection)
    first = run(owner, connection["id"])
    move_error = f"'outside.jpg': the Drive folder for '{into}/outside.jpg' is not there"
    assert first["lastErrors"] in ([], [move_error]), first
    settled_runs(owner, connection, count=2)
    expected_drive = {"Trip/": b"", f"{into}/outside.jpg": b"synced at the top"}
    if into == "Trip/Sub":
        expected_drive["Trip/Sub/"] = b""
    assert drive_files(owner, connection["folder"]["id"]) == expected_drive
    expected_cloud = dict(expected_drive)
    if with_a_doc:
        expected_cloud["Trip/Plan"] = b""
    assert google.tree(parent) == expected_cloud
    assert binned_drive_file_names(services.store) == ["a.jpg", "b.jpg"]


@pytest.mark.parametrize("others", [2, 6], ids=["under the brake", "over the brake"])
def test_a_file_moved_out_of_a_folder_is_not_binned_with_it_when_its_drive_move_fails(services, syncing, google,
                                                                                     monkeypatch, others):
    owner, _ = syncing
    parent = cloud_folder(google)
    g_folder = google.add("G", parent)
    mover = google.add("x.jpg", g_folder, b"moved out, never deleted")
    other_names = [f"g-{number}.jpg" for number in range(others)]
    for number, name in enumerate(other_names):
        google.add(name, g_folder, bytes([65 + number]) * 20)
    for number in range(2):
        google.add(f"keep-{number}.jpg", parent, b"k" * (number + 1))
    connection = signed_in(owner, google)
    first_sync(owner, connection)
    google.files[mover]["parents"] = [parent]
    google.files[g_folder]["trashed"] = True

    def refused(self, entry, old_path, new_path):
        raise SyncError("Drive could not move it just now")

    monkeypatch.setattr(LocalSide, "move", refused)
    shown = preview(owner, connection["id"])
    if shown["binsHeld"]:
        expected = [("drive", f"G/{name}") for name in other_names]
        assert sorted((line["side"], line["path"]) for line in shown["binFiles"]) == expected
        answer = owner.post(f"/api/sync/connections/{connection['id']}/run",
                            json={"approveBins": shown["binApproval"]})
        assert answer.status_code == 202
        first = wait_until_idle(owner, connection["id"])
    else:
        first = run(owner, connection["id"])
    assert "x.jpg" not in binned_drive_file_names(services.store), first
    monkeypatch.undo()
    for _ in range(2):
        view = run_saying_yes_to_what_is_shown(owner, connection["id"])
    assert view["lastStatus"] == "ok" and not view["lastErrors"], view
    assert binned_drive_file_names(services.store) == other_names
    expected_tree = {"x.jpg": b"moved out, never deleted", "keep-0.jpg": b"k", "keep-1.jpg": b"kk"}
    assert drive_files(owner, connection["folder"]["id"]) == expected_tree
    assert google.tree(parent) == expected_tree


@pytest.mark.parametrize("others", [2, 6])
def test_a_folder_deleted_in_drive_after_a_cloud_file_in_it_was_put_back_is_not_kept_for_good(syncing, google, others):
    owner, _ = syncing
    parent = cloud_folder(google)
    g_folder = google.add("G", parent)
    first_copy = google.add("x.jpg", g_folder, b"put back with the same bytes")
    other_names = [f"g-{number}.jpg" for number in range(others)]
    for number, name in enumerate(other_names):
        google.add(name, g_folder, bytes([65 + number]) * 20)
    for number in range(2):
        google.add(f"keep-{number}.jpg", parent, b"k" * (number + 1))
    connection = signed_in(owner, google)
    first_sync(owner, connection)
    google.files[first_copy]["trashed"] = True
    google.files[first_copy]["parents"] = ["somewhere else"]
    google.add("x.jpg", g_folder, b"put back with the same bytes")
    owner.post("/api/nodes/trash", json={"ids": [find_node(owner, connection["folder"]["id"], "G")["id"]]})
    for _ in range(2):
        view = run_saying_yes_to_what_is_shown(owner, connection["id"])
        assert view["lastStatus"] == "ok" and not view["lastErrors"], view
    assert not any("not in the list" in line for line in view["lastSkipped"]), view
    assert run_saying_yes_to_what_is_shown(owner, connection["id"])["lastStatus"] == "ok"
    expected_tree = {"G/": b"", "G/x.jpg": b"put back with the same bytes", "keep-0.jpg": b"k", "keep-1.jpg": b"kk"}
    assert google.tree(parent) == expected_tree
    assert drive_files(owner, connection["folder"]["id"]) == expected_tree


def test_the_google_callback_needs_the_sign_in(syncing, google):
    owner, _ = syncing
    connection = connect(owner)
    query = sign_in_url(owner, connection)
    code = google.authorize(query, FIRST_EMAIL)
    answer = owner.client.get(f"/sync/google/callback?state={query['state']}&code={code}")
    assert answer.status_code == 401
    assert owner.get(f"/api/sync/connections/{connection['id']}").get_json()["needsSignIn"]
    assert google.refresh_tokens == {}
