import io
import secrets
import threading
import zipfile

import pytest

from conftest import OWNER, SignedInClient, make_app
from drive.errors import DriveError
from drive.shares import FAILED_ATTEMPT_LIMIT, FAILED_ATTEMPT_WINDOW_SECONDS, UNLOCK_SECONDS


def share(owner, node_id: int, **extra) -> dict:
    answer = owner.post("/api/shares", json={"nodeId": node_id, **extra})
    assert answer.status_code == 201, answer.get_json()
    return answer.get_json()


@pytest.fixture
def tree(owner):
    shared = owner.folder("Shared")
    inside = owner.upload("inside.txt", b"inside", shared["id"])
    sub = owner.folder("Sub", shared["id"])
    deep = owner.upload("deep.txt", b"deep", sub["id"])
    private = owner.folder("Private")
    secret = owner.upload("secret.txt", b"secret", private["id"])
    loose = owner.upload("loose.txt", b"loose")
    return {"shared": shared, "inside": inside, "sub": sub, "deep": deep, "private": private, "secret": secret,
            "loose": loose}


@pytest.fixture
def anonymous(app):
    return app.test_client()


def test_with_share_links_off_nothing_under_s_answers(services_factory):
    services = services_factory(shares_enabled=False)
    client = make_app(services).test_client()
    owner = SignedInClient(client)
    node = owner.upload("a.txt", b"a")
    refused = owner.post("/api/shares", json={"nodeId": node["id"]})
    assert refused.status_code == 403
    assert "switched off" in refused.get_json()["error"]
    made = services.shares.create({"nodeId": node["id"]})
    for path in (f"/s/{made['token']}", f"/s/{made['token']}/info", f"/s/{made['token']}/file/{node['id']}",
                 "/s/static/share.js"):
        assert client.get(path).status_code == 404
    assert client.post(f"/s/{made['token']}/unlock", json={"password": "x"}).status_code == 404
    assert owner.get("/api/session").get_json()["sharesEnabled"] is False


def test_a_shared_folder_lists_and_downloads_without_signing_in(owner, anonymous, tree):
    link = share(owner, tree["shared"]["id"])
    token = link["token"]
    assert len(token) == 43
    page = anonymous.get(f"/s/{token}")
    assert page.status_code == 200
    assert page.headers["X-Robots-Tag"] == "noindex, nofollow"
    assert page.headers["Referrer-Policy"] == "no-referrer"
    info = anonymous.get(f"/s/{token}/info").get_json()
    assert info["root"]["name"] == "Shared"
    assert [item["name"] for item in info["items"]] == ["Sub", "inside.txt"]
    assert all(set(item) == {"id", "name", "kind", "size", "preview", "modifiedAt"} for item in info["items"])
    sub = anonymous.get(f"/s/{token}/info?folder={tree['sub']['id']}").get_json()
    assert [step["name"] for step in sub["path"]] == ["Shared", "Sub"]
    assert anonymous.get(f"/s/{token}/file/{tree['deep']['id']}").data == b"deep"
    archive = zipfile.ZipFile(io.BytesIO(anonymous.get(f"/s/{token}/zip/{tree['shared']['id']}").data))
    assert sorted(archive.namelist()) == ["Sub/", "Sub/deep.txt", "inside.txt"]


def test_a_shared_file_reaches_only_that_file(owner, anonymous, tree):
    token = share(owner, tree["inside"]["id"])["token"]
    info = anonymous.get(f"/s/{token}/info").get_json()
    assert info["root"]["name"] == "inside.txt" and "items" not in info
    assert anonymous.get(f"/s/{token}/file/{tree['inside']['id']}").data == b"inside"
    for other in ("deep", "secret", "loose", "shared"):
        assert anonymous.get(f"/s/{token}/file/{tree[other]['id']}").status_code == 404
    assert anonymous.get(f"/s/{token}/zip/{tree['shared']['id']}").status_code == 404


def test_nothing_outside_the_shared_folder_is_reachable(owner, anonymous, tree):
    token = share(owner, tree["sub"]["id"])["token"]
    outside = [tree["inside"], tree["secret"], tree["loose"]]
    for node in outside:
        assert anonymous.get(f"/s/{token}/file/{node['id']}").status_code == 404
        assert anonymous.get(f"/s/{token}/text/{node['id']}").status_code == 404
    for folder_id in (tree["shared"]["id"], tree["private"]["id"], 0, -1, 999999):
        assert anonymous.get(f"/s/{token}/info?folder={folder_id}").status_code == 404
        assert anonymous.get(f"/s/{token}/zip/{folder_id}").status_code == 404


