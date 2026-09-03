from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import platform
import re
import secrets
import shutil
import struct
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .capture import (
    CapturedFile,
    CaptureError,
    PostCapture,
    capture_public_post,
    materialize_candidate_image,
)
from .chain import (
    AnchorPendingError,
    AnchorReceipt,
    ChainError,
    ChainVerification,
    anchor_commitment,
    explorer_transaction_url,
    make_web3,
    recover_anchor_receipt,
    verify_commitment_on_chain,
)
from .config import Settings
from .evidence import (
    COMMITMENT_SCHEME,
    build_manifest,
    canonical_manifest_bytes,
    compute_commitment,
    verify_manifest,
)
from .face import (
    DEFAULT_COSINE_THRESHOLD,
    FaceEncoding,
    FaceError,
    OpenCVFaceBackend,
    cosine_similarity,
)
from .model_assets import verify_default_models
from .provenance import ProvenanceError, verify_git_source_revision
from .search import (
    PROFILE_LEAD_PLATFORMS,
    SUPPORTED_PLATFORMS,
    SearchCandidate,
    SearchError,
    SearchRun,
    SerpApiLensProvider,
    filter_profile_candidates,
    filter_social_candidates,
    normalize_page_url,
    platform_name,
)
from .search.base import redact_url_secrets


class PipelineError(RuntimeError):
    """Base error for an incomplete or invalid end-to-end run."""


class InconclusiveError(PipelineError):
    """Raised when live search returns no independently matching social result."""

    def __init__(self, message: str, run_dir: Path) -> None:
        self.run_dir = run_dir
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class CandidateEvaluation:
    candidate: SearchCandidate
    directory: Path
    media_path: Path
    media_artifact: CapturedFile
    local_similarity: float
    detected_faces: int
    matched_face: FaceEncoding
    matched: bool

    @property
    def local_similarity_micros(self) -> int:
        return round(self.local_similarity * 1_000_000)


@dataclass(frozen=True, slots=True)
class PipelineResult:
    run_id: str
    run_dir: Path
    provider: str
    search_id: str
    selected_url: str
    selected_title: str | None
    selected_source: str | None
    selected_media_path: Path
    web_labels: tuple[str, ...]
    local_similarity: float
    similarity_threshold: float
    manifest_sha256: str
    commitment: str
    chain_receipt: AnchorReceipt | None
    chain_verification: ChainVerification | None

    @property
    def explorer_url(self) -> str | None:
        if self.chain_receipt is None:
            return None
        return explorer_transaction_url(
            self.chain_receipt.chain_id, self.chain_receipt.transaction_hash
        )


@dataclass(frozen=True, slots=True)
class RecoveredAnchorResult:
    receipt: AnchorReceipt
    verification: ChainVerification


@dataclass(frozen=True, slots=True)
class LocalVerificationResult:
    evidence_ok: bool
    evidence_detail: dict[str, Any]
    canonical_sidecar_ok: bool
    chain: ChainVerification | None
    external_anchor_ok: bool | None = None

    @property
    def passed(self) -> bool:
        return (
            self.evidence_ok
            and self.canonical_sidecar_ok
            and self.external_anchor_ok is not False
            and (self.chain is None or self.chain.passed)
        )


