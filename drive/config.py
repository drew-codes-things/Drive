import ipaddress
import os
from dataclasses import dataclass
from pathlib import Path

from .sync.planner import DEFAULT_BIN_LIMIT_FILES, DEFAULT_BIN_LIMIT_PERCENT

AUTH_MODES = ("builtin", "proxy", "local")
SECURE_COOKIE_CHOICES = ("auto", "true", "false")
PUBLIC_SCHEMES = ("http", "https")
PUBLIC_SCHEME_FOR_MODE = {"builtin": "http", "proxy": "https", "local": "http"}
ENV_FILE = Path(".env")
LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")
DEFAULT_PORT = 8102
DEFAULT_USER_HEADER = "X-Forwarded-User"
GIB = 1024 ** 3
DEFAULT_MIN_FREE_GB = 15
DEFAULT_MAX_FILE_GB = 20
DEFAULT_ZIP_MAX_GB = 4
MIB = 1024 ** 2
DEFAULT_CONVERT_MINUTES = 15
LONGEST_CONVERT_MINUTES = 24 * 60
DEFAULT_CONVERT_MAX_INPUT_GB = 4
DEFAULT_CONVERT_MAX_OUTPUT_GB = 4
DEFAULT_CONVERT_MEMORY_MB = 3072
SMALLEST_CONVERT_MEMORY_MB = 512
LARGEST_CONVERT_MEMORY_MB = 65536
DEFAULT_WORK_MEMORY_MB = 1536
SMALLEST_WORK_MEMORY_MB = 256
GOOGLE_CLIENT_ID_SUFFIX = ".apps.googleusercontent.com"
LONGEST_GOOGLE_VALUE = 300
TOKEN_KEY_FILE_NAME = "token.key"
LARGEST_SIZE_GB = 100_000
MIN_SECRET_KEY_LENGTH = 32
LARGEST_BIN_LIMIT_FILES = 1_000_000
FULL_PERCENT = 100
TRUE_WORDS = ("1", "true", "yes", "on")
FALSE_WORDS = ("0", "false", "no", "off", "")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    bind_host: str = "127.0.0.1"
    port: int = DEFAULT_PORT
    auth_mode: str = "builtin"
    trusted_proxies: tuple[str, ...] = ()
    user_header: str = DEFAULT_USER_HEADER
    allowed_users: tuple[str, ...] = ()
    local_user: str = "local"
    secret_key: str = ""
    secure_cookies: str = "auto"
    allowed_hosts: tuple[str, ...] = ()
    shares_enabled: bool = False
    min_free_bytes: int = DEFAULT_MIN_FREE_GB * GIB
    max_file_bytes: int = DEFAULT_MAX_FILE_GB * GIB
    zip_max_bytes: int = DEFAULT_ZIP_MAX_GB * GIB
    public_scheme: str = "https"
    proxy_secret: str = ""
    convert_seconds: int = DEFAULT_CONVERT_MINUTES * 60
    convert_max_input_bytes: int = DEFAULT_CONVERT_MAX_INPUT_GB * GIB
    convert_max_output_bytes: int = DEFAULT_CONVERT_MAX_OUTPUT_GB * GIB
    convert_memory_bytes: int = DEFAULT_CONVERT_MEMORY_MB * MIB
    work_memory_bytes: int = DEFAULT_WORK_MEMORY_MB * MIB
    google_client_id: str = ""
    google_client_secret: str = ""
    token_key_file: Path | None = None
    sync_bin_limit_files: int = DEFAULT_BIN_LIMIT_FILES
    sync_bin_limit_percent: int = DEFAULT_BIN_LIMIT_PERCENT

    @property
    def google_ready(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret)

    @property
    def vault_key_path(self) -> Path:
        return self.token_key_file or self.data_dir / TOKEN_KEY_FILE_NAME

    @property
    def database_path(self) -> Path:
        return self.data_dir / "drive.db"


def _text(name: str, default: str) -> str:
    value = os.getenv(name)
    return (default if value is None else value).strip()


