import dataclasses
import io
import logging
import os
import platform
import shutil
import signal
import subprocess
import sys
import threading
import time
import types
import zipfile
from pathlib import Path

import pillow_heif
import pytest
from PIL import Image

from conftest import GIB, SignedInClient, make_app
from drive.conversions import ConversionManager
from drive.media import Limits, Tools, find_tools

WAIT_SECONDS = 60
POLL_SECONDS = 0.05
HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
TOOLS = find_tools()
needs_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg is not installed")
needs_documents = pytest.mark.skipif(not TOOLS.documents_ready, reason="LibreOffice or unshare is not usable here")
ROOMY_LIMITS = Limits(seconds=120, memory_bytes=3 * GIB, max_input_bytes=GIB, max_output_bytes=GIB,
                      max_pixels=100_000_000)
XML_HEAD = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
SLOW_TOOL = "#!/bin/sh\necho $$ > \"$HOME/../pid\"\nexec sleep 30\n"
FORKING_TOOL = """#!/bin/sh
echo $$ > "$HOME/../pid"
sleep 40 &
echo $! > "$HOME/../child-pid"
setsid sleep 41 &
echo $! > "$HOME/../escaped-pid"
(setsid sh -c 'echo $$ > "$HOME/../orphan-pid"; exec sleep 47' &)
sleep 30
"""
GONE_WITHIN_SECONDS = 5
CHATTY_TOOL = """#!/bin/sh
head -c 16000000 /dev/zero
head -c 16000000 /dev/zero | tr '\\0' 'x' >&2
printf '\\nthe real reason\\n' >&2
exit 3
"""
HELPER_LEAVING_TOOL = """#!/bin/sh
(setsid sh -c 'echo $$ > "$HOME/../helper-pid"; exec sleep 25' &)
sleep 0.3
echo finished
"""
HOPPER = """#!/bin/sh
[ -e "$HOME/../stop" ] && exit 0
[ "$(date +%s)" -gt "$1" ] && exit 0
echo $$ > "$HOME/../hop-pid"
sleep 0.02
setsid "$0" "$1" > /dev/null 2>&1 &
exit 0
"""
HOPPING_TOOL = """#!/bin/sh
setsid "$HOME/../../bin/hopper" "$(( $(date +%s) + 40 ))" > /dev/null 2>&1 &
exec sleep 30
"""
FAST_HOPPER = """#!/usr/bin/python3
import os
import sys
import time

deadline, interval, work = float(sys.argv[1]), float(sys.argv[2]), sys.argv[3]
ballast = b"\\x01" * (int(sys.argv[4]) * 1024 * 1024) if len(sys.argv) > 4 else b""
while time.time() < deadline and not os.path.exists(work + "/stop"):
    if os.fork():
        os._exit(0)
    os.setsid()
    with open(work + "/hop-pid", "w") as handle:
        handle.write(str(os.getpid()))
    time.sleep(interval)
"""
FAST_HOPPING_TOOL = """#!/bin/sh
setsid "$HOME/../../bin/fast-hopper" "$(( $(date +%s) + 40 ))" {interval} "$HOME/.." {ballast} > /dev/null 2>&1 &
exec sleep 30
"""
ENDED_CHILDREN_TOOL = """#!/usr/bin/python3
import os
import subprocess
import sys
import time

count, interval, work = int(sys.argv[1]), sys.argv[2], os.environ["HOME"] + "/.."
with open(work + "/pid", "w") as handle:
    handle.write(str(os.getpid()))
ended = []
for _ in range(count):
    child = os.fork()
    if child == 0:
        os._exit(0)
    ended.append(child)
with open(work + "/ended-pids", "w") as handle:
    handle.write(" ".join(map(str, ended)))
if interval != "none":
    subprocess.Popen([work + "/../bin/fast-hopper", str(time.time() + 40), interval, work], start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
os.execvp("sleep", ["sleep", "60"])
"""
HOP_CHECK_SECONDS = 0.3
CHAIN_TOOL_SECONDS = 2
CHILDREN_MODES = ["children lists", "full process scan"]
LARGE_HOP_MEGABYTES = 300
ENDED_CHILDREN = 5000
ENDED_CHILDREN_TOOL_SECONDS = 6
SLOW_LOOK_SECONDS = 0.02
SLOW_LOOK_CHILDREN = 400
BOUND_SLACK_SECONDS = 0.6
MAIN_THREAD_GONE_TOOL = """#!/usr/bin/python3
import ctypes
import os
import threading
import time

work = os.environ["HOME"] + "/.."


def keep_writing():
    deadline = time.time() + 40
    while time.time() < deadline and not os.path.exists(work + "/stop"):
        with open(work + "/beat", "w") as handle:
            handle.write(str(time.time()))
        time.sleep(0.02)
    os._exit(0)


if os.fork() == 0:
    os.setsid()
    with open(work + "/beating-pid", "w") as handle:
        handle.write(str(os.getpid()))
    threading.Thread(target=keep_writing).start()
    time.sleep(0.2)
    ctypes.CDLL(None).pthread_exit(None)
os.execvp("sleep", ["sleep", "60"])
"""
ODD_NAME_PROCESS = ("import time\nwith open('/proc/self/comm', 'wb') as handle:\n    handle.write(b'caf\\xe9')\n"
                    "time.sleep(60)\n")
PIPE_GRACE_BOUND_SECONDS = 2
SIGNAL_REPORTING_TOOL = "#!/bin/sh\ngrep SigIgn /proc/$$/status\n"
FLOODING_TOOL = "#!/bin/sh\nhead -c 64000000 /dev/zero > \"$HOME/../flood\"\n"
HUNGRY_TOOL = "#!/usr/bin/python3\nhunger = bytearray(1536 * 1024 * 1024)\nprint(len(hunger))\n"
SCRIPTED_FREEZE_SECONDS = 0.2
CLONE_SYSCALLS = {"x86_64": 56, "aarch64": 220}
VFORK_TOOL = """#!/usr/bin/python3
import ctypes
import os
import signal
import time

CLONE_VFORK = 0x4000
work = os.environ["HOME"] + "/.."
libc = ctypes.CDLL(None, use_errno=True)
if libc.syscall({clone}, CLONE_VFORK | signal.SIGCHLD, 0, 0, 0, 0) == 0:
    os.setsid()
    with open(work + "/beating-pid", "w") as handle:
        handle.write(str(os.getpid()))
    deadline = time.time() + 40
    while time.time() < deadline and not os.path.exists(work + "/stop"):
        with open(work + "/beat", "w") as handle:
            handle.write(str(time.time()))
        time.sleep(0.02)
    os._exit(0)
time.sleep(60)
"""


