import logging
import os
import threading
from collections import OrderedDict
from contextlib import contextmanager
from pathlib import Path

from PIL import Image

from .blobs import BlobStore
from .errors import DriveError
from .filetypes import HEAD_BYTES, preview_kind
from .imagesize import image_size
from .memory import MemoryBudget, NoRoomYet, as_megabytes, thumbnail_bytes

THUMB_EDGE = 320
THUMB_QUALITY = 80
MAX_THUMB_SOURCE_PIXELS = 40_000_000
FOLDER_MODE = 0o700
BACKGROUND = (255, 255, 255)
RENDERS_AT_ONCE = 2
RENDER_WAIT_SECONDS = 0.1
NOT_READY = 503
BUSY_TEXT = "this thumbnail is not ready yet; ask again in a moment"
REMEMBERED_REFUSALS = 10_000

log = logging.getLogger("drive.thumbnails")


class ThumbnailRefused(Exception):
    pass


class ThumbnailBusy(DriveError):
    def __init__(self):
        super().__init__(BUSY_TEXT, NOT_READY)


def flatten_onto_white(image: Image.Image) -> Image.Image:
    if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
        rgba = image.convert("RGBA")
        flat = Image.new("RGB", rgba.size, BACKGROUND)
        flat.paste(rgba, mask=rgba.getchannel("A"))
        return flat
    return image.convert("RGB")


def _refuse_large(width: int, height: int) -> None:
    if width * height > MAX_THUMB_SOURCE_PIXELS:
        raise ThumbnailRefused(f"{width} x {height} pixels is more than a thumbnail is made from")


def source_size(source: Path, mime: str) -> tuple[int, int]:
    with open(source, "rb") as handle:
        size = image_size(mime, handle.read(HEAD_BYTES))
    if size is None:
        raise ThumbnailRefused("the image size could not be read")
    _refuse_large(*size)
    return size


def _is_about_the_picture(error: OSError) -> bool:
    return error.errno is None


def render_thumbnail(source: Path, target: Path, mime: str) -> None:
    source_size(source, mime)
    with Image.open(source) as image:
        _refuse_large(*image.size)
        image.draft("RGB", (THUMB_EDGE, THUMB_EDGE))
        image.thumbnail((THUMB_EDGE, THUMB_EDGE), Image.Resampling.LANCZOS)
        flat = flatten_onto_white(image)
    target.parent.mkdir(parents=True, exist_ok=True, mode=FOLDER_MODE)
    partial = target.with_name(f".{target.name}.partial")
    flat.save(partial, "JPEG", quality=THUMB_QUALITY, optimize=True)
    os.replace(partial, target)


class ThumbnailMaker:
    def __init__(self, folder: Path, blobs: BlobStore, renders_at_once: int = RENDERS_AT_ONCE,
                 wait_seconds: float = RENDER_WAIT_SECONDS, memory: MemoryBudget | None = None):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True, mode=FOLDER_MODE)
        self.blobs = blobs
        self._render_slots = threading.BoundedSemaphore(renders_at_once)
        self._wait_seconds = wait_seconds
        self._blobs_rendering: set[str] = set()
        self._rendering_lock = threading.Lock()
        self.memory = memory or MemoryBudget()
        self._refused: OrderedDict[str, None] = OrderedDict()

    def path_for(self, blob: str) -> Path:
        self.blobs.path(blob)
        return self.folder / blob[:2] / f"{blob}.jpg"

    def thumbnail(self, record: dict) -> Path | None:
        if preview_kind(record["mime"]) != "image":
            return None
        target = self.path_for(record["blob"])
        if target.exists():
            return target
        if self._was_refused(record["blob"]):
            return None
        with self._render_turn(record["blob"]):
            if target.exists():
                return target
            try:
                self._render_within_budget(record, target)
            except ThumbnailRefused as error:
                log.info("no thumbnail for file %s: %s", record["id"], error)
                self._remember_refusal(record["blob"])
                return None
            except (OSError, ValueError, Image.DecompressionBombError) as error:
                log.warning("no thumbnail for file %s: %s: %s", record["id"], type(error).__name__, error)
                if not isinstance(error, OSError) or _is_about_the_picture(error):
                    self._remember_refusal(record["blob"])
                return None
        return target

    def _render_within_budget(self, record: dict, target: Path) -> None:
        source = self.blobs.path(record["blob"])
        needed = thumbnail_bytes(record["mime"], *source_size(source, record["mime"]))
        if not self.memory.fits(needed):
            raise ThumbnailRefused(f"it needs about {as_megabytes(needed)} MB to read, more than the "
                                   f"{as_megabytes(self.memory.total_bytes)} MB set aside (WORK_MEMORY_MB)")
        try:
            with self.memory.reserve(needed, self._wait_seconds):
                render_thumbnail(source, target, record["mime"])
        except NoRoomYet as busy:
            raise ThumbnailBusy() from busy

    def _was_refused(self, blob: str) -> bool:
        with self._rendering_lock:
            return blob in self._refused

    def _remember_refusal(self, blob: str) -> None:
        with self._rendering_lock:
            self._refused[blob] = None
            while len(self._refused) > REMEMBERED_REFUSALS:
                self._refused.popitem(last=False)

    @contextmanager
    def _render_turn(self, blob: str):
        self._claim(blob)
        try:
            if not self._render_slots.acquire(timeout=self._wait_seconds):
                raise ThumbnailBusy()
            try:
                yield
            finally:
                self._render_slots.release()
        finally:
            with self._rendering_lock:
                self._blobs_rendering.discard(blob)

    def _claim(self, blob: str) -> None:
        with self._rendering_lock:
            if blob in self._blobs_rendering:
                raise ThumbnailBusy()
            self._blobs_rendering.add(blob)

    def prune(self) -> int:
        removed = 0
        for thumb in self.folder.glob("*/*.jpg"):
            try:
                source = self.blobs.path(thumb.stem)
            except ValueError:
                continue
            if not source.exists():
                thumb.unlink(missing_ok=True)
                removed += 1
        return removed
