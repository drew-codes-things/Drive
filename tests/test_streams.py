import os
import socket
import threading
import time

import pytest
from waitress.server import create_server

from conftest import SignedInClient, make_app
from drive.__main__ import WAITRESS_THREADS
from drive.streams import SHARE_STREAM_LIMIT, StreamGuard

BIG_FILE_BYTES = 8 * 1024 * 1024
SMALL_HIGH_WATERMARK = 256 * 1024
WAIT_LIMIT_SECONDS = 15


class FakeServer:
    def __init__(self):
        self.triggers = 0

    def pull_trigger(self):
        self.triggers += 1


class FakeChannel:
    def __init__(self):
        self.outbuf_lock = threading.Condition()
        self.connected = True
        self.will_close = False
        self.server = FakeServer()

    def check_client_disconnected(self):
        return not self.connected


def shared_file(services, content: bytes):
    client = make_app(services).test_client()
    owner = SignedInClient(client)
    node = owner.upload("big.bin", content)
    link = owner.post("/api/shares", json={"nodeId": node["id"]}).get_json()
    return client, owner, node, link


def test_share_downloads_beyond_the_limit_get_503_until_one_closes(services_factory):
    guarded = services_factory(streams=StreamGuard(share_limit=2))
    client, owner, node, link = shared_file(guarded, b"x" * 1000)
    path = f"/s/{link['token']}/file/{node['id']}"
    first = client.get(path, buffered=False)
    second = client.get(path, buffered=False)
    assert (first.status_code, second.status_code) == (200, 200)
    third = client.get(path, buffered=False)
    assert third.status_code == 503
    assert "try again" in third.get_json()["error"]
    owner_download = owner.get(f"/api/files/{node['id']}/content")
    assert owner_download.status_code == 200
    first.close()
    fourth = client.get(path)
    assert fourth.status_code == 200
    assert fourth.data == b"x" * 1000
    for response in (second, fourth, owner_download):
        response.close()
    assert guarded.streams.active_count() == 0


def test_a_failed_share_download_gives_its_slot_back(services_factory):
    guarded = services_factory(zip_max_bytes=10, streams=StreamGuard(share_limit=1))
    client, owner, node, link = shared_file(guarded, b"y" * 100)
    folder = owner.folder("Box")
    owner.post("/api/nodes/move", json={"ids": [node["id"]], "targetId": folder["id"]})
    folder_link = owner.post("/api/shares", json={"nodeId": folder["id"]}).get_json()
    for _ in range(3):
        assert client.get(f"/s/{folder_link['token']}/zip/{folder['id']}").status_code == 413
    assert client.get(f"/s/{link['token']}/file/{node['id']}").status_code == 200


def test_share_streams_leave_threads_for_the_owner():
    assert SHARE_STREAM_LIMIT < WAITRESS_THREADS


def test_a_stream_with_no_progress_past_the_send_timeout_is_disconnected(clock):
    guard = StreamGuard(share_limit=1, send_timeout=60, clock=clock)
    channel = FakeChannel()
    stream = guard.watch(iter([b"a", b"b"]), {"waitress.client_disconnected": channel.check_client_disconnected})
    assert next(iter(stream)) == b"a"
    clock.advance(59)
    assert guard.stop_stalled() == 0
    assert channel.connected
    clock.advance(2)
    assert guard.stop_stalled() == 1
    assert not channel.connected
    assert channel.will_close
    assert channel.server.triggers == 1
    stream.close()
    assert guard.active_count() == 0


def test_a_stream_outside_waitress_is_left_alone(clock):
    guard = StreamGuard(share_limit=1, send_timeout=1, clock=clock)
    guard.watch(iter([b"a"]), {})
    clock.advance(5)
    assert guard.stop_stalled() == 0


@pytest.fixture
def live_server(services_factory):
    guard = StreamGuard(share_limit=1, send_timeout=1)
    guarded = services_factory(streams=guard)
    app = make_app(guarded)
    server = create_server(app, host="127.0.0.1", port=0, threads=2,
                           outbuf_high_watermark=SMALL_HIGH_WATERMARK)
    runner = threading.Thread(target=server.run, daemon=True)
    runner.start()
    stop = threading.Event()
    watcher = threading.Thread(target=guard.keep_watching, args=(stop, 0.2), daemon=True)
    watcher.start()
    yield guarded, app, int(server.effective_port)
    stop.set()
    server.close()


def test_a_client_that_never_reads_cannot_hold_a_thread_forever(live_server):
    services, app, port = live_server
    client, owner, node, link = shared_file(services, os.urandom(BIG_FILE_BYTES))
    sleeper = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sleeper.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    sleeper.connect(("127.0.0.1", port))
    sleeper.sendall(f"GET /s/{link['token']}/file/{node['id']} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode())
    deadline = time.monotonic() + WAIT_LIMIT_SECONDS
    while services.streams.active_count() == 0 and time.monotonic() < deadline:
        time.sleep(0.05)
    assert services.streams.active_count() == 1
    while services.streams.active_count() and time.monotonic() < deadline:
        time.sleep(0.1)
    assert services.streams.active_count() == 0
    sleeper.close()
    with socket.create_connection(("127.0.0.1", port), timeout=WAIT_LIMIT_SECONDS) as again:
        again.sendall(f"GET /api/health HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n".encode())
        assert again.recv(64).startswith(b"HTTP/1.1 200")
