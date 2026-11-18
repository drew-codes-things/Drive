import base64
import hashlib
import itertools
import re
import secrets
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

from flask import Flask, jsonify, request
from waitress import create_server

from drive.sync.connections import GoogleSetup
from drive.sync.google import FOLDER_TYPE, GoogleEndpoints

CLIENT_ID = "stub-client.apps.googleusercontent.com"
CLIENT_SECRET = "stub-client-secret"
ACCESS_SECONDS = 3599
EXPIRED_GRANT_TEXT = "Token has been expired or revoked."
PARENT_QUERY = re.compile(r"'((?:[^'\\]|\\.)*)' in parents")
NAME_QUERY = re.compile(r"name = '((?:[^'\\]|\\.)*)'")
TYPE_QUERY = re.compile(r"mimeType = '((?:[^'\\]|\\.)*)'")
CUT_SHORT_OF_ITS_LENGTH = "short of its length"
CUT_WITHOUT_A_LENGTH = "without a length"
HALF_DOWNLOAD = "half"
GARBLED_DOWNLOAD = "garbled"


def _unescape(text: str) -> str:
    return re.sub(r"\\(.)", r"\1", text)


def _error(status: int, message: str, reason: str = "error"):
    return jsonify({"error": {"code": status, "message": message, "errors": [{"reason": reason}]}}), status


class GoogleStub:
    def __init__(self):
        self.files: dict[str, dict] = {}
        self.codes: dict[str, dict] = {}
        self.refresh_tokens: dict[str, dict] = {}
        self.access_tokens: dict[str, str] = {}
        self.uploads: dict[str, dict] = {}
        self.revoked: list[str] = []
        self.page_size = 2
        self.rate_limit_next = 0
        self.upload_location_origin: str | None = None
        self.cut_listings: str | None = None
        self.download_fault: str | None = None
        self.base = ""
        self._ids = itertools.count(1)
        self.lock = threading.RLock()
        self.app = self._build()

    def endpoints(self) -> GoogleEndpoints:
        return GoogleEndpoints(authorize=f"{self.base}/o/oauth2/v2/auth", token=f"{self.base}/token",
                               revoke=f"{self.base}/revoke", api=f"{self.base}/drive/v3",
                               upload=f"{self.base}/upload/drive/v3")

    def setup(self) -> GoogleSetup:
        return GoogleSetup(CLIENT_ID, CLIENT_SECRET, self.endpoints())

    def authorize(self, url_query: dict, email: str, granted_scope: str | None = None) -> str:
        code = secrets.token_urlsafe(16)
        self.codes[code] = {"challenge": url_query["code_challenge"], "redirect": url_query["redirect_uri"],
                            "scope": granted_scope or url_query["scope"], "email": email}
        return code

    def new_id(self) -> str:
        return f"g{next(self._ids)}"

    def add(self, name: str, parent: str = "root", content: bytes | None = None, mime: str | None = None) -> str:
        file_id = self.new_id()
        self.files[file_id] = {"id": file_id, "name": name, "parents": [parent], "trashed": False,
                               "mimeType": mime or (FOLDER_TYPE if content is None else "text/plain"),
                               "content": content, "modifiedTime": self._now()}
        return file_id

    def find(self, name: str, parent: str = "root", trashed: bool = False) -> dict | None:
        for item in self.files.values():
            if item["name"] == name and parent in item["parents"] and item["trashed"] == trashed:
                return item
        return None

    def tree(self, parent: str = "root", prefix: str = "") -> dict[str, bytes]:
        found = {}
        for item in self.files.values():
            if parent not in item["parents"] or item["trashed"]:
                continue
            path = prefix + item["name"]
            if item["mimeType"] == FOLDER_TYPE:
                found[path + "/"] = b""
                found.update(self.tree(item["id"], path + "/"))
            elif item["content"] is not None:
                found[path] = item["content"]
        return found

    def expire_access_tokens(self) -> None:
        self.access_tokens.clear()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    @staticmethod
    def resource(item: dict) -> dict:
        answer = {key: item[key] for key in ("id", "name", "mimeType", "modifiedTime", "trashed")}
        if item["content"] is not None:
            answer["size"] = str(len(item["content"]))
            answer["md5Checksum"] = hashlib.md5(item["content"]).hexdigest()
        return answer

    def _grant(self, refresh_token: str) -> dict:
        access = secrets.token_urlsafe(16)
        self.access_tokens[access] = refresh_token
        return {"access_token": access, "expires_in": ACCESS_SECONDS, "token_type": "Bearer",
                "scope": self.refresh_tokens[refresh_token]["scope"]}

    def _signed_in(self) -> dict | None:
        header = request.headers.get("Authorization", "")
        refresh = self.access_tokens.get(header.removeprefix("Bearer "))
        if refresh is None or refresh in self.revoked:
            return None
        return self.refresh_tokens[refresh]

    def _build(self) -> Flask:
        app = Flask("google-stub")

        @app.before_request
        def guard():
            if request.path.startswith(("/drive/", "/upload/")):
                if self._signed_in() is None:
                    return _error(401, "Request had invalid authentication credentials.", "authError")
                if self.rate_limit_next:
                    self.rate_limit_next -= 1
                    return _error(403, "User Rate Limit Exceeded", "userRateLimitExceeded")
            return None

        @app.post("/token")
        def token():
            form = request.form
            if form.get("client_id") != CLIENT_ID or form.get("client_secret") != CLIENT_SECRET:
                return jsonify({"error": "invalid_client"}), 401
            if form.get("grant_type") == "authorization_code":
                code = self.codes.pop(form.get("code", ""), None)
                verifier = form.get("code_verifier", "")
                challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
                if code is None or code["challenge"] != challenge or code["redirect"] != form.get("redirect_uri"):
                    return jsonify({"error": "invalid_grant"}), 400
                refresh = secrets.token_urlsafe(16)
                self.refresh_tokens[refresh] = {"email": code["email"], "scope": code["scope"]}
                return jsonify({**self._grant(refresh), "refresh_token": refresh})
            if form.get("grant_type") == "refresh_token":
                refresh = form.get("refresh_token", "")
                if refresh not in self.refresh_tokens or refresh in self.revoked:
                    return jsonify({"error": "invalid_grant", "error_description": EXPIRED_GRANT_TEXT}), 400
                return jsonify(self._grant(refresh))
            return jsonify({"error": "unsupported_grant_type"}), 400

        @app.post("/revoke")
        def revoke():
            self.revoked.append(request.form.get("token", ""))
            return "", 200

        @app.get("/drive/v3/about")
        def about():
            return jsonify({"user": {"emailAddress": self._signed_in()["email"]}})

        @app.get("/drive/v3/files")
        def list_files():
            query = request.args.get("q", "")
            parent = _unescape(PARENT_QUERY.search(query).group(1))
            name = NAME_QUERY.search(query)
            mime = TYPE_QUERY.search(query)
            with self.lock:
                matches = [self.resource(item) for item in self.files.values()
                           if parent in item["parents"] and not item["trashed"]
                           and (name is None or item["name"] == _unescape(name.group(1)))
                           and (mime is None or item["mimeType"] == _unescape(mime.group(1)))]
            start = int(request.args.get("pageToken") or 0)
            page = matches[start:start + self.page_size]
            answer = {"files": page}
            if start + self.page_size < len(matches):
                answer["nextPageToken"] = str(start + self.page_size)
            return jsonify(answer)

        @app.post("/drive/v3/files")
        def create_folder():
            body = request.get_json()
            with self.lock:
                file_id = self.add(body["name"], body["parents"][0], None, body["mimeType"])
                return jsonify(self.resource(self.files[file_id]))

        @app.patch("/drive/v3/files/<file_id>")
        def update_metadata(file_id):
            item = self.files.get(file_id)
            if item is None:
                return _error(404, f"File not found: {file_id}.", "notFound")
            body = request.get_json()
            with self.lock:
                if "trashed" in body:
                    item["trashed"] = bool(body["trashed"])
                if "name" in body:
                    item["name"] = body["name"]
                if request.args.get("addParents"):
                    item["parents"] = [request.args["addParents"]]
            return jsonify(self.resource(item))

        @app.get("/drive/v3/files/<file_id>")
        def download(file_id):
            item = self.files.get(file_id)
            if item is None or request.args.get("alt") != "media":
                return _error(404, f"File not found: {file_id}.", "notFound")
            return item["content"], 200, {"Content-Type": "application/octet-stream"}

        def session_answer(metadata: dict, file_id: str | None):
            upload_id = secrets.token_urlsafe(8)
            self.uploads[upload_id] = {"metadata": metadata, "file_id": file_id}
            origin = self.upload_location_origin or self.base
            return "", 200, {"Location": f"{origin}/upload/drive/v3/files?uploadType=resumable&upload_id={upload_id}"}

        @app.post("/upload/drive/v3/files")
        def start_upload():
            return session_answer(request.get_json(), None)

        @app.patch("/upload/drive/v3/files/<file_id>")
        def start_update(file_id):
            return session_answer(request.get_json(silent=True) or {}, file_id)

        @app.put("/upload/drive/v3/files")
        def finish_upload():
            session = self.uploads.pop(request.args.get("upload_id", ""), None)
            if session is None:
                return _error(404, "No such upload session.", "notFound")
            with self.lock:
                if session["file_id"] is None:
                    metadata = session["metadata"]
                    file_id = self.add(metadata["name"], metadata["parents"][0], request.get_data(),
                                       "application/octet-stream")
                else:
                    file_id = session["file_id"]
                    self.files[file_id]["content"] = request.get_data()
                    self.files[file_id]["modifiedTime"] = self._now()
                return jsonify(self.resource(self.files[file_id]))

        return app


