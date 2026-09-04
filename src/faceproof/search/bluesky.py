from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlsplit

import httpx

from ._http import request_json_object
from .base import SearchCandidate, SearchError, SearchRun, redact_secrets

BLUESKY_PUBLIC_API = "https://public.api.bsky.app"
BLUESKY_AUTHOR_FEED_ENDPOINT = f"{BLUESKY_PUBLIC_API}/xrpc/app.bsky.feed.getAuthorFeed"
BLUESKY_GET_POSTS_ENDPOINT = f"{BLUESKY_PUBLIC_API}/xrpc/app.bsky.feed.getPosts"
BLUESKY_GET_POST_THREAD_ENDPOINT = f"{BLUESKY_PUBLIC_API}/xrpc/app.bsky.feed.getPostThread"
BLUESKY_CDN_HOSTS = frozenset({"cdn.bsky.app", "cdn.bsky.social"})

_HANDLE_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_HANDLE_TLD = r"[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?"
_HANDLE_RE = re.compile(rf"^(?:{_HANDLE_LABEL}\.)+{_HANDLE_TLD}$")
_DID_RE = re.compile(r"^did:[a-z0-9]+:[A-Za-z0-9._:%-]{1,1900}$")
_CID_RE = re.compile(r"^b[a-z2-7]{19,127}$")
_RKEY_RE = re.compile(r"^[A-Za-z0-9._~:-]{1,512}$")
# AppView currently emits a bare blob CID. Older/cached views may append a
# rendition format such as ``@jpeg``; both shapes bind to the same record CID.
_CDN_RENDER_RE = re.compile(r"^(?P<cid>[a-z2-7]{20,128})(?:@(?P<format>[a-z0-9]+))?$")


@dataclass(frozen=True, slots=True)
class BlueskyEvidenceRef:
    """Strong identifiers needed to hydrate and re-check one Bluesky image."""

    at_uri: str
    post_cid: str
    image_cid: str

    @property
    def did(self) -> str:
        return parse_post_at_uri(self.at_uri)[0]

    @property
    def rkey(self) -> str:
        return parse_post_at_uri(self.at_uri)[1]

    def serialize(self) -> str:
        # AT-URIs and CIDs cannot contain a vertical bar, making this both
        # human-auditable and unambiguous without opaque base64 encoding.
        return f"{self.at_uri}|{self.post_cid}|{self.image_cid}"

    @classmethod
    def parse(cls, value: str) -> BlueskyEvidenceRef:
        if not isinstance(value, str):
            raise ValueError("Bluesky evidence identifier must be text")
        parts = value.split("|")
        if len(parts) != 3:
            raise ValueError("Bluesky evidence identifier is malformed")
        at_uri, post_cid, image_cid = parts
        parse_post_at_uri(at_uri)
        validate_cid(post_cid)
        validate_cid(image_cid)
        return cls(at_uri=at_uri, post_cid=post_cid, image_cid=image_cid)


@dataclass(frozen=True, slots=True)
class BlueskyPostImage:
    ref: BlueskyEvidenceRef
    did: str
    rkey: str
    handle: str
    text: str
    fullsize_url: str
    thumbnail_url: str
    alt: str


def normalize_bluesky_actor(actor: str) -> str:
    """Validate and normalize a runtime-supplied AT Protocol handle or DID."""

    if not isinstance(actor, str):
        raise ValueError("Bluesky actor must be a handle or DID")
    value = actor.strip()
    if not value or len(value) > 2048 or any(character.isspace() for character in value):
        raise ValueError("Bluesky actor must be a valid handle or DID")
    if value.casefold().startswith("did:"):
        if not _DID_RE.fullmatch(value):
            raise ValueError("Bluesky actor DID is invalid")
        return value
    value = value.casefold()
    if len(value) > 253 or not _HANDLE_RE.fullmatch(value):
        raise ValueError("Bluesky actor handle is invalid")
    return value


