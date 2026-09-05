from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import httpx
import respx
from PIL import Image

from faceproof.capture import CapturedFile
from faceproof.config import Settings
from faceproof.photo_web import run_photo_search
from faceproof.search.base import (
    SearchCandidate,
    SearchRun,
    classify_domain,
    is_social_profile_url,
    platform_name,
)
from faceproof.search.tech_discovery import (
    TechProfile,
    discover_tech_profiles,
    extract_identity_seeds,
)


def test_platform_classification_devfolio_and_huggingface() -> None:
    assert platform_name("https://devfolio.co/@sahilrakhaiya") == "devfolio"
    assert platform_name("https://www.devfolio.co/@sahilrakhaiya") == "devfolio"
    assert classify_domain("https://devfolio.co/@sahilrakhaiya") == "devfolio"
    assert is_social_profile_url("https://devfolio.co/@sahilrakhaiya") is True

    assert platform_name("https://huggingface.co/LearnerSO") == "huggingface"
    assert platform_name("https://huggingface.co/sahilrakhaiya") == "huggingface"
    assert classify_domain("https://huggingface.co/LearnerSO") == "huggingface"
    assert is_social_profile_url("https://huggingface.co/LearnerSO") is True
    assert is_social_profile_url("https://huggingface.co/models") is False
    assert is_social_profile_url("https://huggingface.co/pricing") is False


def test_extract_identity_seeds_extracts_handles_and_names() -> None:
    candidates = [
        SearchCandidate(
            provider="serpapi",
            rank=1,
            page_url="https://github.com/SahilRakhaiya05",
            normalized_url="https://github.com/SahilRakhaiya05",
            title="Sahil Rakhaiya - Software Engineer - GitHub",
        ),
        SearchCandidate(
            provider="serpapi",
            rank=2,
            page_url="https://in.linkedin.com/in/sahilrakhaiya",
            normalized_url="https://in.linkedin.com/in/sahilrakhaiya",
            title="Sahil Rakhaiya - LinkedIn",
        ),
        SearchCandidate(
            provider="serpapi",
            rank=3,
            page_url="https://devfolio.co/@sahilrakhaiya",
            normalized_url="https://devfolio.co/@sahilrakhaiya",
            title="Devfolio Profile",
        ),
        SearchCandidate(
            provider="serpapi",
            rank=4,
            page_url="https://x.com/sahilrakhaiya",
            normalized_url="https://x.com/sahilrakhaiya",
            title="Sahil Rakhaiya (@sahilrakhaiya) / X",
        ),
    ]
    web_labels = ("Sahil Rakhaiya", "Gentleman in suit", "Formal outerwear")

    handles, names = extract_identity_seeds(candidates, web_labels)

    assert "SahilRakhaiya05" in handles
    assert "sahilrakhaiya" in handles
    assert "Sahil Rakhaiya" in names
    assert "Gentleman in suit" not in names
    assert "Formal outerwear" not in names


def test_tech_profile_to_candidate() -> None:
    profile = TechProfile(
        platform="devfolio",
        url="https://devfolio.co/@sahilrakhaiya",
        title="Sahil Rakhaiya (@sahilrakhaiya) · Devfolio",
        avatar_url="https://avatars.githubusercontent.com/u/144577420?v=4",
        bio="Full-stack builder",
        handle="sahilrakhaiya",
        name="Sahil Rakhaiya",
        verified=True,
    )
    candidate = profile.to_candidate(rank=0)
    assert candidate.provider == "tech-discovery"
    assert candidate.rank == 0
    assert candidate.result_type == "verified_developer_profile"
    assert candidate.normalized_url == "https://devfolio.co/@sahilrakhaiya"
    assert candidate.image_url == "https://avatars.githubusercontent.com/u/144577420?v=4"


