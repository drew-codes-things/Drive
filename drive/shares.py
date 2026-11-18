import hashlib
import hmac
import re
import secrets
import threading
import time
from collections import deque

from .db import Database
from .errors import DriveError, NotFound
from .store import LARGEST_DATABASE_INTEGER, DriveStore

TOKEN_BYTES = 32
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}")
UNLOCK_UNTIL = re.compile(r"[0-9]{1,12}")
MAX_COOKIE_LENGTH = 100
MIN_PASSWORD_LENGTH = 6
MAX_PASSWORD_LENGTH = 200
SCRYPT_COST = 2 ** 14
SCRYPT_BLOCK = 8
SCRYPT_PARALLEL = 1
SCRYPT_BYTES = 32
SALT_BYTES = 16
LONGEST_EXPIRY_SECONDS = 10 * 365 * 86400
UNLOCK_SECONDS = 12 * 3600
UNLOCK_PURPOSE = b"drive share unlock\x00"
FAILED_ATTEMPT_LIMIT = 10
FAILED_ATTEMPT_WINDOW_SECONDS = 15 * 60
GONE = 410
TOO_MANY_REQUESTS = 429
MISSING_SHARE_TEXT = "this link does not work: it may have been switched off or mistyped"


class ShareExpired(DriveError):
    def __init__(self):
        super().__init__("this link has expired", GONE)


def is_token(value) -> bool:
    return isinstance(value, str) and TOKEN_PATTERN.fullmatch(value) is not None


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(SALT_BYTES)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=SCRYPT_COST, r=SCRYPT_BLOCK, p=SCRYPT_PARALLEL,
                            dklen=SCRYPT_BYTES)
    return f"scrypt${salt.hex()}${digest.hex()}"


def password_matches(stored: str, password: str) -> bool:
    try:
        _, salt_hex, digest_hex = stored.split("$")
        salt = bytes.fromhex(salt_hex)
    except ValueError:
        return False
    return hmac.compare_digest(hash_password(password, salt).split("$")[2], digest_hex)


def _password(raw) -> str | None:
    if raw is None or raw == "":
        return None
    if not isinstance(raw, str) or not MIN_PASSWORD_LENGTH <= len(raw) <= MAX_PASSWORD_LENGTH:
        raise DriveError(f"a link password must be {MIN_PASSWORD_LENGTH} to {MAX_PASSWORD_LENGTH} characters")
    return raw


def _share_view(row, now: float) -> dict:
    return {
        "id": row["id"],
        "token": row["token"],
        "nodeId": row["node_id"],
        "createdAt": row["created_at"],
        "expiresAt": row["expires_at"],
        "expired": row["expires_at"] is not None and row["expires_at"] <= now,
        "hasPassword": row["password_hash"] is not None,
    }


class AttemptLimiter:
    def __init__(self, clock):
        self.clock = clock
        self._failures: dict[int, deque] = {}
        self._lock = threading.Lock()

    def _recent(self, share_id: int) -> deque:
        failures = self._failures.setdefault(share_id, deque())
        cutoff = self.clock() - FAILED_ATTEMPT_WINDOW_SECONDS
        while failures and failures[0] < cutoff:
            failures.popleft()
        return failures

    def reserve(self, share_id: int) -> None:
        with self._lock:
            failures = self._recent(share_id)
            if len(failures) >= FAILED_ATTEMPT_LIMIT:
                wait = int(failures[0] + FAILED_ATTEMPT_WINDOW_SECONDS - self.clock()) // 60 + 1
                raise DriveError(f"too many wrong passwords; try again in {wait} minutes", TOO_MANY_REQUESTS)
            failures.append(self.clock())

    def clear(self, share_id: int) -> None:
        with self._lock:
            self._failures.pop(share_id, None)


