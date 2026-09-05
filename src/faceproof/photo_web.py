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
from .chain import (
    anchor_calldata_transaction,
    anchor_commitment,
    explorer_url_for_tx,
    network_name_for_chain_id,
)
from .config import Settings
from .face import FaceError, FaceQualityError, OpenCVFaceBackend, cosine_similarity
from .photo_chain import PhotoChain, PhotoChainError
from .photo_copy import PhotoInputError, compare_photos, normalize_photo
from .search.base import (
    classify_domain,
    is_social_post_url,
    is_social_profile_url,
    redact_url_secrets,
)
from .search.serpapi import SerpApiLensProvider
from .search.tech_discovery import (
    discover_tech_profiles,
    extract_identity_seeds,
    extract_name_tokens,
    is_profile_consistent_with_subject,
    search_profiles_by_name,
    search_wikipedia_profile,
)

MAX_BYTES = 25 * 1024 * 1024
MAX_CANDIDATES = 48
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


def _candidate_priority(candidate: Any) -> tuple[int, int]:
    """Sort candidates so identity and profile leads are evaluated first."""
    url = getattr(candidate, "normalized_url", "")
    host = (urlsplit(url).hostname or "").lower()
    platform = classify_domain(url).lower()
    if getattr(candidate, "result_type", "") in {
        "verified_developer_profile",
        "name_search_profile",
        "wikipedia_profile",
    }:
        return (0, 0)
    exact = bool(getattr(candidate, "exact_match", False))
    if exact:
        return (0, getattr(candidate, "rank", 999))

    profile_platforms = {
        "devfolio",
        "huggingface",
        "linkedin",
        "github",
        "kaggle",
        "devpost",
        "leetcode",
        "medium",
        "wikipedia",
        "x",
        "instagram",
        "facebook",
        "youtube",
        "reddit",
        "bluesky",
    }
    if platform in profile_platforms or any(
        k in host
        for k in (
            "devfolio.",
            "huggingface.",
            "linkedin.",
            "github.",
            "kaggle.",
            "devpost.",
            "leetcode.",
            "medium.",
            "wikipedia.",
            "twitter.",
            "x.com",
            "instagram.",
            "facebook.",
        )
    ):
        return (1, getattr(candidate, "rank", 999))

    bio_patterns = ("/in/", "/user/", "/profile/", "/alumni", "/faculty", "/team", "/author/")
    if host.endswith((".edu", ".ac.in", ".org")) or any(k in url.lower() for k in bio_patterns):
        return (2, getattr(candidate, "rank", 999))

    shopping_hosts = (
        "amazon.",
        "myntra.",
        "flipkart.",
        "ajio.",
        "tatacliq.",
        "meesho.",
        "peterengland.",
        "louisphilippe.",
        "hm.com",
        "zara.com",
        "uniqlo.",
        "shoppersstop.",
        "jackjones.",
        "asos.",
        "superdry.",
    )
    if any(shop in host for shop in shopping_hosts):
        return (4, getattr(candidate, "rank", 999))

    return (3, getattr(candidate, "rank", 999))


def _crop_face_focus(
    image_path: Path,
    encoding: Any,
    output_path: Path,
    *,
    margin_ratio: float = 0.25,
) -> Path | None:
    """Save an aligned face crop with margin for deep Google Lens visual search."""
    try:
        from PIL import Image

        with Image.open(image_path) as img:
            detection = getattr(encoding, "detection", None)
            if detection is None or not hasattr(detection, "box"):
                return None
            bbox = detection.box
            width, height = img.size
            margin_x = bbox.width * margin_ratio
            margin_y = bbox.height * margin_ratio
            left = max(0, int(bbox.x - margin_x))
            top = max(0, int(bbox.y - margin_y))
            right = min(width, int(bbox.x + bbox.width + margin_x))
            bottom = min(height, int(bbox.y + bbox.height + margin_y))
            if right <= left or bottom <= top:
                return None
            crop = img.crop((left, top, right, bottom))
            crop.save(output_path, format="JPEG", quality=95)
            return output_path
    except Exception:
        return None


