import dataclasses
import io
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from PIL import Image

import drive.thumbnails
from conftest import SignedInClient, make_app
from drive.conversions import ConversionManager
from drive.media import Limits, find_tools
from drive.memory import MIB, MemoryBudget, NoRoomYet, image_job_bytes, media_job_bytes, thumbnail_bytes
from drive.thumbnails import ThumbnailBusy, ThumbnailMaker

REPO = Path(__file__).resolve().parent.parent
PROBE = Path(__file__).resolve().parent / "memory_probe.py"
BIG_PICTURE_SIZE = (4600, 3500)
PROBE_BUDGET_MB = 520
PROBE_SAFETY_LIMIT = "1500M"
PROCESS_ALLOWANCE_MB = 60
PEAK_PATTERN = re.compile(r"memory peak: (\d+) MB")
PROBE_SECONDS = 300
SHORT_WAIT = 0.05
MEASURED_40MP = (7000, 5700)
MEASURED_THUMB_EXTRA_MB = {"image/webp": 612, "image/avif": 353, "image/png": 214, "image/bmp": 41}
MEASURED_CONVERT_PEAK_MB = {
    ("image/webp", "webp"): 1507, ("image/webp", "jpg"): 1038, ("image/webp", "gif"): 1163,
    ("image/png", "webp"): 1203, ("image/png", "gif"): 1195, ("image/png", "jpg"): 675,
    ("image/avif", "webp"): 1167, ("image/bmp", "gif"): 1194, ("image/jpeg", "webp"): 1126,
}
MEASURED_HEIC_12MP_TO_WEBP_MB = 378
MEASURED_VIDEO_PEAK_MB = {1920 * 1080: 440, 3840 * 2160: 611}
ROOMY_LIMITS = Limits(seconds=60, memory_bytes=3072 * MIB, max_input_bytes=1024 ** 3, max_output_bytes=1024 ** 3,
                      max_pixels=100_000_000)


def can_cap_memory() -> bool:
    if not shutil.which("systemd-run") or not shutil.which("prlimit"):
        return False
    tried = subprocess.run(["systemd-run", "--user", "--scope", "-q", "-p", "MemoryMax=64M", "true"],
                           capture_output=True, timeout=30, check=False)
    return tried.returncode == 0


def noisy_webp(path: Path, size: tuple[int, int]) -> Path:
    noise = Image.effect_noise(size, 60)
    Image.merge("RGB", (noise, noise.transpose(Image.Transpose.FLIP_LEFT_RIGHT), noise)).save(
        path, "WEBP", quality=80, method=0)
    return path


@pytest.mark.skipif(not can_cap_memory(), reason="needs systemd-run --user and prlimit to cap the probe")
def test_thumbnails_and_a_conversion_together_stay_inside_the_memory_limit(tmp_path):
    pictures = [noisy_webp(tmp_path / f"big-{number}.webp", BIG_PICTURE_SIZE) for number in range(3)]
    environment = {**os.environ, "AUTH_MODE": "local", "DATA_DIR": str(tmp_path / "data"), "MIN_FREE_GB": "0",
                   "WORK_MEMORY_MB": str(PROBE_BUDGET_MB), "PYTHONPATH": str(REPO)}
    finished = subprocess.run(
        ["systemd-run", "--user", "--scope", "-q", "-p", f"MemoryMax={PROBE_SAFETY_LIMIT}", "-p", "MemorySwapMax=0",
         "--", sys.executable, str(PROBE), *map(str, pictures)],
        capture_output=True, text=True, timeout=PROBE_SECONDS, env=environment, cwd=tmp_path, check=False)
    assert finished.returncode == 0, (finished.returncode, finished.stdout, finished.stderr[-2000:])
    assert "conversion: done" in finished.stdout
    peak = PEAK_PATTERN.search(finished.stdout)
    if peak is None:
        pytest.skip("this system does not report the cgroup memory peak")
    assert int(peak.group(1)) <= PROBE_BUDGET_MB + PROCESS_ALLOWANCE_MB, finished.stdout


def test_the_estimates_cover_what_was_measured():
    for mime, extra_mb in MEASURED_THUMB_EXTRA_MB.items():
        assert thumbnail_bytes(mime, *MEASURED_40MP) >= extra_mb * MIB, mime
    for (mime, target), peak_mb in MEASURED_CONVERT_PEAK_MB.items():
        assert image_job_bytes(mime, target, *MEASURED_40MP) >= peak_mb * MIB, (mime, target)
    assert image_job_bytes("image/heif", "webp", 4032, 3024) >= MEASURED_HEIC_12MP_TO_WEBP_MB * MIB
    for frame_pixels, peak_mb in MEASURED_VIDEO_PEAK_MB.items():
        assert media_job_bytes(frame_pixels) >= peak_mb * MIB