def image_bytes(image_format: str, size=(64, 48), mode="RGB") -> bytes:
    if image_format == "HEIF":
        pillow_heif.register_heif_opener()
    buffer = io.BytesIO()
    Image.new(mode, size, (200, 40, 40) if mode == "RGB" else (200, 40, 40, 128)).save(buffer, image_format)
    return buffer.getvalue()


def ffmpeg_fixture(tmp_path_factory, name: str, arguments: list[str]) -> bytes:
    folder = tmp_path_factory.mktemp("media")
    target = folder / name
    subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", *arguments, str(target)], check=True,
                   timeout=60)
    return target.read_bytes()


@pytest.fixture(scope="session")
def wav_bytes(tmp_path_factory):
    return ffmpeg_fixture(tmp_path_factory, "tone.wav", ["-f", "lavfi", "-i", "sine=frequency=440:duration=1"])


@pytest.fixture(scope="session")
def mkv_bytes(tmp_path_factory):
    return ffmpeg_fixture(tmp_path_factory, "clip.mkv", [
        "-f", "lavfi", "-i", "testsrc=size=64x48:rate=10:duration=1", "-f", "lavfi", "-i",
        "sine=frequency=440:duration=1", "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac"])


@pytest.fixture(scope="session")
def silent_mkv_bytes(tmp_path_factory):
    return ffmpeg_fixture(tmp_path_factory, "silent.mkv", [
        "-f", "lavfi", "-i", "testsrc=size=64x48:rate=10:duration=1", "-c:v", "libx264", "-pix_fmt", "yuv420p"])


def docx_bytes(text: str = "Hello Drive") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", XML_HEAD + (
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            '</Types>'))
        archive.writestr("_rels/.rels", XML_HEAD + (
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship '
            'Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
            'Target="word/document.xml"/></Relationships>'))
        archive.writestr("word/document.xml", XML_HEAD + (
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r>'
            f'<w:t>{text}</w:t></w:r></w:p></w:body></w:document>'))
    return buffer.getvalue()


def fake_tool(folder: Path, name: str, body: str) -> str:
    folder.mkdir(exist_ok=True)
    path = folder / name
    path.write_text(body)
    path.chmod(0o755)
    return str(path)


@pytest.fixture
def converting(services, tmp_path):
    def build(tools: Tools = TOOLS, limits: Limits = ROOMY_LIMITS, jobs_at_once: int = 2):
        manager = ConversionManager(services.store, services.blobs, services.disk, tmp_path / "convert", tools, limits,
                                    jobs_at_once=jobs_at_once)
        owner = SignedInClient(make_app(dataclasses.replace(services, conversions=manager)).test_client())
        return owner, manager

    return build


def wait_for(owner, job: dict) -> dict:
    deadline = time.monotonic() + WAIT_SECONDS
    while time.monotonic() < deadline:
        answer = owner.get(f"/api/conversions/{job['id']}").get_json()
        if answer["status"] != "running":
            return answer
        time.sleep(POLL_SECONDS)
    raise AssertionError(f"conversion {job['id']} did not finish in {WAIT_SECONDS} s")


def convert(owner, node: dict, target: str) -> dict:
    started = owner.post(f"/api/files/{node['id']}/conversions", json={"target": target})
    assert started.status_code == 202, started.get_json()
    return wait_for(owner, started.get_json())


def result_bytes(owner, job: dict) -> bytes:
    assert job["status"] == "done", job
    return owner.get(f"/api/files/{job['result']['id']}/content").data


def probe_codecs(data: bytes, tmp_path: Path) -> str:
    sample = tmp_path / "probe.bin"
    sample.write_bytes(data)
    finished = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_name:format=format_name",
                               "-of", "default=nw=1:nk=1", str(sample)], capture_output=True, text=True, timeout=30)
    return finished.stdout


def test_a_png_offers_the_other_image_formats(converting):
    owner, _ = converting()
    node = owner.upload("dot.png", image_bytes("PNG"))
    options = owner.get(f"/api/files/{node['id']}/conversions").get_json()
    assert options["kind"] == "image" and options["ready"]
    assert [target["key"] for target in options["targets"]] == ["jpg", "webp", "gif"]


@pytest.mark.parametrize("target, pillow_format", [("jpg", "JPEG"), ("webp", "WEBP"), ("gif", "GIF")])
def test_images_convert_to_each_format_next_to_the_original(converting, target, pillow_format):
    owner, _ = converting()
    folder = owner.folder("Pictures")
    node = owner.upload("red.png", image_bytes("PNG", mode="RGBA"), parent_id=folder["id"])
    job = convert(owner, node, target)
    converted = Image.open(io.BytesIO(result_bytes(owner, job)))
    assert converted.format == pillow_format and converted.size == (64, 48)
    assert job["result"]["name"] == f"red.{target}" and job["result"]["parentId"] == folder["id"]
    names = [item["name"] for item in owner.get(f"/api/folders/{folder['id']}").get_json()["items"]]
    assert sorted(names) == sorted(["red.png", f"red.{target}"])


def test_a_jpeg_converts_to_png(converting):
    owner, _ = converting()
    node = owner.upload("photo.jpg", image_bytes("JPEG"))
    assert Image.open(io.BytesIO(result_bytes(owner, convert(owner, node, "png")))).format == "PNG"


def test_heic_photos_convert_to_jpeg(converting):
    owner, _ = converting()
    node = owner.upload("IMG_0001.HEIC", image_bytes("HEIF"))
    options = owner.get(f"/api/files/{node['id']}/conversions").get_json()
    assert options["kind"] == "image"
    converted = Image.open(io.BytesIO(result_bytes(owner, convert(owner, node, "jpg"))))
    assert converted.format == "JPEG" and converted.size == (64, 48)


def test_converting_twice_never_overwrites(converting):
    owner, _ = converting()
    node = owner.upload("dot.png", image_bytes("PNG"))
    first, second = convert(owner, node, "jpg"), convert(owner, node, "jpg")
    assert (first["result"]["name"], second["result"]["name"]) == ("dot.jpg", "dot (2).jpg")
    assert owner.get(f"/api/files/{node['id']}/content").data == image_bytes("PNG")


