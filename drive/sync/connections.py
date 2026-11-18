import hashlib
import json
import logging
import re
import secrets
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from ..blobs import BlobStore
from ..db import Database
from ..disk import DiskGuard
from ..errors import Conflict, DriveError, NotFound
from ..store import ROOT_ID, DriveStore, as_folder_id
from ..vault import SecretUnreadable, Vault
from .engine import Report, SyncEngine, SyncState
from .google import (GoogleDriveRemote, GoogleEndpoints, GoogleSession, OAuthClient, account_email, new_pkce_pair,
                     scope_for)
from .http import HttpClient
from .local import LocalSide
from .planner import DIRECTIONS, BinLimits
from .remote import SignInNeeded, SyncError, clean_remote_path
from .webdav import WebDavRemote, clean_password, clean_server_url, clean_username, nextcloud_dav_url

PROVIDERS = {"google": "Google Drive", "nextcloud": "Nextcloud", "webdav": "WebDAV"}
WEBDAV_PROVIDERS = ("nextcloud", "webdav")
INTERVALS = (0, 15, 60, 360, 1440)
SYNCS_AT_ONCE = 2
BIN_PAGE = 500
CARD_COLUMNS = ("id, name, provider, folder_id, remote_path, direction, interval_minutes, paused, server_url, "
                "username, account, secret, created_at, previewed_at, last_run_at, last_finished_at, "
                "last_success_at, last_status, last_counts, last_errors, last_skipped, last_held")
SIGN_IN_SECONDS = 600
SCHEDULE_TICK_SECONDS = 60
LONGEST_LABEL = 100
STATE_BYTES = 32
LONGEST_STATE = 200
LONGEST_GOOGLE_ERROR = 100
APPROVAL_PATTERN = re.compile(r"[0-9a-f]{64}")
EDITABLE = frozenset({"name", "intervalMinutes", "paused", "password"})
BUSY = 503
STALE_SIGN_IN_TEXT = "This Google sign-in link is old or was already used. Start the sign-in again from Cloud sync."

log = logging.getLogger("drive.sync")


class SyncBusy(DriveError):
    def __init__(self):
        super().__init__(f"{SYNCS_AT_ONCE} syncs are already running; try again when one has finished", BUSY)


@dataclass(frozen=True)
class GoogleSetup:
    client_id: str
    client_secret: str
    endpoints: GoogleEndpoints = GoogleEndpoints()


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _label(raw) -> str:
    if not isinstance(raw, str) or not raw.strip() or len(raw.strip()) > LONGEST_LABEL or not raw.isprintable():
        raise DriveError(f"give the connection a name of up to {LONGEST_LABEL} characters")
    return raw.strip()


def _choice(raw, allowed, what: str):
    if isinstance(raw, bool) or raw not in allowed:
        raise DriveError(f"{what} must be one of {', '.join(str(value) for value in allowed)}")
    return raw


def _loads(raw: str | None, fallback):
    try:
        return json.loads(raw) if raw else fallback
    except ValueError:
        return fallback


def _first_bin_page(held: dict | None) -> dict | None:
    return None if held is None else {**held, "files": held["files"][:BIN_PAGE]}


def _split_held(held: dict | None) -> tuple[str | None, str | None]:
    if not held:
        return None, None
    return json.dumps(_first_bin_page(held)), json.dumps(held["files"])


def _approval(raw) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str) or not APPROVAL_PATTERN.fullmatch(raw):
        raise DriveError("approveBins must be the approval shown with the list of files to bin")
    return raw


def _printable(text: str, limit: int) -> str:
    return "".join(character for character in text if character.isprintable())[:limit]


def _run_status(report: Report) -> str:
    if report.held_bins:
        return "held"
    if report.stopped:
        return "failed"
    return "errors" if report.counts.get("errors") else "ok"


