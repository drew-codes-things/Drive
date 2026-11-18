import http.client
import logging
import ssl
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .remote import RemoteRefused, RemoteUnreachable

REQUEST_TIMEOUT_SECONDS = 60
ATTEMPTS = 4
FIRST_PAUSE_SECONDS = 1.0
LONGEST_PAUSE_SECONDS = 30.0
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
ANSWER_LIMIT_BYTES = 32 * 1024 * 1024
COPY_BYTES = 1024 * 1024
USER_AGENT = "drive-sync"
SCHEMES = ("http", "https")
BODILESS_STATUSES = (204, 304)

log = logging.getLogger("drive.sync.http")


@dataclass(frozen=True)
class Answer:
    status: int
    headers: dict
    body: bytes = b""

    def header(self, name: str) -> str:
        return self.headers.get(name.lower(), "")


class TooLarge(RemoteRefused):
    pass


class _Retry(Exception):
    def __init__(self, reason: str, answer: Answer | None = None):
        super().__init__(reason)
        self.answer = answer


def _retry_after(answer: Answer) -> float | None:
    value = answer.header("Retry-After")
    if value.isdigit():
        return min(float(value), LONGEST_PAUSE_SECONDS)
    return None


def _headers(response: http.client.HTTPResponse) -> dict:
    return {name.lower(): value for name, value in response.getheaders()}


def _declared_length(headers: dict) -> int | None:
    value = headers.get("content-length", "")
    return int(value) if value.isdigit() else None


def _read_capped(response: http.client.HTTPResponse, headers: dict) -> bytes:
    body = response.read(ANSWER_LIMIT_BYTES + 1)
    if len(body) > ANSWER_LIMIT_BYTES:
        raise TooLarge(f"the server sent an answer over {ANSWER_LIMIT_BYTES // 1024 ** 2} MB")
    declared = _declared_length(headers)
    if declared is not None and len(body) < declared and response.status not in BODILESS_STATUSES \
            and not response.chunked:
        raise http.client.IncompleteRead(body, declared - len(body))
    return body


def _answer(response: http.client.HTTPResponse) -> Answer:
    headers = _headers(response)
    return Answer(response.status, headers, _read_capped(response, headers))


class HttpClient:
    def __init__(self, timeout: float = REQUEST_TIMEOUT_SECONDS, attempts: int = ATTEMPTS,
                 first_pause: float = FIRST_PAUSE_SECONDS, sleep=time.sleep):
        self.timeout = timeout
        self.attempts = attempts
        self.first_pause = first_pause
        self.sleep = sleep

    def _connection(self, url: str) -> tuple[http.client.HTTPConnection, str]:
        parts = urlsplit(url)
        if parts.scheme not in SCHEMES or not parts.hostname:
            raise RemoteRefused(f"cannot reach {parts.scheme or 'an address without a scheme'} addresses")
        target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        if parts.scheme == "https":
            connection = http.client.HTTPSConnection(parts.hostname, parts.port, timeout=self.timeout,
                                                     context=ssl.create_default_context())
        else:
            connection = http.client.HTTPConnection(parts.hostname, parts.port, timeout=self.timeout)
        return connection, target

    def _pause(self, attempt: int, answer: Answer | None) -> None:
        wait = _retry_after(answer) if answer else None
        if wait is None:
            wait = min(self.first_pause * 2 ** attempt, LONGEST_PAUSE_SECONDS)
        self.sleep(wait)

    def _with_retries(self, url: str, once, should_retry):
        host = urlsplit(url).hostname
        last_reason = ""
        for attempt in range(self.attempts):
            try:
                answer = once()
            except (OSError, http.client.HTTPException) as error:
                last_reason, retry_answer = type(error).__name__, None
            else:
                if answer.status not in RETRY_STATUSES and not should_retry(answer):
                    return answer
                last_reason, retry_answer = f"status {answer.status}", answer
            if attempt + 1 < self.attempts:
                log.info("request to %s failed (%s), trying again", host, last_reason)
                self._pause(attempt, retry_answer)
        raise RemoteUnreachable(f"lost the connection to {host} ({last_reason}, tried {self.attempts} times)")

    def call(self, method: str, url: str, headers: dict | None = None, body: bytes | None = None,
             upload: Path | None = None, should_retry=lambda answer: False) -> Answer:
        def once() -> Answer:
            connection, target = self._connection(url)
            sent = {"User-Agent": USER_AGENT, **(headers or {})}
            try:
                if upload is not None:
                    with open(upload, "rb") as handle:
                        sent["Content-Length"] = str(upload.stat().st_size)
                        connection.request(method, target, body=handle, headers=sent)
                        response = connection.getresponse()
                else:
                    sent.setdefault("Content-Length", str(len(body or b"")))
                    connection.request(method, target, body=body, headers=sent)
                    response = connection.getresponse()
                return _answer(response)
            finally:
                connection.close()

        return self._with_retries(url, once, should_retry)

    def download(self, url: str, headers: dict, target: Path, max_bytes: int,
                 should_retry=lambda answer: False) -> Answer:
        def once() -> Answer:
            connection, path = self._connection(url)
            try:
                connection.request("GET", path, headers={"User-Agent": USER_AGENT, **headers})
                response = connection.getresponse()
                if response.status != 200:
                    return _answer(response)
                answer = Answer(response.status, _headers(response))
                self._save(response, answer, target, max_bytes)
                return answer
            finally:
                connection.close()

        return self._with_retries(url, once, should_retry)

    @staticmethod
    def _save(response: http.client.HTTPResponse, answer: Answer, target: Path, max_bytes: int) -> None:
        declared = None if response.chunked else _declared_length(answer.headers)
        if declared is not None and declared > max_bytes:
            raise TooLarge(f"the file is {declared} bytes, more than the {max_bytes} allowed")
        received = 0
        with open(target, "wb") as handle:
            while True:
                piece = response.read(COPY_BYTES)
                if not piece:
                    break
                received += len(piece)
                if received > max_bytes:
                    raise TooLarge(f"the server kept sending past {max_bytes} bytes")
                handle.write(piece)
        if declared is not None and received != declared:
            raise http.client.IncompleteRead(b"", declared - received)
