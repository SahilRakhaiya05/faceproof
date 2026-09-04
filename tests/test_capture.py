from __future__ import annotations

import base64
import gzip
import socket
import struct
import threading
import time
from pathlib import Path
from typing import Any

import httpcore
import httpx
import pytest
from PIL import Image

from faceproof.capture import (
    CaptureError,
    _PinnedPublicHTTPTransport,
    _PinnedPublicNetworkBackend,
    capture_public_post,
    fetch_public_bytes,
    inspect_image,
    materialize_candidate_image,
    validate_public_url,
)
from faceproof.search.base import SearchCandidate


def _png_bytes(tmp_path: Path) -> bytes:
    path = tmp_path / "source.png"
    Image.new("RGB", (12, 12), color=(10, 20, 30)).save(path, "PNG")
    return path.read_bytes()


def test_materialize_embedded_thumbnail_preserves_exact_bytes(tmp_path: Path) -> None:
    raw = _png_bytes(tmp_path)
    candidate = SearchCandidate(
        provider="serpapi",
        rank=1,
        page_url="https://x.com/user/status/1",
        normalized_url="https://x.com/user/status/1",
        thumbnail_base64="data:image/png;base64," + base64.b64encode(raw).decode(),
    )

    artifact = materialize_candidate_image(candidate, tmp_path / "candidate")

    saved = tmp_path / "candidate" / artifact.relative_path
    assert artifact.media_type == "image/png"
    assert saved.read_bytes() == raw


def test_materialize_accepts_whitespace_after_data_uri_comma(tmp_path: Path) -> None:
    raw = _png_bytes(tmp_path)
    encoded = base64.b64encode(raw).decode()
    candidate = SearchCandidate(
        provider="serpapi",
        rank=1,
        page_url="https://x.com/user/status/1",
        normalized_url="https://x.com/user/status/1",
        thumbnail_base64=f"data:image/png;base64, \n{encoded}",
    )

    artifact = materialize_candidate_image(candidate, tmp_path / "candidate")
    assert (tmp_path / "candidate" / artifact.relative_path).read_bytes() == raw


def test_materialize_rejects_non_image_base64(tmp_path: Path) -> None:
    candidate = SearchCandidate(
        provider="serpapi",
        rank=1,
        page_url="https://x.com/user/status/1",
        normalized_url="https://x.com/user/status/1",
        thumbnail_base64=base64.b64encode(b"not an image").decode(),
    )

    try:
        materialize_candidate_image(candidate, tmp_path / "candidate")
    except CaptureError as exc:
        assert "valid image" in str(exc)
    else:
        raise AssertionError("invalid candidate bytes were accepted")


def test_image_decompression_bomb_is_a_candidate_rejection() -> None:
    # Minimal BMP header declaring 100,000 x 100,000 pixels. Pillow rejects it
    # before decoding; FaceProof must turn that library exception into its
    # normal per-candidate CaptureError instead of aborting the whole run.
    raw = (
        b"BM"
        + struct.pack("<IHHI", 54, 0, 0, 54)
        + struct.pack(
            "<IiiHHIIiiII",
            40,
            100_000,
            100_000,
            1,
            24,
            0,
            0,
            0,
            0,
            0,
            0,
        )
    )

    with pytest.raises(CaptureError, match="not a valid image"):
        inspect_image(raw)


def test_fetch_rejects_compressed_body_without_expanding_it() -> None:
    compressed = gzip.compress(b"x" * 200_000)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["accept-encoding"] == "identity"
        return httpx.Response(
            200,
            stream=httpx.ByteStream(compressed),
            headers={
                "content-type": "image/png",
                "content-encoding": "gzip",
                "content-length": str(len(compressed)),
            },
        )

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(CaptureError, match="Encoded response bodies"),
    ):
        fetch_public_bytes(
            "https://media.example/image.png",
            client=client,
            max_bytes=1024,
            accepted_media_prefixes=("image/",),
            validate_url=lambda _url: None,
        )


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com:99999/image.png",
        "https://[::1/image.png",
        "https://bad\ud800.example/image.png",
        "https://example.com/line\nbreak.png",
        "https://example.com/has space.png",
    ],
)
def test_malformed_evidence_urls_are_normal_capture_errors(url: str) -> None:
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: (_ for _ in ()).throw(AssertionError("request must not run"))
        )
    )
    with client, pytest.raises(CaptureError):
        fetch_public_bytes(
            url,
            client=client,
            max_bytes=1024,
            accepted_media_prefixes=("image/",),
        )


