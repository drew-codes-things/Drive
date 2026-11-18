import json
import logging
import os
import selectors
import shutil
import signal
import subprocess
import sys
import time
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path

from .errors import DriveError
from .filetypes import AUDIO_TYPES, IMAGE_TYPES, VIDEO_TYPES
from .imagesize import HEIF_BRANDS
from .memory import media_job_bytes
from .names import split_extension
from .toolstart import STARTER_NOTE

TOOL_PATH = "/usr/local/bin:/usr/bin:/bin"
STDERR_TAIL_BYTES = 2000
PROBE_OUTPUT_LIMIT = 1024 * 1024
STDOUT_KEPT_BYTES = PROBE_OUTPUT_LIMIT + 1
PIPE_READ_BYTES = 64 * 1024
PIPE_GRACE_SECONDS = 1
PIPE_POLL_SECONDS = 0.1
FREEZE_SECONDS = 3
PROC = Path("/proc")
LAST_PROCESS_ID = PROC / "sys" / "kernel" / "ns_last_pid"
STATE_FIELD = 0
PARENT_FIELD = 1
THREADS_FIELD = 17
STOPPED_STATES = frozenset("Tt")
ENDED_STATES = frozenset("ZX")
ENDED, STOPPED, RUNNING = "ended", "stopped", "running"
OUTPUT_SLACK_BYTES = 16 * 1024 * 1024
SHELL_SIGNAL_BASE = 128
FILE_TOO_BIG_CODES = (-signal.SIGXFSZ, SHELL_SIGNAL_BASE + signal.SIGXFSZ)
MAX_CONVERT_PIXELS = 100_000_000
VIDEO_THREADS = "2"
GIF_SECONDS = "30"
AUDIO_FROM_VIDEO = "mp3"
PACKAGE_PARENT = Path(__file__).resolve().parent.parent
TOOL_STARTER = Path(__file__).resolve().with_name("toolstart.py")

HEIF_EXTENSIONS = frozenset({".heic", ".heif"})
EXTRA_VIDEO_EXTENSIONS = frozenset({".mkv", ".avi"})
ZIP_MAGIC = b"PK\x03\x04"
OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
RTF_MAGIC = b"{\\rtf"
DOCUMENT_MAGIC = {
    ".docx": ZIP_MAGIC, ".xlsx": ZIP_MAGIC, ".pptx": ZIP_MAGIC,
    ".odt": ZIP_MAGIC, ".ods": ZIP_MAGIC, ".odp": ZIP_MAGIC,
    ".doc": OLE_MAGIC, ".xls": OLE_MAGIC, ".ppt": OLE_MAGIC,
    ".rtf": RTF_MAGIC,
}
DEMUXERS = {
    "mov,mp4,m4a,3gp,3g2,mj2": "mov",
    "matroska,webm": "matroska",
    "mp3": "mp3",
    "ogg": "ogg",
    "flac": "flac",
    "wav": "wav",
    "aac": "aac",
    "avi": "avi",
}

LIBREOFFICE_SETTINGS = """<?xml version="1.0" encoding="UTF-8"?>
<oor:items xmlns:oor="http://openoffice.org/2001/registry" xmlns:xs="http://www.w3.org/2001/XMLSchema">
<item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="DisableMacrosExecution" \
oor:op="fuse"><value>true</value></prop></item>
<item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="MacroSecurityLevel" \
oor:op="fuse"><value>3</value></prop></item>
</oor:items>
"""

log = logging.getLogger("drive.media")


@dataclass(frozen=True)
class Target:
    key: str
    label: str
    extension: str


def _targets(*targets: Target) -> dict[str, Target]:
    return {target.key: target for target in targets}


