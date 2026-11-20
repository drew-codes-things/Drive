import io
import zipfile

import pytest

from conftest import OWNER, SignedInClient, make_app, make_png
from drive.auth import PROXY_SECRET_HEADER
from drive.web import TEXT_PREVIEW_BYTES


def test_the_page_is_named_starfire_drive(owner):
    page = owner.get("/").get_data(as_text=True)
    assert "<title>Starfire Drive</title>" in page
    assert 'aria-label="Starfire Drive home"' in page
    assert 'Starfire <span class="brand-accent">Drive</span>' in page


def test_health_is_open_and_everything_else_needs_the_proxy_user(client):
    assert client.get("/api/health").get_json() == {"ok": True}
    assert client.get("/api/folders/0").status_code == 401
    assert client.get("/").status_code == 401
    assert client.get("/static/app.js").status_code == 401


def test_the_user_header_only_counts_from_a_trusted_proxy(client):
    headers = {"X-Forwarded-User": OWNER}
    answer = client.get("/api/folders/0", headers=headers, environ_base={"REMOTE_ADDR": "10.1.2.50"})
    assert answer.status_code == 401


def test_writes_need_the_csrf_token_and_the_same_origin(owner, client):
    body = {"parentId": 0, "name": "x"}
    assert client.post("/api/folders", json=body, headers=owner.headers).status_code == 403
    wrong = {**owner.headers, "X-CSRF-Token": "0" * 64}
    assert client.post("/api/folders", json=body, headers=wrong).status_code == 403
    cross = {**owner.write_headers, "Origin": "https://evil.example"}
    assert client.post("/api/folders", json=body, headers=cross).status_code == 403
    assert client.put("/api/uploads/" + "0" * 32 + "?offset=0", data=b"a", headers=owner.headers).status_code == 403
    assert owner.post("/api/folders", json=body).status_code == 201


def test_pages_and_api_carry_safe_headers(owner):
    page = owner.get("/")
    assert "script-src 'self'" in page.headers["Content-Security-Policy"]
    assert page.headers["X-Frame-Options"] == "DENY"
    assert page.headers["X-Content-Type-Options"] == "nosniff"
    assert owner.get("/api/folders/0").headers["Cache-Control"] == "no-store"


def test_downloads_are_attachments_that_never_run(owner):
    node = owner.upload("evil.html", b"<script>alert(1)</script>")
    for query in ("", "?inline=1"):
        answer = owner.get(f"/api/files/{node['id']}/content{query}")
        assert answer.headers["Content-Type"] == "application/octet-stream"
        assert answer.headers["Content-Disposition"].startswith("attachment;")
        assert "sandbox" in answer.headers["Content-Security-Policy"]
        assert answer.headers["X-Content-Type-Options"] == "nosniff"


def test_svg_is_never_shown_inline(owner):
    node = owner.upload("logo.svg", b"<svg xmlns='http://www.w3.org/2000/svg' onload='alert(1)'/>")
    answer = owner.get(f"/api/files/{node['id']}/content?inline=1")
    assert answer.headers["Content-Type"] == "application/octet-stream"
    assert node["preview"] == "text"


def test_download_names_cannot_break_the_header(owner):
    node = owner.upload('quote"semi;colon é.txt', b"x")
    disposition = owner.get(f"/api/files/{node['id']}/content").headers["Content-Disposition"]
    assert disposition.startswith('attachment; filename="quote_semi;colon _.txt"')
    assert "filename*=UTF-8''quote%22semi%3Bcolon%20%C3%A9.txt" in disposition


def test_real_images_preview_inline_in_a_sandbox(owner):
    node = owner.upload("dot.png", make_png())
    assert node["preview"] == "image"
    answer = owner.get(f"/api/files/{node['id']}/content?inline=1")
    assert answer.headers["Content-Type"] == "image/png"
    assert answer.headers["Content-Disposition"].startswith("inline;")
    assert "sandbox" in answer.headers["Content-Security-Policy"]
    assert answer.headers["X-Frame-Options"] == "SAMEORIGIN"


def test_a_fake_image_gets_no_preview(owner):
    node = owner.upload("cat.png", b"<html><script>alert(1)</script></html>")
    assert node["preview"] == "none"
    assert owner.get(f"/api/files/{node['id']}/content?inline=1").headers["Content-Type"] == \
        "application/octet-stream"


def test_an_image_bomb_gets_no_preview(owner):
    node = owner.upload("bomb.png", make_png(60000, 60000))
    assert node["preview"] == "none"