def test_malformed_redirect_location_is_a_normal_capture_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://[::1"})

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(CaptureError),
    ):
        fetch_public_bytes(
            "https://media.example/image.png",
            client=client,
            max_bytes=1024,
            accepted_media_prefixes=("image/",),
            validate_url=lambda _url: None,
        )


@pytest.mark.parametrize("resolved_ip", ["224.0.0.251", "ff02::1"])
def test_public_url_validator_rejects_multicast_resolution(
    monkeypatch: pytest.MonkeyPatch,
    resolved_ip: str,
) -> None:
    family = socket.AF_INET6 if ":" in resolved_ip else socket.AF_INET
    monkeypatch.setattr(
        "faceproof.capture.socket.getaddrinfo",
        lambda *_args, **_kwargs: [
            (family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (resolved_ip, 443))
        ],
    )

    with pytest.raises(CaptureError, match="Non-public target"):
        validate_public_url("https://multicast.example/image.png")


def test_x_capture_requires_oembed_identity(tmp_path: Path) -> None:
    candidate = SearchCandidate(
        "serpapi",
        1,
        "https://x.com/person/status/123",
        "https://x.com/person/status/123",
        post_id="123",
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        assert _request.url.host == "publish.x.com"
        return httpx.Response(
            200,
            json={"html": '<a href="https://x.com/other/status/9123">unrelated</a>'},
        )

    capture = capture_public_post(
        candidate,
        tmp_path / "candidate",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        validate_url=lambda _url: None,
    )
    assert capture.status == "unavailable"
    assert capture.metadata["post_identity_verified"] is False


def test_x_capture_rejects_same_numeric_id_from_another_platform(tmp_path: Path) -> None:
    candidate = SearchCandidate(
        "serpapi",
        1,
        "https://x.com/person/status/123",
        "https://x.com/person/status/123",
        post_id="123",
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"html": '<a href="https://www.tiktok.com/@person/video/123">post</a>'},
        )

    capture = capture_public_post(
        candidate,
        tmp_path / "candidate",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        validate_url=lambda _url: None,
    )

    assert capture.status == "unavailable"
    assert capture.metadata["post_identity_verified"] is False


