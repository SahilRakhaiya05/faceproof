from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Settings
from .pipeline import (
    DEFAULT_COSINE_THRESHOLD,
    PipelineError,
    ReviewedContentIdentity,
    anchor_submission_path,
    review_anchor_claim_path,
    verify_run,
)
from .search import (
    PROFILE_LEAD_PLATFORMS,
    SUPPORTED_PLATFORMS,
    BlueskyEvidenceRef,
    normalize_bluesky_actor,
    normalize_page_url,
)
from .search.base import extract_post_id

RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


class ReviewError(ValueError):
    """Raised when discovery evidence cannot authorize a fresh anchor pass."""


@dataclass(frozen=True, slots=True)
class ReviewedDiscovery:
    run_id: str
    run_dir: Path
    image_path: Path
    selected_url: str
    search_mode: str
    search_provider: str
    bluesky_actor: str | None
    threshold: float
    platforms: frozenset[str] | None
    max_candidates: int
    max_profile_candidates: int
    profile_discovery_authorized: bool
    profile_platforms: frozenset[str] | None
    manifest_sha256: str
    commitment: str
    input_sha256: str
    input_bytes: bytes
    content_identity: ReviewedContentIdentity


def load_reviewed_discovery(
    *,
    output_dir: Path,
    run_id: str,
    approved_post_url: str,
    settings: Settings,
) -> ReviewedDiscovery:
    """Verify and load the immutable policy needed for a second-pass anchor."""
    normalized_run_id = run_id.strip()
    if not RUN_ID_PATTERN.fullmatch(normalized_run_id):
        raise ReviewError("Discovery run ID is invalid")
    root = Path(output_dir).resolve()
    unresolved = root / normalized_run_id
    if unresolved.is_symlink():
        raise ReviewError("Discovery evidence directory cannot be a symlink")
    run_dir = unresolved.resolve()
    try:
        run_dir.relative_to(root)
    except ValueError as exc:
        raise ReviewError("Discovery run is outside the evidence directory") from exc
    if not run_dir.is_dir():
        raise ReviewError("Discovery evidence run was not found")
    if (run_dir / "chain-receipt.json").exists():
        raise ReviewError("Anchoring requires an unanchored discovery bundle")
    pending_path = anchor_submission_path(run_dir)
    if pending_path.exists() or pending_path.is_symlink():
        raise ReviewError("Anchoring cannot reuse a run with an unresolved transaction")
    claim_path = review_anchor_claim_path(root, normalized_run_id)
    if claim_path.exists() or claim_path.is_symlink():
        raise ReviewError(
            "This discovery already authorized an anchor attempt; run a new discovery"
        )

    # Work only from one immutable filesystem snapshot. Verifying the live
    # directory and then reopening its files would let a concurrent local
    # writer replace the bundle between those operations.
    try:
        with tempfile.TemporaryDirectory(prefix="faceproof-review-") as temporary_name:
            snapshot_run = Path(temporary_name) / normalized_run_id
            shutil.copytree(run_dir, snapshot_run, symlinks=True)
            return _load_reviewed_snapshot(
                source_run_dir=run_dir,
                run_dir=snapshot_run,
                normalized_run_id=normalized_run_id,
                approved_post_url=approved_post_url,
                settings=settings,
            )
    except ReviewError:
        raise
    except OSError as exc:
        raise ReviewError("Discovery evidence could not be snapshotted safely") from exc


