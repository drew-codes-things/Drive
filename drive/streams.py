import logging
import threading
import time
from typing import Callable, Iterable, Iterator

from .errors import DriveError

SHARE_STREAM_LIMIT = 8
SEND_TIMEOUT_SECONDS = 120
WATCH_INTERVAL_SECONDS = 5
WAITRESS_DISCONNECT_KEY = "waitress.client_disconnected"
BUSY_TEXT = "too many shared downloads are running right now; try again in a minute"
SERVICE_UNAVAILABLE = 503

log = logging.getLogger("drive.streams")


class StreamsBusy(DriveError):
    def __init__(self):
        super().__init__(BUSY_TEXT, SERVICE_UNAVAILABLE)


def disconnect_waitress_client(environ: dict) -> bool:
    check = environ.get(WAITRESS_DISCONNECT_KEY)
    channel = getattr(check, "__self__", None)
    if channel is None or not hasattr(channel, "outbuf_lock"):
        return False
    with channel.outbuf_lock:
        channel.will_close = True
        channel.connected = False
        channel.outbuf_lock.notify_all()
    channel.server.pull_trigger()
    return True


class WatchedStream:
    def __init__(self, inner: Iterable[bytes], guard: "StreamGuard", environ: dict,
                 release: Callable[[], None] | None):
        self._inner = inner
        self._guard = guard
        self._environ = environ
        self._release = release
        self._closed = False
        self.last_progress = guard.clock()

    def __iter__(self) -> Iterator[bytes]:
        for chunk in self._inner:
            self.last_progress = self._guard.clock()
            yield chunk

    def is_stalled(self, now: float) -> bool:
        return now - self.last_progress > self._guard.send_timeout

    def disconnect(self) -> bool:
        return disconnect_waitress_client(self._environ)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            close_inner = getattr(self._inner, "close", None)
            if close_inner is not None:
                close_inner()
        finally:
            self._guard.forget(self)
            if self._release is not None:
                self._release()


class StreamGuard:
    def __init__(self, share_limit: int = SHARE_STREAM_LIMIT, send_timeout: float = SEND_TIMEOUT_SECONDS,
                 clock: Callable[[], float] = time.monotonic):
        if share_limit < 1:
            raise ValueError("share_limit must be at least 1")
        self.share_limit = share_limit
        self.send_timeout = send_timeout
        self.clock = clock
        self._share_slots = threading.BoundedSemaphore(share_limit)
        self._lock = threading.Lock()
        self._streams: set[WatchedStream] = set()

    def active_count(self) -> int:
        with self._lock:
            return len(self._streams)

    def watch(self, inner: Iterable[bytes], environ: dict, release: Callable[[], None] | None = None) -> WatchedStream:
        stream = WatchedStream(inner, self, environ, release)
        with self._lock:
            self._streams.add(stream)
        return stream

    def forget(self, stream: WatchedStream) -> None:
        with self._lock:
            self._streams.discard(stream)

    def watch_share(self, build: Callable[[], object], environ: dict):
        if not self._share_slots.acquire(blocking=False):
            raise StreamsBusy()
        try:
            response = build()
            response.response = self.watch(response.response, environ, self._share_slots.release)
        except BaseException:
            self._share_slots.release()
            raise
        return response

    def watch_owner(self, response, environ: dict):
        response.response = self.watch(response.response, environ)
        return response

    def stop_stalled(self) -> int:
        now = self.clock()
        with self._lock:
            stalled = [stream for stream in self._streams if stream.is_stalled(now)]
        stopped = 0
        for stream in stalled:
            if stream.disconnect():
                stopped += 1
        if stopped:
            log.warning("closed %d downloads that made no progress for %d s", stopped, self.send_timeout)
        return stopped

    def keep_watching(self, stop: threading.Event, interval: float = WATCH_INTERVAL_SECONDS) -> None:
        while not stop.wait(interval):
            try:
                self.stop_stalled()
            except Exception:
                log.exception("stalled download check failed; trying again in %d s", interval)
