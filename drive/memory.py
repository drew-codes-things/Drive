import threading
import time
from collections import deque
from contextlib import contextmanager

MIB = 1024 ** 2
DEFAULT_WORK_MEMORY_BYTES = 1536 * MIB
THUMB_BASE_BYTES = 16 * MIB
THUMB_BYTES_PER_PIXEL = {
    "image/jpeg": 0.25, "image/gif": 0.5, "image/bmp": 1.5, "image/png": 6.0, "image/avif": 9.5,
    "image/webp": 16.5,
}
IMAGE_JOB_BASE_BYTES = 64 * MIB
DECODE_BYTES_PER_PIXEL = {
    "image/jpeg": 8.0, "image/png": 8.0, "image/bmp": 8.0, "image/gif": 3.0, "image/heif": 10.0,
    "image/avif": 11.0, "image/webp": 17.0,
}
ENCODE_BYTES_PER_PIXEL = {"png": 1.0, "jpg": 9.0, "gif": 23.0, "webp": 24.0}
UNKNOWN_FORMAT_BYTES_PER_PIXEL = 24.0
AUDIO_JOB_BYTES = 128 * MIB
VIDEO_JOB_BASE_BYTES = 400 * MIB
VIDEO_BYTES_PER_FRAME_PIXEL = 32
DOCUMENT_JOB_BYTES = 1024 * MIB


class NoRoomYet(Exception):
    pass


def thumbnail_bytes(mime: str, width: int, height: int) -> int:
    per_pixel = THUMB_BYTES_PER_PIXEL.get(mime, UNKNOWN_FORMAT_BYTES_PER_PIXEL)
    return THUMB_BASE_BYTES + int(width * height * per_pixel)


def image_job_bytes(mime: str, target: str, width: int, height: int) -> int:
    per_pixel = (DECODE_BYTES_PER_PIXEL.get(mime, UNKNOWN_FORMAT_BYTES_PER_PIXEL)
                 + ENCODE_BYTES_PER_PIXEL.get(target, UNKNOWN_FORMAT_BYTES_PER_PIXEL))
    return IMAGE_JOB_BASE_BYTES + int(width * height * per_pixel)


def media_job_bytes(frame_pixels: int) -> int:
    if not frame_pixels:
        return AUDIO_JOB_BYTES
    return VIDEO_JOB_BASE_BYTES + frame_pixels * VIDEO_BYTES_PER_FRAME_PIXEL


def as_megabytes(amount: int) -> int:
    return -(-amount // MIB)


class MemoryBudget:
    def __init__(self, total_bytes: int = DEFAULT_WORK_MEMORY_BYTES):
        self.total_bytes = total_bytes
        self._used = 0
        self._queue: deque = deque()
        self._room = threading.Condition()

    @property
    def used_bytes(self) -> int:
        with self._room:
            return self._used

    def fits(self, amount: int) -> bool:
        return amount <= self.total_bytes

    def _turn_has_come(self, ticket, amount: int) -> bool:
        return self._queue[0] is ticket and self._used + amount <= self.total_bytes

    @contextmanager
    def reserve(self, amount: int, wait_seconds: float):
        if not self.fits(amount):
            raise ValueError(f"{as_megabytes(amount)} MB is more than the whole budget of "
                             f"{as_megabytes(self.total_bytes)} MB")
        deadline = time.monotonic() + wait_seconds
        ticket = object()
        with self._room:
            self._queue.append(ticket)
            try:
                while not self._turn_has_come(ticket, amount):
                    left = deadline - time.monotonic()
                    if left <= 0:
                        raise NoRoomYet()
                    self._room.wait(left)
                self._used += amount
            finally:
                self._queue.remove(ticket)
                self._room.notify_all()
        try:
            yield
        finally:
            with self._room:
                self._used -= amount
                self._room.notify_all()
