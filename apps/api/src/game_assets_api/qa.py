from __future__ import annotations

import io
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps, UnidentifiedImageError

from .storage import atomic_write_bytes, sha256_file


class ImageProcessingError(RuntimeError):
    pass


def _near_key_color(pixel: tuple[int, ...], key: str, tolerance: int = 55) -> bool:
    red, green, blue = pixel[:3]
    if key == "green":
        return green > 120 and green - max(red, blue) > tolerance
    return red > 120 and blue > 120 and min(red, blue) - green > tolerance


def remove_corner_color_key(image: Image.Image) -> tuple[Image.Image, str | None]:
    rgba = image.convert("RGBA")
    width, height = rgba.size
    corners = [
        rgba.getpixel((0, 0)),
        rgba.getpixel((max(0, width - 1), 0)),
        rgba.getpixel((0, max(0, height - 1))),
        rgba.getpixel((max(0, width - 1), max(0, height - 1))),
    ]
    green_votes = sum(_near_key_color(pixel, "green") for pixel in corners)
    magenta_votes = sum(_near_key_color(pixel, "magenta") for pixel in corners)
    key = "green" if green_votes >= 3 else "magenta" if magenta_votes >= 3 else None
    if key is None:
        return rgba, None

    pixels = []
    for red, green, blue, alpha in rgba.getdata():
        if _near_key_color((red, green, blue, alpha), key):
            pixels.append((red, green, blue, 0))
        else:
            # Pull residual key color toward the other channels to reduce edge fringing.
            if key == "green" and green > max(red, blue):
                green = max(red, blue)
            elif key == "magenta" and min(red, blue) > green:
                red = min(red, green + 24)
                blue = min(blue, green + 24)
            pixels.append((red, green, blue, alpha))
    rgba.putdata(pixels)
    return rgba, key


def normalize_image(
    source: bytes,
    destination: Path,
    *,
    expected_width: int | None,
    expected_height: int | None,
    transparent: bool,
    crop: str = "contain",
) -> dict[str, Any]:
    try:
        with Image.open(io.BytesIO(source)) as opened:
            opened.load()
            image = opened.convert("RGBA" if transparent else "RGB")
    except (UnidentifiedImageError, OSError) as exc:
        raise ImageProcessingError("provider returned an invalid image") from exc

    keyed: str | None = None
    if transparent:
        image, keyed = remove_corner_color_key(image)

    if expected_width and expected_height and image.size != (expected_width, expected_height):
        target_size = (expected_width, expected_height)
        if crop == "cover":
            image = ImageOps.fit(image, target_size, method=Image.Resampling.LANCZOS)
        else:
            contained = ImageOps.contain(image, target_size, method=Image.Resampling.LANCZOS)
            mode = "RGBA" if transparent else "RGB"
            background = (0, 0, 0, 0) if transparent else (0, 0, 0)
            canvas = Image.new(mode, target_size, background)
            canvas.alpha_composite(contained, ((expected_width - contained.width) // 2, (expected_height - contained.height) // 2)) if transparent else canvas.paste(
                contained, ((expected_width - contained.width) // 2, (expected_height - contained.height) // 2)
            )
            image = canvas

    output = io.BytesIO()
    image.save(output, "WEBP", lossless=transparent, quality=92, method=6, exif=b"", icc_profile=b"")
    atomic_write_bytes(destination, output.getvalue(), immutable=True)
    return {
        "width": image.width,
        "height": image.height,
        "byteSize": destination.stat().st_size,
        "sha256": sha256_file(destination),
        "mediaType": "image/webp",
        "colorKeyRemoved": keyed,
    }


def inspect_image(
    path: Path,
    *,
    expected_width: int | None = None,
    expected_height: int | None = None,
    require_alpha: bool = False,
    max_bytes: int | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    checks: list[dict[str, Any]] = []
    try:
        with Image.open(path) as image:
            image.load()
            width, height = image.size
            alpha = "A" in image.getbands()
            checks.append({"name": "decodable", "passed": True, "value": image.format})
            if expected_width is not None:
                checks.append({"name": "width", "passed": width == expected_width, "value": width, "expected": expected_width})
            if expected_height is not None:
                checks.append({"name": "height", "passed": height == expected_height, "value": height, "expected": expected_height})
            if require_alpha:
                extrema = image.getchannel("A").getextrema() if alpha else (255, 255)
                checks.append({"name": "alpha", "passed": alpha and extrema[0] < 255, "value": extrema})
            if alpha:
                alpha_channel = image.getchannel("A")
                bbox = alpha_channel.getbbox()
                coverage = 0.0 if bbox is None else (bbox[2] - bbox[0]) * (bbox[3] - bbox[1]) / (width * height)
                corners = [alpha_channel.getpixel(point) for point in ((0, 0), (width - 1, 0), (0, height - 1), (width - 1, height - 1))]
                checks.append({"name": "transparentCorners", "passed": min(corners) < 32, "value": corners})
                checks.append({"name": "subjectCoverage", "passed": 0.02 <= coverage <= 1.0, "value": round(coverage, 4)})
            rgb = image.convert("RGB")
            sampled = list(rgb.resize((min(64, width), min(64, height))).getdata())
            residual = sum(
                1 for pixel in sampled if _near_key_color(pixel, "green") or _near_key_color(pixel, "magenta")
            ) / max(1, len(sampled))
            checks.append({"name": "residualColorKey", "passed": residual < 0.03, "value": round(residual, 4)})
    except (UnidentifiedImageError, OSError) as exc:
        return "fail", [{"name": "decodable", "passed": False, "message": str(exc)}]

    byte_size = path.stat().st_size
    if max_bytes is not None:
        checks.append({"name": "fileSize", "passed": byte_size <= max_bytes, "value": byte_size, "expectedMax": max_bytes})
    hard_names = {"decodable", "width", "height", "alpha", "fileSize"}
    hard_failed = any(not check["passed"] and check["name"] in hard_names for check in checks)
    warning = any(not check["passed"] for check in checks)
    return ("fail" if hard_failed else "warning" if warning else "pass"), checks
