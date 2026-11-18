import struct
import zlib

import pytest

START_TIME = 1_800_000_000.0
OWNER = "owner"
SECRET = b"k" * 32
GIB = 1024 ** 3


class FakeClock:
    def __init__(self, start: float = START_TIME):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeDisk:
    def __init__(self, free: int = 100 * GIB):
        self.free = free

    def __call__(self) -> int:
        return self.free


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def free_space():
    return FakeDisk()


@pytest.fixture
def services_factory(tmp_path, clock, free_space):
    from drive.blobs import BlobStore
    from drive.db import Database
    from drive.disk import DiskGuard
    from drive.shares import ShareStore
    from drive.store import DriveStore
    from drive.streams import StreamGuard
    from drive.thumbnails import ThumbnailMaker
    from drive.uploads import UploadManager
    from drive.web import Services

    def build(shares_enabled: bool = True, zip_max_bytes: int = GIB, max_file_bytes: int = 4 * GIB,
              min_free_bytes: int = 15 * GIB, streams=None) -> Services:
        database = Database(tmp_path / "drive.db")
        blobs = BlobStore(tmp_path / "blobs")
        store = DriveStore(database, blobs, clock=clock)
        disk = DiskGuard(tmp_path, min_free_bytes, free_space=free_space)
        uploads = UploadManager(database, store, blobs, disk, tmp_path / "uploads", max_file_bytes, clock=clock)
        shares = ShareStore(database, store, SECRET, clock=clock)
        return Services(store=store, blobs=blobs, uploads=uploads, shares=shares, disk=disk,
                        shares_enabled=shares_enabled, zip_max_bytes=zip_max_bytes,
                        streams=streams or StreamGuard(),
                        thumbnails=ThumbnailMaker(tmp_path / "thumbs", blobs))

    return build


@pytest.fixture
def services(services_factory):
    return services_factory()


@pytest.fixture
def store(services):
    return services.store


def make_app(services, public_scheme=None, proxy_secret=""):
    from drive.auth import ProxyAuth
    from drive.web import create_app
    auth = ProxyAuth(trusted_proxies=("127.0.0.1", "::1"), user_header="X-Forwarded-User", proxy_secret=proxy_secret)
    flask_app = create_app(services, auth, SECRET, public_scheme)
    flask_app.testing = True
    return flask_app


@pytest.fixture
def app(services):
    return make_app(services)


@pytest.fixture
def client(app):
    return app.test_client()


class SignedInClient:
    def __init__(self, client):
        self.client = client
        self.headers = {"X-Forwarded-User": OWNER}
        session = client.get("/api/session", headers=self.headers).get_json()
        self.write_headers = {**self.headers, "X-CSRF-Token": session["csrfToken"]}

    def get(self, path, headers=None, **kwargs):
        return self.client.get(path, headers={**self.headers, **(headers or {})}, **kwargs)

    def post(self, path, json=None, **kwargs):
        return self.client.post(path, json=json, headers=self.write_headers, **kwargs)

    def patch(self, path, json=None, **kwargs):
        return self.client.patch(path, json=json, headers=self.write_headers, **kwargs)

    def put(self, path, data=b"", **kwargs):
        return self.client.put(path, data=data, headers=self.write_headers, **kwargs)

    def delete(self, path, **kwargs):
        return self.client.delete(path, headers=self.write_headers, **kwargs)

    def upload(self, name: str, content: bytes, parent_id: int = 0, folders=None) -> dict:
        body = {"name": name, "size": len(content), "parentId": parent_id}
        if folders is not None:
            body["folders"] = folders
        started = self.post("/api/uploads", json=body)
        assert started.status_code == 201, started.get_json()
        upload = started.get_json()
        step = upload["chunkBytes"]
        for offset in range(0, len(content), step):
            answer = self.put(f"/api/uploads/{upload['id']}?offset={offset}", data=content[offset:offset + step])
            assert answer.status_code == 200, answer.get_json()
        finished = self.post(f"/api/uploads/{upload['id']}/finish")
        assert finished.status_code == 200, finished.get_json()
        return finished.get_json()

    def folder(self, name: str, parent_id: int = 0) -> dict:
        answer = self.post("/api/folders", json={"parentId": parent_id, "name": name})
        assert answer.status_code == 201, answer.get_json()
        return answer.get_json()


@pytest.fixture
def owner(client):
    return SignedInClient(client)


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def make_png(width: int = 1, height: int = 1) -> bytes:
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    rows = b"".join(b"\x00" + b"\xe5\x38\x3b" * width for _ in range(min(height, 2)))
    return (b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", header) + _png_chunk(b"IDAT", zlib.compress(rows))
            + _png_chunk(b"IEND", b""))
