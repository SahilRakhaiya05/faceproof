from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path

import httpx
import pytest
from PIL import Image

from faceproof.capture import capture_public_post
from faceproof.search import (
    BlueskyAuthorFeedProvider,
    BlueskyEvidenceRef,
    SearchCandidate,
    SearchError,
    normalize_bluesky_actor,
    parse_bluesky_cdn_url,
)

DID = "did:plc:abcdefghijklmnopqrstuvwx"
OTHER_DID = "did:plc:zyxwvutsrqponmlkjihgfedc"
HANDLE = "consented.example"
RKEY = "3lqexamplepost"
POST_CID = "bafyreiaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
IMAGE_CID = "bafkreibbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
SECOND_IMAGE_CID = "bafkreiccccccccccccccccccccccccccccccccccccccccccccccccccc"


def _cdn_url(
    cid: str,
    *,
    rendition: str = "feed_fullsize",
    did: str = DID,
    format_suffix: str = "",
) -> str:
    return f"https://cdn.bsky.app/img/{rendition}/plain/{did}/{cid}{format_suffix}"


def _post(
    *,
    did: str = DID,
    handle: str = HANDLE,
    rkey: str = RKEY,
    post_cid: str = POST_CID,
    image_cids: tuple[str, ...] = (IMAGE_CID,),
    record_with_media: bool = False,
    gallery: bool = False,
) -> dict[str, object]:
    record_images = [
        {
            "alt": f"portrait {index}",
            "image": {
                "$type": "blob",
                "ref": {"$link": image_cid},
                "mimeType": "image/jpeg",
                "size": 1234,
            },
        }
        for index, image_cid in enumerate(image_cids, start=1)
    ]
    view_images = [
        {
            "alt": f"portrait {index}",
            "fullsize": _cdn_url(image_cid, did=did),
            "thumb": _cdn_url(image_cid, rendition="feed_thumbnail", did=did),
        }
        for index, image_cid in enumerate(image_cids, start=1)
    ]
    record_embed: dict[str, object] = {
        "$type": "app.bsky.embed.images",
        "images": record_images,
    }
    view_embed: dict[str, object] = {
        "$type": "app.bsky.embed.images#view",
        "images": view_images,
    }
    if gallery:
        record_embed = {
            "$type": "app.bsky.embed.gallery",
            "items": [
                {
                    **item,
                    "$type": "app.bsky.embed.gallery#image",
                    "aspectRatio": {"width": 1200, "height": 900},
                }
                for item in record_images
            ],
        }
        view_embed = {
            "$type": "app.bsky.embed.gallery#view",
            "items": [
                {
                    **{key: value for key, value in item.items() if key != "thumb"},
                    "$type": "app.bsky.embed.gallery#viewImage",
                    "thumbnail": item["thumb"],
                    "aspectRatio": {"width": 1200, "height": 900},
                }
                for item in view_images
            ],
        }
    if record_with_media:
        record_embed = {
            "$type": "app.bsky.embed.recordWithMedia",
            "record": {"record": {"uri": "at://example/quoted/1"}},
            "media": record_embed,
        }
        view_embed = {
            "$type": "app.bsky.embed.recordWithMedia#view",
            "record": {"record": {"uri": "at://example/quoted/1"}},
            "media": view_embed,
        }
    return {
        "uri": f"at://{did}/app.bsky.feed.post/{rkey}",
        "cid": post_cid,
        "author": {"did": did, "handle": handle, "displayName": "Consented volunteer"},
        "record": {
            "$type": "app.bsky.feed.post",
            "text": "A runtime-discovered consented portrait",
            "createdAt": "2026-08-01T12:00:00.000Z",
            "embed": record_embed,
        },
        "embed": view_embed,
        "indexedAt": "2026-08-01T12:00:01.000Z",
    }


def _candidate(image_cid: str = IMAGE_CID) -> SearchCandidate:
    reference = BlueskyEvidenceRef(
        at_uri=f"at://{DID}/app.bsky.feed.post/{RKEY}",
        post_cid=POST_CID,
        image_cid=image_cid,
    )
    return SearchCandidate(
        provider="bluesky-public-api",
        rank=1,
        page_url=f"https://bsky.app/profile/{DID}/post/{RKEY}",
        normalized_url=f"https://bsky.app/profile/{DID}/post/{RKEY}",
        image_url=_cdn_url(image_cid),
        thumbnail_url=_cdn_url(image_cid, rendition="feed_thumbnail"),
        provider_item_id=reference.serialize(),
        post_id=f"{DID}/{RKEY}",
        result_type="consented_account_media",
    )