def _load_reviewed_snapshot(
    *,
    source_run_dir: Path,
    run_dir: Path,
    normalized_run_id: str,
    approved_post_url: str,
    settings: Settings,
) -> ReviewedDiscovery:
    """Verify and authorize only the bytes copied into one private snapshot."""

    if (run_dir / "chain-receipt.json").exists():
        raise ReviewError("Anchoring requires an unanchored discovery bundle")

    try:
        verification = verify_run(run_dir, settings=settings, require_chain=False)
    except (PipelineError, OSError, ValueError) as exc:
        raise ReviewError("Reviewed discovery evidence failed verification") from exc
    if not verification.passed:
        raise ReviewError("Reviewed discovery evidence failed verification")

    selection = _read_json_object(run_dir / "selection.json")
    manifest = _read_json_object(run_dir / "manifest.json")
    commitment_record = _read_json_object(run_dir / "commitment.json")
    candidate = selection.get("candidate")
    if not isinstance(candidate, dict):
        raise ReviewError("Discovery bundle has no selected post")
    selected_url = str(candidate.get("normalized_url") or candidate.get("page_url") or "")
    try:
        same_post = normalize_page_url(selected_url) == normalize_page_url(
            approved_post_url.strip()
        )
    except ValueError as exc:
        raise ReviewError("Approved post permalink is invalid") from exc
    if not same_post:
        raise ReviewError("Approved permalink does not match the selected discovery evidence")
    try:
        content_identity = ReviewedContentIdentity.from_dict(
            selection.get("reviewed_content_identity")
        )
    except PipelineError as exc:
        raise ReviewError("Discovery bundle has no valid reviewed content identity") from exc
    if (
        content_identity.normalized_url != normalize_page_url(selected_url)
        or content_identity.post_id != extract_post_id(selected_url)
        or selection.get("reviewed_content_identity_sha256") != content_identity.sha256
    ):
        raise ReviewError("Discovery reviewed content identity is inconsistent")
    candidate_media = selection.get("candidate_media")
    if (
        not isinstance(candidate_media, dict)
        or candidate_media.get("sha256") != content_identity.candidate_media_sha256
    ):
        raise ReviewError("Discovery selected media identity is inconsistent")

    threshold_micros = _bounded_int(
        selection.get("threshold_micros"),
        "face-match threshold",
        minimum=round(DEFAULT_COSINE_THRESHOLD * 1_000_000),
        maximum=1_000_000,
    )

    metadata = manifest.get("metadata")
    search_metadata = metadata.get("search") if isinstance(metadata, dict) else None
    consent_metadata = metadata.get("consent") if isinstance(metadata, dict) else None
    if not isinstance(search_metadata, dict) or not isinstance(consent_metadata, dict):
        raise ReviewError("Discovery bundle does not contain reusable search policy metadata")
    manifest_selection = metadata.get("selection") if isinstance(metadata, dict) else None
    if (
        not isinstance(manifest_selection, dict)
        or manifest_selection.get("threshold_micros") != threshold_micros
    ):
        raise ReviewError("Discovery face-match threshold evidence is inconsistent")
    if (
        manifest_selection.get("reviewed_content_identity") != content_identity.to_dict()
        or manifest_selection.get("reviewed_content_identity_sha256") != content_identity.sha256
    ):
        raise ReviewError("Discovery reviewed content identity is inconsistent")

    search_mode = search_metadata.get("search_mode")
    provider_strategy_value = search_metadata.get("provider_strategy")
    recorded_provider = search_metadata.get("provider")
    if provider_strategy_value is None and (
        recorded_provider is None or recorded_provider == "serpapi"
    ):
        # Backward-compatible replay for evidence produced before provider selection existed.
        search_provider = "lens"
    elif provider_strategy_value in {"lens", "bluesky"}:
        search_provider = str(provider_strategy_value)
    else:
        raise ReviewError("Discovery search provider policy is invalid")
    recorded_actor = search_metadata.get("consented_actor")
    recorded_resolved_did = search_metadata.get("resolved_actor_did")
    bluesky_actor: str | None = None
    if search_provider == "lens":
        if (
            (recorded_provider is not None and recorded_provider != "serpapi")
            or (recorded_actor is not None and recorded_actor != "")
            or (recorded_resolved_did is not None and recorded_resolved_did != "")
        ):
            raise ReviewError("Discovery Lens provider policy is inconsistent")
        provider_record = _read_json_object(run_dir / "search" / "provider-response.json")
        provider_raw = provider_record.get("raw_response")
        recorded_search_types = _string_list(
            search_metadata.get("search_types"),
            "search types",
        )
        expected_search_types = (
            ["all"] if search_mode == "standard" else ["exact_matches", "visual_matches"]
        )
        provider_candidates = provider_record.get("candidates")
        provider_requests = provider_raw.get("requests") if isinstance(provider_raw, dict) else None
        if (
            provider_record.get("provider") != "serpapi"
            or provider_record.get("live") is not True
            or provider_record.get("provider_mode") != "no-cache"
            or provider_record.get("search_id") != search_metadata.get("search_id")
            or provider_record.get("search_ids") != search_metadata.get("search_ids")
            or provider_record.get("search_types") != recorded_search_types
            or recorded_search_types != expected_search_types
            or provider_record.get("retrieved_at") != search_metadata.get("retrieved_at")
            or not isinstance(provider_raw, dict)
            or provider_raw.get("search_mode") != search_mode
            or not isinstance(provider_requests, list)
            or len(provider_requests) != len(expected_search_types)
            or not isinstance(provider_candidates, list)
        ):
            raise ReviewError("Discovery Lens provider evidence is inconsistent")
        for request, expected_type in zip(
            provider_requests,
            expected_search_types,
            strict=True,
        ):
            if (
                not isinstance(request, dict)
                or request.get("engine") != "google_lens"
                or request.get("type") != expected_type
                or str(request.get("no_cache")).casefold() != "true"
            ):
                raise ReviewError("Discovery Lens request evidence is inconsistent")
        provider_urls: set[str] = set()
        for item in provider_candidates:
            if not isinstance(item, dict):
                continue
            item_url = str(item.get("normalized_url") or item.get("page_url") or "")
            try:
                provider_urls.add(normalize_page_url(item_url))
            except ValueError:
                continue
        if normalize_page_url(selected_url) not in provider_urls:
            raise ReviewError("Discovery selection is absent from the Lens provider evidence")
    else:
        provider_record = _read_json_object(run_dir / "search" / "provider-response.json")
        if (
            recorded_provider != "bluesky-public-api"
            or not isinstance(recorded_actor, str)
            or not isinstance(recorded_resolved_did, str)
        ):
            raise ReviewError("Discovery Bluesky provider policy is incomplete")
        try:
            consented_actor = normalize_bluesky_actor(recorded_actor)
            bluesky_actor = normalize_bluesky_actor(recorded_resolved_did)
        except ValueError as exc:
            raise ReviewError("Discovery Bluesky actor is invalid") from exc
        if not bluesky_actor.startswith("did:"):
            raise ReviewError("Discovery Bluesky resolved actor must be a DID")
        provider_raw = provider_record.get("raw_response")
        if (
            provider_record.get("provider") != "bluesky-public-api"
            or not isinstance(provider_raw, dict)
            or provider_raw.get("resolved_actor_did") != bluesky_actor
        ):
            raise ReviewError("Discovery Bluesky provider evidence is inconsistent")
        try:
            provider_actor = normalize_bluesky_actor(str(provider_raw.get("actor") or ""))
        except ValueError as exc:
            raise ReviewError("Discovery Bluesky provider actor is invalid") from exc
        if provider_actor != consented_actor:
            raise ReviewError("Discovery Bluesky provider actor changed inside the evidence")
    if content_identity.search_provider != search_provider:
        raise ReviewError("Discovery content identity provider is inconsistent")
    provider_candidates = provider_record.get("candidates")
    if (
        not isinstance(provider_candidates, list)
        or sum(item == candidate for item in provider_candidates) != 1
    ):
        raise ReviewError("Discovery selection is absent from the exact provider evidence")
    if search_provider == "bluesky":
        try:
            selected_ref = BlueskyEvidenceRef.parse(str(candidate.get("provider_item_id") or ""))
        except ValueError as exc:
            raise ReviewError("Discovery Bluesky selection has invalid CID evidence") from exc
        if (
            selected_ref.at_uri != content_identity.bluesky_at_uri
            or selected_ref.post_cid != content_identity.bluesky_post_cid
            or selected_ref.image_cid != content_identity.bluesky_image_cid
        ):
            raise ReviewError("Discovery Bluesky content identity is inconsistent")
    filter_mode = search_metadata.get("platform_filter_mode")
    recorded_platforms = _string_list(search_metadata.get("platform_filter"), "platform filter")
    max_candidates = _bounded_int(
        search_metadata.get("post_candidate_limit"), "post candidate limit", minimum=1, maximum=20
    )
    max_profile_candidates = _bounded_int(
        search_metadata.get("profile_candidate_limit"),
        "profile candidate limit",
        minimum=0,
        maximum=20,
    )
    profile_platform_values = _string_list(
        consent_metadata.get("profile_platforms"), "profile platform filter"
    )
    profile_authorized = consent_metadata.get("profile_discovery_authorized")
    if search_mode not in {"standard", "deep"} or filter_mode not in {"all", "explicit"}:
        raise ReviewError("Discovery search policy is incomplete; run a new discovery")
    if type(profile_authorized) is not bool:
        raise ReviewError("Discovery profile consent policy is invalid")
    if filter_mode == "explicit" and not recorded_platforms:
        raise ReviewError("Discovery platform policy is empty")
    if max_profile_candidates > 0 and not profile_authorized:
        raise ReviewError("Discovery profile consent policy is inconsistent")
    if search_provider == "bluesky" and (
        search_mode != "standard"
        or filter_mode != "explicit"
        or set(item.casefold() for item in recorded_platforms) != {"bluesky"}
        or max_profile_candidates != 0
        or profile_authorized
    ):
        raise ReviewError("Discovery Bluesky search policy is inconsistent")

    platforms = (
        frozenset(item.casefold() for item in recorded_platforms)
        if filter_mode == "explicit"
        else None
    )
    profile_platforms = frozenset(item.casefold() for item in profile_platform_values) or None
    if (platforms and platforms - SUPPORTED_PLATFORMS) or (
        profile_platforms and profile_platforms - PROFILE_LEAD_PLATFORMS
    ):
        raise ReviewError("Reviewed discovery contains an unsupported platform policy")

    manifest_sha256 = _bytes32(commitment_record.get("manifest_sha256"), "manifest hash")
    commitment = _bytes32(commitment_record.get("commitment"), "commitment")
    query_files = sorted((run_dir / "input").glob("query.*"))
    if len(query_files) != 1 or query_files[0].is_symlink() or not query_files[0].is_file():
        raise ReviewError("Reviewed input image is unavailable")
    query_logical_path = query_files[0].relative_to(run_dir).as_posix()
    artifact_records = manifest.get("artifacts")
    matching_query_records = (
        [
            item
            for item in artifact_records
            if isinstance(item, dict) and item.get("path") == query_logical_path
        ]
        if isinstance(artifact_records, list)
        else []
    )
    if len(matching_query_records) != 1:
        raise ReviewError("Reviewed input image is absent from the verified manifest")
    artifact_hashes = {
        str(item.get("sha256") or "").casefold()
        for item in artifact_records
        if isinstance(item, dict)
    }
    required_content_hashes = {
        content_identity.candidate_media_sha256,
        *content_identity.capture_media_sha256,
    }
    if not required_content_hashes <= artifact_hashes:
        raise ReviewError("Reviewed content media is absent from the verified manifest")
    input_sha256 = str(matching_query_records[0].get("sha256") or "").casefold()
    if not re.fullmatch(r"[0-9a-f]{64}", input_sha256):
        raise ReviewError("Reviewed input image hash is invalid")
    try:
        input_bytes = query_files[0].read_bytes()
        observed_input_sha256 = hashlib.sha256(input_bytes).hexdigest()
    except OSError as exc:
        raise ReviewError("Reviewed input image is unavailable") from exc
    if observed_input_sha256 != input_sha256:
        raise ReviewError("Reviewed input image changed after verification")
    source_state_paths = (
        source_run_dir / "chain-receipt.json",
        anchor_submission_path(source_run_dir),
        review_anchor_claim_path(source_run_dir.parent, normalized_run_id),
    )
    if any(path.exists() or path.is_symlink() for path in source_state_paths):
        raise ReviewError("Discovery anchor state changed during review authorization")

    return ReviewedDiscovery(
        run_id=normalized_run_id,
        run_dir=source_run_dir,
        image_path=source_run_dir / query_logical_path,
        selected_url=normalize_page_url(selected_url),
        search_mode=str(search_mode),
        search_provider=search_provider,
        bluesky_actor=bluesky_actor,
        threshold=threshold_micros / 1_000_000,
        platforms=platforms,
        max_candidates=max_candidates,
        max_profile_candidates=max_profile_candidates,
        profile_discovery_authorized=profile_authorized,
        profile_platforms=profile_platforms,
        manifest_sha256=manifest_sha256,
        commitment=commitment,
        input_sha256=input_sha256,
        input_bytes=input_bytes,
        content_identity=content_identity,
    )


def _read_json_object(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 25 * 1024 * 1024:
        raise ReviewError(f"Required discovery record is unavailable: {path.name}")

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ReviewError(f"Duplicate JSON key in {path.name}: {key!r}")
            result[key] = value
        return result

    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=reject_duplicates)
    except (OSError, UnicodeError, ValueError) as exc:
        if isinstance(exc, ReviewError):
            raise
        raise ReviewError(f"Discovery record is invalid: {path.name}") from exc
    if not isinstance(value, dict):
        raise ReviewError(f"Discovery record must be an object: {path.name}")
    return value


def _string_list(value: object, field: str) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise ReviewError(f"Discovery {field} is invalid")
    normalized = [item.strip() for item in value]
    if len(set(item.casefold() for item in normalized)) != len(normalized):
        raise ReviewError(f"Discovery {field} contains duplicates")
    return normalized


def _bounded_int(value: object, field: str, *, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ReviewError(f"Discovery {field} must be from {minimum} to {maximum}")
    return value


def _bytes32(value: object, field: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"0x[0-9a-fA-F]{64}", value):
        raise ReviewError(f"Discovery {field} is invalid")
    return value.lower()


__all__ = ["ReviewError", "ReviewedDiscovery", "load_reviewed_discovery"]