@needs_ffmpeg
@pytest.mark.parametrize("target, expected", [
    ("mp3", "mp3"), ("m4a", "aac"), ("opus", "opus"), ("ogg", "vorbis"), ("flac", "flac")])
def test_audio_converts_to_each_format(converting, wav_bytes, tmp_path, target, expected):
    owner, _ = converting()
    node = owner.upload("tone.wav", wav_bytes)
    job = convert(owner, node, target)
    assert job["result"]["name"] == f"tone.{target}"
    assert expected in probe_codecs(result_bytes(owner, job), tmp_path)


@needs_ffmpeg
def test_audio_converts_to_wav(converting, wav_bytes, tmp_path):
    owner, _ = converting()
    flac = convert(owner, owner.upload("tone.wav", wav_bytes), "flac")
    job = convert(owner, owner.get(f"/api/nodes/{flac['result']['id']}").get_json(), "wav")
    assert "pcm_s16le" in probe_codecs(result_bytes(owner, job), tmp_path)


@needs_ffmpeg
@pytest.mark.parametrize("target, expected", [
    ("mp4", "h264"), ("webm", "vp9"), ("gif", "gif"), ("mp3", "mp3")])
def test_video_converts_to_each_format(converting, mkv_bytes, tmp_path, target, expected):
    owner, _ = converting()
    node = owner.upload("clip.mkv", mkv_bytes)
    job = convert(owner, node, target)
    assert expected in probe_codecs(result_bytes(owner, job), tmp_path)
    assert job["result"]["name"] == f"clip.{target}"


@needs_ffmpeg
def test_video_without_sound_cannot_become_mp3(converting, silent_mkv_bytes):
    owner, _ = converting()
    job = convert(owner, owner.upload("silent.mkv", silent_mkv_bytes), "mp3")
    assert job["status"] == "failed" and "no sound" in job["error"]


@needs_documents
@pytest.mark.parametrize("name, content", [("letter.docx", docx_bytes()), ("note.rtf", b"{\\rtf1\\ansi Hello Drive}")])
def test_documents_become_pdf(converting, name, content):
    owner, _ = converting()
    node = owner.upload(name, content)
    job = convert(owner, node, "pdf")
    assert result_bytes(owner, job).startswith(b"%PDF-")
    assert job["result"]["preview"] == "pdf"


def test_files_that_only_claim_a_format_are_refused(converting):
    owner, _ = converting()
    fake_png = owner.upload("cat.png", b"<html>not a picture</html>")
    fake_docx = owner.upload("letter.docx", b"plain text pretending")
    for node in (fake_png, fake_docx):
        assert owner.get(f"/api/files/{node['id']}/conversions").get_json()["targets"] == []
        refused = owner.post(f"/api/files/{node['id']}/conversions", json={"target": "pdf"})
        assert refused.status_code == 400


@needs_ffmpeg
def test_a_damaged_video_fails_cleanly(converting, tmp_path):
    owner, manager = converting()
    node = owner.upload("broken.mp4", os.urandom(4096))
    job = convert(owner, node, "webm")
    assert job["status"] == "failed" and job["error"]
    assert list(manager.work_dir.iterdir()) == []
    assert [item["name"] for item in owner.get("/api/folders/0").get_json()["items"]] == ["broken.mp4"]


def test_only_offered_formats_are_accepted(converting):
    owner, _ = converting()
    node = owner.upload("dot.png", image_bytes("PNG"))
    for target in ("png", "exe", "../jpg", None, 5):
        assert owner.post(f"/api/files/{node['id']}/conversions", json={"target": target}).status_code == 400


def test_writes_need_the_csrf_token(converting):
    owner, _ = converting()
    node = owner.upload("dot.png", image_bytes("PNG"))
    answer = owner.client.post(f"/api/files/{node['id']}/conversions", json={"target": "jpg"}, headers=owner.headers)
    assert answer.status_code == 403


def test_large_inputs_are_refused(converting):
    owner, _ = converting(limits=dataclasses.replace(ROOMY_LIMITS, max_input_bytes=100))
    node = owner.upload("dot.png", image_bytes("PNG"))
    assert owner.post(f"/api/files/{node['id']}/conversions", json={"target": "jpg"}).status_code == 413


def test_results_over_the_output_cap_are_dropped(converting):
    owner, manager = converting(limits=dataclasses.replace(ROOMY_LIMITS, max_output_bytes=50))
    job = convert(owner, owner.upload("dot.png", image_bytes("PNG")), "webp")
    assert job["status"] == "failed" and "over" in job["error"]
    assert list(manager.work_dir.iterdir()) == []
    assert len(owner.get("/api/folders/0").get_json()["items"]) == 1


def test_images_over_the_pixel_cap_are_refused(converting):
    owner, _ = converting(limits=dataclasses.replace(ROOMY_LIMITS, max_pixels=1000))
    node = owner.upload("big.png", image_bytes("PNG", size=(100, 100)))
    answer = owner.post(f"/api/files/{node['id']}/conversions", json={"target": "jpg"})
    assert answer.status_code == 413 and "pixels" in answer.get_json()["error"]


@needs_ffmpeg
def test_a_playlist_or_concat_script_cannot_pull_in_other_files(converting, tmp_path):
    owner, _ = converting()
    canary = tmp_path / "canary.ts"
    canary.write_text("CANARY-line-one\nCANARY-line-two\n")
    playlist = f"#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:0\n#EXTINF:1.0,\n{canary.as_uri()}\n#EXT-X-ENDLIST\n".encode()
    concat = f"ffconcat version 1.0\nfile '{canary}'\n".encode()
    for name, content, target in (("holiday.mp4", playlist, "webm"), ("clip.mkv", concat, "mp4"),
                                  ("-i.mp4", playlist, "mp3")):
        job = convert(owner, owner.upload(name, content), target)
        assert job["status"] == "failed", job
        assert "CANARY" not in job["error"] and str(canary) not in job["error"]
    names = [item["name"] for item in owner.get("/api/folders/0").get_json()["items"]]
    assert sorted(names) == ["-i.mp4", "clip.mkv", "holiday.mp4"]


def test_the_disk_floor_stops_conversions(converting, free_space):
    owner, _ = converting()
    node = owner.upload("dot.png", image_bytes("PNG"))
    free_space.free = 15 * GIB
    answer = owner.post(f"/api/files/{node['id']}/conversions", json={"target": "jpg"})
    assert answer.status_code == 507


