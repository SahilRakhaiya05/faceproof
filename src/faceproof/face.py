"""Face detection, quality validation, and embedding utilities.

The OpenCV dependency and the YuNet/SFace model files are deliberately loaded
only when :class:`OpenCVFaceBackend` is used.  The remaining helpers are pure
Python so that ranking, thresholds, evidence metadata, and failure cases can be
tested in environments that do not have OpenCV or model assets installed.

Images passed as arrays are expected to be uint8 BGR images.  Paths and encoded
image bytes are decoded by OpenCV.  Face embeddings are biometric data: callers
should keep them ephemeral and persist a cryptographic digest when possible.
"""

from __future__ import annotations

import hashlib
import importlib
import io
import math
import os
import threading
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError

# OpenCV's SFace tutorial reports 0.363 as an LFW cosine threshold.  It is a
# useful demo default, not a universal operating point; deployments must tune it
# on representative data and usually choose a stricter threshold for web search.
DEFAULT_COSINE_THRESHOLD = 0.363
MAX_ENCODED_IMAGE_BYTES = 25 * 1024 * 1024
MAX_DECODED_IMAGE_PIXELS = 40_000_000


class FaceError(RuntimeError):
    """Base class for expected face-processing failures."""


class FaceDependencyError(FaceError):
    """Raised when an optional runtime dependency is missing or incompatible."""


class FaceInputError(FaceError):
    """Raised when an image cannot be decoded or has an unsupported shape."""


class FaceModelError(FaceError):
    """Raised when a model file is missing, unreadable, or cannot be loaded."""


class FaceDetectionError(FaceError):
    """Base class for errors produced while selecting a detected face."""


class NoFaceError(FaceDetectionError):
    """Raised when no face is detected."""


class MultipleFacesError(FaceDetectionError):
    """Raised when an input that requires one face contains several faces."""

    def __init__(self, count: int) -> None:
        self.count = count
        super().__init__(f"expected exactly one face, detected {count}")


class FaceQualityError(FaceDetectionError):
    """Raised when a detected face does not meet the configured quality policy."""

    def __init__(
        self,
        issues: Iterable[str],
        metrics: FaceQualityMetrics | None = None,
    ) -> None:
        self.issues = tuple(issues)
        self.metrics = metrics
        detail = ", ".join(self.issues) or "unspecified quality failure"
        super().__init__(f"face quality check failed: {detail}")


class InvalidEmbeddingError(FaceError):
    """Raised when an embedding is empty, non-finite, zero, or incompatible."""


@dataclass(frozen=True)
class BoundingBox:
    """A floating-point ``x, y, width, height`` face bounding box."""

    x: float
    y: float
    width: float
    height: float

    def __post_init__(self) -> None:
        values = (self.x, self.y, self.width, self.height)
        if not all(math.isfinite(float(value)) for value in values):
            raise ValueError("bounding-box values must be finite")
        if self.width <= 0 or self.height <= 0:
            raise ValueError("bounding-box width and height must be positive")

    @property
    def area(self) -> float:
        return float(self.width * self.height)

    def pixel_bounds(
        self,
        image_width: int,
        image_height: int,
    ) -> tuple[int, int, int, int]:
        """Return clipped integer ``left, top, right, bottom`` coordinates."""

        if image_width <= 0 or image_height <= 0:
            raise ValueError("image dimensions must be positive")
        left = max(0, min(image_width, math.floor(self.x)))
        top = max(0, min(image_height, math.floor(self.y)))
        right = max(0, min(image_width, math.ceil(self.x + self.width)))
        bottom = max(0, min(image_height, math.ceil(self.y + self.height)))
        return left, top, right, bottom

    def visible_fraction(self, image_width: int, image_height: int) -> float:
        """Return the fraction of the bounding box that lies inside the image."""

        left, top, right, bottom = self.pixel_bounds(image_width, image_height)
        visible_area = max(0, right - left) * max(0, bottom - top)
        return min(1.0, max(0.0, visible_area / self.area))