@pytest.mark.parametrize("path", [
    "/s/{token}/../../api/folders/0",
    "/s/{token}/%2e%2e/%2e%2e/api/folders/0",
    "/s/../api/folders/0",
    "/s/{token}/file/../../../api/files/{secret}/content",
    "/s/{token}/file/{secret}/../../../../api/storage",
    "/s/static/../../api/folders/0",
    "/s/static/../web.py",
    "/s/static/%2e%2e/web.py",
    "/s/static/index.html",
    "/s/static/app.js",
])
def test_path_tricks_under_s_reach_nothing_of_the_owner(owner, anonymous, tree, path):
    token = share(owner, tree["sub"]["id"])["token"]
    answer = anonymous.get(path.format(token=token, secret=tree["secret"]["id"]))
    assert answer.status_code == 404
    assert b"secret" not in answer.data
    assert b"Private" not in answer.data


def test_the_owner_api_ignores_share_cookies_and_needs_the_proxy_user(owner, anonymous, tree):
    token = share(owner, tree["sub"]["id"], password="hunter22")["token"]
    anonymous.post(f"/s/{token}/unlock", json={"password": "hunter22"})
    assert anonymous.get("/api/folders/0").status_code == 401
    assert anonymous.get(f"/api/files/{tree['secret']['id']}/content").status_code == 401


def test_share_routes_never_act_as_the_owner(owner, anonymous, tree):
    token = share(owner, tree["sub"]["id"])["token"]
    headers = {"X-Forwarded-User": OWNER}
    assert anonymous.get(f"/s/{token}/file/{tree['secret']['id']}", headers=headers).status_code == 404
    for method in ("put", "patch", "delete"):
        assert getattr(anonymous, method)(f"/s/{token}/file/{tree['deep']['id']}").status_code == 405


def test_guessed_tokens_find_nothing(owner, anonymous, tree):
    real = share(owner, tree["sub"]["id"])["token"]
    guesses = [secrets.token_urlsafe(32) for _ in range(200)]
    guesses += [real[:-1] + ("A" if real[-1] != "A" else "B"), real[:-1], real + "A", real.upper(), real.lower(),
                "0" * 43, "%" * 43, "a" * 42 + "=", ""]
    for guess in guesses:
        if guess == real:
            continue
        assert anonymous.get(f"/s/{guess}/info").status_code == 404
        assert anonymous.get(f"/s/{guess}/file/{tree['deep']['id']}").status_code == 404


def test_expired_links_stop_working(owner, anonymous, tree, clock):
    token = share(owner, tree["sub"]["id"], expiresAt=clock() + 3600)["token"]
    assert anonymous.get(f"/s/{token}/info").status_code == 200
    clock.advance(3601)
    assert anonymous.get(f"/s/{token}/info").status_code == 410
    assert anonymous.get(f"/s/{token}/file/{tree['deep']['id']}").status_code == 410
    listed = owner.get("/api/shares").get_json()["shares"]
    assert listed[0]["expired"] is True


def test_expiry_must_be_in_the_future(owner, tree, clock):
    for bad in (clock() - 1, clock() + 11 * 365 * 86400, "tomorrow", True):
        assert owner.post("/api/shares", json={"nodeId": tree["sub"]["id"], "expiresAt": bad}).status_code == 400


def test_revoked_links_stop_working_at_once(owner, anonymous, tree):
    link = share(owner, tree["sub"]["id"])
    assert anonymous.get(f"/s/{link['token']}/info").status_code == 200
    assert owner.delete(f"/api/shares/{link['id']}").status_code == 204
    assert anonymous.get(f"/s/{link['token']}/info").status_code == 404
    assert anonymous.get(f"/s/{link['token']}/file/{tree['deep']['id']}").status_code == 404
    assert owner.delete(f"/api/shares/{link['id']}").status_code == 404
    assert owner.get("/api/shares").get_json()["shares"] == []


def test_links_to_binned_items_stop_and_deleted_items_drop_their_links(owner, anonymous, tree):
    link = share(owner, tree["sub"]["id"])
    owner.post("/api/nodes/trash", json={"ids": [tree["sub"]["id"]]})
    assert anonymous.get(f"/s/{link['token']}/info").status_code == 404
    owner.post("/api/nodes/restore", json={"ids": [tree["sub"]["id"]]})
    assert anonymous.get(f"/s/{link['token']}/info").status_code == 200
    owner.post("/api/nodes/trash", json={"ids": [tree["deep"]["id"]]})
    assert anonymous.get(f"/s/{link['token']}/file/{tree['deep']['id']}").status_code == 404
    owner.post("/api/nodes/trash", json={"ids": [tree["sub"]["id"]]})
    owner.post("/api/bin/empty")
    assert anonymous.get(f"/s/{link['token']}/info").status_code == 404