@needs_ffmpeg
def test_only_lossless_audio_and_video_set_aside_the_whole_output_cap(converting, free_space, wav_bytes):
    owner, _ = converting()
    free_space.free = 15 * GIB + 512 * 1024 ** 2
    tone = owner.upload("tone.wav", wav_bytes)
    answer = owner.post(f"/api/files/{tone['id']}/conversions", json={"target": "flac"})
    assert answer.status_code == 507, answer.get_json()
    assert convert(owner, tone, "mp3")["status"] == "done"
    assert convert(owner, tone, "opus")["status"] == "done"
    picture = owner.upload("dot.png", image_bytes("PNG"))
    assert convert(owner, picture, "jpg")["status"] == "done"


SMALL_PROMISE_BYTES = 64 * 1024
LONG_TONE_SECONDS = 30
HUGE_WAV_SECONDS = 220


def seconds_of_sound(owner, job: dict, folder: Path) -> float:
    result = folder / "result"
    result.write_bytes(result_bytes(owner, job))
    shown = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                            str(result)], capture_output=True, text=True, check=True, timeout=60)
    return float(shown.stdout.strip())


@needs_ffmpeg
@pytest.mark.parametrize("target", ["m4a", "mp3"])
def test_a_result_bigger_than_its_disk_promise_is_never_cut_short(converting, monkeypatch, tmp_path_factory,
                                                                  target):
    owner, manager = converting()
    monkeypatch.setattr(manager, "_largest_result", lambda *_: SMALL_PROMISE_BYTES)
    tone = ffmpeg_fixture(tmp_path_factory, "long.flac",
                          ["-f", "lavfi", "-i", f"sine=frequency=440:duration={LONG_TONE_SECONDS}"])
    job = convert(owner, owner.upload("long.flac", tone), target)
    assert job["status"] == "done", job
    assert job["result"]["size"] > SMALL_PROMISE_BYTES
    assert seconds_of_sound(owner, job, tmp_path_factory.mktemp("result")) == pytest.approx(LONG_TONE_SECONDS, abs=1)


@needs_ffmpeg
def test_a_result_past_its_promise_and_slack_fails_instead_of_being_kept(converting, monkeypatch, tmp_path_factory):
    owner, manager = converting()
    monkeypatch.setattr(manager, "_largest_result", lambda *_: SMALL_PROMISE_BYTES)
    tone = ffmpeg_fixture(tmp_path_factory, "huge.flac",
                          ["-f", "lavfi", "-i", f"sine=frequency=440:duration={HUGE_WAV_SECONDS}"])
    job = convert(owner, owner.upload("huge.flac", tone), "wav")
    assert job["status"] == "failed" and "over" in job["error"], job


@needs_ffmpeg
def test_a_result_that_reached_the_size_cap_fails_instead_of_being_kept(converting, tmp_path_factory):
    owner, _ = converting(limits=dataclasses.replace(ROOMY_LIMITS, max_output_bytes=200 * 1024))
    tone = ffmpeg_fixture(tmp_path_factory, "long.flac",
                          ["-f", "lavfi", "-i", f"sine=frequency=440:duration={LONG_TONE_SECONDS}"])
    job = convert(owner, owner.upload("long.flac", tone), "m4a")
    assert job["status"] == "failed" and "over" in job["error"], job


def test_a_failed_size_estimate_gives_the_conversion_slot_back(converting, monkeypatch):
    from drive.errors import DriveError
    owner, manager = converting(jobs_at_once=1)
    picture = owner.upload("dot.png", image_bytes("PNG"))

    def unreadable_estimate(*_):
        raise DriveError("the size could not be estimated", 422)

    monkeypatch.setattr(manager, "_largest_result", unreadable_estimate)
    assert owner.post(f"/api/files/{picture['id']}/conversions", json={"target": "jpg"}).status_code == 422
    monkeypatch.undo()
    assert convert(owner, picture, "jpg")["status"] == "done"


def test_a_tool_cannot_write_a_file_beyond_the_output_cap(tmp_path):
    from drive.media import OUTPUT_SLACK_BYTES, ConversionFailed, run_tool
    writer = fake_tool(tmp_path / "bin", "writer", FLOODING_TOOL)
    work = tmp_path / "work"
    work.mkdir()
    limits = dataclasses.replace(ROOMY_LIMITS, max_output_bytes=1024 ** 2)
    with pytest.raises(ConversionFailed, match="over 1 MB"):
        run_tool(dataclasses.replace(TOOLS, network_sandbox=()), [writer], work, limits, "writer")
    assert (work / "flood").stat().st_size <= 1024 ** 2 + OUTPUT_SLACK_BYTES


def test_a_tool_stopped_for_size_names_the_same_limit_as_the_result_check(tmp_path):
    from drive.media import ConversionFailed, run_tool
    writer = fake_tool(tmp_path / "bin", "writer", FLOODING_TOOL)
    work = tmp_path / "work"
    work.mkdir()
    limits = dataclasses.replace(ROOMY_LIMITS, promised_bytes=4 * 1024 ** 2)
    with pytest.raises(ConversionFailed) as stopped:
        run_tool(dataclasses.replace(TOOLS, network_sandbox=()), [writer], work, limits, "writer")
    assert f"over {limits.result_size_limit // 1024 ** 2} MB" in str(stopped.value), str(stopped.value)


def test_a_tool_that_runs_too_long_is_killed(converting, tmp_path, wav_bytes):
    slow = fake_tool(tmp_path / "bin", "ffprobe", SLOW_TOOL)
    tools = dataclasses.replace(TOOLS, ffmpeg=slow, ffprobe=slow, network_sandbox=())
    owner, manager = converting(tools=tools, limits=dataclasses.replace(ROOMY_LIMITS, seconds=1))
    began = time.monotonic()
    job = convert(owner, owner.upload("tone.wav", wav_bytes), "mp3")
    assert job["status"] == "failed" and "longer than 1 s" in job["error"]
    assert time.monotonic() - began < 10
    assert list(manager.work_dir.iterdir()) == []


def gone_soon(pid: int) -> bool:
    deadline = time.monotonic() + GONE_WITHIN_SECONDS
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(POLL_SECONDS)
    os.kill(pid, 9)
    return False


