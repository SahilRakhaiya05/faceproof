from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import tempfile
import threading
import time
import zipfile
from collections.abc import Callable
from copy import deepcopy
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from PIL import Image, UnidentifiedImageError
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from . import __version__
from .chain import ChainError, make_web3, registry_contract
from .config import Settings
from .face import (
    FaceError,
    FaceQualityError,
    MultipleFacesError,
    NoFaceError,
    OpenCVFaceBackend,
    QualityPolicy,
)
from .model_assets import verify_default_models
from .pipeline import (
    InconclusiveError,
    PipelineError,
    PipelineResult,
    anchor_submission_path,
    recover_pending_anchor,
    run_pipeline,
    run_tamper_demo,
    verify_run,
)
from .provenance import ProvenanceError, verify_git_source_revision
from .reporting import list_run_summaries, summarize_run
from .review import ReviewError, load_reviewed_discovery
from .search import (
    PROFILE_LEAD_PLATFORMS,
    SUPPORTED_PLATFORMS,
    SearchError,
    check_serpapi_account,
    normalize_bluesky_actor,
)

MAX_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
ASSET_NAMES = frozenset({"app.css", "app.js", "favicon.svg"})
MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _snapshot_run(run_dir: Path) -> tuple[tempfile.TemporaryDirectory[str], Path]:
    """Copy one run without following symlinks so later reads share verified bytes."""

    temporary = tempfile.TemporaryDirectory(prefix="faceproof-web-snapshot-")
    snapshot = Path(temporary.name) / run_dir.name
    try:
        shutil.copytree(run_dir, snapshot, symlinks=True)
    except Exception:
        temporary.cleanup()
        raise
    return temporary, snapshot


def _parse_loopback_authority(value: str) -> tuple[str, int | None] | None:
    if not value or any(ord(character) <= 0x20 or ord(character) == 0x7F for character in value):
        return None
    host: str
    port_text: str | None
    if value.startswith("["):
        closing = value.find("]")
        if closing < 0:
            return None
        host = value[1:closing]
        remainder = value[closing + 1 :]
        if remainder and not remainder.startswith(":"):
            return None
        port_text = remainder[1:] if remainder else None
    else:
        if value.count(":") > 1:
            return None
        host, separator, port = value.partition(":")
        port_text = port if separator else None
    if host.casefold() not in {"localhost", "127.0.0.1", "::1"}:
        return None
    if port_text is None:
        return host.casefold(), None
    if not port_text.isascii() or not port_text.isdigit():
        return None
    port = int(port_text)
    if not 1 <= port <= 65535:
        return None
    return host.casefold(), port


def _request_has_trusted_host(request: Request) -> bool:
    host_header = request.headers.get("host", "")
    if _parse_loopback_authority(host_header) is not None:
        return True
    # Starlette's in-process TestClient uses this synthetic, non-network peer.
    return bool(
        host_header == "testserver"
        and request.client is not None
        and request.client.host == "testclient"
    )


def _origin_matches_request(request: Request, origin: str) -> bool:
    try:
        parts = urlsplit(origin)
        origin_host = parts.hostname
        origin_port = parts.port
    except (UnicodeError, ValueError):
        return False
    if (
        parts.scheme not in {"http", "https"}
        or not origin_host
        or parts.username
        or parts.password
        or parts.path not in {"", "/"}
        or parts.query
        or parts.fragment
    ):
        return False
    request_authority = _parse_loopback_authority(request.headers.get("host", ""))
    if request_authority is None:
        return False
    request_host, request_port = request_authority
    default_port = 443 if request.url.scheme == "https" else 80
    expected_port = request_port or default_port
    observed_port = origin_port or (443 if parts.scheme == "https" else 80)
    return (
        parts.scheme == request.url.scheme
        and origin_host.casefold() == request_host
        and observed_port == expected_port
    )


def _preflight_action(issues: tuple[str, ...], policy: QualityPolicy) -> str:
    issue_set = set(issues)
    if issue_set & {"face_too_small", "face_area_too_small"}:
        return (
            "Use the original-resolution image or a closer crop with one face at least "
            f"{policy.min_face_size_px} pixels wide and high."
        )
    if "face_too_blurry" in issue_set:
        return "Use a sharper, in-focus image with one unobstructed face."
    if issue_set & {"face_too_dark", "face_too_bright"}:
        return "Use an evenly lit image with visible facial detail."
    if "face_clipped_by_frame" in issue_set:
        return "Use an image that contains the complete face inside the frame."
    return "Use a clear, front-facing image containing one unobstructed face."


def _quality_payload(metrics: Any | None) -> dict[str, float | None] | None:
    if metrics is None:
        return None
    return {
        "confidence": float(metrics.confidence),
        "face_width_px": float(metrics.face_width_px),
        "face_height_px": float(metrics.face_height_px),
        "face_area_ratio": float(metrics.face_area_ratio),
        "visible_fraction": float(metrics.visible_fraction),
        "sharpness": None if metrics.sharpness is None else float(metrics.sharpness),
        "brightness": None if metrics.brightness is None else float(metrics.brightness),
    }


