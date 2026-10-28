from __future__ import annotations

import io

from PIL import Image, ImageDraw

from game_assets_api.qa import inspect_image, normalize_image, remove_corner_color_key


def _portrait_with_lower_corners_covered() -> bytes:
    image = Image.new("RGB", (64, 96), (0, 255, 102))
    draw = ImageDraw.Draw(image)
    draw.rectangle((17, 12, 47, 95), fill=(80, 62, 48))
    draw.rectangle((0, 78, 63, 95), fill=(80, 62, 48))
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def test_color_key_uses_exposed_border_when_lower_corners_are_covered(tmp_path) -> None:
    destination = tmp_path / "portrait.webp"
    info = normalize_image(
        _portrait_with_lower_corners_covered(), destination,
        expected_width=64, expected_height=96, transparent=True,
    )
    assert info["color_key_removed"] == "green"
    with Image.open(destination) as image:
        assert image.getpixel((0, 0))[3] == 0
        assert image.getpixel((32, 48))[3] == 255
    verdict, checks = inspect_image(destination, expected_width=64, expected_height=96, require_alpha=True)
    assert verdict == "pass"
    assert next(check for check in checks if check["name"] == "residualColorKey")["passed"]


def test_color_key_does_not_guess_from_only_two_green_corners() -> None:
    image = Image.new("RGB", (32, 32), (80, 62, 48))
    for x in range(32):
        image.putpixel((x, 0), (0, 255, 102))
    keyed, key = remove_corner_color_key(image)
    assert key is None
    assert keyed.getchannel("A").getextrema() == (255, 255)
