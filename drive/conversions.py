import logging
import secrets
import shutil
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path

from .blobs import BlobStore
from .disk import DiskGuard
from .errors import DriveError, NotFound
from .filetypes import HEAD_BYTES, detect_type
from .imagesize import HEIF_TYPE, image_size
from .media import (HEIF_EXTENSIONS, OUTPUT_SLACK_BYTES, ConversionFailed, Limits, Target, Tools, convert_document,
                    convert_image, convert_media, output_name, source_kind, targets_for)
from .memory import DOCUMENT_JOB_BYTES, MemoryBudget, NoRoomYet, as_megabytes, image_job_bytes
from .names import split_extension
from .store import DriveStore

JOBS_AT_ONCE = 2
JOBS_KEPT = 50
JOB_ID_BYTES = 12
FOLDER_MODE = 0o700
BUSY = 503
SMALLEST_PROMISE_BYTES = 64 * 1024 * 1024
RAW_BYTES_PER_PIXEL = 4
DOCUMENT_GROWTH = 8
MEDIA_KINDS = ("audio", "video")
COMPRESSED_AUDIO_TARGETS = ("mp3", "m4a", "opus", "ogg")
COMPRESSED_AUDIO_GROWTH = 32
BUSY_TEXT = "two conversions are already running; try again when one has finished"
TOOL_MISSING_TEXT = {
    "image": "image conversion is switched off here (prlimit is missing)",
    "audio": "ffmpeg is not installed here, so audio cannot be converted",
    "video": "ffmpeg is not installed here, so video cannot be converted",
    "document": "documents cannot be converted on this server (LibreOffice or its network sandbox is missing)",
}
TOO_BIG = 413
MEMORY_BUSY_TEXT = ("the server stayed busy with other pictures and conversions for too long, so this one did not "
                    "start; try again")

log = logging.getLogger("drive.conversions")


class ConversionBusy(DriveError):
    def __init__(self):
        super().__init__(BUSY_TEXT, BUSY)


@dataclass
class Job:
    id: str
    node_id: int
    source_name: str
    target: str
    started_at: float
    status: str = "running"
    finished_at: float | None = None
    error: str | None = None
    result: dict | None = field(default=None)

    def view(self) -> dict:
        return {"id": self.id, "nodeId": self.node_id, "sourceName": self.source_name, "target": self.target,
                "status": self.status, "startedAt": self.started_at, "finishedAt": self.finished_at,
                "error": self.error, "result": self.result}


