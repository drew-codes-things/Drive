import base64
import hashlib
import json
import logging
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit

from .http import Answer, HttpClient
from .remote import (FolderMissing, Listing, RemoteEntry, RemoteRefused, RemoteUnreachable, SignInNeeded, SyncError,
                     name_of, parent_of, shown)

FULL_SCOPE = "https://www.googleapis.com/auth/drive"
APP_FILES_SCOPE = "https://www.googleapis.com/auth/drive.file"
FOLDER_TYPE = "application/vnd.google-apps.folder"
GOOGLE_APP_PREFIX = "application/vnd.google-apps."
FILE_FIELDS = "id,name,mimeType,size,md5Checksum,modifiedTime,trashed"
LIST_FIELDS = f"nextPageToken,files({FILE_FIELDS})"
PAGE_SIZE = 1000
PKCE_VERIFIER_BYTES = 64
RENEW_EARLY_SECONDS = 60
RATE_LIMIT_REASONS = frozenset({"rateLimitExceeded", "userRateLimitExceeded"})
JSON_HEADERS = {"Content-Type": "application/json; charset=utf-8"}
FORM_HEADERS = {"Content-Type": "application/x-www-form-urlencoded"}
UPLOAD_TYPE = "application/octet-stream"
RFC3339_SECONDS = "%Y-%m-%dT%H:%M:%S"

log = logging.getLogger("drive.sync.google")


@dataclass(frozen=True)
class GoogleEndpoints:
    authorize: str = "https://accounts.google.com/o/oauth2/v2/auth"
    token: str = "https://oauth2.googleapis.com/token"
    revoke: str = "https://oauth2.googleapis.com/revoke"
    api: str = "https://www.googleapis.com/drive/v3"
    upload: str = "https://www.googleapis.com/upload/drive/v3"


@dataclass(frozen=True)
class Grant:
    access_token: str
    expires_at: float
    refresh_token: str | None
    scopes: frozenset


def scope_for(direction: str) -> str:
    return APP_FILES_SCOPE if direction == "upload" else FULL_SCOPE


