import logging
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, urlsplit

from flask import Flask, Response, abort, g, jsonify, redirect, request, send_file, send_from_directory
from werkzeug.exceptions import HTTPException

from . import __version__
from .auth import csrf_matches, csrf_token
from .blobs import BlobStore
from .conversions import ConversionManager
from .disk import DiskGuard
from .errors import DriveError, NotFound
from .filetypes import INLINE_TYPES, OCTET_STREAM, PDF, TEXT
from .shares import MISSING_SHARE_TEXT, UNLOCK_SECONDS, ShareStore, is_token
from .signin import SIGN_IN_PATHS, PasswordAuth, add_sign_in_pages
from .store import BIN_DAYS, LARGEST_DATABASE_INTEGER, DriveStore, as_folder_id
from .streams import StreamGuard
from .sync.connections import SyncService
from .thumbnails import ThumbnailBusy, ThumbnailMaker
from .uploads import CHUNK_BYTES, UploadManager
from .web_convert import add_conversion_routes
from .web_sync import add_sync_routes
from .zipstream import check_zip_size, stream_zip

STATIC_DIR = Path(__file__).resolve().parent / "static"
APP_NAME = "Starfire Drive"
MAX_REQUEST_BYTES = CHUNK_BYTES + 64 * 1024
TEXT_PREVIEW_BYTES = 512 * 1024
THUMB_RETRY_AFTER_SECONDS = 1
CSRF_HEADER = "X-CSRF-Token"
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")
PUBLIC_PATHS = ("/api/health",)
SHARE_PREFIX = "/s/"
UNLOCK_COOKIE = "drive_share"
SHARE_ASSETS = frozenset({"app.css", "share.js", "icon.svg", "fonts/ibm-plex-sans-latin-400-normal.woff2",
                          "fonts/ibm-plex-sans-latin-500-normal.woff2", "fonts/ibm-plex-sans-latin-600-normal.woff2",
                          "fonts/ibm-plex-mono-latin-400-normal.woff2"})
PUBLIC_NODE_FIELDS = ("id", "name", "kind", "size", "preview", "modifiedAt")
SIGNED_OUT_TEXT = "Sign in through the main sign-in page first."
NESTED_TOO_DEEPLY_TEXT = "that JSON is nested too deeply"
WRONG_HOST_TEXT = ("this app does not answer on that host name (AUTH_MODE=local answers on localhost, 127.0.0.1 or "
                   "[::1] at its own port; the own sign-in answers on the names in ALLOWED_HOSTS, or with it empty on "
                   "localhost, IP addresses and this machine's name)")
SHARES_OFF_TEXT = "share links are switched off on this server"
MISDIRECTED_REQUEST = 421
CONTENT_SECURITY_POLICY = "; ".join((
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self'",
    "font-src 'self'",
    "img-src 'self' data: blob:",
    "media-src 'self'",
    "frame-src 'self'",
    "connect-src 'self'",
    "object-src 'none'",
    "frame-ancestors 'none'",
    "base-uri 'none'",
    "form-action 'self'",
))
MEDIA_SECURITY_POLICY = "default-src 'none'; img-src 'self'; media-src 'self'; style-src 'unsafe-inline'; " \
                        "frame-ancestors 'self'; sandbox"
PDF_SECURITY_POLICY = "frame-ancestors 'self'"
DOWNLOAD_SECURITY_POLICY = "default-src 'none'; frame-ancestors 'none'; sandbox"

log = logging.getLogger("drive.web")


@dataclass(frozen=True)
class Services:
    store: DriveStore
    blobs: BlobStore
    uploads: UploadManager
    shares: ShareStore
    disk: DiskGuard
    shares_enabled: bool
    zip_max_bytes: int
    streams: StreamGuard = field(default_factory=StreamGuard)
    thumbnails: ThumbnailMaker | None = None
    conversions: ConversionManager | None = None
    sync: SyncService | None = None


def _error(message: str, status: int):
    return jsonify({"error": message}), status