def _write_input(path: Path) -> None:
    Image.new("RGB", (24, 24), (20, 40, 80)).save(path, "JPEG")


def _image_bytes(tmp_path: Path) -> bytes:
    path = tmp_path / "download.jpg"
    Image.new("RGB", (16, 16), (60, 100, 140)).save(path, "JPEG")
    return path.read_bytes()


@pytest.mark.parametrize(
    "actor",
    [
        "not-a-handle",
        "bad_handle.example",
        "-bad.example",
        "person.example-",
        "did:plc:has spaces",
        "https://bsky.app/profile/person.example",
    ],
)
def test_bluesky_actor_validation_rejects_ambiguous_input(actor: str) -> None:
    with pytest.raises(ValueError):
        normalize_bluesky_actor(actor)


def test_bluesky_actor_normalizes_runtime_handle() -> None:
    assert normalize_bluesky_actor("  Consented.Example  ") == HANDLE
    assert normalize_bluesky_actor(DID) == DID


def test_bluesky_provider_requires_explicit_consent() -> None:
    with pytest.raises(ValueError, match="Explicit consent"):
        BlueskyAuthorFeedProvider(HANDLE)


def test_bluesky_cdn_parser_binds_rendition_did_and_cid() -> None:
    assert parse_bluesky_cdn_url(_cdn_url(IMAGE_CID)) == (
        "feed_fullsize",
        DID,
        IMAGE_CID,
    )
    assert parse_bluesky_cdn_url(_cdn_url(IMAGE_CID, format_suffix="@jpeg")) == (
        "feed_fullsize",
        DID,
        IMAGE_CID,
    )
    with pytest.raises(ValueError):
        parse_bluesky_cdn_url(f"https://evil.example/plain/{DID}/{IMAGE_CID}@jpeg")
    with pytest.raises(ValueError):
        parse_bluesky_cdn_url(_cdn_url(IMAGE_CID) + "?token=secret")


