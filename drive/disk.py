import os
import threading
from pathlib import Path

from .errors import DriveError

INSUFFICIENT_STORAGE = 507
GIB = 1024 ** 3


def format_gib(size: int) -> str:
    return f"{size / GIB:.1f} GB"


class DiskFull(DriveError):
    def __init__(self, message: str):
        super().__init__(message, INSUFFICIENT_STORAGE)


class DiskGuard:
    def __init__(self, path: Path, min_free_bytes: int, free_space=None):
        self.path = Path(path)
        self.min_free_bytes = min_free_bytes
        self._free_space = free_space or self._statvfs_free
        self._promised: dict[str, int] = {}
        self._lock = threading.RLock()

    def _statvfs_free(self) -> int:
        stats = os.statvfs(self.path)
        return stats.f_bavail * stats.f_frsize

    def free_bytes(self) -> int:
        return self._free_space()

    def total_bytes(self) -> int:
        stats = os.statvfs(self.path)
        return stats.f_blocks * stats.f_frsize

    def promised_bytes(self) -> int:
        with self._lock:
            return sum(self._promised.values())

    def room_for_uploads(self) -> int:
        return max(0, self.free_bytes() - self.promised_bytes() - self.min_free_bytes)

    def _refusal(self, wanted: int) -> DiskFull:
        return DiskFull(f"not enough space: this needs {format_gib(wanted)}, but the disk must keep "
                        f"{format_gib(self.min_free_bytes)} free and only {format_gib(self.room_for_uploads())} "
                        "can still be used")

    def promise(self, key: str, size: int) -> None:
        with self._lock:
            already = sum(value for name, value in self._promised.items() if name != key)
            if self.free_bytes() - already - size < self.min_free_bytes:
                raise self._refusal(size)
            self._promised[key] = size

    def hold(self, key: str, size: int) -> None:
        with self._lock:
            self._promised[key] = max(0, size)

    def update_promise(self, key: str, remaining: int) -> None:
        with self._lock:
            if key in self._promised:
                self._promised[key] = max(0, remaining)

    def release(self, key: str) -> None:
        with self._lock:
            self._promised.pop(key, None)

    def check_write(self, size: int) -> None:
        if self.free_bytes() - size < self.min_free_bytes:
            raise self._refusal(size)
