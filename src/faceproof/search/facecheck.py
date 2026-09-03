from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Any

import httpx

from .base import (
    SearchCandidate,
    SearchError,
    SearchRun,
    extract_post_id,
    normalize_page_url,
    redact_secrets,
)


class FaceCheckProvider:
    """FaceCheck.ID live face-search adapter.

    Testing mode is intentionally explicit because the vendor states that its
    reduced testing index does not produce meaningful matches.
    """

    name = "facecheck"

    def __init__(
        self,
        api_token: str,
        *,
        testing_mode: bool = False,
        timeout_seconds: float = 30,
        poll_interval_seconds: float = 1,
        max_wait_seconds: float = 180,
        base_url: str = "https://facecheck.id",
        client: httpx.Client | None = None,
    ) -> None:
        if not api_token.strip():
            raise ValueError("FaceCheck API token is required")
        self._api_token = api_token
        self._headers = {"accept": "application/json", "Authorization": api_token}
        self.testing_mode = testing_mode
        self.poll_interval_seconds = poll_interval_seconds
        self.max_wait_seconds = max_wait_seconds
        self.base_url = base_url.rstrip("/")
        self._owns_client = client is None
        self.client = client or httpx.Client(timeout=timeout_seconds)

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def __enter__(self) -> FaceCheckProvider:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def search(self, image_path: Path) -> SearchRun:
        image_path = Path(image_path)
        if not image_path.is_file():
            raise SearchError(f"Image does not exist: {image_path}")

        try:
            with image_path.open("rb") as image_handle:
                upload_response = self.client.post(
                    f"{self.base_url}/api/upload_pic",
                    headers=self._headers,
                    files={"images": (image_path.name, image_handle, "application/octet-stream")},
                )
            upload_response.raise_for_status()
            upload_raw = bytes(upload_response.content)
            upload = upload_response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise SearchError(f"FaceCheck upload failed: {exc}") from exc

        self._raise_api_error(upload, "upload")
        search_id = str(upload.get("id_search") or "")
        if not search_id:
            raise SearchError("FaceCheck upload response did not contain id_search")

        payload = {
            "id_search": search_id,
            "with_progress": True,
            "status_only": False,
            "demo": self.testing_mode,
        }
        deadline = time.monotonic() + self.max_wait_seconds
        polls: list[dict[str, Any]] = []
        poll_bodies: list[bytes] = []
        final: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            try:
                response = self.client.post(
                    f"{self.base_url}/api/search", headers=self._headers, json=payload
                )
                response.raise_for_status()
                poll_bodies.append(bytes(response.content))
                body = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                raise SearchError(f"FaceCheck search failed: {exc}") from exc
            self._raise_api_error(body, "search")
            polls.append(body)
            if body.get("output") is not None:
                final = body
                break
            time.sleep(self.poll_interval_seconds)

        if final is None:
            raise SearchError(
                f"FaceCheck search {search_id} timed out after {self.max_wait_seconds:g}s"
            )

        items = (final.get("output") or {}).get("items") or []
        authoritative_demo = bool((final.get("output") or {}).get("demo"))
        candidates: list[SearchCandidate] = []
        for fallback_rank, item in enumerate(items, start=1):
            if not isinstance(item, dict):
                continue
            raw_url = item.get("url")
            if isinstance(raw_url, dict):
                raw_url = raw_url.get("value")
            if not isinstance(raw_url, str) or not raw_url.strip():
                continue
            try:
                normalized = normalize_page_url(raw_url)
            except ValueError:
                continue
            rank = int(item.get("index", fallback_rank))
            if rank < 0:
                rank = fallback_rank
            candidates.append(
                SearchCandidate(
                    provider=self.name,
                    rank=rank,
                    page_url=raw_url,
                    normalized_url=normalized,
                    source="FaceCheck.ID",
                    thumbnail_base64=item.get("base64"),
                    provider_score=_optional_float(item.get("score")),
                    provider_item_id=_optional_string(item.get("guid")),
                    post_id=extract_post_id(normalized),
                )
            )

        return SearchRun.create(
            provider=self.name,
            search_id=search_id,
            candidates=candidates,
            raw_response={
                "upload": redact_secrets(upload, secret_values=(self._api_token,)),
                "polls": redact_secrets(polls, secret_values=(self._api_token,)),
                "raw_http_body_sha256": {
                    "upload": hashlib.sha256(upload_raw).hexdigest(),
                    "polls": [hashlib.sha256(item).hexdigest() for item in poll_bodies],
                },
            },
            live=True,
            provider_mode=("testing" if self.testing_mode or authoritative_demo else "production"),
        )

    def _raise_api_error(self, body: dict[str, Any], operation: str) -> None:
        if body.get("error"):
            error = redact_secrets(str(body["error"]), secret_values=(self._api_token,))
            code = redact_secrets(
                str(body.get("code", "unknown")), secret_values=(self._api_token,)
            )
            raise SearchError(f"FaceCheck {operation} error: {error} ({code})")


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_string(value: Any) -> str | None:
    return str(value) if value is not None else None
