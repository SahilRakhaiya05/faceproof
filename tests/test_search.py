from __future__ import annotations

import gzip
import io
import struct
from pathlib import Path

import httpx
import pytest
import respx
from PIL import Image

from faceproof.search import SearchError, SerpApiLensProvider, check_serpapi_account
from faceproof.search.base import (
    SearchCandidate,
    extract_post_id,
    filter_profile_candidates,
    filter_social_candidates,
    is_social_post_url,
    is_social_profile_url,
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


def test_social_filter_preserves_all_cid_bound_images_but_limits_unique_posts() -> None:
    first_post = "https://bsky.app/profile/did:plc:alice/post/3first"
    second_post = "https://bsky.app/profile/did:plc:alice/post/3second"
    candidates = [
        SearchCandidate(
            "bluesky-public-api",
            1,
            first_post,
            first_post,
            provider_item_id="at://did:plc:alice/app.bsky.feed.post/3first|postcid|image-one",
        ),
        SearchCandidate(
            "bluesky-public-api",
            2,
            first_post,
            first_post,
            provider_item_id="at://did:plc:alice/app.bsky.feed.post/3first|postcid|image-two",
        ),
        SearchCandidate(
            "bluesky-public-api",
            3,
            second_post,
            second_post,
            provider_item_id="at://did:plc:alice/app.bsky.feed.post/3second|postcid|image-three",
        ),
    ]

    filtered = filter_social_candidates(candidates, limit=1)

    assert [item.rank for item in filtered] == [1, 2]
    assert len({item.normalized_url for item in filtered}) == 1


def test_social_filter_preserves_lens_media_variants_but_limits_unique_posts() -> None:
    first_post = "https://x.com/volunteer/status/1"
    second_post = "https://x.com/volunteer/status/2"
    candidates = [
        SearchCandidate(
            "serpapi",
            1,
            first_post,
            first_post,
            image_url="https://images.example/exact.jpg",
            provider_item_id="lens:exact:1",
        ),
        SearchCandidate(
            "serpapi",
            2,
            first_post,
            first_post,
            thumbnail_url="https://images.example/visual.jpg",
            provider_item_id="lens:visual:1",
        ),
        SearchCandidate(
            "serpapi",
            3,
            second_post,
            second_post,
            image_url="https://images.example/second.jpg",
            provider_item_id="lens:visual:2",
        ),
    ]

    filtered = filter_social_candidates(candidates, limit=1)

    assert [item.rank for item in filtered] == [1, 2]
    assert {item.normalized_url for item in filtered} == {first_post}


def test_social_filter_does_not_preserve_unbound_duplicate_post_results() -> None:
    post = "https://bsky.app/profile/did:plc:alice/post/3first"
    candidates = [
        SearchCandidate("serpapi", 1, post, post, provider_item_id="lens:one"),
        SearchCandidate("serpapi", 2, post, post, provider_item_id="lens:two"),
    ]

    assert [item.rank for item in filter_social_candidates(candidates)] == [1]


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


def test_short_youtube_urls_are_eligible_social_posts() -> None:
    candidate = SearchCandidate(
        "test",
        1,
        "https://youtu.be/abc123?t=4",
        "https://youtu.be/abc123?t=4",
    )

    assert is_social_url(candidate.normalized_url)
    assert is_social_post_url(candidate.normalized_url)
    assert extract_post_id(candidate.normalized_url) == "abc123"
    assert [item.post_id for item in filter_social_candidates([candidate])] == ["abc123"]


def test_linkedin_profiles_are_leads_and_never_post_candidates() -> None:
    profile = SearchCandidate(
        "test",
        1,
        "https://www.linkedin.com/in/consented-volunteer/",
        "https://www.linkedin.com/in/consented-volunteer/",
    )
    post = SearchCandidate(
        "test",
        2,
        "https://www.linkedin.com/posts/consented-volunteer_demo-activity-123",
        "https://www.linkedin.com/posts/consented-volunteer_demo-activity-123",
    )

    assert is_social_profile_url(profile.normalized_url)
    assert not is_social_profile_url(post.normalized_url)
    assert [item.rank for item in filter_profile_candidates([profile, post])] == [1]
    assert [item.rank for item in filter_social_candidates([profile, post])] == [2]
    assert filter_profile_candidates([profile], limit=0) == []


def test_platform_filter_is_a_local_eligibility_filter() -> None:
    linkedin = SearchCandidate(
        "test",
        1,
        "https://linkedin.com/posts/person_activity-123",
        "https://linkedin.com/posts/person_activity-123",
    )
    reddit = SearchCandidate(
        "test",
        2,
        "https://reddit.com/r/pics/comments/abc/title",
        "https://reddit.com/r/pics/comments/abc/title",
    )
    filtered = filter_social_candidates([linkedin, reddit], platforms=frozenset({"linkedin"}))
    assert [item.rank for item in filtered] == [1]


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
    assert len(run.candidates) == 2
    assert run.candidates[0].exact_match is True
    assert run.candidates[0].post_id == "99"
    assert run.candidates[0].image_url == "https://images.example/full.jpg"
    assert run.candidates[0].thumbnail_url is None
    assert run.candidates[1].image_url is None
    assert run.candidates[1].thumbnail_url == "https://images.example/thumb.jpg"
    assert run.web_labels == ("Volunteer Example", "Demo portrait")
    assert "api_key" not in run.raw_response["request_parameters"]
    assert run.raw_response["search"]["search_parameters"]["api_key"] == "[REDACTED]"
    assert "raw_http_bodies" not in run.raw_response
    assert len(run.raw_response["raw_http_body_sha256"]["search"]) == 64


def test_serpapi_json_transport_rejects_compressed_response_without_decoding() -> None:
    compressed = gzip.compress(b'{"account_status":"Active"}' * 10_000)

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

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(SearchError, match="encoded response body"),
    ):
        check_serpapi_account("secret", client=client)