@pytest.mark.parametrize("sandboxed", [False, pytest.param(True, marks=needs_documents)])
def test_a_tool_is_killed_with_everything_it_started(tmp_path, sandboxed):
    from drive.media import ConversionFailed, run_tool
    forker = fake_tool(tmp_path / "bin", "forker", FORKING_TOOL)
    work = tmp_path / "work"
    work.mkdir()
    tools = TOOLS if sandboxed else dataclasses.replace(TOOLS, network_sandbox=())
    with pytest.raises(ConversionFailed, match="longer than"):
        run_tool(tools, [forker], work, dataclasses.replace(ROOMY_LIMITS, seconds=2), "forker", sandboxed=sandboxed)
    started = {name: int((work / name).read_text()) for name in ("pid", "child-pid", "escaped-pid", "orphan-pid")}
    assert {name: gone_soon(pid) for name, pid in started.items()} == dict.fromkeys(started, True)


def test_a_tool_is_still_stopped_when_its_processes_cannot_be_listed(tmp_path, monkeypatch):
    from drive import media

    def unreadable_process_list(*_):
        raise PermissionError("the process list cannot be read")

    monkeypatch.setattr(media, "_children", unreadable_process_list)
    monkeypatch.setattr(media, "_process_ids", unreadable_process_list)
    slow = fake_tool(tmp_path / "bin", "slow", SLOW_TOOL)
    work = tmp_path / "work"
    work.mkdir()
    tools = dataclasses.replace(TOOLS, network_sandbox=())
    with pytest.raises(media.ConversionFailed, match="longer than"):
        media.run_tool(tools, [slow], work, dataclasses.replace(ROOMY_LIMITS, seconds=1), "slow")
    assert gone_soon(int((work / "pid").read_text()))


def test_tools_start_with_the_default_pipe_and_file_size_signals(tmp_path):
    from drive.media import run_tool
    reporter = fake_tool(tmp_path / "bin", "reporter", SIGNAL_REPORTING_TOOL)
    work = tmp_path / "work"
    work.mkdir()
    output = run_tool(dataclasses.replace(TOOLS, network_sandbox=()), [reporter], work, ROOMY_LIMITS, "reporter")
    ignored = int(output.read_text().split()[-1], 16)
    assert ignored & (1 << (signal.SIGPIPE - 1)) == 0
    assert ignored & (1 << (signal.SIGXFSZ - 1)) == 0


def test_a_chatty_tool_leaves_only_a_small_head_and_tail_of_its_output(tmp_path):
    from drive.media import PROBE_OUTPUT_LIMIT, STDERR_TAIL_BYTES, ConversionFailed, run_tool
    chatty = fake_tool(tmp_path / "bin", "chatty", CHATTY_TOOL)
    work = tmp_path / "work"
    work.mkdir()
    limits = dataclasses.replace(ROOMY_LIMITS, max_output_bytes=1024 ** 2)
    with pytest.raises(ConversionFailed, match="the real reason"):
        run_tool(dataclasses.replace(TOOLS, network_sandbox=()), [chatty], work, limits, "chatty")
    assert (work / "tool.out").stat().st_size <= PROBE_OUTPUT_LIMIT + 1
    assert (work / "tool.err").stat().st_size <= STDERR_TAIL_BYTES


def open_descriptors() -> int:
    return len(os.listdir("/proc/self/fd"))


def test_a_helper_holding_the_output_open_neither_delays_the_job_nor_leaks(tmp_path):
    from drive.media import run_tool
    helper = fake_tool(tmp_path / "bin", "helper", HELPER_LEAVING_TOOL)
    work = tmp_path / "work"
    work.mkdir()
    descriptors, threads = open_descriptors(), threading.active_count()
    began = time.monotonic()
    try:
        output = run_tool(dataclasses.replace(TOOLS, network_sandbox=()), [helper], work, ROOMY_LIMITS, "helper")
        spent = time.monotonic() - began
        assert output.read_bytes() == b"finished\n"
        assert spent < PIPE_GRACE_BOUND_SECONDS, spent
        assert (open_descriptors(), threading.active_count()) == (descriptors, threads)
    finally:
        os.kill(int((work / "helper-pid").read_text()), signal.SIGKILL)


def hop_still_running(work: Path) -> bool:
    first = (work / "hop-pid").read_text()
    time.sleep(HOP_CHECK_SECONDS)
    return (work / "hop-pid").read_text() != first


def processes_started_under(folder: Path) -> list[int]:
    marker = str(folder).encode()
    found = []
    for entry in Path("/proc").iterdir():
        try:
            if entry.name.isdigit() and marker in (entry / "cmdline").read_bytes():
                found.append(int(entry.name))
        except OSError:
            continue
    return found


def stop_a_chain_and_measure(tmp_path: Path, tool: str | list[str], attempts: int,
                             seconds: int = CHAIN_TOOL_SECONDS) -> list[tuple[bool, list[int], float]]:
    from drive.media import ConversionFailed, run_tool
    command = [tool] if isinstance(tool, str) else tool
    outcomes = []
    for attempt in range(attempts):
        work = tmp_path / f"work-{attempt}"
        work.mkdir()
        began = time.monotonic()
        try:
            with pytest.raises(ConversionFailed, match="longer than"):
                run_tool(dataclasses.replace(TOOLS, network_sandbox=()), command, work,
                         dataclasses.replace(ROOMY_LIMITS, seconds=seconds), "hopping")
            spent = time.monotonic() - began
            time.sleep(HOP_CHECK_SECONDS)
            outcomes.append((hop_still_running(work), processes_started_under(tmp_path), round(spent, 2)))
        finally:
            (work / "stop").write_text("")
    return outcomes


def list_children_or_scan(monkeypatch, children_listed: bool) -> None:
    from drive import media
    if not children_listed:
        monkeypatch.setattr(media, "_children_are_listed", lambda: False)


def fast_hopping_tool(tmp_path: Path, interval: str, ballast_megabytes: int = 0) -> str:
    fake_tool(tmp_path / "bin", "fast-hopper", FAST_HOPPER)
    body = FAST_HOPPING_TOOL.format(interval=interval, ballast=ballast_megabytes)
    return fake_tool(tmp_path / "bin", "fast-hopping", body)


