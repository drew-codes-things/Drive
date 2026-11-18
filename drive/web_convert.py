from flask import Flask, jsonify

from .conversions import ConversionBusy, ConversionManager
from .errors import DriveError, NotFound

BUSY_RETRY_AFTER_SECONDS = 30
JOB_ID_LENGTH = 24


def busy_response(busy: ConversionBusy):
    response = jsonify({"error": str(busy)})
    response.status_code = busy.status
    response.headers["Retry-After"] = str(BUSY_RETRY_AFTER_SECONDS)
    return response


def _manager(conversions: ConversionManager | None) -> ConversionManager:
    if conversions is None:
        raise DriveError("conversions are switched off on this server", 404)
    return conversions


def add_conversion_routes(app: Flask, conversions: ConversionManager | None, read_body) -> None:
    @app.get("/api/files/<int:node_id>/conversions")
    def conversion_options(node_id: int):
        return jsonify(_manager(conversions).options(node_id))

    @app.post("/api/files/<int:node_id>/conversions")
    def start_conversion(node_id: int):
        try:
            job = _manager(conversions).start(node_id, read_body().get("target"))
        except ConversionBusy as busy:
            return busy_response(busy)
        return jsonify(job), 202

    @app.get("/api/conversions")
    def list_conversions():
        return jsonify({"jobs": _manager(conversions).jobs()})

    @app.get("/api/conversions/<job_id>")
    def conversion_status(job_id: str):
        if len(job_id) != JOB_ID_LENGTH or not job_id.isascii() or not job_id.isalnum():
            raise NotFound("that conversion is not known here")
        return jsonify(_manager(conversions).job(job_id))