def test_author_feed_is_no_auth_paginated_and_cid_bound(tmp_path: Path) -> None:
    input_path = tmp_path / "portrait.jpg"
    _write_input(input_path)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.host == "public.api.bsky.app"
        assert request.url.path.endswith("/app.bsky.feed.getAuthorFeed")
        assert request.url.params["actor"] == HANDLE
        assert request.url.params["filter"] == "posts_with_media"
        assert request.url.params["includePins"] == "false"
        assert "authorization" not in request.headers
        assert "cookie" not in request.headers
        if "cursor" not in request.url.params:
            return httpx.Response(
                200,
                json={
                    "feed": [
                        {"post": _post(image_cids=(IMAGE_CID, SECOND_IMAGE_CID))},
                        {
                            "reason": {"$type": "app.bsky.feed.defs#reasonRepost"},
                            "post": _post(did=OTHER_DID, handle="other.example"),
                        },
                    ],
                    "cursor": "next-page",
                },
            )
        assert request.url.params["cursor"] == "next-page"
        return httpx.Response(
            200,
            json={
                "feed": [
                    {"post": _post(image_cids=(IMAGE_CID, SECOND_IMAGE_CID))},
                    {
                        "post": _post(
                            rkey="3lqsecondpost",
                            post_cid="bafyreiddddddddddddddddddddddddddddddddddddddddddddddddddd",
                            image_cids=(
                                "bafkreieeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
                            ),
                            record_with_media=True,
                        )
                    },
                ]
            },
        )

    provider = BlueskyAuthorFeedProvider(
        HANDLE,
        page_size=25,
        max_pages=3,
        consent_confirmed=True,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    run = provider.search(input_path)

    assert len(requests) == 2
    assert len(run.candidates) == 3
    assert run.search_types == ("posts_with_media",)
    assert len(run.search_ids) == 2
    assert run.provider_mode == "public-no-auth-consented-account"
    assert run.raw_response["authentication"] == "none"
    assert run.raw_response["input_image"]["transmitted_to_bluesky"] is False
    assert (
        run.raw_response["input_image"]["sha256"]
        == hashlib.sha256(input_path.read_bytes()).hexdigest()
    )
    first = run.candidates[0]
    assert first.page_url == f"https://bsky.app/profile/{DID}/post/{RKEY}"
    assert first.image_url == _cdn_url(IMAGE_CID)
    assert BlueskyEvidenceRef.parse(first.provider_item_id or "").image_cid == IMAGE_CID
    assert run.raw_response["pages"][0]["raw_http_body_sha256"]
    assert len(run.raw_response["pages"][0]["request_facts_sha256"]) == 64


def test_author_feed_accepts_current_five_image_gallery_view(tmp_path: Path) -> None:
    input_path = tmp_path / "portrait.jpg"
    _write_input(input_path)
    gallery_cids = (
        IMAGE_CID,
        SECOND_IMAGE_CID,
        "bafkreiddddddddddddddddddddddddddddddddddddddddddddddddddd",
        "bafkreieeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
        "bafkreifffffffffffffffffffffffffffffffffffffffffffffffffff",
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"feed": [{"post": _post(image_cids=gallery_cids, gallery=True)}]},
        )

    provider = BlueskyAuthorFeedProvider(
        HANDLE,
        consent_confirmed=True,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    run = provider.search(input_path)

    assert len(run.candidates) == 5
    observed_cids = [
        BlueskyEvidenceRef.parse(item.provider_item_id or "").image_cid for item in run.candidates
    ]
    assert observed_cids == list(gallery_cids)
    assert all(
        parse_bluesky_cdn_url(item.image_url or "")[2] in gallery_cids for item in run.candidates
    )


def test_author_feed_never_truncates_gallery_at_hard_candidate_cap(tmp_path: Path) -> None:
    input_path = tmp_path / "portrait.jpg"
    _write_input(input_path)
    alphabet = "abcdefghijklmnopqrstuvwxyz234567"

    def cid(index: int, marker: str) -> str:
        suffix = alphabet[index // len(alphabet)] + alphabet[index % len(alphabet)]
        return "b" + marker * 50 + suffix

    feed = [
        {
            "post": _post(
                rkey=f"single-{index}",
                post_cid=cid(index, "a"),
                image_cids=(cid(index, "c"),),
            )
        }
        for index in range(99)
    ]
    gallery_cids = tuple(cid(index, "e") for index in range(5))
    feed.append(
        {
            "post": _post(
                rkey="gallery-at-cap",
                post_cid=cid(100, "f"),
                image_cids=gallery_cids,
                gallery=True,
            )
        }
    )

    provider = BlueskyAuthorFeedProvider(
        HANDLE,
        max_pages=1,
        max_candidates=100,
        consent_confirmed=True,
        client=httpx.Client(
            transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={"feed": feed}))
        ),
    )
    run = provider.search(input_path)

    assert len(run.candidates) == 99
    assert run.raw_response["posts_skipped_candidate_limit"] == 1
    assert not set(gallery_cids) & {
        BlueskyEvidenceRef.parse(item.provider_item_id or "").image_cid for item in run.candidates
    }


