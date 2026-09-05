"""Conservative comparisons of complete photographs, with no identity inference.

``score`` is a 0--100 whole-photo similarity index, not a probability. Only
``exact`` and ``likely-copy`` decisions support a copy claim. An ``unconfirmed``
result does not establish whether two photos depict the same subject. The
comparison deliberately does not search for crops, people, or objects.
"""

from __future__ import annotations

import hashlib
import io
import warnings
from dataclasses import asdict, dataclass
from typing import Any

import cv2
import numpy as np
from PIL import Image, ImageOps, UnidentifiedImageError

MAX_ENCODED_IMAGE_BYTES = 25 * 1024 * 1024
MAX_DECODED_IMAGE_PIXELS = 40_000_000
MAX_QUERY_DIMENSION = 1600
ALGORITHM = "whole-photo-phash-ssim-rgb-v1"
_FORMATS = frozenset({"JPEG", "PNG", "WEBP"})
_COMPARISON_SIZE = 256


class PhotoInputError(ValueError):
    """An input cannot safely be used as a supported, still photograph."""


@dataclass(frozen=True)
class PhotoComparison:
    """Whole-photo evidence; this result makes no statement about identity."""

    score: float
    decision: str
    algorithm: str
    metrics: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _decode_photo(data: bytes) -> Image.Image:
    if not isinstance(data, bytes) or not data:
        raise PhotoInputError("Photo must be nonempty encoded image bytes.")
    if len(data) > MAX_ENCODED_IMAGE_BYTES:
        raise PhotoInputError("Photo exceeds the 25 MiB encoded size limit.")

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as header:
                if header.format not in _FORMATS:
                    raise PhotoInputError("Only still JPEG, PNG, and WebP photos are supported.")
                width, height = header.size
                if width < 1 or height < 1 or width * height > MAX_DECODED_IMAGE_PIXELS:
                    raise PhotoInputError("Photo exceeds the 40 million decoded pixel limit.")
                if getattr(header, "n_frames", 1) != 1:
                    raise PhotoInputError("Animated images are not supported; use a still photo.")
                header.verify()

            with Image.open(io.BytesIO(data)) as encoded:
                encoded.load()
                oriented = ImageOps.exif_transpose(encoded)
                # A fixed white background makes transparency deterministic. A
                # fresh RGB image also discards EXIF, ICC, comments, and PNG text.
                if oriented.mode in {"RGBA", "LA", "P"} or "transparency" in oriented.info:
                    rgba = oriented.convert("RGBA")
                    result = Image.new("RGB", rgba.size, "white")
                    result.paste(rgba, mask=rgba.getchannel("A"))
                else:
                    result = Image.new("RGB", oriented.size)
                    result.paste(oriented.convert("RGB"))
                return result
    except PhotoInputError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise PhotoInputError("Photo exceeds safe decoded image limits.") from exc
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        raise PhotoInputError("Photo is invalid, incomplete, or cannot be decoded.") from exc


def normalize_photo(data: bytes) -> bytes:
    """Return a metadata-free, oriented RGB JPEG, at most 1600 pixels per side.

    Inputs are bounded at 25 MiB and 40 million decoded pixels. JPEG, PNG, and
    still WebP are accepted; transparency is composited over white. All content
    is kept when reducing dimensions: normalization never crops the image.
    """
    with _decode_photo(data) as photo:
        photo.thumbnail((MAX_QUERY_DIMENSION, MAX_QUERY_DIMENSION), Image.Resampling.LANCZOS)
        output = io.BytesIO()
        photo.save(output, format="JPEG", quality=92, subsampling=0)
        return output.getvalue()


def _pixel_digest(photo: Image.Image) -> bytes:
    digest = hashlib.sha256()
    digest.update(f"RGB:{photo.width}:{photo.height}:".encode("ascii"))
    # Avoid allocating another full-resolution byte buffer for large inputs.
    for top in range(0, photo.height, 128):
        with photo.crop((0, top, photo.width, min(top + 128, photo.height))) as rows:
            digest.update(rows.tobytes())
    return digest.digest()


def _thumbnail(photo: Image.Image) -> np.ndarray:
    # Both full frames are sampled on the same grid. The aspect-ratio gate below
    # prevents the common grid from treating stretched images as copies.
    with photo.resize((_COMPARISON_SIZE, _COMPARISON_SIZE), Image.Resampling.LANCZOS) as thumbnail:
        return np.asarray(thumbnail, dtype=np.float32).copy()


def _gray(rgb: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)


def _phash(gray: np.ndarray) -> np.ndarray:
    small = cv2.resize(gray, (32, 32), interpolation=cv2.INTER_AREA)
    # Discard the DC coefficient so overall brightness cannot supply a bit.
    coefficients = cv2.dct(small)[:8, :8].ravel()[1:]
    return coefficients > np.median(coefficients)