def _preflight_requirements(policy: QualityPolicy) -> dict[str, float | int | None]:
    return {
        "min_confidence": policy.min_confidence,
        "min_face_size_px": policy.min_face_size_px,
        "min_face_area_ratio": policy.min_face_area_ratio,
        "min_visible_fraction": policy.min_visible_fraction,
        "min_sharpness": policy.min_sharpness,
        "min_brightness": policy.min_brightness,
        "max_brightness": policy.max_brightness,
    }


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _stage_code(message: str) -> str:
    value = message.casefold()
    if "detecting" in value:
        return "face"
    if "profile lead" in value:
        return "profiles"
    if "google lens" in value or "bluesky media feed" in value or value.startswith("search "):
        return "search"
    if "re-matching" in value:
        return "rematch"
    if "capturing" in value:
        return "capture"
    if "canonical" in value or "hashing" in value:
        return "evidence"
    if "anchoring" in value:
        return "anchor"
    if "chain state" in value or "verification" in value:
        return "verify"
    return "working"


def _check_chain_readiness(settings: Settings) -> tuple[bool, str]:
    """Run the same read-only trust gates required before a demo write."""
    if not (
        settings.contract_address
        and settings.private_key
        and settings.contract_code_hash
        and settings.source_revision
    ):
        return False, "Discovery works; chain credentials and deployment are incomplete"
    try:
        verify_git_source_revision(settings.source_revision)
        web3 = make_web3(settings.rpc_url, timeout_seconds=min(settings.http_timeout_seconds, 10))
        if not web3.is_connected():
            return False, "Configured, but the RPC connection failed"
        if int(web3.eth.chain_id) != settings.chain_id:
            return False, "Configured RPC is on the wrong chain"
        registry_contract(
            web3,
            settings.contract_address,
            expected_code_hash=settings.contract_code_hash,
        )
        account = web3.eth.account.from_key(settings.private_key)
        if int(web3.eth.get_balance(account.address)) <= 0:
            return False, "Contract verified, but the dedicated testnet wallet is empty"
    except (ChainError, ProvenanceError, ValueError):
        return False, "Configured chain, source revision, or registry validation failed"
    except Exception as exc:
        return False, f"Read-only chain validation failed ({type(exc).__name__})"
    return True, "RPC, chain ID, registry code, source revision, and wallet balance verified"