def validate_cid(value: Any) -> str:
    if not isinstance(value, str) or not _CID_RE.fullmatch(value):
        raise ValueError("Invalid AT Protocol CID")
    return value


def parse_post_at_uri(value: str) -> tuple[str, str]:
    """Return the DID and rkey from an exact Bluesky post AT-URI."""

    if not isinstance(value, str) or not value.startswith("at://"):
        raise ValueError("Expected a Bluesky post AT-URI")
    remainder = value[5:]
    parts = remainder.split("/")
    if len(parts) != 3 or parts[1] != "app.bsky.feed.post":
        raise ValueError("Expected an app.bsky.feed.post AT-URI")
    did, _, rkey = parts
    if not _DID_RE.fullmatch(did) or not _RKEY_RE.fullmatch(rkey):
        raise ValueError("Bluesky post AT-URI is malformed")
    return did, rkey


def parse_bluesky_permalink(value: str) -> tuple[str, str]:
    """Return the actor and rkey from an exact HTTPS bsky.app permalink."""

    try:
        parts = urlsplit(value)
    except ValueError as exc:
        raise ValueError("Bluesky permalink is malformed") from exc
    if (
        parts.scheme.casefold() != "https"
        or (parts.hostname or "").casefold().rstrip(".") != "bsky.app"
        or parts.username
        or parts.password
        or parts.port not in {None, 443}
        or parts.query
        or parts.fragment
    ):
        raise ValueError("Expected a canonical HTTPS bsky.app permalink")
    segments = [unquote(segment) for segment in parts.path.split("/") if segment]
    if len(segments) != 4 or segments[0] != "profile" or segments[2] != "post":
        raise ValueError("Expected a canonical Bluesky post permalink")
    actor = normalize_bluesky_actor(segments[1])
    rkey = segments[3]
    if not _RKEY_RE.fullmatch(rkey):
        raise ValueError("Bluesky permalink rkey is invalid")
    return actor, rkey


def parse_bluesky_cdn_url(value: str) -> tuple[str, str, str]:
    """Validate an AppView image URL and return rendition, DID, and blob CID."""

    try:
        parts = urlsplit(value)
    except ValueError as exc:
        raise ValueError("Bluesky image URL is malformed") from exc
    host = (parts.hostname or "").casefold().rstrip(".")
    if (
        parts.scheme.casefold() != "https"
        or host not in BLUESKY_CDN_HOSTS
        or parts.username
        or parts.password
        or parts.port not in {None, 443}
        or parts.query
        or parts.fragment
    ):
        raise ValueError("Bluesky image URL is outside the allowlisted CDN")
    segments = [unquote(segment) for segment in parts.path.split("/") if segment]
    if (
        len(segments) != 5
        or segments[0] != "img"
        or segments[1] not in {"feed_fullsize", "feed_thumbnail"}
        or segments[2] != "plain"
        or not _DID_RE.fullmatch(segments[3])
    ):
        raise ValueError("Bluesky image URL has an unexpected CDN path")
    rendered = _CDN_RENDER_RE.fullmatch(segments[4])
    if rendered is None:
        raise ValueError("Bluesky image URL has no valid blob CID")
    return segments[1], segments[3], rendered.group("cid")