class ConversionManager:
    def __init__(self, store: DriveStore, blobs: BlobStore, disk: DiskGuard, work_dir: Path, tools: Tools,
                 limits: Limits, jobs_at_once: int = JOBS_AT_ONCE, clock=time.time,
                 memory: MemoryBudget | None = None):
        self.store = store
        self.blobs = blobs
        self.disk = disk
        self.work_dir = Path(work_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True, mode=FOLDER_MODE)
        self.tools = tools
        self.limits = limits
        self.clock = clock
        self.memory = memory or MemoryBudget()
        self._slots = threading.BoundedSemaphore(jobs_at_once)
        self._jobs: dict[str, Job] = {}
        self._jobs_lock = threading.Lock()

    def clear_leftovers(self) -> int:
        leftovers = [path for path in self.work_dir.iterdir() if path.is_dir()]
        for path in leftovers:
            shutil.rmtree(path, ignore_errors=True)
        return len(leftovers)

    def _tool_ready(self, kind: str) -> bool:
        if kind == "image":
            return self.tools.images_ready
        if kind == "document":
            return self.tools.documents_ready
        return self.tools.media_ready

    def _source(self, node_id: int) -> tuple[dict, str | None, bytes]:
        record = self.store.file_record(node_id)
        with open(self.blobs.path(record["blob"]), "rb") as handle:
            head = handle.read(HEAD_BYTES)
        return record, source_kind(record["name"], record["mime"], head), head

    def options(self, node_id: int) -> dict:
        record, kind, _ = self._source(node_id)
        targets = targets_for(kind, record["name"])
        answer = {"kind": kind, "targets": [{"key": target.key, "label": target.label} for target in targets],
                  "maxInputBytes": self.limits.max_input_bytes, "ready": bool(kind) and self._tool_ready(kind)}
        if kind and not answer["ready"]:
            answer["reason"] = TOOL_MISSING_TEXT[kind]
        return answer

    def _choose_target(self, record: dict, kind: str | None, raw_target) -> Target:
        if kind is None:
            raise DriveError("this kind of file cannot be converted")
        if not self._tool_ready(kind):
            raise DriveError(TOOL_MISSING_TEXT[kind], BUSY)
        for target in targets_for(kind, record["name"]):
            if target.key == raw_target:
                return target
        raise DriveError("pick one of the formats offered for this file")

    def _largest_result(self, record: dict, kind: str, target: Target, head: bytes) -> int:
        if kind in MEDIA_KINDS and target.key in COMPRESSED_AUDIO_TARGETS:
            guess = record["size"] * COMPRESSED_AUDIO_GROWTH
        elif kind in MEDIA_KINDS:
            return self.limits.max_output_bytes
        elif kind == "image":
            _, width, height = self._picture(record, head)
            guess = width * height * RAW_BYTES_PER_PIXEL + record["size"]
        else:
            guess = record["size"] * DOCUMENT_GROWTH
        return min(self.limits.max_output_bytes, max(guess, SMALLEST_PROMISE_BYTES))

    def _picture(self, record: dict, head: bytes) -> tuple[str, int, int]:
        is_heif = split_extension(record["name"])[1].lower() in HEIF_EXTENSIONS
        mime = HEIF_TYPE if is_heif else record["mime"]
        size = image_size(mime, head)
        if size is None:
            raise DriveError("the picture's size could not be read from the file, so it is not converted", 422)
        width, height = size
        if width * height > self.limits.max_pixels:
            raise DriveError(f"{width} x {height} pixels is more than {self.limits.max_pixels} pixels, so it is not "
                             "converted", TOO_BIG)
        return mime, width, height

    def _image_bytes(self, record: dict, target: Target, head: bytes) -> int:
        mime, width, height = self._picture(record, head)
        return image_job_bytes(mime, target.key, width, height)

    def _memory_needed(self, record: dict, kind: str, target: Target, head: bytes) -> int | None:
        if kind == "image":
            return self._image_bytes(record, target, head)
        if kind == "document":
            return DOCUMENT_JOB_BYTES
        return None

    def _refuse_beyond_budget(self, needed: int) -> None:
        if not self.memory.fits(needed):
            raise ConversionFailed(f"converting this needs about {as_megabytes(needed)} MB of memory, more than the "
                                   f"{as_megabytes(self.memory.total_bytes)} MB this server sets aside for "
                                   "pictures and conversions (WORK_MEMORY_MB)")

    @contextmanager
    def _holding_memory(self, needed: int):
        self._refuse_beyond_budget(needed)
        try:
            with self.memory.reserve(needed, self.limits.seconds):
                yield
        except NoRoomYet as busy:
            raise ConversionFailed(MEMORY_BUSY_TEXT) from busy

    def start(self, node_id: int, raw_target) -> dict:
        record, kind, head = self._source(node_id)
        target = self._choose_target(record, kind, raw_target)
        if record["size"] > self.limits.max_input_bytes:
            raise DriveError(f"files over {self.limits.max_input_bytes // 1024 ** 2} MB are not converted", TOO_BIG)
        needed = self._memory_needed(record, kind, target, head)
        if needed is not None:
            self._refuse_beyond_budget(needed)
        if not self._slots.acquire(blocking=False):
            raise ConversionBusy()
        job = Job(secrets.token_hex(JOB_ID_BYTES), node_id, record["name"], target.key, self.clock())
        try:
            largest = self._largest_result(record, kind, target, head)
            self.disk.promise(f"convert-{job.id}", largest + OUTPUT_SLACK_BYTES)
        except BaseException:
            self._slots.release()
            raise
        self._remember(job)
        threading.Thread(target=self._run, args=(job, record, kind, target, needed, largest), daemon=True,
                         name=f"convert-{job.id}").start()
        return job.view()

    def _remember(self, job: Job) -> None:
        with self._jobs_lock:
            self._jobs[job.id] = job
            finished = [old for old in self._jobs.values() if old.status != "running"]
            for old in sorted(finished, key=lambda item: item.started_at)[:max(0, len(self._jobs) - JOBS_KEPT)]:
                self._jobs.pop(old.id, None)

    def _convert(self, record: dict, kind: str, target: Target, work: Path, needed: int | None,
                 largest: int) -> Path:
        source = self.blobs.path(record["blob"])
        limits = replace(self.limits, promised_bytes=largest)
        if needed is None:
            return convert_media(self.tools, kind, target, source, work, limits, self._holding_memory)
        with self._holding_memory(needed):
            if kind == "image":
                return convert_image(self.tools, target, source, work, limits)
            extension = split_extension(record["name"])[1].lower()
            return convert_document(self.tools, source, extension, work, limits)

    def _check_output(self, output: Path, largest: int) -> int:
        if not output.is_file():
            raise ConversionFailed("the converter finished without making a file")
        size = output.stat().st_size
        if size == 0:
            raise ConversionFailed("the converter made an empty file")
        limits = replace(self.limits, promised_bytes=largest)
        if size >= limits.result_size_limit:
            raise ConversionFailed(limits.too_big_message)
        return size

    def _save(self, record: dict, target: Target, output: Path, size: int) -> dict:
        self.disk.check_write(0)
        name = output_name(record["name"], target)
        with open(output, "rb") as handle:
            head = handle.read(HEAD_BYTES)
        parent_id = self.store.get(record["id"])["parentId"]
        blob = self.blobs.adopt(output)
        try:
            return self.store.add_file(parent_id, name, size, blob, detect_type(name, head))
        except BaseException:
            self.blobs.remove(blob)
            raise

    def _run(self, job: Job, record: dict, kind: str, target: Target, needed: int | None, largest: int) -> None:
        work = self.work_dir / job.id
        try:
            work.mkdir(mode=FOLDER_MODE)
            output = self._convert(record, kind, target, work, needed, largest)
            job.result = self._save(record, target, output, self._check_output(output, largest))
            job.status = "done"
        except NotFound:
            job.status, job.error = "failed", "the file or its folder was deleted while it was being converted"
        except DriveError as error:
            job.status, job.error = "failed", str(error)
        except Exception as error:
            log.exception("conversion %s of file %s failed", job.id, job.node_id)
            job.status, job.error = "failed", f"the conversion failed: {type(error).__name__}"
        finally:
            job.finished_at = self.clock()
            shutil.rmtree(work, ignore_errors=True)
            self.disk.release(f"convert-{job.id}")
            self._slots.release()

    def job(self, job_id: str) -> dict:
        with self._jobs_lock:
            job = self._jobs.get(job_id)
        if job is None:
            raise NotFound("that conversion is not known here (the app may have restarted)")
        return job.view()

    def jobs(self) -> list[dict]:
        with self._jobs_lock:
            return [job.view() for job in sorted(self._jobs.values(), key=lambda item: item.started_at,
                                                  reverse=True)]
