import http.server
import threading
from contextlib import contextmanager

import pytest

from drive.sync.http import HttpClient
from drive.sync.remote import RemoteUnreachable

FULL_BODY = b'{"files": [{"id": "1", "name": "a.txt"}, {"id": "2", "name": "b.txt"}]}'
SENT_BYTES = 30
MISSING_BYTES = 500


class ChunkedWithALength(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Content-Length", str(len(FULL_BODY) + MISSING_BYTES))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(b"%x\r\n%s\r\n0\r\n\r\n" % (len(FULL_BODY), FULL_BODY))

    def log_message(self, *args):
        pass


class CutAnswers(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    requests = 0

    def do_GET(self):
        type(self).requests += 1
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        if self.path == "/short":
            self.send_header("Content-Length", str(len(FULL_BODY) + MISSING_BYTES))
            self.end_headers()
            self.wfile.write(FULL_BODY[:SENT_BYTES])
            return
        self.send_header("Content-Length", str(len(FULL_BODY)))
        self.end_headers()
        self.wfile.write(FULL_BODY)

    def log_message(self, *args):
        pass


@contextmanager
def cutting_server(handler=CutAnswers):
    CutAnswers.requests = 0
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def test_an_answer_shorter_than_its_declared_length_is_a_lost_connection():
    client = HttpClient(timeout=5, sleep=lambda seconds: None)
    with cutting_server() as origin:
        with pytest.raises(RemoteUnreachable, match="lost the connection"):
            client.call("GET", origin + "/short")
        assert CutAnswers.requests == client.attempts
        assert client.call("GET", origin + "/whole").body == FULL_BODY


def test_a_whole_chunked_answer_is_read_even_with_a_stray_length():
    client = HttpClient(timeout=5, sleep=lambda seconds: None)
    with cutting_server(ChunkedWithALength) as origin:
        assert client.call("GET", origin + "/").body == FULL_BODY


def test_a_whole_chunked_download_is_kept_even_with_a_stray_length(tmp_path):
    client = HttpClient(timeout=5, sleep=lambda seconds: None)
    target = tmp_path / "file"
    with cutting_server(ChunkedWithALength) as origin:
        client.download(origin + "/", {}, target, len(FULL_BODY))
    assert target.read_bytes() == FULL_BODY
