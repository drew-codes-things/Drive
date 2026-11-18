import base64
import ipaddress
import secrets
import time
import xml.etree.ElementTree as ElementTree
import xml.parsers.expat
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from ..errors import DriveError
from ..names import clean_name
from .http import Answer, HttpClient
from .remote import (SYNC_BIN_NAME, FolderMissing, Listing, RemoteEntry, RemoteRefused, SignInNeeded,
                     join, name_of, parent_of, shown)

DAV = "{DAV:}"
OWNCLOUD = "{http://owncloud.org/ns}"
MULTI_STATUS = 207
PROPFIND_BODY = (b'<?xml version="1.0" encoding="utf-8"?><d:propfind xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">'
                 b'<d:prop><d:resourcetype/><d:getcontentlength/><d:getetag/><d:getlastmodified/><oc:fileid/>'
                 b'</d:prop></d:propfind>')
XML_HEADERS = {"Content-Type": "application/xml; charset=utf-8"}
NEXTCLOUD_DAV_PATH = "/remote.php/dav/files/{user}/"
LOOPBACK_NAMES = ("localhost",)
MOVED = (201, 204)
STORED = (200, 201, 204)
BIN_STAMP = "%Y-%m-%d %H%M%S"
STAGING_NAME = "uploading"
STAGED_ID_BYTES = 8
LONGEST_URL = 2000
LONGEST_USERNAME = 200
LONGEST_PASSWORD = 500
FOLDER_NAME_REWRITTEN = ("the folder name has spaces at the start or end, or characters Drive writes another way, so "
                         "its files cannot be reached by that name; rename it in the cloud to sync it")


def _is_renamed_by_drive(raw_name: str) -> bool:
    try:
        return clean_name(raw_name) != raw_name
    except DriveError:
        return False


def _is_private_host(host: str) -> bool:
    if host in LOOPBACK_NAMES:
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_private or address.is_loopback


def clean_server_url(raw) -> str:
    if not isinstance(raw, str) or not raw.strip() or len(raw) > LONGEST_URL:
        raise DriveError("enter the server address, starting with https://")
    url = raw.strip()
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise DriveError("the server address must start with https:// (or http:// for a home network address)")
    if parts.username or parts.password:
        raise DriveError("put the user name and app password in their own boxes, not in the address")
    if parts.query or parts.fragment:
        raise DriveError("the server address cannot hold ? or #")
    if parts.scheme == "http" and not _is_private_host(parts.hostname):
        raise DriveError("plain http:// is only allowed for home network addresses such as 10.0.0.10; "
                         "use https:// so the app password is not sent in the clear")
    if not url.endswith("/"):
        url += "/"
    return url


def clean_username(raw) -> str:
    if not isinstance(raw, str) or not raw.strip() or len(raw) > LONGEST_USERNAME or not raw.isprintable():
        raise DriveError("enter the user name")
    return raw.strip()


def clean_password(raw) -> str:
    if not isinstance(raw, str) or not raw or len(raw) > LONGEST_PASSWORD or not raw.isprintable():
        raise DriveError("enter the app password")
    return raw


def nextcloud_dav_url(server_url: str, username: str) -> str:
    base = server_url.rstrip("/")
    return base + NEXTCLOUD_DAV_PATH.format(user=quote(username, safe=""))


def _quoted(path: str) -> str:
    return "/".join(quote(part, safe="") for part in path.split("/")) if path else ""


def _text(element, tag: str) -> str:
    found = element.find(tag)
    return (found.text or "").strip() if found is not None else ""


def _modified(raw: str) -> float | None:
    try:
        return parsedate_to_datetime(raw).timestamp() if raw else None
    except (TypeError, ValueError):
        return None


class _DeclaresEntities(Exception):
    pass


def _refuse_declarations(*_):
    raise _DeclaresEntities()


def _check_no_declarations(body: bytes) -> None:
    checker = xml.parsers.expat.ParserCreate()
    checker.StartDoctypeDeclHandler = _refuse_declarations
    checker.EntityDeclHandler = _refuse_declarations
    try:
        checker.Parse(body, True)
    except _DeclaresEntities as error:
        raise RemoteRefused("the server's folder listing declared XML entities, so it was not read") from error
    except (xml.parsers.expat.ExpatError, ValueError, LookupError) as error:
        raise RemoteRefused(f"the server's folder listing could not be read: {error}") from error


