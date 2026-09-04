from __future__ import annotations

import base64
import binascii
import hashlib
import ipaddress
import json
import math
import queue
import socket
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpcore
import httpx
from PIL import Image

from .search.base import (
    SearchCandidate,
    extract_post_id,
    is_social_post_url,
    redact_secrets,
    redact_url_secrets,
)
from .search.bluesky import (
    BLUESKY_CDN_HOSTS,
    BLUESKY_GET_POST_THREAD_ENDPOINT,
    BLUESKY_GET_POSTS_ENDPOINT,
    BlueskyEvidenceRef,
    extract_bluesky_post_images,
    parse_bluesky_cdn_url,
    parse_bluesky_permalink,
)


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

_PLATFORM_SUFFIXES = {
    "bsky": ("bsky.app",),
    "facebook": ("facebook.com",),
    "instagram": ("instagram.com",),
    "linkedin": ("linkedin.com",),
    "reddit": ("reddit.com",),
    "tiktok": ("tiktok.com",),
    "x": ("x.com", "twitter.com"),
    "youtube": ("youtube.com", "youtu.be"),
}

_MAX_RESOLVED_ADDRESSES = 4


def _is_allowed_public_ip(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Allow ordinary globally routed unicast addresses only."""

    return bool(
        address.is_global
        and not address.is_multicast
        and not address.is_unspecified
        and not address.is_reserved
        and not address.is_loopback
        and not address.is_link_local
        and not address.is_private
    )


class _PinnedPublicNetworkBackend(httpcore.NetworkBackend):
    """Resolve once, reject non-public IPs, and connect to the vetted literal IP.

    ``httpcore`` keeps the original request origin after ``connect_tcp`` returns,
    so its TLS upgrade still sends the original hostname as SNI and validates the
    certificate for that hostname. Only the TCP destination is replaced with a
    previously vetted literal address, closing the DNS validation/connect race.
    """

    def __init__(
        self,
        *,
        backend: httpcore.NetworkBackend | None = None,
        resolver: Callable[..., list[tuple[Any, ...]]] | None = None,
    ) -> None:
        self._backend = backend or httpcore.SyncBackend()
        self._resolver = resolver or socket.getaddrinfo

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore.NetworkStream:
        total_budget = float(timeout) if timeout is not None else 30.0
        if total_budget <= 0 or not math.isfinite(total_budget):
            raise httpcore.ConnectTimeout("Public destination resolution timed out")
        deadline = time.monotonic() + total_budget
        addresses = self._resolve_public_addresses(
            host,
            port,
            timeout=total_budget,
        )
        last_error: httpcore.ConnectError | httpcore.ConnectTimeout | None = None
        for address in addresses:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise httpcore.ConnectTimeout(
                    "Timed out before a validated public destination was reachable"
                )
            try:
                stream = self._backend.connect_tcp(
                    address,
                    port,
                    timeout=remaining,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last_error = exc
                continue
            try:
                peer = stream.get_extra_info("server_addr")
                peer_ip = (
                    ipaddress.ip_address(peer[0]) if isinstance(peer, tuple) and peer else None
                )
            except (TypeError, ValueError):
                peer_ip = None
            expected_ip = ipaddress.ip_address(address)
            if peer_ip is None or not _is_allowed_public_ip(peer_ip) or peer_ip != expected_ip:
                stream.close()
                raise httpcore.ConnectError(
                    "Connected peer did not match the validated public destination"
                )
            return stream
        if last_error is not None:
            raise last_error
        raise httpcore.ConnectError("No validated public destination was reachable")

    def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Any = None,
    ) -> httpcore.NetworkStream:
        del path, timeout, socket_options
        raise httpcore.ConnectError("Unix-socket transport is disabled for evidence capture")

    def _resolve_public_addresses(
        self,
        host: str,
        port: int,
        *,
        timeout: float,
    ) -> tuple[str, ...]:
        result_queue: queue.Queue[tuple[bool, object]] = queue.Queue(maxsize=1)

        def resolve() -> None:
            try:
                result = self._resolver(host, port, type=socket.SOCK_STREAM)
            except Exception as exc:  # resolver implementations expose platform-specific errors
                result_queue.put((False, exc))
            else:
                result_queue.put((True, result))

        resolver_thread = threading.Thread(
            target=resolve,
            daemon=True,
            name="faceproof-dns-resolver",
        )
        resolver_thread.start()
        resolver_thread.join(timeout)
        if resolver_thread.is_alive():
            raise httpcore.ConnectTimeout(f"Resolution timed out for evidence host {host!r}")
        try:
            succeeded, value = result_queue.get_nowait()
        except queue.Empty as exc:
            raise httpcore.ConnectError("Resolver returned no result") from exc
        if not succeeded:
            raise httpcore.ConnectError(f"Could not resolve public evidence host {host!r}") from (
                value if isinstance(value, Exception) else None
            )
        if not isinstance(value, (list, tuple)):
            raise httpcore.ConnectError("Resolver returned an invalid destination list")
        answers = value
        addresses: list[str] = []
        seen: set[str] = set()
        for answer in answers:
            try:
                raw_address = answer[4][0]
                address = ipaddress.ip_address(raw_address)
            except (IndexError, TypeError, ValueError) as exc:
                raise httpcore.ConnectError("Resolver returned an invalid destination") from exc
            if not _is_allowed_public_ip(address):
                raise httpcore.ConnectError(f"Non-public target address is not allowed: {address}")
            normalized = str(address)
            if normalized not in seen:
                seen.add(normalized)
                addresses.append(normalized)
                if len(addresses) >= _MAX_RESOLVED_ADDRESSES:
                    break
        if not addresses:
            raise httpcore.ConnectError(f"Could not resolve public evidence host {host!r}")
        return tuple(addresses)


class _PinnedPublicHTTPTransport(httpx.HTTPTransport):
    """HTTPX transport backed by a DNS-pinned, public-address-only connector."""

    def __init__(self, *, network_backend: httpcore.NetworkBackend | None = None) -> None:
        # HTTPTransport's request/response and exception mapping are retained;
        # only its connection pool is constructed with the hardened backend.
        self._pool = httpcore.ConnectionPool(
            ssl_context=httpx.create_ssl_context(verify=True, trust_env=False),
            network_backend=network_backend or _PinnedPublicNetworkBackend(),
            http1=True,
            http2=False,
            retries=0,
        )


def _public_http_client(*, timeout_seconds: float) -> httpx.Client:
    return httpx.Client(
        timeout=timeout_seconds,
        headers={"User-Agent": "FaceProof/0.4 evidence-capture"},
        transport=_PinnedPublicHTTPTransport(),
        trust_env=False,
    )


def _client_read_timeout(client: httpx.Client) -> float:
    value = client.timeout.read
    if isinstance(value, (int, float)) and value > 0 and math.isfinite(value):
        return float(value)
    return 30.0


def _host_matches(host: str, suffix: str) -> bool:
    return host == suffix or host.endswith(f".{suffix}")


def _platform_family(url: str) -> str | None:
    try:
        host = (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return None
    for family, suffixes in _PLATFORM_SUFFIXES.items():
        if any(_host_matches(host, suffix) for suffix in suffixes):
            return family
    return None


def _same_social_post(expected_url: str, observed_url: str) -> bool:
    """Match a post ID only within the same social-platform family."""

    try:
        if any(urlsplit(url).scheme.lower() != "https" for url in (expected_url, observed_url)):
            return False
    except ValueError:
        return False
    expected_family = _platform_family(expected_url)
    observed_family = _platform_family(observed_url)
    if expected_family is None or observed_family != expected_family:
        return False
    try:
        expected_id = extract_post_id(expected_url)
        observed_id = extract_post_id(observed_url)
    except ValueError:
        return False
    return bool(expected_id and observed_id and observed_id == expected_id)


def _post_media_relationship(post_url: str, media_url: str) -> str | None:
    """Classify only platform-specific content URLs as face-matchable post media."""

    family = _platform_family(post_url)
    try:
        parts = urlsplit(media_url)
        host = (parts.hostname or "").lower().rstrip(".")
    except ValueError:
        return None
    if parts.scheme.lower() != "https" or not host:
        return None
    if family == "x" and host in {"pbs.twimg.com", "video.twimg.com"}:
        return "x-content-cdn"
    if family == "reddit" and host in {
        "i.redd.it",
        "preview.redd.it",
        "external-preview.redd.it",
    }:
        return "reddit-content-cdn"
    if family == "youtube" and host in {"i.ytimg.com", "img.youtube.com"}:
        return "youtube-video-thumbnail"
    if (
        family == "bsky"
        and host in BLUESKY_CDN_HOSTS
        and parts.path.startswith("/img/feed_fullsize/plain/")
    ):
        return "bluesky-feed-media"
    return None


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
        http = client or _public_http_client(timeout_seconds=timeout_seconds)
        effective_validator = (
            validate_url
            if validate_url is not None or not owns_client
            else _validate_public_url_syntax
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
                        validate_url=effective_validator,
                    )
                    break
                except CaptureError as exc:
                    failures.append(f"{redact_url_secrets(url)}: {exc}")
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
        source_url=redact_url_secrets(source_url) if source_url else None,
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
    http = client or _public_http_client(timeout_seconds=timeout_seconds)
    effective_validator = (
        validate_url if validate_url is not None or not owns_client else _validate_public_url_syntax
    )
    hostname = (urlsplit(candidate.normalized_url).hostname or "").lower()
    use_bluesky_api = _host_matches(hostname, "bsky.app") and (
        candidate.provider == "bluesky-public-api"
    )
    capture_method = "bluesky-public-api" if use_bluesky_api else "public-http"
    try:
        if any(
            hostname == blocked or hostname.endswith(f".{blocked}")
            for blocked in _AUTOMATED_CAPTURE_DISABLED_HOSTS
        ):
            return _record_capture_disabled(candidate, destination)
        if hostname.endswith(("x.com", "twitter.com")):
            return _capture_x_oembed(
                candidate,
                destination,
                client=http,
                validate_url=effective_validator,
            )
        if use_bluesky_api:
            return _capture_bluesky_api(
                candidate,
                destination,
                client=http,
                validate_url=effective_validator,
            )
        return _capture_public_html(
            candidate,
            destination,
            client=http,
            validate_url=effective_validator,
        )
    except CaptureError as exc:
        metadata = {
            "status": "unavailable",
            "method": capture_method,
            "page_url": candidate.normalized_url,
            "post_id": candidate.post_id,
            "post_identity_verified": False,
            "captured_at": _now_iso(),
            "error": str(exc),
        }
        artifact = _write_json_artifact(destination / "capture_status.json", metadata)
        return PostCapture(
            status="unavailable",
            method=capture_method,
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
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
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


def _read_identity_body(
    response: httpx.Response,
    *,
    max_bytes: int,
    total_timeout_seconds: float = 30.0,
) -> bytes:
    """Read an undecoded response body without ever crossing the byte limit."""

    content_encoding = response.headers.get("content-encoding", "").strip().casefold()
    if content_encoding not in {"", "identity"}:
        raise CaptureError(f"Encoded response bodies are not allowed: {content_encoding}")
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
    # Mock/custom transports may hand HTTPX an already-buffered response. The
    # production transport never takes this branch; still enforce the limit
    # before copying the injected bytes.
    if response.is_stream_consumed:
        buffered = response.content
        if len(buffered) > max_bytes:
            raise CaptureError(f"Response exceeds {max_bytes} bytes")
        return buffered
    deadline = time.monotonic() + max(total_timeout_seconds, 0.001)
    output = bytearray()
    # ``chunk_size=None`` is intentional: a fixed HTTPX chunk size can buffer
    # an endless stream of smaller transport chunks without yielding here,
    # which would bypass the cumulative deadline.
    for chunk in response.iter_raw(chunk_size=None):
        if time.monotonic() > deadline:
            raise CaptureError("Response exceeded the total read deadline")
        if len(chunk) > max_bytes - len(output):
            raise CaptureError(f"Response exceeds {max_bytes} bytes")
        output.extend(chunk)
    return bytes(output)


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
        try:
            validator(current)
        except CaptureError:
            raise
        except (OSError, UnicodeError, ValueError) as exc:
            raise CaptureError("Evidence URL validation failed") from exc
        try:
            with client.stream(
                "GET",
                current,
                follow_redirects=False,
                headers={"Accept-Encoding": "identity"},
            ) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location:
                        raise CaptureError("Redirect response had no Location header")
                    current = _safe_urljoin(current, location)
                    continue
                response.raise_for_status()
                media_type = response.headers.get("content-type", "application/octet-stream")
                media_type = media_type.split(";", 1)[0].strip().lower()
                if not any(media_type.startswith(prefix) for prefix in accepted_media_prefixes):
                    raise CaptureError(f"Unexpected content type: {media_type}")
                return (
                    _read_identity_body(
                        response,
                        max_bytes=max_bytes,
                        total_timeout_seconds=_client_read_timeout(client),
                    ),
                    media_type,
                    current,
                )
        except CaptureError:
            raise
        except (httpx.HTTPError, httpx.InvalidURL, UnicodeError, ValueError) as exc:
            raise CaptureError(f"HTTP fetch failed ({type(exc).__name__})") from exc
    raise CaptureError(f"Too many redirects (>{max_redirects})")


def validate_public_url(url: str) -> None:
    """Reject local/private network targets before evidence downloads."""
    hostname, port = _parse_https_target(url)
    try:
        addresses = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except (OSError, OverflowError, UnicodeError, ValueError) as exc:
        raise CaptureError(f"Could not resolve host {hostname!r}") from exc
    if not addresses:
        raise CaptureError(f"Could not resolve host {hostname!r}")
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address[4][0])
        except (IndexError, TypeError, ValueError) as exc:
            raise CaptureError("Resolver returned an invalid destination") from exc
        if not _is_allowed_public_ip(ip):
            raise CaptureError(f"Non-public target address is not allowed: {ip}")


def _parse_https_target(url: str) -> tuple[str, int]:
    if not isinstance(url, str) or not url or len(url) > 8192:
        raise CaptureError("Evidence URL is empty or too long")
    if any(ord(character) <= 0x20 or ord(character) == 0x7F for character in url):
        raise CaptureError("Evidence URL contains control characters")
    try:
        parts = urlsplit(url)
        hostname = parts.hostname
        port = parts.port
        username = parts.username
        password = parts.password
    except (UnicodeError, ValueError) as exc:
        raise CaptureError("Evidence URL is malformed") from exc
    if parts.scheme.casefold() != "https" or not hostname:
        raise CaptureError("Only HTTPS URLs are allowed for remote evidence")
    if username or password:
        raise CaptureError("URLs containing credentials are not allowed")
    if port == 0:
        raise CaptureError("Evidence URL has an invalid port")
    try:
        ascii_hostname = hostname.rstrip(".").encode("idna").decode("ascii")
    except (UnicodeError, ValueError) as exc:
        raise CaptureError("Evidence URL has an invalid hostname") from exc
    if not ascii_hostname or len(ascii_hostname) > 253:
        raise CaptureError("Evidence URL has an invalid hostname")
    return ascii_hostname, port or 443


def _validate_public_url_syntax(url: str) -> None:
    """Validate syntax/literal IPs; the pinned transport validates DNS and the peer."""

    hostname, _ = _parse_https_target(url)
    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        return
    if not _is_allowed_public_ip(literal):
        raise CaptureError(f"Non-public target address is not allowed: {literal}")


def _safe_urljoin(base: str, reference: str) -> str:
    try:
        resolved = urljoin(base, reference)
    except (UnicodeError, ValueError) as exc:
        raise CaptureError("Remote response contained a malformed URL") from exc
    if not isinstance(resolved, str):
        raise CaptureError("Remote response contained a malformed URL")
    return resolved


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
    try:
        validator(endpoint)
    except CaptureError:
        raise
    except (OSError, OverflowError, UnicodeError, ValueError) as exc:
        raise CaptureError("X oEmbed endpoint validation failed") from exc
    params = {
        "url": candidate.normalized_url,
        "omit_script": "true",
        "dnt": "true",
    }
    try:
        _, body, raw_response, response_headers = _request_json_object(
            client,
            endpoint,
            params=params,
            operation="X oEmbed capture",
            max_bytes=1024 * 1024,
        )
    except _JSONTransportFailure as exc:
        raise CaptureError(str(exc)) from exc

    if body is None or raw_response is None:
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
    if not post_id or not any(
        _same_social_post(candidate.normalized_url, url) for url in identity_urls
    ):
        raise CaptureError("X oEmbed response did not identify the requested post")

    captured_media: list[CapturedFile] = []
    media_artifacts: list[CapturedFile] = []
    media_relationship: str | None = None
    thumbnail_url = body.get("thumbnail_url")
    if isinstance(thumbnail_url, str) and thumbnail_url:
        resolved_thumbnail_url = _safe_urljoin(candidate.normalized_url, thumbnail_url)
        with suppress(CaptureError):
            captured = _capture_remote_image(
                resolved_thumbnail_url,
                destination / "post_preview",
                client=client,
                validate_url=validate_url,
            )
            captured_media.append(captured)
            media_relationship = _post_media_relationship(
                candidate.normalized_url, captured.source_url or resolved_thumbnail_url
            )
            if media_relationship is not None:
                media_artifacts.append(captured)

    metadata = {
        "status": "captured",
        "method": "x-oembed",
        "page_url": candidate.normalized_url,
        "post_id": post_id,
        "post_identity_verified": True,
        "captured_at": _now_iso(),
        "request": {
            "method": "GET",
            "endpoint": endpoint,
            "parameters": params,
        },
        "raw_http_body_sha256": hashlib.sha256(raw_response).hexdigest(),
        "response": redact_secrets(body),
        "response_headers": response_headers,
        "linked_media": [item.to_dict() for item in captured_media],
        "face_match_eligible_media": [item.to_dict() for item in media_artifacts],
        "media_relationship": media_relationship or "page-preview-only",
    }
    artifact = _write_json_artifact(destination / "post_oembed.json", metadata)
    return PostCapture(
        status="captured",
        method="x-oembed",
        artifacts=(artifact, *captured_media),
        metadata=metadata,
        media_artifacts=tuple(media_artifacts),
    )


class _JSONTransportFailure(CaptureError):
    """A transport failure before a trustworthy bounded JSON document existed."""


def _request_json_object(
    client: httpx.Client,
    endpoint: str,
    *,
    params: Any,
    operation: str,
    allow_server_error: bool = False,
    max_bytes: int = 8 * 1024 * 1024,
) -> tuple[int, dict[str, Any] | None, bytes | None, dict[str, str]]:
    try:
        with client.stream(
            "GET",
            endpoint,
            params=params,
            follow_redirects=False,
            headers={"Accept": "application/json", "Accept-Encoding": "identity"},
        ) as response:
            status_code = response.status_code
            response_headers = _safe_headers(response.headers)
            if allow_server_error and 500 <= status_code <= 599:
                try:
                    error_body = _read_identity_body(
                        response,
                        max_bytes=min(max_bytes, 64 * 1024),
                        total_timeout_seconds=_client_read_timeout(client),
                    )
                except CaptureError:
                    error_body = None
                return status_code, None, error_body or None, response_headers
            if status_code != 200:
                raise CaptureError(f"{operation} failed with HTTP {status_code}")
            response_media_type = response.headers.get("content-type", "").split(";", 1)[0]
            if response_media_type.casefold() != "application/json":
                raise CaptureError(f"{operation} returned an unexpected content type")
            raw_response = _read_identity_body(
                response,
                max_bytes=max_bytes,
                total_timeout_seconds=_client_read_timeout(client),
            )
    except CaptureError:
        raise
    except (httpx.HTTPError, httpx.InvalidURL, UnicodeError, ValueError) as exc:
        raise _JSONTransportFailure(f"{operation} transport failed ({type(exc).__name__})") from exc

    if not raw_response:
        raise CaptureError(f"{operation} returned an empty response")
    try:
        body = json.loads(
            raw_response,
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_json_constant,
            parse_float=_finite_json_float,
        )
        _validate_json_tree(body)
    except (RecursionError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise CaptureError(f"{operation} returned invalid JSON") from exc
    if not isinstance(body, dict):
        raise CaptureError(f"{operation} returned a non-object response")
    return status_code, body, raw_response, response_headers


def _capture_bluesky_api(
    candidate: SearchCandidate,
    destination: Path,
    *,
    client: httpx.Client,
    validate_url: Callable[[str], None] | None,
) -> PostCapture:
    """Hydrate an exact CID-bound Bluesky post through the public AppView API."""

    if candidate.provider != "bluesky-public-api":
        raise CaptureError("Bluesky API capture requires a Bluesky connector candidate")
    try:
        evidence_ref = BlueskyEvidenceRef.parse(candidate.provider_item_id or "")
        permalink_actor, permalink_rkey = parse_bluesky_permalink(candidate.normalized_url)
    except ValueError as exc:
        raise CaptureError(
            "Bluesky capture requires the connector's exact AT-URI and CID evidence"
        ) from exc
    if (
        permalink_actor != evidence_ref.did
        or permalink_rkey != evidence_ref.rkey
        or candidate.post_id != f"{evidence_ref.did}/{evidence_ref.rkey}"
    ):
        raise CaptureError("Bluesky permalink, post ID, and AT-URI do not agree")

    validator = validate_url or validate_public_url
    for endpoint in (BLUESKY_GET_POSTS_ENDPOINT, BLUESKY_GET_POST_THREAD_ENDPOINT):
        try:
            validator(endpoint)
        except CaptureError:
            raise
        except (OSError, OverflowError, UnicodeError, ValueError) as exc:
            raise CaptureError("Bluesky API endpoint validation failed") from exc

    primary_params = [("uris", evidence_ref.at_uri)]
    request_attempts: list[dict[str, Any]] = []
    fallback_reason: str | None = None
    try:
        status_code, body, raw_response, response_headers = _request_json_object(
            client,
            BLUESKY_GET_POSTS_ENDPOINT,
            params=primary_params,
            operation="Bluesky getPosts capture",
            allow_server_error=True,
        )
    except _JSONTransportFailure as exc:
        fallback_reason = "transport-failure"
        request_attempts.append(
            {
                "endpoint": BLUESKY_GET_POSTS_ENDPOINT,
                "outcome": "fallback-trigger",
                "failure": "transport",
                "error_type": type(exc.__cause__).__name__ if exc.__cause__ else type(exc).__name__,
            }
        )
    else:
        if 500 <= status_code <= 599:
            fallback_reason = f"http-{status_code}"
            request_attempts.append(
                {
                    "endpoint": BLUESKY_GET_POSTS_ENDPOINT,
                    "status_code": status_code,
                    "outcome": "fallback-trigger",
                    "raw_http_body_sha256": (
                        hashlib.sha256(raw_response).hexdigest() if raw_response else None
                    ),
                    "response_headers": response_headers,
                }
            )
        else:
            request_attempts.append(
                {
                    "endpoint": BLUESKY_GET_POSTS_ENDPOINT,
                    "status_code": status_code,
                    "outcome": "selected",
                    "raw_http_body_sha256": hashlib.sha256(raw_response or b"").hexdigest(),
                    "response_headers": response_headers,
                }
            )

    selected_endpoint = BLUESKY_GET_POSTS_ENDPOINT
    selected_parameters: dict[str, Any] = {"uris": [evidence_ref.at_uri]}
    if fallback_reason is not None:
        selected_endpoint = BLUESKY_GET_POST_THREAD_ENDPOINT
        selected_parameters = {
            "uri": evidence_ref.at_uri,
            "depth": 0,
            "parentHeight": 0,
        }
        status_code, body, raw_response, response_headers = _request_json_object(
            client,
            selected_endpoint,
            params=selected_parameters,
            operation="Bluesky getPostThread fallback capture",
        )
        request_attempts.append(
            {
                "endpoint": selected_endpoint,
                "status_code": status_code,
                "outcome": "selected",
                "raw_http_body_sha256": hashlib.sha256(raw_response or b"").hexdigest(),
                "response_headers": response_headers,
            }
        )

    if body is None or raw_response is None:
        raise CaptureError("Bluesky public API did not provide a verifiable response")
    if selected_endpoint == BLUESKY_GET_POSTS_ENDPOINT:
        posts = body.get("posts")
        if not isinstance(posts, list):
            raise CaptureError("Bluesky getPosts response has no posts array")
        matching_posts = [
            item
            for item in posts
            if isinstance(item, dict) and item.get("uri") == evidence_ref.at_uri
        ]
        if len(posts) != 1 or len(matching_posts) != 1:
            raise CaptureError("Bluesky getPosts did not return exactly the requested post")
        selected_post = matching_posts[0]
    else:
        thread = body.get("thread")
        if (
            not isinstance(thread, dict)
            or thread.get("$type") != "app.bsky.feed.defs#threadViewPost"
            or not isinstance(thread.get("post"), dict)
            or thread["post"].get("uri") != evidence_ref.at_uri
        ):
            raise CaptureError("Bluesky getPostThread did not return exactly the requested post")
        selected_post = thread["post"]
    try:
        images = extract_bluesky_post_images(selected_post)
    except ValueError as exc:
        raise CaptureError("Bluesky public API returned malformed post-image evidence") from exc
    matched_images = [item for item in images if item.ref == evidence_ref]
    if len(matched_images) != 1:
        raise CaptureError("Bluesky public API did not return the expected post and image CIDs")
    image = matched_images[0]
    if candidate.image_url != image.fullsize_url:
        raise CaptureError("Bluesky candidate image URL changed before capture")

    def validate_cdn_image(url: str) -> None:
        try:
            rendition, did, image_cid = parse_bluesky_cdn_url(url)
        except ValueError as exc:
            raise CaptureError("Bluesky media redirect left the allowlisted CDN") from exc
        if rendition != "feed_fullsize" or did != image.did or image_cid != evidence_ref.image_cid:
            raise CaptureError("Bluesky media URL does not carry the expected DID and image CID")
        validator(url)

    captured = _capture_remote_image(
        image.fullsize_url,
        destination / "post_media",
        client=client,
        validate_url=validate_cdn_image,
    )
    media_relationship = _post_media_relationship(
        candidate.normalized_url,
        captured.source_url or image.fullsize_url,
    )
    if media_relationship != "bluesky-feed-media":
        raise CaptureError("Bluesky media did not remain on an eligible content CDN")

    metadata = {
        "status": "captured",
        "method": "bluesky-public-api",
        "page_url": candidate.normalized_url,
        "post_id": candidate.post_id,
        "post_identity_verified": True,
        "captured_at": _now_iso(),
        "request": {
            "method": "GET",
            "endpoint": selected_endpoint,
            "parameters": selected_parameters,
            "authentication": "none",
        },
        "request_attempts": request_attempts,
        "fallback_from": (
            {"endpoint": BLUESKY_GET_POSTS_ENDPOINT, "reason": fallback_reason}
            if fallback_reason is not None
            else None
        ),
        "raw_http_body_sha256": hashlib.sha256(raw_response).hexdigest(),
        "response_headers": response_headers,
        "response": redact_secrets(body),
        "identifiers": {
            "at_uri": evidence_ref.at_uri,
            "did": evidence_ref.did,
            "rkey": evidence_ref.rkey,
            "post_cid": evidence_ref.post_cid,
            "image_cid": evidence_ref.image_cid,
        },
        "image_cid_claim_source": "AT Protocol post record",
        "downloaded_media_is_appview_rendition": True,
        "linked_media": [captured.to_dict()],
        "face_match_eligible_media": [captured.to_dict()],
        "media_relationship": media_relationship,
    }
    artifact = _write_json_artifact(destination / "post_bluesky_api.json", metadata)
    return PostCapture(
        status="captured",
        method="bluesky-public-api",
        artifacts=(artifact, captured),
        metadata=metadata,
        media_artifacts=(captured,),
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
        source_url=redact_url_secrets(final_url),
    )

    parser = _OpenGraphParser()
    decoded_html = raw.decode("utf-8", errors="replace")
    parser.feed(decoded_html)
    requested_post_id = extract_post_id(candidate.normalized_url)
    if not requested_post_id or not _same_social_post(candidate.normalized_url, final_url):
        raise CaptureError("Post capture redirected to a different or non-post URL")
    identity_url = parser.values.get("og:url")
    if not identity_url or not _same_social_post(candidate.normalized_url, identity_url):
        raise CaptureError("Captured page metadata did not identify the requested post")
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

    captured_media: list[CapturedFile] = []
    media_artifacts: list[CapturedFile] = []
    linked_media_error: str | None = None
    media_relationship: str | None = None
    media_url = parser.values.get("og:image") or parser.values.get("twitter:image")
    if media_url:
        resolved_media_url = _safe_urljoin(final_url, media_url)
        try:
            captured = _capture_remote_image(
                resolved_media_url,
                destination / "post_preview",
                client=client,
                validate_url=validate_url,
            )
            captured_media.append(captured)
            media_relationship = _post_media_relationship(
                candidate.normalized_url, captured.source_url or resolved_media_url
            )
            if media_relationship is not None:
                media_artifacts.append(captured)
        except CaptureError as exc:
            linked_media_error = str(exc)
    metadata = {
        "status": "captured",
        "method": "public-html",
        "page_url": candidate.normalized_url,
        "final_url": redact_url_secrets(final_url),
        "post_id": requested_post_id,
        "post_identity_verified": True,
        "captured_at": _now_iso(),
        "open_graph": redact_secrets(parser.values),
        "linked_media": [item.to_dict() for item in captured_media],
        "face_match_eligible_media": [item.to_dict() for item in media_artifacts],
        "media_relationship": media_relationship or "page-preview-only",
        "linked_media_error": linked_media_error,
    }
    metadata_artifact = _write_json_artifact(destination / "post_metadata.json", metadata)
    return PostCapture(
        status="captured",
        method="public-html",
        artifacts=(html_artifact, metadata_artifact, *captured_media),
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
        source_url=redact_url_secrets(final_url),
    )


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _safe_headers(headers: httpx.Headers) -> dict[str, str]:
    allowed = {"content-type", "date", "etag", "last-modified", "x-request-id"}
    return {key.lower(): value for key, value in headers.items() if key.lower() in allowed}


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in pairs:
        if key in output:
            raise ValueError(f"duplicate JSON key: {key}")
        output[key] = value
    return output


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _finite_json_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("non-finite JSON number")
    return parsed


def _validate_json_tree(value: Any, *, max_depth: int = 64, max_nodes: int = 100_000) -> None:
    stack: list[tuple[Any, int]] = [(value, 0)]
    nodes = 0
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > max_nodes:
            raise ValueError("JSON document has too many values")
        if depth > max_depth:
            raise ValueError("JSON document is nested too deeply")
        if isinstance(current, dict):
            for key, item in current.items():
                _reject_surrogates(key)
                stack.append((item, depth + 1))
        elif isinstance(current, list):
            stack.extend((item, depth + 1) for item in current)
        elif isinstance(current, str):
            _reject_surrogates(current)
        elif isinstance(current, float) and not math.isfinite(current):
            raise ValueError("JSON document contains a non-finite number")


def _reject_surrogates(value: str) -> None:
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise ValueError("JSON document contains a lone Unicode surrogate")


def _write_json_artifact(path: Path, value: dict[str, Any]) -> CapturedFile:
    try:
        raw = (
            json.dumps(
                value,
                ensure_ascii=True,
                allow_nan=False,
                indent=2,
                sort_keys=True,
            ).encode("utf-8")
            + b"\n"
        )
    except (RecursionError, UnicodeEncodeError, ValueError) as exc:
        raise CaptureError("Capture metadata could not be safely serialized") from exc
    path.write_bytes(raw)
    return CapturedFile(
        relative_path=path.name,
        sha256=hashlib.sha256(raw).hexdigest(),
        byte_size=len(raw),
        media_type="application/json",
    )
