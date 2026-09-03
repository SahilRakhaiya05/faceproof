from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Settings
from .pipeline import PipelineError, verify_run
from .search import PROFILE_LEAD_PLATFORMS, SUPPORTED_PLATFORMS, normalize_page_url

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
    platforms: frozenset[str] | None
    max_candidates: int
    max_profile_candidates: int
    profile_discovery_authorized: bool
    profile_platforms: frozenset[str] | None
    manifest_sha256: str
    commitment: str


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

    metadata = manifest.get("metadata")
    search_metadata = metadata.get("search") if isinstance(metadata, dict) else None
    consent_metadata = metadata.get("consent") if isinstance(metadata, dict) else None
    if not isinstance(search_metadata, dict) or not isinstance(consent_metadata, dict):
        raise ReviewError("Discovery bundle does not contain reusable search policy metadata")

    search_mode = search_metadata.get("search_mode")
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

    return ReviewedDiscovery(
        run_id=normalized_run_id,
        run_dir=run_dir,
        image_path=query_files[0],
        selected_url=normalize_page_url(selected_url),
        search_mode=str(search_mode),
        platforms=platforms,
        max_candidates=max_candidates,
        max_profile_candidates=max_profile_candidates,
        profile_discovery_authorized=profile_authorized,
        profile_platforms=profile_platforms,
        manifest_sha256=manifest_sha256,
        commitment=commitment,
    )


def _read_json_object(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.stat().st_size > 25 * 1024 * 1024:
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
