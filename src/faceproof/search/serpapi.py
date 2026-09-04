from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from PIL import Image, ImageOps

from ._http import request_json_object
from .base import (
    SearchCandidate,
    SearchError,
    SearchRun,
    extract_post_id,
    normalize_page_url,
    redact_secrets,
)


@dataclass(frozen=True, slots=True)
class SerpApiAccount:
    """Non-sensitive SerpApi readiness data returned by the free Account API."""

    status: str
    plan_name: str
    searches_per_month: int
    searches_left: int
    this_month_usage: int
    hourly_limit: int

    @property
    def ready(self) -> bool:
        return self.status.casefold() == "active" and self.searches_left > 0


@dataclass(frozen=True, slots=True)
class _LensResponse:
    """One independently recorded Lens lane.

    Google Lens reports a genuine zero-result search through the same ``error``
    field used for authentication and quota failures.  ``outcome`` preserves the
    distinction without weakening fail-closed handling for other provider errors.
    """

    search_type: str
    params: dict[str, str]
    raw: bytes
    body: dict[str, Any]
    metadata: dict[str, Any]
    outcome: str
    provider_error: str | None = None


@dataclass(frozen=True, slots=True)
class _ProviderUpload:
    """One Image API upload retained for later Lens lane requests."""

    image_id: str
    raw: bytes
    body: dict[str, Any]


_SOFT_EMPTY_LENS_ERRORS = frozenset(
    {
        "google lens hasn't returned any results for this query",
        "google lens has not returned any results for this query",
    }
)


def check_serpapi_account(
    api_key: str,
    *,
    timeout_seconds: float = 10,
    client: httpx.Client | None = None,
) -> SerpApiAccount:
    """Validate a key and quota without consuming a search credit."""
    if not api_key.strip():
        raise ValueError("SerpApi API key is required")
    owns_client = client is None
    http_client = client or httpx.Client(timeout=timeout_seconds)
    try:
        response = request_json_object(
            http_client,
            "GET",
            "https://serpapi.com/account.json",
            operation="SerpApi account check",
            max_bytes=1024 * 1024,
            params={"api_key": api_key},
        )
        body = response.body
    finally:
        if owns_client:
            http_client.close()

    if not isinstance(body, dict):
        raise SearchError("SerpApi account check returned a malformed response")
    if body.get("error"):
        error = redact_secrets(str(body["error"]), secret_values=(api_key,))
        raise SearchError(f"SerpApi account error: {error}")
    try:
        return SerpApiAccount(
            status=_required_string(body.get("account_status"), "account_status"),
            plan_name=_required_string(body.get("plan_name"), "plan_name"),
            searches_per_month=_non_negative_int(
                body.get("searches_per_month"), "searches_per_month"
            ),
            searches_left=_non_negative_int(body.get("total_searches_left"), "total_searches_left"),
            this_month_usage=_non_negative_int(body.get("this_month_usage"), "this_month_usage"),
            hourly_limit=_non_negative_int(
                body.get("account_rate_limit_per_hour"), "account_rate_limit_per_hour"
            ),
        )
    except ValueError as exc:
        raise SearchError(f"SerpApi account check returned malformed quota data: {exc}") from exc


