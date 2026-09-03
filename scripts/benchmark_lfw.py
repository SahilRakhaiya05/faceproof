"""Reproduce an aggregate LFW verification benchmark for FaceProof.

This runner evaluates the exact pinned YuNet/SFace path used by FaceProof on
the canonical LFW View 2 ``pairs.txt`` protocol. It deliberately keeps images
and embeddings local and emits aggregate statistics only.

The original UMass download host is retained in the provenance metadata. The
download command uses scikit-learn's SHA-256-pinned Figshare mirror because the
original host is not reliably available.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import sys
import tarfile
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urljoin

import httpx
import numpy as np

import faceproof.face as face_module
from faceproof import __version__
from faceproof.face import (
    DEFAULT_COSINE_THRESHOLD,
    DEFAULT_QUALITY_POLICY,
    FaceError,
    FaceQualityError,
    OpenCVFaceBackend,
    cosine_similarity,
)
from faceproof.model_assets import DEFAULT_MODELS

LFW_ORIGINAL_PAGE = "http://vis-www.cs.umass.edu/lfw/"
LFW_FUNNELED_ORIGINAL_URL = "http://vis-www.cs.umass.edu/lfw/lfw-funneled.tgz"
LFW_PAIRS_ORIGINAL_URL = "http://vis-www.cs.umass.edu/lfw/pairs.txt"

# These URLs and digests are the values pinned by scikit-learn's LFW loader.
LFW_FUNNELED_MIRROR_URL = "https://ndownloader.figshare.com/files/5976015"
LFW_FUNNELED_SHA256 = "b47c8422c8cded889dc5a13418c4bc2abbda121092b3533a83306f90d900100a"
LFW_FUNNELED_BYTES = 243_346_528
# Derived once from the verified archive using dataset_tree_fingerprint's
# length-prefixed relative-path, byte-size, and content framing.
LFW_FUNNELED_TREE_SHA256 = "61fda5733c31c83a4c1605759851714c25ad9f1b2f1503bb044b07eb663253d7"
LFW_FUNNELED_IMAGE_BYTES = 250_937_116
LFW_PAIRS_MIRROR_URL = "https://ndownloader.figshare.com/files/5976006"
LFW_PAIRS_SHA256 = "ea42330c62c92989f9d7c03237ed5d591365e89b3e649747777b70e692dc1592"
LFW_PAIRS_BYTES = 155_335
LFW_IMAGE_COUNT = 13_233
LFW_PAIR_COUNT = 6_000
LFW_FOLD_COUNT = 10


class BenchmarkError(RuntimeError):
    """Raised when benchmark inputs or results violate the declared protocol."""


@dataclass(frozen=True, slots=True)
class LfwPair:
    fold: int
    left: Path
    right: Path
    same_identity: bool


@dataclass(frozen=True, slots=True)
class PairScore:
    fold: int
    same_identity: bool
    cosine_similarity: float


@dataclass(frozen=True, slots=True)
class DatasetAttestation:
    acquisition_mode: str
    archive_sha256: str | None
    archive_integrity_verified: bool
    extraction_marker_verified: bool
    dataset_tree_sha256: str
    image_count: int
    image_bytes: int


@dataclass(frozen=True, slots=True)
class Confusion:
    true_positive: int
    true_negative: int
    false_positive: int
    false_negative: int

    @property
    def total(self) -> int:
        return self.true_positive + self.true_negative + self.false_positive + self.false_negative

    @property
    def correct(self) -> int:
        return self.true_positive + self.true_negative

    def __add__(self, other: Confusion) -> Confusion:
        return Confusion(
            true_positive=self.true_positive + other.true_positive,
            true_negative=self.true_negative + other.true_negative,
            false_positive=self.false_positive + other.false_positive,
            false_negative=self.false_negative + other.false_negative,
        )


def file_sha256(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download_verified(
    *,
    url: str,
    destination: Path,
    expected_sha256: str,
    expected_bytes: int,
    force: bool,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file() and not force:
        size_matches = destination.stat().st_size == expected_bytes
        digest_matches = size_matches and file_sha256(destination) == expected_sha256
        if not digest_matches:
            raise BenchmarkError(
                f"existing download failed integrity validation: {destination}; "
                "use --force-download to replace it"
            )
        return

    partial = destination.with_name(f"{destination.name}.part")
    if force:
        partial.unlink(missing_ok=True)
    timeout = httpx.Timeout(120.0, connect=30.0)
    range_download = expected_bytes > 8 * 1024 * 1024
    try:
        if range_download:
            _download_verified_ranges(
                url=url,
                destination=partial,
                expected_bytes=expected_bytes,
            )
        else:
            partial.unlink(missing_ok=True)
            with (
                httpx.Client(follow_redirects=True, timeout=timeout) as client,
                client.stream("GET", url) as response,
                partial.open("wb") as output,
            ):
                response.raise_for_status()
                for chunk in response.iter_bytes(chunk_size=1024 * 1024):
                    output.write(chunk)
                    if output.tell() > expected_bytes:
                        raise BenchmarkError(f"download exceeded pinned size: {destination.name}")
    except Exception:
        # Range downloads are an append-only, chunk-aligned verified prefix and
        # can safely resume. A failed small streaming response is not resumable.
        if not range_download:
            partial.unlink(missing_ok=True)
        raise
    byte_count = partial.stat().st_size
    if byte_count != expected_bytes:
        partial.unlink(missing_ok=True)
        raise BenchmarkError(
            f"download size mismatch for {destination.name}: {byte_count} != {expected_bytes}"
        )
    actual_sha256 = file_sha256(partial)
    if actual_sha256 != expected_sha256:
        partial.unlink(missing_ok=True)
        raise BenchmarkError(f"download SHA-256 mismatch for {destination.name}: {actual_sha256}")
    os.replace(partial, destination)


def _download_verified_ranges(
    *,
    url: str,
    destination: Path,
    expected_bytes: int,
    chunk_size: int = 4 * 1024 * 1024,
    workers: int = 8,
) -> None:
    """Fetch a large immutable file in bounded, validated byte ranges."""

    completed_bytes = destination.stat().st_size if destination.is_file() else 0
    if completed_bytes > expected_bytes or completed_bytes % chunk_size:
        raise BenchmarkError(
            "partial range download has an invalid size; use --force-download to replace it"
        )
    ranges = [
        (start, min(start + chunk_size, expected_bytes) - 1)
        for start in range(completed_bytes, expected_bytes, chunk_size)
    ]
    if not ranges:
        return
    with (
        ThreadPoolExecutor(max_workers=workers) as executor,
        destination.open("ab") as output,
    ):
        for batch_start in range(0, len(ranges), workers):
            batch = ranges[batch_start : batch_start + workers]
            futures = [
                executor.submit(
                    _fetch_validated_range,
                    url=url,
                    start=start,
                    end=end,
                    expected_bytes=expected_bytes,
                )
                for start, end in batch
            ]
            for future in futures:
                output.write(future.result())
            output.flush()
            completed = min(batch_start + len(batch), len(ranges))
            downloaded = min(completed_bytes + completed * chunk_size, expected_bytes)
            print(
                f"downloaded {downloaded / (1024 * 1024):.1f}/"
                f"{expected_bytes / (1024 * 1024):.1f} MiB",
                file=sys.stderr,
                flush=True,
            )


def _fetch_validated_range(
    *,
    url: str,
    start: int,
    end: int,
    expected_bytes: int,
    attempts: int = 8,
) -> bytes:
    expected_content_range = f"bytes {start}-{end}/{expected_bytes}"
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            # Figshare currently issues S3 URLs that expire after ten seconds.
            # Resolve a fresh URL per range and consume it immediately, without
            # letting redirected requests wait behind a shared connection pool.
            timeout = httpx.Timeout(30.0, connect=8.0)
            with httpx.Client(follow_redirects=False, timeout=timeout) as client:
                redirect = client.get(url)
                if redirect.status_code not in {301, 302, 303, 307, 308}:
                    redirect.raise_for_status()
                    raise BenchmarkError("Figshare did not return a signed download redirect")
                location = redirect.headers.get("location")
                if not location:
                    raise BenchmarkError("Figshare redirect did not contain a Location header")
                signed_url = urljoin(url, location)
                response = client.get(
                    signed_url,
                    headers={"Range": f"bytes={start}-{end}"},
                )
            if response.status_code >= 400:
                raise BenchmarkError(f"signed range request returned HTTP {response.status_code}")
            if response.status_code != 206:
                raise BenchmarkError(
                    f"range server returned HTTP {response.status_code} instead of 206"
                )
            if response.headers.get("content-range") != expected_content_range:
                raise BenchmarkError("range server returned an unexpected Content-Range")
            expected_length = end - start + 1
            if len(response.content) != expected_length:
                raise BenchmarkError(
                    f"range length mismatch: {len(response.content)} != {expected_length}"
                )
            return response.content
        except (BenchmarkError, httpx.HTTPError) as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(0.25 * (attempt + 1))
    if last_error is None:
        raise BenchmarkError("range download failed without a captured error")
    raise BenchmarkError(f"range download failed after {attempts} attempts: {last_error}")


def _safe_extract_tar(archive_path: Path, destination: Path) -> None:
    """Extract regular files/directories while rejecting archive path tricks."""

    destination.mkdir(parents=True, exist_ok=True)
    destination_root = destination.resolve()
    with tarfile.open(archive_path, mode="r:gz") as archive:
        for member in archive:
            member_path = PurePosixPath(member.name)
            if member_path.is_absolute() or ".." in member_path.parts:
                raise BenchmarkError(f"unsafe archive member: {member.name!r}")
            target = destination.joinpath(*member_path.parts).resolve()
            if not target.is_relative_to(destination_root):
                raise BenchmarkError(f"archive member escaped destination: {member.name!r}")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                raise BenchmarkError(f"unsupported archive member type: {member.name!r}")
            source = archive.extractfile(member)
            if source is None:
                raise BenchmarkError(f"could not read archive member: {member.name!r}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with source, target.open("wb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)


def dataset_tree_fingerprint(lfw_root: Path) -> tuple[str, int, int]:
    """Hash every relative JPEG path, size, and byte sequence in the dataset."""

    root = Path(lfw_root).resolve()
    images = sorted(root.rglob("*.jpg"), key=lambda path: path.relative_to(root).as_posix())
    digest = hashlib.sha256()
    byte_count = 0
    for image_path in images:
        relative = image_path.relative_to(root).as_posix().encode("utf-8")
        size = image_path.stat().st_size
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(size.to_bytes(8, "big"))
        with image_path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        byte_count += size
    return digest.hexdigest(), len(images), byte_count


def _remove_cached_dataset(dataset_root: Path, cache_dir: Path) -> None:
    resolved_root = dataset_root.resolve()
    resolved_cache = cache_dir.resolve()
    if resolved_root.parent != resolved_cache or resolved_root.name != "lfw_funneled":
        raise BenchmarkError("refusing to replace a dataset outside the dedicated cache")
    if resolved_root.is_dir():
        shutil.rmtree(resolved_root)


def acquire_official_lfw(
    cache_dir: Path,
    *,
    force_download: bool = False,
) -> tuple[Path, Path, DatasetAttestation]:
    """Download, verify, cleanly extract, and re-attest pinned funneled LFW."""

    cache_dir = Path(cache_dir)
    archive_path = cache_dir / "lfw-funneled.tgz"
    pairs_path = cache_dir / "pairs.txt"
    dataset_root = cache_dir / "lfw_funneled"
    extraction_marker = cache_dir / ".lfw_funneled.complete.json"
    legacy_marker = cache_dir / ".lfw_funneled.complete"

    _download_verified(
        url=LFW_FUNNELED_MIRROR_URL,
        destination=archive_path,
        expected_sha256=LFW_FUNNELED_SHA256,
        expected_bytes=LFW_FUNNELED_BYTES,
        force=force_download,
    )
    _download_verified(
        url=LFW_PAIRS_MIRROR_URL,
        destination=pairs_path,
        expected_sha256=LFW_PAIRS_SHA256,
        expected_bytes=LFW_PAIRS_BYTES,
        force=force_download,
    )

    marker: dict[str, Any] = {}
    if extraction_marker.is_file():
        try:
            loaded_marker = json.loads(extraction_marker.read_text(encoding="utf-8"))
            if isinstance(loaded_marker, dict):
                marker = loaded_marker
        except (OSError, ValueError):
            marker = {}

    marker_ok = False
    tree_sha256 = ""
    image_count = image_bytes = 0
    if dataset_root.is_dir() and marker:
        tree_sha256, image_count, image_bytes = dataset_tree_fingerprint(dataset_root)
        marker_ok = (
            marker.get("archive_sha256") == LFW_FUNNELED_SHA256
            and marker.get("dataset_tree_sha256") == tree_sha256 == LFW_FUNNELED_TREE_SHA256
            and marker.get("image_count") == image_count == LFW_IMAGE_COUNT
            and marker.get("image_bytes") == image_bytes == LFW_FUNNELED_IMAGE_BYTES
        )
    if not marker_ok:
        _remove_cached_dataset(dataset_root, cache_dir)
        extraction_marker.unlink(missing_ok=True)
        legacy_marker.unlink(missing_ok=True)
        _safe_extract_tar(archive_path, cache_dir)
        tree_sha256, image_count, image_bytes = dataset_tree_fingerprint(dataset_root)
        if image_count != LFW_IMAGE_COUNT:
            raise BenchmarkError(
                f"extracted LFW image count mismatch: {image_count} != {LFW_IMAGE_COUNT}"
            )
        if tree_sha256 != LFW_FUNNELED_TREE_SHA256 or image_bytes != LFW_FUNNELED_IMAGE_BYTES:
            raise BenchmarkError("clean extraction did not match the pinned LFW dataset tree")
        marker = {
            "archive_sha256": LFW_FUNNELED_SHA256,
            "dataset_tree_sha256": tree_sha256,
            "image_count": image_count,
            "image_bytes": image_bytes,
        }
        extraction_marker.write_text(
            json.dumps(marker, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        marker_ok = True
    attestation = DatasetAttestation(
        acquisition_mode="sha256-pinned-mirror-clean-extraction",
        archive_sha256=LFW_FUNNELED_SHA256,
        archive_integrity_verified=True,
        extraction_marker_verified=marker_ok,
        dataset_tree_sha256=tree_sha256,
        image_count=image_count,
        image_bytes=image_bytes,
    )
    return dataset_root, pairs_path, attestation


def _lfw_image_path(root: Path, name: str, index: str) -> Path:
    if not name or any(character in name for character in ("/", "\\", "\0")):
        raise BenchmarkError(f"invalid LFW identity field: {name!r}")
    try:
        numeric_index = int(index)
    except ValueError as exc:
        raise BenchmarkError(f"invalid LFW image index: {index!r}") from exc
    if numeric_index <= 0:
        raise BenchmarkError(f"LFW image index must be positive: {numeric_index}")
    return root / name / f"{name}_{numeric_index:04d}.jpg"


def parse_lfw_pairs(pairs_path: Path, lfw_root: Path) -> list[LfwPair]:
    """Parse canonical LFW View 2 pairs while preserving the declared folds."""

    lines = [line.strip() for line in Path(pairs_path).read_text(encoding="utf-8").splitlines()]
    lines = [line for line in lines if line]
    if not lines:
        raise BenchmarkError("LFW pairs file is empty")
    header = lines[0].split()
    if len(header) != 2:
        raise BenchmarkError("expected a two-field LFW View 2 pairs header")
    try:
        fold_count, pairs_per_class = (int(value) for value in header)
    except ValueError as exc:
        raise BenchmarkError("LFW pairs header must contain integers") from exc
    if fold_count <= 1 or pairs_per_class <= 0:
        raise BenchmarkError("LFW pairs header contains invalid counts")
    expected_pair_count = fold_count * pairs_per_class * 2
    specifications = lines[1:]
    if len(specifications) != expected_pair_count:
        raise BenchmarkError(
            f"pairs file count mismatch: {len(specifications)} != {expected_pair_count}"
        )

    pairs: list[LfwPair] = []
    per_fold = pairs_per_class * 2
    for ordinal, line in enumerate(specifications):
        fields = line.split()
        if len(fields) == 3:
            name, left_index, right_index = fields
            left = _lfw_image_path(lfw_root, name, left_index)
            right = _lfw_image_path(lfw_root, name, right_index)
            same_identity = True
        elif len(fields) == 4:
            left_name, left_index, right_name, right_index = fields
            left = _lfw_image_path(lfw_root, left_name, left_index)
            right = _lfw_image_path(lfw_root, right_name, right_index)
            same_identity = False
        else:
            raise BenchmarkError(f"invalid LFW pair at data line {ordinal + 2}")
        pairs.append(
            LfwPair(
                fold=ordinal // per_fold,
                left=left,
                right=right,
                same_identity=same_identity,
            )
        )

    for fold in range(fold_count):
        fold_pairs = [pair for pair in pairs if pair.fold == fold]
        positives = sum(pair.same_identity for pair in fold_pairs)
        negatives = len(fold_pairs) - positives
        if positives != pairs_per_class or negatives != pairs_per_class:
            raise BenchmarkError(
                f"fold {fold + 1} is not balanced: {positives} same, {negatives} different"
            )
    return pairs


def validate_pair_images(pairs: list[LfwPair]) -> None:
    unique_paths = {item for pair in pairs for item in (pair.left, pair.right)}
    missing = sum(1 for path in unique_paths if not path.is_file())
    if missing:
        raise BenchmarkError(f"{missing} image files referenced by pairs.txt are missing")


def wilson_interval(
    successes: int,
    total: int,
    *,
    z: float = 1.959963984540054,
) -> tuple[float, float]:
    """Return a two-sided 95% Wilson score interval for a binomial rate."""

    if total <= 0:
        raise ValueError("total must be positive")
    if not 0 <= successes <= total:
        raise ValueError("successes must be between zero and total")
    proportion = successes / total
    denominator = 1 + (z * z / total)
    center = (proportion + z * z / (2 * total)) / denominator
    half_width = (
        z
        * math.sqrt(proportion * (1 - proportion) / total + z * z / (4 * total * total))
        / denominator
    )
    return max(0.0, center - half_width), min(1.0, center + half_width)


def _rate(successes: int, total: int) -> dict[str, Any]:
    if total == 0:
        return {"count": successes, "denominator": 0, "rate": None, "ci95_wilson": None}
    lower, upper = wilson_interval(successes, total)
    return {
        "count": successes,
        "denominator": total,
        "rate": successes / total,
        "ci95_wilson": [lower, upper],
    }


def confusion_at_threshold(scores: list[PairScore], threshold: float) -> Confusion:
    true_positive = true_negative = false_positive = false_negative = 0
    for item in scores:
        predicted_same = item.cosine_similarity >= threshold
        if item.same_identity and predicted_same:
            true_positive += 1
        elif item.same_identity:
            false_negative += 1
        elif predicted_same:
            false_positive += 1
        else:
            true_negative += 1
    return Confusion(true_positive, true_negative, false_positive, false_negative)


def metrics_at_threshold(scores: list[PairScore], threshold: float) -> dict[str, Any]:
    confusion = confusion_at_threshold(scores, threshold)
    positive_count = confusion.true_positive + confusion.false_negative
    negative_count = confusion.true_negative + confusion.false_positive
    return {
        "threshold": threshold,
        "confusion": asdict(confusion),
        "accuracy": _rate(confusion.correct, confusion.total),
        "false_accept_rate": _rate(confusion.false_positive, negative_count),
        "false_reject_rate": _rate(confusion.false_negative, positive_count),
    }


def best_accuracy_threshold(scores: list[PairScore]) -> tuple[float, Confusion]:
    """Find the pooled post-hoc threshold, breaking ties toward fewer accepts."""

    if not scores:
        raise ValueError("at least one score is required")
    ordered = sorted(scores, key=lambda item: item.cosine_similarity, reverse=True)
    positive_count = sum(item.same_identity for item in ordered)
    negative_count = len(ordered) - positive_count
    true_positive = false_positive = 0
    best_threshold = math.nextafter(ordered[0].cosine_similarity, math.inf)
    best_confusion = Confusion(0, negative_count, 0, positive_count)
    best_correct = best_confusion.correct

    cursor = 0
    while cursor < len(ordered):
        score = ordered[cursor].cosine_similarity
        while cursor < len(ordered) and ordered[cursor].cosine_similarity == score:
            if ordered[cursor].same_identity:
                true_positive += 1
            else:
                false_positive += 1
            cursor += 1
        candidate = Confusion(
            true_positive=true_positive,
            true_negative=negative_count - false_positive,
            false_positive=false_positive,
            false_negative=positive_count - true_positive,
        )
        if candidate.correct > best_correct:
            best_correct = candidate.correct
            best_threshold = score
            best_confusion = candidate
    return best_threshold, best_confusion


def roc_auc(scores: list[PairScore]) -> float:
    """Compute ROC-AUC as the Mann-Whitney probability with half-credit ties."""

    positives = np.asarray(
        [item.cosine_similarity for item in scores if item.same_identity], dtype=np.float64
    )
    negatives = np.asarray(
        [item.cosine_similarity for item in scores if not item.same_identity], dtype=np.float64
    )
    if not len(positives) or not len(negatives):
        raise ValueError("ROC-AUC requires both positive and negative scores")
    ordered_negatives = np.sort(negatives)
    strictly_lower = np.searchsorted(ordered_negatives, positives, side="left")
    lower_or_equal = np.searchsorted(ordered_negatives, positives, side="right")
    tie_count = lower_or_equal - strictly_lower
    return float(np.sum(strictly_lower + 0.5 * tie_count) / (len(positives) * len(negatives)))


def bootstrap_auc_interval(
    scores: list[PairScore],
    *,
    replicates: int,
    seed: int,
) -> tuple[float, float] | None:
    """Return a deterministic, class-stratified percentile bootstrap interval."""

    if replicates <= 0:
        return None
    positives = np.asarray(
        [item.cosine_similarity for item in scores if item.same_identity], dtype=np.float64
    )
    negatives = np.asarray(
        [item.cosine_similarity for item in scores if not item.same_identity], dtype=np.float64
    )
    if not len(positives) or not len(negatives):
        raise ValueError("ROC-AUC bootstrap requires both classes")
    generator = np.random.default_rng(seed)
    samples = np.empty(replicates, dtype=np.float64)
    for index in range(replicates):
        sampled_positive = generator.choice(positives, size=len(positives), replace=True)
        sampled_negative = generator.choice(negatives, size=len(negatives), replace=True)
        ordered_negative = np.sort(sampled_negative)
        lower = np.searchsorted(ordered_negative, sampled_positive, side="left")
        upper = np.searchsorted(ordered_negative, sampled_positive, side="right")
        samples[index] = np.sum(lower + 0.5 * (upper - lower)) / (
            len(sampled_positive) * len(sampled_negative)
        )
    lower, upper = np.quantile(samples, [0.025, 0.975])
    return float(lower), float(upper)


def cross_validated_threshold_metrics(scores: list[PairScore]) -> dict[str, Any]:
    folds = sorted({item.fold for item in scores})
    aggregate = Confusion(0, 0, 0, 0)
    fold_results: list[dict[str, Any]] = []
    for fold in folds:
        training = [item for item in scores if item.fold != fold]
        testing = [item for item in scores if item.fold == fold]
        if not training or not testing:
            raise BenchmarkError(f"fold {fold + 1} has no scored training or test pairs")
        threshold, _ = best_accuracy_threshold(training)
        confusion = confusion_at_threshold(testing, threshold)
        aggregate += confusion
        fold_results.append(
            {
                "fold": fold + 1,
                "threshold_selected_on_other_folds": threshold,
                "scored_pairs": confusion.total,
                "accuracy": confusion.correct / confusion.total,
            }
        )
    accuracies = np.asarray([item["accuracy"] for item in fold_results], dtype=np.float64)
    return {
        "selection": "best training-fold accuracy; test fold held out",
        "folds": fold_results,
        "pooled_confusion": asdict(aggregate),
        "pooled_accuracy": _rate(aggregate.correct, aggregate.total),
        "mean_fold_accuracy": float(np.mean(accuracies)),
        "sample_stddev_fold_accuracy": float(np.std(accuracies, ddof=1)),
    }


def _score_distribution(values: list[float]) -> dict[str, float]:
    array = np.asarray(values, dtype=np.float64)
    percentiles = np.quantile(array, [0.0, 0.05, 0.5, 0.95, 1.0])
    return {
        "min": float(percentiles[0]),
        "p05": float(percentiles[1]),
        "median": float(percentiles[2]),
        "p95": float(percentiles[3]),
        "max": float(percentiles[4]),
        "mean": float(np.mean(array)),
    }


def _validated_dataset_attestation(
    lfw_root: Path,
    supplied: DatasetAttestation | None,
) -> DatasetAttestation:
    tree_sha256, image_count, image_bytes = dataset_tree_fingerprint(lfw_root)
    if image_count != LFW_IMAGE_COUNT:
        raise BenchmarkError(
            f"LFW directory image count mismatch: {image_count} != {LFW_IMAGE_COUNT}"
        )
    if tree_sha256 != LFW_FUNNELED_TREE_SHA256 or image_bytes != LFW_FUNNELED_IMAGE_BYTES:
        raise BenchmarkError("LFW directory does not match the pinned funneled dataset tree")
    if supplied is None:
        return DatasetAttestation(
            acquisition_mode="external-directory-unverified-archive",
            archive_sha256=None,
            archive_integrity_verified=False,
            extraction_marker_verified=False,
            dataset_tree_sha256=tree_sha256,
            image_count=image_count,
            image_bytes=image_bytes,
        )
    expected = DatasetAttestation(
        acquisition_mode="sha256-pinned-mirror-clean-extraction",
        archive_sha256=LFW_FUNNELED_SHA256,
        archive_integrity_verified=True,
        extraction_marker_verified=True,
        dataset_tree_sha256=tree_sha256,
        image_count=image_count,
        image_bytes=image_bytes,
    )
    if supplied != expected:
        raise BenchmarkError("dataset no longer matches its verified extraction attestation")
    return supplied


def _verified_model_report(backend: OpenCVFaceBackend) -> dict[str, Any]:
    fingerprints = backend.model_fingerprints
    specs = {item.filename: item for item in DEFAULT_MODELS}
    detector = specs["face_detection_yunet_2023mar.onnx"]
    recognizer = specs["face_recognition_sface_2021dec.onnx"]
    if fingerprints.detector_sha256 != detector.sha256:
        raise BenchmarkError("YuNet model does not match the FaceProof pinned SHA-256")
    if fingerprints.recognizer_sha256 != recognizer.sha256:
        raise BenchmarkError("SFace model does not match the FaceProof pinned SHA-256")
    return {
        **fingerprints.as_dict(),
        "pinned_integrity_verified": True,
        "detector_expected_filename": detector.filename,
        "detector_source_url": detector.source_url,
        "recognizer_expected_filename": recognizer.filename,
        "recognizer_source_url": recognizer.source_url,
    }


def run_benchmark(
    *,
    lfw_root: Path,
    pairs_path: Path,
    detector_model: Path,
    recognizer_model: Path,
    fixed_threshold: float = DEFAULT_COSINE_THRESHOLD,
    bootstrap_replicates: int = 1_000,
    seed: int = 20_260_903,
    progress_every: int = 250,
    dataset_attestation: DatasetAttestation | None = None,
) -> dict[str, Any]:
    if not math.isfinite(fixed_threshold) or not -1 <= fixed_threshold <= 1:
        raise BenchmarkError("fixed threshold must be finite and between -1 and 1")
    if bootstrap_replicates < 0:
        raise BenchmarkError("bootstrap replicates cannot be negative")

    started = time.perf_counter()
    attestation = _validated_dataset_attestation(lfw_root, dataset_attestation)
    pairs = parse_lfw_pairs(pairs_path, lfw_root)
    if len(pairs) != LFW_PAIR_COUNT or len({pair.fold for pair in pairs}) != LFW_FOLD_COUNT:
        raise BenchmarkError("this runner requires the canonical 6,000-pair, 10-fold protocol")
    if file_sha256(pairs_path) != LFW_PAIRS_SHA256:
        raise BenchmarkError("pairs.txt does not match the pinned canonical SHA-256")
    validate_pair_images(pairs)

    backend = OpenCVFaceBackend(detector_model, recognizer_model)
    model_report = _verified_model_report(backend)
    unique_images = sorted({path for pair in pairs for path in (pair.left, pair.right)})
    embeddings: dict[Path, tuple[float, ...]] = {}
    rejection_classes: Counter[str] = Counter()
    quality_issues: Counter[str] = Counter()
    encode_started = time.perf_counter()
    for ordinal, image_path in enumerate(unique_images, start=1):
        try:
            embeddings[image_path] = backend.encode_one(image_path).embedding
        except FaceQualityError as exc:
            rejection_classes[type(exc).__name__] += 1
            quality_issues.update(exc.issues)
        except FaceError as exc:
            rejection_classes[type(exc).__name__] += 1
        if progress_every > 0 and (ordinal % progress_every == 0 or ordinal == len(unique_images)):
            elapsed = time.perf_counter() - encode_started
            print(
                f"encoded {ordinal}/{len(unique_images)} unique images in {elapsed:.1f}s",
                file=sys.stderr,
                flush=True,
            )
    encode_seconds = time.perf_counter() - encode_started

    pair_scores: list[PairScore] = []
    rejected_positive = rejected_negative = 0
    for pair in pairs:
        left = embeddings.get(pair.left)
        right = embeddings.get(pair.right)
        if left is None or right is None:
            if pair.same_identity:
                rejected_positive += 1
            else:
                rejected_negative += 1
            continue
        pair_scores.append(
            PairScore(
                fold=pair.fold,
                same_identity=pair.same_identity,
                cosine_similarity=cosine_similarity(left, right),
            )
        )
    if not pair_scores:
        raise BenchmarkError("quality policy rejected every benchmark pair")

    scored_positive = sum(item.same_identity for item in pair_scores)
    scored_negative = len(pair_scores) - scored_positive
    fixed_metrics = metrics_at_threshold(pair_scores, fixed_threshold)
    posthoc_threshold, _ = best_accuracy_threshold(pair_scores)
    posthoc_metrics = metrics_at_threshold(pair_scores, posthoc_threshold)
    auc = roc_auc(pair_scores)
    auc_interval = bootstrap_auc_interval(pair_scores, replicates=bootstrap_replicates, seed=seed)
    cross_validated = cross_validated_threshold_metrics(pair_scores)
    fixed_correct = fixed_metrics["accuracy"]["count"]

    import cv2  # Imported after backend use so the report captures the runtime backend.

    return {
        "schema": "faceproof-lfw-benchmark-v1",
        "claim": "LFW pair verification only; not web-search or open-set identification accuracy",
        "protocol": {
            "dataset": "Labeled Faces in the Wild, funneled, full 250x250 JPEGs",
            "view": "View 2 canonical 10-fold pairs.txt",
            "reporting_category": (
                "replication with an off-the-shelf model trained on labeled outside data; "
                "not an official leaderboard submission"
            ),
            "pairs_sha256": file_sha256(pairs_path),
            "original_dataset_page": LFW_ORIGINAL_PAGE,
            "original_archive_url": LFW_FUNNELED_ORIGINAL_URL,
            "original_pairs_url": LFW_PAIRS_ORIGINAL_URL,
            "pinned_mirror_archive_url": LFW_FUNNELED_MIRROR_URL,
            "expected_mirror_archive_sha256": LFW_FUNNELED_SHA256,
            "expected_dataset_tree_sha256": LFW_FUNNELED_TREE_SHA256,
            "pinned_mirror_pairs_url": LFW_PAIRS_MIRROR_URL,
            "expected_pairs": LFW_PAIR_COUNT,
            "expected_folds": LFW_FOLD_COUNT,
            "acquisition": asdict(attestation),
        },
        "runtime": {
            "faceproof_version": __version__,
            "python": sys.version.split()[0],
            "opencv": cv2.__version__,
            "benchmark_elapsed_seconds_excluding_acquisition": time.perf_counter() - started,
            "embedding_seconds": encode_seconds,
            "bootstrap_replicates": bootstrap_replicates,
            "bootstrap_seed": seed,
            "face_api_source_sha256": file_sha256(Path(face_module.__file__)),
            "benchmark_script_sha256": file_sha256(Path(__file__)),
        },
        "models": model_report,
        "face_policy": {
            "detector_score_threshold": backend.detection_score_threshold,
            "quality": asdict(DEFAULT_QUALITY_POLICY),
            "one_face_required": True,
        },
        "coverage": {
            "unique_images": len(unique_images),
            "encoded_images": len(embeddings),
            "rejected_images": len(unique_images) - len(embeddings),
            "image_coverage": _rate(len(embeddings), len(unique_images)),
            "image_rejection_classes": dict(sorted(rejection_classes.items())),
            "quality_issue_counts": dict(sorted(quality_issues.items())),
            "protocol_pairs": len(pairs),
            "scored_pairs": len(pair_scores),
            "rejected_pairs": len(pairs) - len(pair_scores),
            "pair_coverage": _rate(len(pair_scores), len(pairs)),
            "positive_pairs": {
                "scored": scored_positive,
                "rejected": rejected_positive,
            },
            "negative_pairs": {
                "scored": scored_negative,
                "rejected": rejected_negative,
            },
        },
        "roc_auc": {
            "value": auc,
            "ci95_stratified_percentile_bootstrap": (
                list(auc_interval) if auc_interval is not None else None
            ),
        },
        "fixed_operating_point": fixed_metrics,
        "fixed_correct_and_scored_yield_over_all_pairs": _rate(fixed_correct, len(pairs)),
        "posthoc_pooled_best_accuracy": {
            "warning": "threshold selected and evaluated on the same scored pairs; optimistic",
            **posthoc_metrics,
        },
        "cross_validated_threshold_selection": cross_validated,
        "score_distribution": {
            "same_identity": _score_distribution(
                [item.cosine_similarity for item in pair_scores if item.same_identity]
            ),
            "different_identity": _score_distribution(
                [item.cosine_similarity for item in pair_scores if not item.same_identity]
            ),
        },
        "limitations": [
            (
                "Rejected pairs are excluded from conditional ROC and error-rate metrics; "
                "coverage is separate."
            ),
            (
                "The fixed 0.363 operating point was already reported on LFW by OpenCV and "
                "is not independent."
            ),
            (
                "The pooled best threshold is post-hoc; only the fold-held-out result limits "
                "threshold leakage."
            ),
            (
                "LFW is not representative of current social-media crops, compression, "
                "occlusion, or demographics."
            ),
            (
                "Verification of curated 1:1 pairs does not measure open-web retrieval or "
                "1:N identification."
            ),
            (
                "Wilson and pair-bootstrap intervals are descriptive: pairs share images "
                "and are not strictly independent observations."
            ),
            "No names, images, pair scores, or embeddings are included in this aggregate report.",
        ],
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--download",
        action="store_true",
        help="download and SHA-256-verify the pinned LFW mirror before running",
    )
    parser.add_argument(
        "--force-download",
        action="store_true",
        help="replace existing pinned archive/pairs downloads (requires --download)",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=Path("benchmark-data/lfw"),
        help="local ignored cache for downloaded LFW data",
    )
    parser.add_argument("--lfw-dir", type=Path, help="existing lfw_funneled directory")
    parser.add_argument("--pairs-file", type=Path, help="existing canonical pairs.txt")
    parser.add_argument(
        "--detector-model",
        type=Path,
        default=Path("models/face_detection_yunet_2023mar.onnx"),
    )
    parser.add_argument(
        "--recognizer-model",
        type=Path,
        default=Path("models/face_recognition_sface_2021dec.onnx"),
    )
    parser.add_argument("--threshold", type=float, default=DEFAULT_COSINE_THRESHOLD)
    parser.add_argument("--bootstrap-replicates", type=int, default=1_000)
    parser.add_argument("--seed", type=int, default=20_260_903)
    parser.add_argument("--progress-every", type=int, default=250)
    parser.add_argument("--output", type=Path, help="optional aggregate JSON output path")
    return parser


def main(argv: list[str] | None = None) -> int:
    command_started = time.perf_counter()
    parser = _build_parser()
    arguments = parser.parse_args(argv)
    if arguments.force_download and not arguments.download:
        parser.error("--force-download requires --download")
    if arguments.download and (arguments.lfw_dir or arguments.pairs_file):
        parser.error("--download cannot be combined with --lfw-dir or --pairs-file")
    if bool(arguments.lfw_dir) != bool(arguments.pairs_file):
        parser.error("--lfw-dir and --pairs-file must be provided together")

    try:
        dataset_attestation: DatasetAttestation | None = None
        if arguments.download:
            print("acquiring SHA-256-pinned LFW inputs", file=sys.stderr, flush=True)
            lfw_root, pairs_path, dataset_attestation = acquire_official_lfw(
                arguments.cache_dir, force_download=arguments.force_download
            )
        elif arguments.lfw_dir:
            lfw_root, pairs_path = arguments.lfw_dir, arguments.pairs_file
        else:
            lfw_root = arguments.cache_dir / "lfw_funneled"
            pairs_path = arguments.cache_dir / "pairs.txt"

        result = run_benchmark(
            lfw_root=lfw_root,
            pairs_path=pairs_path,
            detector_model=arguments.detector_model,
            recognizer_model=arguments.recognizer_model,
            fixed_threshold=arguments.threshold,
            bootstrap_replicates=arguments.bootstrap_replicates,
            seed=arguments.seed,
            progress_every=arguments.progress_every,
            dataset_attestation=dataset_attestation,
        )
        result["runtime"]["command_elapsed_seconds_including_acquisition"] = (
            time.perf_counter() - command_started
        )
    except (BenchmarkError, FaceError, httpx.HTTPError, OSError, tarfile.TarError) as exc:
        print(f"benchmark failed: {exc}", file=sys.stderr)
        return 2

    serialized = json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n"
    if arguments.output:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(serialized, encoding="utf-8")
        print(f"wrote aggregate report to {arguments.output}", file=sys.stderr)
    sys.stdout.write(serialized)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
