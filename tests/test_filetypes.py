import io
import struct
import zipfile

import pytest

from conftest import make_png
from drive.blobs import BlobStore
from drive.errors import DriveError
from drive.filetypes import OCTET_STREAM, detect_type, preview_kind
from drive.imagesize import image_size
from drive.zipstream import check_zip_size, stream_zip


def jpeg(width: int, height: int) -> bytes:
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + b"\x00" * 9
    frame = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, height, width, 1) + b"\x01\x11\x00"
    return b"\xff\xd8" + app0 + frame + b"\xff\xd9"


def test_image_sizes_are_read_from_the_header():
    assert image_size("image/png", make_png(640, 480)) == (640, 480)
    assert image_size("image/jpeg", jpeg(4000, 3000)) == (4000, 3000)
    assert image_size("image/gif", b"GIF89a" + struct.pack("<HH", 10, 20)) == (10, 20)
    assert image_size("image/bmp", b"BM" + b"\x00" * 16 + struct.pack("<ii", 8, -9)) == (8, 9)
    assert image_size("image/png", b"not a png") is None
    assert image_size("image/jpeg", b"\xff\xd8\xff") is None
    assert image_size("image/tiff", b"II*\x00") is None


@pytest.mark.parametrize("name,head,expected", [
    ("a.png", make_png(), "image/png"),
    ("a.PNG", make_png(), "image/png"),
    ("a.jpg", jpeg(100, 100), "image/jpeg"),
    ("huge.jpg", jpeg(20000, 20000), OCTET_STREAM),
    ("a.png", b"<script>", OCTET_STREAM),
    ("a.svg", b"<svg/>", "text/plain"),
    ("a.html", b"<html>", "text/plain"),
    ("a.pdf", b"%PDF-1.4", "application/pdf"),
    ("a.pdf", b"PK\x03\x04", OCTET_STREAM),
    ("a.exe", b"MZ\x00\x00", OCTET_STREAM),
    ("README", b"hello", "text/plain"),
    ("notes.txt", b"\x00\x01binary", OCTET_STREAM),
    ("a.mp4", b"", "video/mp4"),
])
def test_served_types_come_from_the_name_and_the_bytes(name, head, expected):
    assert detect_type(name, head) == expected


def test_preview_kinds():
    assert preview_kind("image/png") == "image"
    assert preview_kind("text/plain") == "text"
    assert preview_kind("application/pdf") == "pdf"
    assert preview_kind("image/svg+xml") == "none"
    assert preview_kind(OCTET_STREAM) == "none"


def test_streamed_zip_round_trips(tmp_path):
    blobs = BlobStore(tmp_path)
    source = tmp_path / "source"
    source.write_bytes(b"x" * 3_000_000)
    blob = blobs.adopt(source)
    entries = [("dir/", {"kind": "folder", "modifiedAt": 0}),
               ("dir/big.bin", {"kind": "file", "size": 3_000_000, "blob": blob, "modifiedAt": 1_700_000_000})]
    data = b"".join(stream_zip(entries, blobs))
    archive = zipfile.ZipFile(io.BytesIO(data))
    assert archive.read("dir/big.bin") == b"x" * 3_000_000
    assert archive.getinfo("dir/").is_dir()


def test_zip_caps_count_and_size():
    with pytest.raises(DriveError):
        check_zip_size([("a", {"kind": "file", "size": 11})], 10)
    assert check_zip_size([("a", {"kind": "file", "size": 10})], 10) == 10


def test_blob_paths_refuse_anything_but_ids(tmp_path):
    blobs = BlobStore(tmp_path)
    for bad in ("../../etc/passwd", "0" * 31, "g" * 32, None):
        with pytest.raises(ValueError):
            blobs.path(bad)


def gif_with_frame(screen: tuple[int, int], frame: tuple[int, int]) -> bytes:
    header = b"GIF89a" + struct.pack("<HHBBB", *screen, 0, 0, 0)
    extension = b"\x21\xf9\x04\x00\x00\x00\x00\x00"
    descriptor = b"\x2c" + struct.pack("<HHHHB", 0, 0, *frame, 0)
    return header + extension + descriptor + b"\x02\x02\x4c\x01\x00\x3b"


def riff(chunks: bytes) -> bytes:
    return b"RIFF" + struct.pack("<I", 4 + len(chunks)) + b"WEBP" + chunks


def webp_chunk(kind: bytes, data: bytes) -> bytes:
    return kind + struct.pack("<I", len(data)) + data + (b"\x00" if len(data) % 2 else b"")


def vp8x(width: int, height: int) -> bytes:
    return webp_chunk(b"VP8X", b"\x02\x00\x00\x00" + (width - 1).to_bytes(3, "little")
                      + (height - 1).to_bytes(3, "little"))


def anmf(width: int, height: int) -> bytes:
    return webp_chunk(b"ANMF", b"\x00" * 6 + (width - 1).to_bytes(3, "little") + (height - 1).to_bytes(3, "little")
                      + b"\x00" * 4)


def avif(*sizes: tuple[int, int]) -> bytes:
    boxes = b"".join(struct.pack(">I", 20) + b"ispe" + b"\x00" * 4 + struct.pack(">II", *size) for size in sizes)
    return struct.pack(">I", 16) + b"ftypavif" + b"\x00" * 4 + boxes


def test_gif_frames_larger_than_the_screen_count_for_the_pixel_guard():
    assert image_size("image/gif", gif_with_frame((10, 10), (8, 8))) == (10, 10)
    assert image_size("image/gif", gif_with_frame((10, 10), (20000, 20000))) == (20000, 20000)
    assert detect_type("bomb.gif", gif_with_frame((10, 10), (20000, 20000))) == OCTET_STREAM
    assert detect_type("fine.gif", gif_with_frame((10, 10), (8, 8))) == "image/gif"


def test_webp_frames_and_avif_tiles_count_for_the_pixel_guard():
    assert image_size("image/webp", riff(vp8x(16, 16) + anmf(16, 16))) == (16, 16)
    assert detect_type("bomb.webp", riff(vp8x(16, 16) + anmf(16000, 16000))) == OCTET_STREAM
    assert detect_type("fine.webp", riff(vp8x(16, 16) + anmf(16, 16))) == "image/webp"
    assert image_size("image/avif", avif((512, 512), (20000, 20000))) == (20000, 20000)
    assert detect_type("bomb.avif", avif((512, 512), (20000, 20000))) == OCTET_STREAM
    assert detect_type("fine.avif", avif((512, 512))) == "image/avif"
