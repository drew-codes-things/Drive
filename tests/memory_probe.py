import shutil
import sys
import threading
import time
from pathlib import Path

from drive.__main__ import build_services
from drive.config import load_settings
from drive.thumbnails import ThumbnailBusy
from drive.vault import Vault, load_or_create_vault_key

RETRY_SECONDS = 0.05
GIVE_UP_SECONDS = 120
SECRET = b"p" * 32


def add_picture(services, source: Path) -> dict:
    staged = services.blobs.root / f"staged-{source.name}"
    shutil.copyfile(source, staged)
    size = staged.stat().st_size
    return services.store.add_file(0, source.name, size, services.blobs.adopt(staged), "image/webp")


def wait_for_thumbnail(services, record: dict, outcomes: list) -> None:
    deadline = time.monotonic() + GIVE_UP_SECONDS
    while time.monotonic() < deadline:
        try:
            outcomes.append(services.thumbnails.thumbnail(record) is not None)
            return
        except ThumbnailBusy:
            time.sleep(RETRY_SECONDS)
    outcomes.append(False)


def wait_for_conversion(services, job_id: str) -> str:
    deadline = time.monotonic() + GIVE_UP_SECONDS
    while time.monotonic() < deadline:
        job = services.conversions.job(job_id)
        if job["status"] != "running":
            return job["status"]
        time.sleep(RETRY_SECONDS)
    return "stuck"


def own_memory_peak() -> str:
    cgroup = Path("/proc/self/cgroup").read_text().strip().split("::", 1)[-1]
    peak = Path("/sys/fs/cgroup") / cgroup.lstrip("/") / "memory.peak"
    return f"{int(peak.read_text()) // 1024 ** 2} MB" if peak.exists() else "unknown"


def main(pictures: list[Path]) -> int:
    settings = load_settings()
    services = build_services(settings, SECRET, Vault(load_or_create_vault_key(settings.data_dir / "token.key")))
    records = [services.store.file_record(add_picture(services, picture)["id"]) for picture in pictures]
    *thumbnailed, converted = records
    outcomes: list = []
    workers = [threading.Thread(target=wait_for_thumbnail, args=(services, record, outcomes))
               for record in thumbnailed]
    job = services.conversions.start(converted["id"], "jpg")
    for worker in workers:
        worker.start()
    status = wait_for_conversion(services, job["id"])
    for worker in workers:
        worker.join()
    print(f"thumbnails made: {outcomes}, conversion: {status}, memory peak: {own_memory_peak()}")
    return 0 if all(outcomes) and status == "done" else 1


if __name__ == "__main__":
    sys.exit(main([Path(argument) for argument in sys.argv[1:]]))