def extract_bluesky_post_images(post: Any) -> list[BlueskyPostImage]:
    """Strictly pair record blob CIDs with AppView image renditions."""

    if not isinstance(post, dict):
        raise ValueError("Bluesky post view must be an object")
    at_uri = _required_text(post.get("uri"), "post uri")
    did, rkey = parse_post_at_uri(at_uri)
    post_cid = validate_cid(post.get("cid"))
    author = post.get("author")
    if not isinstance(author, dict):
        raise ValueError("Bluesky post author is missing")
    author_did = _required_text(author.get("did"), "author DID")
    if author_did != did:
        raise ValueError("Bluesky post URI and author DID disagree")
    handle = normalize_bluesky_actor(_required_text(author.get("handle"), "author handle"))
    if handle.startswith("did:"):
        raise ValueError("Bluesky author handle is not a handle")

    record = post.get("record")
    if not isinstance(record, dict) or record.get("$type") != "app.bsky.feed.post":
        raise ValueError("Bluesky post record has an unexpected type")
    text = record.get("text") if isinstance(record.get("text"), str) else ""
    record_images = _record_image_items(record.get("embed"))
    view_images = _view_image_items(post.get("embed"))
    if len(record_images) != len(view_images):
        raise ValueError("Bluesky record blobs and AppView images do not align")

    output: list[BlueskyPostImage] = []
    for record_image, view_image in zip(record_images, view_images, strict=True):
        image_cid = _blob_cid(record_image.get("image"))
        fullsize = _required_text(view_image.get("fullsize"), "fullsize image URL")
        thumbnail = _required_text(
            view_image.get("thumb", view_image.get("thumbnail")),
            "thumbnail image URL",
        )
        for image_url, expected_rendition in (
            (fullsize, "feed_fullsize"),
            (thumbnail, "feed_thumbnail"),
        ):
            rendition, image_did, url_cid = parse_bluesky_cdn_url(image_url)
            if rendition != expected_rendition or image_did != did or url_cid != image_cid:
                raise ValueError("Bluesky image view does not match its record blob CID")
        alt = view_image.get("alt")
        if not isinstance(alt, str):
            raise ValueError("Bluesky image view has no alt text field")
        output.append(
            BlueskyPostImage(
                ref=BlueskyEvidenceRef(at_uri, post_cid, image_cid),
                did=did,
                rkey=rkey,
                handle=handle,
                text=text,
                fullsize_url=fullsize,
                thumbnail_url=thumbnail,
                alt=alt,
            )
        )
    return output