class SerpApiLensProvider:
    """Google Lens adapter using SerpApi's Image and Search APIs."""

    name = "serpapi"

    def __init__(
        self,
        api_key: str,
        *,
        timeout_seconds: float = 30,
        country: str = "in",
        language: str = "en",
        no_cache: bool = True,
        search_mode: str = "standard",
        client: httpx.Client | None = None,
    ) -> None:
        if not api_key.strip():
            raise ValueError("SerpApi API key is required")
        if search_mode not in {"standard", "deep"}:
            raise ValueError("search_mode must be 'standard' or 'deep'")
        self.api_key = api_key
        self.country = country
        self.language = language
        self.no_cache = no_cache
        self.search_mode = search_mode
        self._owns_client = client is None
        self.client = client or httpx.Client(timeout=timeout_seconds)

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def __enter__(self) -> SerpApiLensProvider:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def search(
        self,
        image_path: Path,
        *,
        focus_image_path: Path | None = None,
    ) -> SearchRun:
        image_path = Path(image_path)
        if not image_path.is_file():
            raise SearchError(f"Image does not exist: {image_path}")
        primary_prepared = prepare_serpapi_upload(image_path)
        prepared_inputs = {"primary": primary_prepared}
        reused_from: dict[str, str | None] = {"primary": None}

        if self.search_mode == "standard":
            lane_plan = (("all", "primary"),)
        else:
            focus_path = image_path if focus_image_path is None else Path(focus_image_path)
            if not focus_path.is_file():
                raise SearchError(f"Focus image does not exist: {focus_path}")
            focus_prepared = (
                primary_prepared if focus_path == image_path else prepare_serpapi_upload(focus_path)
            )
            prepared_inputs["focus"] = focus_prepared
            lane_plan = (
                ("exact_matches", "primary"),
                ("visual_matches", "focus"),
            )

        # Validate and prepare every local input before transmitting either one.
        primary_upload = self._upload_image(*primary_prepared)
        provider_uploads = {"primary": primary_upload}
        if "focus" in prepared_inputs:
            focus_prepared = prepared_inputs["focus"]
            if focus_prepared[0] == primary_prepared[0]:
                provider_uploads["focus"] = primary_upload
                reused_from["focus"] = "primary"
            else:
                provider_uploads["focus"] = self._upload_image(*focus_prepared)
                reused_from["focus"] = None

        requested_types = tuple(search_type for search_type, _input_role in lane_plan)
        # A no-results response in one Deep lane is not a provider failure and
        # must not prevent the other lane from running.
        responses = [
            self._lens_request(provider_uploads[input_role].image_id, search_type)
            for search_type, input_role in lane_plan
        ]
        lane_image_ids = {
            search_type: provider_uploads[input_role].image_id
            for search_type, input_role in lane_plan
        }

        candidates: list[SearchCandidate] = []
        seen_urls: set[str] = set()
        seen_media: set[tuple[str, str]] = set()
        web_labels: list[str] = []
        seen_labels: set[str] = set()
        search_ids: list[str] = []
        for response in responses:
            search_ids.append(
                str(response.metadata.get("id") or lane_image_ids[response.search_type])
            )
            # A semantic no-results response is retained verbatim as evidence,
            # but any contradictory result arrays or labels in that same body
            # are not eligible discovery output.
            if response.outcome != "success":
                continue
            for label in _extract_web_labels(response.body):
                if label.casefold() not in seen_labels:
                    seen_labels.add(label.casefold())
                    web_labels.append(label)
            # ``type=all`` can contain both arrays. Parse exact results first so
            # a duplicate visual result cannot downgrade exact-match provenance.
            for result_key, result_type in (
                ("exact_matches", "exact_match"),
                ("visual_matches", "visual_match"),
            ):
                values = response.body.get(result_key)
                if not isinstance(values, list):
                    continue
                for fallback_rank, item in enumerate(values, start=1):
                    if not isinstance(item, dict) or not item.get("link"):
                        continue
                    try:
                        normalized = normalize_page_url(str(item["link"]))
                    except ValueError:
                        continue
                    image_url = _optional_string(item.get("image"))
                    thumbnail_url = _optional_string(item.get("thumbnail"))
                    media_variants: list[tuple[str, str | None, str | None]] = []
                    if image_url:
                        media_variants.append(("image", image_url, None))
                    if thumbnail_url and thumbnail_url != image_url:
                        media_variants.append(("thumbnail", None, thumbnail_url))
                    if not media_variants:
                        media_variants.append(("unbound", None, None))
                    provider_result_id = (
                        f"{search_ids[-1]}:{result_key}:"
                        f"{_positive_int(item.get('position'), fallback_rank)}"
                    )
                    for media_kind, variant_image, variant_thumbnail in media_variants:
                        media_reference = variant_image or variant_thumbnail
                        if media_reference is None and normalized in seen_urls:
                            continue
                        if (
                            media_reference is not None
                            and (
                                normalized,
                                media_reference,
                            )
                            in seen_media
                        ):
                            continue
                        seen_urls.add(normalized)
                        if media_reference is not None:
                            seen_media.add((normalized, media_reference))
                        candidates.append(
                            SearchCandidate(
                                provider=self.name,
                                rank=len(candidates) + 1,
                                page_url=str(item["link"]),
                                normalized_url=normalized,
                                title=_optional_string(item.get("title")),
                                source=_optional_string(item.get("source")),
                                image_url=variant_image,
                                thumbnail_url=variant_thumbnail,
                                exact_match=(
                                    True
                                    if result_type == "exact_match"
                                    else _optional_bool(item.get("exact_matches"))
                                ),
                                provider_item_id=f"{provider_result_id}:{media_kind}",
                                post_id=extract_post_id(normalized),
                                result_type=result_type,
                            )
                        )

        primary = responses[0]
        search_id = search_ids[0]
        sanitized_params = {key: value for key, value in primary.params.items() if key != "api_key"}
        query_uploads = {
            input_role: _query_upload_evidence(
                prepared,
                reused_from=reused_from[input_role],
            )
            for input_role, prepared in prepared_inputs.items()
        }
        sanitized_searches = {
            response.search_type: redact_secrets(response.body, secret_values=(self.api_key,))
            for response in responses
        }
        search_outcomes = {
            response.search_type: {
                "outcome": response.outcome,
                "provider_error": redact_secrets(
                    response.provider_error, secret_values=(self.api_key,)
                )
                if response.provider_error
                else None,
            }
            for response in responses
        }
        return SearchRun.create(
            provider=self.name,
            search_id=search_id,
            candidates=candidates,
            web_labels=web_labels,
            raw_response={
                # ``query_upload`` and ``upload`` are retained for evidence-bundle
                # compatibility. The plural records make Deep's two inputs and
                # any provider-upload reuse explicit.
                "query_upload": query_uploads["primary"],
                "query_uploads": query_uploads,
                "upload": redact_secrets(
                    provider_uploads["primary"].body,
                    secret_values=(self.api_key,),
                ),
                "uploads": {
                    input_role: {
                        "provider_response": redact_secrets(
                            upload.body,
                            secret_values=(self.api_key,),
                        ),
                        "raw_http_body_sha256": hashlib.sha256(upload.raw).hexdigest(),
                        "reused_from": reused_from[input_role],
                    }
                    for input_role, upload in provider_uploads.items()
                },
                "search": redact_secrets(primary.body, secret_values=(self.api_key,)),
                "searches": sanitized_searches,
                "search_outcomes": search_outcomes,
                "raw_http_body_sha256": {
                    "upload": hashlib.sha256(provider_uploads["primary"].raw).hexdigest(),
                    "uploads": {
                        input_role: hashlib.sha256(upload.raw).hexdigest()
                        for input_role, upload in provider_uploads.items()
                    },
                    "search": hashlib.sha256(primary.raw).hexdigest(),
                    "searches": {
                        response.search_type: hashlib.sha256(response.raw).hexdigest()
                        for response in responses
                    },
                },
                "request_parameters": sanitized_params,
                "requests": [
                    {key: value for key, value in response.params.items() if key != "api_key"}
                    for response in responses
                ],
                "lane_to_input": {
                    search_type: {
                        "input_role": input_role,
                        "image_id": lane_image_ids[search_type],
                        "query_upload_sha256": query_uploads[input_role]["sha256"],
                    }
                    for search_type, input_role in lane_plan
                },
                "search_mode": self.search_mode,
                "primary_search_type": primary.search_type,
            },
            live=self.no_cache,
            provider_mode="no-cache" if self.no_cache else "cache-allowed",
            search_ids=search_ids,
            search_types=requested_types,
        )

    def _upload_image(
        self,
        upload_bytes: bytes,
        upload_name: str,
        media_type: str,
    ) -> _ProviderUpload:
        upload_response = request_json_object(
            self.client,
            "POST",
            "https://serpapi.com/image",
            operation="SerpApi image upload",
            max_bytes=1024 * 1024,
            data={"api_key": self.api_key},
            files={"image": (upload_name, upload_bytes, media_type)},
        )
        upload_raw = upload_response.raw
        upload = upload_response.body
        if upload.get("error"):
            error = redact_secrets(str(upload["error"]), secret_values=(self.api_key,))
            raise SearchError(f"SerpApi image upload error: {error}")
        image_id = str(upload.get("image_id") or "")
        if not image_id:
            raise SearchError("SerpApi image upload did not return image_id")
        return _ProviderUpload(image_id=image_id, raw=upload_raw, body=upload)

    def _lens_request(self, image_id: str, search_type: str) -> _LensResponse:
        params = {
            "engine": "google_lens",
            "image_id": image_id,
            "type": search_type,
            "safe": "active",
            "country": self.country,
            "hl": self.language,
            "no_cache": str(self.no_cache).lower(),
            "api_key": self.api_key,
        }
        search_response = request_json_object(
            self.client,
            "GET",
            "https://serpapi.com/search.json",
            operation="SerpApi Lens request",
            max_bytes=8 * 1024 * 1024,
            params=params,
        )
        search_raw = search_response.raw
        body = search_response.body
        metadata = body.get("search_metadata") or {}
        if not isinstance(metadata, dict):
            raise SearchError("SerpApi Lens returned malformed search metadata")
        echoed = body.get("search_parameters") or {}
        _validate_echoed_parameters(
            echoed,
            image_id=image_id,
            search_type=search_type,
            no_cache=self.no_cache,
        )
        if body.get("error"):
            error = redact_secrets(str(body["error"]), secret_values=(self.api_key,))
            if _is_soft_empty_lens_error(str(error)):
                return _LensResponse(
                    search_type=search_type,
                    params=params,
                    raw=search_raw,
                    body=body,
                    metadata=metadata,
                    outcome="soft-empty",
                    provider_error=str(error),
                )
            raise SearchError(f"SerpApi Lens error: {error}")
        if str(metadata.get("status", "")).casefold() != "success":
            raise SearchError("SerpApi Lens search did not complete successfully")
        return _LensResponse(
            search_type=search_type,
            params=params,
            raw=search_raw,
            body=body,
            metadata=metadata,
            outcome="success",
        )