def new_pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(PKCE_VERIFIER_BYTES)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def _json(answer: Answer) -> dict:
    try:
        data = json.loads(answer.body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _is_listed_file(item) -> bool:
    return isinstance(item, dict) and isinstance(item.get("id"), str) and isinstance(item.get("name"), str)


def _listing_page(answer: Answer) -> dict:
    try:
        data = json.loads(answer.body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise RemoteUnreachable("Google Drive sent a folder listing that was cut off or unreadable, so the run "
                                "stopped before changing anything") from error
    files = data.get("files") if isinstance(data, dict) else None
    if not isinstance(files, list) or not all(_is_listed_file(item) for item in files):
        raise RemoteUnreachable("Google Drive sent a folder listing without a proper file list, so the run "
                                "stopped before changing anything")
    return data


def _error_reason(answer: Answer) -> str:
    error = _json(answer).get("error")
    if isinstance(error, dict):
        message = str(error.get("message") or "")
        return message[:200] if message.isprintable() else ""
    return str(error or "")[:100]


def _is_rate_limited(answer: Answer) -> bool:
    if answer.status != 403:
        return False
    error = _json(answer).get("error")
    details = error.get("errors") if isinstance(error, dict) else None
    return any(isinstance(item, dict) and item.get("reason") in RATE_LIMIT_REASONS for item in details or [])


def _modified(raw) -> float | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.strptime(raw[:19], RFC3339_SECONDS).replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


class OAuthClient:
    def __init__(self, client_id: str, client_secret: str, endpoints: GoogleEndpoints, http: HttpClient,
                 clock=time.time):
        self.client_id = client_id
        self.client_secret = client_secret
        self.endpoints = endpoints
        self.http = http
        self.clock = clock

    def authorization_url(self, redirect_uri: str, state: str, challenge: str, scope: str) -> str:
        query = urlencode({
            "client_id": self.client_id, "redirect_uri": redirect_uri, "response_type": "code", "scope": scope,
            "access_type": "offline", "prompt": "select_account consent", "state": state,
            "code_challenge": challenge, "code_challenge_method": "S256",
        })
        return f"{self.endpoints.authorize}?{query}"

    def _post_token(self, fields: dict) -> Answer:
        body = urlencode({"client_id": self.client_id, "client_secret": self.client_secret, **fields}).encode()
        return self.http.call("POST", self.endpoints.token, FORM_HEADERS, body)

    def _grant(self, answer: Answer, refresh_token: str | None) -> Grant:
        data = _json(answer)
        access_token, expires_in = data.get("access_token"), data.get("expires_in")
        if not isinstance(access_token, str) or not access_token or not isinstance(expires_in, (int, float)):
            raise SyncError(f"Google's token answer had no access token (status {answer.status})")
        fresh_refresh = data.get("refresh_token")
        return Grant(access_token=access_token, expires_at=self.clock() + float(expires_in),
                     refresh_token=fresh_refresh if isinstance(fresh_refresh, str) and fresh_refresh else refresh_token,
                     scopes=frozenset(str(data.get("scope") or "").split()))

    def exchange_code(self, code: str, verifier: str, redirect_uri: str) -> Grant:
        answer = self._post_token({"grant_type": "authorization_code", "code": code, "code_verifier": verifier,
                                   "redirect_uri": redirect_uri})
        if answer.status != 200:
            raise SyncError(f"Google refused the sign-in code: {_json(answer).get('error') or answer.status}")
        return self._grant(answer, None)

    def refresh(self, refresh_token: str) -> Grant:
        answer = self._post_token({"grant_type": "refresh_token", "refresh_token": refresh_token})
        if answer.status in (400, 401) and _json(answer).get("error") in ("invalid_grant", "unauthorized_client"):
            raise SignInNeeded("Google no longer accepts this sign-in (it ran out or access was taken back); "
                               "sign in again")
        if answer.status != 200:
            raise SyncError(f"Google's token service answered {answer.status} while renewing access")
        return self._grant(answer, refresh_token)

    def revoke(self, token: str) -> bool:
        try:
            answer = self.http.call("POST", self.endpoints.revoke, FORM_HEADERS, urlencode({"token": token}).encode())
        except SyncError as error:
            log.warning("revoking a Google sign-in failed: %s", error)
            return False
        return answer.status == 200


class GoogleSession:
    def __init__(self, oauth: OAuthClient, refresh_token: str, http: HttpClient, access: Grant | None = None):
        self.oauth = oauth
        self.refresh_token = refresh_token
        self.http = http
        self._access = access

    def _token(self, renew: bool = False) -> str:
        if renew or self._access is None or self._access.expires_at - RENEW_EARLY_SECONDS < self.oauth.clock():
            self._access = self.oauth.refresh(self.refresh_token)
        return self._access.access_token

    def call(self, method: str, url: str, body: dict | None = None, upload: Path | None = None,
             extra_headers: dict | None = None) -> Answer:
        headers = dict(extra_headers or {})
        payload = None
        if body is not None:
            headers.update(JSON_HEADERS)
            payload = json.dumps(body).encode()
        answer = self._send(method, url, headers, payload, upload, renew=False)
        if answer.status == 401:
            answer = self._send(method, url, headers, payload, upload, renew=True)
        if answer.status == 401:
            raise SignInNeeded("Google refused this sign-in; sign in again")
        return answer

    def _send(self, method, url, headers, payload, upload, renew: bool) -> Answer:
        sent = {**headers, "Authorization": f"Bearer {self._token(renew)}"}
        return self.http.call(method, url, sent, payload, upload, should_retry=_is_rate_limited)

    def download(self, url: str, target: Path, max_bytes: int) -> Answer:
        answer = self.http.download(url, {"Authorization": f"Bearer {self._token()}"}, target, max_bytes,
                                    should_retry=_is_rate_limited)
        if answer.status == 401:
            answer = self.http.download(url, {"Authorization": f"Bearer {self._token(renew=True)}"}, target,
                                        max_bytes, should_retry=_is_rate_limited)
        return answer


def _escape_query(text: str) -> str:
    return text.replace("\\", "\\\\").replace("'", "\\'")


def account_email(session: GoogleSession, endpoints: GoogleEndpoints) -> str:
    answer = session.call("GET", f"{endpoints.api}/about?fields=user(emailAddress)")
    if answer.status != 200:
        raise SyncError(f"Google answered {answer.status} when asked which account this is")
    user = _json(answer).get("user")
    email = user.get("emailAddress") if isinstance(user, dict) else None
    if not isinstance(email, str) or "@" not in email or not email.isprintable():
        raise SyncError("Google did not say which account this is")
    return email[:254]


class GoogleDriveRemote:
    label = "Google Drive"

    def __init__(self, session: GoogleSession, endpoints: GoogleEndpoints, remote_path: str):
        self.session = session
        self.endpoints = endpoints
        self.remote_path = remote_path
        self._folder_ids: dict[str, str] = {}
        self._upload_origin = urlsplit(endpoints.upload)[:2]

    def _refused(self, action: str, answer: Answer) -> RemoteRefused:
        reason = _error_reason(answer)
        return RemoteRefused(f"Google Drive answered {answer.status} to {action}{': ' + reason if reason else ''}",
                             answer.status)

    def _children(self, folder_id: str, extra_query: str = ""):
        page_token = ""
        while True:
            query = {"q": f"'{_escape_query(folder_id)}' in parents and trashed = false{extra_query}",
                     "fields": LIST_FIELDS, "pageSize": str(PAGE_SIZE), "spaces": "drive"}
            if page_token:
                query["pageToken"] = page_token
            answer = self.session.call("GET", f"{self.endpoints.api}/files?{urlencode(query)}")
            if answer.status != 200:
                raise self._refused("a folder listing", answer)
            data = _listing_page(answer)
            yield from data["files"]
            page_token = data.get("nextPageToken")
            if not isinstance(page_token, str) or not page_token:
                return

    def _create_folder(self, parent_id: str, name: str) -> dict:
        answer = self.session.call("POST", f"{self.endpoints.api}/files?fields={FILE_FIELDS}",
                                   {"name": name, "mimeType": FOLDER_TYPE, "parents": [parent_id]})
        if answer.status != 200:
            raise self._refused(f"making the folder {shown(name)}", answer)
        return _json(answer)

    def prepare(self, create_missing: bool) -> None:
        current = "root"
        for name in [part for part in self.remote_path.split("/") if part]:
            extra = f" and name = '{_escape_query(name)}' and mimeType = '{FOLDER_TYPE}'"
            matches = list(self._children(current, extra))
            if len(matches) > 1:
                raise SyncError(f"Google Drive has more than one folder called {shown(name)} there; rename one")
            if matches:
                current = matches[0]["id"]
                continue
            if not create_missing:
                raise FolderMissing(f"the Google Drive folder {shown(self.remote_path)} does not exist")
            current = self._create_folder(current, name)["id"]
        self._folder_ids = {"": current}

    def _entry(self, item: dict) -> RemoteEntry:
        size = int(item["size"]) if str(item.get("size", "")).isdigit() else 0
        kind = "folder" if item.get("mimeType") == FOLDER_TYPE else "file"
        md5 = item.get("md5Checksum") if isinstance(item.get("md5Checksum"), str) else None
        version = md5 or f"{size}:{item.get('modifiedTime')}"
        return RemoteEntry(kind=kind, ref=item["id"], version=version if kind == "file" else item["id"], size=size,
                           modified_at=_modified(item.get("modifiedTime")), id=item["id"], md5=md5)

    def list_tree(self, known: dict[str, str | None] | None = None, start: str = "") -> Listing:
        if start not in self._folder_ids:
            raise SyncError(f"the Google Drive folder {shown(start)} was not listed in this run")
        listing = Listing(known=known or {})
        folders = [start]
        while folders:
            folder = folders.pop(0)
            if folder != start and folder not in listing.entries:
                continue
            for item in sorted(self._children(self._folder_ids[folder]), key=lambda item: item["id"]):
                mime = str(item.get("mimeType") or "")
                is_folder = mime == FOLDER_TYPE
                if mime.startswith(GOOGLE_APP_PREFIX) and not is_folder:
                    listing.skip(folder, item["name"], "Google Docs, Sheets, Slides and shortcuts are not synced",
                                 item["id"])
                    continue
                path = listing.accept(folder, item["name"], "folder" if is_folder else "file", item["id"])
                if path is None:
                    continue
                listing.entries[path] = self._entry(item)
                if is_folder:
                    self._folder_ids[path] = item["id"]
                    if path not in folders:
                        folders.append(path)
        return listing

    def download(self, entry: RemoteEntry, target: Path, max_bytes: int) -> None:
        answer = self.session.download(f"{self.endpoints.api}/files/{quote(entry.ref, safe='')}?alt=media", target,
                                       max_bytes)
        if answer.status != 200:
            raise self._refused("a download", answer)

    def _parent_id(self, path: str) -> str:
        parent = parent_of(path)
        if parent not in self._folder_ids:
            raise SyncError(f"the Google Drive folder for {shown(path)} is not known yet")
        return self._folder_ids[parent]

    def _send_content(self, session_answer: Answer, source: Path, action: str) -> RemoteEntry:
        if session_answer.status != 200:
            raise self._refused(action, session_answer)
        location = session_answer.header("Location")
        if urlsplit(location)[:2] != self._upload_origin:
            raise RemoteRefused("Google gave an upload address outside its upload service, so nothing was sent")
        answer = self.session.call("PUT", location, upload=source, extra_headers={"Content-Type": UPLOAD_TYPE})
        if answer.status not in (200, 201):
            raise self._refused(action, answer)
        return self._entry(_json(answer))

    def upload_new(self, path: str, source: Path, size: int) -> RemoteEntry:
        started = self.session.call(
            "POST", f"{self.endpoints.upload}/files?uploadType=resumable&fields={FILE_FIELDS}",
            {"name": name_of(path), "parents": [self._parent_id(path)]},
            extra_headers={"X-Upload-Content-Type": UPLOAD_TYPE, "X-Upload-Content-Length": str(size)})
        return self._send_content(started, source, f"uploading {shown(path)}")

    def replace(self, entry: RemoteEntry, path: str, source: Path, size: int) -> RemoteEntry:
        started = self.session.call(
            "PATCH", f"{self.endpoints.upload}/files/{quote(entry.ref, safe='')}?uploadType=resumable"
                     f"&fields={FILE_FIELDS}", {},
            extra_headers={"X-Upload-Content-Type": UPLOAD_TYPE, "X-Upload-Content-Length": str(size)})
        return self._send_content(started, source, f"replacing {shown(path)}")

    def make_folder(self, path: str) -> RemoteEntry:
        item = self._create_folder(self._parent_id(path), name_of(path))
        self._folder_ids[path] = item["id"]
        return self._entry(item)

    def move(self, entry: RemoteEntry, old_path: str, new_path: str) -> RemoteEntry:
        query = {"fields": FILE_FIELDS}
        old_parent, new_parent = self._parent_id(old_path), self._parent_id(new_path)
        if old_parent != new_parent:
            query.update({"addParents": new_parent, "removeParents": old_parent})
        answer = self.session.call("PATCH", f"{self.endpoints.api}/files/{quote(entry.ref, safe='')}?"
                                            f"{urlencode(query)}", {"name": name_of(new_path)})
        if answer.status != 200:
            raise self._refused(f"moving {shown(old_path)}", answer)
        if entry.kind == "folder":
            self._folder_ids[new_path] = entry.ref
        return self._entry(_json(answer))

    def trash(self, entry: RemoteEntry, path: str) -> None:
        answer = self.session.call("PATCH", f"{self.endpoints.api}/files/{quote(entry.ref, safe='')}?fields=id",
                                   {"trashed": True})
        if answer.status != 200:
            raise self._refused(f"moving {shown(path)} to the Google Drive bin", answer)