class JobStore:
    """Single-flight local job registry with idempotent submission."""

    def __init__(
        self,
        settings: Settings,
        *,
        pipeline_runner: Callable[..., PipelineResult] = run_pipeline,
    ) -> None:
        self.settings = settings
        self.pipeline_runner = pipeline_runner
        self._jobs: dict[str, dict[str, Any]] = {}
        self._idempotency: dict[str, str] = {}
        self._lock = threading.RLock()
        self._gate = threading.BoundedSemaphore(1)

    def submit(
        self,
        *,
        image_path: Path,
        idempotency_key: str,
        options: dict[str, Any],
    ) -> dict[str, Any]:
        with self._lock:
            existing_id = self._idempotency.get(idempotency_key)
            if existing_id and existing_id in self._jobs:
                image_path.unlink(missing_ok=True)
                return self._public(self._jobs[existing_id])
            pending = sum(job["state"] in {"queued", "running"} for job in self._jobs.values())
            if pending >= 2:
                raise RuntimeError("The local analysis queue is full; wait for the active run")
            job_id = secrets.token_hex(10)
            job = {
                "job_id": job_id,
                "state": "queued",
                "created_at": _now(),
                "updated_at": _now(),
                "latest_stage": "Waiting for the local pipeline",
                "stages": [],
                "result": None,
                "error": None,
                "_image_path": image_path,
                "_options": options,
            }
            self._jobs[job_id] = job
            self._idempotency[idempotency_key] = job_id
        thread = threading.Thread(
            target=self._execute,
            args=(job_id,),
            daemon=True,
            name=f"faceproof-web-{job_id[:8]}",
        )
        thread.start()
        return self.get(job_id)

    def get(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise KeyError(job_id)
            return self._public(job)

    def _public(self, job: dict[str, Any]) -> dict[str, Any]:
        return deepcopy({key: value for key, value in job.items() if not key.startswith("_")})

    def _update(self, job_id: str, **values: Any) -> None:
        with self._lock:
            job = self._jobs[job_id]
            job.update(values)
            job["updated_at"] = _now()

    def _stage(self, job_id: str, message: str) -> None:
        event = {"code": _stage_code(message), "message": message, "at": _now()}
        with self._lock:
            job = self._jobs[job_id]
            job["latest_stage"] = message
            job["stages"].append(event)
            job["updated_at"] = event["at"]

    def _execute(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs[job_id]
            image_path = Path(job["_image_path"])
            options = dict(job["_options"])
        self._gate.acquire()
        root = self.settings.output_dir
        before: set[Path] = set()
        try:
            root.mkdir(parents=True, exist_ok=True)
            before = {path.resolve() for path in root.iterdir() if path.is_dir()}
            self._update(job_id, state="running", latest_stage="Starting consented analysis")
            result = self.pipeline_runner(
                image_path=image_path,
                settings=self.settings,
                consent_acknowledged=True,
                live=True,
                on_stage=lambda message: self._stage(job_id, message),
                **options,
            )
        except InconclusiveError as exc:
            self._update(
                job_id,
                state="inconclusive",
                latest_stage="Search completed without a verified post match",
                error={"type": "inconclusive", "message": str(exc)},
                result=summarize_run(exc.run_dir),
            )
        except (PipelineError, ValueError, OSError) as exc:
            after = [
                path for path in root.iterdir() if path.is_dir() and path.resolve() not in before
            ]
            partial = max(after, key=lambda path: path.stat().st_mtime, default=None)
            self._update(
                job_id,
                state="failed",
                latest_stage="Pipeline stopped safely",
                error={"type": type(exc).__name__, "message": str(exc)},
                result=summarize_run(partial) if partial else None,
            )
        except Exception as exc:  # pragma: no cover - final fail-safe boundary
            self._update(
                job_id,
                state="failed",
                latest_stage="Unexpected local error",
                error={"type": type(exc).__name__, "message": "Unexpected local pipeline error"},
            )
        else:
            summary = summarize_run(result.run_dir)
            if result.chain_verification is not None and result.chain_verification.passed:
                summary["status"] = "anchored"
                summary["verification_state"] = "verified-at-run"
            self._update(
                job_id,
                state="completed",
                latest_stage=(
                    "Blockchain proof verified"
                    if result.chain_receipt
                    else "Evidence bundle completed"
                ),
                result=summary,
            )
        finally:
            image_path.unlink(missing_ok=True)
            self._gate.release()


def _web_asset(name: str) -> bytes:
    if name not in ASSET_NAMES:
        raise FileNotFoundError(name)
    return resources.files("faceproof.web_assets").joinpath(name).read_bytes()


def _safe_run_dir(settings: Settings, run_id: str) -> Path:
    if not RUN_ID_PATTERN.fullmatch(run_id):
        raise HTTPException(status_code=404, detail="Evidence run not found")
    root = settings.output_dir.resolve()
    candidate = (root / run_id).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Evidence run not found") from exc
    if not candidate.is_dir():
        raise HTTPException(status_code=404, detail="Evidence run not found")
    return candidate


def _safe_asset(settings: Settings, run_id: str, asset_path: str) -> Path:
    run_dir = _safe_run_dir(settings, run_id)
    candidate = (run_dir / asset_path).resolve()
    try:
        candidate.relative_to(run_dir)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Evidence asset not found") from exc
    if not candidate.is_file() or candidate.suffix.casefold() not in {
        ".jpg",
        ".jpeg",
        ".png",
        ".webp",
    }:
        raise HTTPException(status_code=404, detail="Evidence asset not found")
    return candidate


async def _save_upload(upload: UploadFile) -> Path:
    payload = bytearray()
    while chunk := await upload.read(1024 * 1024):
        payload.extend(chunk)
        if len(payload) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="Image exceeds the 25 MB limit")
    if not payload:
        raise HTTPException(status_code=400, detail="Choose a non-empty image")
    try:
        with Image.open(io.BytesIO(payload)) as opened:
            width, height = opened.size
            image_format = str(opened.format or "").upper()
            if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
                raise HTTPException(status_code=400, detail="Image dimensions are unsafe")
            if image_format not in {"JPEG", "PNG", "WEBP"}:
                raise HTTPException(status_code=400, detail="Use a JPEG, PNG, or WebP image")
            opened.verify()
    except HTTPException:
        raise
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        raise HTTPException(
            status_code=400, detail="The uploaded file is not a valid image"
        ) from exc
    suffix = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}[image_format]
    file_descriptor, temporary_name = tempfile.mkstemp(prefix="faceproof-upload-", suffix=suffix)
    os.close(file_descriptor)
    path = Path(temporary_name)
    path.write_bytes(bytes(payload))
    return path