@dataclass(frozen=True)
class FaceDetection:
    """A YuNet-compatible detection with five facial landmarks."""

    box: BoundingBox
    landmarks: tuple[tuple[float, float], ...]
    confidence: float

    def __post_init__(self) -> None:
        if len(self.landmarks) != 5:
            raise ValueError("a YuNet detection must contain five landmarks")
        if not all(
            len(point) == 2 and math.isfinite(float(point[0])) and math.isfinite(float(point[1]))
            for point in self.landmarks
        ):
            raise ValueError("landmark coordinates must be finite x/y pairs")
        if not math.isfinite(float(self.confidence)) or not 0 <= self.confidence <= 1:
            raise ValueError("detection confidence must be between 0 and 1")

    @classmethod
    def from_yunet_row(cls, row: Sequence[float]) -> FaceDetection:
        """Build a detection from YuNet's 15-value output row."""

        if len(row) < 15:
            raise ValueError("a YuNet detection row must contain at least 15 values")
        try:
            values = tuple(float(value) for value in row[:15])
        except (TypeError, ValueError) as exc:
            raise ValueError("YuNet detection values must be numeric") from exc
        if not all(math.isfinite(value) for value in values):
            raise ValueError("YuNet detection values must be finite")
        landmarks = tuple((values[index], values[index + 1]) for index in range(4, 14, 2))
        return cls(
            box=BoundingBox(*values[:4]),
            landmarks=landmarks,
            confidence=values[14],
        )

    def as_yunet_row(self) -> tuple[float, ...]:
        """Return the row format accepted by ``FaceRecognizerSF.alignCrop``."""

        coordinates = tuple(value for point in self.landmarks for value in point)
        return (
            self.box.x,
            self.box.y,
            self.box.width,
            self.box.height,
            *coordinates,
            self.confidence,
        )


@dataclass(frozen=True)
class QualityPolicy:
    """Configurable checks applied before producing an input embedding."""

    min_confidence: float = 0.85
    min_face_size_px: int = 64
    min_face_area_ratio: float = 0.015
    max_face_area_ratio: float = 0.95
    min_visible_fraction: float = 0.95
    min_sharpness: float | None = 20.0
    min_brightness: float | None = 25.0
    max_brightness: float | None = 235.0

    def __post_init__(self) -> None:
        if not 0 <= self.min_confidence <= 1:
            raise ValueError("min_confidence must be between 0 and 1")
        if self.min_face_size_px <= 0:
            raise ValueError("min_face_size_px must be positive")
        if not 0 <= self.min_face_area_ratio <= self.max_face_area_ratio <= 1:
            raise ValueError("face area ratios must satisfy 0 <= min <= max <= 1")
        if not 0 <= self.min_visible_fraction <= 1:
            raise ValueError("min_visible_fraction must be between 0 and 1")
        if self.min_sharpness is not None and self.min_sharpness < 0:
            raise ValueError("min_sharpness cannot be negative")
        for name, value in (
            ("min_brightness", self.min_brightness),
            ("max_brightness", self.max_brightness),
        ):
            if value is not None and not 0 <= value <= 255:
                raise ValueError(f"{name} must be between 0 and 255")
        if (
            self.min_brightness is not None
            and self.max_brightness is not None
            and self.min_brightness > self.max_brightness
        ):
            raise ValueError("min_brightness cannot exceed max_brightness")


@dataclass(frozen=True)
class FaceQualityMetrics:
    """Measured input values used to make a reproducible quality decision."""

    confidence: float
    face_width_px: float
    face_height_px: float
    face_area_ratio: float
    visible_fraction: float
    sharpness: float | None = None
    brightness: float | None = None

    def __post_init__(self) -> None:
        numeric = (
            self.confidence,
            self.face_width_px,
            self.face_height_px,
            self.face_area_ratio,
            self.visible_fraction,
        )
        if not all(math.isfinite(float(value)) for value in numeric):
            raise ValueError("quality metrics must be finite")
        if self.face_width_px <= 0 or self.face_height_px <= 0:
            raise ValueError("measured face dimensions must be positive")
        if not 0 <= self.confidence <= 1:
            raise ValueError("confidence must be between 0 and 1")
        if self.face_area_ratio < 0:
            raise ValueError("face_area_ratio cannot be negative")
        if not 0 <= self.visible_fraction <= 1:
            raise ValueError("visible_fraction must be between 0 and 1")
        if self.sharpness is not None and (
            not math.isfinite(float(self.sharpness)) or self.sharpness < 0
        ):
            raise ValueError("sharpness must be finite and non-negative")
        if self.brightness is not None and (
            not math.isfinite(float(self.brightness)) or not 0 <= self.brightness <= 255
        ):
            raise ValueError("brightness must be finite and between 0 and 255")