def test_x_capture_uses_current_oembed_endpoint_and_confirms_post(tmp_path: Path) -> None:
    candidate = SearchCandidate(
        "serpapi",
        1,
        "https://x.com/person/status/123",
        "https://x.com/person/status/123",
        post_id="123",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "publish.x.com"
        return httpx.Response(
            200,
            json={
                "html": '<blockquote><a href="https://x.com/person/status/123">post</a></blockquote>'
            },
        )

    capture = capture_public_post(
        candidate,
        tmp_path / "candidate",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        validate_url=lambda _url: None,
    )
    assert capture.status == "captured"
    assert capture.metadata["post_identity_verified"] is True


def test_public_html_capture_downloads_linked_post_media(tmp_path: Path) -> None:
    image = _png_bytes(tmp_path)
    candidate = SearchCandidate(
        "serpapi",
        1,
        "https://www.reddit.com/r/pics/comments/abc/title",
        "https://www.reddit.com/r/pics/comments/abc/title",
        post_id="abc",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "www.reddit.com":
            html = (
                '<meta property="og:url" content="https://www.reddit.com/r/pics/comments/abc/title">'
                '<meta property="og:title" content="A post">'
                '<meta property="og:image" content="https://preview.redd.it/post.png?'
                'width=640&amp;X-Amz-Signature=top-secret">'
            )
            return httpx.Response(200, text=html, headers={"content-type": "text/html"})
        return httpx.Response(200, content=image, headers={"content-type": "image/png"})

    capture = capture_public_post(
        candidate,
        tmp_path / "candidate",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        validate_url=lambda _url: None,
    )
    assert capture.status == "captured"
    assert capture.metadata["post_identity_verified"] is True
    assert len(capture.media_artifacts) == 1
    assert capture.metadata["media_relationship"] == "reddit-content-cdn"
    assert "top-secret" not in str(capture.artifacts)
    assert "%5BREDACTED%5D" in str(capture.artifacts)


def test_public_html_generic_preview_is_not_face_match_eligible(tmp_path: Path) -> None:
    image = _png_bytes(tmp_path)
    candidate = SearchCandidate(
        "serpapi",
        1,
        "https://www.reddit.com/r/pics/comments/abc/title",
        "https://www.reddit.com/r/pics/comments/abc/title",
        post_id="abc",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "www.reddit.com":
            html = (
                '<meta property="og:url" content="https://www.reddit.com/r/pics/comments/abc/title">'
                '<meta property="og:title" content="A post">'
                '<meta property="og:image" content="https://avatars.example/person.png">'
            )
            return httpx.Response(200, text=html, headers={"content-type": "text/html"})
        return httpx.Response(200, content=image, headers={"content-type": "image/png"})

    capture = capture_public_post(
        candidate,
        tmp_path / "candidate",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        validate_url=lambda _url: None,
    )

    assert capture.status == "captured"
    assert capture.metadata["post_identity_verified"] is True
    assert capture.metadata["media_relationship"] == "page-preview-only"
    assert len(capture.artifacts) == 3
    assert capture.media_artifacts == ()


def test_public_html_content_cdn_redirect_loses_media_eligibility(tmp_path: Path) -> None:
    image = _png_bytes(tmp_path)
    candidate = SearchCandidate(
        "serpapi",
        1,
        "https://www.reddit.com/r/pics/comments/abc/title",
        "https://www.reddit.com/r/pics/comments/abc/title",
        post_id="abc",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "www.reddit.com":
            html = (
                '<meta property="og:url" content="https://www.reddit.com/r/pics/comments/abc/title">'
                '<meta property="og:title" content="A post">'
                '<meta property="og:image" content="https://preview.redd.it/post.png">'
            )
            return httpx.Response(200, text=html, headers={"content-type": "text/html"})
        if request.url.host == "preview.redd.it":
            return httpx.Response(
                302,
                headers={"location": "https://avatars.example/person.png"},
            )
        return httpx.Response(200, content=image, headers={"content-type": "image/png"})

    capture = capture_public_post(
        candidate,
        tmp_path / "candidate",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        validate_url=lambda _url: None,
    )

    assert capture.status == "captured"
    assert capture.metadata["media_relationship"] == "page-preview-only"
    assert capture.media_artifacts == ()


def test_public_html_requires_same_platform_canonical_url(tmp_path: Path) -> None:
    candidate = SearchCandidate(
        "serpapi",
        1,
        "https://www.reddit.com/r/pics/comments/123/title",
        "https://www.reddit.com/r/pics/comments/123/title",
        post_id="123",
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        html = (
            '<meta property="og:url" content="https://www.tiktok.com/@person/video/123">'
            '<meta property="og:title" content="Not the requested post">'
        )
        return httpx.Response(200, text=html, headers={"content-type": "text/html"})

    capture = capture_public_post(
        candidate,
        tmp_path / "candidate",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        validate_url=lambda _url: None,
    )

    assert capture.status == "unavailable"
    assert capture.metadata["post_identity_verified"] is False


def test_public_html_rejects_https_downgrade_for_same_post(tmp_path: Path) -> None:
    candidate = SearchCandidate(
        "serpapi",
        1,
        "https://www.reddit.com/r/pics/comments/abc/title",
        "https://www.reddit.com/r/pics/comments/abc/title",
        post_id="abc",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.scheme == "https":
            return httpx.Response(
                302,
                headers={"location": "http://www.reddit.com/r/pics/comments/abc/title"},
            )
        html = (
            '<meta property="og:url" content="http://www.reddit.com/r/pics/comments/abc/title">'
            '<meta property="og:title" content="Untrusted downgrade">'
        )
        return httpx.Response(200, text=html, headers={"content-type": "text/html"})

    capture = capture_public_post(
        candidate,
        tmp_path / "candidate",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        validate_url=lambda _url: None,
    )

    assert capture.status == "unavailable"
    assert capture.metadata["post_identity_verified"] is False


def test_pinned_transport_connects_to_vetted_ip_but_preserves_host_and_sni() -> None:
    class ScriptedStream(httpcore.NetworkStream):
        def __init__(self) -> None:
            self.response = bytearray(
                b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
            )
            self.writes: list[bytes] = []
            self.server_hostname: str | None = None
            self.closed = False

        def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
            del timeout
            chunk = bytes(self.response[:max_bytes])
            del self.response[:max_bytes]
            return chunk

        def write(self, buffer: bytes, timeout: float | None = None) -> None:
            del timeout
            self.writes.append(buffer)

        def close(self) -> None:
            self.closed = True

        def start_tls(
            self,
            ssl_context: Any,
            server_hostname: str | None = None,
            timeout: float | None = None,
        ) -> httpcore.NetworkStream:
            del ssl_context, timeout
            self.server_hostname = server_hostname
            return self

        def get_extra_info(self, info: str) -> Any:
            if info == "server_addr":
                return ("8.8.8.8", 443)
            return None

    class RecordingBackend(httpcore.NetworkBackend):
        def __init__(self, stream: ScriptedStream) -> None:
            self.stream = stream
            self.hosts: list[str] = []

        def connect_tcp(
            self,
            host: str,
            port: int,
            timeout: float | None = None,
            local_address: str | None = None,
            socket_options: Any = None,
        ) -> httpcore.NetworkStream:
            del port, timeout, local_address, socket_options
            self.hosts.append(host)
            return self.stream

    stream = ScriptedStream()
    raw_backend = RecordingBackend(stream)

    def resolver(*_args: Any, **_kwargs: Any) -> list[tuple[Any, ...]]:
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("8.8.8.8", 443))]

    backend = _PinnedPublicNetworkBackend(backend=raw_backend, resolver=resolver)
    transport = _PinnedPublicHTTPTransport(network_backend=backend)

    with httpx.Client(transport=transport, trust_env=False) as client:
        response = client.get("https://media.example/post")

    assert response.status_code == 200
    assert raw_backend.hosts == ["8.8.8.8"]
    assert stream.server_hostname == "media.example"
    assert b"Host: media.example" in b"".join(stream.writes)


def test_pinned_backend_rejects_private_or_mismatched_actual_destination() -> None:
    class PeerStream(httpcore.NetworkStream):
        def __init__(self, peer: str) -> None:
            self.peer = peer
            self.closed = False

        def close(self) -> None:
            self.closed = True

        def get_extra_info(self, info: str) -> Any:
            return (self.peer, 443) if info == "server_addr" else None

    class PeerBackend(httpcore.NetworkBackend):
        def __init__(self, stream: PeerStream) -> None:
            self.stream = stream
            self.calls = 0

        def connect_tcp(self, *_args: Any, **_kwargs: Any) -> httpcore.NetworkStream:
            self.calls += 1
            return self.stream

    def private_resolution(*_args: Any, **_kwargs: Any) -> list[tuple[Any, ...]]:
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", 443))]

    never_connected = PeerBackend(PeerStream("127.0.0.1"))
    backend = _PinnedPublicNetworkBackend(
        backend=never_connected,
        resolver=private_resolution,
    )
    with pytest.raises(httpcore.ConnectError, match="Non-public target"):
        backend.connect_tcp("rebind.example", 443)
    assert never_connected.calls == 0

    def public_resolution(*_args: Any, **_kwargs: Any) -> list[tuple[Any, ...]]:
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("8.8.8.8", 443))]

    rebound_stream = PeerStream("127.0.0.1")
    rebound_backend = PeerBackend(rebound_stream)
    backend = _PinnedPublicNetworkBackend(
        backend=rebound_backend,
        resolver=public_resolution,
    )
    with pytest.raises(httpcore.ConnectError, match="validated public destination"):
        backend.connect_tcp("rebind.example", 443)
    assert rebound_backend.calls == 1
    assert rebound_stream.closed is True


def test_pinned_backend_bounds_blocking_dns_resolution() -> None:
    release = threading.Event()

    def blocking_resolver(*_args: Any, **_kwargs: Any) -> list[tuple[Any, ...]]:
        release.wait(2)
        return [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("8.8.8.8", 443),
            )
        ]

    backend = _PinnedPublicNetworkBackend(resolver=blocking_resolver)
    started = time.monotonic()
    try:
        with pytest.raises(httpcore.ConnectTimeout, match="Resolution timed out"):
            backend.connect_tcp("slow-resolver.example", 443, timeout=0.02)
    finally:
        release.set()

    assert time.monotonic() - started < 0.5


def test_public_fetch_enforces_cumulative_deadline_on_raw_trickle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class TrickleStream(httpx.SyncByteStream):
        def __iter__(self):
            yield b"first"
            yield b"second"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            stream=TrickleStream(),
            headers={"content-type": "image/jpeg"},
        )

    clock = iter([0.0, 0.1, 0.7])
    monkeypatch.setattr("faceproof.capture.time.monotonic", lambda: next(clock))
    with (
        httpx.Client(
            transport=httpx.MockTransport(handler),
            timeout=httpx.Timeout(0.5),
        ) as client,
        pytest.raises(CaptureError, match="total read deadline"),
    ):
        fetch_public_bytes(
            "https://images.example/slow.jpg",
            client=client,
            max_bytes=100,
            accepted_media_prefixes=("image/",),
            validate_url=lambda _url: None,
        )
