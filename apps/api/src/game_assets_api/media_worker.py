from __future__ import annotations

import io
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageOps

from .qa import ImageProcessingError, normalize_image, remove_corner_color_key
from .storage import atomic_write_bytes, sha256_file


WORKER_VERSION = "m2.1"
REGISTERED_STRATEGIES = {
    "normalize",
    "color_key",
    "smart_matte",
    "edge_cleanup",
}


@dataclass(slots=True)
class EvidenceFile:
    kind: str
    label: str
    path: Path
    metadata: dict[str, Any]

    @property
    def sha256(self) -> str:
        return sha256_file(self.path)

    @property
    def byte_size(self) -> int:
        return self.path.stat().st_size


def _read_rgba(path: Path) -> Image.Image:
    try:
        with Image.open(path) as opened:
            opened.load()
            return opened.convert("RGBA")
    except OSError as exc:
        raise ImageProcessingError(f"worker input is not a decodable image: {path}") from exc


def _encode(image: Image.Image, path: Path, *, lossless: bool = True) -> None:
    output = io.BytesIO()
    image.save(
        output,
        "WEBP",
        lossless=lossless,
        quality=94,
        method=6,
        exif=b"",
        icc_profile=b"",
    )
    atomic_write_bytes(path, output.getvalue())