@dataclass(frozen=True)
class ModelFingerprints:
    """SHA-256 provenance for the exact detector and recognizer model files."""

    detector_name: str
    detector_sha256: str
    recognizer_name: str
    recognizer_sha256: str

    def as_dict(self) -> dict[str, str]:
        return {
            "detector_name": self.detector_name,
            "detector_sha256": self.detector_sha256,
            "recognizer_name": self.recognizer_name,
            "recognizer_sha256": self.recognizer_sha256,
        }


@dataclass(frozen=True)
class FaceEncoding:
    """A normalized embedding plus the evidence needed to reproduce it."""

    embedding: tuple[float, ...]
    detection: FaceDetection
    quality: FaceQualityMetrics
    aligned_size: tuple[int, int]
    models: ModelFingerprints

    def __post_init__(self) -> None:
        object.__setattr__(self, "embedding", normalize_embedding(self.embedding))
        if len(self.aligned_size) != 2 or self.aligned_size[0] <= 0 or self.aligned_size[1] <= 0:
            raise ValueError("aligned_size must be a positive (width, height) pair")


@dataclass(frozen=True)
class FaceMatch:
    """A deterministic candidate-ranking result."""

    candidate_id: str
    similarity: float
    matched: bool
    rank: int


DEFAULT_QUALITY_POLICY = QualityPolicy()


def normalize_embedding(embedding: Iterable[float]) -> tuple[float, ...]:
    """L2-normalize an embedding without requiring NumPy."""

    try:
        values = tuple(float(value) for value in embedding)
    except (TypeError, ValueError) as exc:
        raise InvalidEmbeddingError("embedding values must be numeric") from exc
    if not values:
        raise InvalidEmbeddingError("embedding cannot be empty")
    if not all(math.isfinite(value) for value in values):
        raise InvalidEmbeddingError("embedding values must be finite")
    norm_squared = math.fsum(value * value for value in values)
    if norm_squared <= 1e-24:
        raise InvalidEmbeddingError("embedding norm is zero")
    norm = math.sqrt(norm_squared)
    return tuple(value / norm for value in values)


def cosine_similarity(left: Iterable[float], right: Iterable[float]) -> float:
    """Return cosine similarity in ``[-1, 1]`` for two embeddings."""

    normalized_left = normalize_embedding(left)
    normalized_right = normalize_embedding(right)
    if len(normalized_left) != len(normalized_right):
        raise InvalidEmbeddingError(
            f"embedding dimensions differ: {len(normalized_left)} != {len(normalized_right)}"
        )
    score = math.fsum(
        normalized_left[index] * normalized_right[index] for index in range(len(normalized_left))
    )
    # Floating point rounding can produce values a few ulps beyond the range.
    return max(-1.0, min(1.0, score))


def rank_candidates(
    query_embedding: Iterable[float],
    candidates: Mapping[str, Iterable[float]] | Iterable[tuple[str, Iterable[float]]],
    *,
    threshold: float = DEFAULT_COSINE_THRESHOLD,
) -> tuple[FaceMatch, ...]:
    """Score and deterministically rank candidate embeddings.

    ``candidate_id`` should be a durable identifier such as a result URL or
    evidence-record ID.  Duplicate IDs are rejected so that later audit records
    cannot ambiguously refer to two different embeddings.
    """

    if not math.isfinite(float(threshold)) or not -1 <= threshold <= 1:
        raise ValueError("threshold must be finite and between -1 and 1")
    normalized_query = normalize_embedding(query_embedding)
    items = candidates.items() if isinstance(candidates, Mapping) else candidates
    scores: list[tuple[str, float]] = []
    seen: set[str] = set()
    for raw_id, candidate_embedding in items:
        candidate_id = str(raw_id).strip()
        if not candidate_id:
            raise ValueError("candidate_id cannot be empty")
        if candidate_id in seen:
            raise ValueError(f"duplicate candidate_id: {candidate_id!r}")
        seen.add(candidate_id)
        try:
            normalized_candidate = normalize_embedding(candidate_embedding)
        except InvalidEmbeddingError as exc:
            raise InvalidEmbeddingError(
                f"candidate {candidate_id!r} has an invalid embedding: {exc}"
            ) from exc
        if len(normalized_query) != len(normalized_candidate):
            raise InvalidEmbeddingError(
                f"candidate {candidate_id!r} dimension differs: "
                f"{len(normalized_candidate)} != {len(normalized_query)}"
            )
        score = math.fsum(
            normalized_query[index] * normalized_candidate[index]
            for index in range(len(normalized_query))
        )
        scores.append((candidate_id, max(-1.0, min(1.0, score))))

    scores.sort(key=lambda item: (-item[1], item[0]))
    return tuple(
        FaceMatch(
            candidate_id=candidate_id,
            similarity=score,
            matched=score >= threshold,
            rank=rank,
        )
        for rank, (candidate_id, score) in enumerate(scores, start=1)
    )