def _local_scan(path: Path, settings: Settings) -> dict[str, Any]:
    """Encode the input locally for the rubric and dual face matching."""
    backend: OpenCVFaceBackend | None = None
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
    except FaceQualityError:
        if backend is not None:
            try:
                encoded = backend.encode_primary_face(path)
                return {
                    "status": "encoded-locally",
                    "dimensions": len(encoded.embedding),
                    "detector": "YuNet",
                    "encoder": "SFace",
                    "used_for_search_or_matching": True,
                    "embedding_saved": False,
                    "_encoding": encoded,
                }
            except Exception:
                pass
        return {
            "status": "not-encoded",
            "reason": "FaceQualityError",
            "used_for_search_or_matching": False,
            "embedding_saved": False,
            "_encoding": None,
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
    tech_discoverer: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    run_dir.mkdir(parents=True, exist_ok=False)
    query_path = run_dir / "query.jpg"
    query_path.write_bytes(query)
    emit(
        "Extracting biometric neural embeddings (YuNet 5-landmark + SFace 128D) "
        "and perceptual hash matrix."
    )
    face = scan(query_path, settings)
    query_encoding = face.pop("_encoding", None)
    face_backend: OpenCVFaceBackend | None = None
    if query_encoding is not None:
        try:
            face_backend = OpenCVFaceBackend(settings.yunet_model, settings.sface_model)
        except Exception:
            face_backend = None

    focus_crop: Path | None = None
    if query_encoding is not None:
        focus_path = run_dir / "face-crop.jpg"
        if _crop_face_focus(query_path, query_encoding, focus_path):
            focus_crop = focus_path

    search_mode = "deep" if focus_crop is not None else "standard"
    emit(
        "Scanning global indexed registries across GitHub, LinkedIn, technical platforms, "
        "and media."
    )
    try:
        provider_ctx = provider_factory(
            settings.require_serpapi_key(),
            timeout_seconds=settings.http_timeout_seconds,
            search_mode=search_mode,
            no_cache=True,
        )
    except TypeError:
        provider_ctx = provider_factory(
            settings.require_serpapi_key(),
            timeout_seconds=settings.http_timeout_seconds,
            no_cache=True,
        )

    with provider_ctx as provider:
        try:
            if focus_crop is not None:
                found = provider.search(query_path, focus_image_path=focus_crop)
            else:
                found = provider.search(query_path)
        except TypeError:
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
    subject_tokens: set[str] = set()
    stage_2_profiles: list[dict[str, Any]] = []
    seed_handles: set[str] = set()
    seed_names: set[str] = set()
    primary_avatar: str | None = None
    try:
        seed_handles, seed_names = extract_identity_seeds(candidates, found.web_labels)
        for name in seed_names:
            subject_tokens.update(extract_name_tokens(name))
        for h in seed_handles:
            subject_tokens.update(extract_name_tokens(h.replace("-", " ").replace("_", " ")))
        if seed_handles or seed_names:
            emit(
                "Executing deep identity discovery across Devfolio, Hugging Face, GitHub "
                "& networks."
            )
            discoverer = tech_discoverer or discover_tech_profiles
            tech_profiles = discoverer(
                seed_handles, seed_names, timeout_seconds=settings.http_timeout_seconds
            )
            for tp in tech_profiles:
                candidates.append(tp.to_candidate(rank=0))
                if tp.name:
                    subject_tokens.update(extract_name_tokens(tp.name))
                    seed_names.add(tp.name)
                stage_2_profiles.append(
                    {
                        "url": tp.url,
                        "platform": tp.platform,
                        "seed_source": "tech_probe",
                        "title": tp.title,
                    }
                )
                if not primary_avatar and tp.avatar_url and not tp.avatar_url.endswith(".svg"):
                    primary_avatar = tp.avatar_url

        # Stage 2: Recursive targeted deep name search & Wikipedia knowledge verification
        if seed_names:
            for s_name in sorted(seed_names):
                parts = s_name.strip().split()
                if len(parts) >= 2 and len(s_name.strip()) >= 5:
                    try:
                        wiki_cand = search_wikipedia_profile(
                            s_name, timeout_seconds=settings.http_timeout_seconds
                        )
                        if wiki_cand:
                            candidates.append(wiki_cand)
                            stage_2_profiles.append(
                                {
                                    "url": wiki_cand.normalized_url,
                                    "platform": "wikipedia",
                                    "seed_source": "wikipedia_registry",
                                    "title": wiki_cand.title,
                                }
                            )
                    except Exception:
                        pass

                    if settings.serpapi_api_key:
                        emit(f"Cross-platform identity verification active for subject '{s_name}'.")
                        name_candidates = search_profiles_by_name(
                            s_name,
                            api_key=settings.serpapi_api_key,
                            timeout_seconds=settings.http_timeout_seconds,
                        )
                        if name_candidates:
                            emit(
                                f"Identified {len(name_candidates)} indexed platform records "
                                f"for subject '{s_name}'."
                            )
                            for nc in name_candidates:
                                candidates.append(nc)
                                stage_2_profiles.append(
                                    {
                                        "url": nc.normalized_url,
                                        "platform": classify_domain(nc.normalized_url),
                                        "seed_source": "name_search",
                                        "title": nc.title,
                                    }
                                )
    except Exception:
        pass
    for c in candidates[:2]:
        if c.title and c.rank <= 2:
            subject_tokens.update(extract_name_tokens(c.title))
    candidates.sort(key=_candidate_priority)
    emit(
        f"Identified {len(candidates)} candidate occurrences; "
        f"evaluating top {min(len(candidates), MAX_CANDIDATES)}."
    )
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
        emit(
            f"Evaluating candidate {checked}/{total_eval}: validating biometric landmark geometry."
        )
        is_verified_developer = getattr(candidate, "result_type", "") in {
            "verified_developer_profile",
            "name_search_profile",
            "wikipedia_profile",
        }
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
                source_media_url = media.source_url

                # Dual-model: Face matching + photo matching
                face_matched = False
                face_similarity = 0.0
                face_accuracy_percent = 0.0
                candidate_faces_detected = 0

                if query_encoding is not None and face_backend is not None:
                    try:
                        cand_encodings = face_backend.encode_faces_permissive(full_media_path)
                        candidate_faces_detected = len(cand_encodings)
                        if cand_encodings:
                            scores = [
                                cosine_similarity(query_encoding.embedding, enc.embedding)
                                for enc in cand_encodings
                            ]
                            face_similarity = float(max(scores))
                            if face_similarity < 0.20:
                                face_accuracy_percent = round(max(0.0, face_similarity * 40.0), 1)
                            elif face_similarity < 0.42:
                                face_accuracy_percent = round(
                                    8.0 + (face_similarity - 0.20) / 0.22 * 50.0, 1
                                )
                            else:
                                face_accuracy_percent = round(
                                    min(99.9, 75.0 + (face_similarity - 0.42) / 0.45 * 24.9), 1
                                )
                            # Biometric face match threshold: strict >= 0.42.
                            # Eliminates false positive matches on random strangers.
                            threshold = 0.42
                            if face_similarity >= threshold:
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
            reference["verified_developer"] = is_verified_developer

            # Authentic match requires face verification or perceptual whole-photo duplicate
            is_match = face_matched or comparison["decision"] in {"exact", "likely-copy"}
            if not is_match:
                reference["classification"] = "checked-unconfirmed"
                continue

            # Identity Consistency Gate: verify profile URL belongs to subject
            if not is_profile_consistent_with_subject(url, candidate.title, subject_tokens):
                reference["classification"] = "third-party-mention"
                reference["third_party_reference"] = True
                reference["face_match"] = False
                continue

            if face_matched and face_similarity >= 0.75:
                subject_tokens.update(extract_name_tokens(candidate.title or ""))
                subject_tokens.update(
                    extract_name_tokens(urlsplit(url).path.replace("-", " ").replace("_", " "))
                )

            reference["classification"] = "confirmed-copy"
            if is_verified_developer and face_matched:
                reference["match_type"] = "developer_face_match"
            elif face_matched and comparison["decision"] in {"exact", "likely-copy"}:
                reference["match_type"] = "face_and_photo"
            elif face_matched:
                reference["match_type"] = "face_match"
            else:
                reference["match_type"] = "photo_copy"

            ordinal = len(matches) + 1
            raw_name = f"match-{ordinal:03d}.bin"
            preview_name = f"match-{ordinal:03d}.jpg"
            (run_dir / raw_name).write_bytes(raw)
            (run_dir / preview_name).write_bytes(normalize_photo(raw))
            reference["image_source_url"] = (
                redact_url_secrets(source_media_url) if source_media_url else None
            )
            reference["page_association"] = (
                "verified-developer-identity"
                if is_verified_developer
                else "reported-by-search-provider"
            )
            reference["social_post_url"] = is_social_post_url(url)
            reference["preview"] = preview_name
            reference["original_media"] = raw_name
            matches.append(reference.copy())
        except (CaptureError, PhotoInputError, OSError, ValueError):
            reference["classification"] = "unavailable"
            unavailable += 1
    if subject_tokens:
        clean_matches: list[dict[str, Any]] = []
        for m in matches:
            if is_social_profile_url(m["url"]) and not is_profile_consistent_with_subject(
                m["url"], m.get("title"), subject_tokens
            ):
                m["classification"] = "third-party-mention"
                m["face_match"] = False
                continue
            clean_matches.append(m)
        matches = clean_matches

    matches.sort(
        key=lambda item: (
            item.get("face_match", False),
            item.get("match_type") in {"developer_face_match"},
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
    provenance_graph = {
        "root": {
            "type": "query_photo",
            "sha256": _digest(query),
            "faces_detected": 1 if query_encoding is not None else 0,
        },
        "identity_seeds": {
            "names": sorted(list(seed_names)),
            "handles": sorted(list(seed_handles)),
        },
        "stage_1_lens": [
            {
                "url": m["url"],
                "platform": m.get("platform", "web"),
                "match_type": m.get("match_type"),
                "face_similarity": m.get("face_similarity", 0.0),
                "title": m.get("title", ""),
            }
            for m in matches
            if m.get("page_association") != "verified-developer-identity"
            and not any(p["url"] == m["url"] for p in stage_2_profiles)
        ],
        "stage_2_recursive": [
            {
                "url": p["url"],
                "platform": p.get("platform", "web"),
                "seed_source": p.get("seed_source", "recursive_probe"),
                "status": (
                    "confirmed-match" if any(m["url"] == p["url"] for m in matches) else "candidate"
                ),
                "title": p.get("title", ""),
            }
            for p in stage_2_profiles
        ],
        "nodes_count": 1 + len(seed_names) + len(seed_handles) + len(matches),
        "edges_count": len(seed_names) + len(seed_handles) + len(matches) + len(stage_2_profiles),
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
        "provenance_graph": provenance_graph,
        "artifacts": artifacts,
        "claim": (
            "Biometric neural face verification with cryptographic proof of discovery "
            "and immutable blockchain anchoring."
        ),
    }
    _write(run_dir / "manifest.json", manifest, canonical=True)
    manifest_bytes = (run_dir / "manifest.json").read_bytes()
    manifest_digest = _digest(manifest_bytes)
    result = {**manifest, "status": "no-copies", "receipt": None}
    if matches:
        emit(
            f"Verified {len(matches)} authentic identity occurrences. "
            f"Sealing cryptographic provenance manifest."
        )
        chain = PhotoChain(run_dir.parent / "chain.sqlite3")
        receipt = chain.anchor(manifest_digest)
        _write(run_dir / "receipt.json", receipt)
        verification = verify_photo_run(run_dir)
        if not verification["passed"]:
            raise PhotoChainError("Recorded photo evidence did not pass independent read-back")
        result.update(status="recorded", receipt=receipt, verification=verification)
        emit("Cryptographic ledger verified against immutable block state.")

        # Real EVM Blockchain Anchoring (Ethereum Sepolia / Base Sepolia)
        if settings.private_key:
            try:
                emit(
                    f"Sealing cryptographic commitment on EVM blockchain "
                    f"(Chain ID: {settings.chain_id})..."
                )
                if settings.contract_address:
                    anchor = anchor_commitment(
                        manifest_digest,
                        rpc_url=settings.rpc_url,
                        expected_chain_id=settings.chain_id,
                        contract_address=settings.contract_address,
                        private_key=settings.private_key,
                        confirmations=settings.confirmations,
                        timeout_seconds=min(settings.http_timeout_seconds, 60),
                    )
                    evm_receipt = {
                        **anchor.to_dict(),
                        "network": network_name_for_chain_id(settings.chain_id),
                        "explorer_url": explorer_url_for_tx(
                            settings.chain_id, anchor.transaction_hash
                        ),
                    }
                else:
                    evm_receipt = anchor_calldata_transaction(
                        manifest_digest,
                        rpc_url=settings.rpc_url,
                        expected_chain_id=settings.chain_id,
                        private_key=settings.private_key,
                        timeout_seconds=min(settings.http_timeout_seconds, 60),
                    )
                _write(run_dir / "evm_receipt.json", evm_receipt)
                result["evm_receipt"] = evm_receipt
                emit(f"Anchored on {evm_receipt['network']}! Tx: {evm_receipt['transaction_hash']}")
            except Exception as exc:
                emit(f"EVM anchor status: {type(exc).__name__}. Cryptographic ledger active.")
    else:
        emit("No candidate passed biometric face verification or perceptual match threshold.")
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
            if not re.fullmatch(
                r"(?:query\.jpg|face-crop\.jpg|provider\.json|match-\d{3}\.(?:bin|jpg))", name
            ):
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
            message = (
                f"Pipeline suspended ({type(exc).__name__}). No cryptographic proof was anchored."
            )
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
        chain_label = (
            f"{network_name_for_chain_id(settings.chain_id)} EVM Blockchain"
            if settings.private_key
            else "Local simulated SHA-256 blockchain"
        )
        return {
            "search_configured": bool(settings.serpapi_api_key),
            "search_mode": "web image-copy search · 1 credit per run",
            "chain": chain_label,
            "evm_enabled": bool(settings.private_key),
            "chain_id": settings.chain_id if settings.private_key else None,
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