@respx.mock
def test_discover_tech_profiles_with_mocked_endpoints() -> None:
    # 1. Mock GitHub API
    gh_data: dict[str, Any] = {
        "name": "Sahil Rakhaiya",
        "html_url": "https://github.com/SahilRakhaiya05",
        "avatar_url": "https://avatars.githubusercontent.com/u/144577420?v=4",
        "bio": "Passionate developer",
        "blog": "https://sahil.dev",
    }
    respx.get("https://api.github.com/users/SahilRakhaiya05").mock(
        return_value=httpx.Response(200, json=gh_data)
    )

    # 2. Mock GitHub README with connected social badges
    readme_text = """
    # Hi, I'm Sahil!
    [![LinkedIn](https://img.shields.io/badge/LinkedIn-blue)](https://www.linkedin.com/in/sahilrakhaiya)
    [![Devfolio](https://img.shields.io/badge/Devfolio-white)](https://devfolio.co/@sahilrakhaiya)
    [![X](https://img.shields.io/badge/X-black)](https://x.com/sahilrakhaiya)
    """
    respx.get(
        "https://raw.githubusercontent.com/SahilRakhaiya05/SahilRakhaiya05/main/README.md"
    ).mock(return_value=httpx.Response(200, text=readme_text))

    # 3. Mock Devfolio
    next_data = {
        "props": {
            "pageProps": {
                "dehydratedState": {
                    "queries": [
                        {
                            "state": {
                                "data": {
                                    "users": [
                                        {
                                            "first_name": "Sahil",
                                            "last_name": "Rakhaiya",
                                            "short_bio": "Blockchain developer",
                                            "profiles": [
                                                {
                                                    "profile": {"name": "GitHub"},
                                                    "value": "https://github.com/SahilRakhaiya05",
                                                },
                                                {
                                                    "profile": {"name": "LinkedIn"},
                                                    "value": "https://linkedin.com/in/sahilrakhaiya",
                                                },
                                            ],
                                        }
                                    ]
                                }
                            }
                        }
                    ]
                }
            }
        }
    }
    next_json = json.dumps(next_data)
    devfolio_html = (
        f'<html><script id="__NEXT_DATA__" type="application/json">{next_json}</script></html>'
    )
    respx.get("https://devfolio.co/@sahilrakhaiya").mock(
        return_value=httpx.Response(200, text=devfolio_html)
    )

    # 4. Mock Hugging Face
    hf_data: dict[str, Any] = {
        "user": "LearnerSO",
        "fullname": "Sahil Rakhaiya",
        "avatarUrl": "/avatars/custom.svg",
    }
    respx.get("https://huggingface.co/api/users/LearnerSO/overview").mock(
        return_value=httpx.Response(200, json=hf_data)
    )
    respx.get("https://huggingface.co/api/users/sahilrakhaiya/overview").mock(
        return_value=httpx.Response(404)
    )
    respx.get("https://huggingface.co/api/users/SahilRakhaiya05/overview").mock(
        return_value=httpx.Response(404)
    )

    # 5. Mock DuckDuckGo HTML search for Hugging Face
    ddg_html = '<html><a href="https://huggingface.co/LearnerSO">LearnerSO</a></html>'
    respx.get("https://html.duckduckgo.com/html/?q=site:huggingface.co+%22Sahil+Rakhaiya%22").mock(
        return_value=httpx.Response(200, text=ddg_html)
    )

    with httpx.Client() as client:
        profiles = discover_tech_profiles(
            seed_handles=["SahilRakhaiya05"],
            seed_names=["Sahil Rakhaiya"],
            client=client,
        )

    platforms = {p.platform for p in profiles}
    assert "github" in platforms
    assert "linkedin" in platforms
    assert "devfolio" in platforms
    assert "x" in platforms
    assert "huggingface" in platforms

    gh_profile = next(p for p in profiles if p.platform == "github")
    assert gh_profile.avatar_url == "https://avatars.githubusercontent.com/u/144577420?v=4"
    hf_profile = next(p for p in profiles if p.platform == "huggingface")
    assert hf_profile.avatar_url == "https://huggingface.co/avatars/custom.svg"