@pytest.mark.parametrize("children_listed", [True, False], ids=CHILDREN_MODES)
def test_a_chain_of_processes_that_keep_leaving_their_session_is_stopped(tmp_path, monkeypatch, children_listed):
    from drive import media
    from drive.media import FREEZE_SECONDS
    if not children_listed:
        monkeypatch.setattr(media, "_children_are_listed", lambda: False)
    fake_tool(tmp_path / "bin", "hopper", HOPPER)
    tool = fake_tool(tmp_path / "bin", "hopping", HOPPING_TOOL)
    outcomes = stop_a_chain_and_measure(tmp_path, tool, attempts=3)
    assert [(escaped, left) for escaped, left, _ in outcomes] == [(False, [])] * 3, outcomes
    assert all(spent < CHAIN_TOOL_SECONDS + FREEZE_SECONDS for _, _, spent in outcomes), outcomes


@pytest.mark.parametrize("interval", ["0.005", "0.001"])
@pytest.mark.parametrize("children_listed", [True, False], ids=CHILDREN_MODES)
def test_a_chain_that_forks_faster_than_one_look_at_the_processes_is_stopped(tmp_path, monkeypatch, interval,
                                                                             children_listed):
    from drive.media import FREEZE_SECONDS
    list_children_or_scan(monkeypatch, children_listed)
    outcomes = stop_a_chain_and_measure(tmp_path, fast_hopping_tool(tmp_path, interval), attempts=4)
    assert [(escaped, left) for escaped, left, _ in outcomes] == [(False, [])] * 4, outcomes
    assert all(spent < CHAIN_TOOL_SECONDS + FREEZE_SECONDS for _, _, spent in outcomes), outcomes


@pytest.mark.parametrize("children_listed", [True, False], ids=CHILDREN_MODES)
def test_a_chain_of_large_processes_whose_forks_take_a_while_is_stopped(tmp_path, monkeypatch, children_listed):
    from drive.media import FREEZE_SECONDS
    list_children_or_scan(monkeypatch, children_listed)
    tool = fast_hopping_tool(tmp_path, "0.002", LARGE_HOP_MEGABYTES)
    outcomes = stop_a_chain_and_measure(tmp_path, tool, attempts=8)
    assert [(escaped, left) for escaped, left, _ in outcomes] == [(False, [])] * 8, outcomes
    assert all(spent < CHAIN_TOOL_SECONDS + FREEZE_SECONDS for _, _, spent in outcomes), outcomes


def ended_children_tool(tmp_path: Path, count: int, interval: str) -> list[str]:
    fake_tool(tmp_path / "bin", "fast-hopper", FAST_HOPPER)
    return [fake_tool(tmp_path / "bin", "leaves-ended-children", ENDED_CHILDREN_TOOL), str(count), interval]


@pytest.mark.parametrize("children_listed", [True, False], ids=CHILDREN_MODES)
def test_a_chain_after_thousands_of_ended_children_is_stopped_without_signalling_them(tmp_path, monkeypatch,
                                                                                    children_listed):
    from drive import media
    from drive.media import FREEZE_SECONDS
    list_children_or_scan(monkeypatch, children_listed)
    stopped = []
    real_signal = media._signal

    def recording_signal(pid: int, signal_number: int) -> None:
        if signal_number == signal.SIGSTOP:
            stopped.append(pid)
        real_signal(pid, signal_number)

    monkeypatch.setattr(media, "_signal", recording_signal)
    tool = ended_children_tool(tmp_path, ENDED_CHILDREN, "0.001")
    outcomes = stop_a_chain_and_measure(tmp_path, tool, attempts=1, seconds=ENDED_CHILDREN_TOOL_SECONDS)
    ended ={int(pid) for pid in (tmp_path / "work-0" / "ended-pids").read_text().split()}
    assert len(ended) == ENDED_CHILDREN
    assert [(escaped, left) for escaped, left, _ in outcomes] == [(False, [])], outcomes
    assert outcomes[0][2] < ENDED_CHILDREN_TOOL_SECONDS + FREEZE_SECONDS, outcomes
    assert not ended & set(stopped)


def still_beating(work: Path) -> bool:
    first = (work / "beat").read_text()
    time.sleep(HOP_CHECK_SECONDS)
    return (work / "beat").read_text() != first


@pytest.mark.parametrize("children_listed", [True, False], ids=CHILDREN_MODES)
def test_a_process_whose_main_thread_ended_while_another_runs_is_killed(tmp_path, monkeypatch, children_listed):
    from drive.media import ConversionFailed, run_tool
    list_children_or_scan(monkeypatch, children_listed)
    tool = fake_tool(tmp_path / "bin", "main-thread-gone", MAIN_THREAD_GONE_TOOL)
    work = tmp_path / "work"
    work.mkdir()
    try:
        with pytest.raises(ConversionFailed, match="longer than"):
            run_tool(dataclasses.replace(TOOLS, network_sandbox=()), [tool], work,
                     dataclasses.replace(ROOMY_LIMITS, seconds=CHAIN_TOOL_SECONDS), "main thread gone")
        assert not still_beating(work)
        assert gone_soon(int((work / "beating-pid").read_text()))
    finally:
        (work / "stop").write_text("")


@pytest.mark.skipif(platform.machine() not in CLONE_SYSCALLS, reason="no clone system call number for this machine")
@pytest.mark.parametrize("children_listed", [True, False], ids=CHILDREN_MODES)
def test_the_child_of_a_tool_waiting_in_vfork_is_killed(tmp_path, monkeypatch, children_listed):
    from drive.media import ConversionFailed, run_tool
    list_children_or_scan(monkeypatch, children_listed)
    tool = fake_tool(tmp_path / "bin", "vfork", VFORK_TOOL.format(clone=CLONE_SYSCALLS[platform.machine()]))
    work = tmp_path / "work"
    work.mkdir()
    try:
        with pytest.raises(ConversionFailed, match="longer than"):
            run_tool(dataclasses.replace(TOOLS, network_sandbox=()), [tool], work,
                     dataclasses.replace(ROOMY_LIMITS, seconds=CHAIN_TOOL_SECONDS), "waiting in vfork")
        assert not still_beating(work)
        assert gone_soon(int((work / "beating-pid").read_text()))
    finally:
        (work / "stop").write_text("")