class FaultyGoogle:
    def __init__(self, stub: GoogleStub):
        self.stub = stub

    def __call__(self, environ, start_response):
        method, path = environ["REQUEST_METHOD"], environ["PATH_INFO"]
        query = environ.get("QUERY_STRING", "")
        is_listing = method == "GET" and path == "/drive/v3/files" and self.stub.cut_listings
        is_download = method == "GET" and "alt=media" in query and self.stub.download_fault
        if not (is_listing or is_download):
            return self.stub.app(environ, start_response)
        captured = {}

        def capture(status, headers, exc_info=None):
            captured["status"], captured["headers"] = status, headers

        body = b"".join(self.stub.app(environ, capture))
        headers = [(name, value) for name, value in captured["headers"] if name.lower() != "content-length"]
        if is_listing and self.stub.cut_listings == CUT_SHORT_OF_ITS_LENGTH:
            headers.append(("Content-Length", str(len(body))))
        start_response(captured["status"], headers)
        if is_download and self.stub.download_fault == GARBLED_DOWNLOAD:
            return [bytes(255 - byte for byte in body)]
        return [body[:len(body) // 2]]


@contextmanager
def running_google():
    stub = GoogleStub()
    server = create_server(FaultyGoogle(stub), host="127.0.0.1", port=0, threads=8)
    threading.Thread(target=server.run, daemon=True).start()
    stub.base = f"http://127.0.0.1:{server.effective_port}"
    try:
        yield stub
    finally:
        server.close()
        server.task_dispatcher.shutdown()