def _corner_background(image: Image.Image) -> tuple[int, int, int]:
    rgb = image.convert("RGB")
    width, height = rgb.size
    radius = max(1, min(width, height) // 32)
    samples: list[tuple[int, int, int]] = []
    for left, top in (
        (0, 0),
        (max(0, width - radius), 0),
        (0, max(0, height - radius)),
        (max(0, width - radius), max(0, height - radius)),
    ):
        crop = rgb.crop((left, top, min(width, left + radius), min(height, top + radius)))
        samples.extend(crop.getdata())
    count = max(1, len(samples))
    return tuple(round(sum(pixel[channel] for pixel in samples) / count) for channel in range(3))


def _smart_matte(image: Image.Image, *, tolerance: int, feather: float) -> tuple[Image.Image, float]:
    background = _corner_background(image)
    rgba = image.convert("RGBA")
    pixels: list[tuple[int, int, int, int]] = []
    for red, green, blue, original_alpha in rgba.getdata():
        distance = math.sqrt(
            (red - background[0]) ** 2
            + (green - background[1]) ** 2
            + (blue - background[2]) ** 2
        )
        alpha = int(max(0.0, min(255.0, (distance - tolerance) * 6.0)))
        pixels.append((red, green, blue, min(original_alpha, alpha)))
    rgba.putdata(pixels)
    if feather > 0:
        alpha = rgba.getchannel("A").filter(ImageFilter.GaussianBlur(radius=feather))
        rgba.putalpha(alpha)
    alpha = rgba.getchannel("A")
    coverage = sum(1 for value in alpha.resize((64, 64)).getdata() if value >= 32) / 4096
    return rgba, round(coverage, 4)


def _edge_cleanup(image: Image.Image, *, radius: float) -> Image.Image:
    rgba = image.convert("RGBA")
    alpha = rgba.getchannel("A")
    softened = alpha.filter(ImageFilter.GaussianBlur(radius=max(0.1, radius)))
    tightened = ImageEnhance.Contrast(softened).enhance(1.35)
    rgba.putalpha(tightened)
    red, green, blue, alpha = rgba.split()
    # Pull common green/magenta spill toward local luminance without changing opaque interiors.
    luminance = ImageOps.grayscale(rgba.convert("RGB"))
    edge_mask = ImageChops.difference(tightened, tightened.filter(ImageFilter.MaxFilter(3)))
    edge_mask = ImageOps.invert(edge_mask)
    red = Image.composite(red, luminance, edge_mask)
    green = Image.composite(green, luminance, edge_mask)
    blue = Image.composite(blue, luminance, edge_mask)
    return Image.merge("RGBA", (red, green, blue, alpha))


def apply_media_worker(
    source: Path,
    destination: Path,
    *,
    strategy: str,
    parameters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    parameters = parameters or {}
    if strategy not in REGISTERED_STRATEGIES:
        raise ImageProcessingError(f"unknown media worker strategy: {strategy}")
    if strategy == "normalize":
        source_bytes = source.read_bytes()
        return {
            **normalize_image(
                source_bytes,
                destination,
                expected_width=parameters.get("width"),
                expected_height=parameters.get("height"),
                transparent=bool(parameters.get("transparent", True)),
                crop=str(parameters.get("crop", "contain")),
            ),
            "strategy": strategy,
            "worker_version": WORKER_VERSION,
        }

    image = _read_rgba(source)
    metadata: dict[str, Any] = {}
    if strategy == "color_key":
        image, key = remove_corner_color_key(image)
        metadata["color_key_removed"] = key
    elif strategy == "smart_matte":
        image, coverage = _smart_matte(
            image,
            tolerance=int(parameters.get("tolerance", 22)),
            feather=float(parameters.get("feather", 0.8)),
        )
        metadata["subject_coverage"] = coverage
        metadata["coverage_warning"] = coverage < 0.02 or coverage > 0.98
    elif strategy == "edge_cleanup":
        image = _edge_cleanup(image, radius=float(parameters.get("radius", 0.7)))
    _encode(image, destination)
    return {
        "width": image.width,
        "height": image.height,
        "byte_size": destination.stat().st_size,
        "sha256": sha256_file(destination),
        "media_type": "image/webp",
        "strategy": strategy,
        "worker_version": WORKER_VERSION,
        **metadata,
    }


def _checkerboard(size: tuple[int, int], block: int = 16) -> Image.Image:
    background = Image.new("RGBA", size, (205, 205, 200, 255))
    draw = ImageDraw.Draw(background)
    for top in range(0, size[1], block):
        for left in range(0, size[0], block):
            if (left // block + top // block) % 2:
                draw.rectangle(
                    (left, top, min(size[0], left + block), min(size[1], top + block)),
                    fill=(151, 151, 147, 255),
                )
    return background


def _fit_on_background(image: Image.Image, background: Image.Image, padding: int = 18) -> None:
    target = (max(1, background.width - padding * 2), max(1, background.height - padding * 2))
    fitted = ImageOps.contain(image.convert("RGBA"), target, method=Image.Resampling.LANCZOS)
    position = ((background.width - fitted.width) // 2, (background.height - fitted.height) // 2)
    background.alpha_composite(fitted, position)


def make_contact_sheets(
    items: list[tuple[str, Path]],
    destination: Path,
    *,
    batch_index: int,
) -> list[EvidenceFile]:
    if not items:
        return []
    items = items[:6]
    cell_width, cell_height = 260, 220
    columns = min(3, len(items))
    rows = math.ceil(len(items) / columns)
    themes = {
        "dark": (24, 24, 22, 255),
        "light": (236, 232, 224, 255),
        "checkerboard": None,
    }
    results: list[EvidenceFile] = []
    destination.mkdir(parents=True, exist_ok=True)
    for theme, fill in themes.items():
        size = (columns * cell_width, rows * cell_height)
        canvas = _checkerboard(size) if fill is None else Image.new("RGBA", size, fill)
        draw = ImageDraw.Draw(canvas)
        for index, (label, path) in enumerate(items):
            row, column = divmod(index, columns)
            cell = Image.new("RGBA", (cell_width, cell_height), (0, 0, 0, 0))
            image = _read_rgba(path)
            _fit_on_background(image, cell, padding=26)
            left, top = column * cell_width, row * cell_height
            canvas.alpha_composite(cell, (left, top))
            text_fill = (235, 231, 222, 255) if theme == "dark" else (38, 36, 31, 255)
            draw.rectangle(
                (left, top + cell_height - 28, left + cell_width, top + cell_height),
                fill=(15, 15, 14, 190) if theme == "dark" else (245, 242, 235, 220),
            )
            draw.text((left + 10, top + cell_height - 21), label[:34], fill=text_fill)
        path = destination / f"batch-{batch_index:02d}-{theme}.webp"
        _encode(canvas, path)
        results.append(
            EvidenceFile(
                kind=f"contact_sheet_{theme}",
                label=f"批次 {batch_index} · {theme}",
                path=path,
                metadata={"batch": batch_index, "theme": theme, "items": len(items)},
            )
        )
    return results


def make_comparison_bundle(
    reference: Path,
    candidate: Path,
    destination: Path,
) -> list[EvidenceFile]:
    destination.mkdir(parents=True, exist_ok=True)
    candidate_image = _read_rgba(candidate)
    reference_image = ImageOps.fit(
        _read_rgba(reference), candidate_image.size, method=Image.Resampling.LANCZOS
    )
    results: list[EvidenceFile] = []

    side = Image.new("RGBA", (candidate_image.width * 2, candidate_image.height), (22, 22, 20, 255))
    side.alpha_composite(reference_image, (0, 0))
    side.alpha_composite(candidate_image, (candidate_image.width, 0))
    side_path = destination / "side-by-side.webp"
    _encode(side, side_path)
    results.append(EvidenceFile("side_by_side", "参考 / 候选", side_path, {}))

    overlay = Image.blend(reference_image, candidate_image, 0.5)
    overlay_path = destination / "overlay-50.webp"
    _encode(overlay, overlay_path)
    results.append(EvidenceFile("overlay", "50% 叠加", overlay_path, {"opacity": 0.5}))

    difference = ImageChops.difference(reference_image.convert("RGB"), candidate_image.convert("RGB"))
    difference = ImageOps.autocontrast(difference).convert("RGBA")
    diff_path = destination / "difference.webp"
    _encode(difference, diff_path)
    results.append(EvidenceFile("difference", "像素差异", diff_path, {"mode": "pixel"}))
    return results
