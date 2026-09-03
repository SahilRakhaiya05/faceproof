from __future__ import annotations

import io
from pathlib import Path

import httpx
import pytest
import respx
from PIL import Image

from faceproof.search import SearchError, SerpApiLensProvider, check_serpapi_account
from faceproof.search.base import (
    SearchCandidate,
    extract_post_id,
    filter_social_candidates,
    is_social_post_url,
    is_social_url,
    normalize_page_url,
    redact_secrets,
)
from faceproof.search.serpapi import prepare_serpapi_upload


def _write_jpeg(path: Path, size: tuple[int, int] = (32, 32)) -> None:
    image = Image.new("RGB", size, color=(40, 80, 120))
    image.save(path, format="JPEG", quality=90)


def test_normalize_url_strips_fragment_and_tracking() -> None:
    normalized = normalize_page_url(
        "HTTPS://X.COM/user/status/123/?utm_source=test&lang=en#fragment"
    )
    assert normalized == "https://x.com/user/status/123?lang=en"
    assert extract_post_id(normalized) == "123"


def test_social_urls_are_normalized_to_https_and_plain_http_is_not_trusted() -> None:
    assert normalize_page_url("http://x.com/user/status/123") == ("https://x.com/user/status/123")
    assert not is_social_post_url("http://x.com/user/status/123")


def test_url_credentials_are_removed_or_redacted_before_evidence_serialization() -> None:
    normalized = normalize_page_url(
        "https://x.com/user/status/123?lang=en&access_token=top-secret&X-Amz-Signature=sig"
    )
    assert normalized == "https://x.com/user/status/123?lang=en"

    candidate = SearchCandidate(
        provider="serpapi",
        rank=1,
        page_url="https://x.com/user/status/123?access_token=top-secret",
        normalized_url=normalized,
        image_url=("https://preview.redd.it/post.png?width=640&X-Amz-Signature=top-secret"),
    )
    serialized = candidate.public_dict()
    assert "top-secret" not in str(serialized)
    assert "%5BREDACTED%5D" in str(serialized)

    embedded = redact_secrets(
        "open https://cdn.example/file?X-Amz-Credential=credential-value&size=10"
    )
    assert "credential-value" not in embedded
    assert "size=10" in embedded

    nested = redact_secrets(
        {
            "X-Amz-Credential": "credential-value",
            "session_token": "session-value",
            "safe": "kept",
        }
    )
    assert nested == {
        "X-Amz-Credential": "[REDACTED]",
        "session_token": "[REDACTED]",
        "safe": "kept",
    }


def test_social_filter_deduplicates_and_rejects_open_web() -> None:
    candidates = [
        SearchCandidate("test", 1, "https://example.com/a", "https://example.com/a"),
        SearchCandidate("test", 2, "https://x.com/a/status/1", "https://x.com/a/status/1"),
        SearchCandidate("test", 3, "https://x.com/a/status/1", "https://x.com/a/status/1"),
    ]
    filtered = filter_social_candidates(candidates)
    assert [item.rank for item in filtered] == [2]
    assert is_social_url("https://subdomain.reddit.com/comments/abc")
    assert not is_social_url("https://notreddit.com/comments/abc")
    assert is_social_post_url("https://bsky.app/profile/example.test/post/3abc")


def test_social_filter_rejects_profiles_and_homepages() -> None:
    candidates = [
        SearchCandidate("test", 1, "https://x.com/person", "https://x.com/person"),
        SearchCandidate("test", 2, "https://youtube.com/@channel", "https://youtube.com/@channel"),
        SearchCandidate(
            "test",
            3,
            "https://reddit.com/r/pics/comments/abc/title",
            "https://reddit.com/r/pics/comments/abc/title",
        ),
    ]
    assert [item.rank for item in filter_social_candidates(candidates)] == [3]


