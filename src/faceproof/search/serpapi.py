from __future__ import annotations

import base64
import hashlib
import io
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from PIL import Image, ImageOps

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
        response = http_client.get(
            "https://serpapi.com/account.json",
            params={"api_key": api_key},
        )
        response.raise_for_status()
        body = response.json()
    except httpx.HTTPError as exc:
        raise SearchError(f"SerpApi account check failed: {_safe_http_error(exc)}") from exc
    except ValueError as exc:
        raise SearchError("SerpApi account check returned invalid JSON") from exc
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
        client: httpx.Client | None = None,
    ) -> None:
        if not api_key.strip():
            raise ValueError("SerpApi API key is required")
        self.api_key = api_key
        self.country = country
        self.language = language
        self.no_cache = no_cache
        self._owns_client = client is None
        self.client = client or httpx.Client(timeout=timeout_seconds)

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def __enter__(self) -> SerpApiLensProvider:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def search(self, image_path: Path) -> SearchRun:
        image_path = Path(image_path)
        if not image_path.is_file():
            raise SearchError(f"Image does not exist: {image_path}")
        upload_bytes, upload_name, media_type = prepare_serpapi_upload(image_path)

        try:
            upload_response = self.client.post(
                "https://serpapi.com/image",
                data={"api_key": self.api_key},
                files={"image": (upload_name, upload_bytes, media_type)},
            )
            upload_response.raise_for_status()
            upload_raw = bytes(upload_response.content)
            upload = upload_response.json()
        except httpx.HTTPError as exc:
            raise SearchError(f"SerpApi image upload failed: {_safe_http_error(exc)}") from exc
        except ValueError as exc:
            raise SearchError("SerpApi image upload returned invalid JSON") from exc
        if upload.get("error"):
            error = redact_secrets(str(upload["error"]), secret_values=(self.api_key,))
            raise SearchError(f"SerpApi image upload error: {error}")
        image_id = str(upload.get("image_id") or "")
        if not image_id:
            raise SearchError("SerpApi image upload did not return image_id")

        params = {
            "engine": "google_lens",
            "image_id": image_id,
            "type": "all",
            "safe": "active",
            "country": self.country,
            "hl": self.language,
            "no_cache": str(self.no_cache).lower(),
            "api_key": self.api_key,
        }
        try:
            search_response = self.client.get("https://serpapi.com/search.json", params=params)
            search_response.raise_for_status()
            search_raw = bytes(search_response.content)
            body = search_response.json()
        except httpx.HTTPError as exc:
            raise SearchError(f"SerpApi Lens request failed: {_safe_http_error(exc)}") from exc
        except ValueError as exc:
            raise SearchError("SerpApi Lens returned invalid JSON") from exc
        if body.get("error"):
            error = redact_secrets(str(body["error"]), secret_values=(self.api_key,))
            raise SearchError(f"SerpApi Lens error: {error}")
        metadata = body.get("search_metadata") or {}
        if (
            not isinstance(metadata, dict)
            or str(metadata.get("status", "")).casefold() != "success"
        ):
            raise SearchError("SerpApi Lens search did not complete successfully")
        echoed = body.get("search_parameters") or {}
        if isinstance(echoed, dict):
            if echoed.get("engine") not in {None, "google_lens"}:
                raise SearchError("SerpApi response engine did not match google_lens")
            if echoed.get("image_id") not in {None, image_id}:
                raise SearchError("SerpApi response image_id did not match the uploaded image")
            if echoed.get("type") not in {None, "all"}:
                raise SearchError("SerpApi response search type did not match all")
            if (
                "no_cache" in echoed
                and str(echoed["no_cache"]).casefold() != str(self.no_cache).lower()
            ):
                raise SearchError("SerpApi response cache mode did not match the request")

        candidates: list[SearchCandidate] = []
        for fallback_rank, item in enumerate(body.get("visual_matches") or [], start=1):
            if not isinstance(item, dict) or not item.get("link"):
                continue
            try:
                normalized = normalize_page_url(str(item["link"]))
            except ValueError:
                continue
            candidates.append(
                SearchCandidate(
                    provider=self.name,
                    rank=_positive_int(item.get("position"), fallback_rank),
                    page_url=str(item["link"]),
                    normalized_url=normalized,
                    title=_optional_string(item.get("title")),
                    source=_optional_string(item.get("source")),
                    image_url=_optional_string(item.get("image")),
                    thumbnail_url=_optional_string(item.get("thumbnail")),
                    exact_match=_optional_bool(item.get("exact_matches")),
                    post_id=extract_post_id(normalized),
                )
            )

        search_id = str(metadata.get("id") or image_id)
        sanitized_params = {key: value for key, value in params.items() if key != "api_key"}
        return SearchRun.create(
            provider=self.name,
            search_id=search_id,
            candidates=candidates,
            raw_response={
                "query_upload": {
                    "filename": upload_name,
                    "media_type": media_type,
                    "byte_size": len(upload_bytes),
                    "sha256": hashlib.sha256(upload_bytes).hexdigest(),
                    "base64": base64.b64encode(upload_bytes).decode("ascii"),
                    "metadata_stripped": True,
                },
                "upload": redact_secrets(upload, secret_values=(self.api_key,)),
                "search": redact_secrets(body, secret_values=(self.api_key,)),
                "raw_http_body_sha256": {
                    "upload": hashlib.sha256(upload_raw).hexdigest(),
                    "search": hashlib.sha256(search_raw).hexdigest(),
                },
                "request_parameters": sanitized_params,
            },
            live=self.no_cache,
            provider_mode="no-cache" if self.no_cache else "cache-allowed",
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
    except (OSError, ValueError) as exc:
        raise SearchError(f"Cannot prepare image for SerpApi: {exc}") from exc
    raise SearchError("Image could not be compressed below SerpApi's 500KB limit")


def _optional_string(value: Any) -> str | None:
    return str(value) if value is not None else None


def _optional_bool(value: Any) -> bool | None:
    return value if type(value) is bool else None


def _positive_int(value: Any, fallback: int) -> int:
    if type(value) is int and value > 0:
        return value
    return fallback


def _required_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _non_negative_int(value: Any, field: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return value


def _safe_http_error(error: httpx.HTTPError) -> str:
    """Describe transport failures without echoing query-string API keys."""
    if isinstance(error, httpx.HTTPStatusError):
        return f"HTTP {error.response.status_code}"
    return type(error).__name__