def test_serpapi_json_transport_enforces_cumulative_deadline_on_raw_trickle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class TrickleStream(httpx.SyncByteStream):
        def __iter__(self):
            yield b'{"account_status"'
            yield b':"Active"}'

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            stream=TrickleStream(),
            headers={"content-type": "application/json"},
        )

    clock = iter([0.0, 0.1, 0.7])
    monkeypatch.setattr("faceproof.search._http.time.monotonic", lambda: next(clock))
    with (
        httpx.Client(
            transport=httpx.MockTransport(handler),
            timeout=httpx.Timeout(0.5),
        ) as client,
        pytest.raises(SearchError, match="total read deadline"),
    ):
        check_serpapi_account("secret", client=client)


@pytest.mark.parametrize(
    "payload",
    [
        b'{"value":1e10000}',
        b'{"value":"\\ud800"}',
        b'{"value":' + b"[" * 70 + b"0" + b"]" * 70 + b"}",
    ],
)
def test_serpapi_json_transport_rejects_unsafe_json_values(payload: bytes) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            stream=httpx.ByteStream(payload),
            headers={"content-type": "application/json"},
        )

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(SearchError, match="invalid JSON"),
    ):
        check_serpapi_account("secret", client=client)


def test_serpapi_upload_preparation_rejects_decompression_bomb(tmp_path: Path) -> None:
    path = tmp_path / "bomb.bmp"
    path.write_bytes(
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

    with pytest.raises(SearchError, match="Cannot prepare image"):
        prepare_serpapi_upload(path)


def test_serpapi_deep_mode_queries_exact_and_visual_then_deduplicates(tmp_path: Path) -> None:
    image_path = tmp_path / "input.jpg"
    _write_jpeg(image_path)
    requested_types: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/image":
            return httpx.Response(200, json={"image_id": "image-deep"})
        search_type = request.url.params["type"]
        requested_types.append(search_type)
        common = {
            "search_metadata": {"id": f"lens-{search_type}", "status": "Success"},
            "search_parameters": {
                "engine": "google_lens",
                "image_id": "image-deep",
                "type": search_type,
                "no_cache": "true",
            },
        }
        if search_type == "exact_matches":
            common["exact_matches"] = [
                {
                    "position": 1,
                    "link": "https://linkedin.com/in/volunteer",
                    "title": "Volunteer profile",
                    "thumbnail": "https://images.example/exact.jpg",
                }
            ]
        else:
            common["visual_matches"] = [
                {
                    "position": 1,
                    "link": "https://linkedin.com/in/volunteer",
                    "title": "Duplicate profile",
                },
                {
                    "position": 2,
                    "link": "https://x.com/volunteer/status/123",
                    "title": "Public post",
                },
            ]
        return httpx.Response(200, json=common)

    provider = SerpApiLensProvider(
        "secret",
        search_mode="deep",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    run = provider.search(image_path)

    assert requested_types == ["exact_matches", "visual_matches"]
    assert run.search_types == ("exact_matches", "visual_matches")
    assert run.search_ids == ("lens-exact_matches", "lens-visual_matches")
    assert len(run.candidates) == 2
    assert run.candidates[0].exact_match is True
    assert run.candidates[0].result_type == "exact_match"
    assert run.candidates[1].result_type == "visual_match"


def test_serpapi_deep_routes_full_and_focus_images_to_separate_lanes(
    tmp_path: Path,
) -> None:
    image_path = tmp_path / "full.jpg"
    focus_path = tmp_path / "face-crop.jpg"
    _write_jpeg(image_path, size=(96, 64))
    Image.new("RGB", (48, 48), color=(180, 40, 20)).save(
        focus_path,
        format="JPEG",
        quality=90,
    )
    upload_bodies: list[bytes] = []
    lens_requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/image":
            upload_bodies.append(request.content)
            return httpx.Response(
                200,
                json={"image_id": f"image-{len(upload_bodies)}"},
            )
        search_type = request.url.params["type"]
        image_id = request.url.params["image_id"]
        lens_requests.append((search_type, image_id))
        return httpx.Response(
            200,
            json={
                "search_metadata": {"id": f"lens-{search_type}", "status": "Success"},
                "search_parameters": {
                    "engine": "google_lens",
                    "image_id": image_id,
                    "type": search_type,
                    "no_cache": "true",
                },
            },
        )

    provider = SerpApiLensProvider(
        "secret",
        search_mode="deep",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    run = provider.search(image_path, focus_image_path=focus_path)

    assert len(upload_bodies) == 2
    assert upload_bodies[0] != upload_bodies[1]
    assert lens_requests == [
        ("exact_matches", "image-1"),
        ("visual_matches", "image-2"),
    ]
    assert run.raw_response["lane_to_input"] == {
        "exact_matches": {
            "input_role": "primary",
            "image_id": "image-1",
            "query_upload_sha256": run.raw_response["query_uploads"]["primary"]["sha256"],
        },
        "visual_matches": {
            "input_role": "focus",
            "image_id": "image-2",
            "query_upload_sha256": run.raw_response["query_uploads"]["focus"]["sha256"],
        },
    }
    primary_evidence = run.raw_response["query_uploads"]["primary"]
    focus_evidence = run.raw_response["query_uploads"]["focus"]
    assert primary_evidence["byte_size"] > 0
    assert focus_evidence["byte_size"] > 0
    assert len(primary_evidence["sha256"]) == 64
    assert len(focus_evidence["sha256"]) == 64
    assert primary_evidence["sha256"] != focus_evidence["sha256"]
    assert focus_evidence["provider_upload_reused"] is False
    assert run.raw_response["uploads"]["focus"]["reused_from"] is None
    assert len(run.raw_response["raw_http_body_sha256"]["uploads"]["focus"]) == 64
    assert len(run.raw_response["requests"]) == 2
    assert all("api_key" not in request for request in run.raw_response["requests"])


def test_serpapi_deep_reuses_provider_upload_for_identical_focus_content(
    tmp_path: Path,
) -> None:
    image_path = tmp_path / "full.jpg"
    focus_path = tmp_path / "same-face-different-name.jpg"
    _write_jpeg(image_path)
    focus_path.write_bytes(image_path.read_bytes())
    upload_count = 0
    lens_image_ids: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal upload_count
        if request.url.path == "/image":
            upload_count += 1
            return httpx.Response(200, json={"image_id": "shared-image"})
        image_id = request.url.params["image_id"]
        lens_image_ids.append(image_id)
        return httpx.Response(
            200,
            json={
                "search_metadata": {
                    "id": f"lens-{request.url.params['type']}",
                    "status": "Success",
                },
                "search_parameters": {
                    "engine": "google_lens",
                    "image_id": image_id,
                    "type": request.url.params["type"],
                    "no_cache": "true",
                },
            },
        )

    provider = SerpApiLensProvider(
        "secret",
        search_mode="deep",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    run = provider.search(image_path, focus_image_path=focus_path)

    assert upload_count == 1
    assert lens_image_ids == ["shared-image", "shared-image"]
    assert run.raw_response["query_uploads"]["focus"]["provider_upload_reused"] is True
    assert run.raw_response["query_uploads"]["focus"]["provider_upload_reused_from"] == ("primary")
    assert run.raw_response["uploads"]["focus"]["reused_from"] == "primary"
    assert run.raw_response["lane_to_input"]["visual_matches"]["input_role"] == "focus"
    assert (
        run.raw_response["query_uploads"]["primary"]["sha256"]
        == (run.raw_response["query_uploads"]["focus"]["sha256"])
    )


def test_serpapi_standard_ignores_optional_focus_and_keeps_one_search(
    tmp_path: Path,
) -> None:
    image_path = tmp_path / "full.jpg"
    focus_path = tmp_path / "unused-focus.jpg"
    _write_jpeg(image_path)
    Image.new("RGB", (48, 48), color=(220, 30, 80)).save(focus_path, format="JPEG")
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        if request.url.path == "/image":
            return httpx.Response(200, json={"image_id": "standard-image"})
        assert request.url.params["image_id"] == "standard-image"
        assert request.url.params["type"] == "all"
        return httpx.Response(
            200,
            json={
                "search_metadata": {"id": "lens-all", "status": "Success"},
                "search_parameters": {
                    "engine": "google_lens",
                    "image_id": "standard-image",
                    "type": "all",
                    "no_cache": "true",
                },
            },
        )

    provider = SerpApiLensProvider(
        "secret",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    run = provider.search(image_path, focus_image_path=focus_path)

    assert requests == [("POST", "/image"), ("GET", "/search.json")]
    assert set(run.raw_response["query_uploads"]) == {"primary"}
    assert run.raw_response["lane_to_input"]["all"]["input_role"] == "primary"
    assert run.raw_response["search_mode"] == "standard"


def test_serpapi_deep_validates_focus_before_any_provider_upload(tmp_path: Path) -> None:
    image_path = tmp_path / "full.jpg"
    _write_jpeg(image_path)
    provider_requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal provider_requests
        provider_requests += 1
        return httpx.Response(500)

    provider = SerpApiLensProvider(
        "secret",
        search_mode="deep",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(SearchError, match="Focus image does not exist"):
        provider.search(image_path, focus_image_path=tmp_path / "missing-focus.jpg")

    assert provider_requests == 0


def test_serpapi_standard_soft_empty_returns_preserved_zero_candidate_run(
    tmp_path: Path,
) -> None:
    image_path = tmp_path / "input.jpg"
    _write_jpeg(image_path)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/image":
            return httpx.Response(200, json={"image_id": "image-empty"})
        assert request.url.params["type"] == "all"
        return httpx.Response(
            200,
            json={
                "search_metadata": {"id": "lens-empty", "status": "Error"},
                "search_parameters": {
                    "engine": "google_lens",
                    "image_id": "image-empty",
                    "type": "all",
                    "no_cache": "true",
                },
                "error": "Google Lens hasn't returned any results for this query.",
            },
        )

    provider = SerpApiLensProvider(
        "secret", client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    run = provider.search(image_path)

    assert run.search_id == "lens-empty"
    assert run.search_ids == ("lens-empty",)
    assert run.search_types == ("all",)
    assert run.candidates == ()
    assert run.raw_response["search_outcomes"]["all"] == {
        "outcome": "soft-empty",
        "provider_error": "Google Lens hasn't returned any results for this query.",
    }
    assert run.raw_response["search"]["search_metadata"]["id"] == "lens-empty"


def test_serpapi_soft_empty_ignores_contradictory_candidates_and_labels(tmp_path: Path) -> None:
    image_path = tmp_path / "input.jpg"
    _write_jpeg(image_path)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/image":
            return httpx.Response(200, json={"image_id": "image-empty"})
        return httpx.Response(
            200,
            json={
                "search_metadata": {"id": "lens-empty", "status": "Error"},
                "search_parameters": {
                    "engine": "google_lens",
                    "image_id": "image-empty",
                    "type": "all",
                    "no_cache": "true",
                },
                "error": "Google Lens hasn't returned any results for this query.",
                "knowledge_graph": {"title": "Must not be trusted"},
                "visual_matches": [
                    {
                        "position": 1,
                        "link": "https://x.com/should-not-pass/status/123",
                    }
                ],
            },
        )

    provider = SerpApiLensProvider(
        "secret", client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    run = provider.search(image_path)

    assert run.candidates == ()
    assert run.web_labels == ()
    assert run.raw_response["search"]["visual_matches"]


def test_serpapi_deep_exact_soft_empty_continues_to_visual_results(tmp_path: Path) -> None:
    image_path = tmp_path / "input.jpg"
    _write_jpeg(image_path)
    requested_types: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/image":
            return httpx.Response(200, json={"image_id": "image-deep"})
        search_type = request.url.params["type"]
        requested_types.append(search_type)
        common = {
            "search_metadata": {"id": f"lens-{search_type}", "status": "Success"},
            "search_parameters": {
                "engine": "google_lens",
                "image_id": "image-deep",
                "type": search_type,
                "no_cache": "true",
            },
        }
        if search_type == "exact_matches":
            common["search_metadata"]["status"] = "Error"
            common["error"] = "Google Lens hasn't returned any results for this query."
        else:
            common["visual_matches"] = [
                {
                    "position": 1,
                    "link": "https://x.com/volunteer/status/123",
                    "title": "Visual result",
                }
            ]
        return httpx.Response(200, json=common)

    provider = SerpApiLensProvider(
        "secret",
        search_mode="deep",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    run = provider.search(image_path)

    assert requested_types == ["exact_matches", "visual_matches"]
    assert [item.normalized_url for item in run.candidates] == [
        "https://x.com/volunteer/status/123"
    ]
    assert run.raw_response["search_outcomes"]["exact_matches"]["outcome"] == "soft-empty"
    assert run.raw_response["search_outcomes"]["visual_matches"]["outcome"] == "success"


def test_serpapi_deep_visual_soft_empty_keeps_exact_results(tmp_path: Path) -> None:
    image_path = tmp_path / "input.jpg"
    _write_jpeg(image_path)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/image":
            return httpx.Response(200, json={"image_id": "image-deep"})
        search_type = request.url.params["type"]
        common = {
            "search_metadata": {"id": f"lens-{search_type}", "status": "Success"},
            "search_parameters": {
                "engine": "google_lens",
                "image_id": "image-deep",
                "type": search_type,
                "no_cache": "true",
            },
        }
        if search_type == "exact_matches":
            common["exact_matches"] = [
                {
                    "position": 1,
                    "link": "https://reddit.com/r/pics/comments/abc/consented-post",
                    "title": "Exact result",
                }
            ]
        else:
            common["search_metadata"]["status"] = "Error"
            common["error"] = "Google Lens has not returned any results for this query"
        return httpx.Response(200, json=common)

    provider = SerpApiLensProvider(
        "secret",
        search_mode="deep",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    run = provider.search(image_path)

    assert len(run.candidates) == 1
    assert run.candidates[0].result_type == "exact_match"
    assert run.candidates[0].exact_match is True
    assert run.raw_response["search_outcomes"]["visual_matches"]["outcome"] == "soft-empty"


def test_serpapi_all_parses_exact_before_visual_and_deduplicates(tmp_path: Path) -> None:
    image_path = tmp_path / "input.jpg"
    _write_jpeg(image_path)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/image":
            return httpx.Response(200, json={"image_id": "image-all"})
        return httpx.Response(
            200,
            json={
                "search_metadata": {"id": "lens-all", "status": "Success"},
                "search_parameters": {
                    "engine": "google_lens",
                    "image_id": "image-all",
                    "type": "all",
                    "no_cache": "true",
                },
                "exact_matches": [
                    {
                        "position": 8,
                        "link": "https://x.com/volunteer/status/123",
                        "title": "Exact copy",
                    }
                ],
                "visual_matches": [
                    {
                        "position": 1,
                        "link": "https://x.com/volunteer/status/123",
                        "title": "Duplicate visual copy",
                    },
                    {
                        "position": 2,
                        "link": "https://youtu.be/video456",
                        "title": "Another visual result",
                    },
                ],
            },
        )

    provider = SerpApiLensProvider(
        "secret", client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    run = provider.search(image_path)

    assert [item.result_type for item in run.candidates] == ["exact_match", "visual_match"]
    assert [item.rank for item in run.candidates] == [1, 2]
    assert run.candidates[0].title == "Exact copy"
    assert run.candidates[1].post_id == "video456"


def test_serpapi_all_retains_distinct_media_variants_for_the_same_post(
    tmp_path: Path,
) -> None:
    image_path = tmp_path / "input.jpg"
    _write_jpeg(image_path)
    post = "https://x.com/volunteer/status/123"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/image":
            return httpx.Response(200, json={"image_id": "image-all"})
        return httpx.Response(
            200,
            json={
                "search_metadata": {"id": "lens-all", "status": "Success"},
                "search_parameters": {
                    "engine": "google_lens",
                    "image_id": "image-all",
                    "type": "all",
                    "no_cache": "true",
                },
                "exact_matches": [
                    {
                        "position": 1,
                        "link": post,
                        "image": "https://images.example/exact.jpg",
                    }
                ],
                "visual_matches": [
                    {
                        "position": 2,
                        "link": post,
                        "image": "https://images.example/exact.jpg",
                    },
                    {
                        "position": 3,
                        "link": post,
                        "thumbnail": "https://images.example/face-bearing-visual.jpg",
                    },
                ],
            },
        )

    provider = SerpApiLensProvider(
        "secret",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    run = provider.search(image_path)

    assert [item.result_type for item in run.candidates] == ["exact_match", "visual_match"]
    assert run.candidates[0].exact_match is True
    assert run.candidates[1].thumbnail_url == ("https://images.example/face-bearing-visual.jpg")
    assert run.candidates[0].provider_item_id != run.candidates[1].provider_item_id


@pytest.mark.parametrize(
    "provider_error",
    [
        "Invalid API key.",
        "Your account has run out of searches.",
    ],
)
def test_serpapi_deep_hard_provider_errors_still_fail_closed(
    tmp_path: Path, provider_error: str
) -> None:
    image_path = tmp_path / "input.jpg"
    _write_jpeg(image_path)
    requested_types: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/image":
            return httpx.Response(200, json={"image_id": "image-deep"})
        requested_types.append(request.url.params["type"])
        return httpx.Response(
            200,
            json={
                "search_metadata": {"id": "lens-error", "status": "Error"},
                "search_parameters": {
                    "engine": "google_lens",
                    "image_id": "image-deep",
                    "type": request.url.params["type"],
                    "no_cache": "true",
                },
                "error": provider_error,
            },
        )

    provider = SerpApiLensProvider(
        "secret",
        search_mode="deep",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(SearchError, match="SerpApi Lens error"):
        provider.search(image_path)

    assert requested_types == ["exact_matches"]


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