JPG = Target("jpg", "JPEG image", ".jpg")
PNG = Target("png", "PNG image", ".png")
WEBP = Target("webp", "WebP image", ".webp")
GIF = Target("gif", "GIF image", ".gif")
MP3 = Target("mp3", "MP3 audio", ".mp3")
M4A = Target("m4a", "M4A audio (AAC)", ".m4a")
OPUS = Target("opus", "Opus audio", ".opus")
OGG = Target("ogg", "Ogg Vorbis audio", ".ogg")
FLAC = Target("flac", "FLAC audio (lossless)", ".flac")
WAV = Target("wav", "WAV audio", ".wav")
MP4 = Target("mp4", "MP4 video (H.264, AAC)", ".mp4")
WEBM = Target("webm", "WebM video (VP9, Opus)", ".webm")
VIDEO_GIF = Target("gif", "Animated GIF (first 30 s, 480 px wide)", ".gif")
VIDEO_MP3 = Target("mp3", "MP3 audio from the video", ".mp3")
PDF = Target("pdf", "PDF document", ".pdf")

TARGETS_BY_KIND = {
    "image": _targets(JPG, PNG, WEBP, GIF),
    "audio": _targets(MP3, M4A, OPUS, OGG, FLAC, WAV),
    "video": _targets(MP4, WEBM, VIDEO_GIF, VIDEO_MP3),
    "document": _targets(PDF),
}
SAME_FORMAT_EXTENSIONS = {
    "jpg": {".jpg", ".jpeg"}, "png": {".png"}, "webp": {".webp"}, "gif": {".gif"},
    "mp3": {".mp3"}, "m4a": {".m4a"}, "opus": {".opus"}, "ogg": {".ogg", ".oga"}, "flac": {".flac"},
    "wav": {".wav"}, "mp4": {".mp4", ".m4v"}, "webm": {".webm"}, "pdf": {".pdf"},
}
AUDIO_ENCODING = {
    "mp3": (["-c:a", "libmp3lame", "-q:a", "2"], "mp3"),
    "m4a": (["-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart"], "ipod"),
    "opus": (["-c:a", "libopus", "-b:a", "128k"], "ogg"),
    "ogg": (["-c:a", "libvorbis", "-q:a", "5"], "ogg"),
    "flac": (["-c:a", "flac"], "flac"),
    "wav": (["-c:a", "pcm_s16le"], "wav"),
}
EVEN_WIDTH_UP_TO_1080P = "scale='trunc(min(1920,iw)/2)*2':-2"
GIF_FILTER = ("[0:v:0]fps=10,scale='min(480,iw)':-2:flags=lanczos,split[a][b];"
              "[a]palettegen=stats_mode=diff[p];[b][p]paletteuse")
VIDEO_ENCODING = {
    "mp4": (["-map", "0:v:0", "-map", "0:a:0?", "-vf", EVEN_WIDTH_UP_TO_1080P, "-c:v", "libx264", "-preset",
             "veryfast", "-crf", "23", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-movflags",
             "+faststart"], "mp4"),
    "webm": (["-map", "0:v:0", "-map", "0:a:0?", "-vf", EVEN_WIDTH_UP_TO_1080P, "-c:v", "libvpx-vp9", "-deadline",
              "realtime", "-cpu-used", "8", "-row-mt", "1", "-b:v", "0", "-crf", "34", "-c:a", "libopus", "-b:a",
              "128k"], "webm"),
    "gif": (["-t", GIF_SECONDS, "-filter_complex", GIF_FILTER, "-loop", "0"], "gif"),
    "mp3": (["-vn", "-map", "0:a:0", "-c:a", "libmp3lame", "-q:a", "2"], "mp3"),
}


class ConversionFailed(DriveError):
    def __init__(self, message: str):
        super().__init__(message, 422)


@dataclass(frozen=True)
class Limits:
    seconds: int
    memory_bytes: int
    max_input_bytes: int
    max_output_bytes: int
    max_pixels: int
    promised_bytes: int | None = None

    @property
    def promised_result_bytes(self) -> int:
        return self.max_output_bytes if self.promised_bytes is None else self.promised_bytes

    @property
    def file_size_limit(self) -> int:
        return self.promised_result_bytes + OUTPUT_SLACK_BYTES

    @property
    def result_size_limit(self) -> int:
        return min(self.max_output_bytes, self.file_size_limit)

    @property
    def too_big_message(self) -> str:
        return f"the result would be over {self.result_size_limit // 1024 ** 2} MB"