def test_strict_face_match_threshold_rejects_strangers_and_confirms_developers(
    tmp_path: Path,
) -> None:
    # Query image
    img = Image.new("RGB", (200, 200), (30, 40, 50))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    query = buf.getvalue()

    # Different candidate image
    img_cand = Image.new("RGB", (200, 200), (200, 100, 50))
    buf_cand = io.BytesIO()
    img_cand.save(buf_cand, format="JPEG")
    cand_bytes = buf_cand.getvalue()

    class _MockProvider:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def __enter__(self) -> _MockProvider:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def search(self, _path: Path) -> SearchRun:
            candidates = [
                # Stranger on LinkedIn
                SearchCandidate(
                    provider="serpapi",
                    rank=1,
                    page_url="https://in.linkedin.com/in/seemanth-kulal",
                    normalized_url="https://in.linkedin.com/in/seemanth-kulal",
                    title="Seemanth Kulal - LinkedIn",
                    source="LinkedIn",
                    image_url="https://cdn.example.test/stranger.jpg",
                    result_type="visual_match",
                ),
                # Verified Devfolio Profile
                SearchCandidate(
                    provider="tech-discovery",
                    rank=0,
                    page_url="https://devfolio.co/@sahilrakhaiya",
                    normalized_url="https://devfolio.co/@sahilrakhaiya",
                    title="Sahil Rakhaiya (@sahilrakhaiya) · Devfolio",
                    source="Devfolio",
                    image_url="https://cdn.example.test/devfolio.jpg",
                    result_type="verified_developer_profile",
                ),
            ]
            return SearchRun.create(
                provider="serpapi",
                search_id="test-search-thresh",
                candidates=candidates,
                raw_response={"exact_matches": [], "visual_matches": []},
                live=True,
                provider_mode="no-cache",
                search_types=["all"],
            )

    def download(_candidate: SearchCandidate, destination: Path, **_kwargs: object) -> CapturedFile:
        path = destination / "candidate.jpg"
        is_devfolio = "devfolio" in getattr(_candidate, "normalized_url", "")
        img_data = query if is_devfolio else cand_bytes
        path.write_bytes(img_data)
        return CapturedFile(
            relative_path=path.name,
            sha256="0" * 64,
            byte_size=len(img_data),
            media_type="image/jpeg",
            source_url="https://cdn.example.test/image.jpg",
        )

    settings = Settings(
        serpapi_api_key="test-key",
        model_dir=tmp_path / "models",
        output_dir=tmp_path / "evidence",
        rpc_url="http://127.0.0.1:8545",
        chain_id=84532,
        contract_address=None,
        private_key=None,
        confirmations=1,
        http_timeout_seconds=2,
    )

    result = run_photo_search(
        query,
        "a" * 64,
        tmp_path / "evidence" / "run-thresh",
        settings,
        lambda _m: None,
        provider_factory=_MockProvider,
        download=download,
        scan=lambda _path, _settings: {"status": "not-encoded", "embedding_saved": False},
    )

    assert result["status"] == "recorded"
    # The stranger must NOT be in matches
    match_urls = [m["url"] for m in result["matches"]]
    assert "https://in.linkedin.com/in/seemanth-kulal" not in match_urls
    # The verified developer profile with matching photo MUST be in matches
    assert "https://devfolio.co/@sahilrakhaiya" in match_urls
    devfolio_match = next(
        m for m in result["matches"] if m["url"] == "https://devfolio.co/@sahilrakhaiya"
    )
    assert devfolio_match["classification"] == "confirmed-copy"

    # Verify stranger classification was marked checked-unconfirmed
    stranger_ref = next(
        r for r in result["references"] if r["url"] == "https://in.linkedin.com/in/seemanth-kulal"
    )
    assert stranger_ref["classification"] == "checked-unconfirmed"


def test_extract_name_tokens_and_profile_consistency() -> None:
    from faceproof.search.tech_discovery import (
        extract_name_tokens,
        is_profile_consistent_with_subject,
    )

    tokens = extract_name_tokens("RajBhattacharyya (Raj Bhattacharyya) · GitHub")
    assert "raj" in tokens
    assert "bhattacharyya" in tokens
    assert "github" not in tokens

    subject = {"raj", "bhattacharyya"}
    # Real profile matching subject
    assert (
        is_profile_consistent_with_subject(
            "https://github.com/RajBhattacharyya",
            "RajBhattacharyya (Raj Bhattacharyya) · GitHub",
            subject,
        )
        is True
    )
    assert (
        is_profile_consistent_with_subject(
            "https://in.linkedin.com/in/rajbhattacharyya2004",
            "Raj Bhattacharyya - Machine Learning / Artificial ...",
            subject,
        )
        is True
    )

    # Third-party profile where face appeared in sidebar/connections
    assert (
        is_profile_consistent_with_subject(
            "https://in.linkedin.com/in/udita-bhaskar-709842244",
            "Udita Bhaskar - -- | LinkedIn",
            subject,
        )
        is False
    )
    assert (
        is_profile_consistent_with_subject(
            "https://in.linkedin.com/in/debabratamaity",
            "Debabrata Maity - Software Developer | LinkedIn",
            subject,
        )
        is False
    )