def test_pdf_audio_and_video_preview(owner):
    pdf = owner.upload("doc.pdf", b"%PDF-1.7\n%fake")
    assert pdf["preview"] == "pdf"
    assert owner.get(f"/api/files/{pdf['id']}/content?inline=1").headers["Content-Type"] == "application/pdf"
    not_pdf = owner.upload("doc2.pdf", b"<html>")
    assert not_pdf["preview"] == "none"
    assert owner.upload("song.mp3", b"ID3....")["preview"] == "audio"
    assert owner.upload("clip.mp4", b"\x00\x00\x00\x18ftypmp42")["preview"] == "video"


def test_video_supports_range_requests(owner):
    node = owner.upload("clip.mp4", bytes(range(256)) * 4)
    answer = owner.get(f"/api/files/{node['id']}/content?inline=1", headers={"Range": "bytes=10-19"})
    assert answer.status_code == 206
    assert answer.data == bytes(range(10, 20))


def test_text_previews_come_back_as_json_text(owner):
    node = owner.upload("page.html", b"<b>bold</b> \xff")
    preview = owner.get(f"/api/files/{node['id']}/text").get_json()
    assert preview == {"text": "<b>bold</b> �", "truncated": False}
    big = owner.upload("big.txt", b"a" * (TEXT_PREVIEW_BYTES + 10))
    preview = owner.get(f"/api/files/{big['id']}/text").get_json()
    assert preview["truncated"] and len(preview["text"]) == TEXT_PREVIEW_BYTES
    binary = owner.upload("blob.bin", b"\x00\x01")
    assert owner.get(f"/api/files/{binary['id']}/text").status_code == 400


def test_folder_zip_streams_the_tree(owner):
    folder = owner.folder("Trip")
    owner.upload("a.txt", b"alpha", folder["id"])
    sub = owner.folder("Day 2", folder["id"])
    owner.upload("b.txt", b"beta", sub["id"])
    owner.folder("Empty", folder["id"])
    answer = owner.get(f"/api/folders/{folder['id']}/zip")
    assert answer.headers["Content-Disposition"].startswith('attachment; filename="Trip.zip"')
    archive = zipfile.ZipFile(io.BytesIO(answer.data))
    assert sorted(archive.namelist()) == ["Day 2/", "Day 2/b.txt", "Empty/", "a.txt"]
    assert archive.read("Day 2/b.txt") == b"beta"
    assert archive.testzip() is None


def test_zip_entry_names_never_leave_the_folder(owner):
    folder = owner.folder("safe")
    owner.upload("..txt", b"x", folder["id"])
    archive = zipfile.ZipFile(io.BytesIO(owner.get(f"/api/folders/{folder['id']}/zip").data))
    for name in archive.namelist():
        assert not name.startswith("/") and ".." not in name.split("/")


def test_folder_zip_has_a_size_cap(services_factory):
    from conftest import SignedInClient, make_app
    small = SignedInClient(make_app(services_factory(zip_max_bytes=0)).test_client())
    folder = small.folder("big")
    small.upload("a.txt", b"12345", folder["id"])
    answer = small.get(f"/api/folders/{folder['id']}/zip")
    assert answer.status_code == 413
    assert "more than" in answer.get_json()["error"]


def test_rename_star_move_copy_trash_restore_through_the_api(owner):
    folder = owner.folder("box")
    node = owner.upload("a.txt", b"a")
    assert owner.patch(f"/api/nodes/{node['id']}", json={"name": "b.txt", "starred": True}).get_json()["starred"]
    assert owner.post("/api/nodes/move", json={"ids": [node["id"]], "targetId": folder["id"]}).status_code == 200
    copied = owner.post("/api/nodes/copy", json={"ids": [node["id"]], "targetId": 0}).get_json()["items"][0]
    assert copied["name"] == "b.txt"
    assert owner.post("/api/nodes/trash", json={"ids": [folder["id"]]}).get_json() == {"trashed": 1}
    assert [item["name"] for item in owner.get("/api/bin").get_json()["items"]] == ["box"]
    assert owner.get(f"/api/files/{node['id']}/content").status_code == 404
    owner.post("/api/nodes/restore", json={"ids": [folder["id"]]})
    assert owner.get(f"/api/files/{node['id']}/content").data == b"a"
    assert [item["name"] for item in owner.get("/api/starred").get_json()["items"]] == ["b.txt"]
    assert [item["name"] for item in owner.get("/api/search?q=B.T").get_json()["items"]] == ["b.txt", "b.txt"]


def test_breadcrumbs_follow_the_folders(owner):
    outer = owner.folder("outer")
    inner = owner.folder("inner", outer["id"])
    listing = owner.get(f"/api/folders/{inner['id']}").get_json()
    assert [step["name"] for step in listing["path"]] == ["outer", "inner"]


@pytest.mark.parametrize("path", ["/api/folders/99999999999999999999", "/api/nodes/9223372036854775808"])
def test_ids_beyond_the_database_are_not_found(owner, path):
    assert owner.get(path).status_code == 404