@dataclass(frozen=True)
class Tools:
    prlimit: str | None
    ffmpeg: str | None
    ffprobe: str | None
    soffice: str | None
    network_sandbox: tuple[str, ...]

    @property
    def media_ready(self) -> bool:
        return bool(self.prlimit and self.ffmpeg and self.ffprobe)

    @property
    def images_ready(self) -> bool:
        return bool(self.prlimit)

    @property
    def documents_ready(self) -> bool:
        return bool(self.prlimit and self.soffice and self.network_sandbox)


def network_sandbox_prefix(unshare: str | None) -> tuple[str, ...]:
    if not unshare:
        return ()
    prefix = (unshare, "--user", "--map-root-user", "--net", "--")
    try:
        finished = subprocess.run([*prefix, "/bin/true"], stdin=subprocess.DEVNULL, capture_output=True, timeout=10,
                                  env={"PATH": TOOL_PATH}, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        log.info("network sandbox unavailable: %s", error)
        return ()
    if finished.returncode != 0:
        log.info("network sandbox unavailable: unshare answered %d", finished.returncode)
        return ()
    return prefix


def find_tools() -> Tools:
    return Tools(prlimit=shutil.which("prlimit"), ffmpeg=shutil.which("ffmpeg"), ffprobe=shutil.which("ffprobe"),
                 soffice=shutil.which("soffice"), network_sandbox=network_sandbox_prefix(shutil.which("unshare")))


def _is_heif(head: bytes) -> bool:
    return head[4:8] == b"ftyp" and head[8:12] in HEIF_BRANDS


def source_kind(name: str, mime: str, head: bytes) -> str | None:
    extension = split_extension(name)[1].lower()
    if mime in IMAGE_TYPES.values():
        return "image"
    if extension in HEIF_EXTENSIONS:
        return "image" if _is_heif(head) else None
    if extension in AUDIO_TYPES:
        return "audio"
    if extension in VIDEO_TYPES or extension in EXTRA_VIDEO_EXTENSIONS:
        return "video"
    if extension in DOCUMENT_MAGIC:
        return "document" if head.startswith(DOCUMENT_MAGIC[extension]) else None
    return None


def targets_for(kind: str | None, name: str) -> list[Target]:
    extension = split_extension(name)[1].lower()
    choices = TARGETS_BY_KIND.get(kind or "", {})
    return [target for target in choices.values() if extension not in SAME_FORMAT_EXTENSIONS.get(target.key, ())]


def output_name(source_name: str, target: Target) -> str:
    stem = split_extension(source_name)[0]
    return f"{stem}{target.extension}"


def _tail(path: Path) -> str:
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - STDERR_TAIL_BYTES))
            text = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
    lines = [line.strip() for line in text.splitlines()
             if line.strip().isprintable() and line.strip() and not line.startswith(STARTER_NOTE)]
    return lines[-1][:300] if lines else ""


def _read_stat(entry: Path) -> tuple[int, str, int] | None:
    try:
        fields = (entry / "stat").read_bytes().rpartition(b")")[2].split()
    except OSError:
        return None
    if len(fields) <= THREADS_FIELD or not fields[PARENT_FIELD].isdigit() or not fields[THREADS_FIELD].isdigit():
        return None
    return int(fields[PARENT_FIELD]), fields[STATE_FIELD].decode("ascii", "replace"), int(fields[THREADS_FIELD])


def _every_thread_stopped(pid: int) -> bool:
    try:
        tasks = list((PROC / str(pid) / "task").iterdir())
    except OSError:
        return False
    states = [stat[1] for stat in map(_read_stat, tasks) if stat is not None]
    return all(state in STOPPED_STATES or state in ENDED_STATES for state in states)


def _state_of(pid: int, stat: tuple[int, str, int] | None) -> str:
    if stat is None:
        return ENDED
    _, state, threads = stat
    if state in ENDED_STATES and threads <= 1:
        return ENDED
    if threads > 1:
        return STOPPED if _every_thread_stopped(pid) else RUNNING
    return STOPPED if state in STOPPED_STATES else RUNNING


def _process_ids():
    for entry in PROC.iterdir():
        if entry.name.isdigit():
            yield int(entry.name)


def _last_process_id() -> int | None:
    try:
        return int(LAST_PROCESS_ID.read_bytes())
    except (OSError, ValueError):
        return None


