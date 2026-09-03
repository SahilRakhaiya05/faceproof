from __future__ import annotations

import base64
from pathlib import Path

import httpx
from PIL import Image

from faceproof.capture import CaptureError, capture_public_post, materialize_candidate_image
from faceproof.search.base import SearchCandidate


def _png_bytes(tmp_path: Path) -> bytes:
    path = tmp_path / "source.png"
    Image.new("RGB", (12, 12), color=(10, 20, 30)).save(path, "PNG")
    return path.read_bytes()


def test_materialize_facecheck_thumbnail_preserves_exact_bytes(tmp_path: Path) -> None:
    raw = _png_bytes(tmp_path)
    candidate = SearchCandidate(
        provider="facecheck",
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
        provider="facecheck",
        rank=1,
        page_url="https://x.com/user/status/1",
        normalized_url="https://x.com/user/status/1",
        thumbnail_base64=f"data:image/png;base64, \n{encoded}",
    )

    artifact = materialize_candidate_image(candidate, tmp_path / "candidate")
    assert (tmp_path / "candidate" / artifact.relative_path).read_bytes() == raw


def test_materialize_rejects_non_image_base64(tmp_path: Path) -> None:
    candidate = SearchCandidate(
        provider="facecheck",
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
                '<meta property="og:image" content="https://images.example/post.png">'
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