def test_serpapi_upload_and_live_lens_search(tmp_path: Path) -> None:
    image_path = tmp_path / "input.jpg"
    _write_jpeg(image_path)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/image":
            assert request.method == "POST"
            return httpx.Response(200, json={"image_id": "image-1"})
        if request.url.path == "/search.json":
            assert request.url.params["engine"] == "google_lens"
            assert request.url.params["no_cache"] == "true"
            return httpx.Response(
                200,
                json={
                    "search_metadata": {"id": "lens-1", "status": "Success"},
                    "search_parameters": {
                        "engine": "google_lens",
                        "image_id": "image-1",
                        "api_key": "secret",
                    },
                    "visual_matches": [
                        {
                            "position": 3,
                            "title": "A real post",
                            "link": "https://x.com/example/status/99",
                            "source": "X",
                            "thumbnail": "https://images.example/thumb.jpg",
                            "image": "https://images.example/full.jpg",
                            "exact_matches": True,
                        }
                    ],
                    "related_content": [
                        {"query": "  Volunteer   Example  "},
                        {"query": "volunteer example"},
                        {"query": "Demo portrait"},
                    ],
                },
            )
        raise AssertionError(f"Unexpected URL: {request.url}")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = SerpApiLensProvider("secret", client=client)
    run = provider.search(image_path)

    assert run.search_id == "lens-1"
    assert run.live is True
    assert run.candidates[0].exact_match is True
    assert run.candidates[0].post_id == "99"
    assert run.web_labels == ("Volunteer Example", "Demo portrait")
    assert "api_key" not in run.raw_response["request_parameters"]
    assert run.raw_response["search"]["search_parameters"]["api_key"] == "[REDACTED]"
    assert "raw_http_bodies" not in run.raw_response
    assert len(run.raw_response["raw_http_body_sha256"]["search"]) == 64


def test_serpapi_rejects_non_success_search_status(tmp_path: Path) -> None:
    image_path = tmp_path / "input.jpg"
    _write_jpeg(image_path)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/image":
            return httpx.Response(200, json={"image_id": "image-1"})
        return httpx.Response(
            200,
            json={
                "search_metadata": {"id": "lens-1", "status": "Error"},
                "visual_matches": [{"link": "https://x.com/example/status/99", "position": 1}],
            },
        )

    provider = SerpApiLensProvider(
        "secret", client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    try:
        provider.search(image_path)
    except SearchError as exc:
        assert "did not complete" in str(exc)
    else:
        raise AssertionError("non-success SerpApi result was accepted")


def test_serpapi_api_error_does_not_disclose_key(tmp_path: Path) -> None:
    image_path = tmp_path / "face.png"
    Image.new("RGB", (50, 50), "white").save(image_path)
    secret = "serpapi-secret-value"

    with respx.mock(assert_all_called=True) as router:
        router.post("https://serpapi.com/image").mock(
            return_value=httpx.Response(200, json={"error": f"invalid api key {secret}"})
        )
        with SerpApiLensProvider(secret) as provider, pytest.raises(SearchError) as exc_info:
            provider.search(image_path)

    assert secret not in str(exc_info.value)
    assert "[REDACTED]" in str(exc_info.value)


def test_serpapi_preparation_compresses_unsupported_large_input(tmp_path: Path) -> None:
    image_path = tmp_path / "input.bmp"
    image = Image.new("RGB", (800, 800), color=(100, 120, 140))
    image.save(image_path, format="BMP")

    data, name, media_type = prepare_serpapi_upload(image_path)

    assert len(data) < 490_000
    assert name.endswith(".jpg")
    assert media_type == "image/jpeg"
    with Image.open(io.BytesIO(data)) as reopened:
        assert reopened.format == "JPEG"


def test_serpapi_account_check_returns_only_safe_quota_data() -> None:
    secret = "serpapi-secret-value"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["api_key"] == secret
        return httpx.Response(
            200,
            json={
                "account_id": "private-account-id",
                "api_key": secret,
                "account_email": "private@example.test",
                "account_status": "Active",
                "plan_name": "Free",
                "searches_per_month": 250,
                "total_searches_left": 249,
                "this_month_usage": 1,
                "account_rate_limit_per_hour": 50,
            },
        )

    account = check_serpapi_account(
        secret,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert account.ready
    assert account.plan_name == "Free"
    assert account.searches_left == 249
    assert secret not in repr(account)
    assert "private@example.test" not in repr(account)


def test_serpapi_account_check_redacts_errors() -> None:
    secret = "serpapi-secret-value"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": f"invalid key {secret}"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(SearchError) as exc_info:
        check_serpapi_account(secret, client=client)

    assert secret not in str(exc_info.value)
    assert "[REDACTED]" in str(exc_info.value)
