import logging
import sys
import tempfile
import threading

from waitress import serve

from .auth import LocalAuth, ProxyAuth, SecretKeyError, load_or_create_secret
from .blobs import BlobStore
from .config import ConfigError, Settings, load_settings
from .conversions import ConversionManager
from .db import Database
from .disk import DiskGuard
from .media import MAX_CONVERT_PIXELS, Limits, find_tools
from .memory import MIB, MemoryBudget
from .shares import ShareStore
from .signin import OWNER_FILE_NAME, OwnerAccount, PasswordAuth
from .store import DriveStore
from .streams import SHARE_STREAM_LIMIT, StreamGuard
from .sync.connections import GoogleSetup, SyncService
from .sync.planner import BinLimits
from .thumbnails import ThumbnailMaker
from .uploads import UploadManager
from .vault import Vault, VaultKeyError, load_or_create_vault_key
from .web import MAX_REQUEST_BYTES, Services, create_app

WAITRESS_THREADS = 16
SESSION_COOKIE = "drive_session"
WAITRESS_CHANNEL_TIMEOUT_SECONDS = 300
TIDY_INTERVAL_SECONDS = 3600

log = logging.getLogger("drive")


def build_auth(settings: Settings, secret: bytes):
    if settings.auth_mode == "local":
        return LocalAuth(settings.local_user, settings.port)
    if settings.auth_mode == "proxy":
        return ProxyAuth(settings.trusted_proxies, settings.user_header, settings.allowed_users, settings.proxy_secret)
    return PasswordAuth(OwnerAccount(settings.data_dir / OWNER_FILE_NAME), secret, SESSION_COOKIE, settings.secure_cookies,
                        settings.allowed_hosts, trusted_proxies=settings.trusted_proxies)


def use_data_disk_for_temporary_files(settings: Settings) -> None:
    folder = settings.data_dir / "tmp"
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    tempfile.tempdir = str(folder)


def conversion_limits(settings: Settings) -> Limits:
    return Limits(seconds=settings.convert_seconds, memory_bytes=settings.convert_memory_bytes,
                  max_input_bytes=settings.convert_max_input_bytes,
                  max_output_bytes=settings.convert_max_output_bytes, max_pixels=MAX_CONVERT_PIXELS)


def build_conversions(settings: Settings, store: DriveStore, blobs: BlobStore, disk: DiskGuard,
                      memory: MemoryBudget) -> ConversionManager:
    tools = find_tools()
    log.info("conversions: images %s, audio and video %s, documents %s; %d MB set aside for pictures and conversions",
             *("on" if ready else "off" for ready in (tools.images_ready, tools.media_ready, tools.documents_ready)),
             memory.total_bytes // MIB)
    conversions = ConversionManager(store, blobs, disk, settings.data_dir / "tmp" / "convert", tools,
                                    conversion_limits(settings), memory=memory)
    conversions.clear_leftovers()
    return conversions


def build_sync(settings: Settings, database: Database, store: DriveStore, blobs: BlobStore, disk: DiskGuard,
               vault: Vault) -> SyncService:
    google = GoogleSetup(settings.google_client_id, settings.google_client_secret) if settings.google_ready else None
    sync = SyncService(database, store, blobs, disk, vault, settings.data_dir / "tmp" / "sync", settings.max_file_bytes,
                       google=google,
                       bin_limits=BinLimits(settings.sync_bin_limit_files, settings.sync_bin_limit_percent))
    sync.clear_leftovers()
    return sync


def build_services(settings: Settings, secret: bytes, vault: Vault) -> Services:
    database = Database(settings.database_path)
    blobs = BlobStore(settings.data_dir / "blobs")
    store = DriveStore(database, blobs)
    disk = DiskGuard(settings.data_dir, settings.min_free_bytes)
    uploads = UploadManager(database, store, blobs, disk, settings.data_dir / "uploads", settings.max_file_bytes)
    shares = ShareStore(database, store, secret)
    memory = MemoryBudget(settings.work_memory_bytes)
    return Services(store=store, blobs=blobs, uploads=uploads, shares=shares, disk=disk,
                    shares_enabled=settings.shares_enabled, zip_max_bytes=settings.zip_max_bytes,
                    streams=StreamGuard(SHARE_STREAM_LIMIT),
                    thumbnails=ThumbnailMaker(settings.data_dir / "thumbs", blobs, memory=memory),
                    conversions=build_conversions(settings, store, blobs, disk, memory),
                    sync=build_sync(settings, database, store, blobs, disk, vault))


def tidy(services: Services) -> None:
    removed = services.store.purge_expired()
    stale = services.uploads.purge_stale()
    thumbs = services.thumbnails.prune() if services.thumbnails else 0
    if removed or stale or thumbs:
        log.info("tidy: %d items older than the bin limit deleted, %d unfinished uploads dropped, %d thumbnails "
                 "of deleted files removed", removed, stale, thumbs)


def keep_tidying(services: Services, stop: threading.Event) -> None:
    while not stop.wait(TIDY_INTERVAL_SECONDS):
        try:
            tidy(services)
        except Exception:
            log.exception("tidy failed; trying again in %d s", TIDY_INTERVAL_SECONDS)


def waitress_options(settings: Settings) -> dict:
    keeps_forwarded_for = settings.auth_mode == "builtin" and bool(settings.trusted_proxies)
    return {"clear_untrusted_proxy_headers": not keeps_forwarded_for}


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        settings = load_settings()
    except ConfigError as error:
        log.error("configuration problem: %s", error)
        return 2
    try:
        secret = load_or_create_secret(settings.data_dir, settings.secret_key)
    except SecretKeyError as error:
        log.error("secret key problem: %s", error)
        return 2
    try:
        vault = Vault(load_or_create_vault_key(settings.vault_key_path))
    except VaultKeyError as error:
        log.error("token key problem: %s", error)
        return 2
    use_data_disk_for_temporary_files(settings)
    services = build_services(settings, secret, vault)
    services.uploads.restore_promises()
    tidy(services)
    threading.Thread(target=keep_tidying, args=(services, threading.Event()), daemon=True, name="tidy").start()
    threading.Thread(target=services.streams.keep_watching, args=(threading.Event(),), daemon=True,
                     name="stalled-downloads").start()
    threading.Thread(target=services.sync.keep_scheduling, args=(threading.Event(),), daemon=True,
                     name="sync-schedule").start()
    auth = build_auth(settings, secret)
    if isinstance(auth, PasswordAuth) and auth.needs_setup():
        auth.setup_code()
    app = create_app(services, auth, secret, settings.public_scheme)
    log.info("Drive on http://%s:%d, data in %s, sign-in %s, share links %s, keeps %d GB free, Google sign-in %s",
             settings.bind_host, settings.port, settings.data_dir, settings.auth_mode,
             "on" if settings.shares_enabled else "off", settings.min_free_bytes // 1024 ** 3,
             "set up" if settings.google_ready else "not set up")
    serve(app, host=settings.bind_host, port=settings.port, threads=WAITRESS_THREADS,
          max_request_body_size=MAX_REQUEST_BYTES, channel_timeout=WAITRESS_CHANNEL_TIMEOUT_SECONDS,
          **waitress_options(settings))
    return 0


if __name__ == "__main__":
    sys.exit(main())