class ForkFinishingAfterTheStop:
    tool, forker, late_child = 5_000_001, 5_000_002, 5_000_003
    reads_before_the_fork_finishes = 2

    def __init__(self):
        self.parents = {self.tool: 1, self.forker: self.tool}
        self.states = {self.tool: "S", self.forker: "R"}
        self.children = {self.tool: [self.forker], self.forker: []}
        self.reads_since_stop: dict[int, int] = {}
        self.killed: set[int] = set()

    def read_stat(self, entry: Path):
        pid = int(entry.name)
        if pid not in self.states:
            return None
        if pid in self.reads_since_stop:
            self.reads_since_stop[pid] += 1
            self.take_the_stop(pid)
        return self.parents[pid], self.states[pid], 1

    def take_the_stop(self, pid: int) -> None:
        if pid != self.forker:
            self.states[pid] = "T"
            return
        if self.reads_since_stop[pid] >= self.reads_before_the_fork_finishes:
            self.parents[self.late_child], self.states[self.late_child] = self.forker, "R"
            self.children[self.forker], self.children[self.late_child] = [self.late_child], []
            self.states[pid] = "T"

    def listed_children(self, pid: int) -> list[int]:
        return list(self.children.get(pid, []))

    def signal(self, pid: int, signal_number: int) -> None:
        if signal_number == signal.SIGSTOP:
            self.reads_since_stop.setdefault(pid, 0)
        elif signal_number == signal.SIGKILL:
            self.killed.add(pid)


def test_a_child_whose_fork_finishes_after_its_parent_was_told_to_stop_is_killed_too(monkeypatch):
    from drive import media
    table = ForkFinishingAfterTheStop()
    monkeypatch.setattr(media, "_children_are_listed", lambda: True)
    monkeypatch.setattr(media, "_read_stat", table.read_stat)
    monkeypatch.setattr(media, "_children", table.listed_children)
    monkeypatch.setattr(media, "_signal", table.signal)
    monkeypatch.setattr(media, "_signal_group", table.signal)
    media._kill_tree(types.SimpleNamespace(pid=table.tool))
    assert table.states[table.late_child] == "T"
    assert table.late_child in table.killed


class ScriptedProcesses:
    def __init__(self, states: dict[int, str], children: dict[int, list[int]], never_stop: frozenset[int] = frozenset(),
                 when_stopped: dict[int, object] | None = None):
        self.states = dict(states)
        self.children = {pid: list(found) for pid, found in children.items()}
        self.never_stop = never_stop
        self.when_stopped = when_stopped or {}
        self.told_to_stop: set[int] = set()
        self.killed: set[int] = set()

    def read_stat(self, entry: Path):
        pid = int(entry.name)
        if pid not in self.states:
            return None
        if pid in self.told_to_stop and pid not in self.never_stop and self.states[pid] != "T":
            self.states[pid] = "T"
            self.when_stopped.get(pid, lambda: None)()
        return 1, self.states[pid], 1

    def listed_children(self, pid: int) -> list[int]:
        return list(self.children.get(pid, []))

    def signal(self, pid: int, signal_number: int) -> None:
        if signal_number == signal.SIGSTOP:
            self.told_to_stop.add(pid)
        elif signal_number == signal.SIGKILL:
            self.killed.add(pid)

    def kill_tree(self, monkeypatch, tool: int) -> None:
        from drive import media
        monkeypatch.setattr(media, "FREEZE_SECONDS", SCRIPTED_FREEZE_SECONDS)
        monkeypatch.setattr(media, "_children_are_listed", lambda: True)
        monkeypatch.setattr(media, "_read_stat", self.read_stat)
        monkeypatch.setattr(media, "_children", self.listed_children)
        monkeypatch.setattr(media, "_signal", self.signal)
        monkeypatch.setattr(media, "_signal_group", self.signal)
        media._kill_tree(types.SimpleNamespace(pid=tool))


def test_the_child_of_a_process_that_never_stops_is_still_found_and_killed(monkeypatch):
    tool, child = 5_000_001, 5_000_002
    table = ScriptedProcesses({tool: "D", child: "S"}, {tool: [child], child: []}, never_stop=frozenset({tool}))
    table.kill_tree(monkeypatch, tool)
    assert table.states[child] == "T"
    assert {tool, child} <= table.killed


def test_a_process_handed_to_the_tool_by_an_end_that_left_no_zombie_is_found_and_killed(monkeypatch):
    tool, hopper, handed_over = 5_000_001, 5_000_002, 5_000_003

    def hand_a_process_to_the_tool() -> None:
        table.states[handed_over] = "R"
        table.children[tool].append(handed_over)
        table.children[handed_over] = []

    table = ScriptedProcesses({tool: "S", hopper: "R"}, {tool: [hopper], hopper: []},
                              when_stopped={hopper: hand_a_process_to_the_tool})
    table.kill_tree(monkeypatch, tool)
    assert table.states[handed_over] == "T"
    assert handed_over in table.killed


class HopBetweenTwoListReads(ScriptedProcesses):
    def __init__(self, tool: int, parent: int, middle: int, handed_over: int):
        super().__init__({tool: "S", parent: "S", middle: "R"}, {tool: [parent], parent: [middle], middle: []})
        self.tool, self.parent, self.middle, self.handed_over = tool, parent, middle, handed_over
        self.has_hopped = False

    def listed_children(self, pid: int) -> list[int]:
        found = super().listed_children(pid)
        if pid == self.tool and self.states.get(self.parent) == "T" and not self.has_hopped:
            self.has_hopped = True
            del self.states[self.middle]
            self.children[self.parent] = []
            self.states[self.handed_over] = "R"
            self.children[self.tool].append(self.handed_over)
            self.children[self.handed_over] = []
        return found


@pytest.mark.parametrize("tool, parent", [(5_000_001, 5_000_002), (5_000_002, 5_000_001)])
def test_a_process_handed_to_the_tool_between_two_list_reads_of_one_pass_is_found_and_killed(monkeypatch, tool, parent):
    middle, handed_over = 5_000_003, 5_000_004
    table = HopBetweenTwoListReads(tool, parent, middle, handed_over)
    table.kill_tree(monkeypatch, tool)
    assert table.states[handed_over] == "T"
    assert handed_over in table.killed