def run_pipeline(
    *,
    image_path: Path,
    settings: Settings,
    consent_acknowledged: bool,
    consent_reference: str | None = None,
    live: bool,
    skip_anchor: bool = False,
    threshold: float = DEFAULT_COSINE_THRESHOLD,
    max_candidates: int = 6,
    max_profile_candidates: int = 0,
    profile_discovery_authorized: bool = False,
    profile_platforms: frozenset[str] | None = None,
    search_mode: str = "standard",
    platforms: frozenset[str] | None = None,
    approved_post_url: str | None = None,
    review_run_id: str | None = None,
    review_manifest_sha256: str | None = None,
    review_commitment: str | None = None,
    output_dir: Path | None = None,
    on_stage: Callable[[str], None] | None = None,
) -> PipelineResult:
    if not consent_acknowledged:
        raise PipelineError("Explicit consent acknowledgement is required")
    normalized_consent_reference = (consent_reference or "").strip() or None
    if normalized_consent_reference and len(normalized_consent_reference) > 128:
        raise PipelineError("Consent reference must be at most 128 characters")
    if not live:
        raise PipelineError(
            "A pipeline run requires explicit --live acknowledgement; fixtures are test-only"
        )
    if not -1 <= threshold <= 1:
        raise PipelineError("Face similarity threshold must be between -1 and 1")
    if not skip_anchor and threshold < DEFAULT_COSINE_THRESHOLD:
        raise PipelineError(
            f"Anchored runs require a threshold of at least {DEFAULT_COSINE_THRESHOLD:g}"
        )
    if max_candidates <= 0:
        raise PipelineError("max_candidates must be positive")
    if max_profile_candidates < 0:
        raise PipelineError("max_profile_candidates cannot be negative")
    if max_profile_candidates and not profile_discovery_authorized:
        raise PipelineError("Public profile discovery requires an explicit consent acknowledgement")
    if search_mode not in {"standard", "deep"}:
        raise PipelineError("Search mode must be 'standard' or 'deep'")
    normalized_platforms = (
        frozenset(item.strip().casefold() for item in platforms if item.strip())
        if platforms is not None
        else None
    )
    if platforms is not None and not normalized_platforms:
        raise PipelineError("At least one platform is required when filtering results")
    unsupported_platforms = (normalized_platforms or frozenset()) - SUPPORTED_PLATFORMS
    if unsupported_platforms:
        raise PipelineError(
            "Unsupported platform filter: " + ", ".join(sorted(unsupported_platforms))
        )
    normalized_profile_platforms = (
        frozenset(item.strip().casefold() for item in profile_platforms if item.strip())
        if profile_platforms is not None
        else (PROFILE_LEAD_PLATFORMS if max_profile_candidates else None)
    )
    if profile_platforms is not None and not normalized_profile_platforms:
        raise PipelineError("At least one profile platform is required for profile discovery")
    unsupported_profile_platforms = (
        normalized_profile_platforms or frozenset()
    ) - PROFILE_LEAD_PLATFORMS
    if unsupported_profile_platforms:
        raise PipelineError(
            "Unsupported profile platform: " + ", ".join(sorted(unsupported_profile_platforms))
        )
    approved_normalized: str | None = None
    normalized_review_run_id = (review_run_id or "").strip() or None
    if normalized_review_run_id and (
        len(normalized_review_run_id) > 128
        or any(
            character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
            for character in normalized_review_run_id
        )
    ):
        raise PipelineError("Review run ID is invalid")
    normalized_review_manifest = (review_manifest_sha256 or "").strip() or None
    normalized_review_commitment = (review_commitment or "").strip() or None
    review_values = (normalized_review_manifest, normalized_review_commitment)
    if any(review_values) and not all(review_values):
        raise PipelineError("Review evidence requires both manifest hash and commitment")
    for label, value in (
        ("review manifest hash", normalized_review_manifest),
        ("review commitment", normalized_review_commitment),
    ):
        if value and not re.fullmatch(r"0x[0-9a-fA-F]{64}", value):
            raise PipelineError(f"{label} must be a bytes32 hexadecimal value")
    if approved_post_url:
        try:
            approved_normalized = normalize_page_url(approved_post_url)
        except ValueError as exc:
            raise PipelineError(f"Approved post URL is invalid: {exc}") from exc
    if not skip_anchor and not approved_normalized:
        raise PipelineError(
            "Anchoring requires --approve-post-url after a human reviews the discovered post"
        )
    if not skip_anchor and not normalized_consent_reference:
        raise PipelineError(
            "Anchoring requires a non-sensitive --consent-reference for the authorized demo"
        )
    try:
        settings.require_serpapi_key()
        if not skip_anchor:
            settings.require_chain_write()
            if not settings.contract_code_hash:
                raise PipelineError(
                    "FACEPROOF_CONTRACT_CODE_HASH is required for an anchored evidence claim"
                )
            verify_git_source_revision(settings.source_revision)
    except ProvenanceError as exc:
        raise PipelineError(str(exc)) from exc
    except ValueError as exc:
        raise PipelineError(str(exc)) from exc

    model_results = verify_default_models(settings.model_dir)
    invalid_models = [
        f"{filename}: {status}" for filename, status in model_results.items() if status != "ok"
    ]
    if invalid_models:
        raise PipelineError(
            "Pinned face-model integrity check failed: " + "; ".join(invalid_models)
        )

    source = Path(image_path)
    if not source.is_file():
        raise PipelineError(f"Input image does not exist: {source}")
    if source.stat().st_size > 25 * 1024 * 1024:
        raise PipelineError("Input image exceeds the 25MB safety limit")

    run_id = _new_run_id()
    base_output = Path(output_dir or settings.output_dir)
    run_dir = base_output / run_id
    run_dir.mkdir(parents=True, exist_ok=False)

    input_dir = run_dir / "input"
    input_dir.mkdir()
    suffix = source.suffix.lower() if source.suffix else ".img"
    query_path = input_dir / f"query{suffix}"
    query_path.write_bytes(source.read_bytes())

    _stage(on_stage, "Detecting, quality-checking, and encoding the query face")
    backend = OpenCVFaceBackend(settings.yunet_model, settings.sface_model)
    try:
        query_encoding = backend.encode_one(query_path)
    except FaceError as exc:
        _write_json(run_dir / "run-error.json", {"stage": "face", "error": str(exc)})
        raise PipelineError(str(exc)) from exc
    _save_face_preview(query_path, query_encoding, input_dir / "detected-face.jpg")
    _save_face_crop(query_path, query_encoding, input_dir / "face-crop.jpg")
    _write_json(input_dir / "face-encoding.json", _face_metadata(query_encoding))
    _write_json(
        input_dir / "consent.json",
        {
            "acknowledged": True,
            "reference": normalized_consent_reference,
            "scope": "face-search-and-public-post-evidence",
            "profile_discovery_authorized": profile_discovery_authorized,
            "profile_platforms": (
                sorted(normalized_profile_platforms)
                if profile_discovery_authorized and normalized_profile_platforms
                else []
            ),
            "statement": "Operator attestation; FaceProof does not independently prove consent.",
        },
    )

    # Lens receives the full scan because surrounding visual context materially
    # improves exact/cropped/repost retrieval. The detected crop and embedding
    # remain hashed evidence and every returned candidate is rechecked locally.
    search_image_path = query_path
    search_query_strategy = "full-input-face-scan"

    provider = _make_provider(
        settings=settings,
        live=live,
        search_mode=search_mode,
    )
    _stage(on_stage, "Running genuine live Google Lens search through SerpApi")
    try:
        with provider:
            search_run = provider.search(search_image_path)
    except (SearchError, OSError) as exc:
        _write_json(run_dir / "run-error.json", {"stage": "search", "error": str(exc)})
        raise PipelineError(f"Search failed: {exc}") from exc
    if not search_run.live or search_run.provider != "serpapi":
        raise PipelineError("Search provider did not return a verifiable live production run")

    search_dir = run_dir / "search"
    search_dir.mkdir()
    _write_search_record(search_dir / "provider-response.json", search_run)
    _stage(
        on_stage,
        f"Search {search_run.search_id} returned {len(search_run.candidates)} candidates",
    )
    profile_evaluations: list[CandidateEvaluation] = []
    profile_candidates = filter_profile_candidates(
        search_run.candidates,
        limit=max_profile_candidates,
        platforms=normalized_profile_platforms,
    )

    social_candidates = filter_social_candidates(
        search_run.candidates,
        limit=max_candidates,
        platforms=normalized_platforms,
    )
    if not social_candidates:
        profile_evaluations = _evaluate_profile_leads(
            profile_candidates=profile_candidates,
            run_dir=run_dir,
            backend=backend,
            query_encoding=query_encoding,
            settings=settings,
            threshold=threshold,
            on_stage=on_stage,
        )
        _write_json(
            run_dir / "run-error.json",
            {
                "stage": "social-filter",
                "error": "No public social-media candidate was returned",
                "search_id": search_run.search_id,
                "profile_leads": len(profile_candidates),
                "face_matched_profile_leads": sum(item.matched for item in profile_evaluations),
                "platform_filter": sorted(normalized_platforms) if normalized_platforms else [],
            },
        )
        raise InconclusiveError("Live search returned no public social-media candidate", run_dir)

    evaluations: list[CandidateEvaluation] = []
    candidates_dir = run_dir / "candidates"
    candidates_dir.mkdir()
    for ordinal, candidate in enumerate(social_candidates, start=1):
        _stage(
            on_stage,
            f"Independently re-matching social candidate {ordinal}/{len(social_candidates)}",
        )
        candidate_dir = candidates_dir / f"{ordinal:02d}"
        candidate_dir.mkdir()
        try:
            media_artifact = materialize_candidate_image(
                candidate,
                candidate_dir,
                timeout_seconds=settings.http_timeout_seconds,
            )
            media_path = candidate_dir / media_artifact.relative_path
            encodings = backend.encode_faces(media_path)
            scored_faces = [
                (cosine_similarity(query_encoding.embedding, item.embedding), item)
                for item in encodings
            ]
            similarity, matched_face = max(
                scored_faces,
                key=lambda scored: scored[0],
            )
            matched = similarity >= threshold
            evaluation = CandidateEvaluation(
                candidate=candidate,
                directory=candidate_dir,
                media_path=media_path,
                media_artifact=media_artifact,
                local_similarity=similarity,
                detected_faces=len(encodings),
                matched_face=matched_face,
                matched=matched,
            )
            evaluations.append(evaluation)
            _save_face_preview(
                media_path,
                matched_face,
                candidate_dir / "matched-face-preview.jpg",
            )
            _write_json(
                candidate_dir / "assessment.json",
                _candidate_assessment(evaluation, threshold=threshold),
            )
        except (CaptureError, FaceError) as exc:
            _write_json(
                candidate_dir / "assessment.json",
                {
                    "candidate": candidate.public_dict(),
                    "status": "rejected",
                    "error": str(exc),
                },
            )

    matched = [evaluation for evaluation in evaluations if evaluation.matched]
    if not matched:
        profile_evaluations = _evaluate_profile_leads(
            profile_candidates=profile_candidates,
            run_dir=run_dir,
            backend=backend,
            query_encoding=query_encoding,
            settings=settings,
            threshold=threshold,
            on_stage=on_stage,
        )
        _write_json(
            run_dir / "run-error.json",
            {
                "stage": "local-rematch",
                "error": "No social candidate passed independent local face matching",
                "threshold_micros": round(threshold * 1_000_000),
                "evaluated": len(evaluations),
                "profile_leads": len(profile_candidates),
                "face_matched_profile_leads": sum(item.matched for item in profile_evaluations),
            },
        )
        raise InconclusiveError(
            "No social candidate passed independent local face matching", run_dir
        )

    ordered_matches = sorted(
        matched,
        key=lambda item: (-item.local_similarity, item.candidate.rank),
    )
    if approved_normalized:
        ordered_matches = [
            item for item in ordered_matches if item.candidate.normalized_url == approved_normalized
        ]
        if not ordered_matches:
            profile_evaluations = _evaluate_profile_leads(
                profile_candidates=profile_candidates,
                run_dir=run_dir,
                backend=backend,
                query_encoding=query_encoding,
                settings=settings,
                threshold=threshold,
                on_stage=on_stage,
            )
            raise InconclusiveError(
                "The human-approved post was not returned and independently matched in this run",
                run_dir,
            )

    selected: CandidateEvaluation | None = None
    post_capture: PostCapture | None = None
    linkage_level: str | None = None
    post_media_similarity: float | None = None
    for candidate_match in ordered_matches:
        _stage(on_stage, "Capturing and validating the public post permalink")
        capture = capture_public_post(
            candidate_match.candidate,
            candidate_match.directory,
            timeout_seconds=settings.http_timeout_seconds,
        )
        if capture.status != "captured" or not capture.metadata.get("post_identity_verified"):
            continue

        if capture.media_artifacts:
            similarities: list[float] = []
            for artifact in capture.media_artifacts:
                try:
                    encodings = backend.encode_faces(
                        candidate_match.directory / artifact.relative_path
                    )
                except FaceError:
                    continue
                similarities.extend(
                    cosine_similarity(query_encoding.embedding, item.embedding)
                    for item in encodings
                )
            if not similarities or max(similarities) < threshold:
                continue
            post_media_similarity = max(similarities)
            linkage_level = "captured-post-media-rematched"
        else:
            if not skip_anchor:
                # A provider thumbnail associated with a confirmed permalink is
                # useful discovery evidence, but it is not strong enough for a
                # submission-mode on-chain claim about the post's own media.
                continue
            linkage_level = "provider-result-associated-post-independently-confirmed"

        selected = candidate_match
        post_capture = capture
        break

    if selected is None or post_capture is None or linkage_level is None:
        profile_evaluations = _evaluate_profile_leads(
            profile_candidates=profile_candidates,
            run_dir=run_dir,
            backend=backend,
            query_encoding=query_encoding,
            settings=settings,
            threshold=threshold,
            on_stage=on_stage,
        )
        _write_json(
            run_dir / "run-error.json",
            {
                "stage": "post-validation",
                "error": "No face-matched permalink could be independently captured and validated",
                "matched_candidates": len(ordered_matches),
            },
        )
        raise InconclusiveError(
            "No face-matched social post could be independently captured and validated",
            run_dir,
        )

    profile_evaluations = _evaluate_profile_leads(
        profile_candidates=profile_candidates,
        run_dir=run_dir,
        backend=backend,
        query_encoding=query_encoding,
        settings=settings,
        threshold=threshold,
        on_stage=on_stage,
    )

    selection_record = {
        "status": "selected",
        "candidate": selected.candidate.public_dict(),
        "candidate_media": selected.media_artifact.to_dict(),
        "local_similarity_micros": selected.local_similarity_micros,
        "threshold_micros": round(threshold * 1_000_000),
        "capture_status": post_capture.status,
        "capture_method": post_capture.method,
        "linkage_level": linkage_level,
        "post_media_similarity_micros": (
            round(post_media_similarity * 1_000_000) if post_media_similarity is not None else None
        ),
        "human_review": {
            "approved_for_anchor": bool(approved_normalized),
            "approved_post_url": approved_normalized,
            "discovery_run_id": normalized_review_run_id,
            "discovery_manifest_sha256": normalized_review_manifest,
            "discovery_commitment": normalized_review_commitment,
            "recorded_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        },
    }
    _write_json(run_dir / "selection.json", selection_record)

    artifacts, media_types = _collect_pre_anchor_artifacts(run_dir)
    _stage(on_stage, "Canonicalizing and hashing the evidence bundle")
    metadata = _manifest_metadata(
        query_encoding=query_encoding,
        search_run=search_run,
        selected=selected,
        capture=post_capture,
        threshold=threshold,
        search_query_strategy=search_query_strategy,
        linkage_level=linkage_level,
        post_media_similarity=post_media_similarity,
        approved_post_url=approved_normalized,
        consent_reference=normalized_consent_reference,
        source_revision=settings.source_revision,
        search_mode=search_mode,
        platforms=normalized_platforms,
        profile_evaluations=profile_evaluations,
        profile_candidate_count=len(profile_candidates),
        profile_discovery_authorized=profile_discovery_authorized,
        profile_platforms=normalized_profile_platforms,
        review_run_id=normalized_review_run_id,
        review_manifest_sha256=normalized_review_manifest,
        review_commitment=normalized_review_commitment,
        max_candidates=max_candidates,
        max_profile_candidates=max_profile_candidates,
    )
    manifest = build_manifest(
        artifacts,
        run_id=run_id,
        observed_at=search_run.retrieved_at,
        metadata=metadata,
        media_types=media_types,
    )
    canonical = canonical_manifest_bytes(manifest)
    commitment_record = compute_commitment(manifest)
    _write_json(run_dir / "manifest.json", manifest)
    _write_bytes_atomic(run_dir / "manifest.canonical.json", canonical)
    _write_json(run_dir / "commitment.json", commitment_record)

    chain_receipt: AnchorReceipt | None = None
    chain_verification: ChainVerification | None = None
    if not skip_anchor:
        _stage(on_stage, "Anchoring the salted commitment on the configured blockchain")
        contract_address, private_key = settings.require_chain_write()
        pending_path = anchor_submission_path(run_dir)
        try:
            chain_receipt = anchor_commitment(
                commitment_record["commitment"],
                rpc_url=settings.rpc_url,
                expected_chain_id=settings.chain_id,
                contract_address=contract_address,
                private_key=private_key,
                confirmations=settings.confirmations,
                expected_code_hash=settings.contract_code_hash,
                on_submission=lambda submission: _write_json(pending_path, submission.to_dict()),
            )
        except AnchorPendingError as exc:
            raise PipelineError(
                "Blockchain transaction outcome is pending recovery; do not submit a new anchor. "
                f"Recover the saved transaction {exc.submission.transaction_hash}."
            ) from exc
        except ChainError as exc:
            pending_path.unlink(missing_ok=True)
            _write_json(
                run_dir / "run-error.json",
                {"stage": "blockchain", "error": str(exc)},
            )
            raise PipelineError(f"Blockchain anchoring failed: {exc}") from exc

        receipt_dict = chain_receipt.to_dict()
        _write_json(run_dir / "chain-receipt.json", receipt_dict)
        _stage(on_stage, "Reading chain state back through an independent verification call")
        try:
            chain_verification = verify_commitment_on_chain(
                commitment_record["commitment"],
                rpc_url=settings.rpc_url,
                expected_chain_id=settings.chain_id,
                contract_address=contract_address,
                receipt=receipt_dict,
                required_confirmations=settings.confirmations,
                expected_code_hash=settings.contract_code_hash,
            )
        except ChainError as exc:
            raise PipelineError(
                "Blockchain receipt was recorded but independent verification is pending; "
                "recover the saved transaction instead of submitting again."
            ) from exc
        if not chain_verification.passed:
            raise PipelineError(
                "Transaction was mined, but independent verification is pending; recover the "
                "saved transaction instead of submitting again."
            )
        pending_path.unlink(missing_ok=True)

    return PipelineResult(
        run_id=run_id,
        run_dir=run_dir,
        provider=search_run.provider,
        search_id=search_run.search_id,
        selected_url=selected.candidate.normalized_url,
        selected_title=selected.candidate.title,
        selected_source=selected.candidate.source,
        selected_media_path=selected.media_path,
        web_labels=search_run.web_labels,
        local_similarity=selected.local_similarity,
        similarity_threshold=threshold,
        manifest_sha256=commitment_record["manifest_sha256"],
        commitment=commitment_record["commitment"],
        chain_receipt=chain_receipt,
        chain_verification=chain_verification,
    )


def anchor_submission_path(run_dir: Path) -> Path:
    """Return the out-of-bundle journal used for exact-hash recovery."""
    resolved = Path(run_dir).resolve()
    return resolved.parent / f".{resolved.name}.anchor-submission.json"


def recover_pending_anchor(run_dir: Path, *, settings: Settings) -> RecoveredAnchorResult:
    """Recover one persisted anchor transaction without signing or rebroadcasting."""
    run_dir = Path(run_dir).resolve()
    if not run_dir.is_dir():
        raise PipelineError("Evidence run directory does not exist")
    pending_path = anchor_submission_path(run_dir)
    if not pending_path.is_file() or pending_path.is_symlink():
        raise PipelineError("No recoverable anchor submission exists for this run")
    manifest = _read_json(run_dir / "manifest.json")
    commitment_record = _read_json(run_dir / "commitment.json")
    commitment = str(commitment_record.get("commitment") or "")
    detail = verify_manifest(
        manifest,
        run_dir,
        salt=commitment_record.get("salt"),
        expected_commitment=commitment,
        expected_manifest_sha256=commitment_record.get("manifest_sha256"),
    )
    try:
        canonical_sidecar = (run_dir / "manifest.canonical.json").read_bytes()
    except OSError as exc:
        raise PipelineError("Canonical manifest sidecar is unavailable") from exc
    if not detail.get("ok") or not secrets.compare_digest(
        canonical_sidecar, canonical_manifest_bytes(manifest)
    ):
        raise PipelineError("Evidence changed after the anchor transaction was prepared")

    submission_record = _read_json(pending_path)
    contract_address, private_key = settings.require_chain_write()
    if not settings.contract_code_hash:
        raise PipelineError("A trusted contract code hash is required for anchor recovery")
    try:
        expected_submitter = (
            make_web3(
                settings.rpc_url,
                timeout_seconds=min(settings.http_timeout_seconds, 30),
            )
            .eth.account.from_key(private_key)
            .address
        )
        receipt = recover_anchor_receipt(
            commitment,
            submission=submission_record,
            rpc_url=settings.rpc_url,
            expected_chain_id=settings.chain_id,
            contract_address=contract_address,
            expected_submitter=expected_submitter,
            confirmations=settings.confirmations,
            timeout_seconds=settings.http_timeout_seconds,
            expected_code_hash=settings.contract_code_hash,
        )
        receipt_dict = receipt.to_dict()
        _write_json(run_dir / "chain-receipt.json", receipt_dict)
        verification = verify_commitment_on_chain(
            commitment,
            rpc_url=settings.rpc_url,
            expected_chain_id=settings.chain_id,
            contract_address=contract_address,
            receipt=receipt_dict,
            required_confirmations=settings.confirmations,
            timeout_seconds=settings.http_timeout_seconds,
            expected_code_hash=settings.contract_code_hash,
        )
    except (ChainError, ValueError) as exc:
        raise PipelineError(f"Anchor recovery is still pending: {exc}") from exc
    if not verification.passed:
        raise PipelineError("Recovered receipt did not pass independent on-chain verification")
    pending_path.unlink(missing_ok=True)
    return RecoveredAnchorResult(receipt=receipt, verification=verification)


def verify_run(
    run_dir: Path,
    *,
    settings: Settings,
    require_chain: bool = True,
    expected_commitment: str | None = None,
    expected_transaction_hash: str | None = None,
) -> LocalVerificationResult:
    run_dir = Path(run_dir)
    try:
        manifest = _read_json(run_dir / "manifest.json")
        commitment = _read_json(run_dir / "commitment.json")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise PipelineError(f"Cannot read evidence sidecars: {exc}") from exc

    expected_commitment_fields = {
        "scheme",
        "manifest_sha256",
        "salt",
        "commitment",
        "abi_backend",
        "keccak_backend",
    }
    if set(commitment) != expected_commitment_fields or not all(
        isinstance(value, str) for value in commitment.values()
    ):
        raise PipelineError("Malformed commitment sidecar")
    if commitment.get("scheme") != COMMITMENT_SCHEME:
        raise PipelineError("Unsupported or missing commitment scheme")
    detail = verify_manifest(
        manifest,
        run_dir,
        salt=commitment.get("salt"),
        expected_commitment=commitment.get("commitment"),
        expected_manifest_sha256=commitment.get("manifest_sha256"),
    )
    try:
        regenerated_commitment = compute_commitment(manifest, salt=commitment["salt"])
    except ValueError as exc:
        raise PipelineError(f"Cannot regenerate commitment sidecar: {exc}") from exc
    if regenerated_commitment != commitment:
        detail["errors"].append("commitment sidecar does not match regenerated values")
        detail["ok"] = False
        detail["valid"] = False
    listed_paths = {
        str(item.get("path"))
        for item in manifest.get("artifacts", [])
        if isinstance(item, dict) and isinstance(item.get("path"), str)
    }
    allowed_sidecars = {
        "chain-receipt.json",
        "commitment.json",
        "manifest.canonical.json",
        "manifest.json",
    }
    actual_paths = {
        path.relative_to(run_dir).as_posix() for path in run_dir.rglob("*") if path.is_file()
    }
    unexpected_paths = sorted(actual_paths - listed_paths - allowed_sidecars)
    if unexpected_paths:
        detail["errors"].extend(
            f"unexpected unlisted bundle file: {path}" for path in unexpected_paths
        )
        detail["ok"] = False
        detail["valid"] = False
    expected_canonical = canonical_manifest_bytes(manifest)
    try:
        sidecar_canonical = (run_dir / "manifest.canonical.json").read_bytes()
    except OSError:
        canonical_ok = False
    else:
        canonical_ok = secrets.compare_digest(expected_canonical, sidecar_canonical)

    receipt_path = run_dir / "chain-receipt.json"
    chain: ChainVerification | None = None
    external_anchor_ok: bool | None = None
    if expected_commitment is not None:
        external_anchor_ok = secrets.compare_digest(
            str(expected_commitment).lower(), str(commitment.get("commitment", "")).lower()
        )
    if receipt_path.is_file():
        saved_receipt = _read_json(receipt_path)
        if expected_transaction_hash is not None:
            tx_matches = secrets.compare_digest(
                str(expected_transaction_hash).lower(),
                str(saved_receipt.get("transaction_hash", "")).lower(),
            )
            external_anchor_ok = (
                tx_matches if external_anchor_ok is None else external_anchor_ok and tx_matches
            )
        if not settings.contract_address:
            chain = ChainVerification(
                connected=False,
                chain_id_matches=False,
                anchored=False,
                commitment=str(commitment.get("commitment", "")),
                receipt_consistent=False,
                detail="A trusted FACEPROOF_CONTRACT_ADDRESS is required for verification",
            )
        elif not settings.contract_code_hash:
            chain = ChainVerification(
                connected=False,
                chain_id_matches=False,
                anchored=False,
                commitment=str(commitment.get("commitment", "")),
                confirmations_required=settings.confirmations,
                detail="A trusted FACEPROOF_CONTRACT_CODE_HASH is required for verification",
            )
        else:
            try:
                chain = verify_commitment_on_chain(
                    commitment["commitment"],
                    rpc_url=settings.rpc_url,
                    expected_chain_id=settings.chain_id,
                    contract_address=settings.contract_address,
                    receipt=saved_receipt,
                    required_confirmations=settings.confirmations,
                    timeout_seconds=settings.http_timeout_seconds,
                    expected_code_hash=settings.contract_code_hash,
                )
            except ChainError as exc:
                raise PipelineError(f"On-chain verification failed: {exc}") from exc
    elif require_chain:
        if expected_transaction_hash is not None:
            external_anchor_ok = False
        chain = ChainVerification(
            connected=False,
            chain_id_matches=False,
            anchored=False,
            commitment=str(commitment.get("commitment", "")),
            detail="chain-receipt.json is absent; this run was not anchored",
        )
    elif expected_transaction_hash is not None:
        external_anchor_ok = False

    return LocalVerificationResult(
        evidence_ok=bool(detail["ok"]),
        evidence_detail=detail,
        canonical_sidecar_ok=canonical_ok,
        chain=chain,
        external_anchor_ok=external_anchor_ok,
    )


def run_tamper_demo(run_dir: Path) -> tuple[str, dict[str, Any]]:
    run_dir = Path(run_dir)
    manifest = _read_json(run_dir / "manifest.json")
    commitment = _read_json(run_dir / "commitment.json")
    artifacts = manifest.get("artifacts") or []
    if not artifacts:
        raise PipelineError("Manifest has no artifact to tamper with")

    with tempfile.TemporaryDirectory(prefix="faceproof-tamper-") as temporary:
        temporary_root = Path(temporary)
        for record in artifacts:
            logical_path = str(record["path"])
            source = run_dir / Path(*logical_path.split("/"))
            target = temporary_root / Path(*logical_path.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        changed_path = str(artifacts[0]["path"])
        changed_file = temporary_root / Path(*changed_path.split("/"))
        with changed_file.open("ab") as handle:
            handle.write(b"\x00")
        result = verify_manifest(
            manifest,
            temporary_root,
            salt=commitment.get("salt"),
            expected_commitment=commitment.get("commitment"),
            expected_manifest_sha256=commitment.get("manifest_sha256"),
        )
    return changed_path, result


def _make_provider(
    *,
    settings: Settings,
    live: bool,
    search_mode: str = "standard",
) -> SerpApiLensProvider:
    key = settings.require_serpapi_key()
    return SerpApiLensProvider(
        key,
        timeout_seconds=settings.http_timeout_seconds,
        no_cache=live,
        search_mode=search_mode,
    )


def _stage(callback: Callable[[str], None] | None, message: str) -> None:
    if callback is not None:
        callback(message)


def _new_run_id() -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{secrets.token_hex(3)}"


def _face_metadata(encoding: FaceEncoding) -> dict[str, Any]:
    detection = encoding.detection
    quality = encoding.quality
    return {
        "model_fingerprints": encoding.models.as_dict(),
        "embedding": {
            "dimension": len(encoding.embedding),
            "fingerprint_sha256": _embedding_fingerprint(encoding.embedding),
            "representation": "normalized IEEE-754 binary64 big-endian",
            "persisted": False,
        },
        "detection": {
            "box": {
                "x": _decimal(detection.box.x),
                "y": _decimal(detection.box.y),
                "width": _decimal(detection.box.width),
                "height": _decimal(detection.box.height),
            },
            "landmarks": [{"x": _decimal(x), "y": _decimal(y)} for x, y in detection.landmarks],
            "confidence": _decimal(detection.confidence),
        },
        "quality": {
            "face_width_px": _decimal(quality.face_width_px),
            "face_height_px": _decimal(quality.face_height_px),
            "face_area_ratio": _decimal(quality.face_area_ratio),
            "visible_fraction": _decimal(quality.visible_fraction),
            "sharpness": _optional_decimal(quality.sharpness),
            "brightness": _optional_decimal(quality.brightness),
        },
        "aligned_size": list(encoding.aligned_size),
    }


def _candidate_assessment(evaluation: CandidateEvaluation, *, threshold: float) -> dict[str, Any]:
    return {
        "status": "matched" if evaluation.matched else "below-threshold",
        "candidate": evaluation.candidate.public_dict(),
        "media": evaluation.media_artifact.to_dict(),
        "detected_faces": evaluation.detected_faces,
        "winning_face": _face_metadata(evaluation.matched_face),
        "local_similarity_micros": evaluation.local_similarity_micros,
        "threshold_micros": round(threshold * 1_000_000),
        "human_review_required": True,
    }


def _profile_assessment(evaluation: CandidateEvaluation, *, threshold: float) -> dict[str, Any]:
    return {
        "status": "face-match-lead" if evaluation.matched else "below-threshold",
        "claim": "unverified-public-profile-lead",
        "eligible_for_anchor": False,
        "platform": platform_name(evaluation.candidate.normalized_url),
        "candidate": evaluation.candidate.public_dict(),
        "media": evaluation.media_artifact.to_dict(),
        "detected_faces": evaluation.detected_faces,
        "winning_face": _face_metadata(evaluation.matched_face),
        "local_similarity_micros": evaluation.local_similarity_micros,
        "threshold_micros": round(threshold * 1_000_000),
        "human_review_required": True,
        "warning": "A profile lead is not a Task 3 post match or proof of identity.",
    }


def _evaluate_profile_leads(
    *,
    profile_candidates: list[SearchCandidate],
    run_dir: Path,
    backend: Any,
    query_encoding: FaceEncoding,
    settings: Settings,
    threshold: float,
    on_stage: Callable[[str], None] | None,
) -> list[CandidateEvaluation]:
    """Evaluate opt-in profile leads after the primary Task 3 post lane."""
    evaluations: list[CandidateEvaluation] = []
    if not profile_candidates:
        return evaluations
    profile_dir = run_dir / "profile-leads"
    profile_dir.mkdir(exist_ok=True)
    for ordinal, candidate in enumerate(profile_candidates, start=1):
        _stage(
            on_stage,
            f"Checking public profile lead {ordinal}/{len(profile_candidates)}",
        )
        candidate_dir = profile_dir / f"{ordinal:02d}"
        candidate_dir.mkdir(exist_ok=True)
        try:
            media_artifact = materialize_candidate_image(
                candidate,
                candidate_dir,
                timeout_seconds=settings.http_timeout_seconds,
            )
            media_path = candidate_dir / media_artifact.relative_path
            encodings = backend.encode_faces(media_path)
            scored_faces = [
                (cosine_similarity(query_encoding.embedding, item.embedding), item)
                for item in encodings
            ]
            similarity, matched_face = max(scored_faces, key=lambda scored: scored[0])
            evaluation = CandidateEvaluation(
                candidate=candidate,
                directory=candidate_dir,
                media_path=media_path,
                media_artifact=media_artifact,
                local_similarity=similarity,
                detected_faces=len(encodings),
                matched_face=matched_face,
                matched=similarity >= threshold,
            )
            evaluations.append(evaluation)
            _save_face_preview(
                media_path,
                matched_face,
                candidate_dir / "matched-face-preview.jpg",
            )
            _write_json(
                candidate_dir / "assessment.json",
                _profile_assessment(evaluation, threshold=threshold),
            )
        except (CaptureError, FaceError) as exc:
            _write_json(
                candidate_dir / "assessment.json",
                {
                    "candidate": candidate.public_dict(),
                    "platform": platform_name(candidate.normalized_url),
                    "status": "rejected",
                    "claim": "unverified-public-profile-lead",
                    "eligible_for_anchor": False,
                    "error": str(exc),
                },
            )
    return evaluations


def _write_search_record(path: Path, run: SearchRun) -> None:
    _write_json(
        path,
        {
            "provider": run.provider,
            "search_id": run.search_id,
            "retrieved_at": run.retrieved_at,
            "live": run.live,
            "provider_mode": run.provider_mode,
            "search_ids": list(run.search_ids),
            "search_types": list(run.search_types),
            "web_labels": list(run.web_labels),
            "web_label_interpretation": "unverified-provider-derived-search-hint",
            "candidates": [candidate.public_dict() for candidate in run.candidates],
            "raw_response": run.raw_response,
        },
    )


def _manifest_metadata(
    *,
    query_encoding: FaceEncoding,
    search_run: SearchRun,
    selected: CandidateEvaluation,
    capture: PostCapture,
    threshold: float,
    search_query_strategy: str,
    linkage_level: str,
    post_media_similarity: float | None,
    approved_post_url: str | None,
    consent_reference: str | None,
    source_revision: str | None,
    search_mode: str,
    platforms: frozenset[str] | None,
    profile_evaluations: list[CandidateEvaluation],
    profile_candidate_count: int,
    profile_discovery_authorized: bool,
    profile_platforms: frozenset[str] | None,
    review_run_id: str | None,
    review_manifest_sha256: str | None,
    review_commitment: str | None,
    max_candidates: int,
    max_profile_candidates: int,
) -> dict[str, Any]:
    candidate = selected.candidate
    return {
        "project": "FaceProof",
        "runtime": _runtime_metadata(source_revision),
        "claim_boundary": [
            "live_provider_returned_candidate_url",
            "local_model_similarity_is_not_legal_identity",
            "bundle_integrity_is_not_content_truth",
        ],
        "consent": {
            "acknowledged": True,
            "reference": consent_reference,
            "scope": "face-search-and-public-post-evidence",
            "profile_discovery_authorized": profile_discovery_authorized,
            "profile_platforms": (
                sorted(profile_platforms)
                if profile_discovery_authorized and profile_platforms
                else []
            ),
        },
        "query_face": _face_metadata(query_encoding),
        "search": {
            "provider": search_run.provider,
            "search_id": search_run.search_id,
            "search_ids": list(search_run.search_ids),
            "search_types": list(search_run.search_types),
            "search_mode": search_mode,
            "retrieved_at": search_run.retrieved_at,
            "live": search_run.live,
            "provider_mode": search_run.provider_mode,
            "web_labels": list(search_run.web_labels),
            "web_label_interpretation": "unverified-provider-derived-search-hint",
            "query_strategy": search_query_strategy,
            "candidate_count": len(search_run.candidates),
            "post_candidate_limit": max_candidates,
            "profile_candidate_limit": max_profile_candidates,
            "platform_filter": sorted(platforms) if platforms else [],
            "platform_filter_mode": "explicit" if platforms is not None else "all",
            "profile_lead_count": profile_candidate_count,
            "profile_lead_evaluated_count": len(profile_evaluations),
            "face_matched_profile_lead_count": sum(item.matched for item in profile_evaluations),
        },
        "selection": {
            "rank": candidate.rank,
            "page_url": redact_url_secrets(candidate.page_url),
            "normalized_url": redact_url_secrets(candidate.normalized_url),
            "post_id": candidate.post_id,
            "source": candidate.source,
            "title": candidate.title,
            "provider_score": _optional_decimal(candidate.provider_score),
            "local_similarity_micros": selected.local_similarity_micros,
            "threshold_micros": round(threshold * 1_000_000),
            "detected_faces": selected.detected_faces,
            "candidate_media_sha256": selected.media_artifact.sha256,
            "winning_face": _face_metadata(selected.matched_face),
            "linkage_level": linkage_level,
            "post_media_similarity_micros": (
                round(post_media_similarity * 1_000_000)
                if post_media_similarity is not None
                else None
            ),
            "human_review": {
                "approved_for_anchor": bool(approved_post_url),
                "approved_post_url": approved_post_url,
                "discovery_run_id": review_run_id,
                "discovery_manifest_sha256": review_manifest_sha256,
                "discovery_commitment": review_commitment,
            },
        },
        "capture": {
            "status": capture.status,
            "method": capture.method,
            "post_identity_verified": bool(capture.metadata.get("post_identity_verified")),
            "linked_media_count": len(capture.media_artifacts),
        },
    }


def _collect_pre_anchor_artifacts(
    run_dir: Path,
) -> tuple[dict[str, Path], dict[str, str]]:
    excluded = {
        "chain-receipt.json",
        "commitment.json",
        "manifest.canonical.json",
        "manifest.json",
    }
    artifacts: dict[str, Path] = {}
    media_types: dict[str, str] = {}
    for path in sorted(run_dir.rglob("*")):
        if not path.is_file() or path.name in excluded:
            continue
        logical = path.relative_to(run_dir).as_posix()
        artifacts[logical] = path
        media_types[logical] = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    if not artifacts:
        raise PipelineError("No evidence artifacts were produced")
    return artifacts, media_types


def _embedding_fingerprint(values: tuple[float, ...]) -> str:
    payload = b"".join(struct.pack(">d", value) for value in values)
    return hashlib.sha256(payload).hexdigest()


def _save_face_preview(source: Path, encoding: FaceEncoding, destination: Path) -> None:
    try:
        import cv2

        image = cv2.imread(str(source), cv2.IMREAD_COLOR)
        if image is None:
            return
        box = encoding.detection.box
        left, top = round(box.x), round(box.y)
        right, bottom = round(box.x + box.width), round(box.y + box.height)
        cv2.rectangle(image, (left, top), (right, bottom), (40, 220, 80), 3)
        for x, y in encoding.detection.landmarks:
            cv2.circle(image, (round(x), round(y)), 3, (40, 80, 240), -1)
        cv2.imwrite(str(destination), image)
    except Exception:
        # A preview is presentation-only. The exact query bytes and typed
        # detection metadata remain the evidence source of truth.
        return


def _save_face_crop(
    source: Path, encoding: FaceEncoding, destination: Path, *, margin_ratio: float = 0.18
) -> Path | None:
    """Save the detected face crop as an explicit hashed evidence artifact."""
    try:
        import cv2

        image = cv2.imread(str(source), cv2.IMREAD_COLOR)
        if image is None:
            return None
        image_height, image_width = image.shape[:2]
        box = encoding.detection.box
        margin_x = box.width * margin_ratio
        margin_y = box.height * margin_ratio
        left = max(0, int(box.x - margin_x))
        top = max(0, int(box.y - margin_y))
        right = min(image_width, int(box.x + box.width + margin_x))
        bottom = min(image_height, int(box.y + box.height + margin_y))
        if right <= left or bottom <= top:
            return None
        crop = image[top:bottom, left:right]
        if not cv2.imwrite(str(destination), crop):
            return None
        return destination
    except Exception:
        return None


def _write_json(path: Path, value: Any) -> None:
    payload = (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )
    _write_bytes_atomic(path, payload)


def _write_bytes_atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(payload)
    os.replace(temporary, path)


def _read_json(path: Path) -> dict[str, Any]:
    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError(f"Duplicate JSON key: {key!r}")
            result[key] = item
        return result

    value = json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=reject_duplicate_keys,
    )
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return value


def _decimal(value: float) -> str:
    return format(float(value), ".8f")


def _optional_decimal(value: float | None) -> str | None:
    return None if value is None else _decimal(value)


def _runtime_metadata(source_revision: str | None) -> dict[str, str | None]:
    import cv2
    import numpy

    from . import __version__

    return {
        "faceproof_version": __version__,
        "python_version": platform.python_version(),
        "opencv_version": cv2.__version__,
        "numpy_version": numpy.__version__,
        "source_revision": source_revision,
    }


__all__ = [
    "CandidateEvaluation",
    "InconclusiveError",
    "LocalVerificationResult",
    "PipelineError",
    "PipelineResult",
    "RecoveredAnchorResult",
    "anchor_submission_path",
    "recover_pending_anchor",
    "run_pipeline",
    "run_tamper_demo",
    "verify_run",
]