class SyncService:
    def __init__(self, database: Database, store: DriveStore, blobs: BlobStore, disk: DiskGuard, vault: Vault,
                 work_dir: Path, max_file_bytes: int, google: GoogleSetup | None = None, http_factory=HttpClient,
                 clock=time.time, syncs_at_once: int = SYNCS_AT_ONCE, bin_limits: BinLimits = BinLimits()):
        self.database = database
        self.store = store
        self.blobs = blobs
        self.disk = disk
        self.vault = vault
        self.work_dir = Path(work_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.max_file_bytes = max_file_bytes
        self.google = google
        self.http_factory = http_factory
        self.clock = clock
        self.syncs_at_once = syncs_at_once
        self.bin_limits = bin_limits
        self._running: dict[int, str] = {}
        self._lock = threading.Lock()

    def clear_leftovers(self) -> None:
        for leftover in self.work_dir.iterdir():
            shutil.rmtree(leftover, ignore_errors=True)

    def _oauth(self, http: HttpClient) -> OAuthClient:
        if self.google is None:
            raise DriveError("Google sign-in is not set up on this server yet (GOOGLE_CLIENT_ID and "
                             "GOOGLE_CLIENT_SECRET); the README has the steps", 409)
        return OAuthClient(self.google.client_id, self.google.client_secret, self.google.endpoints, http, self.clock)

    def _row(self, connection_id: int, columns: str = "*"):
        with self.database.connect() as connection:
            row = connection.execute(f"SELECT {columns} FROM sync_connections WHERE id = ?",
                                     (connection_id,)).fetchone()
        if row is None:
            raise NotFound("that sync connection is not here any more")
        return row

    def _folder_path(self, folder_id: int) -> str | None:
        try:
            return " / ".join(["My Drive"] + [step["name"] for step in self.store.path(folder_id)])
        except NotFound:
            return None

    def _next_run(self, row) -> float | None:
        if row["paused"] or not row["interval_minutes"] or row["last_success_at"] is None:
            return None
        return (row["last_run_at"] or 0) + row["interval_minutes"] * 60

    def _view(self, row, running: dict[int, str]) -> dict:
        folder_path = self._folder_path(row["folder_id"])
        return {
            "id": row["id"], "name": row["name"], "provider": row["provider"],
            "providerLabel": PROVIDERS.get(row["provider"], row["provider"]), "folderId": row["folder_id"],
            "folderPath": folder_path, "folderMissing": folder_path is None, "remotePath": row["remote_path"],
            "direction": row["direction"], "intervalMinutes": row["interval_minutes"], "paused": bool(row["paused"]),
            "serverUrl": row["server_url"], "username": row["username"], "account": row["account"],
            "hasSecret": row["secret"] is not None,
            "needsSignIn": row["provider"] == "google" and row["secret"] is None,
            "activity": running.get(row["id"]), "createdAt": row["created_at"],
            "previewedAt": row["previewed_at"], "lastRunAt": row["last_run_at"],
            "lastFinishedAt": row["last_finished_at"], "lastSuccessAt": row["last_success_at"],
            "lastStatus": row["last_status"], "lastCounts": _loads(row["last_counts"], {}),
            "lastErrors": _loads(row["last_errors"], []), "lastSkipped": _loads(row["last_skipped"], []),
            "held": _first_bin_page(_loads(row["last_held"], None)), "nextRunAt": self._next_run(row),
        }

    def _running_now(self) -> dict[int, str]:
        with self._lock:
            return dict(self._running)

    def views(self) -> list[dict]:
        running = self._running_now()
        with self.database.connect() as connection:
            rows = connection.execute(f"SELECT {CARD_COLUMNS} FROM sync_connections"
                                      " ORDER BY name COLLATE NOCASE, id").fetchall()
        return [self._view(row, running) for row in rows]

    def get(self, connection_id: int) -> dict:
        running = self._running_now()
        return self._view(self._row(connection_id, CARD_COLUMNS), running)

    def _overlaps(self, folder_id: int, other_id: int) -> bool:
        if ROOT_ID in (folder_id, other_id) or folder_id == other_id:
            return True
        return self.store.is_within(folder_id, other_id) or self.store.is_within(other_id, folder_id)

    def _check_overlap(self, folder_id: int, connection_id: int = 0) -> None:
        with self.database.connect() as connection:
            rows = connection.execute("SELECT name, folder_id FROM sync_connections WHERE id != ?",
                                      (connection_id,)).fetchall()
        for row in rows:
            if self._overlaps(folder_id, row["folder_id"]):
                raise Conflict(f"that folder overlaps the folder of the connection {row['name']!r}; two connections "
                               "on the same files would undo each other")

    def create(self, body: dict) -> dict:
        name = _label(body.get("name"))
        provider = _choice(body.get("provider"), tuple(PROVIDERS), "provider")
        direction = _choice(body.get("direction"), DIRECTIONS, "direction")
        interval = _choice(body.get("intervalMinutes", 0), INTERVALS, "intervalMinutes")
        folder_id = as_folder_id(body.get("folderId"))
        self.store.require_folder(folder_id)
        remote_path = clean_remote_path(body.get("remotePath"))
        server_url, username, secret = "", "", None
        if provider in WEBDAV_PROVIDERS:
            server_url = clean_server_url(body.get("serverUrl"))
            username = clean_username(body.get("username"))
            secret = self.vault.seal(clean_password(body.get("password")))
        else:
            self._oauth(self.http_factory())
        with self.store.lock:
            self._check_overlap(folder_id)
            with self.database.connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO sync_connections (name, provider, folder_id, remote_path, direction, interval_minutes,"
                    " server_url, username, secret, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (name, provider, folder_id, remote_path, direction, interval, server_url, username, secret,
                     self.clock()))
        return self.get(cursor.lastrowid)

    def update(self, connection_id: int, body: dict) -> dict:
        row = self._row(connection_id)
        unknown = set(body) - EDITABLE
        if unknown:
            raise DriveError(f"{', '.join(sorted(unknown))} cannot be changed; make a new connection instead")
        changes = {}
        if "name" in body:
            changes["name"] = _label(body["name"])
        if "intervalMinutes" in body:
            changes["interval_minutes"] = _choice(body["intervalMinutes"], INTERVALS, "intervalMinutes")
        if "paused" in body:
            if not isinstance(body["paused"], bool):
                raise DriveError("paused must be true or false")
            changes["paused"] = int(body["paused"])
        if "password" in body:
            if row["provider"] not in WEBDAV_PROVIDERS:
                raise DriveError("Google connections use Sign in with Google, not a password")
            changes["secret"] = self.vault.seal(clean_password(body["password"]))
        if changes:
            assignments = ", ".join(f"{column} = ?" for column in changes)
            with self.database.connect() as connection:
                connection.execute(f"UPDATE sync_connections SET {assignments} WHERE id = ?",
                                   (*changes.values(), connection_id))
        return self.get(connection_id)

    def delete(self, connection_id: int) -> None:
        row = self._row(connection_id)
        if connection_id in self._running:
            raise Conflict("this connection is syncing; wait for it to finish, then delete it")
        if row["provider"] == "google" and row["secret"] is not None and not self._account_shared(row):
            self._revoke(row["secret"])
        with self.database.connect() as connection:
            connection.execute("DELETE FROM sync_entries WHERE connection_id = ?", (connection_id,))
            connection.execute("DELETE FROM sync_connections WHERE id = ?", (connection_id,))
        shutil.rmtree(self.work_dir / str(connection_id), ignore_errors=True)

    def _account_shared(self, row) -> bool:
        with self.database.connect() as connection:
            other = connection.execute(
                "SELECT 1 FROM sync_connections WHERE provider = 'google' AND account = ? AND id != ?"
                " AND secret IS NOT NULL", (row["account"], row["id"])).fetchone()
        return other is not None

    def _revoke(self, sealed) -> None:
        try:
            self._oauth(self.http_factory()).revoke(self.vault.open(sealed))
        except (SecretUnreadable, SyncError, DriveError) as error:
            log.warning("could not take back a Google sign-in: %s", type(error).__name__)

    def preview_result(self, connection_id: int) -> dict:
        return _loads(self._row(connection_id)["last_preview"], {})

    def held_page(self, connection_id: int, start: int) -> dict:
        row = self._row(connection_id)
        held = _loads(row["last_held"], None)
        if held is None:
            raise NotFound("no files are waiting for a yes on this connection")
        files = _loads(row["last_held_files"], None) or held["files"]
        if start >= len(files) and len(files) < held["bins"]:
            raise Conflict(f"an older version of Drive kept only the first {len(files)} of these {held['bins']} files; "
                           "press Sync now to list them all again")
        following = start + BIN_PAGE
        return {"bins": held["bins"], "approval": held["approval"], "files": files[start:following],
                "next": following if following < len(files) else None}

    def start(self, connection_id: int, mode: str, approve_bins=None) -> dict:
        approval = _approval(approve_bins)
        row = self._row(connection_id)
        if approval is not None and approval != (_loads(row["last_held"], None) or {}).get("approval"):
            raise Conflict("this yes is out of date: the files to bin have changed since that list; look at the new "
                           "list and confirm again")
        if row["provider"] == "google" and row["secret"] is None:
            raise Conflict("sign in with Google first")
        if mode == "run" and row["previewed_at"] is None and row["last_success_at"] is None:
            raise Conflict("look at the preview of the first sync before running it")
        with self._lock:
            if connection_id in self._running:
                raise Conflict("this connection is already syncing")
            if len(self._running) >= self.syncs_at_once:
                raise SyncBusy()
            self._running[connection_id] = mode
        if mode == "run":
            with self.database.connect() as connection:
                connection.execute("UPDATE sync_connections SET last_run_at = ? WHERE id = ?",
                                   (self.clock(), connection_id))
        threading.Thread(target=self._work, args=(connection_id, mode, approval), daemon=True,
                         name=f"sync-{connection_id}").start()
        return self.get(connection_id)

    def _remote(self, row, http: HttpClient):
        try:
            secret = self.vault.open(row["secret"])
        except SecretUnreadable as error:
            raise SignInNeeded(str(error)) from error
        if row["provider"] == "google":
            session = GoogleSession(self._oauth(http), secret, http)
            return GoogleDriveRemote(session, self.google.endpoints, row["remote_path"])
        base = row["server_url"]
        if row["provider"] == "nextcloud":
            base = nextcloud_dav_url(row["server_url"], row["username"])
        return WebDavRemote(base, row["username"], secret, row["remote_path"], http,
                            label=PROVIDERS[row["provider"]], clock=self.clock)

    def _engine(self, row) -> SyncEngine:
        http = self.http_factory()
        local = LocalSide(self.store, self.blobs, self.disk, row["folder_id"], self.work_dir / str(row["id"]))
        return SyncEngine(SyncState(self.database, row["id"]), local, self._remote(row, http), row["direction"],
                          self.max_file_bytes, self.clock, self.bin_limits,
                          synced_before=row["last_success_at"] is not None)

    def _work(self, connection_id: int, mode: str, approval: str | None = None) -> None:
        report = Report()
        try:
            row = self._row(connection_id)
            self._check_overlap(row["folder_id"], connection_id)
            engine = self._engine(row)
            report = engine.preview() if mode == "preview" else engine.run(approval)
        except SignInNeeded as error:
            report.stopped, report.sign_in_needed = str(error), True
        except (SyncError, DriveError) as error:
            report.stopped = str(error)
        except Exception as error:
            log.exception("sync %s of connection %d failed", mode, connection_id)
            report.stopped = f"the sync failed inside Drive ({type(error).__name__}); the log has the details"
        try:
            self._save_report(connection_id, mode, report)
        finally:
            with self._lock:
                self._running.pop(connection_id, None)

    def _save_report(self, connection_id: int, mode: str, report: Report) -> None:
        now = self.clock()
        with self.database.connect() as connection:
            if mode == "preview":
                held = report.held or {}
                preview = {"at": now, "counts": report.counts, "actions": report.actions,
                           "skipped": report.shown_skips(), "notes": report.notes, "stopped": report.stopped,
                           "binsHeld": report.held_bins, "binFiles": held.get("files", [])[:BIN_PAGE],
                           "binApproval": held.get("approval")}
                connection.execute(
                    "UPDATE sync_connections SET last_preview = ?, previewed_at = CASE WHEN ? THEN previewed_at"
                    " ELSE ? END WHERE id = ?", (json.dumps(preview), report.stopped is not None, now, connection_id))
            else:
                status = _run_status(report)
                errors = ([report.stopped] if report.stopped else []) + report.errors
                connection.execute(
                    "UPDATE sync_connections SET last_finished_at = ?, last_status = ?, last_counts = ?,"
                    " last_errors = ?, last_skipped = ?, last_success_at = CASE WHEN ? THEN last_success_at"
                    " ELSE ? END WHERE id = ?",
                    (now, status, json.dumps(report.counts), json.dumps(errors), json.dumps(report.shown_skips()),
                     report.stopped is not None, now, connection_id))
            if report.held or not report.stopped:
                held_card, held_files = _split_held(report.held)
                connection.execute("UPDATE sync_connections SET last_held = ?, last_held_files = ? WHERE id = ?",
                                   (held_card, held_files, connection_id))
            if report.sign_in_needed:
                connection.execute("UPDATE sync_connections SET last_errors = ?,"
                                   " secret = CASE WHEN provider = 'google' THEN NULL ELSE secret END WHERE id = ?",
                                   (json.dumps([report.stopped]), connection_id))

    def run_due(self) -> list[int]:
        now = self.clock()
        with self.database.connect() as connection:
            rows = connection.execute("SELECT * FROM sync_connections WHERE paused = 0 AND interval_minutes > 0"
                                      " AND last_success_at IS NOT NULL AND secret IS NOT NULL").fetchall()
        started = []
        for row in rows:
            due = self._next_run(row)
            if due is None or due > now or row["id"] in self._running:
                continue
            try:
                self.start(row["id"], "run")
            except DriveError as refused:
                log.info("scheduled sync of connection %d waits: %s", row["id"], refused)
                continue
            started.append(row["id"])
        return started

    def keep_scheduling(self, stop: threading.Event) -> None:
        while not stop.wait(SCHEDULE_TICK_SECONDS):
            try:
                self.run_due()
            except Exception:
                log.exception("the sync schedule check failed; trying again in %d s", SCHEDULE_TICK_SECONDS)

    def google_start(self, connection_id: int, redirect_uri: str) -> str:
        row = self._row(connection_id)
        if row["provider"] != "google":
            raise DriveError("only Google connections sign in with Google")
        oauth = self._oauth(self.http_factory())
        state = secrets.token_urlsafe(STATE_BYTES)
        verifier, challenge = new_pkce_pair()
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE sync_connections SET sign_in_state = ?, sign_in_verifier = ?, sign_in_redirect = ?,"
                " sign_in_started_at = ? WHERE id = ?",
                (_digest(state), self.vault.seal(verifier), redirect_uri, self.clock(), connection_id))
        return oauth.authorization_url(redirect_uri, state, challenge, scope_for(row["direction"]))

    def _take_sign_in(self, state) -> tuple:
        if not isinstance(state, str) or not state or len(state) > LONGEST_STATE:
            raise NotFound(STALE_SIGN_IN_TEXT)
        with self.database.connect() as connection:
            row = connection.execute("SELECT * FROM sync_connections WHERE sign_in_state = ?",
                                     (_digest(state),)).fetchone()
            if row is None:
                raise NotFound(STALE_SIGN_IN_TEXT)
            connection.execute("UPDATE sync_connections SET sign_in_state = NULL, sign_in_verifier = NULL,"
                               " sign_in_redirect = NULL, sign_in_started_at = NULL WHERE id = ?", (row["id"],))
        return row

    def _note(self, connection_id: int, message: str) -> None:
        with self.database.connect() as connection:
            connection.execute("UPDATE sync_connections SET last_errors = ? WHERE id = ?",
                               (json.dumps([message] if message else []), connection_id))

    def google_finish(self, state, code, google_error) -> int:
        row = self._take_sign_in(state)
        connection_id = row["id"]
        if (row["sign_in_started_at"] or 0) + SIGN_IN_SECONDS < self.clock():
            self._note(connection_id, "the Google sign-in took longer than 10 minutes; start it again")
            return connection_id
        if google_error or not isinstance(code, str) or not code:
            reason = _printable(str(google_error or "no code"), LONGEST_GOOGLE_ERROR)
            self._note(connection_id, f"the Google sign-in was not finished ({reason})")
            return connection_id
        try:
            self._finish_with_code(row, code)
        except (SyncError, DriveError, SecretUnreadable) as error:
            self._note(connection_id, f"the Google sign-in did not work: {error}")
        return connection_id

    def _finish_with_code(self, row, code: str) -> None:
        http = self.http_factory()
        oauth = self._oauth(http)
        grant = oauth.exchange_code(code, self.vault.open(row["sign_in_verifier"]), row["sign_in_redirect"])
        if scope_for(row["direction"]) not in grant.scopes:
            oauth.revoke(grant.refresh_token or grant.access_token)
            raise SyncError("the Google Drive permission was not ticked on Google's page; sign in again and tick it")
        if not grant.refresh_token:
            raise SyncError("Google did not hand out a lasting sign-in; remove Drive at "
                            "https://myaccount.google.com/connections and sign in again")
        email = account_email(GoogleSession(oauth, grant.refresh_token, http, grant), self.google.endpoints)
        with self.database.connect() as connection:
            connection.execute("UPDATE sync_connections SET secret = ?, account = ?, last_errors = '[]' WHERE id = ?",
                               (self.vault.seal(grant.refresh_token), email, row["id"]))