class ShareStore:
    def __init__(self, database: Database, store: DriveStore, secret: bytes, clock=time.time):
        self.database = database
        self.store = store
        self.secret = secret
        self.clock = clock
        self.limiter = AttemptLimiter(clock)

    def _expiry(self, raw):
        if raw is None:
            return None
        now = self.clock()
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not now < raw <= now + LONGEST_EXPIRY_SECONDS:
            raise DriveError("the expiry must be a time in the future, at most 10 years ahead (seconds since 1970)")
        return float(raw)

    def create(self, body: dict) -> dict:
        node_id = body.get("nodeId")
        if isinstance(node_id, bool) or not isinstance(node_id, int) or not 0 < node_id <= LARGEST_DATABASE_INTEGER:
            raise DriveError("nodeId must be the id of a file or folder")
        expires_at = self._expiry(body.get("expiresAt"))
        password = _password(body.get("password"))
        self.store.get(node_id)
        token = secrets.token_urlsafe(TOKEN_BYTES)
        password_hash = hash_password(password) if password else None
        with self.database.connect() as connection:
            cursor = connection.execute(
                "INSERT INTO shares (token, node_id, created_at, expires_at, password_hash) VALUES (?, ?, ?, ?, ?)",
                (token, node_id, self.clock(), expires_at, password_hash))
            row = connection.execute("SELECT * FROM shares WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return _share_view(row, self.clock())

    def list(self, node_id: int | None = None) -> list[dict]:
        query = ("SELECT shares.*, nodes.name AS node_name, nodes.kind AS node_kind, nodes.in_bin AS node_in_bin"
                 " FROM shares JOIN nodes ON nodes.id = shares.node_id WHERE revoked_at IS NULL")
        parameters: tuple = ()
        if node_id is not None:
            query += " AND node_id = ?"
            parameters = (node_id,)
        with self.database.connect() as connection:
            rows = connection.execute(query + " ORDER BY shares.id DESC", parameters).fetchall()
        now = self.clock()
        return [{**_share_view(row, now), "nodeName": row["node_name"], "nodeKind": row["node_kind"],
                 "inBin": bool(row["node_in_bin"])} for row in rows]

    def revoke(self, share_id: int) -> None:
        with self.database.connect() as connection:
            cursor = connection.execute("UPDATE shares SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                                        (self.clock(), share_id))
        if cursor.rowcount == 0:
            raise NotFound("that link is not here any more")
        self.limiter.clear(share_id)

    def resolve(self, token) -> dict:
        if not is_token(token):
            raise NotFound(MISSING_SHARE_TEXT)
        with self.database.connect() as connection:
            row = connection.execute("SELECT * FROM shares WHERE token = ?", (token,)).fetchone()
        if row is None or row["revoked_at"] is not None or not hmac.compare_digest(row["token"], token):
            raise NotFound(MISSING_SHARE_TEXT)
        try:
            node = self.store.get(row["node_id"])
        except NotFound as error:
            raise NotFound(MISSING_SHARE_TEXT) from error
        if row["expires_at"] is not None and row["expires_at"] <= self.clock():
            raise ShareExpired()
        return {"id": row["id"], "token": row["token"], "node": node, "expiresAt": row["expires_at"],
                "passwordHash": row["password_hash"]}

    def _unlock_signature(self, share: dict, until: int) -> str:
        message = UNLOCK_PURPOSE + f"{share['id']}\x00{share['token']}\x00{until}\x00{share['passwordHash']}".encode()
        return hmac.new(self.secret, message, hashlib.sha256).hexdigest()

    def unlock(self, share: dict, password) -> str:
        if share["passwordHash"] is None:
            raise DriveError("this link has no password")
        self.limiter.reserve(share["id"])
        if not isinstance(password, str) or not password or len(password) > MAX_PASSWORD_LENGTH \
                or not password_matches(share["passwordHash"], password):
            raise DriveError("that password is not right", 403)
        self.limiter.clear(share["id"])
        until = int(self.clock()) + UNLOCK_SECONDS
        return f"{until}.{self._unlock_signature(share, until)}"

    def is_unlocked(self, share: dict, cookie) -> bool:
        if share["passwordHash"] is None:
            return True
        if not isinstance(cookie, str) or not cookie.isascii() or len(cookie) > MAX_COOKIE_LENGTH or "." not in cookie:
            return False
        until_text, signature = cookie.split(".", 1)
        if not UNLOCK_UNTIL.fullmatch(until_text) or int(until_text) <= self.clock():
            return False
        return hmac.compare_digest(self._unlock_signature(share, int(until_text)), signature)

    def contains(self, share: dict, node_id: int) -> bool:
        return self.store.is_within(node_id, share["node"]["id"])