def parse_multistatus(body: bytes) -> list[dict]:
    _check_no_declarations(body)
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError as error:
        raise RemoteRefused(f"the server's folder listing could not be read: {error}") from error
    items = []
    for response in root.iter(f"{DAV}response"):
        href = _text(response, f"{DAV}href")
        for propstat in response.findall(f"{DAV}propstat"):
            if " 200 " not in f" {_text(propstat, f'{DAV}status')} ":
                continue
            prop = propstat.find(f"{DAV}prop")
            if prop is None:
                continue
            kind_holder = prop.find(f"{DAV}resourcetype")
            is_folder = kind_holder is not None and kind_holder.find(f"{DAV}collection") is not None
            length = _text(prop, f"{DAV}getcontentlength")
            listed_size = int(length) if length.isdigit() else None
            items.append({"href": href, "folder": is_folder, "size": listed_size or 0, "listed_size": listed_size,
                          "etag": _text(prop, f"{DAV}getetag").strip('"'),
                          "modified": _modified(_text(prop, f"{DAV}getlastmodified")),
                          "fileid": _text(prop, f"{OWNCLOUD}fileid") or None})
    return items


class WebDavRemote:
    def __init__(self, base_url: str, username: str, password: str, remote_path: str, http: HttpClient,
                 label: str = "WebDAV", clock=time.time):
        self.label = label
        self.http = http
        self.clock = clock
        self.base_url = base_url.rstrip("/") + "/"
        self.remote_path = remote_path
        self.root_url = self.base_url + (_quoted(remote_path) + "/" if remote_path else "")
        self._root_parts = urlsplit(self.root_url)
        token = base64.b64encode(f"{username}:{password}".encode()).decode()
        self._auth = {"Authorization": f"Basic {token}"}
        self._bin_folder: str | None = None
        self._staging_ready = False

    def _url(self, path: str, folder: bool = False) -> str:
        return self.root_url + _quoted(path) + ("/" if folder and path else "")

    def _send(self, method: str, url: str, headers: dict | None = None, body: bytes | None = None,
              upload: Path | None = None) -> Answer:
        answer = self.http.call(method, url, {**self._auth, **(headers or {})}, body, upload)
        if answer.status == 401:
            raise SignInNeeded(f"{self.label} refused the user name or app password")
        return answer

    def _propfind(self, url: str, depth: str) -> Answer:
        return self._send("PROPFIND", url, {**XML_HEADERS, "Depth": depth}, PROPFIND_BODY)

    def _refused(self, action: str, answer: Answer) -> RemoteRefused:
        return RemoteRefused(f"{self.label} answered {answer.status} to {action}", answer.status)

    def prepare(self, create_missing: bool) -> None:
        answer = self._propfind(self.root_url, "0")
        if answer.status == MULTI_STATUS:
            return
        if answer.status != 404:
            raise self._refused("opening the cloud folder", answer)
        if not create_missing:
            raise FolderMissing(f"the cloud folder {unquote(self._root_parts.path)} does not exist on {self.label}")
        self._make_remote_path()

    def _make_remote_path(self) -> None:
        url = self.base_url
        for part in [part for part in self.remote_path.split("/") if part]:
            url += quote(part, safe="") + "/"
            self._make_collection(url)

    def _make_collection(self, url: str) -> None:
        answer = self._send("MKCOL", url)
        if answer.status in (201, 405):
            return
        raise self._refused("making a folder", answer)

    def _child_name(self, folder_path: str, href: str) -> str | None:
        parts = urlsplit(href)
        if parts.netloc and parts.netloc != self._root_parts.netloc:
            return None
        decoded = unquote(parts.path).rstrip("/")
        prefix = unquote(self._root_parts.path).rstrip("/") + ("/" + folder_path if folder_path else "") + "/"
        if not decoded.startswith(prefix):
            return None
        rest = decoded[len(prefix):]
        return rest if rest and "/" not in rest else None

    def list_tree(self, known: dict[str, str | None] | None = None, start: str = "") -> Listing:
        listing = Listing(known=known or {})
        folders = [start]
        while folders:
            folder = folders.pop(0)
            if folder != start and folder not in listing.entries:
                continue
            answer = self._propfind(self._url(folder, folder=True), "1")
            if answer.status != MULTI_STATUS:
                raise self._refused(f"listing {shown(folder or '/')}", answer)
            items = parse_multistatus(answer.body)
            own_path = self._folder_path(folder)
            if not any(unquote(urlsplit(item["href"]).path).rstrip("/") == own_path for item in items):
                raise RemoteRefused(f"{self.label} sent a listing of {shown(folder or '/')} without the folder itself, "
                                    "so it was not trusted")
            for item in items:
                if unquote(urlsplit(item["href"]).path).rstrip("/") == own_path:
                    continue
                raw_name = self._child_name(folder, item["href"])
                if raw_name is None:
                    listing.skip(folder, unquote(item["href"])[-60:], "the server listed it outside this folder",
                                 item["fileid"], "folder" if item["folder"] else "file", item["listed_size"])
                    listing.distrust(folder)
                    continue
                kind = "folder" if item["folder"] else "file"
                if kind == "folder" and _is_renamed_by_drive(raw_name):
                    listing.doubt(folder, clean_name(raw_name), FOLDER_NAME_REWRITTEN, item["fileid"], kind)
                    continue
                path = listing.accept(folder, raw_name, kind, item["fileid"], item["listed_size"])
                if path is None:
                    continue
                version = item["etag"] or f"{item['size']}:{item['modified']}"
                listing.entries[path] = RemoteEntry(kind=kind, ref=self._absolute(item["href"]), version=version,
                                                    size=item["size"], modified_at=item["modified"],
                                                    id=item["fileid"])
                if kind == "folder":
                    folders.append(path)
        return listing

    def _folder_path(self, folder: str) -> str:
        return unquote(self._url(folder, folder=True)[len(self._origin()):]).rstrip("/")

    def _origin(self) -> str:
        return f"{self._root_parts.scheme}://{self._root_parts.netloc}"

    def _absolute(self, href: str) -> str:
        return self._origin() + urlsplit(href).path

    def download(self, entry: RemoteEntry, target: Path, max_bytes: int) -> None:
        answer = self.http.download(entry.ref, self._auth, target, max_bytes)
        if answer.status == 401:
            raise SignInNeeded(f"{self.label} refused the user name or app password")
        if answer.status != 200:
            raise self._refused("a download", answer)

    def _stat(self, url: str, kind: str) -> RemoteEntry:
        answer = self._propfind(url, "0")
        if answer.status != MULTI_STATUS:
            raise self._refused("checking what was just written", answer)
        items = parse_multistatus(answer.body)
        if not items:
            raise RemoteRefused(f"{self.label} did not describe what was just written")
        item = items[0]
        version = item["etag"] or f"{item['size']}:{item['modified']}"
        return RemoteEntry(kind=kind, ref=url, version=version, size=item["size"], modified_at=item["modified"],
                           id=item["fileid"])

    def _stage(self, source: Path) -> str:
        if not self._staging_ready:
            self._make_collection(self._url(SYNC_BIN_NAME, folder=True))
            self._make_collection(self._url(join(SYNC_BIN_NAME, STAGING_NAME), folder=True))
            self._staging_ready = True
        staged = self._url(join(join(SYNC_BIN_NAME, STAGING_NAME), secrets.token_hex(STAGED_ID_BYTES)))
        answer = self._send("PUT", staged, {"Content-Type": "application/octet-stream"}, upload=source)
        if answer.status not in STORED:
            raise self._refused("an upload", answer)
        return staged

    def _place(self, staged: str, path: str) -> RemoteEntry:
        url = self._url(path)
        answer = self._send("MOVE", staged, {"Destination": url, "Overwrite": "F"})
        if answer.status not in MOVED:
            raise self._refused(f"putting {shown(path)} in place", answer)
        return self._stat(url, "file")

    def upload_new(self, path: str, source: Path, size: int) -> RemoteEntry:
        return self._place(self._stage(source), path)

    def replace(self, entry: RemoteEntry, path: str, source: Path, size: int) -> RemoteEntry:
        staged = self._stage(source)
        self.trash(entry, path)
        return self._place(staged, path)

    def make_folder(self, path: str) -> RemoteEntry:
        url = self._url(path, folder=True)
        self._make_collection(url)
        return self._stat(url, "folder")

    def move(self, entry: RemoteEntry, old_path: str, new_path: str) -> RemoteEntry:
        is_folder = entry.kind == "folder"
        destination = self._url(new_path, folder=is_folder)
        answer = self._send("MOVE", entry.ref, {"Destination": destination, "Overwrite": "F"})
        if answer.status not in MOVED:
            raise self._refused(f"moving {shown(old_path)}", answer)
        return self._stat(destination, entry.kind)

    def _bin(self) -> str:
        if self._bin_folder is None:
            stamp = time.strftime(BIN_STAMP, time.localtime(self.clock()))
            self._make_collection(self._url(SYNC_BIN_NAME, folder=True))
            self._make_collection(self._url(join(SYNC_BIN_NAME, stamp), folder=True))
            self._bin_folder = join(SYNC_BIN_NAME, stamp)
        return self._bin_folder

    def trash(self, entry: RemoteEntry, path: str) -> None:
        bin_folder = self._bin()
        parent = parent_of(path)
        made = bin_folder
        for part in parent.split("/") if parent else []:
            made = join(made, part)
            self._make_collection(self._url(made, folder=True))
        destination = self._url(join(join(bin_folder, parent) if parent else bin_folder, name_of(path)),
                                folder=entry.kind == "folder")
        answer = self._send("MOVE", entry.ref, {"Destination": destination, "Overwrite": "F"})
        if answer.status not in MOVED:
            raise self._refused(f"moving {shown(path)} to {SYNC_BIN_NAME}", answer)