def _body() -> dict:
    try:
        data = request.get_json(silent=True)
    except RecursionError as error:
        raise DriveError(NESTED_TOO_DEEPLY_TEXT) from error
    if not isinstance(data, dict):
        raise DriveError("send a JSON object")
    return data


def _is_same_origin(public_scheme: str | None) -> bool:
    origin = request.headers.get("Origin")
    if origin is None:
        return True
    parts = urlsplit(origin)
    return parts.scheme == (public_scheme or request.scheme) and parts.netloc == request.host


def _id_argument(name: str) -> int | None:
    raw = request.args.get(name)
    if raw is None:
        return None
    if not raw.isascii() or not raw.isdigit() or int(raw) > LARGEST_DATABASE_INTEGER:
        raise NotFound()
    return int(raw)


def _wants_inline() -> bool:
    return request.args.get("inline") == "1"


def _content_disposition(kind: str, name: str) -> str:
    ascii_name = name.encode("ascii", "replace").decode().replace('"', "_").replace("?", "_")
    return f"{kind}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name, safe='')}"


def file_response(blobs: BlobStore, record: dict, inline: bool) -> Response:
    is_inline = inline and record["mime"] in INLINE_TYPES
    mimetype = record["mime"] if is_inline else OCTET_STREAM
    response = send_file(blobs.path(record["blob"]), mimetype=mimetype, conditional=True, etag=True,
                         last_modified=record["modifiedAt"], max_age=0)
    response.headers["Content-Disposition"] = _content_disposition("inline" if is_inline else "attachment",
                                                                   record["name"])
    response.headers["Cache-Control"] = "private, no-cache"
    if not is_inline:
        response.headers["Content-Security-Policy"] = DOWNLOAD_SECURITY_POLICY
    elif record["mime"] == PDF:
        response.headers["Content-Security-Policy"] = PDF_SECURITY_POLICY
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
    else:
        response.headers["Content-Security-Policy"] = MEDIA_SECURITY_POLICY
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
    return response


def text_preview(blobs: BlobStore, record: dict) -> dict:
    if record["mime"] != TEXT:
        raise DriveError("this file has no text preview; download it instead")
    with open(blobs.path(record["blob"]), "rb") as handle:
        raw = handle.read(TEXT_PREVIEW_BYTES)
    return {"text": raw.decode("utf-8", errors="replace"), "truncated": record["size"] > TEXT_PREVIEW_BYTES}


def zip_response(services: Services, folder_id: int) -> Response:
    name, entries = services.store.zip_entries(folder_id)
    check_zip_size(entries, services.zip_max_bytes)
    response = Response(stream_zip(entries, services.blobs), mimetype="application/zip", direct_passthrough=True)
    response.headers["Content-Disposition"] = _content_disposition("attachment", f"{name}.zip")
    response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Security-Policy"] = DOWNLOAD_SECURITY_POLICY
    return response


def zip_check(services: Services, folder_id: int) -> dict:
    _, entries = services.store.zip_entries(folder_id)
    return {"bytes": check_zip_size(entries, services.zip_max_bytes), "entries": len(entries)}


def thumbnail_not_ready(busy: ThumbnailBusy) -> Response:
    response = jsonify({"error": str(busy)})
    response.status_code = busy.status
    response.headers["Retry-After"] = str(THUMB_RETRY_AFTER_SECONDS)
    response.headers["Cache-Control"] = "no-store"
    return response


def thumbnail_response(services: Services, record: dict) -> Response:
    try:
        thumb = services.thumbnails.thumbnail(record) if services.thumbnails else None
    except ThumbnailBusy as busy:
        return thumbnail_not_ready(busy)
    if thumb is None:
        raise NotFound("this file has no thumbnail")
    response = send_file(thumb, mimetype="image/jpeg", conditional=True, etag=True, max_age=0)
    response.headers["Cache-Control"] = "private, no-cache"
    response.headers["Content-Security-Policy"] = MEDIA_SECURITY_POLICY
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    return response


def public_node(node: dict) -> dict:
    return {field: node[field] for field in PUBLIC_NODE_FIELDS}