def match_candidates(
    query_embedding: Iterable[float],
    candidates: Mapping[str, Iterable[float]] | Iterable[tuple[str, Iterable[float]]],
    *,
    threshold: float = DEFAULT_COSINE_THRESHOLD,
) -> tuple[FaceMatch, ...]:
    """Alias with an intent-revealing name for :func:`rank_candidates`."""

    return rank_candidates(query_embedding, candidates, threshold=threshold)


def best_match(
    query_embedding: Iterable[float],
    candidates: Mapping[str, Iterable[float]] | Iterable[tuple[str, Iterable[float]]],
    *,
    threshold: float = DEFAULT_COSINE_THRESHOLD,
) -> FaceMatch | None:
    """Return the best threshold-passing candidate, or ``None``."""

    ranked = rank_candidates(query_embedding, candidates, threshold=threshold)
    return ranked[0] if ranked and ranked[0].matched else None


def file_sha256(path: str | os.PathLike[str], *, chunk_size: int = 1024 * 1024) -> str:
    """Stream a file into SHA-256 and return its lowercase hexadecimal digest."""

    if not isinstance(chunk_size, int) or chunk_size <= 0:
        raise ValueError("chunk_size must be a positive integer")
    file_path = Path(path)
    if not file_path.is_file():
        raise FaceModelError(f"model file does not exist: {file_path}")
    digest = hashlib.sha256()
    try:
        with file_path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(chunk_size), b""):
                digest.update(chunk)
    except OSError as exc:
        raise FaceModelError(f"cannot read model file: {file_path}") from exc
    return digest.hexdigest()


def hash_model_files(
    detector_path: str | os.PathLike[str],
    recognizer_path: str | os.PathLike[str],
) -> ModelFingerprints:
    """Fingerprint the exact YuNet and SFace files used for an encoding."""

    detector = Path(detector_path)
    recognizer = Path(recognizer_path)
    return ModelFingerprints(
        detector_name=detector.name,
        detector_sha256=file_sha256(detector),
        recognizer_name=recognizer.name,
        recognizer_sha256=file_sha256(recognizer),
    )


def parse_yunet_detections(rows: Any) -> tuple[FaceDetection, ...]:
    """Convert NumPy-like or plain-list YuNet output into typed detections."""

    if rows is None:
        return ()
    raw_rows = rows.tolist() if hasattr(rows, "tolist") else rows
    try:
        materialized = list(raw_rows)
    except TypeError as exc:
        raise ValueError("YuNet detections must be an iterable of rows") from exc
    if not materialized:
        return ()
    # Also accept a single, flat 15-value row for convenient pure helper use.
    if isinstance(materialized[0], (int, float)):  # noqa: UP038 - Python 3.9 tests
        materialized = [materialized]
    return tuple(FaceDetection.from_yunet_row(row) for row in materialized)


def require_single_face(
    detections: Sequence[FaceDetection],
) -> FaceDetection:
    """Return exactly one detection or raise a precise selection error."""

    if not detections:
        raise NoFaceError("no face detected")
    if len(detections) != 1:
        raise MultipleFacesError(len(detections))
    return detections[0]


