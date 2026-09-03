from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx


class ModelDownloadError(RuntimeError):
    """Raised when an official model download fails integrity validation."""


@dataclass(frozen=True, slots=True)
class ModelAsset:
    filename: str
    url: str
    sha256: str
    byte_size: int
    license_name: str
    source_url: str


DEFAULT_MODELS = (
    ModelAsset(
        filename="face_detection_yunet_2023mar.onnx",
        url=(
            "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/"
            "models/face_detection_yunet/face_detection_yunet_2023mar.onnx"
        ),
        sha256="8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4",
        byte_size=232_589,
        license_name="MIT",
        source_url=("https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet"),
    ),
    ModelAsset(
        filename="face_recognition_sface_2021dec.onnx",
        url=(
            "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/"
            "models/face_recognition_sface/face_recognition_sface_2021dec.onnx"
        ),
        sha256="0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79",
        byte_size=38_696_353,
        license_name="Apache-2.0",
        source_url=("https://github.com/opencv/opencv_zoo/tree/main/models/face_recognition_sface"),
    ),
)


def download_default_models(
    destination: Path,
    *,
    force: bool = False,
    timeout_seconds: float = 120,
    on_progress: Callable[[str], None] | None = None,
) -> tuple[Path, ...]:
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    downloaded: list[Path] = []
    with httpx.Client(
        timeout=timeout_seconds,
        follow_redirects=True,
        headers={"User-Agent": "FaceProof/0.1 model-downloader"},
    ) as client:
        for model in DEFAULT_MODELS:
            output = destination / model.filename
            if output.is_file() and not force:
                _validate_model(output, model)
                if on_progress:
                    on_progress(f"verified existing {model.filename}")
                downloaded.append(output)
                continue
            if on_progress:
                on_progress(f"downloading {model.filename}")
            temporary = output.with_suffix(output.suffix + ".part")
            digest = hashlib.sha256()
            size = 0
            try:
                with client.stream("GET", model.url) as response:
                    response.raise_for_status()
                    with temporary.open("wb") as handle:
                        for chunk in response.iter_bytes(chunk_size=1024 * 1024):
                            size += len(chunk)
                            if size > model.byte_size:
                                raise ModelDownloadError(f"{model.filename} exceeded expected size")
                            digest.update(chunk)
                            handle.write(chunk)
                if size != model.byte_size:
                    raise ModelDownloadError(
                        f"{model.filename} size mismatch: {size} != {model.byte_size}"
                    )
                actual_hash = digest.hexdigest()
                if actual_hash != model.sha256:
                    raise ModelDownloadError(f"{model.filename} SHA-256 mismatch: {actual_hash}")
                os.replace(temporary, output)
            except (httpx.HTTPError, OSError) as exc:
                raise ModelDownloadError(f"Failed to download {model.filename}: {exc}") from exc
            finally:
                if temporary.exists():
                    temporary.unlink()
            if on_progress:
                on_progress(f"verified {model.filename} ({size:,} bytes)")
            downloaded.append(output)
    return tuple(downloaded)


def verify_default_models(destination: Path) -> dict[str, str]:
    results: dict[str, str] = {}
    for model in DEFAULT_MODELS:
        path = Path(destination) / model.filename
        try:
            _validate_model(path, model)
        except ModelDownloadError as exc:
            results[model.filename] = str(exc)
        else:
            results[model.filename] = "ok"
    return results


def _validate_model(path: Path, model: ModelAsset) -> None:
    if not path.is_file():
        raise ModelDownloadError(f"missing: {path}")
    if path.stat().st_size != model.byte_size:
        raise ModelDownloadError(
            f"size mismatch for {model.filename}: {path.stat().st_size} != {model.byte_size}"
        )
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != model.sha256:
        raise ModelDownloadError(f"SHA-256 mismatch for {model.filename}")