def _static_page(name: str) -> Response:
    response = send_from_directory(STATIC_DIR, name, max_age=0)
    response.headers["Cache-Control"] = "no-cache"
    return response


def create_app(services: Services, auth, secret: bytes, public_scheme: str | None = None) -> Flask:
    app = Flask(__name__, static_folder=None)
    app.config["MAX_CONTENT_LENGTH"] = MAX_REQUEST_BYTES
    has_own_sign_in = isinstance(auth, PasswordAuth)
    if has_own_sign_in:
        add_sign_in_pages(app, auth, APP_NAME)
    app.url_map.merge_slashes = False
    store, uploads, shares = services.store, services.uploads, services.shares

    @app.url_value_preprocessor
    def refuse_ids_beyond_the_database(endpoint, values):
        if any(isinstance(value, int) and value > LARGEST_DATABASE_INTEGER for value in (values or {}).values()):
            abort(404)

    @app.before_request
    def identify_and_guard_writes():
        if request.path in PUBLIC_PATHS:
            return None
        if not auth.accepts_host(request.host):
            return _error(WRONG_HOST_TEXT, MISDIRECTED_REQUEST)
        if has_own_sign_in and request.path in SIGN_IN_PATHS:
            return None
        if request.path.startswith(SHARE_PREFIX):
            return guard_share_request()
        user = auth.user_for(request.remote_addr or "", request.headers)
        if user is None:
            if request.path.startswith("/api/"):
                return _error("sign in first", 401)
            if has_own_sign_in:
                return redirect(auth.entry_path(), code=303)
            return Response(SIGNED_OUT_TEXT, status=401, mimetype="text/plain")
        g.user = user
        if request.method in SAFE_METHODS:
            return None
        if not _is_same_origin(public_scheme):
            return _error("this request came from another site", 403)
        if not csrf_matches(secret, user, request.headers.get(CSRF_HEADER)):
            return _error("missing or wrong CSRF token, reload the page", 403)
        return None

    def guard_share_request():
        g.user = None
        if not services.shares_enabled:
            return _error(SHARES_OFF_TEXT, 404)
        if request.method not in SAFE_METHODS and not _is_same_origin(public_scheme):
            return _error("this request came from another site", 403)
        return None

    @app.after_request
    def add_security_headers(response: Response) -> Response:
        response.headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        if request.path.startswith(SHARE_PREFIX):
            response.headers["X-Robots-Tag"] = "noindex, nofollow"
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.errorhandler(DriveError)
    def drive_refused(error: DriveError):
        return _error(str(error), error.status)

    @app.errorhandler(HTTPException)
    def http_error(error: HTTPException):
        if error.code == 413:
            return _error("that request is too large", 413)
        if request.path.startswith(("/api/", SHARE_PREFIX)):
            return _error(error.description or error.name, error.code or 500)
        return error

    @app.get("/")
    def index():
        return _static_page("index.html")

    @app.get("/static/<path:name>")
    def static_file(name: str):
        return send_from_directory(STATIC_DIR, name, max_age=0)

    @app.get("/api/health")
    def health():
        if not store.database.is_reachable():
            log.error("health check: the database could not be read at %s", store.database.path)
            return jsonify({"ok": False, "error": "database unavailable"}), 503
        return jsonify({"ok": True})

    @app.get("/api/session")
    def session_state():
        return jsonify({"user": g.user, "csrfToken": csrf_token(secret, g.user), "version": __version__,
                        "sharesEnabled": services.shares_enabled, "binDays": BIN_DAYS, "chunkBytes": CHUNK_BYTES})

    @app.get("/api/storage")
    def storage():
        disk = services.disk
        return jsonify({**store.usage(), "freeBytes": disk.free_bytes(), "diskBytes": disk.total_bytes(),
                        "minFreeBytes": disk.min_free_bytes, "uploadRoomBytes": disk.room_for_uploads()})

    @app.get("/api/folders/<int:folder_id>")
    def list_folder(folder_id: int):
        return jsonify(store.list_folder(folder_id, request.args.get("sort", "name"),
                                         request.args.get("order", "asc")))

    @app.post("/api/folders")
    def create_folder():
        body = _body()
        return jsonify(store.create_folder(as_folder_id(body.get("parentId")), body.get("name"))), 201

    @app.get("/api/nodes/<int:node_id>")
    def read_node(node_id: int):
        node = store.get(node_id)
        return jsonify({**node, "path": store.path(node["parentId"])})

    @app.patch("/api/nodes/<int:node_id>")
    def update_node(node_id: int):
        body = _body()
        node = store.get(node_id)
        if "name" in body:
            node = store.rename(node_id, body["name"])
        if "starred" in body:
            node = store.set_starred(node_id, body["starred"])
        return jsonify(node)

    @app.post("/api/nodes/move")
    def move_nodes():
        body = _body()
        return jsonify({"items": store.move(body.get("ids"), body.get("targetId"))})

    @app.post("/api/nodes/copy")
    def copy_nodes():
        body = _body()
        if services.blobs.copies_take_space():
            services.disk.check_write(store.copy_size(body.get("ids")))
        return jsonify({"items": store.copy(body.get("ids"), body.get("targetId"))})

    @app.post("/api/nodes/trash")
    def trash_nodes():
        return jsonify({"trashed": store.trash(_body().get("ids"))})

    @app.post("/api/nodes/restore")
    def restore_nodes():
        return jsonify({"items": store.restore(_body().get("ids"))})

    @app.post("/api/nodes/delete")
    def delete_nodes():
        return jsonify({"deleted": store.delete_forever(_body().get("ids"))})

    @app.get("/api/bin")
    def bin_items():
        return jsonify({"items": store.bin_items()})

    @app.post("/api/bin/empty")
    def empty_bin():
        return jsonify({"deleted": store.empty_bin()})

    @app.get("/api/starred")
    def starred_items():
        return jsonify({"items": store.starred()})

    @app.get("/api/recent")
    def recent_items():
        return jsonify({"items": store.recent()})

    @app.get("/api/search")
    def search_items():
        return jsonify({"items": store.search(request.args.get("q", ""))})

    @app.get("/api/files/<int:node_id>/content")
    def file_content(node_id: int):
        record = store.file_record(node_id)
        store.mark_opened(node_id)
        return services.streams.watch_owner(file_response(services.blobs, record, _wants_inline()), request.environ)

    @app.get("/api/files/<int:node_id>/thumb")
    def file_thumb(node_id: int):
        return thumbnail_response(services, store.file_record(node_id))

    @app.get("/api/files/<int:node_id>/text")
    def file_text(node_id: int):
        record = store.file_record(node_id)
        store.mark_opened(node_id)
        return jsonify(text_preview(services.blobs, record))

    @app.get("/api/folders/<int:folder_id>/zip")
    def folder_zip(folder_id: int):
        return services.streams.watch_owner(zip_response(services, folder_id), request.environ)

    @app.get("/api/uploads")
    def unfinished_uploads():
        return jsonify({"uploads": uploads.unfinished()})

    @app.get("/api/folders/<int:folder_id>/zip/check")
    def folder_zip_check(folder_id: int):
        return jsonify(zip_check(services, folder_id))

    @app.post("/api/uploads")
    def start_upload():
        return jsonify(uploads.start(_body())), 201

    @app.get("/api/uploads/<upload_id>")
    def upload_status(upload_id: str):
        return jsonify(uploads.status(upload_id))

    @app.put("/api/uploads/<upload_id>")
    def upload_chunk(upload_id: str):
        offset = request.args.get("offset", "")
        if not offset.isascii() or not offset.isdigit() or len(offset) > 15:
            raise DriveError("offset must be a whole number of bytes")
        length = request.content_length
        if length is None:
            raise DriveError("send the chunk with a Content-Length")
        return jsonify(uploads.append(upload_id, int(offset), request.stream, length))

    @app.post("/api/uploads/<upload_id>/finish")
    def finish_upload(upload_id: str):
        return jsonify(uploads.finish(upload_id))

    @app.delete("/api/uploads/<upload_id>")
    def cancel_upload(upload_id: str):
        uploads.cancel(upload_id)
        return "", 204

    @app.get("/api/shares")
    def list_shares():
        node_id = _id_argument("nodeId")
        return jsonify({"shares": shares.list(node_id), "enabled": services.shares_enabled})

    @app.post("/api/shares")
    def create_share():
        if not services.shares_enabled:
            raise DriveError(SHARES_OFF_TEXT + "; set SHARES_ENABLED=true to use them", 403)
        return jsonify(shares.create(_body())), 201

    @app.delete("/api/shares/<int:share_id>")
    def revoke_share(share_id: int):
        shares.revoke(share_id)
        return "", 204

    def open_share(token: str) -> dict:
        share = shares.resolve(token)
        if not shares.is_unlocked(share, request.cookies.get(UNLOCK_COOKIE)):
            raise DriveError("this link needs its password", 401)
        return share

    def shared_node(token: str, node_id: int) -> dict:
        share = open_share(token)
        if not shares.contains(share, node_id):
            raise NotFound()
        return share

    @app.get("/s/static/<path:name>")
    def share_asset(name: str):
        if name not in SHARE_ASSETS:
            raise NotFound("no such file")
        return send_from_directory(STATIC_DIR, name, max_age=0)

    @app.get("/s/<token>")
    def share_page(token: str):
        if not is_token(token):
            return Response(MISSING_SHARE_TEXT, status=404, mimetype="text/plain")
        return _static_page("share.html")

    @app.get("/s/<token>/info")
    def share_info(token: str):
        share = shares.resolve(token)
        if not shares.is_unlocked(share, request.cookies.get(UNLOCK_COOKIE)):
            return jsonify({"locked": True})
        root = share["node"]
        answer = {"locked": False, "root": public_node(root), "expiresAt": share["expiresAt"]}
        if root["kind"] == "file":
            return jsonify(answer)
        asked_folder = _id_argument("folder")
        folder_id = root["id"] if asked_folder is None else asked_folder
        if not shares.contains(share, folder_id):
            raise NotFound()
        listing = store.list_folder(folder_id, request.args.get("sort", "name"), request.args.get("order", "asc"))
        return jsonify({**answer, "folder": listing["folder"], "path": store.path(folder_id, stop_at=root["id"]),
                        "items": [public_node(item) for item in listing["items"]]})

    @app.post("/s/<token>/unlock")
    def share_unlock(token: str):
        share = shares.resolve(token)
        value = shares.unlock(share, _body().get("password"))
        response = jsonify({"locked": False})
        response.set_cookie(UNLOCK_COOKIE, value, max_age=UNLOCK_SECONDS, path=f"{SHARE_PREFIX}{token}",
                            httponly=True, samesite="Strict", secure=public_scheme == "https" or request.is_secure)
        return response

    @app.get("/s/<token>/file/<int:node_id>")
    def share_file(token: str, node_id: int):
        shared_node(token, node_id)
        record = store.file_record(node_id)
        return services.streams.watch_share(lambda: file_response(services.blobs, record, _wants_inline()),
                                            request.environ)

    @app.get("/s/<token>/text/<int:node_id>")
    def share_text(token: str, node_id: int):
        shared_node(token, node_id)
        return jsonify(text_preview(services.blobs, store.file_record(node_id)))

    @app.get("/s/<token>/zip/<int:node_id>/check")
    def share_zip_check(token: str, node_id: int):
        shared_node(token, node_id)
        return jsonify(zip_check(services, node_id))

    @app.get("/s/<token>/zip/<int:node_id>")
    def share_zip(token: str, node_id: int):
        shared_node(token, node_id)
        return services.streams.watch_share(lambda: zip_response(services, node_id), request.environ)

    add_conversion_routes(app, services.conversions, _body)
    add_sync_routes(app, services.sync, _body, public_scheme)
    return app
