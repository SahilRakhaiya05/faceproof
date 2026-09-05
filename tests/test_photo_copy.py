from __future__ import annotations

import io
import json
import struct
import zlib

import numpy as np
import pytest
from PIL import Image, ImageDraw, PngImagePlugin

from faceproof.photo_copy import (
    MAX_ENCODED_IMAGE_BYTES,
    MAX_QUERY_DIMENSION,
    PhotoInputError,
    compare_photos,
    normalize_photo,
)


def _encode(photo: Image.Image, format: str = "PNG", **kwargs: object) -> bytes:
    output = io.BytesIO()
    photo.save(output, format=format, **kwargs)
    return output.getvalue()


def _scene(seed: int = 7) -> Image.Image:
    """Deterministic non-biometric scene with texture and varied composition."""
    generator = np.random.default_rng(seed)
    values = generator.integers(0, 256, (24, 32, 3), dtype=np.uint8)
    photo = Image.fromarray(values).resize((640, 480), Image.Resampling.BICUBIC)
    drawing = ImageDraw.Draw(photo)
    drawing.rectangle((58, 250, 330, 419), fill=(180, 74, 40))
    drawing.polygon([(40, 250), (185, 102), (345, 250)], fill=(52, 43, 39))
    drawing.rectangle((154, 305, 220, 419), fill=(50, 47, 45))
    drawing.ellipse((430, 55, 560, 185), fill=(251, 234, 125))
    return photo


@pytest.mark.parametrize("format", ["PNG", "JPEG", "WEBP"])
def test_normalization_returns_metadata_free_rgb_jpeg(format: str) -> None:
    photo = _scene()
    exif = Image.Exif()
    exif[315] = "Private creator metadata"
    original = _encode(photo, format, exif=exif)

    result = normalize_photo(original)

    with Image.open(io.BytesIO(result)) as normalized:
        assert normalized.format == "JPEG"
        assert normalized.mode == "RGB"
        assert normalized.size == photo.size
        assert not normalized.getexif()
        assert "icc_profile" not in normalized.info
        assert "comment" not in normalized.info
    assert b"Private creator metadata" not in result


def test_normalization_applies_exif_orientation_and_strips_png_text() -> None:
    photo = _scene()
    exif = Image.Exif()
    exif[274] = 6
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("secret", "Private location metadata")

    result = normalize_photo(_encode(photo, exif=exif, pnginfo=metadata))

    with Image.open(io.BytesIO(result)) as normalized:
        assert normalized.size == (480, 640)
        assert not normalized.getexif()
    assert b"Private location metadata" not in result
    rotated = photo.transpose(Image.Transpose.ROTATE_270)
    assert compare_photos(result, _encode(rotated)).decision == "likely-copy"


