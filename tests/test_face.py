from __future__ import annotations

import hashlib
import importlib
import math
import struct
import sys
from pathlib import Path

import pytest

# Keep these tests runnable before packaging metadata is added to the repository.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from faceproof.face import (  # noqa: E402
    BoundingBox,
    FaceDependencyError,
    FaceDetection,
    FaceInputError,
    FaceModelError,
    FaceQualityError,
    InvalidEmbeddingError,
    MultipleFacesError,
    NoFaceError,
    OpenCVFaceBackend,
    QualityPolicy,
    best_match,
    cosine_similarity,
    file_sha256,
    hash_model_files,
    normalize_embedding,
    parse_yunet_detections,
    quality_issues,
    quality_metrics_for_detection,
    rank_candidates,
    require_single_face,
    validate_single_face,
)


def yunet_row(
    *,
    x: float = 100,
    y: float = 50,
    width: float = 100,
    height: float = 100,
    confidence: float = 0.95,
) -> list[float]:
    return [
        x,
        y,
        width,
        height,
        130,
        80,
        170,
        80,
        150,
        100,
        135,
        125,
        165,
        125,
        confidence,
    ]


def test_parse_yunet_detection_round_trip() -> None:
    detection = parse_yunet_detections(yunet_row())[0]

    assert detection.box == BoundingBox(100, 50, 100, 100)
    assert detection.landmarks[2] == (150, 100)
    assert detection.confidence == pytest.approx(0.95)
    assert detection.as_yunet_row() == pytest.approx(tuple(yunet_row()))


def test_parse_none_and_multiple_rows() -> None:
    assert parse_yunet_detections(None) == ()
    rows = [yunet_row(x=10), yunet_row(x=220)]
    assert len(parse_yunet_detections(rows)) == 2


def test_backend_rejects_decompression_bomb_before_loading_opencv(tmp_path: Path) -> None:
    bomb = tmp_path / "bomb.bmp"
    bomb.write_bytes(
        b"BM"
        + struct.pack("<IHHI", 54, 0, 0, 54)
        + struct.pack(
            "<IiiHHIIiiII",
            40,
            100_000,
            100_000,
            1,
            24,
            0,
            0,
            0,
            0,
            0,
            0,
        )
    )
    backend = OpenCVFaceBackend(tmp_path / "missing-yunet.onnx", tmp_path / "missing-sface.onnx")

    with pytest.raises(FaceInputError, match="decoded pixels|safe, decodable"):
        backend.encode_one(bomb)


def test_invalid_yunet_row_is_rejected() -> None:
    with pytest.raises(ValueError, match="at least 15"):
        FaceDetection.from_yunet_row([0.0] * 14)
    with pytest.raises(ValueError, match="positive"):
        FaceDetection.from_yunet_row(yunet_row(width=0))


def test_require_single_face_has_specific_errors() -> None:
    first, second = parse_yunet_detections([yunet_row(), yunet_row(x=250)])

    with pytest.raises(NoFaceError):
        require_single_face(())
    with pytest.raises(MultipleFacesError) as exc_info:
        require_single_face((first, second))
    assert exc_info.value.count == 2
    assert require_single_face((first,)) is first


def test_quality_metrics_pass_default_policy() -> None:
    detection = parse_yunet_detections(yunet_row())[0]
    metrics = quality_metrics_for_detection(
        detection,
        image_size=(400, 300),
        sharpness=80,
        brightness=120,
    )

    assert metrics.face_area_ratio == pytest.approx(1 / 12)
    assert metrics.visible_fraction == pytest.approx(1.0)
    assert quality_issues(metrics) == ()
    selected, selected_metrics = validate_single_face(
        (detection,),
        image_size=(400, 300),
        sharpness=80,
        brightness=120,
    )
    assert selected is detection
    assert selected_metrics is metrics or selected_metrics == metrics


def test_quality_reports_all_relevant_failures() -> None:
    detection = parse_yunet_detections(
        yunet_row(x=-30, y=-20, width=50, height=50, confidence=0.5)
    )[0]
    metrics = quality_metrics_for_detection(
        detection,
        image_size=(1000, 1000),
        sharpness=2,
        brightness=5,
    )

    issues = quality_issues(metrics)
    assert {
        "confidence_below_minimum",
        "face_too_small",
        "face_area_too_small",
        "face_clipped_by_frame",
        "face_too_blurry",
        "face_too_dark",
    }.issubset(issues)
    with pytest.raises(FaceQualityError) as exc_info:
        validate_single_face(
            (detection,),
            image_size=(1000, 1000),
            sharpness=2,
            brightness=5,
        )
    assert exc_info.value.metrics == metrics
    assert "face_too_blurry" in exc_info.value.issues


def test_quality_policy_can_disable_photometric_checks() -> None:
    policy = QualityPolicy(
        min_sharpness=None,
        min_brightness=None,
        max_brightness=None,
    )
    detection = parse_yunet_detections(yunet_row())[0]
    metrics = quality_metrics_for_detection(detection, image_size=(400, 300))
    assert quality_issues(metrics, policy) == ()


def test_normalize_and_cosine_similarity_are_pure() -> None:
    normalized = normalize_embedding([3, 4])
    assert normalized == pytest.approx((0.6, 0.8))
    assert math.fsum(value * value for value in normalized) == pytest.approx(1.0)
    assert cosine_similarity([1, 0], [10, 0]) == pytest.approx(1.0)
    assert cosine_similarity([1, 0], [0, 1]) == pytest.approx(0.0)
    assert cosine_similarity([1, 0], [-1, 0]) == pytest.approx(-1.0)


