from __future__ import annotations

import base64
import binascii
import hashlib
import ipaddress
import json
import socket
from collections.abc import Callable
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx
from PIL import Image

from .search.base import SearchCandidate, extract_post_id, is_social_post_url


class CaptureError(RuntimeError):
    """Raised when discovered public evidence cannot be safely captured."""


@dataclass(frozen=True, slots=True)
class CapturedFile:
    relative_path: str
    sha256: str
    byte_size: int
    media_type: str
    source_url: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class PostCapture:
    status: str
    method: str
    artifacts: tuple[CapturedFile, ...]
    metadata: dict[str, Any]
    media_artifacts: tuple[CapturedFile, ...] = ()


class _OpenGraphParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.values: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "meta":
            return
        values = {key.lower(): value for key, value in attrs if value is not None}
        key = values.get("property") or values.get("name")
        content = values.get("content")
        if key and content and (key.startswith("og:") or key.startswith("twitter:")):
            self.values[key] = content


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.urls: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        values = {key.lower(): value for key, value in attrs if value is not None}
        if values.get("href"):
            self.urls.append(values["href"])


_AUTOMATED_CAPTURE_DISABLED_HOSTS = (
    "facebook.com",
    "instagram.com",
    "linkedin.com",
    "tiktok.com",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def materialize_candidate_image(
    candidate: SearchCandidate,
    destination: Path,
    *,
    timeout_seconds: float = 30,
    max_bytes: int = 12 * 1024 * 1024,
    client: httpx.Client | None = None,
    validate_url: Callable[[str], None] | None = None,
) -> CapturedFile:
    """Persist the exact provider thumbnail or referenced candidate image bytes."""
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)

    raw: bytes | None = None
    source_url: str | None = None
    declared_type: str | None = None
    failures: list[str] = []

    if candidate.thumbnail_base64:
        try:
            raw, declared_type = decode_data_image(candidate.thumbnail_base64, max_bytes=max_bytes)
        except CaptureError as exc:
            failures.append(str(exc))

    if raw is None:
        owns_client = client is None
        http = client or httpx.Client(
            timeout=timeout_seconds,
            headers={"User-Agent": "FaceProof/0.1 evidence-capture"},
        )
        try:
            for url in (candidate.image_url, candidate.thumbnail_url):
                if not url:
                    continue
                try:
                    raw, declared_type, source_url = fetch_public_bytes(
                        url,
                        client=http,
                        max_bytes=max_bytes,
                        accepted_media_prefixes=("image/",),
                        validate_url=validate_url,
                    )
                    break
                except CaptureError as exc:
                    failures.append(f"{url}: {exc}")
        finally:
            if owns_client:
                http.close()

    if raw is None:
        details = "; ".join(failures) or "provider returned no candidate image"
        raise CaptureError(f"Could not materialize candidate image: {details}")
    if len(raw) > max_bytes:
        raise CaptureError(f"Candidate image exceeds {max_bytes} bytes")

    media_type, extension = inspect_image(raw, declared_type)
    output = destination / f"candidate_media{extension}"
    output.write_bytes(raw)
    return CapturedFile(
        relative_path=output.name,
        sha256=hashlib.sha256(raw).hexdigest(),
        byte_size=len(raw),
        media_type=media_type,
        source_url=source_url,
    )


def capture_public_post(
    candidate: SearchCandidate,
    destination: Path,
    *,
    timeout_seconds: float = 30,
    client: httpx.Client | None = None,
    validate_url: Callable[[str], None] | None = None,
) -> PostCapture:
    """Capture public metadata without logging in or bypassing access controls.

    Capture failure is recorded rather than disguised as successful platform
    attestation. The search receipt and candidate media remain usable evidence.
    """
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    if not is_social_post_url(candidate.normalized_url):
        raise CaptureError("Refusing to capture a URL that is not a recognized post permalink")

    owns_client = client is None
    http = client or httpx.Client(
        timeout=timeout_seconds,
        headers={"User-Agent": "FaceProof/0.1 evidence-capture"},
    )
    try:
        hostname = (urlsplit(candidate.normalized_url).hostname or "").lower()
        if any(
            hostname == blocked or hostname.endswith(f".{blocked}")
            for blocked in _AUTOMATED_CAPTURE_DISABLED_HOSTS
        ):
            return _record_capture_disabled(candidate, destination)
        if hostname.endswith(("x.com", "twitter.com")):
            return _capture_x_oembed(candidate, destination, client=http, validate_url=validate_url)
        return _capture_public_html(candidate, destination, client=http, validate_url=validate_url)
    except CaptureError as exc:
        metadata = {
            "status": "unavailable",
            "method": "public-http",
            "page_url": candidate.normalized_url,
            "post_id": candidate.post_id,
            "post_identity_verified": False,
            "captured_at": _now_iso(),
            "error": str(exc),
        }
        artifact = _write_json_artifact(destination / "capture_status.json", metadata)
        return PostCapture(
            status="unavailable",
            method="public-http",
            artifacts=(artifact,),
            metadata=metadata,
        )
    finally:
        if owns_client:
            http.close()


