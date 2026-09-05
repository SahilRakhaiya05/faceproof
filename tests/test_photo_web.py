from __future__ import annotations

import io
import json
import re
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from faceproof.capture import CapturedFile
from faceproof.config import Settings
from faceproof.photo_web import run_photo_search, verify_photo_run
from faceproof.search.base import SearchCandidate, SearchRun
from faceproof.web import create_app


def _photo(*, quality: int = 95, shift: int = 0) -> bytes:
    image = Image.new("RGB", (420, 300), (24, 35, 28))
    draw = ImageDraw.Draw(image)
    draw.rectangle((45 + shift, 34, 220 + shift, 245), fill=(177, 229, 112))
    draw.ellipse((250 - shift, 65, 355 - shift, 170), fill=(85, 188, 201))
    draw.line((20, 275, 395, 275), fill=(235, 235, 220), width=5)
    output = io.BytesIO()
    image.save(output, format="JPEG", quality=quality, subsampling=0)
    return output.getvalue()


class _Provider:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        pass

    def __enter__(self) -> _Provider:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def search(self, _path: Path) -> SearchRun:
        candidates = [
            SearchCandidate(
                provider="serpapi",
                rank=1,
                page_url="https://example.test/post/123",
                normalized_url="https://example.test/post/123",
                title="A public page containing the photo",
                source="Example",
                image_url="https://cdn.example.test/photo.jpg",
                result_type="visual_match",
            )
        ]
        return SearchRun.create(
            provider="serpapi",
            search_id="test-search-1",
            candidates=candidates,
            raw_response={"exact_matches": [], "visual_matches": []},
            live=True,
            provider_mode="no-cache",
            search_types=["all"],
        )


def _settings(root: Path) -> Settings:
    return Settings(
        serpapi_api_key="test-key",
        model_dir=root / "models",
        output_dir=root / "evidence",
        rpc_url="http://127.0.0.1:8545",
        chain_id=84532,
        contract_address=None,
        private_key=None,
        confirmations=1,
        http_timeout_seconds=2,
    )


def test_photo_search_records_copy_links_and_verifies_tamper_rejection(tmp_path: Path) -> None:
    query = _photo()
    candidate = _photo(quality=72)

    def download(_candidate: SearchCandidate, destination: Path, **_kwargs: object) -> CapturedFile:
        path = destination / "candidate.jpg"
        path.write_bytes(candidate)
        return CapturedFile(
            relative_path=path.name,
            sha256="0" * 64,
            byte_size=len(candidate),
            media_type="image/jpeg",
            source_url="https://cdn.example.test/photo.jpg",
        )

    events: list[str] = []
    result = run_photo_search(
        query,
        "1" * 64,
        tmp_path / "evidence" / "run-1",
        _settings(tmp_path),
        events.append,
        provider_factory=_Provider,
        download=download,
        scan=lambda _path, _settings: {
            "status": "encoded-locally",
            "dimensions": 128,
            "used_for_search_or_matching": False,
            "embedding_saved": False,
        },
    )

    assert result["status"] == "recorded"
    assert len(result["matches"]) == 1
    assert result["matches"][0]["url"] == "https://example.test/post/123"
    assert result["matches"][0]["comparison"]["decision"] in {"exact", "likely-copy"}
    assert result["references"][0]["classification"] == "confirmed-copy"
    assert result["references"][0]["checked"] is True
    assert result["receipt"]["network"] == "local-simulated-sha256"
    assert "complete photo" in " ".join(events)
    assert verify_photo_run(tmp_path / "evidence" / "run-1")["passed"] is True
    assert verify_photo_run(tmp_path / "evidence" / "run-1", tamper=True)["passed"] is False

    manifest = json.loads((tmp_path / "evidence" / "run-1" / "manifest.json").read_bytes())
    assert manifest["claim"].startswith("Whole-photo copies")
    assert manifest["search_type"] == "all"


def test_photo_search_keeps_unconfirmed_results_off_chain(tmp_path: Path) -> None:
    query = _photo()
    unrelated = _photo(shift=75)

    def download(_candidate: SearchCandidate, destination: Path, **_kwargs: object) -> CapturedFile:
        path = destination / "candidate.jpg"
        path.write_bytes(unrelated)
        return CapturedFile(
            relative_path=path.name,
            sha256="0" * 64,
            byte_size=len(unrelated),
            media_type="image/jpeg",
        )

    result = run_photo_search(
        query,
        "2" * 64,
        tmp_path / "evidence" / "run-2",
        _settings(tmp_path),
        lambda _message: None,
        provider_factory=_Provider,
        download=download,
        scan=lambda _path, _settings: {"status": "not-encoded", "embedding_saved": False},
    )

    assert result["status"] == "no-copies"
    assert result["matches"] == []
    assert result["references"][0]["classification"] == "checked-unconfirmed"
    assert result["receipt"] is None
    assert not (tmp_path / "evidence" / "chain.sqlite3").exists()


def test_photo_routes_report_status_and_reject_invalid_requests(tmp_path: Path) -> None:
    with TestClient(create_app(_settings(tmp_path))) as client:
        status = client.get("/api/photos/status")
        assert status.status_code == 200
        assert status.json()["search_mode"].startswith("web image-copy search")

        denied = client.post(
            "/api/photos",
            files={"image": ("photo.png", b"not-an-image", "image/png")},
            headers={"Idempotency-Key": "photo-route-test-0001"},
        )
        assert denied.status_code == 403

        homepage = client.get("/")
        token = re.search(r'name="faceproof-csrf" content="([^"]+)"', homepage.text)
        assert token
        invalid = client.post(
            "/api/photos",
            files={"image": ("photo.png", b"not-an-image", "image/png")},
            headers={
                "Idempotency-Key": "photo-route-test-0001",
                "X-FaceProof-CSRF": token.group(1),
            },
        )
        assert invalid.status_code == 403

        invalid = client.post(
            "/api/photos",
            data={"consent": "true"},
            files={"image": ("photo.png", b"not-an-image", "image/png")},
            headers={
                "Idempotency-Key": "photo-route-test-0001",
                "X-FaceProof-CSRF": token.group(1),
            },
        )
        assert invalid.status_code == 400


def test_photo_copy_storage_is_not_presented_as_face_run_history(tmp_path: Path) -> None:
    photo_root = tmp_path / "evidence" / ".photo-copies" / ("a" * 32)
    photo_root.mkdir(parents=True)
    (photo_root / "result.json").write_text(
        json.dumps(
            {
                "run_id": "a" * 32,
                "created_at": "2026-09-05T00:00:00Z",
                "matches": [],
                "status": "no-copies",
            }
        ),
        encoding="utf-8",
    )
    with TestClient(create_app(_settings(tmp_path))) as client:
        history = client.get("/api/history")
    assert history.status_code == 200
    assert history.json()["runs"] == []
