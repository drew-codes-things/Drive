import dataclasses
import secrets
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import os
import xml.etree.ElementTree as ElementTree
from urllib.parse import unquote, urlsplit

from waitress import create_server
from wsgidav.wsgidav_app import WsgiDAVApp

from conftest import SignedInClient, make_app
from drive.sync.connections import GoogleSetup, SyncService
from drive.sync.http import HttpClient
from drive.vault import Vault, load_or_create_vault_key

WAIT_SECONDS = 60
POLL_SECONDS = 0.05
TEST_TIMEOUT_SECONDS = 5
PARTIAL_LENGTH = 1_000_000
SHORTER = "shorter"
LONGER = "longer"
EXTRA_BYTES = b" and some more"


class Faults:
    def __init__(self):
        self.down = False
        self.drop_downloads_after: int | None = None
        self.delay_seconds = 0.0
        self.download_change: str | None = None
        self.downloads = 0
        self.lock = threading.Lock()

    def heal(self) -> None:
        self.down, self.drop_downloads_after, self.delay_seconds = False, None, 0.0
        self.download_change = None


class FaultyApp:
    def __init__(self, app, faults: Faults):
        self.app = app
        self.faults = faults

    def __call__(self, environ, start_response):
        faults = self.faults
        if faults.delay_seconds:
            time.sleep(faults.delay_seconds)
        if faults.down:
            start_response("503 Service Unavailable", [("Content-Length", "0")])
            return [b""]
        if environ["REQUEST_METHOD"] == "GET" and faults.drop_downloads_after is not None:
            with faults.lock:
                faults.downloads += 1
                dropping = faults.downloads > faults.drop_downloads_after
            if dropping:
                faults.down = True
                start_response("200 OK", [("Content-Length", str(PARTIAL_LENGTH)),
                                          ("Content-Type", "application/octet-stream")])
                return [b"only the start of it"]
        if environ["REQUEST_METHOD"] == "GET" and faults.download_change:
            return self._changed_download(environ, start_response)
        return self.app(environ, start_response)

    def _changed_download(self, environ, start_response):
        captured = {}

        def capture(status, headers, exc_info=None):
            captured["status"], captured["headers"] = status, headers

        body = b"".join(self.app(environ, capture))
        headers = [(name, value) for name, value in captured["headers"] if name.lower() != "content-length"]
        start_response(captured["status"], headers)
        if self.faults.download_change == SHORTER:
            return [body[:len(body) // 2]]
        return [body + EXTRA_BYTES]


class InodeFileIds:
    def __init__(self, app, mount: str, root: Path):
        self.app = app
        self.mount = mount.rstrip("/")
        self.root = root

    def __call__(self, environ, start_response):
        if environ["REQUEST_METHOD"] != "PROPFIND":
            return self.app(environ, start_response)
        captured = {}

        def capture(status, headers, exc_info=None):
            captured["status"], captured["headers"] = status, headers

        body = b"".join(self.app(environ, capture))
        if captured["status"].startswith("207"):
            body = self._with_file_ids(body)
        headers = [(name, value) for name, value in captured["headers"] if name.lower() != "content-length"]
        start_response(captured["status"], headers + [("Content-Length", str(len(body)))])
        return [body]

    def _with_file_ids(self, body: bytes) -> bytes:
        tree = ElementTree.fromstring(body)
        for response in tree.iter("{DAV:}response"):
            href = unquote(urlsplit(response.findtext("{DAV:}href", "")).path)
            relative = href[len(self.mount):].strip("/")
            target = self.root / relative if relative else self.root
            if not target.exists():
                continue
            for propstat in response.findall("{DAV:}propstat"):
                prop = propstat.find("{DAV:}prop")
                if prop is not None and " 200 " in f" {propstat.findtext('{DAV:}status', '')} ":
                    ElementTree.SubElement(prop, "{http://owncloud.org/ns}fileid").text = str(os.stat(target).st_ino)
                    break
        return ElementTree.tostring(tree, xml_declaration=True, encoding="utf-8")


@dataclasses.dataclass
class DavServer:
    root: Path
    url: str
    origin: str
    username: str
    password: str
    faults: Faults


def dav_app(mount: str, root: Path, username: str, password: str):
    return WsgiDAVApp({
        "provider_mapping": {mount: str(root)},
        "http_authenticator": {"domain_controller": None, "accept_basic": True, "accept_digest": False,
                               "default_to_digest": False, "trusted_auth_header": None},
        "simple_dc": {"user_mapping": {"*": {username: {"password": password, "roles": []}}}},
        "verbose": 0, "logging": {"enable_loggers": []}, "lock_storage": True, "property_manager": True,
    })


@contextmanager
def running_dav(root: Path, mount: str = "/dav", file_ids: bool = False):
    root.mkdir(parents=True, exist_ok=True)
    username, password = "tester", secrets.token_urlsafe(18)
    faults = Faults()
    app = dav_app(mount, root, username, password)
    if file_ids:
        app = InodeFileIds(app, mount, root)
    server = create_server(FaultyApp(app, faults), host="127.0.0.1", port=0, threads=8)
    runner = threading.Thread(target=server.run, daemon=True)
    runner.start()
    origin = f"http://127.0.0.1:{server.effective_port}"
    try:
        yield DavServer(root, f"{origin}{mount}/", origin, username, password, faults)
    finally:
        server.close()
        server.task_dispatcher.shutdown()


def quick_http() -> HttpClient:
    return HttpClient(timeout=TEST_TIMEOUT_SECONDS, sleep=lambda seconds: None)


def attach_sync(services, tmp_path: Path, max_file_bytes: int = 1024 ** 3, google: GoogleSetup | None = None,
                clock=time.time, syncs_at_once: int = 2):
    vault = Vault(load_or_create_vault_key(tmp_path / "token.key"))
    sync = SyncService(services.store.database, services.store, services.blobs, services.disk, vault,
                       tmp_path / "sync-work", max_file_bytes, google=google, http_factory=quick_http, clock=clock,
                       syncs_at_once=syncs_at_once)
    with_sync = dataclasses.replace(services, sync=sync)
    return SignedInClient(make_app(with_sync).test_client()), sync


def wait_until_idle(owner, connection_id: int) -> dict:
    deadline = time.monotonic() + WAIT_SECONDS
    while time.monotonic() < deadline:
        view = owner.get(f"/api/sync/connections/{connection_id}").get_json()
        if view["activity"] is None:
            return view
        time.sleep(POLL_SECONDS)
    raise AssertionError(f"connection {connection_id} was still syncing after {WAIT_SECONDS} s")


def preview(owner, connection_id: int) -> dict:
    started = owner.post(f"/api/sync/connections/{connection_id}/preview")
    assert started.status_code == 202, started.get_json()
    wait_until_idle(owner, connection_id)
    return owner.get(f"/api/sync/connections/{connection_id}/preview").get_json()


def run(owner, connection_id: int) -> dict:
    started = owner.post(f"/api/sync/connections/{connection_id}/run")
    assert started.status_code == 202, started.get_json()
    return wait_until_idle(owner, connection_id)


def run_saying_yes_to_what_is_shown(owner, connection_id: int) -> dict:
    view = run(owner, connection_id)
    if view["lastStatus"] != "held":
        return view
    answer = owner.post(f"/api/sync/connections/{connection_id}/run", json={"approveBins": view["held"]["approval"]})
    assert answer.status_code == 202, answer.get_json()
    return wait_until_idle(owner, connection_id)


def drive_files(owner, folder_id: int, prefix: str = "") -> dict[str, bytes]:
    found = {}
    for item in owner.get(f"/api/folders/{folder_id}").get_json()["items"]:
        path = f"{prefix}{item['name']}"
        if item["kind"] == "folder":
            found[path + "/"] = b""
            found.update(drive_files(owner, item["id"], path + "/"))
        else:
            found[path] = owner.get(f"/api/files/{item['id']}/content").data
    return found


def disk_files(root: Path, skip_bin: bool = True) -> dict[str, bytes]:
    found = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if skip_bin and relative.startswith(".drive-sync-bin"):
            continue
        found[relative + ("/" if path.is_dir() else "")] = b"" if path.is_dir() else path.read_bytes()
    return found


def binned_drive_file_names(store) -> list[str]:
    with store.database.connect() as connection:
        rows = connection.execute("SELECT name FROM nodes WHERE in_bin = 1 AND kind = 'file'").fetchall()
    return sorted(row["name"] for row in rows)


def find_node(owner, folder_id: int, name: str) -> dict:
    for item in owner.get(f"/api/folders/{folder_id}").get_json()["items"]:
        if item["name"] == name:
            return item
    raise AssertionError(f"{name!r} is not in folder {folder_id}")