def test_moving_a_file_out_of_a_shared_folder_takes_it_out_of_the_link(owner, anonymous, tree):
    token = share(owner, tree["sub"]["id"])["token"]
    owner.post("/api/nodes/move", json={"ids": [tree["deep"]["id"]], "targetId": 0})
    assert anonymous.get(f"/s/{token}/file/{tree['deep']['id']}").status_code == 404


def test_password_links_stay_locked_until_the_password_is_given(owner, anonymous, tree):
    token = share(owner, tree["sub"]["id"], password="correct horse")["token"]
    assert anonymous.get(f"/s/{token}/info").get_json() == {"locked": True}
    assert anonymous.get(f"/s/{token}/file/{tree['deep']['id']}").status_code == 401
    assert anonymous.post(f"/s/{token}/unlock", json={"password": "wrong"}).status_code == 403
    unlocked = anonymous.post(f"/s/{token}/unlock", json={"password": "correct horse"})
    assert unlocked.status_code == 200
    cookie = unlocked.headers["Set-Cookie"]
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie and f"Path=/s/{token}" in cookie
    assert anonymous.get(f"/s/{token}/file/{tree['deep']['id']}").data == b"deep"


def test_an_unlock_for_one_link_does_not_open_another(owner, app, tree):
    first = share(owner, tree["sub"]["id"], password="first-pass")["token"]
    second = share(owner, tree["shared"]["id"], password="second-pass")["token"]
    visitor = app.test_client()
    value = visitor.post(f"/s/{first}/unlock", json={"password": "first-pass"}).headers["Set-Cookie"]
    stolen = value.split(";")[0].split("=", 1)[1]
    other = app.test_client()
    other.set_cookie("drive_share", stolen, path=f"/s/{second}")
    assert other.get(f"/s/{second}/info").get_json() == {"locked": True}


def test_forged_and_old_unlock_cookies_are_refused(owner, app, tree, clock):
    token = share(owner, tree["sub"]["id"], password="pass-word")["token"]
    for forged in ("9999999999.abc", "x.y", "1" * 3000 + ".a", "", "." * 50, "١٢.ab"):
        visitor = app.test_client()
        visitor.set_cookie("drive_share", forged, path=f"/s/{token}")
        assert visitor.get(f"/s/{token}/info").get_json() == {"locked": True}
    visitor = app.test_client()
    visitor.post(f"/s/{token}/unlock", json={"password": "pass-word"})
    clock.advance(UNLOCK_SECONDS + 1)
    assert visitor.get(f"/s/{token}/info").get_json() == {"locked": True}


def test_password_guessing_is_slowed_down(owner, anonymous, tree, clock):
    token = share(owner, tree["sub"]["id"], password="right-one")["token"]
    for _ in range(FAILED_ATTEMPT_LIMIT):
        assert anonymous.post(f"/s/{token}/unlock", json={"password": "guess"}).status_code == 403
    blocked = anonymous.post(f"/s/{token}/unlock", json={"password": "right-one"})
    assert blocked.status_code == 429
    clock.advance(FAILED_ATTEMPT_WINDOW_SECONDS + 1)
    assert anonymous.post(f"/s/{token}/unlock", json={"password": "right-one"}).status_code == 200


def test_unlock_refuses_other_sites_and_odd_bodies(owner, anonymous, tree):
    token = share(owner, tree["sub"]["id"], password="right-one")["token"]
    cross = anonymous.post(f"/s/{token}/unlock", json={"password": "right-one"},
                           headers={"Origin": "https://evil.example"})
    assert cross.status_code == 403
    for body in ({"password": 5}, {"password": ""}, {"password": "x" * 201}, []):
        assert anonymous.post(f"/s/{token}/unlock", json=body).status_code in (400, 403)


def test_link_passwords_are_stored_hashed(owner, services, tree):
    share(owner, tree["sub"]["id"], password="plain-text-pass")
    with services.store.database.connect() as connection:
        stored = connection.execute("SELECT password_hash FROM shares").fetchone()[0]
    assert stored.startswith("scrypt$") and "plain-text-pass" not in stored


def test_share_creation_checks_its_input(owner, tree):
    for body in ({"nodeId": "1"}, {"nodeId": 0}, {"nodeId": 999999}, {"nodeId": tree["sub"]["id"], "password": "abc"}):
        assert owner.post("/api/shares", json=body).status_code in (400, 404)