def decode_data_image(value: str, *, max_bytes: int = 12 * 1024 * 1024) -> tuple[bytes, str | None]:
    text = value.strip()
    media_type: str | None = None
    if text.startswith("data:"):
        try:
            header, text = text.split(",", 1)
        except ValueError as exc:
            raise CaptureError("Malformed data URI thumbnail") from exc
        if ";base64" not in header.lower():
            raise CaptureError("Only base64 data URI thumbnails are supported")
        media_type = header[5:].split(";", 1)[0].lower() or None
    text = "".join(text.split())
    if len(text) > ((max_bytes + 2) // 3) * 4 + 4:
        raise CaptureError(f"Provider thumbnail exceeds {max_bytes} decoded bytes")
    try:
        raw = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise CaptureError("Provider thumbnail contains invalid base64") from exc
    if not raw:
        raise CaptureError("Provider thumbnail decoded to an empty file")
    if len(raw) > max_bytes:
        raise CaptureError(f"Provider thumbnail exceeds {max_bytes} decoded bytes")
    return raw, media_type


def inspect_image(
    raw: bytes,
    declared_type: str | None = None,
    *,
    max_pixels: int = 40_000_000,
) -> tuple[str, str]:
    try:
        from io import BytesIO

        with Image.open(BytesIO(raw)) as image:
            width, height = image.size
            if width <= 0 or height <= 0 or width * height > max_pixels:
                raise CaptureError(f"Candidate image exceeds {max_pixels} decoded pixels")
            image.verify()
            image_format = (image.format or "").upper()
    except CaptureError:
        raise
    except (OSError, ValueError) as exc:
        raise CaptureError("Candidate bytes are not a valid image") from exc

    formats = {
        "JPEG": ("image/jpeg", ".jpg"),
        "PNG": ("image/png", ".png"),
        "WEBP": ("image/webp", ".webp"),
        "GIF": ("image/gif", ".gif"),
    }
    if image_format not in formats:
        raise CaptureError(f"Unsupported candidate image format: {image_format}")
    detected_type, extension = formats[image_format]
    if declared_type and declared_type.startswith("image/") and declared_type != detected_type:
        # The detected bytes are authoritative; the mismatch is deliberately not fatal.
        pass
    return detected_type, extension


def fetch_public_bytes(
    url: str,
    *,
    client: httpx.Client,
    max_bytes: int,
    accepted_media_prefixes: tuple[str, ...],
    validate_url: Callable[[str], None] | None = None,
    max_redirects: int = 4,
) -> tuple[bytes, str, str]:
    validator = validate_url or validate_public_url
    current = url
    for _ in range(max_redirects + 1):
        validator(current)
        try:
            with client.stream("GET", current, follow_redirects=False) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location:
                        raise CaptureError("Redirect response had no Location header")
                    current = urljoin(current, location)
                    continue
                response.raise_for_status()
                media_type = response.headers.get("content-type", "application/octet-stream")
                media_type = media_type.split(";", 1)[0].strip().lower()
                if not any(media_type.startswith(prefix) for prefix in accepted_media_prefixes):
                    raise CaptureError(f"Unexpected content type: {media_type}")
                declared_length = response.headers.get("content-length")
                if declared_length:
                    try:
                        parsed_length = int(declared_length)
                    except ValueError as exc:
                        raise CaptureError("Response has an invalid Content-Length") from exc
                    if parsed_length < 0:
                        raise CaptureError("Response has a negative Content-Length")
                    if parsed_length > max_bytes:
                        raise CaptureError(f"Response exceeds {max_bytes} bytes")
                output = bytearray()
                for chunk in response.iter_bytes():
                    output.extend(chunk)
                    if len(output) > max_bytes:
                        raise CaptureError(f"Response exceeds {max_bytes} bytes")
                return bytes(output), media_type, current
        except httpx.HTTPError as exc:
            raise CaptureError(f"HTTP fetch failed: {exc}") from exc
    raise CaptureError(f"Too many redirects (>{max_redirects})")


def validate_public_url(url: str) -> None:
    """Reject local/private network targets before evidence downloads."""
    parts = urlsplit(url)
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        raise CaptureError("Only HTTP(S) URLs are allowed")
    if parts.username or parts.password:
        raise CaptureError("URLs containing credentials are not allowed")
    try:
        default_port = 443 if parts.scheme.lower() == "https" else 80
        addresses = socket.getaddrinfo(
            parts.hostname, parts.port or default_port, type=socket.SOCK_STREAM
        )
    except OSError as exc:
        raise CaptureError(f"Could not resolve host {parts.hostname!r}") from exc
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if not ip.is_global:
            raise CaptureError(f"Non-public target address is not allowed: {ip}")


def _capture_x_oembed(
    candidate: SearchCandidate,
    destination: Path,
    *,
    client: httpx.Client,
    validate_url: Callable[[str], None] | None,
) -> PostCapture:
    # The legacy publish.twitter.com endpoint now redirects; use the current
    # endpoint directly so capture does not silently depend on redirect policy.
    endpoint = "https://publish.x.com/oembed"
    validator = validate_url or validate_public_url
    validator(endpoint)
    try:
        response = client.get(
            endpoint,
            params={
                "url": candidate.normalized_url,
                "omit_script": "true",
                "dnt": "true",
            },
        )
        response.raise_for_status()
        body = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise CaptureError(f"X oEmbed capture failed: {exc}") from exc

    if not isinstance(body, dict):
        raise CaptureError("X oEmbed returned a non-object response")
    post_id = extract_post_id(candidate.normalized_url)
    identity_urls: list[str] = []
    response_url = body.get("url")
    if isinstance(response_url, str):
        identity_urls.append(response_url)
    response_html = body.get("html")
    if isinstance(response_html, str):
        link_parser = _LinkParser()
        link_parser.feed(response_html)
        identity_urls.extend(link_parser.urls)
    if not post_id or not any(extract_post_id(url) == post_id for url in identity_urls):
        raise CaptureError("X oEmbed response did not identify the requested post")

    media_artifacts: list[CapturedFile] = []
    thumbnail_url = body.get("thumbnail_url")
    if isinstance(thumbnail_url, str) and thumbnail_url:
        # The post identity remains independently confirmed by oEmbed even
        # when a platform does not expose a downloadable thumbnail.
        with suppress(CaptureError):
            media_artifacts.append(
                _capture_remote_image(
                    thumbnail_url,
                    destination / "post_media",
                    client=client,
                    validate_url=validate_url,
                )
            )

    metadata = {
        "status": "captured",
        "method": "x-oembed",
        "page_url": candidate.normalized_url,
        "post_id": post_id,
        "post_identity_verified": True,
        "captured_at": _now_iso(),
        "response": body,
        "response_headers": _safe_headers(response.headers),
        "linked_media": [item.to_dict() for item in media_artifacts],
    }
    artifact = _write_json_artifact(destination / "post_oembed.json", metadata)
    return PostCapture(
        status="captured",
        method="x-oembed",
        artifacts=(artifact, *media_artifacts),
        metadata=metadata,
        media_artifacts=tuple(media_artifacts),
    )


def _record_capture_disabled(candidate: SearchCandidate, destination: Path) -> PostCapture:
    metadata = {
        "status": "not-attempted",
        "method": "provider-evidence-only",
        "page_url": candidate.normalized_url,
        "post_id": candidate.post_id,
        "post_identity_verified": False,
        "captured_at": _now_iso(),
        "reason": "automated platform capture disabled; no login or access-control bypass",
    }
    artifact = _write_json_artifact(destination / "capture_status.json", metadata)
    return PostCapture(
        status="not-attempted",
        method="provider-evidence-only",
        artifacts=(artifact,),
        metadata=metadata,
    )


def _capture_public_html(
    candidate: SearchCandidate,
    destination: Path,
    *,
    client: httpx.Client,
    validate_url: Callable[[str], None] | None,
) -> PostCapture:
    raw, media_type, final_url = fetch_public_bytes(
        candidate.normalized_url,
        client=client,
        max_bytes=3 * 1024 * 1024,
        accepted_media_prefixes=("text/html", "application/xhtml+xml"),
        validate_url=validate_url,
    )
    html_path = destination / "post_page.html"
    html_path.write_bytes(raw)
    html_artifact = CapturedFile(
        relative_path=html_path.name,
        sha256=hashlib.sha256(raw).hexdigest(),
        byte_size=len(raw),
        media_type=media_type,
        source_url=final_url,
    )

    parser = _OpenGraphParser()
    decoded_html = raw.decode("utf-8", errors="replace")
    parser.feed(decoded_html)
    requested_post_id = extract_post_id(candidate.normalized_url)
    final_post_id = extract_post_id(final_url)
    if not requested_post_id or final_post_id != requested_post_id:
        raise CaptureError("Post capture redirected to a different or non-post URL")
    identity_url = parser.values.get("og:url")
    if identity_url and extract_post_id(identity_url) != requested_post_id:
        raise CaptureError("Captured page metadata identifies a different post")
    meaningful_metadata = any(
        parser.values.get(key)
        for key in (
            "og:title",
            "twitter:title",
            "og:description",
            "twitter:description",
            "og:image",
            "twitter:image",
        )
    )
    if not meaningful_metadata:
        raise CaptureError(
            "Captured HTML has no post metadata; it may be a login or challenge page"
        )

    media_artifacts: list[CapturedFile] = []
    linked_media_error: str | None = None
    media_url = parser.values.get("og:image") or parser.values.get("twitter:image")
    if media_url:
        try:
            media_artifacts.append(
                _capture_remote_image(
                    media_url,
                    destination / "post_media",
                    client=client,
                    validate_url=validate_url,
                )
            )
        except CaptureError as exc:
            linked_media_error = str(exc)
    metadata = {
        "status": "captured",
        "method": "public-html",
        "page_url": candidate.normalized_url,
        "final_url": final_url,
        "post_id": requested_post_id,
        "post_identity_verified": True,
        "captured_at": _now_iso(),
        "open_graph": parser.values,
        "linked_media": [item.to_dict() for item in media_artifacts],
        "linked_media_error": linked_media_error,
    }
    metadata_artifact = _write_json_artifact(destination / "post_metadata.json", metadata)
    return PostCapture(
        status="captured",
        method="public-html",
        artifacts=(html_artifact, metadata_artifact, *media_artifacts),
        metadata=metadata,
        media_artifacts=tuple(media_artifacts),
    )


def _capture_remote_image(
    url: str,
    destination_stem: Path,
    *,
    client: httpx.Client,
    validate_url: Callable[[str], None] | None,
) -> CapturedFile:
    raw, declared_type, final_url = fetch_public_bytes(
        url,
        client=client,
        max_bytes=12 * 1024 * 1024,
        accepted_media_prefixes=("image/",),
        validate_url=validate_url,
    )
    media_type, extension = inspect_image(raw, declared_type)
    output = destination_stem.with_suffix(extension)
    output.write_bytes(raw)
    return CapturedFile(
        relative_path=output.name,
        sha256=hashlib.sha256(raw).hexdigest(),
        byte_size=len(raw),
        media_type=media_type,
        source_url=final_url,
    )


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _safe_headers(headers: httpx.Headers) -> dict[str, str]:
    allowed = {"content-type", "date", "etag", "last-modified", "x-request-id"}
    return {key.lower(): value for key, value in headers.items() if key.lower() in allowed}


def _write_json_artifact(path: Path, value: dict[str, Any]) -> CapturedFile:
    raw = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    path.write_bytes(raw)
    return CapturedFile(
        relative_path=path.name,
        sha256=hashlib.sha256(raw).hexdigest(),
        byte_size=len(raw),
        media_type="application/json",
    )