def _number(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = _text(name, str(default))
    try:
        value = int(raw)
    except ValueError as error:
        raise ConfigError(f"{name} must be a whole number, got {raw!r}") from error
    if not minimum <= value <= maximum:
        raise ConfigError(f"{name} must be between {minimum} and {maximum}, got {value}")
    return value


def _gigabytes(name: str, default: int, minimum: int) -> int:
    return _number(name, default, minimum, LARGEST_SIZE_GB) * GIB


def _switch(name: str, default: bool) -> bool:
    raw = _text(name, "true" if default else "false").lower()
    if raw in TRUE_WORDS:
        return True
    if raw in FALSE_WORDS:
        return False
    raise ConfigError(f"{name} must be true or false, got {raw!r}")


def _list(name: str, default: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in _text(name, default).split(",") if part.strip())


def _has_host_bits(entry: str) -> bool:
    try:
        ipaddress.ip_network(entry, strict=True)
    except ValueError:
        return True
    return False


def _check_networks(name: str, entries: tuple[str, ...]) -> None:
    if not entries:
        raise ConfigError(f"{name} needs at least one address: AUTH_MODE=proxy trusts the user header only from the proxy named there")
    for entry in entries:
        try:
            network = ipaddress.ip_network(entry, strict=False)
        except ValueError as error:
            raise ConfigError(f"{name} has {entry!r}, which is not an address or network") from error
        if _has_host_bits(entry):
            raise ConfigError(f"{name} has {entry!r}, which has host bits set; write {network} for the network "
                              f"or {entry.split('/')[0]} for one address")
        if network.num_addresses > 1 and not network.is_private:
            raise ConfigError(f"{name} has {entry!r}, which reaches public addresses; list the proxy's own address "
                              "or a private network")


def _secret_key(name: str) -> str:
    value = _text(name, "")
    if value and len(value) < MIN_SECRET_KEY_LENGTH:
        raise ConfigError(f"{name} must be at least {MIN_SECRET_KEY_LENGTH} characters, or left empty so the app "
                          "makes its own key in DATA_DIR")
    return value


def _public_scheme(auth_mode: str) -> str:
    scheme = _text("PUBLIC_SCHEME", PUBLIC_SCHEME_FOR_MODE[auth_mode]).lower()
    if scheme not in PUBLIC_SCHEMES:
        raise ConfigError(f"PUBLIC_SCHEME must be http or https, got {scheme!r}")
    return scheme


def _proxy_secret(auth_mode: str) -> str:
    value = _text("PROXY_SECRET", "")
    if not value:
        return ""
    if auth_mode != "proxy":
        raise ConfigError("PROXY_SECRET only means something with AUTH_MODE=proxy")
    if len(value) < MIN_SECRET_KEY_LENGTH or not value.isascii() or not value.isprintable():
        raise ConfigError(f"PROXY_SECRET must be at least {MIN_SECRET_KEY_LENGTH} plain ASCII characters")
    return value


def _google_value(name: str) -> str:
    value = _text(name, "")
    if len(value) > LONGEST_GOOGLE_VALUE or not value.isascii() or not value.isprintable() or " " in value:
        raise ConfigError(f"{name} must be the value from the Google Cloud console, without spaces")
    return value


def _google_client() -> tuple[str, str]:
    client_id, client_secret = _google_value("GOOGLE_CLIENT_ID"), _google_value("GOOGLE_CLIENT_SECRET")
    if client_id and not client_id.endswith(GOOGLE_CLIENT_ID_SUFFIX):
        raise ConfigError(f"GOOGLE_CLIENT_ID must end in {GOOGLE_CLIENT_ID_SUFFIX}")
    if bool(client_id) != bool(client_secret):
        raise ConfigError("set both GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET, or neither")
    return client_id, client_secret


def _optional_path(name: str) -> Path | None:
    value = _text(name, "")
    return Path(value).resolve() if value else None


def read_env_file(path: Path = ENV_FILE) -> None:
    if not path.is_file():
        return
    for number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, value = line.partition("=")
        if not separator or not name.strip():
            raise ConfigError(f"{path}:{number} should look like NAME=value")
        os.environ.setdefault(name.strip(), value.strip().strip('"').strip("'"))


def load_settings() -> Settings:
    read_env_file()
    auth_mode = _text("AUTH_MODE", "builtin").lower()
    if auth_mode not in AUTH_MODES:
        raise ConfigError(f"AUTH_MODE must be one of {', '.join(AUTH_MODES)}, got {auth_mode!r}")
    bind_host = _text("BIND_HOST", "127.0.0.1")
    if auth_mode == "local" and bind_host not in LOOPBACK_HOSTS:
        raise ConfigError("AUTH_MODE=local has no sign-in, so BIND_HOST must be 127.0.0.1, ::1 or localhost")
    trusted_proxies = _list("TRUSTED_PROXIES", "")
    if auth_mode == "proxy" or trusted_proxies:
        _check_networks("TRUSTED_PROXIES", trusted_proxies)
    secure_cookies = _text("SECURE_COOKIES", "auto").lower()
    if secure_cookies not in SECURE_COOKIE_CHOICES:
        raise ConfigError(f"SECURE_COOKIES must be one of {', '.join(SECURE_COOKIE_CHOICES)}, got {secure_cookies!r}")
    user_header = _text("USER_HEADER", DEFAULT_USER_HEADER)
    if not user_header:
        raise ConfigError("USER_HEADER cannot be empty")
    google_client = _google_client()
    return Settings(
        data_dir=Path(_text("DATA_DIR", "./data")).resolve(),
        bind_host=bind_host,
        port=_number("PORT", DEFAULT_PORT, 1, 65535),
        auth_mode=auth_mode,
        trusted_proxies=trusted_proxies,
        user_header=user_header,
        allowed_users=_list("ALLOWED_USERS", ""),
        local_user=_text("LOCAL_USER", "local") or "local",
        secret_key=_secret_key("SECRET_KEY"),
        secure_cookies=secure_cookies,
        allowed_hosts=tuple(host.lower() for host in _list("ALLOWED_HOSTS", "")),
        shares_enabled=_switch("SHARES_ENABLED", False),
        min_free_bytes=_gigabytes("MIN_FREE_GB", DEFAULT_MIN_FREE_GB, 0),
        max_file_bytes=_gigabytes("MAX_FILE_GB", DEFAULT_MAX_FILE_GB, 1),
        zip_max_bytes=_gigabytes("ZIP_MAX_GB", DEFAULT_ZIP_MAX_GB, 1),
        public_scheme=_public_scheme(auth_mode),
        proxy_secret=_proxy_secret(auth_mode),
        convert_seconds=_number("CONVERT_TIMEOUT_MINUTES", DEFAULT_CONVERT_MINUTES, 1, LONGEST_CONVERT_MINUTES) * 60,
        convert_max_input_bytes=_gigabytes("CONVERT_MAX_INPUT_GB", DEFAULT_CONVERT_MAX_INPUT_GB, 1),
        convert_max_output_bytes=_gigabytes("CONVERT_MAX_OUTPUT_GB", DEFAULT_CONVERT_MAX_OUTPUT_GB, 1),
        convert_memory_bytes=_number("CONVERT_MEMORY_MB", DEFAULT_CONVERT_MEMORY_MB, SMALLEST_CONVERT_MEMORY_MB,
                                     LARGEST_CONVERT_MEMORY_MB) * MIB,
        work_memory_bytes=_number("WORK_MEMORY_MB", DEFAULT_WORK_MEMORY_MB, SMALLEST_WORK_MEMORY_MB,
                                  LARGEST_CONVERT_MEMORY_MB) * MIB,
        google_client_id=google_client[0],
        google_client_secret=google_client[1],
        token_key_file=_optional_path("TOKEN_KEY_FILE"),
        sync_bin_limit_files=_number("SYNC_BIN_LIMIT_FILES", DEFAULT_BIN_LIMIT_FILES, 1, LARGEST_BIN_LIMIT_FILES),
        sync_bin_limit_percent=_number("SYNC_BIN_LIMIT_PERCENT", DEFAULT_BIN_LIMIT_PERCENT, 1, FULL_PERCENT),
    )