def test_normalization_bounds_query_dimensions_and_flattens_transparency() -> None:
    photo = Image.new("RGBA", (2400, 1200), (120, 12, 240, 0))

    with Image.open(io.BytesIO(normalize_photo(_encode(photo)))) as normalized:
        assert normalized.size == (MAX_QUERY_DIMENSION, MAX_QUERY_DIMENSION // 2)
        assert normalized.getpixel((0, 0)) == (255, 255, 255)


def test_exact_compares_decoded_pixels_across_metadata_and_lossless_formats() -> None:
    photo = _scene()
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("description", "Different metadata")

    result = compare_photos(_encode(photo, pnginfo=metadata), _encode(photo, "WEBP", lossless=True))

    assert result.decision == "exact"
    assert result.score == 100
    assert result.metrics["exact_normalized_pixels"] is True
    assert json.loads(json.dumps(result.to_dict()))["decision"] == "exact"


def test_exact_respects_exif_orientation() -> None:
    photo = _scene()
    exif = Image.Exif()
    exif[274] = 6

    result = compare_photos(
        _encode(photo, exif=exif), _encode(photo.transpose(Image.Transpose.ROTATE_270))
    )

    assert result.decision == "exact"


@pytest.mark.parametrize("size,quality", [((640, 480), 60), ((320, 240), 75), ((960, 720), 90)])
def test_recognizes_whole_photo_resize_and_jpeg_recompression(
    size: tuple[int, int], quality: int
) -> None:
    photo = _scene()
    candidate = photo.resize(size, Image.Resampling.LANCZOS)

    result = compare_photos(
        normalize_photo(_encode(photo)), _encode(candidate, "JPEG", quality=quality)
    )

    assert result.decision == "likely-copy", result.to_dict()
    assert 95 <= result.score < 100
    assert result.metrics["passed_copy_checks"] is True


def test_rejects_different_aspect_even_when_image_content_is_stretched_to_match() -> None:
    photo = _scene()
    result = compare_photos(_encode(photo), _encode(photo.resize((640, 320))))

    assert result.decision == "unconfirmed"
    assert result.metrics["aspect_matches"] is False


def test_unrelated_photos_are_unconfirmed() -> None:
    result = compare_photos(_encode(_scene(1)), _encode(_scene(21)))

    assert result.decision == "unconfirmed"
    assert result.metrics["passed_copy_checks"] is False
    assert 0 <= result.score < 95


def test_matching_geometry_with_changed_colors_fails_pixel_gate() -> None:
    photo = _scene()
    changed = Image.fromarray(np.asarray(photo)[:, :, [2, 1, 0]])

    result = compare_photos(_encode(photo), _encode(changed))

    assert result.decision == "unconfirmed"
    assert result.metrics["rgb_mean_absolute_error"] > 0.045


def test_local_replacement_is_not_a_confirmed_copy() -> None:
    photo = _scene()
    changed = photo.copy()
    ImageDraw.Draw(changed).rectangle((270, 210, 370, 290), fill="white")

    result = compare_photos(_encode(photo), _encode(changed))

    assert result.decision == "unconfirmed"
    assert result.metrics["maximum_tile_rgb_error"] > 0.14


def test_flat_images_only_match_if_full_normalized_pixels_are_exact() -> None:
    same = _encode(Image.new("RGB", (200, 100), (120, 120, 120)))
    changed = _encode(Image.new("RGB", (200, 100), (121, 121, 121)))
    resized = _encode(Image.new("RGB", (400, 200), (120, 120, 120)))

    assert compare_photos(same, same).decision == "exact"
    for candidate in (changed, resized):
        result = compare_photos(same, candidate)
        assert result.decision == "unconfirmed"
        assert result.metrics["sufficient_detail"] is False


def test_featureless_gradient_is_not_a_likely_copy() -> None:
    gradient = np.tile(np.linspace(30, 220, 400).astype(np.uint8), (200, 1))
    photo = Image.fromarray(gradient).convert("RGB")

    result = compare_photos(_encode(photo), _encode(photo.resize((800, 400))))

    assert result.decision == "unconfirmed"
    assert result.metrics["sufficient_detail"] is False


@pytest.mark.parametrize("bad", [b"", b"not an image", b"\x89PNG\r\n\x1a\ninvalid"])
def test_rejects_invalid_and_incomplete_input(bad: bytes) -> None:
    with pytest.raises(PhotoInputError):
        normalize_photo(bad)
    with pytest.raises(PhotoInputError):
        compare_photos(_encode(_scene()), bad)


def test_rejects_truncated_jpeg() -> None:
    encoded = _encode(_scene(), "JPEG")
    with pytest.raises(PhotoInputError, match="invalid, incomplete"):
        normalize_photo(encoded[: len(encoded) // 2])


def test_rejects_unsupported_formats_and_animation() -> None:
    with pytest.raises(PhotoInputError, match="JPEG, PNG, and WebP"):
        normalize_photo(_encode(_scene(), "BMP"))

    animation = _encode(
        _scene(), "PNG", save_all=True, append_images=[_scene(4)], duration=100, loop=0
    )
    with pytest.raises(PhotoInputError, match="Animated"):
        normalize_photo(animation)


def test_rejects_oversized_encoded_input_before_decoding() -> None:
    with pytest.raises(PhotoInputError, match="25 MiB"):
        normalize_photo(b"x" * (MAX_ENCODED_IMAGE_BYTES + 1))


@pytest.mark.parametrize("dimensions", [(8000, 6000), (100000, 100000)])
def test_rejects_oversized_decoded_headers_without_allocating_pixels(
    dimensions: tuple[int, int],
) -> None:
    # A CRC-correct PNG header advertises huge dimensions but contains no pixels.
    header = struct.pack(">IIBBBBB", *dimensions, 8, 2, 0, 0, 0)
    chunk = b"IHDR" + header
    encoded = (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", len(header))
        + chunk
        + struct.pack(">I", zlib.crc32(chunk))
        + b"\x00\x00\x00\x00IEND\xaeB`\x82"
    )

    with pytest.raises(PhotoInputError, match="limit"):
        normalize_photo(encoded)