def prepare_serpapi_upload(image_path: Path, *, max_bytes: int = 490_000) -> tuple[bytes, str, str]:
    """Prepare a metadata-free, sub-500KB JPEG for SerpApi's Image API."""
    raw = image_path.read_bytes()

    try:
        with Image.open(io.BytesIO(raw)) as opened:
            transposed = ImageOps.exif_transpose(opened)
            if transposed.mode in {"RGBA", "LA"}:
                rgba = transposed.convert("RGBA")
                background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
                background.alpha_composite(rgba)
                image = background.convert("RGB")
            else:
                image = transposed.convert("RGB")
            image.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
            for quality in (92, 86, 80, 74, 68, 60, 52, 44):
                output = io.BytesIO()
                image.save(output, format="JPEG", quality=quality, optimize=True)
                encoded = output.getvalue()
                if len(encoded) <= max_bytes:
                    return encoded, f"{image_path.stem}.jpg", "image/jpeg"
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        raise SearchError(f"Cannot prepare image for SerpApi: {exc}") from exc
    raise SearchError("Image could not be compressed below SerpApi's 500KB limit")


def _query_upload_evidence(
    prepared: tuple[bytes, str, str],
    *,
    reused_from: str | None,
) -> dict[str, Any]:
    upload_bytes, upload_name, media_type = prepared
    return {
        "filename": upload_name,
        "media_type": media_type,
        "byte_size": len(upload_bytes),
        "sha256": hashlib.sha256(upload_bytes).hexdigest(),
        "metadata_stripped": True,
        "content_retained_in_provider_record": False,
        "provider_upload_reused": reused_from is not None,
        "provider_upload_reused_from": reused_from,
    }