class BlueskyAuthorFeedProvider:
    """No-auth candidate discovery for one explicitly supplied Bluesky actor.

    The input portrait is never uploaded. This connector enumerates media from
    the consented account and lets FaceProof's local matcher compare the media.
    """

    name = "bluesky-public-api"

    def __init__(
        self,
        actor: str,
        *,
        page_size: int = 50,
        max_pages: int = 2,
        max_candidates: int = 100,
        timeout_seconds: float = 30,
        consent_confirmed: bool = False,
        client: httpx.Client | None = None,
    ) -> None:
        self.actor = normalize_bluesky_actor(actor)
        if consent_confirmed is not True:
            raise ValueError("Explicit consent is required before scanning a Bluesky account")
        if not 1 <= page_size <= 100:
            raise ValueError("Bluesky page_size must be between 1 and 100")
        if not 1 <= max_pages <= 10:
            raise ValueError("Bluesky max_pages must be between 1 and 10")
        if not 1 <= max_candidates <= 400:
            raise ValueError("Bluesky max_candidates must be between 1 and 400")
        self.page_size = page_size
        self.max_pages = max_pages
        self.max_candidates = max_candidates
        self._owns_client = client is None
        self.client = client or httpx.Client(
            timeout=timeout_seconds,
            headers={"User-Agent": "FaceProof/0.4 Bluesky-public-connector"},
        )

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def __enter__(self) -> BlueskyAuthorFeedProvider:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def search(self, image_path: Path) -> SearchRun:
        image_path = Path(image_path)
        if not image_path.is_file():
            raise SearchError(f"Input image does not exist: {image_path}")
        input_sha256 = _sha256_file(image_path)

        candidates: list[SearchCandidate] = []
        seen_images: set[tuple[str, str]] = set()
        seen_cursors: set[str] = set()
        page_records: list[dict[str, Any]] = []
        page_ids: list[str] = []
        cursor: str | None = None
        malformed_posts = 0
        non_image_posts = 0
        posts_skipped_candidate_limit = 0
        resolved_actor_did = self.actor if self.actor.startswith("did:") else None

        for page_number in range(1, self.max_pages + 1):
            params: dict[str, Any] = {
                "actor": self.actor,
                "filter": "posts_with_media",
                "includePins": "false",
                "limit": self.page_size,
            }
            if cursor is not None:
                params["cursor"] = cursor
            request_facts = {
                "method": "GET",
                "endpoint": BLUESKY_AUTHOR_FEED_ENDPOINT,
                "parameters": redact_secrets(params),
                "authentication": "none",
            }
            body, raw, response_headers = _get_json_object(
                self.client,
                BLUESKY_AUTHOR_FEED_ENDPOINT,
                params=params,
                operation="Bluesky author-feed search",
            )
            body_digest = hashlib.sha256(raw).hexdigest()
            page_id = f"bsky-page-{body_digest[:20]}"
            page_ids.append(page_id)
            page_records.append(
                {
                    "page": page_number,
                    "request": request_facts,
                    "request_facts_sha256": _json_sha256(request_facts),
                    "raw_http_body_sha256": body_digest,
                    "response_headers": _safe_headers(response_headers),
                    "response": redact_secrets(body),
                }
            )

            feed = body.get("feed")
            if not isinstance(feed, list):
                raise SearchError("Bluesky author-feed response has no feed array")
            for feed_item in feed:
                post = feed_item.get("post") if isinstance(feed_item, dict) else None
                try:
                    images = extract_bluesky_post_images(post)
                except ValueError:
                    malformed_posts += 1
                    continue
                if not images:
                    non_image_posts += 1
                    continue
                if not self._is_consented_author(images[0]):
                    continue
                if resolved_actor_did is None:
                    resolved_actor_did = images[0].did
                elif images[0].did != resolved_actor_did:
                    raise SearchError(
                        "Bluesky author-feed returned inconsistent DIDs for the consented actor"
                    )
                unseen_images = [
                    image
                    for image in images
                    if (image.ref.at_uri, image.ref.image_cid) not in seen_images
                ]
                # Admit posts atomically so a gallery is never truncated. If the
                # complete post cannot fit, stop at the documented hard cap.
                if len(candidates) + len(unseen_images) > self.max_candidates:
                    posts_skipped_candidate_limit += 1
                    continue
                for image in unseen_images:
                    dedupe_key = (image.ref.at_uri, image.ref.image_cid)
                    seen_images.add(dedupe_key)
                    rank = len(candidates) + 1
                    permalink = (
                        "https://bsky.app/profile/"
                        f"{quote(image.did, safe=':%')}/post/{quote(image.rkey, safe='')}"
                    )
                    candidates.append(
                        SearchCandidate(
                            provider=self.name,
                            rank=rank,
                            page_url=permalink,
                            normalized_url=permalink,
                            title=_candidate_title(image),
                            source="Bluesky public author feed (no authentication)",
                            image_url=image.fullsize_url,
                            thumbnail_url=image.thumbnail_url,
                            exact_match=None,
                            provider_item_id=image.ref.serialize(),
                            post_id=f"{image.did}/{image.rkey}",
                            result_type="consented_account_media",
                        )
                    )
                if len(candidates) >= self.max_candidates:
                    break
            if len(candidates) >= self.max_candidates:
                break

            next_cursor = body.get("cursor")
            if next_cursor is None:
                break
            if (
                not isinstance(next_cursor, str)
                or not next_cursor
                or len(next_cursor) > 4096
                or next_cursor in seen_cursors
            ):
                raise SearchError("Bluesky author-feed returned an invalid pagination cursor")
            seen_cursors.add(next_cursor)
            cursor = next_cursor

        aggregate_id = "bsky-" + hashlib.sha256("|".join(page_ids).encode()).hexdigest()[:24]
        raw_response = {
            "connector": "app.bsky.feed.getAuthorFeed",
            "actor": self.actor,
            "resolved_actor_did": resolved_actor_did,
            "filter": "posts_with_media",
            "authentication": "none",
            "consent_confirmed": True,
            "input_image": {
                "sha256": input_sha256,
                "transmitted_to_bluesky": False,
            },
            "candidate_count": len(candidates),
            "malformed_posts_skipped": malformed_posts,
            "non_image_media_posts_skipped": non_image_posts,
            "posts_skipped_candidate_limit": posts_skipped_candidate_limit,
            "pages": page_records,
        }
        return SearchRun.create(
            provider=self.name,
            search_id=aggregate_id,
            candidates=candidates,
            raw_response=raw_response,
            live=True,
            provider_mode="public-no-auth-consented-account",
            search_ids=page_ids,
            search_types=["posts_with_media"],
        )

    def _is_consented_author(self, image: BlueskyPostImage) -> bool:
        if self.actor.startswith("did:"):
            return image.did == self.actor
        return image.handle.casefold() == self.actor