def test_share_links_list_per_item(owner, tree):
    share(owner, tree["sub"]["id"])
    share(owner, tree["inside"]["id"], password="pass-word")
    listed = owner.get(f"/api/shares?nodeId={tree['inside']['id']}").get_json()["shares"]
    assert len(listed) == 1 and listed[0]["hasPassword"] is True
    assert "password_hash" not in listed[0] and "passwordHash" not in listed[0]


def test_shared_previews_keep_the_safe_headers(owner, anonymous, tree):
    page = owner.upload("page.html", b"<script>alert(1)</script>", tree["sub"]["id"])
    token = share(owner, tree["sub"]["id"])["token"]
    answer = anonymous.get(f"/s/{token}/file/{page['id']}?inline=1")
    assert answer.headers["Content-Type"] == "application/octet-stream"
    assert answer.headers["Content-Disposition"].startswith("attachment;")
    assert anonymous.get(f"/s/{token}/text/{page['id']}").get_json()["text"] == "<script>alert(1)</script>"


def test_share_assets_are_an_allow_list(anonymous):
    assert anonymous.get("/s/static/share.js").status_code == 200
    assert anonymous.get("/s/static/app.css").status_code == 200
    assert anonymous.get("/s/static/fonts/ibm-plex-sans-latin-400-normal.woff2").status_code == 200
    assert anonymous.get("/s/static/app.js").status_code == 404


def test_the_unlock_cookie_is_secure_when_the_public_site_is_https(services, tree):
    client = make_app(services, public_scheme="https").test_client()
    owner = SignedInClient(client)
    token = share(owner, tree["sub"]["id"], password="pass-word")["token"]
    unlocked = client.post(f"/s/{token}/unlock", json={"password": "pass-word"},
                           headers={"Origin": "https://localhost"})
    assert unlocked.status_code == 200
    assert "Secure" in unlocked.headers["Set-Cookie"]


def test_parallel_wrong_passwords_never_get_past_the_limit(owner, services, tree):
    token = share(owner, tree["sub"]["id"], password="right-one")["token"]
    found = services.shares.resolve(token)
    outcomes = []
    gate = threading.Barrier(FAILED_ATTEMPT_LIMIT * 2)

    def guess():
        gate.wait()
        try:
            services.shares.unlock(found, "wrong-guess")
        except DriveError as error:
            outcomes.append(error.status)

    workers = [threading.Thread(target=guess) for _ in range(FAILED_ATTEMPT_LIMIT * 2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    assert outcomes.count(403) == FAILED_ATTEMPT_LIMIT
    assert outcomes.count(429) == FAILED_ATTEMPT_LIMIT


def test_odd_cookies_and_huge_ids_are_refused_without_a_server_error(owner, anonymous, tree):
    token = share(owner, tree["sub"]["id"], password="pass-word")["token"]
    anonymous.set_cookie("drive_share", "9999999999.éé", path=f"/s/{token}")
    assert anonymous.get(f"/s/{token}/info").get_json() == {"locked": True}
    open_token = share(owner, tree["sub"]["id"])["token"]
    for folder in ("9" * 30, "-1", "١", "1e5"):
        assert anonymous.get(f"/s/{open_token}/info?folder={folder}").status_code == 404
    assert owner.get(f"/api/shares?nodeId={'9' * 30}").status_code == 404


def test_links_on_binned_items_stay_listed_as_in_the_bin_and_can_be_revoked(owner, anonymous, tree):
    link = share(owner, tree["sub"]["id"])
    owner.post("/api/nodes/trash", json={"ids": [tree["shared"]["id"]]})
    listed = owner.get("/api/shares").get_json()["shares"]
    assert [(item["id"], item["inBin"]) for item in listed] == [(link["id"], True)]
    assert owner.get(f"/api/shares?nodeId={tree['sub']['id']}").get_json()["shares"][0]["inBin"] is True
    assert owner.delete(f"/api/shares/{link['id']}").status_code == 204
    owner.post("/api/nodes/restore", json={"ids": [tree["shared"]["id"]]})
    assert anonymous.get(f"/s/{link['token']}/info").status_code == 404
    assert owner.get("/api/shares").get_json()["shares"] == []


def test_share_writes_from_the_same_host_on_another_scheme_are_refused(services, tree):
    client = make_app(services, public_scheme="https").test_client()
    owner = SignedInClient(client)
    token = share(owner, tree["sub"]["id"], password="pass-word")["token"]
    plain = client.post(f"/s/{token}/unlock", json={"password": "pass-word"}, headers={"Origin": "http://localhost"})
    assert plain.status_code == 403
