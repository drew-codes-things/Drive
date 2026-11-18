import base64
import binascii
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

KEY_BYTES = 32
KEY_FILE_MODE = 0o600


class VaultKeyError(ValueError):
    pass


class SecretUnreadable(ValueError):
    pass


def _is_fernet_key(raw: bytes) -> bool:
    try:
        return len(base64.urlsafe_b64decode(raw)) == KEY_BYTES
    except (binascii.Error, ValueError):
        return False


def _write_new_key(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, KEY_FILE_MODE)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(Fernet.generate_key())


def load_or_create_vault_key(path: Path) -> bytes:
    path = Path(path)
    if not path.exists():
        _write_new_key(path)
    try:
        raw = path.read_bytes().strip()
    except OSError as error:
        raise VaultKeyError(f"the token key {path} cannot be read: {error.strerror}") from error
    if not _is_fernet_key(raw):
        raise VaultKeyError(f"{path} is not a token key (44 characters of URL-safe base64); delete it to have a new "
                            "one made, which means signing in to every cloud connection again")
    if path.stat().st_mode & 0o077:
        os.chmod(path, KEY_FILE_MODE)
    return raw


class Vault:
    def __init__(self, key: bytes):
        self._fernet = Fernet(key)

    def seal(self, secret: str) -> bytes:
        return self._fernet.encrypt(secret.encode())

    def open(self, sealed) -> str:
        try:
            return self._fernet.decrypt(bytes(sealed)).decode()
        except (InvalidToken, TypeError) as error:
            raise SecretUnreadable("a saved password or sign-in cannot be decrypted with the current token key; "
                                   "enter it again") from error