def _is_soft_empty_lens_error(value: str) -> bool:
    normalized = " ".join(value.strip().casefold().split()).rstrip(".")
    return normalized in _SOFT_EMPTY_LENS_ERRORS


def _validate_echoed_parameters(
    echoed: Any,
    *,
    image_id: str,
    search_type: str,
    no_cache: bool,
) -> None:
    if not isinstance(echoed, dict):
        raise SearchError("SerpApi Lens returned malformed search parameters")
    if echoed.get("engine") not in {None, "google_lens"}:
        raise SearchError("SerpApi response engine did not match google_lens")
    if echoed.get("image_id") not in {None, image_id}:
        raise SearchError("SerpApi response image_id did not match the uploaded image")
    if echoed.get("type") not in {None, search_type}:
        raise SearchError(f"SerpApi response search type did not match {search_type}")
    if "no_cache" in echoed and str(echoed["no_cache"]).casefold() != str(no_cache).lower():
        raise SearchError("SerpApi response cache mode did not match the request")


def _optional_string(value: Any) -> str | None:
    return str(value) if value is not None else None


def _optional_bool(value: Any) -> bool | None:
    return value if type(value) is bool else None


def _positive_int(value: Any, fallback: int) -> int:
    if type(value) is int and value > 0:
        return value
    return fallback


def _extract_web_labels(body: dict[str, Any], *, limit: int = 8) -> list[str]:
    """Extract deterministic Lens query labels without inferring identity locally."""
    labels: list[str] = []
    seen: set[str] = set()
    related = body.get("related_content")
    if not isinstance(related, list):
        return labels
    for item in related:
        if not isinstance(item, dict) or not isinstance(item.get("query"), str):
            continue
        label = " ".join(item["query"].split())[:160]
        key = label.casefold()
        if not label or key in seen:
            continue
        seen.add(key)
        labels.append(label)
        if len(labels) >= limit:
            break
    return labels


def _required_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _non_negative_int(value: Any, field: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return value