def quality_metrics_for_detection(
    detection: FaceDetection,
    *,
    image_size: tuple[int, int],
    sharpness: float | None = None,
    brightness: float | None = None,
) -> FaceQualityMetrics:
    """Construct reproducible quality metrics from geometry and measurements."""

    if len(image_size) != 2:
        raise ValueError("image_size must be a (width, height) pair")
    image_width, image_height = image_size
    if image_width <= 0 or image_height <= 0:
        raise ValueError("image dimensions must be positive")
    return FaceQualityMetrics(
        confidence=detection.confidence,
        face_width_px=detection.box.width,
        face_height_px=detection.box.height,
        face_area_ratio=detection.box.area / (image_width * image_height),
        visible_fraction=detection.box.visible_fraction(image_width, image_height),
        sharpness=sharpness,
        brightness=brightness,
    )


def quality_issues(
    metrics: FaceQualityMetrics,
    policy: QualityPolicy = DEFAULT_QUALITY_POLICY,
) -> tuple[str, ...]:
    """Return stable machine-readable issue codes for failed quality checks."""

    issues: list[str] = []
    if metrics.confidence < policy.min_confidence:
        issues.append("confidence_below_minimum")
    if min(metrics.face_width_px, metrics.face_height_px) < policy.min_face_size_px:
        issues.append("face_too_small")
    if metrics.face_area_ratio < policy.min_face_area_ratio:
        issues.append("face_area_too_small")
    if metrics.face_area_ratio > policy.max_face_area_ratio:
        issues.append("face_area_too_large")
    if metrics.visible_fraction < policy.min_visible_fraction:
        issues.append("face_clipped_by_frame")
    if policy.min_sharpness is not None:
        if metrics.sharpness is None:
            issues.append("sharpness_unavailable")
        elif metrics.sharpness < policy.min_sharpness:
            issues.append("face_too_blurry")
    if policy.min_brightness is not None:
        if metrics.brightness is None:
            issues.append("brightness_unavailable")
        elif metrics.brightness < policy.min_brightness:
            issues.append("face_too_dark")
    if (
        policy.max_brightness is not None
        and metrics.brightness is not None
        and metrics.brightness > policy.max_brightness
    ):
        issues.append("face_too_bright")
    return tuple(issues)


def require_quality(
    metrics: FaceQualityMetrics,
    policy: QualityPolicy = DEFAULT_QUALITY_POLICY,
) -> FaceQualityMetrics:
    """Return metrics if they pass, otherwise raise :class:`FaceQualityError`."""

    issues = quality_issues(metrics, policy)
    if issues:
        raise FaceQualityError(issues, metrics)
    return metrics


def validate_single_face(
    detections: Sequence[FaceDetection],
    *,
    image_size: tuple[int, int],
    sharpness: float | None,
    brightness: float | None,
    policy: QualityPolicy = DEFAULT_QUALITY_POLICY,
) -> tuple[FaceDetection, FaceQualityMetrics]:
    """Apply the one-face invariant and all configured quality checks."""

    detection = require_single_face(detections)
    metrics = quality_metrics_for_detection(
        detection,
        image_size=image_size,
        sharpness=sharpness,
        brightness=brightness,
    )
    return detection, require_quality(metrics, policy)


def _optional_import(module_name: str, install_hint: str) -> Any:
    try:
        return importlib.import_module(module_name)
    except (ImportError, ModuleNotFoundError) as exc:
        raise FaceDependencyError(
            f"optional dependency {module_name!r} is unavailable; {install_hint}"
        ) from exc


def _validate_encoded_image(source: Path | bytes) -> None:
    """Reject unsafe encoded dimensions before OpenCV allocates decoded pixels."""

    if isinstance(source, Path):
        if not source.is_file():
            raise FaceInputError(f"image file does not exist: {source}")
        try:
            byte_size = source.stat().st_size
        except OSError as exc:
            raise FaceInputError(f"image file cannot be inspected: {source}") from exc
        payload: Path | io.BytesIO = source
    else:
        byte_size = len(source)
        payload = io.BytesIO(source)
    if byte_size <= 0:
        raise FaceInputError("encoded image bytes cannot be empty")
    if byte_size > MAX_ENCODED_IMAGE_BYTES:
        raise FaceInputError(f"image exceeds {MAX_ENCODED_IMAGE_BYTES} encoded bytes")
    try:
        with Image.open(payload) as opened:
            width, height = opened.size
            if width <= 0 or height <= 0 or width * height > MAX_DECODED_IMAGE_PIXELS:
                raise FaceInputError(f"image exceeds {MAX_DECODED_IMAGE_PIXELS} decoded pixels")
            opened.verify()
    except FaceInputError:
        raise
    except (Image.DecompressionBombError, UnidentifiedImageError, OSError, ValueError) as exc:
        raise FaceInputError("image is not a safe, decodable image") from exc