def _signal(pid: int, signal_number: int) -> None:
    try:
        os.kill(pid, signal_number)
    except ProcessLookupError:
        return


def _signal_group(pid: int, signal_number: int) -> None:
    try:
        os.killpg(pid, signal_number)
    except ProcessLookupError:
        return


def _children_are_listed() -> bool:
    return (PROC / "self" / "task" / str(os.getpid()) / "children").exists()


def _children(pid: int) -> list[int]:
    found = []
    try:
        tasks = list((PROC / str(pid) / "task").iterdir())
    except OSError:
        return found
    for task in tasks:
        try:
            found += [int(child) for child in (task / "children").read_bytes().split()]
        except OSError:
            continue
    return found


class _FreezeTookTooLong(Exception):
    pass


class _Freeze:
    def __init__(self, root: int, give_up_at: float):
        self.root = root
        self.give_up_at = give_up_at
        self.known = {root}
        self.live = {root}
        self.found_in_order = [root]

    def _check_time(self) -> None:
        if time.monotonic() >= self.give_up_at:
            raise _FreezeTookTooLong

    def _adopt(self, pid: int, stat: tuple[int, str, int] | None) -> None:
        self.known.add(pid)
        if _state_of(pid, stat) == ENDED:
            return
        self.live.add(pid)
        self.found_in_order.append(pid)
        _signal(pid, signal.SIGSTOP)

    def _settle_the_live(self) -> bool:
        moving = False
        for pid in list(self.live):
            self._check_time()
            state = _state_of(pid, _read_stat(PROC / str(pid)))
            if state == STOPPED:
                continue
            moving = True
            if state == ENDED:
                self.live.discard(pid)
            else:
                _signal(pid, signal.SIGSTOP)
        return moving

    def _live_youngest_first(self) -> list[int]:
        return [pid for pid in reversed(self.found_in_order) if pid in self.live]

    def pass_by_children(self) -> bool:
        self._check_time()
        moving = self._settle_the_live()
        for parent in self._live_youngest_first():
            for child in reversed(_children(parent)):
                if child in self.known:
                    continue
                self._check_time()
                self._adopt(child, _read_stat(PROC / str(child)))
                moving = True
        return moving

    def _adopt_if_ours(self, pid: int) -> bool:
        stat = _read_stat(PROC / str(pid))
        if stat is None or stat[0] not in self.live:
            return False
        self._adopt(pid, stat)
        return True

    def _chase_new_ids(self, after: int) -> bool:
        moving, last = False, _last_process_id()
        while last is not None and last > after:
            for pid in range(after + 1, last + 1):
                self._check_time()
                if pid not in self.known:
                    moving = self._adopt_if_ours(pid) or moving
            after, last = last, _last_process_id()
        return moving

    def pass_by_scan(self) -> bool:
        self._check_time()
        moving = self._settle_the_live()
        highest = 0
        for pid in _process_ids():
            highest = max(highest, pid)
            if pid in self.known:
                continue
            self._check_time()
            moving = self._adopt_if_ours(pid) or moving
        return self._chase_new_ids(highest) or moving


def _freeze_tree(freeze: _Freeze) -> None:
    _signal_group(freeze.root, signal.SIGSTOP)
    freeze_pass = freeze.pass_by_children if _children_are_listed() else freeze.pass_by_scan
    try:
        while freeze_pass():
            continue
    except _FreezeTookTooLong:
        log.warning("tool %d: not every process it started was seen stopped within %d s; killing the %d found so far",
                    freeze.root, FREEZE_SECONDS, len(freeze.live))


def _kill_tree(process: subprocess.Popen) -> None:
    freeze = _Freeze(process.pid, time.monotonic() + FREEZE_SECONDS)
    try:
        _freeze_tree(freeze)
    except Exception:
        log.exception("could not list what tool %d started, stopping its group and what was found", process.pid)
    finally:
        for pid in freeze.live:
            _signal(pid, signal.SIGKILL)
        _signal_group(process.pid, signal.SIGKILL)


def _keep_head(kept: bytearray, chunk: bytes) -> None:
    room = STDOUT_KEPT_BYTES - len(kept)
    if room > 0:
        kept.extend(chunk[:room])