def test_session_reports_whether_share_links_are_on(owner):
    session = owner.get("/api/session").get_json()
    assert session["sharesEnabled"] is True
    assert session["binDays"] == 30


def test_a_csrf_token_with_odd_characters_is_refused_without_a_server_error(owner, client):
    odd = {**owner.headers, "X-CSRF-Token": "\u00e9" * 64}
    assert client.post("/api/folders", json={"parentId": 0, "name": "x"}, headers=odd).status_code == 403


def test_owner_writes_must_come_from_the_public_scheme(services):
    client = make_app(services, public_scheme="https").test_client()
    owner = SignedInClient(client)
    body = {"parentId": 0, "name": "x"}
    plain = client.post("/api/folders", json=body, headers={**owner.write_headers, "Origin": "http://localhost"})
    assert plain.status_code == 403
    secure = client.post("/api/folders", json=body, headers={**owner.write_headers, "Origin": "https://localhost"})
    assert secure.status_code == 201
    local = make_app(services).test_client()
    other_scheme = local.post("/api/folders", json={"parentId": 0, "name": "y"},
                              headers={**owner.write_headers, "Origin": "https://localhost"})
    assert other_scheme.status_code == 403


def test_with_a_proxy_secret_only_requests_carrying_it_act_as_the_owner(services):
    secret = "s" * 40
    client = make_app(services, proxy_secret=secret).test_client()
    headers = {"X-Forwarded-User": OWNER}
    assert client.get("/api/session", headers=headers).status_code == 401
    for wrong in ("x" * 40, "\u00e9" * 40, ""):
        assert client.get("/api/session", headers={**headers, PROXY_SECRET_HEADER: wrong}).status_code == 401
    assert client.get("/api/session", headers={**headers, PROXY_SECRET_HEADER: secret}).status_code == 200
    assert client.get("/api/health").status_code == 200


def test_unfinished_uploads_are_listed_so_they_can_be_cancelled(owner):
    started = owner.post("/api/uploads", json={"name": "half.bin", "size": 10, "parentId": 0}).get_json()
    owner.put(f"/api/uploads/{started['id']}?offset=0", data=b"abcd")
    listed = owner.get("/api/uploads").get_json()["uploads"]
    assert [(item["id"], item["name"], item["received"], item["size"]) for item in listed] == [
        (started["id"], "half.bin", 4, 10)]
    assert owner.delete(f"/api/uploads/{started['id']}").status_code == 204
    assert owner.get("/api/uploads").get_json()["uploads"] == []


def test_grid_thumbnails_are_small_jpegs_not_the_original(owner, services):
    from PIL import Image
    big = io.BytesIO()
    Image.new("RGB", (1600, 1200), (200, 30, 30)).save(big, "PNG")
    node = owner.upload("photo.png", big.getvalue())
    thumb = owner.get(f"/api/files/{node['id']}/thumb")
    assert thumb.status_code == 200
    assert thumb.headers["Content-Type"] == "image/jpeg"
    assert "sandbox" in thumb.headers["Content-Security-Policy"]
    with Image.open(io.BytesIO(thumb.data)) as small:
        assert max(small.size) <= 320
    assert len(thumb.data) < len(big.getvalue())
    assert owner.get(f"/api/nodes/{node['id']}").get_json()["openedAt"] is None
    text = owner.upload("a.txt", b"hello")
    assert owner.get(f"/api/files/{text['id']}/thumb").status_code == 404
    fake = owner.upload("fake.png", make_png(4, 4)[:30])
    assert owner.get(f"/api/files/{fake['id']}/thumb").status_code == 404


def test_a_zip_over_the_cap_can_be_checked_first_and_says_why(services_factory):
    services = services_factory(zip_max_bytes=10)
    owner = SignedInClient(make_app(services).test_client())
    folder = owner.folder("Big")
    owner.upload("a.bin", b"x" * 20, folder["id"])
    refused = owner.get(f"/api/folders/{folder['id']}/zip/check")
    assert refused.status_code == 413
    assert "more than" in refused.get_json()["error"]
    small = owner.folder("Small")
    owner.upload("b.bin", b"x" * 5, small["id"])
    assert owner.get(f"/api/folders/{small['id']}/zip/check").get_json() == {"bytes": 5, "entries": 1}
    link = owner.post("/api/shares", json={"nodeId": folder["id"]}).get_json()
    anonymous = make_app(services).test_client()
    shared = anonymous.get(f"/s/{link['token']}/zip/{folder['id']}/check")
    assert shared.status_code == 413
    assert anonymous.get(f"/s/{link['token']}/zip/{small['id']}/check").status_code == 404