@pytest.mark.parametrize(
    "embedding",
    [[], [0, 0], [float("nan"), 1], [float("inf"), 1]],
)
def test_invalid_embeddings_are_rejected(embedding: list[float]) -> None:
    with pytest.raises(InvalidEmbeddingError):
        normalize_embedding(embedding)


def test_cosine_rejects_dimension_mismatch() -> None:
    with pytest.raises(InvalidEmbeddingError, match="dimensions differ"):
        cosine_similarity([1, 2], [1, 2, 3])


def test_candidates_are_ranked_deterministically_and_thresholded() -> None:
    ranked = rank_candidates(
        [1, 0],
        {
            "same": [2, 0],
            "near": [0.8, 0.6],
            "other": [0, 1],
        },
        threshold=0.75,
    )

    assert [match.candidate_id for match in ranked] == ["same", "near", "other"]
    assert [match.rank for match in ranked] == [1, 2, 3]
    assert [match.matched for match in ranked] == [True, True, False]
    assert ranked[1].similarity == pytest.approx(0.8)
    assert best_match([1, 0], {"different": [0, 1]}, threshold=0.75) is None


def test_candidate_ties_sort_by_id_and_duplicates_are_rejected() -> None:
    ranked = rank_candidates([1, 0], [("z", [1, 0]), ("a", [1, 0])])
    assert [match.candidate_id for match in ranked] == ["a", "z"]
    with pytest.raises(ValueError, match="duplicate"):
        rank_candidates([1, 0], [("same", [1, 0]), ("same", [1, 0])])


def test_file_sha256_streams_known_digest(tmp_path: Path) -> None:
    model = tmp_path / "model.onnx"
    model.write_bytes(b"abc")

    assert file_sha256(model, chunk_size=1) == hashlib.sha256(b"abc").hexdigest()
    with pytest.raises(FaceModelError, match="does not exist"):
        file_sha256(tmp_path / "missing.onnx")


def test_model_fingerprints_name_and_hash_both_files(tmp_path: Path) -> None:
    detector = tmp_path / "yunet.onnx"
    recognizer = tmp_path / "sface.onnx"
    detector.write_bytes(b"detector")
    recognizer.write_bytes(b"recognizer")

    fingerprints = hash_model_files(detector, recognizer)

    assert fingerprints.detector_name == "yunet.onnx"
    assert fingerprints.recognizer_name == "sface.onnx"
    assert fingerprints.detector_sha256 == hashlib.sha256(b"detector").hexdigest()
    assert fingerprints.recognizer_sha256 == hashlib.sha256(b"recognizer").hexdigest()
    assert fingerprints.as_dict()["detector_sha256"] == fingerprints.detector_sha256


def test_backend_construction_and_hashing_do_not_import_opencv(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    detector = tmp_path / "yunet.onnx"
    recognizer = tmp_path / "sface.onnx"
    detector.write_bytes(b"detector")
    recognizer.write_bytes(b"recognizer")
    imported: list[str] = []
    real_import = importlib.import_module

    def tracking_import(name: str, package: str | None = None):
        imported.append(name)
        return real_import(name, package)

    monkeypatch.setattr("faceproof.face.importlib.import_module", tracking_import)
    backend = OpenCVFaceBackend(detector, recognizer)

    assert not backend.is_loaded
    assert backend.model_fingerprints.detector_name == "yunet.onnx"
    assert imported == []


def test_backend_has_friendly_error_when_opencv_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    detector = tmp_path / "yunet.onnx"
    recognizer = tmp_path / "sface.onnx"
    detector.write_bytes(b"detector")
    recognizer.write_bytes(b"recognizer")

    def missing_import(name: str, package: str | None = None):
        del package
        raise ModuleNotFoundError(name)

    monkeypatch.setattr("faceproof.face.importlib.import_module", missing_import)
    backend = OpenCVFaceBackend(detector, recognizer)

    with pytest.raises(FaceDependencyError, match="opencv-python-headless"):
        backend.load()


def test_backend_encode_faces_permissive_empty_when_no_detections(tmp_path: Path) -> None:
    detector = tmp_path / "yunet.onnx"
    recognizer = tmp_path / "sface.onnx"
    detector.write_bytes(b"detector")
    recognizer.write_bytes(b"recognizer")
    backend = OpenCVFaceBackend(detector, recognizer)
    backend._ensure_loaded = lambda: None
    backend._coerce_image = lambda img: img
    backend._detect_prepared = lambda img: ()
    assert backend.encode_faces_permissive(b"img") == ()


def test_backend_encode_primary_face_raises_when_no_face(tmp_path: Path) -> None:
    detector = tmp_path / "yunet.onnx"
    recognizer = tmp_path / "sface.onnx"
    detector.write_bytes(b"detector")
    recognizer.write_bytes(b"recognizer")
    backend = OpenCVFaceBackend(detector, recognizer)
    backend._ensure_loaded = lambda: None
    backend._coerce_image = lambda img: img
    backend._detect_prepared = lambda img: ()
    with pytest.raises(NoFaceError, match="no face detected"):
        backend.encode_primary_face(b"img")