def _ssim(first: np.ndarray, second: np.ndarray) -> float:
    mean_a = cv2.GaussianBlur(first, (11, 11), 1.5)
    mean_b = cv2.GaussianBlur(second, (11, 11), 1.5)
    variance_a = np.maximum(cv2.GaussianBlur(first * first, (11, 11), 1.5) - mean_a**2, 0)
    variance_b = np.maximum(cv2.GaussianBlur(second * second, (11, 11), 1.5) - mean_b**2, 0)
    covariance = cv2.GaussianBlur(first * second, (11, 11), 1.5) - mean_a * mean_b
    luminance = (2 * mean_a * mean_b + 6.5025) / (mean_a**2 + mean_b**2 + 6.5025)
    structure = (2 * covariance + 58.5225) / (variance_a + variance_b + 58.5225)
    return float(np.clip(np.mean(luminance * structure), 0, 1))


def _detail(gray: np.ndarray) -> tuple[float, float]:
    contrast = float(np.std(gray))
    residual = gray - cv2.GaussianBlur(gray, (0, 0), 2)
    detail = float(np.sqrt(np.mean(residual**2)))
    return contrast, detail


def compare_photos(query: bytes, candidate: bytes) -> PhotoComparison:
    """Compare complete images after EXIF orientation and RGB conversion.

    ``exact`` requires identical normalized full-resolution pixels and size.
    ``likely-copy`` requires similar aspect ratios, sufficient image detail,
    and agreement of perceptual hash, structural similarity, and RGB pixels.
    This is intentionally conservative and does not align crops or major edits.
    Very small or featureless images remain ``unconfirmed`` unless their pixels
    are exact. The uncalibrated numeric index never expresses a person's identity.
    """
    with _decode_photo(query) as first, _decode_photo(candidate) as second:
        sizes = {"query_size": list(first.size), "candidate_size": list(second.size)}
        if first.size == second.size and _pixel_digest(first) == _pixel_digest(second):
            return PhotoComparison(
                score=100.0,
                decision="exact",
                algorithm=ALGORITHM,
                metrics={**sizes, "exact_normalized_pixels": True, "passed_copy_checks": True},
            )

        ratio_a = first.width / first.height
        ratio_b = second.width / second.height
        aspect_difference = abs(ratio_a - ratio_b) / max(ratio_a, ratio_b)
        enough_resolution = min(*first.size, *second.size) >= 64
        rgb_a, rgb_b = _thumbnail(first), _thumbnail(second)

    # Light smoothing suppresses codec noise without searching for alignment,
    # regions, faces, or other semantic features.
    rgb_a = cv2.GaussianBlur(rgb_a, (3, 3), 0.6)
    rgb_b = cv2.GaussianBlur(rgb_b, (3, 3), 0.6)
    gray_a, gray_b = _gray(rgb_a), _gray(rgb_b)
    hash_distance = int(np.count_nonzero(_phash(gray_a) != _phash(gray_b)))
    hash_similarity = 1 - hash_distance / 63
    ssim = _ssim(gray_a, gray_b)
    differences = np.abs(rgb_a - rgb_b) / 255
    mean_error = float(np.mean(differences))
    # This also rejects changes confined to a small part of the full frame.
    tile_error = float(differences.reshape(16, 16, 16, 16, 3).mean(axis=(1, 3, 4)).max())
    contrast_a, detail_a = _detail(gray_a)
    contrast_b, detail_b = _detail(gray_b)
    enough_detail = min(contrast_a, contrast_b) >= 10 and min(detail_a, detail_b) >= 1.5
    aspect_matches = aspect_difference <= 0.02
    passed = (
        aspect_matches
        and enough_resolution
        and enough_detail
        and hash_distance <= 8
        and ssim >= 0.95
        and mean_error <= 0.045
        and tile_error <= 0.14
    )
    # Include geometry in the index as well as in the independent decision gate.
    appearance = 0.30 * hash_similarity + 0.50 * ssim + 0.20 * (1 - mean_error)
    score = round(min(99.9, max(0.0, appearance * (1 - aspect_difference) * 100)), 2)
    return PhotoComparison(
        score=score,
        decision="likely-copy" if passed else "unconfirmed",
        algorithm=ALGORITHM,
        metrics={
            **sizes,
            "exact_normalized_pixels": False,
            "aspect_ratio_difference": round(aspect_difference, 6),
            "phash_hamming_distance": hash_distance,
            "phash_bits": 63,
            "structural_similarity": round(ssim, 6),
            "rgb_mean_absolute_error": round(mean_error, 6),
            "maximum_tile_rgb_error": round(tile_error, 6),
            "query_contrast": round(contrast_a, 4),
            "candidate_contrast": round(contrast_b, 4),
            "query_detail": round(detail_a, 4),
            "candidate_detail": round(detail_b, 4),
            "aspect_matches": aspect_matches,
            "sufficient_resolution": enough_resolution,
            "sufficient_detail": enough_detail,
            "passed_copy_checks": bool(passed),
        },
    )