def _record_image_items(embed: Any) -> list[dict[str, Any]]:
    if not isinstance(embed, dict):
        return []
    embed_type = embed.get("$type")
    if embed_type == "app.bsky.embed.recordWithMedia":
        embed = embed.get("media")
        if not isinstance(embed, dict):
            return []
        embed_type = embed.get("$type")
    if embed_type == "app.bsky.embed.images":
        images = embed.get("images")
        maximum = 4
        expected_item_type = None
    elif embed_type == "app.bsky.embed.gallery":
        images = embed.get("items")
        maximum = 20
        expected_item_type = "app.bsky.embed.gallery#image"
    else:
        return []
    if not isinstance(images, list) or not 1 <= len(images) <= maximum:
        return []
    if not all(isinstance(item, dict) for item in images):
        return []
    if expected_item_type and any(item.get("$type") != expected_item_type for item in images):
        return []
    return images


def _view_image_items(embed: Any) -> list[dict[str, Any]]:
    if not isinstance(embed, dict):
        return []
    embed_type = embed.get("$type")
    if embed_type == "app.bsky.embed.recordWithMedia#view":
        embed = embed.get("media")
        if not isinstance(embed, dict):
            return []
        embed_type = embed.get("$type")
    if embed_type == "app.bsky.embed.images#view":
        images = embed.get("images")
        maximum = 4
        expected_item_type = None
    elif embed_type == "app.bsky.embed.gallery#view":
        images = embed.get("items")
        maximum = 20
        expected_item_type = "app.bsky.embed.gallery#viewImage"
    else:
        return []
    if not isinstance(images, list) or not 1 <= len(images) <= maximum:
        return []
    if not all(isinstance(item, dict) for item in images):
        return []
    if expected_item_type and any(item.get("$type") != expected_item_type for item in images):
        return []
    return images


def _blob_cid(blob: Any) -> str:
    if not isinstance(blob, dict):
        raise ValueError("Bluesky image blob is missing")
    reference = blob.get("ref")
    if isinstance(reference, dict):
        value = reference.get("$link")
    elif isinstance(reference, str):
        value = reference
    else:
        value = blob.get("cid")
    return validate_cid(value)


def _candidate_title(image: BlueskyPostImage) -> str:
    description = image.text.strip() or image.alt.strip() or "Image post"
    description = " ".join(description.split())
    if len(description) > 240:
        description = description[:237] + "..."
    return f"@{image.handle}: {description}"


def _get_json_object(
    client: httpx.Client,
    endpoint: str,
    *,
    params: dict[str, Any],
    operation: str,
    max_bytes: int = 8 * 1024 * 1024,
) -> tuple[dict[str, Any], bytes, httpx.Headers]:
    response = request_json_object(
        client,
        "GET",
        endpoint,
        operation=operation,
        max_bytes=max_bytes,
        params=params,
    )
    return response.body, response.raw, response.headers


def _required_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"Bluesky {label} is missing")
    return value


def _safe_headers(headers: httpx.Headers) -> dict[str, str]:
    allowed = {"content-type", "date", "etag", "last-modified", "x-request-id"}
    return {key.casefold(): value for key, value in headers.items() if key.casefold() in allowed}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_sha256(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()