def test_evm_helpers() -> None:
    from faceproof.chain import explorer_url_for_tx, network_name_for_chain_id

    assert "sepolia.etherscan.io" in explorer_url_for_tx(11155111, "0x123abc")
    assert "sepolia.basescan.org" in explorer_url_for_tx(84532, "0x123abc")
    assert network_name_for_chain_id(11155111) == "Ethereum Sepolia"
    assert network_name_for_chain_id(84532) == "Base Sepolia"


def test_platform_classification_kaggle_devpost_leetcode() -> None:
    assert platform_name("https://www.kaggle.com/rajbhattacharyya") == "kaggle"
    assert classify_domain("https://www.kaggle.com/rajbhattacharyya") == "kaggle"
    assert is_social_profile_url("https://www.kaggle.com/rajbhattacharyya") is True

    assert platform_name("https://devpost.com/raj_frrr") == "devpost"
    assert classify_domain("https://devpost.com/raj_frrr") == "devpost"
    assert is_social_profile_url("https://devpost.com/raj_frrr") is True

    assert platform_name("https://leetcode.com/u/rajbhattacharyya") == "leetcode"
    assert classify_domain("https://leetcode.com/u/rajbhattacharyya") == "leetcode"
    assert is_social_profile_url("https://leetcode.com/u/rajbhattacharyya") is True

    assert platform_name("https://en.wikipedia.org/wiki/Narendra_Modi") == "wikipedia"
    assert classify_domain("https://en.wikipedia.org/wiki/Narendra_Modi") == "wikipedia"
    assert is_social_profile_url("https://en.wikipedia.org/wiki/Narendra_Modi") is True
    assert is_social_profile_url("https://en.wikipedia.org/wiki/Special:Search") is False


@respx.mock
def test_search_profiles_by_name() -> None:
    from faceproof.search.tech_discovery import search_profiles_by_name

    respx.get("https://serpapi.com/search.json").respond(
        status_code=200,
        json={
            "organic_results": [
                {
                    "link": "https://github.com/RajBhattacharyya",
                    "title": "RajBhattacharyya (Raj Bhattacharyya) - GitHub",
                },
                {
                    "link": "https://devfolio.co/@raj-bhattacharyya18",
                    "title": "Raj Bhattacharyya · Devfolio",
                },
                {
                    "link": "https://www.linkedin.com/in/kimberly-follmuth-123",
                    "title": "Kimberly Follmuth - Director of Strategic Initiatives",
                },
            ]
        },
    )

    candidates = search_profiles_by_name(
        "Raj Bhattacharyya",
        api_key="mock_key",
    )

    # Kimberly Follmuth is rejected because title does not match subject name tokens
    assert len(candidates) == 2
    gh_cand = next(c for c in candidates if "github.com" in c.normalized_url)
    assert gh_cand.image_url == "https://github.com/RajBhattacharyya.png"
    assert gh_cand.result_type == "name_search_profile"

    dev_cand = next(c for c in candidates if "devfolio.co" in c.normalized_url)
    # Devfolio without og:image does not forge a fake avatar
    assert dev_cand.image_url is None
    assert dev_cand.result_type == "name_search_profile"