class OpenCVFaceBackend:
    """Lazy OpenCV YuNet detector and SFace embedding backend.

    Construction performs no file access and imports neither OpenCV nor NumPy.
    This keeps command discovery, configuration validation, and pure helper tests
    usable in lightweight environments.  Call :meth:`load`, :meth:`detect`, or
    :meth:`encode_one` to initialize the models.
    """

    def __init__(
        self,
        detector_model_path: str | os.PathLike[str],
        recognizer_model_path: str | os.PathLike[str],
        *,
        quality_policy: QualityPolicy | None = None,
        detection_score_threshold: float = 0.60,
        nms_threshold: float = 0.30,
        top_k: int = 5_000,
    ) -> None:
        if not 0 <= detection_score_threshold <= 1:
            raise ValueError("detection_score_threshold must be between 0 and 1")
        if not 0 <= nms_threshold <= 1:
            raise ValueError("nms_threshold must be between 0 and 1")
        if top_k <= 0:
            raise ValueError("top_k must be positive")
        self.detector_model_path = Path(detector_model_path)
        self.recognizer_model_path = Path(recognizer_model_path)
        self.quality_policy = quality_policy or QualityPolicy()
        self.detection_score_threshold = float(detection_score_threshold)
        self.nms_threshold = float(nms_threshold)
        self.top_k = int(top_k)
        self._cv2: Any | None = None
        self._np: Any | None = None
        self._detector: Any | None = None
        self._recognizer: Any | None = None
        self._fingerprints: ModelFingerprints | None = None
        self._lock = threading.RLock()

    @property
    def is_loaded(self) -> bool:
        return self._detector is not None and self._recognizer is not None

    @property
    def model_fingerprints(self) -> ModelFingerprints:
        """Hash model files without forcing OpenCV to be installed."""

        with self._lock:
            if self._fingerprints is None:
                self._fingerprints = hash_model_files(
                    self.detector_model_path,
                    self.recognizer_model_path,
                )
            return self._fingerprints

    def load(self) -> OpenCVFaceBackend:
        """Load optional dependencies and both OpenCV networks once."""

        self._ensure_loaded()
        return self

    def _ensure_loaded(self) -> None:
        with self._lock:
            if self.is_loaded:
                return
            # Hash first: it gives a clearer error than an OpenCV assertion when
            # a model path is wrong and records exactly what is about to load.
            fingerprints = self.model_fingerprints
            cv2 = _optional_import("cv2", "install opencv-python-headless")
            np = _optional_import("numpy", "install numpy")

            detector_type = getattr(cv2, "FaceDetectorYN", None)
            detector_factory = getattr(detector_type, "create", None)
            if detector_factory is None:
                detector_factory = getattr(cv2, "FaceDetectorYN_create", None)
            recognizer_type = getattr(cv2, "FaceRecognizerSF", None)
            recognizer_factory = getattr(recognizer_type, "create", None)
            if recognizer_factory is None:
                recognizer_factory = getattr(cv2, "FaceRecognizerSF_create", None)
            if detector_factory is None or recognizer_factory is None:
                raise FaceDependencyError(
                    "this OpenCV build lacks FaceDetectorYN/FaceRecognizerSF; "
                    "install a current opencv-python-headless build"
                )

            try:
                detector = detector_factory(
                    str(self.detector_model_path),
                    "",
                    (320, 320),
                    self.detection_score_threshold,
                    self.nms_threshold,
                    self.top_k,
                )
                recognizer = recognizer_factory(
                    str(self.recognizer_model_path),
                    "",
                )
            except Exception as exc:  # OpenCV exposes backend-specific exceptions.
                raise FaceModelError(
                    "OpenCV could not load the YuNet/SFace model files "
                    f"({fingerprints.detector_name}, {fingerprints.recognizer_name})"
                ) from exc
            self._cv2 = cv2
            self._np = np
            self._detector = detector
            self._recognizer = recognizer

    def _coerce_image(self, image: Any) -> Any:
        if isinstance(image, (str, os.PathLike)):  # noqa: UP038 - Python 3.9 tests
            _validate_encoded_image(Path(image))
        elif isinstance(image, (bytes, bytearray, memoryview)):  # noqa: UP038
            _validate_encoded_image(bytes(image))

        self._ensure_loaded()
        cv2 = self._cv2
        np = self._np
        if cv2 is None or np is None:
            raise FaceModelError("OpenCV backend was not initialized")

        if isinstance(image, (str, os.PathLike)):  # noqa: UP038 - Python 3.9 tests
            path = Path(image)
            if not path.is_file():
                raise FaceInputError(f"image file does not exist: {path}")
            decoded = cv2.imread(str(path), cv2.IMREAD_COLOR)
            if decoded is None:
                raise FaceInputError(f"OpenCV could not decode image: {path}")
            image = decoded
        elif isinstance(  # noqa: UP038 - Python 3.9 tests
            image, (bytes, bytearray, memoryview)
        ):
            encoded = np.frombuffer(image, dtype=np.uint8)
            if encoded.size == 0:
                raise FaceInputError("encoded image bytes cannot be empty")
            decoded = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
            if decoded is None:
                raise FaceInputError("OpenCV could not decode image bytes")
            image = decoded

        if not hasattr(image, "shape") or not hasattr(image, "dtype"):
            raise FaceInputError("image must be a path, encoded bytes, or array")
        shape = tuple(int(value) for value in image.shape)
        if len(shape) not in (2, 3) or shape[0] <= 0 or shape[1] <= 0:
            raise FaceInputError(f"unsupported image shape: {shape}")
        if shape[0] * shape[1] > MAX_DECODED_IMAGE_PIXELS:
            raise FaceInputError(f"image exceeds {MAX_DECODED_IMAGE_PIXELS} decoded pixels")
        if image.dtype != np.uint8:
            raise FaceInputError("image arrays must use uint8 pixels")
        if len(shape) == 2:
            image = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
        elif shape[2] == 1:
            image = cv2.cvtColor(image[:, :, 0], cv2.COLOR_GRAY2BGR)
        elif shape[2] == 4:
            image = cv2.cvtColor(image, cv2.COLOR_BGRA2BGR)
        elif shape[2] != 3:
            raise FaceInputError(f"unsupported channel count: {shape[2]}")
        return np.ascontiguousarray(image)

    def _detect_prepared(self, image: Any) -> tuple[FaceDetection, ...]:
        if self._detector is None:
            raise FaceModelError("YuNet detector was not initialized")
        image_height, image_width = image.shape[:2]
        try:
            self._detector.setInputSize((int(image_width), int(image_height)))
            result = self._detector.detect(image)
        except Exception as exc:
            raise FaceDetectionError("YuNet face detection failed") from exc
        faces = result[1] if isinstance(result, tuple) and len(result) >= 2 else result
        return parse_yunet_detections(faces)

    def detect(self, image: Any) -> tuple[FaceDetection, ...]:
        """Detect all faces without applying the one-face or quality policy."""

        with self._lock:
            prepared = self._coerce_image(image)
            return self._detect_prepared(prepared)

    def _measure_quality(
        self,
        image: Any,
        detection: FaceDetection,
    ) -> FaceQualityMetrics:
        cv2 = self._cv2
        if cv2 is None:
            raise FaceModelError("OpenCV backend was not initialized")
        image_height, image_width = image.shape[:2]
        left, top, right, bottom = detection.box.pixel_bounds(int(image_width), int(image_height))
        if right <= left or bottom <= top:
            raise FaceQualityError(("face_outside_frame",))
        crop = image[top:bottom, left:right]
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        brightness = float(gray.mean())
        sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())
        return quality_metrics_for_detection(
            detection,
            image_size=(int(image_width), int(image_height)),
            sharpness=sharpness,
            brightness=brightness,
        )

    def _encode_detection(
        self,
        image: Any,
        detection: FaceDetection,
        metrics: FaceQualityMetrics,
    ) -> FaceEncoding:
        np = self._np
        recognizer = self._recognizer
        if np is None or recognizer is None:
            raise FaceModelError("SFace recognizer was not initialized")
        row = np.asarray(detection.as_yunet_row(), dtype=np.float32)
        try:
            aligned = recognizer.alignCrop(image, row)
            if aligned is None:
                raise ValueError("alignCrop returned no image")
            feature = recognizer.feature(aligned)
            if feature is None:
                raise ValueError("feature returned no embedding")
            embedding = normalize_embedding(feature.reshape(-1).tolist())
        except InvalidEmbeddingError:
            raise
        except Exception as exc:
            raise FaceDetectionError("SFace alignment or embedding failed") from exc
        aligned_height, aligned_width = aligned.shape[:2]
        return FaceEncoding(
            embedding=embedding,
            detection=detection,
            quality=metrics,
            aligned_size=(int(aligned_width), int(aligned_height)),
            models=self.model_fingerprints,
        )

    def encode_one(self, image: Any) -> FaceEncoding:
        """Require one good-quality face, align it, and return its embedding."""

        with self._lock:
            prepared = self._coerce_image(image)
            detections = self._detect_prepared(prepared)
            detection = require_single_face(detections)
            metrics = self._measure_quality(prepared, detection)
            require_quality(metrics, self.quality_policy)
            return self._encode_detection(prepared, detection, metrics)

    def encode(self, image: Any) -> FaceEncoding:
        """Convenience alias for :meth:`encode_one`."""

        return self.encode_one(image)

    def encode_faces(
        self,
        image: Any,
        *,
        skip_low_quality: bool = True,
    ) -> tuple[FaceEncoding, ...]:
        """Encode every usable face in a candidate image.

        Search-result images often contain several people, so candidate handling
        should compare the query against every detected face.  Poor detections are
        skipped by default.  If all detections fail quality checks, a
        :class:`FaceQualityError` is raised instead of silently returning no data.
        """

        with self._lock:
            prepared = self._coerce_image(image)
            detections = self._detect_prepared(prepared)
            if not detections:
                raise NoFaceError("no face detected")
            encodings: list[FaceEncoding] = []
            rejected: list[tuple[FaceQualityMetrics, tuple[str, ...]]] = []
            for detection in detections:
                metrics = self._measure_quality(prepared, detection)
                issues = quality_issues(metrics, self.quality_policy)
                if issues:
                    if not skip_low_quality:
                        raise FaceQualityError(issues, metrics)
                    rejected.append((metrics, issues))
                    continue
                encodings.append(self._encode_detection(prepared, detection, metrics))
            if not encodings:
                first_metrics, first_issues = rejected[0]
                raise FaceQualityError(first_issues, first_metrics)
            return tuple(encodings)

    @staticmethod
    def similarity(left: Iterable[float], right: Iterable[float]) -> float:
        return cosine_similarity(left, right)

    @staticmethod
    def match(
        query_embedding: Iterable[float],
        candidates: Mapping[str, Iterable[float]] | Iterable[tuple[str, Iterable[float]]],
        *,
        threshold: float = DEFAULT_COSINE_THRESHOLD,
    ) -> tuple[FaceMatch, ...]:
        return rank_candidates(query_embedding, candidates, threshold=threshold)


__all__ = [
    "DEFAULT_COSINE_THRESHOLD",
    "DEFAULT_QUALITY_POLICY",
    "BoundingBox",
    "FaceDependencyError",
    "FaceDetection",
    "FaceDetectionError",
    "FaceEncoding",
    "FaceError",
    "FaceInputError",
    "FaceMatch",
    "FaceModelError",
    "FaceQualityError",
    "FaceQualityMetrics",
    "InvalidEmbeddingError",
    "ModelFingerprints",
    "MultipleFacesError",
    "NoFaceError",
    "OpenCVFaceBackend",
    "QualityPolicy",
    "best_match",
    "cosine_similarity",
    "file_sha256",
    "hash_model_files",
    "match_candidates",
    "normalize_embedding",
    "parse_yunet_detections",
    "quality_issues",
    "quality_metrics_for_detection",
    "rank_candidates",
    "require_quality",
    "require_single_face",
    "validate_single_face",
]
