from flask import Flask, Response, jsonify, redirect, request

from .errors import DriveError, NotFound
from .sync.connections import INTERVALS, PROVIDERS, STALE_SIGN_IN_TEXT, SyncBusy, SyncService

CALLBACK_PATH = "/sync/google/callback"
BUSY_RETRY_AFTER_SECONDS = 30
AFTER_SIGN_IN = "/#/sync"
HELD_FROM_DIGITS = 9


def _is_plain_count(text: str) -> bool:
    return 0 < len(text) <= HELD_FROM_DIGITS and text.isascii() and text.isdigit()


def _service(sync: SyncService | None) -> SyncService:
    if sync is None:
        raise DriveError("cloud sync is switched off on this server", 404)
    return sync


def _busy(busy: SyncBusy):
    response = jsonify({"error": str(busy)})
    response.status_code = busy.status
    response.headers["Retry-After"] = str(BUSY_RETRY_AFTER_SECONDS)
    return response


def add_sync_routes(app: Flask, sync: SyncService | None, read_body, public_scheme: str | None) -> None:
    def start(connection_id: int, mode: str, approve_bins=None):
        try:
            return jsonify(_service(sync).start(connection_id, mode, approve_bins)), 202
        except SyncBusy as busy:
            return _busy(busy)

    @app.get("/api/sync")
    def sync_overview():
        service = _service(sync)
        return jsonify({"connections": service.views(), "googleReady": service.google is not None,
                        "providers": PROVIDERS, "intervals": INTERVALS})

    @app.post("/api/sync/connections")
    def create_connection():
        return jsonify(_service(sync).create(read_body())), 201

    @app.get("/api/sync/connections/<int:connection_id>")
    def read_connection(connection_id: int):
        return jsonify(_service(sync).get(connection_id))

    @app.patch("/api/sync/connections/<int:connection_id>")
    def update_connection(connection_id: int):
        return jsonify(_service(sync).update(connection_id, read_body()))

    @app.delete("/api/sync/connections/<int:connection_id>")
    def delete_connection(connection_id: int):
        _service(sync).delete(connection_id)
        return "", 204

    @app.post("/api/sync/connections/<int:connection_id>/preview")
    def preview_connection(connection_id: int):
        return start(connection_id, "preview")

    @app.get("/api/sync/connections/<int:connection_id>/preview")
    def preview_result(connection_id: int):
        return jsonify(_service(sync).preview_result(connection_id))

    @app.get("/api/sync/connections/<int:connection_id>/held")
    def held_bins(connection_id: int):
        start = request.args.get("from", "0")
        if not _is_plain_count(start):
            raise DriveError(f"from must be a whole number of files to skip, at most {HELD_FROM_DIGITS} digits")
        return jsonify(_service(sync).held_page(connection_id, int(start)))

    @app.post("/api/sync/connections/<int:connection_id>/run")
    def run_connection(connection_id: int):
        body = read_body() if request.content_length else {}
        if "allowBins" in body:
            raise DriveError("allowBins is no longer accepted: send approveBins with the approval shown with the "
                             "list of files to bin")
        return start(connection_id, "run", body.get("approveBins"))

    @app.post("/api/sync/connections/<int:connection_id>/google")
    def google_sign_in(connection_id: int):
        redirect_uri = f"{public_scheme or request.scheme}://{request.host}{CALLBACK_PATH}"
        return jsonify({"url": _service(sync).google_start(connection_id, redirect_uri)})

    @app.get(CALLBACK_PATH)
    def google_callback():
        try:
            _service(sync).google_finish(request.args.get("state"), request.args.get("code"),
                                         request.args.get("error"))
        except NotFound:
            return Response(STALE_SIGN_IN_TEXT, status=400, mimetype="text/plain")
        return redirect(AFTER_SIGN_IN, code=303)
