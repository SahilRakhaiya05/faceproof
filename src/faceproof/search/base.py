from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

SOCIAL_HOSTS = frozenset(
    {
        "bsky.app",
        "facebook.com",
        "instagram.com",
        "linkedin.com",
        "m.facebook.com",
        "reddit.com",
        "tiktok.com",
        "twitter.com",
        "x.com",
        "www.facebook.com",
        "www.instagram.com",
        "www.linkedin.com",
        "www.reddit.com",
        "www.tiktok.com",
        "www.twitter.com",
        "www.x.com",
        "youtube.com",
        "www.youtube.com",
    }
)

_TRACKING_KEYS = {
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "ref_src",
    "ref_url",
}


class SearchError(RuntimeError):
    """Raised when a provider request or response is invalid."""


@dataclass(frozen=True, slots=True)
class SearchCandidate:
    provider: str
    rank: int
    page_url: str
    normalized_url: str
    title: str | None = None
    source: str | None = None
    image_url: str | None = None
    thumbnail_url: str | None = None
    thumbnail_base64: str | None = field(default=None, repr=False)
    provider_score: float | None = None
    exact_match: bool | None = None
    provider_item_id: str | None = None
    post_id: str | None = None

    def public_dict(self) -> dict[str, Any]:
        """Return serializable metadata without embedding bulky thumbnail bytes."""
        data = asdict(self)
        data.pop("thumbnail_base64", None)
        return data


@dataclass(frozen=True, slots=True)
class SearchRun:
    provider: str
    search_id: str
    retrieved_at: str
    candidates: tuple[SearchCandidate, ...]
    raw_response: dict[str, Any] = field(repr=False)
    live: bool = True
    provider_mode: str = "production"

    @classmethod
    def create(
        cls,
        *,
        provider: str,
        search_id: str,
        candidates: list[SearchCandidate],
        raw_response: dict[str, Any],
        live: bool,
        provider_mode: str,
    ) -> SearchRun:
        return cls(
            provider=provider,
            search_id=search_id,
            retrieved_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            candidates=tuple(candidates),
            raw_response=raw_response,
            live=live,
            provider_mode=provider_mode,
        )


class SearchProvider(Protocol):
    name: str

    def search(self, image_path: Path) -> SearchRun: ...


def normalize_page_url(url: str) -> str:
    parts = urlsplit(url.strip())
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        raise ValueError(f"Expected a public HTTP(S) URL, got: {url!r}")

    hostname = parts.hostname.lower().rstrip(".")
    if parts.port and not (
        (parts.scheme.lower() == "http" and parts.port == 80)
        or (parts.scheme.lower() == "https" and parts.port == 443)
    ):
        netloc = f"{hostname}:{parts.port}"
    else:
        netloc = hostname

    query_items = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        lower = key.lower()
        if lower.startswith("utm_") or lower in _TRACKING_KEYS:
            continue
        query_items.append((key, value))

    path = re.sub(r"/{2,}", "/", parts.path or "/")
    if path != "/":
        path = path.rstrip("/")
    return urlunsplit((parts.scheme.lower(), netloc, path, urlencode(query_items, doseq=True), ""))


def is_social_url(url: str) -> bool:
    try:
        host = (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    return any(host == allowed or host.endswith(f".{allowed}") for allowed in SOCIAL_HOSTS)


def extract_post_id(url: str) -> str | None:
    parts = urlsplit(url)
    hostname = (parts.hostname or "").lower().rstrip(".")
    path = parts.path
    query = dict(parse_qsl(parts.query))

    if hostname.endswith("youtu.be"):
        return parts.path.strip("/") or None
    if hostname.endswith("youtube.com"):
        video_ids = query.get("v")
        if video_ids:
            return video_ids
        match = re.search(r"/(?:shorts|live)/([^/?#]+)", path, re.IGNORECASE)
        return match.group(1) if match else None

    patterns: tuple[re.Pattern[str], ...]
    if hostname.endswith(("x.com", "twitter.com")):
        patterns = (re.compile(r"/(?:i/web/)?status/(\d+)", re.IGNORECASE),)
    elif hostname.endswith("reddit.com"):
        patterns = (re.compile(r"/comments/([^/?#]+)", re.IGNORECASE),)
    elif hostname.endswith("instagram.com"):
        patterns = (re.compile(r"/(?:p|reel|tv)/([^/?#]+)", re.IGNORECASE),)
    elif hostname.endswith("tiktok.com"):
        patterns = (re.compile(r"/video/(\d+)", re.IGNORECASE),)
    elif hostname.endswith("bsky.app"):
        match = re.search(r"/profile/([^/?#]+)/post/([^/?#]+)", path, re.IGNORECASE)
        return f"{match.group(1)}/{match.group(2)}" if match else None
    elif hostname.endswith("linkedin.com"):
        patterns = (
            re.compile(r"/feed/update/urn:li:(?:activity|share):(\d+)", re.IGNORECASE),
            re.compile(r"/posts/([^/?#]+)", re.IGNORECASE),
        )
    elif hostname.endswith("facebook.com"):
        for key in ("story_fbid", "fbid", "v"):
            if query.get(key):
                return query[key]
        patterns = (
            re.compile(r"/(?:posts|reel|videos)/([^/?#]+)", re.IGNORECASE),
            re.compile(r"/permalink/([^/?#]+)", re.IGNORECASE),
        )
    else:
        return None

    for pattern in patterns:
        match = pattern.search(path)
        if match:
            return match.group(1)
    return None


def is_social_post_url(url: str) -> bool:
    """Return true only for a recognized platform permalink with a stable post ID."""
    return is_social_url(url) and extract_post_id(url) is not None


def filter_social_candidates(
    candidates: list[SearchCandidate] | tuple[SearchCandidate, ...],
    *,
    limit: int | None = None,
) -> list[SearchCandidate]:
    """Filter to stable social-post permalinks and de-duplicate normalized URLs."""
    result: list[SearchCandidate] = []
    seen: set[str] = set()
    for candidate in sorted(candidates, key=lambda item: item.rank):
        try:
            normalized_url = normalize_page_url(candidate.normalized_url)
        except ValueError:
            continue
        post_id = extract_post_id(normalized_url)
        if not is_social_url(normalized_url) or not post_id:
            continue
        if normalized_url in seen:
            continue
        seen.add(normalized_url)
        result.append(replace(candidate, normalized_url=normalized_url, post_id=post_id))
        if limit is not None and len(result) >= limit:
            break
    return result
