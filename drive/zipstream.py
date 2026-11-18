import time
import zipfile
from collections.abc import Iterator

from .blobs import BlobStore
from .errors import DriveError

READ_BYTES = 1024 * 1024
MAX_ZIP_ENTRIES = 50_000
EARLIEST_ZIP_YEAR = 1980
ZIP64_LIMIT = (1 << 31) - 1
PAYLOAD_TOO_LARGE = 413
MSDOS_DIRECTORY_FLAG = 0x10
FOLDER_ATTRIBUTES = (0o40755 << 16) | MSDOS_DIRECTORY_FLAG
FILE_ATTRIBUTES = 0o100644 << 16


class _Sink:
    def __init__(self):
        self.pieces: list[bytes] = []

    def write(self, data) -> int:
        self.pieces.append(bytes(data))
        return len(data)

    def flush(self) -> None:
        return None

    def drain(self) -> bytes:
        data = b"".join(self.pieces)
        self.pieces.clear()
        return data


def _zip_time(timestamp: float) -> tuple:
    moment = time.localtime(timestamp)
    if moment.tm_year < EARLIEST_ZIP_YEAR:
        return (EARLIEST_ZIP_YEAR, 1, 1, 0, 0, 0)
    return moment[:6]


def check_zip_size(entries: list, largest: int) -> int:
    if len(entries) > MAX_ZIP_ENTRIES:
        raise DriveError(f"a zip can hold at most {MAX_ZIP_ENTRIES} files and folders", PAYLOAD_TOO_LARGE)
    total = sum(entry.get("size", 0) for _, entry in entries)
    if total > largest:
        raise DriveError(f"this folder holds {total / 1024 ** 3:.1f} GB, more than the {largest / 1024 ** 3:.1f} GB "
                         "a zip download may hold; download smaller folders", PAYLOAD_TOO_LARGE)
    return total


def stream_zip(entries: list, blobs: BlobStore) -> Iterator[bytes]:
    sink = _Sink()
    with zipfile.ZipFile(sink, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
        for name, entry in entries:
            info = zipfile.ZipInfo(name, date_time=_zip_time(entry["modifiedAt"]))
            if entry["kind"] == "folder":
                info.external_attr = FOLDER_ATTRIBUTES
                archive.writestr(info, b"")
                yield sink.drain()
                continue
            info.file_size = entry["size"]
            info.external_attr = FILE_ATTRIBUTES
            with open(blobs.path(entry["blob"]), "rb") as source, \
                    archive.open(info, "w", force_zip64=entry["size"] > ZIP64_LIMIT) as target:
                while piece := source.read(READ_BYTES):
                    target.write(piece)
                    yield sink.drain()
            yield sink.drain()
    yield sink.drain()
