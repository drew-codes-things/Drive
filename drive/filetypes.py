from .imagesize import image_size
from .names import split_extension

OCTET_STREAM = "application/octet-stream"
TEXT = "text/plain"
PDF = "application/pdf"
HEAD_BYTES = 256 * 1024
TEXT_SNIFF_BYTES = 8192
MAX_PREVIEW_PIXELS = 100_000_000

IMAGE_TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
    ".webp": "image/webp", ".avif": "image/avif", ".bmp": "image/bmp",
}
AUDIO_TYPES = {
    ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".aac": "audio/aac", ".ogg": "audio/ogg", ".oga": "audio/ogg",
    ".opus": "audio/ogg", ".wav": "audio/wav", ".flac": "audio/flac", ".weba": "audio/webm",
}
VIDEO_TYPES = {
    ".mp4": "video/mp4", ".m4v": "video/mp4", ".webm": "video/webm", ".ogv": "video/ogg", ".mov": "video/quicktime",
}
TEXT_EXTENSIONS = frozenset("""
.txt .md .markdown .csv .tsv .json .jsonl .yaml .yml .toml .ini .cfg .conf .log .env .properties .gradle
.py .pyi .js .mjs .cjs .ts .tsx .jsx .vue .svelte .css .scss .less .html .htm .xml .svg .sh .bash .zsh .fish
.c .h .cpp .hpp .cc .cs .java .kt .kts .go .rs .rb .php .sql .swift .lua .pl .r .dart .scala .ps1 .bat .cmd
.srt .vtt .tex .rst .adoc .diff .patch .gitignore .dockerignore .editorconfig .lock
""".split())
TEXT_FILE_NAMES = frozenset({"dockerfile", "makefile", "readme", "license", "caddyfile", "procfile"})
INLINE_TYPES = frozenset(IMAGE_TYPES.values()) | frozenset(AUDIO_TYPES.values()) | \
    frozenset(VIDEO_TYPES.values()) | {PDF}


def _looks_like_text(head: bytes) -> bool:
    sample = head[:TEXT_SNIFF_BYTES]
    if b"\x00" in sample:
        return False
    try:
        sample.decode("utf-8")
    except UnicodeDecodeError as error:
        return error.start >= len(sample) - 3
    return True


def _image_type(mime: str, head: bytes) -> str:
    size = image_size(mime, head)
    if size is None or size[0] * size[1] > MAX_PREVIEW_PIXELS:
        return OCTET_STREAM
    return mime


def detect_type(name: str, head: bytes) -> str:
    extension = split_extension(name)[1].lower()
    if extension in IMAGE_TYPES:
        return _image_type(IMAGE_TYPES[extension], head)
    if extension == ".pdf":
        return PDF if head.startswith(b"%PDF-") else OCTET_STREAM
    if extension in AUDIO_TYPES:
        return AUDIO_TYPES[extension]
    if extension in VIDEO_TYPES:
        return VIDEO_TYPES[extension]
    is_text_name = extension in TEXT_EXTENSIONS or name.lower() in TEXT_FILE_NAMES or not extension
    if is_text_name and _looks_like_text(head):
        return TEXT
    return OCTET_STREAM


def preview_kind(mime: str) -> str:
    if mime == PDF:
        return "pdf"
    if mime == TEXT:
        return "text"
    family = mime.split("/", 1)[0]
    if mime in INLINE_TYPES and family in ("image", "audio", "video"):
        return family
    return "none"
