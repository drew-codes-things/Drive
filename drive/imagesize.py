import struct

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
JPEG_START = b"\xff\xd8"
JPEG_FRAME_MARKERS = frozenset({0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF})
JPEG_MARKERS_WITHOUT_LENGTH = frozenset({0x01, 0xD0, 0xD1, 0xD2, 0xD3, 0xD4, 0xD5, 0xD6, 0xD7, 0xD8})
AVIF_SIZE_BOX = b"ispe"
AVIF_BRANDS = frozenset({b"avif", b"avis"})
HEIF_BRANDS = frozenset({b"heic", b"heix", b"heim", b"heis", b"hevc", b"hevx", b"mif1", b"msf1"})
HEIF_TYPE = "image/heif"
VP8L_SIGNATURE = 0x2F
GIF_SIZE_BYTES = 10
GIF_HEADER_BYTES = 13
GIF_TABLE_FLAG = 0x80
GIF_TABLE_SIZE_BITS = 0x07
GIF_EXTENSION = 0x21
GIF_IMAGE = 0x2C
GIF_TRAILER = 0x3B
GIF_DESCRIPTOR_BYTES = 10
WEBP_FIRST_CHUNK = 12
WEBP_CHUNK_HEADER_BYTES = 8
WEBP_FRAME_HEADER_BYTES = 16


def _png(head: bytes):
    if head[:8] != PNG_SIGNATURE or head[12:16] != b"IHDR" or len(head) < 24:
        return None
    return struct.unpack(">II", head[16:24])


def _color_table_bytes(flags: int) -> int:
    if not flags & GIF_TABLE_FLAG:
        return 0
    return 3 * 2 ** ((flags & GIF_TABLE_SIZE_BITS) + 1)


def _skip_gif_sub_blocks(head: bytes, position: int) -> int:
    while position < len(head) and head[position]:
        position += head[position] + 1
    return position + 1


def _gif(head: bytes):
    if head[:6] not in (b"GIF87a", b"GIF89a") or len(head) < GIF_SIZE_BYTES:
        return None
    width, height = struct.unpack("<HH", head[6:10])
    if len(head) < GIF_HEADER_BYTES:
        return width, height
    position = GIF_HEADER_BYTES + _color_table_bytes(head[10])
    while position < len(head) and head[position] != GIF_TRAILER:
        if head[position] == GIF_EXTENSION:
            position = _skip_gif_sub_blocks(head, position + 2)
            continue
        if head[position] != GIF_IMAGE or position + GIF_DESCRIPTOR_BYTES > len(head):
            break
        left, top, frame_width, frame_height = struct.unpack("<HHHH", head[position + 1:position + 9])
        width, height = max(width, left + frame_width), max(height, top + frame_height)
        position += GIF_DESCRIPTOR_BYTES + _color_table_bytes(head[position + 9])
        position = _skip_gif_sub_blocks(head, position + 1)
    return width, height


def _bmp(head: bytes):
    if head[:2] != b"BM" or len(head) < 26:
        return None
    width, height = struct.unpack("<ii", head[18:26])
    return abs(width), abs(height)


def _webp_chunk_size(kind: bytes, head: bytes, data: int):
    if kind == b"VP8X":
        width = int.from_bytes(head[data + 4:data + 7], "little") + 1
        return width, int.from_bytes(head[data + 7:data + 10], "little") + 1
    if kind == b"VP8L" and head[data] == VP8L_SIGNATURE:
        bits = int.from_bytes(head[data + 1:data + 5], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if kind == b"VP8 ":
        width, height = struct.unpack("<HH", head[data + 6:data + 10])
        return width & 0x3FFF, height & 0x3FFF
    if kind == b"ANMF":
        left = int.from_bytes(head[data:data + 3], "little") * 2
        top = int.from_bytes(head[data + 3:data + 6], "little") * 2
        width = int.from_bytes(head[data + 6:data + 9], "little") + 1
        height = int.from_bytes(head[data + 9:data + 12], "little") + 1
        return left + width, top + height
    return None


def _webp_sizes(head: bytes, position: int, end: int) -> list:
    sizes = []
    while position + WEBP_CHUNK_HEADER_BYTES <= min(end, len(head)):
        kind = head[position:position + 4]
        length = int.from_bytes(head[position + 4:position + 8], "little")
        data = position + WEBP_CHUNK_HEADER_BYTES
        try:
            size = _webp_chunk_size(kind, head, data)
        except (struct.error, IndexError):
            size = None
        if size is not None:
            sizes.append(size)
        if kind == b"ANMF":
            sizes.extend(_webp_sizes(head, data + WEBP_FRAME_HEADER_BYTES, data + length))
        position = data + length + (length & 1)
    return sizes


def _webp(head: bytes):
    if head[:4] != b"RIFF" or head[8:12] != b"WEBP" or len(head) < 30:
        return None
    sizes = _webp_sizes(head, WEBP_FIRST_CHUNK, len(head))
    if not sizes:
        return None
    return max(width for width, _ in sizes), max(height for _, height in sizes)


def _jpeg(head: bytes):
    if head[:2] != JPEG_START:
        return None
    position = 2
    while position + 4 <= len(head):
        if head[position] != 0xFF:
            return None
        marker = head[position + 1]
        if marker == 0xFF:
            position += 1
            continue
        if marker in JPEG_MARKERS_WITHOUT_LENGTH:
            position += 2
            continue
        length = struct.unpack(">H", head[position + 2:position + 4])[0]
        if marker in JPEG_FRAME_MARKERS:
            if position + 9 > len(head):
                return None
            height, width = struct.unpack(">HH", head[position + 5:position + 9])
            return width, height
        position += 2 + length
    return None


def _avif(head: bytes):
    return _image_box_size(head, AVIF_BRANDS)


def _heif(head: bytes):
    return _image_box_size(head, HEIF_BRANDS)


def _image_box_size(head: bytes, brands: frozenset):
    if head[4:8] != b"ftyp" or head[8:12] not in brands:
        return None
    sizes = []
    box = head.find(AVIF_SIZE_BOX)
    while 0 <= box and box + 16 <= len(head):
        sizes.append(struct.unpack(">II", head[box + 8:box + 16]))
        box = head.find(AVIF_SIZE_BOX, box + 1)
    if not sizes:
        return None
    return max(width for width, _ in sizes), max(height for _, height in sizes)


READERS = {
    "image/png": _png,
    "image/gif": _gif,
    "image/bmp": _bmp,
    "image/webp": _webp,
    "image/jpeg": _jpeg,
    "image/avif": _avif,
    HEIF_TYPE: _heif,
}


def image_size(mime: str, head: bytes):
    reader = READERS.get(mime)
    if reader is None:
        return None
    try:
        size = reader(head)
    except (struct.error, IndexError):
        return None
    if not size or size[0] <= 0 or size[1] <= 0:
        return None
    return size