def test_author_feed_requires_existing_local_input_without_uploading_it(tmp_path: Path) -> None:
    called = False

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={"feed": []})

    provider = BlueskyAuthorFeedProvider(
        HANDLE,
        consent_confirmed=True,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(SearchError, match="does not exist"):
        provider.search(tmp_path / "missing.jpg")
    assert called is False


def test_author_feed_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    input_path = tmp_path / "portrait.jpg"
    _write_input(input_path)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b'{"feed":[],"feed":[]}',
            headers={"content-type": "application/json"},
        )

    provider = BlueskyAuthorFeedProvider(
        HANDLE,
        consent_confirmed=True,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(SearchError, match="invalid JSON"):
        provider.search(input_path)


def test_author_feed_rejects_compressed_json_without_decoding(tmp_path: Path) -> None:
    input_path = tmp_path / "portrait.jpg"
    _write_input(input_path)
    compressed = gzip.compress(b'{"feed":[]}' * 100_000)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["accept-encoding"] == "identity"
        return httpx.Response(
            200,
            stream=httpx.ByteStream(compressed),
            headers={
                "content-type": "application/json",
                "content-encoding": "gzip",
            },
        )

    provider = BlueskyAuthorFeedProvider(
        HANDLE,
        consent_confirmed=True,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(SearchError, match="encoded response body"):
        provider.search(input_path)


def test_author_feed_rejects_repeated_cursor(tmp_path: Path) -> None:
    input_path = tmp_path / "portrait.jpg"
    _write_input(input_path)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"feed": [], "cursor": "same"})

    provider = BlueskyAuthorFeedProvider(
        HANDLE,
        max_pages=3,
        consent_confirmed=True,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(SearchError, match="pagination cursor"):
        provider.search(input_path)


def test_author_feed_rejects_inconsistent_did_for_consented_handle(tmp_path: Path) -> None:
    input_path = tmp_path / "portrait.jpg"
    _write_input(input_path)
    call_count = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(200, json={"feed": [{"post": _post()}], "cursor": "next"})
        return httpx.Response(
            200,
            json={"feed": [{"post": _post(did=OTHER_DID, handle=HANDLE)}]},
        )

    provider = BlueskyAuthorFeedProvider(
        HANDLE,
        max_pages=2,
        consent_confirmed=True,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(SearchError, match="inconsistent DIDs"):
        provider.search(input_path)


def test_bluesky_capture_rehydrates_exact_post_and_downloads_eligible_media(
    tmp_path: Path,
) -> None:
    media = _image_bytes(tmp_path)
    api_raw: bytes | None = None
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal api_raw
        requests.append(request)
        assert "authorization" not in request.headers
        assert "cookie" not in request.headers
        if request.url.host == "public.api.bsky.app":
            assert request.url.path.endswith("/app.bsky.feed.getPosts")
            assert request.url.params.get_list("uris") == [f"at://{DID}/app.bsky.feed.post/{RKEY}"]
            api_raw = json.dumps({"posts": [_post()]}, separators=(",", ":")).encode()
            return httpx.Response(
                200,
                content=api_raw,
                headers={"content-type": "application/json", "x-request-id": "request-1"},
            )
        assert request.url.host == "cdn.bsky.app"
        assert request.url.path.endswith(f"/{IMAGE_CID}")
        return httpx.Response(200, content=media, headers={"content-type": "image/jpeg"})

    capture = capture_public_post(
        _candidate(),
        tmp_path / "capture",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        validate_url=lambda _url: None,
    )

    assert len(requests) == 2
    assert capture.status == "captured"
    assert capture.method == "bluesky-public-api"
    assert capture.metadata["post_identity_verified"] is True
    assert capture.metadata["media_relationship"] == "bluesky-feed-media"
    assert capture.metadata["identifiers"] == {
        "at_uri": f"at://{DID}/app.bsky.feed.post/{RKEY}",
        "did": DID,
        "rkey": RKEY,
        "post_cid": POST_CID,
        "image_cid": IMAGE_CID,
    }
    assert api_raw is not None
    assert capture.metadata["raw_http_body_sha256"] == hashlib.sha256(api_raw).hexdigest()
    assert len(capture.media_artifacts) == 1
    saved = tmp_path / "capture" / capture.media_artifacts[0].relative_path
    assert saved.read_bytes() == media


def test_bluesky_capture_falls_back_to_exact_post_thread_on_getposts_502(
    tmp_path: Path,
) -> None:
    media = _image_bytes(tmp_path)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/app.bsky.feed.getPosts"):
            return httpx.Response(
                502,
                content=b"upstream unavailable",
                headers={"content-type": "text/plain"},
            )
        if request.url.path.endswith("/app.bsky.feed.getPostThread"):
            assert request.url.params["uri"] == f"at://{DID}/app.bsky.feed.post/{RKEY}"
            assert request.url.params["depth"] == "0"
            assert request.url.params["parentHeight"] == "0"
            return httpx.Response(
                200,
                json={
                    "thread": {
                        "$type": "app.bsky.feed.defs#threadViewPost",
                        "post": _post(),
                    }
                },
            )
        assert request.url.host == "cdn.bsky.app"
        return httpx.Response(200, content=media, headers={"content-type": "image/jpeg"})

    capture = capture_public_post(
        _candidate(),
        tmp_path / "capture",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        validate_url=lambda _url: None,
    )

    assert [request.url.path.rsplit("/", 1)[-1] for request in requests[:2]] == [
        "app.bsky.feed.getPosts",
        "app.bsky.feed.getPostThread",
    ]
    assert capture.status == "captured"
    assert capture.metadata["post_identity_verified"] is True
    assert capture.metadata["request"]["endpoint"].endswith("getPostThread")
    assert capture.metadata["fallback_from"]["reason"] == "http-502"
    assert capture.metadata["request_attempts"][0]["status_code"] == 502
    assert capture.metadata["request_attempts"][1]["outcome"] == "selected"


@pytest.mark.parametrize(
    ("candidate", "post", "message"),
    [
        (
            _candidate(),
            _post(post_cid="bafyreizzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz"),
            "expected post and image CIDs",
        ),
        (
            _candidate(SECOND_IMAGE_CID),
            _post(),
            "expected post and image CIDs",
        ),
    ],
)
def test_bluesky_capture_rejects_cid_mismatch_without_downloading(
    tmp_path: Path,
    candidate: SearchCandidate,
    post: dict[str, object],
    message: str,
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.url.host == "public.api.bsky.app"
        return httpx.Response(200, json={"posts": [post]})

    capture = capture_public_post(
        candidate,
        tmp_path / "capture",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        validate_url=lambda _url: None,
    )

    assert calls == 1
    assert capture.status == "unavailable"
    assert capture.method == "bluesky-public-api"
    assert message in capture.metadata["error"]
    assert capture.metadata["post_identity_verified"] is False


@pytest.mark.parametrize(
    "extra_json",
    [
        b"1e10000",
        b'"\\ud800"',
        b"[" * 70 + b"0" + b"]" * 70,
    ],
)
def test_bluesky_capture_rejects_unsafe_json_values(
    tmp_path: Path,
    extra_json: bytes,
) -> None:
    posts = json.dumps({"posts": [_post()]}, separators=(",", ":")).encode()
    payload = posts[:-1] + b',"extra":' + extra_json + b"}"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "public.api.bsky.app"
        return httpx.Response(
            200,
            stream=httpx.ByteStream(payload),
            headers={"content-type": "application/json"},
        )

    capture = capture_public_post(
        _candidate(),
        tmp_path / "capture",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        validate_url=lambda _url: None,
    )

    assert capture.status == "unavailable"
    assert "invalid JSON" in capture.metadata["error"]


def test_bluesky_capture_rejects_redirect_outside_allowlisted_cdn(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "public.api.bsky.app":
            return httpx.Response(200, json={"posts": [_post()]})
        if request.url.host == "cdn.bsky.app":
            return httpx.Response(302, headers={"location": "https://evil.example/image.jpg"})
        raise AssertionError("off-CDN redirect should be rejected before a request")

    capture = capture_public_post(
        _candidate(),
        tmp_path / "capture",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        validate_url=lambda _url: None,
    )

    assert capture.status == "unavailable"
    assert "allowlisted CDN" in capture.metadata["error"]


def test_lens_bluesky_candidate_uses_strict_public_html_capture(tmp_path: Path) -> None:
    media = _image_bytes(tmp_path)
    permalink = f"https://bsky.app/profile/{DID}/post/{RKEY}"
    candidate = SearchCandidate(
        provider="serpapi",
        rank=1,
        page_url=permalink,
        normalized_url=permalink,
        post_id=f"{DID}/{RKEY}",
    )

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "bsky.app":
            html = (
                f'<meta property="og:url" content="{permalink}">'
                '<meta property="og:title" content="Consented portrait">'
                f'<meta property="og:image" content="{_cdn_url(IMAGE_CID)}">'
            )
            return httpx.Response(200, text=html, headers={"content-type": "text/html"})
        assert request.url.host == "cdn.bsky.app"
        return httpx.Response(200, content=media, headers={"content-type": "image/jpeg"})

    capture = capture_public_post(
        candidate,
        tmp_path / "capture",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        validate_url=lambda _url: None,
    )

    assert [request.url.host for request in requests] == ["bsky.app", "cdn.bsky.app"]
    assert capture.status == "captured"
    assert capture.method == "public-html"
    assert capture.metadata["post_identity_verified"] is True
    assert capture.metadata["media_relationship"] == "bluesky-feed-media"
    assert len(capture.media_artifacts) == 1