def test_profile_consistency_rejects_strangers() -> None:
    from faceproof.search.tech_discovery import is_profile_consistent_with_subject

    subject_tokens = {"raj", "bhattacharyya"}

    # Authentic matches
    assert (
        is_profile_consistent_with_subject(
            "https://github.com/RajBhattacharyya",
            "Raj Bhattacharyya · GitHub",
            subject_tokens,
        )
        is True
    )
    assert (
        is_profile_consistent_with_subject(
            "https://devfolio.co/@raj-bhattacharyya18",
            "Raj Bhattacharyya · Devfolio",
            subject_tokens,
        )
        is True
    )

    # Strangers with unrelated names
    assert (
        is_profile_consistent_with_subject(
            "https://www.linkedin.com/in/kimberly-follmuth",
            "Kimberly Follmuth",
            subject_tokens,
        )
        is False
    )
    assert (
        is_profile_consistent_with_subject(
            "https://www.linkedin.com/in/david-barrett",
            "David Barrett",
            subject_tokens,
        )
        is False
    )

    # Strangers sharing only a 3-letter first name ("raj") without the distinctive surname
    assert (
        is_profile_consistent_with_subject(
            "https://www.linkedin.com/in/avinash-raj",
            "Avinash Raj",
            subject_tokens,
        )
        is False
    )
    assert (
        is_profile_consistent_with_subject(
            "https://github.com/Raj",
            "raj deenoo · GitHub",
            subject_tokens,
        )
        is False
    )


@respx.mock
def test_search_wikipedia_profile() -> None:
    from faceproof.search.tech_discovery import search_wikipedia_profile

    respx.get("https://en.wikipedia.org/w/api.php").respond(
        status_code=200,
        json=[
            "Narendra Modi",
            ["Narendra Modi"],
            ["Prime Minister of India"],
            ["https://en.wikipedia.org/wiki/Narendra_Modi"],
        ],
    )
    respx.get("https://en.wikipedia.org/api/rest_v1/page/summary/Narendra%20Modi").respond(
        status_code=200,
        json={
            "title": "Narendra Modi",
            "thumbnail": {"source": "https://upload.wikimedia.org/modi_thumb.jpg"},
            "originalimage": {"source": "https://upload.wikimedia.org/modi_orig.jpg"},
        },
    )

    cand = search_wikipedia_profile("Narendra Modi")
    assert cand is not None
    assert cand.source == "wikipedia"
    assert cand.result_type == "wikipedia_profile"
    assert cand.image_url == "https://upload.wikimedia.org/modi_orig.jpg"


def test_provenance_graph_in_photo_search(tmp_path: Path) -> None:
    settings = Settings(
        serpapi_api_key="test-key",
        model_dir=tmp_path / "models",
        output_dir=tmp_path / "evidence",
        rpc_url="http://127.0.0.1:8545",
        chain_id=84532,
        contract_address=None,
        private_key=None,
        confirmations=1,
        http_timeout_seconds=2,
    )

    img = Image.new("RGB", (100, 100), color=(120, 120, 120))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    query_bytes = buf.getvalue()

    class _MockProvider:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def __enter__(self) -> _MockProvider:
            return self

        def __exit__(self, *args: Any) -> None:
            pass

        def search(self, *args: Any, **kwargs: Any) -> SearchRun:
            return SearchRun(
                provider="serpapi",
                search_id="prov_test_id",
                retrieved_at="2026-09-05T00:00:00Z",
                live=False,
                search_types=("all",),
                candidates=(
                    SearchCandidate(
                        provider="serpapi",
                        rank=1,
                        page_url="https://github.com/RajBhattacharyya",
                        normalized_url="https://github.com/RajBhattacharyya",
                        title="RajBhattacharyya (Raj Bhattacharyya) · GitHub",
                    ),
                ),
                raw_response={},
                web_labels=("Raj Bhattacharyya",),
            )

    def download(candidate: Any, temporary: Path, timeout_seconds: float = 10) -> CapturedFile:
        dest = temporary / "image.jpg"
        dest.write_bytes(query_bytes)
        return CapturedFile(
            relative_path="image.jpg",
            sha256="fake_sha",
            byte_size=len(query_bytes),
            media_type="image/jpeg",
        )

    run_dir = tmp_path / "prov_run"
    result = run_photo_search(
        query_bytes,
        "query_sha",
        run_dir,
        settings,
        lambda _m: None,
        provider_factory=_MockProvider,
        download=download,
        scan=lambda _path, _settings: {"status": "not-encoded", "embedding_saved": False},
    )

    assert "provenance_graph" in result
    prov = result["provenance_graph"]
    assert prov["root"]["type"] == "query_photo"
    assert "Raj Bhattacharyya" in prov["identity_seeds"]["names"]
    assert "RajBhattacharyya" in prov["identity_seeds"]["handles"]
    assert prov["nodes_count"] >= 1
    assert prov["edges_count"] >= 1
