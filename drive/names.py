import unicodedata

from .errors import DriveError

MAX_NAME_BYTES = 255
FORBIDDEN_CHARACTERS = frozenset("/\\")
FORBIDDEN_CATEGORIES = frozenset({"Cc", "Cs"})
BIDI_CONTROLS = frozenset("\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")
RESERVED_NAMES = frozenset({".", ".."})


def _is_forbidden(character: str) -> bool:
    return (character in FORBIDDEN_CHARACTERS or character in BIDI_CONTROLS
            or unicodedata.category(character) in FORBIDDEN_CATEGORIES)


def clean_name(raw) -> str:
    if not isinstance(raw, str):
        raise DriveError("a name must be text")
    name = unicodedata.normalize("NFC", raw).strip()
    if not name:
        raise DriveError("a name cannot be empty")
    if name in RESERVED_NAMES:
        raise DriveError(f"{name!r} cannot be used as a name")
    if any(_is_forbidden(character) for character in name):
        raise DriveError("a name cannot hold slashes, backslashes, control characters or text direction overrides")
    if len(name.encode("utf-8")) > MAX_NAME_BYTES:
        raise DriveError(f"a name can be at most {MAX_NAME_BYTES} bytes long")
    return name


def name_key(name: str) -> str:
    return name.casefold()


def split_extension(name: str) -> tuple[str, str]:
    dot = name.rfind(".")
    if dot <= 0:
        return name, ""
    return name[:dot], name[dot:]


def numbered_name(name: str, number: int, is_folder: bool) -> str:
    stem, extension = (name, "") if is_folder else split_extension(name)
    suffix = f" ({number})"
    while len((stem + suffix + extension).encode("utf-8")) > MAX_NAME_BYTES and stem:
        stem = stem[:-1]
    return f"{stem}{suffix}{extension}"