def test_a_reservation_waits_its_turn_and_never_overfills_the_budget():
    budget = MemoryBudget(100)
    with pytest.raises(ValueError):
        with budget.reserve(101, SHORT_WAIT):
            pass
    with budget.reserve(70, SHORT_WAIT):
        with pytest.raises(NoRoomYet):
            with budget.reserve(40, SHORT_WAIT):
                pass
        with budget.reserve(30, SHORT_WAIT):
            assert budget.used_bytes == 100
    assert budget.used_bytes == 0


def test_a_waiting_large_reservation_is_not_overtaken_by_small_ones():
    budget = MemoryBudget(100)
    order = []
    holder = budget.reserve(60, SHORT_WAIT)
    holder.__enter__()

    def large():
        with budget.reserve(80, 5):
            order.append("large")

    waiting = threading.Thread(target=large)
    waiting.start()
    time.sleep(0.2)
    with pytest.raises(NoRoomYet):
        with budget.reserve(10, SHORT_WAIT):
            order.append("small")
    holder.__exit__(None, None, None)
    waiting.join(5)
    assert order == ["large"]
    assert budget.used_bytes == 0


def picture_bytes(kind: str, size: tuple[int, int]) -> bytes:
    data = io.BytesIO()
    Image.new("RGB", size, (200, 30, 60)).save(data, kind)
    return data.getvalue()


def test_a_thumbnail_waits_while_a_conversion_holds_the_shared_budget(services, owner):
    budget = MemoryBudget(64 * MIB)
    maker = ThumbnailMaker(services.thumbnails.folder, services.blobs, memory=budget)
    node = owner.upload("photo.webp", picture_bytes("WEBP", (1600, 1200)))
    record = services.store.file_record(node["id"])
    with budget.reserve(60 * MIB, SHORT_WAIT):
        with pytest.raises(ThumbnailBusy):
            maker.thumbnail(record)
    assert maker.thumbnail(record) is not None


def test_a_picture_too_big_for_the_budget_gets_no_thumbnail_and_is_not_tried_again(services, owner, monkeypatch):
    calls = []
    real_render = drive.thumbnails.render_thumbnail
    monkeypatch.setattr(drive.thumbnails, "render_thumbnail", lambda *args: calls.append(args) or real_render(*args))
    maker = ThumbnailMaker(services.thumbnails.folder, services.blobs, memory=MemoryBudget(20 * MIB))
    record = services.store.file_record(owner.upload("wide.webp", picture_bytes("WEBP", (2000, 1500)))["id"])
    assert maker.thumbnail(record) is None
    assert maker.thumbnail(record) is None
    assert calls == []


@pytest.fixture
def budgeted_conversions(services):
    def build(budget: MemoryBudget):
        conversions = ConversionManager(services.store, services.blobs, services.disk,
                                        services.thumbnails.folder.parent / "convert", find_tools(), ROOMY_LIMITS,
                                        memory=budget)
        return SignedInClient(make_app(dataclasses.replace(services, conversions=conversions)).test_client())
    return build


@pytest.mark.skipif(not find_tools().images_ready, reason="needs prlimit for image conversions")
def test_a_conversion_bigger_than_the_budget_is_refused_at_once(budgeted_conversions):
    owner = budgeted_conversions(MemoryBudget(256 * MIB))
    node = owner.upload("poster.png", picture_bytes("PNG", (4000, 3000)))
    answer = owner.post(f"/api/files/{node['id']}/conversions", json={"target": "webp"})
    assert answer.status_code == 422
    assert "WORK_MEMORY_MB" in answer.get_json()["error"]


@pytest.mark.skipif(not find_tools().images_ready, reason="needs prlimit for image conversions")
def test_a_conversion_waits_for_room_then_finishes(budgeted_conversions):
    budget = MemoryBudget(256 * MIB)
    owner = budgeted_conversions(budget)
    node = owner.upload("small.png", picture_bytes("PNG", (640, 480)))
    holder = budget.reserve(250 * MIB, SHORT_WAIT)
    holder.__enter__()
    job = owner.post(f"/api/files/{node['id']}/conversions", json={"target": "jpg"}).get_json()
    time.sleep(0.5)
    assert owner.get(f"/api/conversions/{job['id']}").get_json()["status"] == "running"
    holder.__exit__(None, None, None)
    deadline = time.monotonic() + 30
    while owner.get(f"/api/conversions/{job['id']}").get_json()["status"] == "running":
        assert time.monotonic() < deadline
        time.sleep(0.1)
    assert owner.get(f"/api/conversions/{job['id']}").get_json()["status"] == "done"
    assert budget.used_bytes == 0
