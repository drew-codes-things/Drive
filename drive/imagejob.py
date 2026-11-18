import sys

import pillow_heif
from PIL import Image, ImageOps

from .thumbnails import flatten_onto_white

SAVE_OPTIONS = {
    "jpg": ("JPEG", {"quality": 90, "optimize": True}),
    "png": ("PNG", {"optimize": True}),
    "webp": ("WEBP", {"quality": 90, "method": 4}),
    "gif": ("GIF", {}),
}
MODES_FOR_TARGET = {
    "png": ("1", "L", "LA", "P", "RGB", "RGBA", "I;16"),
    "webp": ("RGB", "RGBA"),
    "gif": ("P", "L"),
}
REFUSED_EXIT = 3


def refuse(message: str) -> int:
    print(message, file=sys.stderr)
    return REFUSED_EXIT


def prepared(image: Image.Image, target: str) -> Image.Image:
    upright = ImageOps.exif_transpose(image)
    if target == "jpg":
        return flatten_onto_white(upright)
    if upright.mode in MODES_FOR_TARGET[target]:
        return upright
    if target == "gif":
        full_colour = upright.convert("RGBA" if "A" in upright.getbands() else "RGB")
        return full_colour.convert("P", palette=Image.Palette.ADAPTIVE)
    return upright.convert("RGBA" if "A" in upright.getbands() or "transparency" in upright.info else "RGB")


def convert(source: str, output: str, target: str, max_pixels: int) -> int:
    pillow_heif.register_heif_opener()
    Image.MAX_IMAGE_PIXELS = max_pixels
    with Image.open(source) as image:
        width, height = image.size
        if width * height > max_pixels:
            return refuse(f"{width} x {height} pixels is more than {max_pixels} pixels, so it is not converted")
        image.seek(0)
        image.load()
        image_format, options = SAVE_OPTIONS[target]
        prepared(image, target).save(output, image_format, **options)
    return 0


def main(arguments: list[str]) -> int:
    if len(arguments) != 4 or arguments[2] not in SAVE_OPTIONS or not arguments[3].isdigit():
        return refuse("usage: imagejob SOURCE OUTPUT jpg|png|webp|gif MAX_PIXELS")
    try:
        return convert(arguments[0], arguments[1], arguments[2], int(arguments[3]))
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError) as error:
        return refuse(f"the image could not be read: {type(error).__name__}: {error}")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