def _keep_tail(kept: bytearray, chunk: bytes) -> None:
    kept.extend(chunk)
    del kept[:-STDERR_TAIL_BYTES]


def _read_output(process: subprocess.Popen, deadline: float, head: bytearray, tail: bytearray) -> bool:
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ, (_keep_head, head))
        selector.register(process.stderr, selectors.EVENT_READ, (_keep_tail, tail))
        finished_at = None
        while selector.get_map():
            now = time.monotonic()
            if finished_at is None and process.poll() is not None:
                finished_at = now
            if finished_at is not None and now - finished_at >= PIPE_GRACE_SECONDS:
                return True
            if finished_at is None and now >= deadline:
                return False
            for key, _ in selector.select(PIPE_POLL_SECONDS):
                chunk = os.read(key.fd, PIPE_READ_BYTES)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                keeper, kept = key.data
                keeper(kept, chunk)
    return True


def _run_until_done(process: subprocess.Popen, seconds: int, head: bytearray, tail: bytearray) -> None:
    deadline = time.monotonic() + seconds
    if not _read_output(process, deadline, head, tail):
        raise subprocess.TimeoutExpired(process.args, seconds)
    process.wait(timeout=max(0.0, deadline - time.monotonic()))


def run_tool(tools: Tools, command: list[str], work: Path, limits: Limits, tool_name: str,
             sandboxed: bool = False, extra_environment: dict | None = None) -> Path:
    if not tools.prlimit:
        raise ConversionFailed("prlimit is missing, so conversions cannot be limited and are switched off")
    home = work / "home"
    home.mkdir(exist_ok=True, mode=0o700)
    limited = [sys.executable, "-I", str(TOOL_STARTER), tools.prlimit, f"--as={limits.memory_bytes}",
               f"--cpu={limits.seconds * 4}", f"--fsize={limits.file_size_limit}", "--"]
    full = limited + list(tools.network_sandbox if sandboxed else ()) + command
    stdout_path, stderr_path = work / "tool.out", work / "tool.err"
    environment = {"PATH": TOOL_PATH, "HOME": str(home), "TMPDIR": str(home), "LANG": "C.UTF-8",
                   **(extra_environment or {})}
    head, tail = bytearray(), bytearray()
    process = subprocess.Popen(full, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               bufsize=0, cwd=work, env=environment, start_new_session=True)
    with process.stdout, process.stderr:
        try:
            _run_until_done(process, limits.seconds, head, tail)
        except subprocess.TimeoutExpired:
            _kill_tree(process)
            process.wait()
            raise ConversionFailed(f"{tool_name} took longer than {limits.seconds} s, so it was stopped")
        _kill_tree(process)
    stdout_path.write_bytes(bytes(head))
    stderr_path.write_bytes(bytes(tail))
    if process.returncode in FILE_TOO_BIG_CODES:
        raise ConversionFailed(f"{limits.too_big_message}, so {tool_name} was stopped")
    if process.returncode != 0:
        reason = _tail(stderr_path) or f"it stopped with code {process.returncode}"
        raise ConversionFailed(f"{tool_name} could not convert this file: {reason}")
    return stdout_path


def _frame_pixels(streams: list) -> int:
    largest = 0
    for stream in streams:
        if not isinstance(stream, dict) or stream.get("codec_type") != "video":
            continue
        width, height = stream.get("width"), stream.get("height")
        if isinstance(width, int) and isinstance(height, int) and width > 0 and height > 0:
            largest = max(largest, width * height)
    return largest


