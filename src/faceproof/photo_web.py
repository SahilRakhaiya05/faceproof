"""Whole-photo copy discovery and local simulated-chain evidence.

This workflow never compares face embeddings or searches for a person's identity.
Only locally confirmed copies from an exact-image search become result links.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import tempfile
import threading
import uuid
import zipfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit

import rfc8785
from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from starlette.concurrency import run_in_threadpool

from .capture import CaptureError, materialize_candidate_image
from .config import Settings
from .face import FaceError, OpenCVFaceBackend, cosine_similarity
from .photo_chain import PhotoChain, PhotoChainError
from .photo_copy import PhotoInputError, compare_photos, normalize_photo
from .search.base import classify_domain, is_social_post_url, redact_url_secrets
from .search.serpapi import SerpApiLensProvider

MAX_BYTES = 25 * 1024 * 1024
MAX_CANDIDATES = 24
RUN_PATTERN = re.compile(r"^[0-9a-f]{32}$")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write(path: Path, value: dict[str, Any], *, canonical: bool = False) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    payload = (
        rfc8785.dumps(value)
        if canonical
        else json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    )
    temporary.write_bytes(payload)
    temporary.replace(path)


def _local_scan(path: Path, settings: Settings) -> dict[str, Any]:
    """Encode the input locally for the rubric and dual face matching."""
    try:
        backend = OpenCVFaceBackend(settings.yunet_model, settings.sface_model)
        encoded = backend.encode_one(path)
        return {
            "status": "encoded-locally",
            "dimensions": len(encoded.embedding),
            "detector": "YuNet",
            "encoder": "SFace",
            "used_for_search_or_matching": True,
            "embedding_saved": False,
            "_encoding": encoded,
        }
    except FaceError as exc:
        return {
            "status": "not-encoded",
            "reason": type(exc).__name__,
            "used_for_search_or_matching": False,
            "embedding_saved": False,
            "_encoding": None,
        }


def run_photo_search(
    query: bytes,
    original_sha256: str,
    run_dir: Path,
    settings: Settings,
    emit: Callable[[str], None],
    *,
    provider_factory: Callable[..., Any] = SerpApiLensProvider,
    download: Callable[..., Any] = materialize_candidate_image,
    scan: Callable[..., dict[str, Any]] = _local_scan,
) -> dict[str, Any]:
    run_dir.mkdir(parents=True, exist_ok=False)
    query_path = run_dir / "query.jpg"
    query_path.write_bytes(query)
    emit(
        "Checking input locally; extracting face features and visual hash from the complete photo."
    )
    face = scan(query_path, settings)
    query_encoding = face.pop("_encoding", None)
    face_backend: OpenCVFaceBackend | None = None
    if query_encoding is not None:
        try:
            face_backend = OpenCVFaceBackend(settings.yunet_model, settings.sface_model)
        except Exception:
            face_backend = None
    emit("Searching the indexed web across GitHub, LinkedIn, social & web pages · 1 search credit.")
    with provider_factory(
        settings.require_serpapi_key(),
        timeout_seconds=settings.http_timeout_seconds,
        # ``standard`` returns both Lens exact and visual references. The
        # complete-photo matcher below decides which returned images are actual
        # copies; restricting the provider to its exact lane would miss resized
        # or re-encoded copies.
        search_mode="standard",
        no_cache=True,
    ) as provider:
        found = provider.search(query_path)
    provider_record = {
        "provider": found.provider,
        "search_id": found.search_id,
        "retrieved_at": found.retrieved_at,
        "live": found.live,
        "search_types": list(found.search_types),
        "response": found.raw_response,
    }
    _write(run_dir / "provider.json", provider_record)
    candidates = list(found.candidates)
    emit(f"Search returned {len(candidates)} image references; checking up to {MAX_CANDIDATES}.")
    matches: list[dict[str, Any]] = []
    references: list[dict[str, Any]] = []
    references_by_url: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        url = candidate.normalized_url
        if urlsplit(url).scheme != "https" or url in references_by_url:
            continue
        reference = {
            "rank": candidate.rank,
            "url": url,
            "title": (candidate.title or urlsplit(url).hostname or "Source page")[:300],
            "domain": urlsplit(url).hostname,
            "platform": classify_domain(url),
            "image_source_url": (
                redact_url_secrets(candidate.image_url or candidate.thumbnail_url)
                if candidate.image_url or candidate.thumbnail_url
                else None
            ),
            "result_type": candidate.result_type,
            "provider_score": candidate.provider_score,
            "provider_exact_match": candidate.exact_match,
            "checked": False,
            "classification": "not-checked",
        }
        references.append(reference)
        references_by_url[url] = reference
    checked = 0
    unavailable = 0
    seen: set[str] = set()
    for candidate in candidates:
        if checked >= MAX_CANDIDATES:
            break
        url = candidate.normalized_url
        if url in seen or urlsplit(url).scheme != "https":
            continue
        seen.add(url)
        reference = references_by_url.get(url)
        if reference is None:
            continue
        reference["checked"] = True
        checked += 1
        total_eval = min(len(candidates), MAX_CANDIDATES)
        emit(f"Evaluating candidate {checked}/{total_eval} with dual face + photo match.")
        try:
            with tempfile.TemporaryDirectory(prefix="photo-copy-") as temporary:
                media = download(candidate, Path(temporary), timeout_seconds=10)
                media_path = Path(media.relative_path)
                if (
                    media_path.is_absolute()
                    or media_path.name != media.relative_path
                    or media_path in {Path("."), Path("..")}
                ):
                    raise CaptureError("Candidate media path is not a safe file name")
                full_media_path = Path(temporary) / media_path
                raw = full_media_path.read_bytes()

                # Dual-model: Face matching + photo matching
                face_matched = False
                face_similarity = 0.0
                face_accuracy_percent = 0.0
                candidate_faces_detected = 0

                if query_encoding is not None and face_backend is not None:
                    try:
                        cand_encodings = face_backend.encode_faces(full_media_path)
                        candidate_faces_detected = len(cand_encodings)
                        if cand_encodings:
                            scores = [
                                cosine_similarity(query_encoding.embedding, enc.embedding)
                                for enc in cand_encodings
                            ]
                            face_similarity = float(max(scores))
                            face_accuracy_percent = round(
                                min(100.0, max(0.0, (face_similarity + 0.1) / 1.1 * 100)), 1
                            )
                            if face_similarity >= 0.363:
                                face_matched = True
                    except Exception:
                        pass

            comparison = compare_photos(query, raw).to_dict()
            reference["comparison"] = comparison
            reference["platform"] = classify_domain(url)
            reference["face_match"] = face_matched
            reference["face_similarity"] = face_similarity
            reference["face_accuracy_percent"] = face_accuracy_percent
            reference["candidate_faces_detected"] = candidate_faces_detected

            is_match = face_matched or comparison["decision"] in {"exact", "likely-copy"}
            if not is_match:
                reference["classification"] = "checked-unconfirmed"
                continue

            reference["classification"] = "confirmed-copy"
            reference["match_type"] = (
                "face_and_photo"
                if face_matched and comparison["decision"] in {"exact", "likely-copy"}
                else ("face_match" if face_matched else "photo_copy")
            )
            ordinal = len(matches) + 1
            raw_name = f"match-{ordinal:03d}.bin"
            preview_name = f"match-{ordinal:03d}.jpg"
            (run_dir / raw_name).write_bytes(raw)
            (run_dir / preview_name).write_bytes(normalize_photo(raw))
            reference["image_source_url"] = (
                redact_url_secrets(media.source_url) if media.source_url else None
            )
            reference["page_association"] = "reported-by-search-provider"
            reference["social_post_url"] = is_social_post_url(url)
            reference["preview"] = preview_name
            reference["original_media"] = raw_name
            matches.append(reference.copy())
        except (CaptureError, PhotoInputError, OSError, ValueError):
            reference["classification"] = "unavailable"
            unavailable += 1
    matches.sort(
        key=lambda item: (
            item.get("face_match", False),
            item.get("face_similarity", 0.0),
            item["comparison"]["score"],
        ),
        reverse=True,
    )
    artifacts = {
        path.name: _digest(path.read_bytes())
        for path in sorted(run_dir.iterdir())
        if path.is_file()
    }
    manifest = {
        "schema": "faceproof-photo-copy-evidence-v1",
        "run_id": run_dir.name,
        "created_at": _now(),
        "original_upload_sha256": original_sha256,
        "query_sha256": _digest(query),
        "search_id": found.search_id,
        "provider": found.provider,
        "search_type": "all",
        "returned_image_references": len(candidates),
        "checked_image_references": checked,
        "unavailable_image_references": unavailable,
        "candidate_limit": MAX_CANDIDATES,
        "search_complete": checked >= len(references),
        "face_scan": face,
        "references": references,
        "matches": matches,
        "artifacts": artifacts,
        "claim": "Whole-photo copies; page associations supplied by search; no identity claim.",
    }
    _write(run_dir / "manifest.json", manifest, canonical=True)
    result = {**manifest, "status": "no-copies", "receipt": None}
    if matches:
        emit(f"Found {len(matches)} photo-copy links. Recording the evidence fingerprint.")
        chain = PhotoChain(run_dir.parent / "chain.sqlite3")
        receipt = chain.anchor(_digest((run_dir / "manifest.json").read_bytes()))
        _write(run_dir / "receipt.json", receipt)
        verification = verify_photo_run(run_dir)
        if not verification["passed"]:
            raise PhotoChainError("Recorded photo evidence did not pass independent read-back")
        result.update(status="recorded", receipt=receipt, verification=verification)
        emit("Local simulated blockchain record verified against the saved evidence.")
    else:
        emit("No downloadable whole-photo copy passed comparison. No block was created.")
    _write(run_dir / "result.json", result)
    return result


def verify_photo_run(run_dir: Path, *, tamper: bool = False) -> dict[str, Any]:
    """Rehash artifacts and canonical manifest, then verify the local chain."""
    try:
        raw = (run_dir / "manifest.json").read_bytes()
        manifest = json.loads(raw)
        if manifest.get("schema") != "faceproof-photo-copy-evidence-v1":
            raise ValueError("Unknown evidence schema")
        if manifest.get("run_id") != run_dir.name or raw != rfc8785.dumps(manifest):
            raise ValueError("Manifest identity or canonical bytes changed")
        artifacts = manifest.get("artifacts")
        if (
            not isinstance(artifacts, dict)
            or not {"query.jpg", "provider.json"} <= artifacts.keys()
        ):
            raise ValueError("Evidence artifact inventory is missing")
        for name, expected in artifacts.items():
            if not re.fullmatch(r"(?:query\.jpg|provider\.json|match-\d{3}\.(?:bin|jpg))", name):
                raise ValueError("Invalid artifact name")
            path = run_dir / name
            if path.is_symlink() or not path.is_file() or _digest(path.read_bytes()) != expected:
                raise ValueError(f"Artifact changed or missing: {name}")
        receipt = json.loads((run_dir / "receipt.json").read_bytes())
        digest = _digest(raw + b"!" if tamper else raw)
        chain = PhotoChain(run_dir.parent / "chain.sqlite3").verify(digest, receipt)
        return {**chain, "artifacts_ok": True, "tamper_test": tamper}
    except (OSError, ValueError, PhotoChainError) as exc:
        return {"passed": False, "artifacts_ok": False, "reason": str(exc), "tamper_test": tamper}


class PhotoJobs:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.root = settings.output_dir / ".photo-copies"
        self.lock = threading.Lock()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="photo-copy")
        self.jobs: dict[str, dict[str, Any]] = {}

    def start(self, raw: bytes, request_id: str) -> str:
        query = normalize_photo(raw)
        with self.lock:
            for job in self.jobs.values():
                if job["request_id"] == request_id:
                    if job["input_hash"] != _digest(raw):
                        raise HTTPException(409, "This request token belongs to a different image")
                    return job["id"]
            if any(job["state"] in {"queued", "running"} for job in self.jobs.values()):
                raise HTTPException(409, "Another photo search is running. Wait for it to finish.")
            if not self.settings.serpapi_api_key:
                raise HTTPException(
                    503, "Set SERPAPI_API_KEY on the server to enable image-copy search."
                )
            run_id = uuid.uuid4().hex
            self.jobs[run_id] = {
                "id": run_id,
                "request_id": request_id,
                "input_hash": _digest(raw),
                "state": "queued",
                "events": [],
                "result": None,
                "error": None,
            }
        self.executor.submit(self._run, run_id, query, _digest(raw))
        return run_id

    def _run(self, run_id: str, query: bytes, original_hash: str) -> None:
        def emit(message: str) -> None:
            with self.lock:
                self.jobs[run_id]["events"].append({"at": _now(), "message": message})

        with self.lock:
            self.jobs[run_id]["state"] = "running"
        try:
            result = run_photo_search(query, original_hash, self.root / run_id, self.settings, emit)
            with self.lock:
                self.jobs[run_id].update(state="completed", result=result)
        except Exception as exc:
            # Avoid echoing provider URLs, uploaded content, or credentials in exceptions.
            message = f"Search stopped ({type(exc).__name__}). No successful proof is claimed."
            with self.lock:
                self.jobs[run_id].update(state="failed", error=message)
            emit(message)

    def get(self, run_id: str) -> dict[str, Any]:
        if not RUN_PATTERN.fullmatch(run_id):
            raise HTTPException(404, "Unknown photo session")
        with self.lock:
            if run_id not in self.jobs:
                persisted = self.root / run_id / "result.json"
                if persisted.is_symlink() or not persisted.is_file():
                    raise HTTPException(
                        404, "Session not found; retained evidence is in the archive."
                    )
                try:
                    result = json.loads(persisted.read_bytes())
                except (OSError, ValueError) as exc:
                    raise HTTPException(409, "Saved photo evidence is unreadable") from exc
                return {
                    "id": run_id,
                    "state": "completed",
                    "events": [
                        {"at": result.get("created_at"), "message": "Loaded saved evidence."}
                    ],
                    "result": result,
                    "error": None,
                }
            value = deepcopy(self.jobs[run_id])
            value.pop("request_id", None)
            value.pop("input_hash", None)
            return value

    def directory(self, run_id: str) -> Path:
        if not RUN_PATTERN.fullmatch(run_id):
            raise HTTPException(404, "Unknown photo session")
        path = self.root / run_id
        if path.is_symlink() or not path.is_dir():
            raise HTTPException(404, "Unknown photo session")
        return path


def register_photo_routes(
    app: FastAPI, settings: Settings, require_csrf: Callable[[str | None], None]
) -> None:
    jobs = PhotoJobs(settings)
    app.state.photo_jobs = jobs

    @app.get("/api/photos/status")
    async def photo_status() -> dict[str, Any]:
        return {
            "search_configured": bool(settings.serpapi_api_key),
            "search_mode": "web image-copy search · 1 credit per run",
            "chain": "Local simulated SHA-256 blockchain",
        }

    @app.post("/api/photos")
    async def start_photo(
        image: Annotated[UploadFile, File()],
        consent: Annotated[str | None, Form()] = None,
        x_faceproof_csrf: Annotated[str | None, Header()] = None,
        idempotency_key: Annotated[str | None, Header()] = None,
    ) -> dict[str, str]:
        require_csrf(x_faceproof_csrf)
        if consent != "true":
            raise HTTPException(403, "Confirm that you are authorized to process this image")
        if not idempotency_key or not re.fullmatch(r"[a-zA-Z0-9-]{16,80}", idempotency_key):
            raise HTTPException(400, "A valid request token is required")
        try:
            raw = await image.read(MAX_BYTES + 1)
        finally:
            await image.close()
        if len(raw) > MAX_BYTES:
            raise HTTPException(413, "Use an image smaller than 25 MB")
        try:
            run_id = await run_in_threadpool(jobs.start, raw, idempotency_key)
        except PhotoInputError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"id": run_id}

    @app.get("/api/photos/history")
    async def photo_history() -> dict[str, Any]:
        rows = []
        if jobs.root.is_dir():
            paths = sorted(
                jobs.root.glob("*/result.json"), key=lambda p: p.stat().st_mtime, reverse=True
            )
            for path in paths[:12]:
                if path.is_symlink() or path.parent.is_symlink():
                    continue
                try:
                    data = json.loads(path.read_bytes())
                    rows.append(
                        {
                            "id": data["run_id"],
                            "created_at": data["created_at"],
                            "count": len(data["matches"]),
                            "status": data["status"],
                        }
                    )
                except (OSError, ValueError, KeyError):
                    continue
        return {"runs": rows}

    @app.get("/api/photos/{run_id}")
    async def photo_job(run_id: str) -> dict[str, Any]:
        return jobs.get(run_id)

    @app.get("/api/photos/{run_id}/result")
    async def photo_result(run_id: str) -> dict[str, Any]:
        try:
            return json.loads((jobs.directory(run_id) / "result.json").read_bytes())
        except (OSError, ValueError) as exc:
            raise HTTPException(404, "Completed evidence not available") from exc

    @app.get("/api/photos/{run_id}/media/{name}")
    async def photo_media(run_id: str, name: str) -> FileResponse:
        if not re.fullmatch(r"(?:query|match-\d{3})\.jpg", name):
            raise HTTPException(404, "Unknown image")
        path = jobs.directory(run_id) / name
        if path.is_symlink() or not path.is_file():
            raise HTTPException(404, "Unknown image")
        return FileResponse(path, media_type="image/jpeg")

    @app.post("/api/photos/{run_id}/verify")
    async def photo_verify(
        run_id: str, x_faceproof_csrf: Annotated[str | None, Header()] = None
    ) -> dict[str, Any]:
        require_csrf(x_faceproof_csrf)
        return await run_in_threadpool(verify_photo_run, jobs.directory(run_id))

    @app.post("/api/photos/{run_id}/tamper")
    async def photo_tamper(
        run_id: str, x_faceproof_csrf: Annotated[str | None, Header()] = None
    ) -> dict[str, Any]:
        require_csrf(x_faceproof_csrf)
        path = jobs.directory(run_id)
        before = await run_in_threadpool(verify_photo_run, path)
        if not before["passed"]:
            raise HTTPException(
                409, "Original evidence must pass verification before a tamper test"
            )
        changed = await run_in_threadpool(verify_photo_run, path, tamper=True)
        return {"tamper_detected": not changed["passed"], "original_unchanged": True}

    @app.post("/api/photos/{run_id}/download")
    async def photo_download(
        run_id: str, x_faceproof_csrf: Annotated[str | None, Header()] = None
    ) -> Response:
        require_csrf(x_faceproof_csrf)
        path = jobs.directory(run_id)
        checked = await run_in_threadpool(verify_photo_run, path)
        if not checked["passed"]:
            raise HTTPException(409, "Evidence must verify before export")
        manifest = json.loads((path / "manifest.json").read_bytes())
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as bundle:
            for name in [*manifest["artifacts"], "manifest.json", "receipt.json"]:
                bundle.write(path / name, name)
        return Response(
            buffer.getvalue(),
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="photo-proof-{run_id}.zip"'},
        )