def create_app(
    settings: Settings | None = None,
    *,
    pipeline_runner: Callable[..., PipelineResult] = run_pipeline,
) -> FastAPI:
    if settings is None:
        load_dotenv(override=False)
    runtime = settings or Settings.from_env()
    csrf_token = secrets.token_urlsafe(32)
    jobs = JobStore(runtime, pipeline_runner=pipeline_runner)
    readiness_cache: dict[str, Any] = {"at": 0.0, "value": None}
    readiness_lock = threading.Lock()
    app = FastAPI(
        title="FaceProof Judge Console",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        if not _request_has_trusted_host(request):
            response = JSONResponse(
                status_code=421,
                content={"detail": "FaceProof only accepts loopback Host headers"},
            )
        elif request.method in MUTATING_METHODS:
            origin = request.headers.get("origin")
            fetch_site = request.headers.get("sec-fetch-site", "").casefold()
            if origin and not _origin_matches_request(request, origin):
                response = JSONResponse(
                    status_code=403,
                    content={"detail": "Cross-origin local requests are not allowed"},
                )
            elif fetch_site and fetch_site not in {"same-origin", "none"}:
                response = JSONResponse(
                    status_code=403,
                    content={"detail": "Cross-site local requests are not allowed"},
                )
            else:
                response = await call_next(request)
        else:
            response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; "
            "frame-ancestors 'none'; form-action 'self'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response

    def require_csrf(value: str | None) -> None:
        if value is None or not secrets.compare_digest(value, csrf_token):
            raise HTTPException(status_code=403, detail="Invalid local request token")

    async def verify_stable_anchor_state(
        run_dir: Path,
        *,
        forbid_pending: bool,
    ) -> tuple[Any, bool, bool]:
        for _ in range(3):
            pending_path = anchor_submission_path(run_dir)
            pending_before = pending_path.exists() or pending_path.is_symlink()
            receipt_before = (run_dir / "chain-receipt.json").is_file()
            if forbid_pending and pending_before:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Anchor outcome is pending recovery; no public receipt can be issued yet"
                    ),
                )
            result = await run_in_threadpool(
                verify_run,
                run_dir,
                settings=runtime,
                require_chain=receipt_before,
            )
            pending_after = pending_path.exists() or pending_path.is_symlink()
            receipt_after = (run_dir / "chain-receipt.json").is_file()
            if forbid_pending and pending_after:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Anchor outcome is pending recovery; no public receipt can be issued yet"
                    ),
                )
            if (pending_before, receipt_before) == (pending_after, receipt_after):
                return result, pending_after, receipt_after
        raise HTTPException(
            status_code=409,
            detail="Anchor state changed during verification; retry after it settles",
        )

    async def verified_anchor_snapshot(
        run_dir: Path,
        *,
        forbid_pending: bool,
    ) -> tuple[tempfile.TemporaryDirectory[str], Path, Any, bool]:
        """Return a stable run snapshot whose exact bytes passed verification."""

        for _ in range(3):
            pending_path = anchor_submission_path(run_dir)
            pending_before = pending_path.exists() or pending_path.is_symlink()
            receipt_before = (run_dir / "chain-receipt.json").is_file()
            if forbid_pending and pending_before:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Anchor outcome is pending recovery; no public receipt can be issued yet"
                    ),
                )
            temporary, snapshot = await run_in_threadpool(_snapshot_run, run_dir)
            try:
                snapshot_receipt = (snapshot / "chain-receipt.json").is_file()
                result = await run_in_threadpool(
                    verify_run,
                    snapshot,
                    settings=runtime,
                    require_chain=snapshot_receipt,
                )
                pending_after = pending_path.exists() or pending_path.is_symlink()
                receipt_after = (run_dir / "chain-receipt.json").is_file()
                if forbid_pending and pending_after:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "Anchor outcome is pending recovery; no public receipt can be "
                            "issued yet"
                        ),
                    )
                if (pending_before, receipt_before) == (
                    pending_after,
                    receipt_after,
                ) and snapshot_receipt == receipt_before:
                    return temporary, snapshot, result, snapshot_receipt
            except Exception:
                temporary.cleanup()
                raise
            temporary.cleanup()
        raise HTTPException(
            status_code=409,
            detail="Anchor state changed during verification; retry after it settles",
        )

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        template = (
            resources.files("faceproof.web_assets")
            .joinpath("index.html")
            .read_text(encoding="utf-8")
        )
        rendered = template.replace("__FACEPROOF_CSRF__", csrf_token).replace(
            "__FACEPROOF_VERSION__", __version__
        )
        return HTMLResponse(rendered)

    @app.get("/assets/{name}")
    async def asset(name: str) -> Response:
        try:
            payload = _web_asset(name)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Asset not found") from exc
        media_type = {
            ".css": "text/css",
            ".js": "application/javascript",
            ".svg": "image/svg+xml",
        }[Path(name).suffix]
        return Response(payload, media_type=media_type)

    @app.get("/api/readiness")
    async def readiness() -> dict[str, Any]:
        with readiness_lock:
            if (
                readiness_cache["value"] is not None
                and time.monotonic() - readiness_cache["at"] < 60
            ):
                return deepcopy(readiness_cache["value"])
        model_status = await run_in_threadpool(verify_default_models, runtime.model_dir)
        models_ready = bool(model_status) and all(value == "ok" for value in model_status.values())
        search: dict[str, Any] = {
            "ready": bool(runtime.serpapi_api_key),
            "plan": None,
            "remaining": None,
            "monthly": None,
            "detail": "API key configured"
            if runtime.serpapi_api_key
            else "SERPAPI_API_KEY is missing",
        }
        if runtime.serpapi_api_key:
            try:
                account = await run_in_threadpool(
                    check_serpapi_account,
                    runtime.serpapi_api_key,
                    timeout_seconds=min(runtime.http_timeout_seconds, 10),
                )
            except (SearchError, ValueError):
                search.update(ready=False, detail="Account validation failed")
            else:
                search.update(
                    ready=account.ready,
                    plan=account.plan_name,
                    remaining=account.searches_left,
                    monthly=account.searches_per_month,
                    detail=(
                        f"{account.searches_left} of {account.searches_per_month} searches remain"
                    ),
                )
        chain_ready, chain_detail = await run_in_threadpool(_check_chain_readiness, runtime)
        value = {
            "version": __version__,
            "models": {"ready": models_ready, "detail": model_status},
            "search": search,
            "bluesky": {
                "ready": True,
                "detail": (
                    "Public author-feed connector needs no API key and never uploads the query face"
                ),
            },
            "blockchain": {
                "ready": chain_ready,
                "chain_id": runtime.chain_id,
                "network": "Base Sepolia"
                if runtime.chain_id == 84532
                else f"Chain {runtime.chain_id}",
                "contract": runtime.contract_address,
                "detail": chain_detail,
            },
            "accuracy": {
                "scored_pair_accuracy": 0.9920,
                "pair_coverage": 0.6877,
                "all_pair_yield": 0.6822,
                "far": 0.00145,
                "frr": 0.01458,
                "scope": "LFW 1:1 verification baseline; not web-search accuracy",
            },
        }
        with readiness_lock:
            readiness_cache.update(at=time.monotonic(), value=value)
        return deepcopy(value)

    @app.post("/api/preflight")
    async def preflight(
        consent_adult: Annotated[bool, Form()],
        consent_authorized: Annotated[bool, Form()],
        image: Annotated[UploadFile, File()],
        x_faceproof_csrf: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        """Validate one face locally without searching, uploading, or persisting an embedding."""
        require_csrf(x_faceproof_csrf)
        if not consent_adult or not consent_authorized:
            raise HTTPException(
                status_code=400,
                detail="Confirm adult status and authorization before local face processing",
            )
        model_status = await run_in_threadpool(verify_default_models, runtime.model_dir)
        if not model_status or any(value != "ok" for value in model_status.values()):
            raise HTTPException(
                status_code=503,
                detail="Pinned face-model integrity verification failed",
            )

        backend = OpenCVFaceBackend(runtime.yunet_model, runtime.sface_model)
        temporary = await _save_upload(image)
        policy = backend.quality_policy
        base: dict[str, Any] = {
            "local_only": True,
            "external_upload": False,
            "search_credit_consumed": False,
            "embedding_persisted": False,
            "requirements": _preflight_requirements(policy),
        }
        try:
            encoding = await run_in_threadpool(backend.encode_one, temporary)
        except FaceQualityError as exc:
            return {
                **base,
                "passed": False,
                "code": "face-quality",
                "issues": list(exc.issues),
                "quality": _quality_payload(exc.metrics),
                "action": _preflight_action(exc.issues, policy),
            }
        except NoFaceError:
            return {
                **base,
                "passed": False,
                "code": "no-face",
                "issues": ["no_face_detected"],
                "quality": None,
                "action": "Use a clear, front-facing photo in which the full face is visible.",
            }
        except MultipleFacesError as exc:
            return {
                **base,
                "passed": False,
                "code": "multiple-faces",
                "issues": ["multiple_faces_detected"],
                "detected_faces": exc.count,
                "quality": None,
                "action": "Crop the image so it contains exactly one authorized face.",
            }
        except FaceError as exc:
            return {
                **base,
                "passed": False,
                "code": "face-processing",
                "issues": [type(exc).__name__],
                "quality": None,
                "action": "Use a valid original-resolution JPEG, PNG, or WebP image.",
            }
        finally:
            temporary.unlink(missing_ok=True)

        box = encoding.detection.box
        return {
            **base,
            "passed": True,
            "code": "ready",
            "issues": [],
            "message": "Face passed every local quality gate; no search credit was used.",
            "embedding_dimensions": len(encoding.embedding),
            "detection": {
                "confidence": encoding.detection.confidence,
                "box": {
                    "x": box.x,
                    "y": box.y,
                    "width": box.width,
                    "height": box.height,
                },
            },
            "quality": _quality_payload(encoding.quality),
            "aligned_size": list(encoding.aligned_size),
            "model_fingerprints": encoding.models.as_dict(),
        }

    @app.post("/api/runs", status_code=202)
    async def create_run(
        consent_adult: Annotated[bool, Form()],
        consent_authorized: Annotated[bool, Form()],
        consent_public_search: Annotated[bool, Form()],
        consent_provider_upload: Annotated[bool, Form()],
        image: Annotated[UploadFile | None, File()] = None,
        mode: Annotated[str, Form()] = "discovery",
        consent_profile_discovery: Annotated[bool, Form()] = False,
        consent_chain_irreversible: Annotated[bool, Form()] = False,
        consent_reference: Annotated[str, Form()] = "",
        approved_post_url: Annotated[str, Form()] = "",
        review_run_id: Annotated[str, Form()] = "",
        search_mode: Annotated[str, Form()] = "standard",
        search_provider: Annotated[str, Form()] = "lens",
        bluesky_actor: Annotated[str, Form()] = "",
        platforms: Annotated[
            str, Form()
        ] = "bluesky,facebook,instagram,linkedin,reddit,tiktok,x,youtube",
        max_candidates: Annotated[int, Form()] = 10,
        x_faceproof_csrf: Annotated[str | None, Header()] = None,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> JSONResponse:
        require_csrf(x_faceproof_csrf)
        if not idempotency_key or not 8 <= len(idempotency_key) <= 128:
            raise HTTPException(status_code=400, detail="A valid idempotency key is required")
        if not all((consent_adult, consent_authorized, consent_public_search)):
            raise HTTPException(
                status_code=400, detail="Complete every required consent attestation"
            )
        if mode not in {"discovery", "anchor"}:
            raise HTTPException(status_code=400, detail="Mode must be discovery or anchor")
        if search_mode not in {"standard", "deep"}:
            raise HTTPException(status_code=400, detail="Search mode must be standard or deep")
        search_provider = search_provider.strip().casefold()
        if search_provider not in {"lens", "bluesky"}:
            raise HTTPException(status_code=400, detail="Search provider must be Lens or Bluesky")
        normalized_bluesky_actor: str | None = None
        if search_provider == "bluesky" and mode == "discovery":
            try:
                normalized_bluesky_actor = normalize_bluesky_actor(bluesky_actor)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        selected_platforms = frozenset(
            item.strip().casefold() for item in platforms.split(",") if item.strip()
        )
        if not selected_platforms or selected_platforms - SUPPORTED_PLATFORMS:
            raise HTTPException(status_code=400, detail="Select at least one supported platform")
        if not 1 <= max_candidates <= 20:
            raise HTTPException(status_code=400, detail="Candidate limit must be from 1 to 20")
        review_manifest_hash: str | None = None
        review_commitment: str | None = None
        review_input_sha256: str | None = None
        reviewed_content_identity = None
        threshold = 0.363
        profile_candidate_limit = 6 if consent_profile_discovery else 0
        profile_platforms = PROFILE_LEAD_PLATFORMS
        if mode == "anchor":
            if not consent_chain_irreversible:
                raise HTTPException(
                    status_code=400, detail="Acknowledge the irreversible chain commitment"
                )
            chain_ready, _chain_detail = await run_in_threadpool(_check_chain_readiness, runtime)
            if not chain_ready:
                raise HTTPException(status_code=400, detail="Blockchain setup is incomplete")
            if not review_run_id.strip() or not approved_post_url.strip():
                raise HTTPException(
                    status_code=400,
                    detail="Anchoring must follow a reviewed local discovery run",
                )
            try:
                reviewed = await run_in_threadpool(
                    load_reviewed_discovery,
                    output_dir=runtime.output_dir,
                    run_id=review_run_id,
                    approved_post_url=approved_post_url,
                    settings=runtime,
                )
            except ReviewError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            if reviewed.profile_discovery_authorized and not consent_profile_discovery:
                raise HTTPException(
                    status_code=400,
                    detail="Re-acknowledge public profile discovery for the fresh anchor pass",
                )
            selected_platforms = reviewed.platforms
            search_mode = reviewed.search_mode
            search_provider = reviewed.search_provider
            normalized_bluesky_actor = reviewed.bluesky_actor
            threshold = reviewed.threshold
            max_candidates = reviewed.max_candidates
            profile_candidate_limit = reviewed.max_profile_candidates
            profile_platforms = reviewed.profile_platforms
            review_manifest_hash = reviewed.manifest_sha256
            review_commitment = reviewed.commitment
            review_input_sha256 = reviewed.input_sha256
            reviewed_content_identity = reviewed.content_identity
            descriptor, temporary_name = tempfile.mkstemp(
                prefix="faceproof-reviewed-", suffix=reviewed.image_path.suffix
            )
            os.close(descriptor)
            temporary = Path(temporary_name)
            reviewed_bytes = reviewed.input_bytes
            if (
                len(reviewed_bytes) > MAX_UPLOAD_BYTES
                or hashlib.sha256(reviewed_bytes).hexdigest() != reviewed.input_sha256
            ):
                temporary.unlink(missing_ok=True)
                raise HTTPException(
                    status_code=409,
                    detail="Reviewed input image changed after verification",
                )
            temporary.write_bytes(reviewed_bytes)
        else:
            if image is None:
                raise HTTPException(status_code=400, detail="Choose one consented image")
            temporary = await _save_upload(image)
        if search_provider == "lens" and not consent_provider_upload:
            temporary.unlink(missing_ok=True)
            raise HTTPException(
                status_code=400,
                detail="Allow the metadata-stripped query image to reach SerpApi for Lens search",
            )
        if search_provider == "bluesky":
            if search_mode != "standard":
                temporary.unlink(missing_ok=True)
                raise HTTPException(
                    status_code=400, detail="Bluesky author-feed search uses standard mode only"
                )
            if consent_profile_discovery:
                temporary.unlink(missing_ok=True)
                raise HTTPException(
                    status_code=400,
                    detail="Profile-lead discovery is available only with Lens",
                )
            selected_platforms = frozenset({"bluesky"})
            profile_candidate_limit = 0
            profile_platforms = PROFILE_LEAD_PLATFORMS
        options = {
            "consent_reference": consent_reference.strip() or None,
            "skip_anchor": mode == "discovery",
            "threshold": threshold,
            "max_candidates": max_candidates,
            "max_profile_candidates": profile_candidate_limit,
            "profile_discovery_authorized": profile_candidate_limit > 0,
            "profile_platforms": profile_platforms,
            "search_mode": search_mode,
            "search_provider": search_provider,
            "bluesky_actor": normalized_bluesky_actor,
            "platforms": selected_platforms,
            "approved_post_url": approved_post_url.strip() or None,
            "review_run_id": review_run_id.strip() or None,
            "review_manifest_sha256": review_manifest_hash,
            "review_commitment": review_commitment,
            "review_input_sha256": review_input_sha256,
            "reviewed_content_identity": reviewed_content_identity,
        }
        try:
            job = jobs.submit(
                image_path=temporary,
                idempotency_key=idempotency_key,
                options=options,
            )
        except RuntimeError as exc:
            temporary.unlink(missing_ok=True)
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        return JSONResponse(job, status_code=202)

    @app.get("/api/runs/{job_id}")
    async def get_job(job_id: str) -> dict[str, Any]:
        try:
            return jobs.get(job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Job not found") from exc

    @app.get("/api/runs/{job_id}/events")
    async def job_events(job_id: str) -> StreamingResponse:
        try:
            jobs.get(job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Job not found") from exc

        async def stream():
            last_revision = ""
            while True:
                try:
                    job = jobs.get(job_id)
                except KeyError:
                    return
                revision = f"{job['updated_at']}:{job['state']}:{len(job['stages'])}"
                if revision != last_revision:
                    last_revision = revision
                    yield (
                        f"id: {len(job['stages'])}\n"
                        "event: job\n"
                        f"data: {json.dumps(job, ensure_ascii=False)}\n\n"
                    )
                if job["state"] in {"completed", "inconclusive", "failed"}:
                    return
                await asyncio.sleep(0.5)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"X-Accel-Buffering": "no"},
        )

    @app.get("/api/history")
    async def history(limit: int = 12) -> dict[str, Any]:
        return {"runs": list_run_summaries(runtime.output_dir, limit=min(max(limit, 1), 50))}

    @app.get("/api/evidence/{run_id}")
    async def evidence_summary(run_id: str) -> dict[str, Any]:
        return summarize_run(_safe_run_dir(runtime, run_id))

    @app.get("/api/evidence/{run_id}/asset/{asset_path:path}")
    async def evidence_asset(run_id: str, asset_path: str) -> FileResponse:
        path = _safe_asset(runtime, run_id, asset_path)
        return FileResponse(path)

    @app.post("/api/evidence/{run_id}/download")
    async def evidence_download(
        run_id: str,
        x_faceproof_csrf: Annotated[str | None, Header()] = None,
    ) -> FileResponse:
        require_csrf(x_faceproof_csrf)
        run_dir = _safe_run_dir(runtime, run_id)
        temporary: tempfile.TemporaryDirectory[str] | None = None
        try:
            temporary, snapshot_run = await run_in_threadpool(_snapshot_run, run_dir)
            require_chain = (snapshot_run / "chain-receipt.json").is_file()
            verification = await run_in_threadpool(
                verify_run,
                snapshot_run,
                settings=runtime,
                require_chain=require_chain,
            )
        except (PipelineError, ValueError, OSError) as exc:
            raise HTTPException(status_code=409, detail="Evidence is not exportable") from exc
        if not verification.passed:
            raise HTTPException(
                status_code=409,
                detail="Evidence verification failed; refusing to export an invalid bundle",
            )
        manifest = json.loads((snapshot_run / "manifest.json").read_text(encoding="utf-8"))
        artifact_records = manifest.get("artifacts") if isinstance(manifest, dict) else None
        if not isinstance(artifact_records, list):
            raise HTTPException(status_code=409, detail="Evidence manifest is invalid")
        export_paths: list[Path] = []
        for record in artifact_records:
            logical_path = record.get("path") if isinstance(record, dict) else None
            if not isinstance(logical_path, str) or not logical_path:
                raise HTTPException(status_code=409, detail="Evidence manifest is invalid")
            path = (snapshot_run / Path(*logical_path.split("/"))).resolve()
            try:
                path.relative_to(snapshot_run)
            except ValueError as exc:
                raise HTTPException(status_code=409, detail="Unsafe evidence path") from exc
            if not path.is_file() or path.is_symlink():
                raise HTTPException(status_code=409, detail="Evidence artifact is unavailable")
            export_paths.append(path)
        for sidecar_name in (
            "manifest.json",
            "manifest.canonical.json",
            "commitment.json",
            "chain-receipt.json",
        ):
            sidecar = snapshot_run / sidecar_name
            if sidecar.is_file() and not sidecar.is_symlink():
                export_paths.append(sidecar)
        descriptor, archive_name = tempfile.mkstemp(prefix=f"faceproof-{run_id}-", suffix=".zip")
        os.close(descriptor)
        archive = Path(archive_name)
        try:
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
                for path in sorted(set(export_paths)):
                    bundle.write(
                        path,
                        arcname=f"{run_id}/{path.relative_to(snapshot_run).as_posix()}",
                    )
        except Exception:
            archive.unlink(missing_ok=True)
            raise
        finally:
            if temporary is not None:
                temporary.cleanup()
        return FileResponse(
            archive,
            filename=f"faceproof-{run_id}.zip",
            media_type="application/zip",
            background=BackgroundTask(archive.unlink, missing_ok=True),
        )

    @app.get("/api/evidence/{run_id}/public-receipt")
    async def public_receipt(run_id: str) -> Response:
        run_dir = _safe_run_dir(runtime, run_id)
        temporary: tempfile.TemporaryDirectory[str] | None = None
        try:
            temporary, snapshot_run, verification, receipt_present = await verified_anchor_snapshot(
                run_dir,
                forbid_pending=True,
            )
        except (PipelineError, ValueError, OSError) as exc:
            raise HTTPException(status_code=409, detail="Evidence is not exportable") from exc
        if not verification.passed:
            raise HTTPException(status_code=409, detail="Evidence verification failed")
        anchored = bool(
            receipt_present
            and getattr(verification, "chain", None) is not None
            and verification.chain.passed
        )
        if receipt_present and not anchored:
            raise HTTPException(status_code=409, detail="Blockchain verification failed")
        manifest = json.loads((snapshot_run / "manifest.json").read_text(encoding="utf-8"))
        summary = summarize_run(snapshot_run)
        metadata = manifest.get("metadata") if isinstance(manifest, dict) else {}
        query_face = metadata.get("query_face") if isinstance(metadata, dict) else {}
        models = query_face.get("model_fingerprints") if isinstance(query_face, dict) else {}
        selected = summary.get("selected") or {}
        search_summary = summary.get("search") or {}
        provider_mode = search_summary.get("provider_mode")
        provider_strategy = search_summary.get("provider_strategy")
        receipt = {
            "schema": "faceproof-public-receipt/v1",
            "project": "FaceProof",
            "run_id": run_id,
            "status": "anchored-verified" if anchored else "discovered-verified",
            "claim": (
                "The listed commitment binds the exact private evidence bundle; it does not "
                "prove legal identity, authorship, or truth."
            ),
            "observed_at": search_summary.get("retrieved_at"),
            "provider": search_summary.get("provider"),
            "provider_mode": provider_mode,
            "search_id": (
                None if provider_strategy == "bluesky" else search_summary.get("search_id")
            ),
            "live_provider_response": search_summary.get("live") is True,
            "live_no_cache": (provider_mode == "no-cache" if provider_strategy == "lens" else None),
            "local_similarity_micros": (
                round(float(selected["similarity"]) * 1_000_000)
                if selected.get("similarity") is not None
                else None
            ),
            "threshold_micros": (
                round(float(selected["threshold"]) * 1_000_000)
                if selected.get("threshold") is not None
                else None
            ),
            "linkage_level": selected.get("linkage_level"),
            "model_fingerprints": models,
            "manifest_sha256": (summary.get("integrity") or {}).get("manifest_sha256"),
            "commitment": (summary.get("integrity") or {}).get("commitment"),
            "artifact_count": (summary.get("integrity") or {}).get("artifact_count"),
            "chain": summary.get("chain"),
            "privacy_exclusions": [
                "raw face and candidate media",
                "biometric embedding values",
                "names, labels, post text, and URLs",
                "commitment salt and provider response",
            ],
        }
        try:
            payload = json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True).encode(
                "utf-8"
            )
            return Response(
                payload,
                media_type="application/json",
                headers={
                    "Content-Disposition": (
                        f'attachment; filename="faceproof-{run_id}-public.json"'
                    )
                },
            )
        finally:
            if temporary is not None:
                temporary.cleanup()

    @app.post("/api/evidence/{run_id}/verify")
    async def verify_evidence(
        run_id: str,
        x_faceproof_csrf: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        require_csrf(x_faceproof_csrf)
        run_dir = _safe_run_dir(runtime, run_id)
        try:
            result, anchor_pending, _ = await verify_stable_anchor_state(
                run_dir,
                forbid_pending=False,
            )
        except (PipelineError, ValueError, OSError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "passed": result.passed,
            "evidence_ok": result.evidence_ok,
            "canonical_sidecar_ok": result.canonical_sidecar_ok,
            "external_anchor_ok": result.external_anchor_ok,
            "errors": result.evidence_detail.get("errors", []),
            "scope": "chain-and-evidence" if result.chain else "evidence-only",
            "anchor_pending": anchor_pending,
            "chain": {
                "passed": result.chain.passed,
                "detail": result.chain.detail,
            }
            if result.chain
            else None,
        }

    @app.post("/api/evidence/{run_id}/recover-anchor")
    async def recover_anchor(
        run_id: str,
        x_faceproof_csrf: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        require_csrf(x_faceproof_csrf)
        run_dir = _safe_run_dir(runtime, run_id)
        try:
            recovered = await run_in_threadpool(
                recover_pending_anchor,
                run_dir,
                settings=runtime,
            )
        except (PipelineError, ValueError, OSError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {
            "recovered": True,
            "transaction_hash": recovered.receipt.transaction_hash,
            "block_number": recovered.receipt.block_number,
            "verification": recovered.verification.to_dict(),
        }

    @app.post("/api/evidence/{run_id}/tamper")
    async def tamper_evidence(
        run_id: str,
        x_faceproof_csrf: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        require_csrf(x_faceproof_csrf)
        run_dir = _safe_run_dir(runtime, run_id)
        try:
            changed_path, result = await run_in_threadpool(run_tamper_demo, run_dir)
        except (PipelineError, ValueError, OSError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "tamper_detected": not bool(result.get("ok")),
            "changed_temporary_copy": changed_path,
            "errors": result.get("errors", []),
        }

    return app


__all__ = ["JobStore", "create_app"]
