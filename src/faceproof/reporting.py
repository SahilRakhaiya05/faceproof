from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .chain import explorer_transaction_url
from .search.base import is_social_post_url, is_social_profile_url, platform_name


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file() or path.stat().st_size > 25 * 1024 * 1024:
        return None

    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"Duplicate JSON key: {key!r}")
            result[key] = value
        return result

    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=reject_duplicate_keys,
        )
    except (OSError, UnicodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _float(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _micros(value: object) -> float | None:
    number = _float(value)
    return None if number is None else round(number / 1_000_000, 6)


def _asset(run_id: str, relative_path: str | None) -> str | None:
    if not relative_path:
        return None
    normalized = relative_path.replace("\\", "/").lstrip("/")
    return f"/api/evidence/{run_id}/asset/{normalized}"


def _candidate_summary(candidate: dict[str, Any]) -> dict[str, Any]:
    url = str(candidate.get("normalized_url") or candidate.get("page_url") or "")
    return {
        "rank": candidate.get("rank"),
        "url": url,
        "title": candidate.get("title"),
        "source": candidate.get("source"),
        "platform": platform_name(url),
        "post_id": candidate.get("post_id"),
        "exact_match": candidate.get("exact_match") is True,
        "result_type": candidate.get("result_type") or "visual_match",
    }


def _assessment_summary(path: Path, *, run_id: str, root_name: str) -> dict[str, Any] | None:
    value = _read_json(path)
    if value is None or not isinstance(value.get("candidate"), dict):
        return None
    ordinal = path.parent.name
    candidate = _candidate_summary(value["candidate"])
    candidate.update(
        {
            "ordinal": ordinal,
            "status": value.get("status") or "unknown",
            "reason": value.get("error"),
            "similarity": _micros(value.get("local_similarity_micros")),
            "threshold": _micros(value.get("threshold_micros")),
            "detected_faces": value.get("detected_faces"),
            "eligible_for_anchor": bool(value.get("eligible_for_anchor", False)),
            "claim": value.get("claim"),
            "media_url": None,
            "face_preview_url": None,
        }
    )
    media = value.get("media")
    if isinstance(media, dict) and isinstance(media.get("relative_path"), str):
        candidate["media_url"] = _asset(run_id, f"{root_name}/{ordinal}/{media['relative_path']}")
    preview = path.parent / "matched-face-preview.jpg"
    if preview.is_file():
        candidate["face_preview_url"] = _asset(
            run_id, f"{root_name}/{ordinal}/matched-face-preview.jpg"
        )
    return candidate


def summarize_run(run_dir: Path) -> dict[str, Any]:
    """Build a secret-free, presentation-oriented summary of one evidence run."""
    run_dir = Path(run_dir)
    run_id = run_dir.name
    error = _read_json(run_dir / "run-error.json") or {}
    face = _read_json(run_dir / "input" / "face-encoding.json") or {}
    provider = _read_json(run_dir / "search" / "provider-response.json") or {}
    selection = _read_json(run_dir / "selection.json") or {}
    manifest = _read_json(run_dir / "manifest.json") or {}
    commitment = _read_json(run_dir / "commitment.json") or {}
    receipt = _read_json(run_dir / "chain-receipt.json") or {}
    pending = _read_json(run_dir.parent / f".{run_id}.anchor-submission.json") or {}

    if pending:
        status = "anchor-pending"
    elif receipt:
        status = "anchor-recorded"
    elif manifest and commitment:
        status = "discovered"
    elif error.get("stage") in {"social-filter", "local-rematch", "post-validation"}:
        status = "inconclusive"
    elif error:
        status = "failed"
    else:
        status = "interrupted"

    input_candidates = (
        sorted((run_dir / "input").glob("query.*")) if (run_dir / "input").is_dir() else []
    )
    query_relative = f"input/{input_candidates[0].name}" if input_candidates else None
    detection = face.get("detection") if isinstance(face.get("detection"), dict) else {}
    quality = face.get("quality") if isinstance(face.get("quality"), dict) else {}

    post_assessments = [
        item
        for item in (
            _assessment_summary(path, run_id=run_id, root_name="candidates")
            for path in sorted((run_dir / "candidates").glob("*/assessment.json"))
            if (run_dir / "candidates").is_dir()
        )
        if item is not None
    ]
    profile_assessments = [
        item
        for item in (
            _assessment_summary(path, run_id=run_id, root_name="profile-leads")
            for path in sorted((run_dir / "profile-leads").glob("*/assessment.json"))
            if (run_dir / "profile-leads").is_dir()
        )
        if item is not None
    ]
    disposition_by_url = {
        str(item["url"]): str(item["status"])
        for item in [*post_assessments, *profile_assessments]
        if item.get("url")
    }

    selected_candidate = (
        selection.get("candidate") if isinstance(selection.get("candidate"), dict) else {}
    )
    selected_url = str(selected_candidate.get("normalized_url") or "")
    provider_candidates = provider.get("candidates")
    result_board: list[dict[str, Any]] = []
    if isinstance(provider_candidates, list):
        for candidate in provider_candidates[:100]:
            if not isinstance(candidate, dict):
                continue
            item = _candidate_summary(candidate)
            url = str(item["url"])
            if url == selected_url:
                disposition = "selected"
            elif url in disposition_by_url:
                disposition = disposition_by_url[url]
            elif is_social_post_url(url):
                disposition = "post-not-evaluated"
            elif is_social_profile_url(url):
                disposition = "profile-not-evaluated"
            else:
                disposition = "general-web-result"
            item["disposition"] = disposition
            result_board.append(item)

    selected = _candidate_summary(selected_candidate) if selected_candidate else None
    if selected is not None:
        selected.update(
            {
                "similarity": _micros(selection.get("local_similarity_micros")),
                "threshold": _micros(selection.get("threshold_micros")),
                "post_media_similarity": _micros(selection.get("post_media_similarity_micros")),
                "linkage_level": selection.get("linkage_level"),
                "capture_method": selection.get("capture_method"),
                "human_approved": bool(
                    isinstance(selection.get("human_review"), dict)
                    and selection["human_review"].get("approved_for_anchor")
                ),
                "media_url": None,
            }
        )
        media = selection.get("candidate_media")
        selected_ordinal = next(
            (
                str(item["ordinal"])
                for item in post_assessments
                if item.get("url") == selected.get("url")
            ),
            None,
        )
        if (
            isinstance(media, dict)
            and selected_ordinal
            and isinstance(media.get("relative_path"), str)
        ):
            selected["media_url"] = _asset(
                run_id, f"candidates/{selected_ordinal}/{media['relative_path']}"
            )

    artifacts = manifest.get("artifacts")
    search_ids = provider.get("search_ids")
    search_types = provider.get("search_types")
    chain = None
    if receipt:
        chain = {
            "chain_id": receipt.get("chain_id"),
            "network": receipt.get("network"),
            "contract_address": receipt.get("contract_address"),
            "transaction_hash": receipt.get("transaction_hash"),
            "block_number": receipt.get("block_number"),
            "block_hash": receipt.get("block_hash"),
            "confirmations": receipt.get("confirmations_observed"),
            "submitter": receipt.get("submitter"),
            "chain_timestamp": receipt.get("chain_timestamp"),
            "explorer_url": explorer_transaction_url(
                int(receipt["chain_id"]), str(receipt["transaction_hash"])
            )
            if isinstance(receipt.get("chain_id"), int) and receipt.get("transaction_hash")
            else None,
        }
        chain["network"] = receipt.get("rpc_network")

    return {
        "run_id": run_id,
        "status": status,
        "error": {
            "stage": error.get("stage"),
            "message": error.get("error"),
        }
        if error
        else None,
        "face": {
            "confidence": _float(detection.get("confidence")),
            "box": detection.get("box"),
            "sharpness": _float(quality.get("sharpness")),
            "brightness": _float(quality.get("brightness")),
            "face_area_ratio": _float(quality.get("face_area_ratio")),
            "query_url": _asset(run_id, query_relative),
            "detected_preview_url": (
                _asset(run_id, "input/detected-face.jpg")
                if (run_dir / "input" / "detected-face.jpg").is_file()
                else None
            ),
        },
        "search": {
            "provider": provider.get("provider"),
            "search_id": provider.get("search_id"),
            "search_ids": search_ids if isinstance(search_ids, list) else [],
            "search_types": search_types if isinstance(search_types, list) else [],
            "retrieved_at": provider.get("retrieved_at"),
            "live": provider.get("live") is True,
            "provider_mode": provider.get("provider_mode"),
            "candidate_count": len(provider_candidates)
            if isinstance(provider_candidates, list)
            else 0,
            "web_labels": provider.get("web_labels")
            if isinstance(provider.get("web_labels"), list)
            else [],
            "web_label_interpretation": provider.get("web_label_interpretation"),
        },
        "selected": selected,
        "post_assessments": post_assessments,
        "profile_leads": profile_assessments,
        "result_board": result_board,
        "integrity": {
            "manifest_sha256": commitment.get("manifest_sha256"),
            "commitment": commitment.get("commitment"),
            "scheme": commitment.get("scheme"),
            "artifact_count": len(artifacts) if isinstance(artifacts, list) else 0,
            "canonicalization": manifest.get("canonicalization"),
        },
        "chain": chain,
        "anchor_pending": {
            "transaction_hash": pending.get("transaction_hash"),
            "chain_id": pending.get("chain_id"),
            "contract_address": pending.get("contract_address"),
            "submitter": pending.get("submitter"),
            "nonce": pending.get("nonce"),
        }
        if pending
        else None,
        "download_url": f"/api/evidence/{run_id}/download" if run_dir.is_dir() else None,
    }


def list_run_summaries(output_dir: Path, *, limit: int = 20) -> list[dict[str, Any]]:
    root = Path(output_dir)
    if not root.is_dir():
        return []
    directories = sorted(
        (path for path in root.iterdir() if path.is_dir()),
        key=lambda path: path.name,
        reverse=True,
    )
    return [summarize_run(path) for path in directories[: max(0, limit)]]


__all__ = ["list_run_summaries", "summarize_run"]