def probe(tools: Tools, source: Path, work: Path, limits: Limits) -> tuple[str, set[str], int]:
    command = [tools.ffprobe, "-v", "error", "-protocol_whitelist", "file", "-show_entries",
               "format=format_name:stream=codec_type,width,height", "-of", "json", f"file:{source}"]
    output = run_tool(tools, command, work, limits, "ffprobe", sandboxed=bool(tools.network_sandbox))
    if output.stat().st_size > PROBE_OUTPUT_LIMIT:
        raise ConversionFailed("ffprobe said too much about this file to trust it")
    try:
        answer = json.loads(output.read_text(encoding="utf-8", errors="replace"))
    except ValueError as error:
        raise ConversionFailed("ffprobe gave an answer that could not be read") from error
    format_name = str((answer.get("format") or {}).get("format_name") or "")
    demuxer = DEMUXERS.get(format_name)
    if demuxer is None:
        raise ConversionFailed(f"this is not an audio or video format that can be converted here ({format_name[:40]})")
    listed = answer.get("streams") if isinstance(answer.get("streams"), list) else []
    streams = {str(stream.get("codec_type")) for stream in listed if isinstance(stream, dict)}
    return demuxer, streams, _frame_pixels(listed)


def ffmpeg_command(tools: Tools, kind: str, target: Target, demuxer: str, source: Path, output: Path,
                   limits: Limits) -> list[str]:
    if kind == "audio":
        codec, muxer = AUDIO_ENCODING[target.key]
        encoding = ["-vn", "-map", "0:a:0", *codec]
    else:
        encoding, muxer = VIDEO_ENCODING[target.key]
    return [tools.ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-protocol_whitelist", "file",
            "-f", demuxer, "-i", f"file:{source}", *encoding, "-threads", VIDEO_THREADS,
            "-fs", str(limits.max_output_bytes), "-f", muxer, f"file:{output}"]


def _needed_streams(kind: str, target: Target) -> tuple[str, str]:
    if kind == "audio" or target.key == "mp3":
        return "audio", "this file has no sound to convert"
    return "video", "this file has no picture to convert"


def _no_memory_hold(amount: int):
    return nullcontext()


def convert_media(tools: Tools, kind: str, target: Target, source: Path, work: Path, limits: Limits,
                  holding_memory=_no_memory_hold) -> Path:
    if not tools.media_ready:
        raise ConversionFailed("ffmpeg is not installed here, so audio and video cannot be converted")
    demuxer, streams, frame_pixels = probe(tools, source, work, limits)
    needed, missing_text = _needed_streams(kind, target)
    if needed not in streams:
        raise ConversionFailed(missing_text)
    output = work / f"output{target.extension}"
    pixels_decoded = frame_pixels if kind == "video" and target.key != AUDIO_FROM_VIDEO else 0
    with holding_memory(media_job_bytes(pixels_decoded)):
        run_tool(tools, ffmpeg_command(tools, kind, target, demuxer, source, output, limits), work, limits,
                 "ffmpeg", sandboxed=bool(tools.network_sandbox))
    return output


def convert_image(tools: Tools, target: Target, source: Path, work: Path, limits: Limits) -> Path:
    output = work / f"output{target.extension}"
    command = [sys.executable, "-P", "-m", "drive.imagejob", str(source), str(output), target.key,
               str(limits.max_pixels)]
    run_tool(tools, command, work, limits, "the image converter",
             extra_environment={"PYTHONPATH": str(PACKAGE_PARENT)})
    return output


def _link_or_copy(source: Path, target: Path) -> None:
    try:
        os.link(source, target)
    except OSError:
        shutil.copyfile(source, target)


def convert_document(tools: Tools, source: Path, extension: str, work: Path, limits: Limits) -> Path:
    if not tools.documents_ready:
        raise ConversionFailed("documents cannot be converted on this server (LibreOffice or its network sandbox "
                               "is missing)")
    profile = work / "profile"
    (profile / "user").mkdir(parents=True, mode=0o700)
    (profile / "user" / "registrymodifications.xcu").write_text(LIBREOFFICE_SETTINGS, encoding="utf-8")
    document = work / f"input{extension}"
    _link_or_copy(source, document)
    outdir = work / "out"
    outdir.mkdir(mode=0o700)
    command = [tools.soffice, "--headless", "--norestore", "--nologo", "--nodefault", "--nolockcheck",
               f"-env:UserInstallation={profile.as_uri()}", "--convert-to", "pdf", "--outdir", str(outdir),
               str(document)]
    run_tool(tools, command, work, limits, "LibreOffice", sandboxed=True)
    output = outdir / "input.pdf"
    if not output.is_file():
        raise ConversionFailed("LibreOffice finished without making a PDF; the document may be damaged")
    return output