@pytest.mark.parametrize("children_listed", [True, False], ids=CHILDREN_MODES)
def test_the_freeze_gives_up_at_its_bound_even_inside_one_long_look(tmp_path, monkeypatch, caplog, children_listed):
    from drive import media
    from drive.media import FREEZE_SECONDS
    list_children_or_scan(monkeypatch, children_listed)
    real_read_stat = media._read_stat

    def slow_read_stat(entry: Path):
        time.sleep(SLOW_LOOK_SECONDS)
        return real_read_stat(entry)

    monkeypatch.setattr(media, "_read_stat", slow_read_stat)
    work = tmp_path / "work"
    work.mkdir()
    began = time.monotonic()
    with caplog.at_level(logging.WARNING, logger="drive.media"):
        with pytest.raises(media.ConversionFailed, match="longer than"):
            media.run_tool(dataclasses.replace(TOOLS, network_sandbox=()),
                           ended_children_tool(tmp_path, SLOW_LOOK_CHILDREN, "none"), work,
                           dataclasses.replace(ROOMY_LIMITS, seconds=CHAIN_TOOL_SECONDS), "slow to look at")
    spent = time.monotonic() - began
    assert spent < CHAIN_TOOL_SECONDS + FREEZE_SECONDS + BOUND_SLACK_SECONDS, spent
    assert gone_soon(int((work / "pid").read_text()))
    assert processes_started_under(tmp_path) == []
    assert any(f"{FREEZE_SECONDS} s" in record.getMessage() for record in caplog.records), caplog.text


@pytest.mark.parametrize("children_listed", [True, False], ids=["children lists", "full process scan"])
def test_a_process_name_that_is_not_text_never_leaves_a_tool_running_or_stopped(tmp_path, monkeypatch,
                                                                                 children_listed):
    from drive import media
    from drive.media import ConversionFailed, run_tool
    if not children_listed:
        monkeypatch.setattr(media, "_children_are_listed", lambda: False)
    odd = subprocess.Popen([sys.executable, "-I", "-c", ODD_NAME_PROCESS])
    try:
        deadline = time.monotonic() + GONE_WITHIN_SECONDS
        while Path(f"/proc/{odd.pid}/comm").read_bytes() != b"caf\xe9\n" and time.monotonic() < deadline:
            time.sleep(POLL_SECONDS)
        slow = fake_tool(tmp_path / "bin", "slow", SLOW_TOOL)
        work = tmp_path / "work"
        work.mkdir()
        tools = dataclasses.replace(TOOLS, network_sandbox=())
        with pytest.raises(ConversionFailed, match="longer than"):
            run_tool(tools, [slow], work, dataclasses.replace(ROOMY_LIMITS, seconds=1), "slow")
        assert gone_soon(int((work / "pid").read_text()))
        quick = tmp_path / "quick"
        quick.mkdir()
        assert run_tool(tools, ["/bin/sh", "-c", "echo done"], quick, ROOMY_LIMITS, "quick").read_bytes() == b"done\n"
    finally:
        odd.kill()
        odd.wait()


def test_the_starter_carries_on_when_the_c_library_cannot_be_opened(monkeypatch, capsys):
    from drive import toolstart

    def unopenable(*_, **__):
        raise OSError("no C library here")

    monkeypatch.setattr(toolstart.ctypes, "CDLL", unopenable)
    toolstart.keep_orphans_as_children()
    assert capsys.readouterr().err.startswith(toolstart.STARTER_NOTE)


def test_a_starter_note_is_never_given_as_the_reason_a_tool_failed(tmp_path):
    from drive.media import ConversionFailed, run_tool
    from drive.toolstart import STARTER_NOTE
    quiet = fake_tool(tmp_path / "bin", "quiet", f"#!/bin/sh\necho '{STARTER_NOTE}orphans may escape' >&2\nexit 4\n")
    work = tmp_path / "work"
    work.mkdir()
    with pytest.raises(ConversionFailed, match="stopped with code 4"):
        run_tool(dataclasses.replace(TOOLS, network_sandbox=()), [quiet], work, ROOMY_LIMITS, "quiet")


def test_a_tool_that_wants_too_much_memory_fails(tmp_path):
    from drive.media import ConversionFailed, run_tool
    hungry = fake_tool(tmp_path / "bin", "hungry", HUNGRY_TOOL)
    work = tmp_path / "work"
    work.mkdir()
    limits = dataclasses.replace(ROOMY_LIMITS, memory_bytes=512 * 1024 * 1024)
    with pytest.raises(ConversionFailed, match="MemoryError"):
        run_tool(dataclasses.replace(TOOLS, network_sandbox=()), [hungry], work, limits, "hungry")


def test_more_than_two_at_once_hear_busy(converting, tmp_path, wav_bytes):
    slow = fake_tool(tmp_path / "bin", "ffprobe", SLOW_TOOL)
    tools = dataclasses.replace(TOOLS, ffmpeg=slow, ffprobe=slow, network_sandbox=())
    owner, _ = converting(tools=tools, limits=dataclasses.replace(ROOMY_LIMITS, seconds=3))
    node = owner.upload("tone.wav", wav_bytes)
    first = owner.post(f"/api/files/{node['id']}/conversions", json={"target": "mp3"})
    second = owner.post(f"/api/files/{node['id']}/conversions", json={"target": "flac"})
    third = owner.post(f"/api/files/{node['id']}/conversions", json={"target": "ogg"})
    assert (first.status_code, second.status_code, third.status_code) == (202, 202, 503)
    assert third.headers["Retry-After"] and "try again" in third.get_json()["error"]
    wait_for(owner, first.get_json())
    wait_for(owner, second.get_json())
    assert owner.post(f"/api/files/{node['id']}/conversions", json={"target": "ogg"}).status_code == 202


def test_missing_tools_are_explained(converting, wav_bytes):
    owner, _ = converting(tools=Tools(prlimit=TOOLS.prlimit, ffmpeg=None, ffprobe=None, soffice=None,
                                      network_sandbox=()))
    node = owner.upload("tone.wav", wav_bytes)
    options = owner.get(f"/api/files/{node['id']}/conversions").get_json()
    assert options["ready"] is False and "ffmpeg" in options["reason"]
    assert owner.post(f"/api/files/{node['id']}/conversions", json={"target": "mp3"}).status_code == 503


def test_jobs_are_listed_and_unknown_ids_are_404(converting):
    owner, _ = converting()
    job = convert(owner, owner.upload("dot.png", image_bytes("PNG")), "gif")
    assert [listed["id"] for listed in owner.get("/api/conversions").get_json()["jobs"]] == [job["id"]]
    assert owner.get("/api/conversions/" + "0" * 24).status_code == 404
    assert owner.get("/api/conversions/../etc").status_code == 404
