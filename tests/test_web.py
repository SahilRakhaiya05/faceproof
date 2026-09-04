from __future__ import annotations

import hashlib
import io
import json
import re
import struct
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fastapi.testclient import TestClient
from PIL import Image

import faceproof.web as web_module
from faceproof.config import Settings
from faceproof.face import FaceQualityError, FaceQualityMetrics, QualityPolicy
from faceproof.pipeline import PipelineResult
from faceproof.web import create_app


def _settings(tmp_path: Path, *, chain: bool = False) -> Settings:
    return Settings(
        serpapi_api_key=None,
        model_dir=tmp_path / "models",
        output_dir=tmp_path / "evidence",
        rpc_url="http://127.0.0.1:8545",
        chain_id=84532,
        contract_address="0x" + "11" * 20 if chain else None,
        private_key="0x" + "22" * 32 if chain else None,
        confirmations=1,
        http_timeout_seconds=1,
        contract_code_hash="0x" + "33" * 32 if chain else None,
        source_revision="44" * 20 if chain else None,
    )


def _csrf(client: TestClient) -> str:
    response = client.get("/")
    match = re.search(r'name="faceproof-csrf" content="([^"]+)"', response.text)
    assert match
    return match.group(1)


def _png_bytes() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (80, 80), color=(80, 100, 120)).save(buffer, "PNG")
    return buffer.getvalue()


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _fake_result(root: Path, run_id: str, *, selected_url: str) -> PipelineResult:
    run = root / run_id
    query = run / "input" / "query.png"
    query.parent.mkdir(parents=True, exist_ok=True)
    query.write_bytes(_png_bytes())
    media = run / "candidates" / "01" / "candidate_media.png"
    media.parent.mkdir(parents=True, exist_ok=True)
    media.write_bytes(_png_bytes())
    candidate = {
        "provider": "test",
        "rank": 1,
        "page_url": selected_url,
        "normalized_url": selected_url,
        "title": "Consented public post",
        "source": "X",
        "post_id": "123",
    }
    _write_json(
        run / "search" / "provider-response.json",
        {
            "provider": "serpapi",
            "search_id": "search-1",
            "search_ids": ["search-1"],
            "search_types": ["all"],
            "retrieved_at": "2026-09-03T10:00:00Z",
            "live": True,
            "provider_mode": "no-cache",
            "candidates": [candidate],
            "web_labels": [],
        },
    )
    _write_json(
        run / "selection.json",
        {
            "candidate": candidate,
            "candidate_media": {"relative_path": "candidate_media.png"},
            "local_similarity_micros": 900_000,
            "threshold_micros": 363_000,
            "linkage_level": "captured-post-media-rematched",
        },
    )
    _write_json(run / "manifest.json", {"artifacts": [], "canonicalization": {}})
    _write_json(
        run / "commitment.json",
        {"manifest_sha256": "0x" + "55" * 32, "commitment": "0x" + "66" * 32},
    )
    return PipelineResult(
        run_id=run_id,
        run_dir=run,
        provider="serpapi",
        search_id="search-1",
        selected_url=selected_url,
        selected_title="Consented public post",
        selected_source="X",
        selected_media_path=media,
        web_labels=(),
        local_similarity=0.9,
        similarity_threshold=0.363,
        manifest_sha256="0x" + "55" * 32,
        commitment="0x" + "66" * 32,
        chain_receipt=None,
        chain_verification=None,
    )


def _consent_data(**overrides: str) -> dict[str, str]:
    data = {
        "consent_adult": "true",
        "consent_authorized": "true",
        "consent_public_search": "true",
        "consent_provider_upload": "true",
        "consent_profile_discovery": "false",
        "consent_chain_irreversible": "false",
        "mode": "discovery",
        "search_mode": "standard",
        "platforms": "x,linkedin",
        "max_candidates": "10",
    }
    data.update(overrides)
    return data


def _wait_for_job(client: TestClient, job_id: str) -> dict[str, Any]:
    for _ in range(100):
        job = client.get(f"/api/runs/{job_id}").json()
        if job["state"] in {"completed", "inconclusive", "failed"}:
            return job
        time.sleep(0.01)
    raise AssertionError("web job did not complete")


def test_homepage_is_local_security_hardened(tmp_path: Path) -> None:
    with TestClient(create_app(_settings(tmp_path))) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert "FaceProof" in response.text
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "__FACEPROOF_CSRF__" not in response.text


def test_untrusted_host_cannot_read_local_csrf_or_evidence(tmp_path: Path) -> None:
    with TestClient(create_app(_settings(tmp_path))) as client:
        response = client.get("/", headers={"Host": "attacker-rebind.example"})
        history = client.get(
            "/api/history",
            headers={"Host": "attacker-rebind.example"},
        )

    assert response.status_code == 421
    assert "faceproof-csrf" not in response.text
    assert history.status_code == 421


def test_mutating_routes_reject_cross_origin_browser_requests(tmp_path: Path) -> None:
    with TestClient(create_app(_settings(tmp_path))) as client:
        token = _csrf(client)
        denied_origin = client.post(
            "/api/preflight",
            files={"image": ("portrait.png", _png_bytes(), "image/png")},
            headers={
                "X-FaceProof-CSRF": token,
                "Origin": "https://attacker.example",
            },
        )
        denied_fetch_site = client.post(
            "/api/preflight",
            files={"image": ("portrait.png", _png_bytes(), "image/png")},
            headers={
                "X-FaceProof-CSRF": token,
                "Sec-Fetch-Site": "cross-site",
            },
        )

    assert denied_origin.status_code == 403
    assert denied_fetch_site.status_code == 403


def test_run_api_requires_csrf_consent_and_a_valid_image(tmp_path: Path) -> None:
    with TestClient(create_app(_settings(tmp_path))) as client:
        denied = client.post("/api/runs", data=_consent_data())
        assert denied.status_code == 403
        token = _csrf(client)
        invalid = client.post(
            "/api/runs",
            data=_consent_data(),
            files={"image": ("fake.jpg", b"not-an-image", "image/jpeg")},
            headers={"X-FaceProof-CSRF": token, "Idempotency-Key": "request-12345"},
        )

    assert invalid.status_code == 400
    assert "valid image" in invalid.json()["detail"]


def test_local_preflight_returns_quality_without_searching_or_persisting(
    tmp_path: Path, monkeypatch
) -> None:
    observed_path: Path | None = None

    class FakeBackend:
        quality_policy = QualityPolicy()

        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def encode_one(self, path: Path) -> SimpleNamespace:
            nonlocal observed_path
            observed_path = Path(path)
            assert observed_path.is_file()
            return SimpleNamespace(
                embedding=tuple([0.1] * 128),
                detection=SimpleNamespace(
                    confidence=0.97,
                    box=SimpleNamespace(x=8.0, y=9.0, width=70.0, height=72.0),
                ),
                quality=FaceQualityMetrics(
                    confidence=0.97,
                    face_width_px=70.0,
                    face_height_px=72.0,
                    face_area_ratio=0.30,
                    visible_fraction=1.0,
                    sharpness=85.0,
                    brightness=120.0,
                ),
                aligned_size=(112, 112),
                models=SimpleNamespace(
                    as_dict=lambda: {
                        "detector_name": "yunet.onnx",
                        "detector_sha256": "a" * 64,
                        "recognizer_name": "sface.onnx",
                        "recognizer_sha256": "b" * 64,
                    }
                ),
            )

    monkeypatch.setattr(web_module, "OpenCVFaceBackend", FakeBackend)
    monkeypatch.setattr(
        web_module,
        "verify_default_models",
        lambda _path: {"detector": "ok", "recognizer": "ok"},
    )

    with TestClient(create_app(_settings(tmp_path))) as client:
        token = _csrf(client)
        response = client.post(
            "/api/preflight",
            data={"consent_adult": "true", "consent_authorized": "true"},
            files={"image": ("face.png", _png_bytes(), "image/png")},
            headers={"X-FaceProof-CSRF": token},
        )

    assert response.status_code == 200
    result = response.json()
    assert result["passed"] is True
    assert result["local_only"] is True
    assert result["external_upload"] is False
    assert result["search_credit_consumed"] is False
    assert result["embedding_persisted"] is False
    assert result["embedding_dimensions"] == 128
    assert "embedding" not in result
    assert result["quality"]["face_width_px"] == 70.0
    assert observed_path is not None and not observed_path.exists()


def test_local_preflight_reports_actionable_quality_failure_without_credit(
    tmp_path: Path, monkeypatch
) -> None:
    metrics = FaceQualityMetrics(
        confidence=0.94,
        face_width_px=41.0,
        face_height_px=43.0,
        face_area_ratio=0.01,
        visible_fraction=1.0,
        sharpness=60.0,
        brightness=120.0,
    )

    class FakeBackend:
        quality_policy = QualityPolicy()

        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def encode_one(self, _path: Path) -> None:
            raise FaceQualityError(("face_too_small", "face_area_too_small"), metrics)

    monkeypatch.setattr(web_module, "OpenCVFaceBackend", FakeBackend)
    monkeypatch.setattr(
        web_module,
        "verify_default_models",
        lambda _path: {"detector": "ok", "recognizer": "ok"},
    )

    with TestClient(create_app(_settings(tmp_path))) as client:
        token = _csrf(client)
        denied = client.post(
            "/api/preflight",
            data={"consent_adult": "true", "consent_authorized": "true"},
            files={"image": ("face.png", _png_bytes(), "image/png")},
        )
        response = client.post(
            "/api/preflight",
            data={"consent_adult": "true", "consent_authorized": "true"},
            files={"image": ("face.png", _png_bytes(), "image/png")},
            headers={"X-FaceProof-CSRF": token},
        )

    assert denied.status_code == 403
    result = response.json()
    assert result["passed"] is False
    assert result["code"] == "face-quality"
    assert result["issues"] == ["face_too_small", "face_area_too_small"]
    assert result["quality"]["face_width_px"] == 41.0
    assert result["requirements"]["min_face_size_px"] == 64
    assert result["search_credit_consumed"] is False
    assert "original-resolution" in result["action"]


def test_evidence_asset_route_exposes_images_but_not_private_json(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _fake_result(
        settings.output_dir,
        "asset-run",
        selected_url="https://x.com/volunteer/status/123",
    )

    with TestClient(create_app(settings)) as client:
        image = client.get("/api/evidence/asset-run/asset/input/query.png")
        private_json = client.get("/api/evidence/asset-run/asset/commitment.json")

    assert image.status_code == 200
    assert image.headers["content-type"] == "image/png"
    assert private_json.status_code == 404


def test_discovery_job_is_idempotent_and_returns_safe_summary(tmp_path: Path) -> None:
    calls: list[dict[str, Any]] = []

    def fake_pipeline(**kwargs: Any) -> PipelineResult:
        calls.append(kwargs)
        kwargs["on_stage"]("Detecting, quality-checking, and encoding the query face")
        kwargs["on_stage"]("Running genuine live Google Lens search through SerpApi")
        return _fake_result(
            kwargs["settings"].output_dir,
            "web-discovery-1",
            selected_url="https://x.com/volunteer/status/123",
        )

    with TestClient(create_app(_settings(tmp_path), pipeline_runner=fake_pipeline)) as client:
        token = _csrf(client)
        request = {
            "data": _consent_data(
                consent_profile_discovery="true",
                platforms="x",
            ),
            "files": {"image": ("face.png", _png_bytes(), "image/png")},
            "headers": {"X-FaceProof-CSRF": token, "Idempotency-Key": "same-request-123"},
        }
        first = client.post("/api/runs", **request)
        second = client.post("/api/runs", **request)
        assert first.status_code == second.status_code == 202
        assert first.json()["job_id"] == second.json()["job_id"]
        job = _wait_for_job(client, first.json()["job_id"])

    assert job["state"] == "completed"
    assert job["result"]["status"] == "discovered"
    assert len(calls) == 1
    assert calls[0]["skip_anchor"] is True
    assert calls[0]["platforms"] == frozenset({"x"})
    assert calls[0]["profile_discovery_authorized"] is True
    assert calls[0]["profile_platforms"] == frozenset({"linkedin"})


def test_bluesky_discovery_requires_no_provider_upload_or_api_key(tmp_path: Path) -> None:
    observed: dict[str, Any] = {}

    def fake_pipeline(**kwargs: Any) -> PipelineResult:
        observed.update(kwargs)
        return _fake_result(
            kwargs["settings"].output_dir,
            "web-bluesky-1",
            selected_url="https://bsky.app/profile/did:plc:volunteer/post/3ktest",
        )

    with TestClient(create_app(_settings(tmp_path), pipeline_runner=fake_pipeline)) as client:
        token = _csrf(client)
        response = client.post(
            "/api/runs",
            data=_consent_data(
                consent_provider_upload="false",
                search_provider="bluesky",
                bluesky_actor="volunteer.bsky.social",
                search_mode="standard",
                platforms="x,reddit",
            ),
            files={"image": ("face.png", _png_bytes(), "image/png")},
            headers={"X-FaceProof-CSRF": token, "Idempotency-Key": "bluesky-request-123"},
        )
        assert response.status_code == 202, response.text
        job = _wait_for_job(client, response.json()["job_id"])

    assert job["state"] == "completed"
    assert observed["search_provider"] == "bluesky"
    assert observed["bluesky_actor"] == "volunteer.bsky.social"
    assert observed["search_mode"] == "standard"
    assert observed["platforms"] == frozenset({"bluesky"})
    assert observed["profile_discovery_authorized"] is False


def test_lens_discovery_requires_query_upload_consent(tmp_path: Path) -> None:
    with TestClient(create_app(_settings(tmp_path))) as client:
        token = _csrf(client)
        response = client.post(
            "/api/runs",
            data=_consent_data(consent_provider_upload="false", search_provider="lens"),
            files={"image": ("face.png", _png_bytes(), "image/png")},
            headers={"X-FaceProof-CSRF": token, "Idempotency-Key": "lens-no-upload-123"},
        )

    assert response.status_code == 400
    assert "reach SerpApi" in response.json()["detail"]


def test_anchor_reuses_verified_discovery_input_and_policy(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path, chain=True)
    reviewed = settings.output_dir / "reviewed-run"
    query = reviewed / "input" / "query.jpg"
    query.parent.mkdir(parents=True, exist_ok=True)
    query.write_bytes(b"sealed-review-input")
    selected_url = "https://x.com/volunteer/status/123"
    _write_json(
        reviewed / "selection.json",
        {
            "candidate": {
                "rank": 1,
                "normalized_url": selected_url,
                "page_url": selected_url,
                "title": "Reviewed post",
                "source": "X",
                "post_id": "123",
            },
            "local_similarity_micros": 900_000,
            "threshold_micros": 363_000,
        },
    )
    _write_json(
        reviewed / "manifest.json",
        {
            "artifacts": [],
            "metadata": {
                "search": {
                    "platform_filter": ["x"],
                    "platform_filter_mode": "explicit",
                    "search_mode": "deep",
                    "post_candidate_limit": 7,
                    "profile_candidate_limit": 0,
                },
                "consent": {
                    "profile_discovery_authorized": False,
                    "profile_platforms": [],
                },
            },
        },
    )
    review_manifest = "0x" + "77" * 32
    review_commitment = "0x" + "88" * 32
    _write_json(
        reviewed / "commitment.json",
        {"manifest_sha256": review_manifest, "commitment": review_commitment},
    )
    _write_json(
        reviewed / "search" / "provider-response.json",
        {"provider": "serpapi", "live": True, "candidates": []},
    )
    monkeypatch.setattr(
        web_module,
        "load_reviewed_discovery",
        lambda **_kwargs: SimpleNamespace(
            image_path=query,
            platforms=frozenset({"x"}),
            search_mode="deep",
            search_provider="lens",
            bluesky_actor=None,
            threshold=0.417,
            max_candidates=7,
            max_profile_candidates=0,
            profile_discovery_authorized=False,
            profile_platforms=None,
            manifest_sha256=review_manifest,
            commitment=review_commitment,
            input_sha256=hashlib.sha256(b"sealed-review-input").hexdigest(),
            input_bytes=b"sealed-review-input",
            content_identity="reviewed-content-identity",
        ),
    )
    monkeypatch.setattr(
        web_module,
        "_check_chain_readiness",
        lambda _settings: (True, "verified"),
    )
    observed: dict[str, Any] = {}

    def fake_pipeline(**kwargs: Any) -> PipelineResult:
        observed.update(kwargs)
        observed["image_bytes"] = Path(kwargs["image_path"]).read_bytes()
        return _fake_result(
            settings.output_dir,
            "anchored-attempt",
            selected_url=selected_url,
        )

    with TestClient(create_app(settings, pipeline_runner=fake_pipeline)) as client:
        token = _csrf(client)
        response = client.post(
            "/api/runs",
            data=_consent_data(
                mode="anchor",
                consent_chain_irreversible="true",
                consent_reference="volunteer-2026",
                approved_post_url=selected_url,
                review_run_id="reviewed-run",
                search_mode="standard",
                platforms="linkedin",
                max_candidates="20",
            ),
            files={"image": ("different.png", _png_bytes(), "image/png")},
            headers={"X-FaceProof-CSRF": token, "Idempotency-Key": "anchor-request-123"},
        )
        assert response.status_code == 202, response.text
        job = _wait_for_job(client, response.json()["job_id"])

    assert job["state"] == "completed"
    assert observed["image_bytes"] == b"sealed-review-input"
    assert observed["search_mode"] == "deep"
    assert observed["search_provider"] == "lens"
    assert observed["bluesky_actor"] is None
    assert observed["threshold"] == 0.417
    assert observed["platforms"] == frozenset({"x"})
    assert observed["max_candidates"] == 7
    assert observed["review_manifest_sha256"] == review_manifest
    assert observed["review_commitment"] == review_commitment
    assert observed["review_input_sha256"] == hashlib.sha256(b"sealed-review-input").hexdigest()
    assert observed["reviewed_content_identity"] == "reviewed-content-identity"


def test_private_export_is_verified_csrf_protected_and_manifest_allowlisted(
    tmp_path: Path, monkeypatch
) -> None:
    settings = _settings(tmp_path)
    result = _fake_result(
        settings.output_dir,
        "exportable-run",
        selected_url="https://x.com/volunteer/status/123",
    )
    (result.run_dir / "proof.txt").write_text("included", encoding="utf-8")
    (result.run_dir / "unlisted-private.txt").write_text("excluded", encoding="utf-8")
    _write_json(
        result.run_dir / "manifest.json",
        {
            "artifacts": [{"path": "proof.txt"}],
            "metadata": {"query_face": {"model_fingerprints": {"detector": "abc"}}},
        },
    )
    monkeypatch.setattr(
        web_module,
        "verify_run",
        lambda *_args, **_kwargs: SimpleNamespace(passed=True),
    )

    with TestClient(create_app(settings)) as client:
        token = _csrf(client)
        assert client.get("/api/evidence/exportable-run/download").status_code == 405
        assert client.post("/api/evidence/exportable-run/download").status_code == 403
        response = client.post(
            "/api/evidence/exportable-run/download",
            headers={"X-FaceProof-CSRF": token},
        )

    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        names = set(archive.namelist())
    assert "exportable-run/proof.txt" in names
    assert "exportable-run/manifest.json" in names
    assert "exportable-run/commitment.json" in names
    assert "exportable-run/unlisted-private.txt" not in names


def test_private_export_zips_the_same_snapshot_that_was_verified(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = _settings(tmp_path)
    result = _fake_result(
        settings.output_dir,
        "snapshot-export",
        selected_url="https://x.com/volunteer/status/123",
    )
    proof = result.run_dir / "proof.txt"
    proof.write_bytes(b"verified-version-a")
    _write_json(
        result.run_dir / "manifest.json",
        {"artifacts": [{"path": "proof.txt"}], "metadata": {}},
    )

    def verify_then_replace(snapshot: Path, **_kwargs):
        assert snapshot.resolve() != result.run_dir.resolve()
        proof.write_bytes(b"unverified-version-b")
        return SimpleNamespace(passed=True)

    monkeypatch.setattr(web_module, "verify_run", verify_then_replace)

    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/api/evidence/snapshot-export/download",
            headers={"X-FaceProof-CSRF": _csrf(client)},
        )

    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert archive.read("snapshot-export/proof.txt") == b"verified-version-a"


def test_public_receipt_reads_the_same_snapshot_that_was_verified(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = _settings(tmp_path)
    result = _fake_result(
        settings.output_dir,
        "snapshot-receipt",
        selected_url="https://x.com/volunteer/status/123",
    )
    _write_json(
        result.run_dir / "manifest.json",
        {
            "artifacts": [],
            "metadata": {"query_face": {"model_fingerprints": {"detector": "abc"}}},
        },
    )
    original = json.loads((result.run_dir / "commitment.json").read_text(encoding="utf-8"))
    replacement = dict(original)
    replacement["commitment"] = "0x" + "ee" * 32

    def verify_then_replace(snapshot: Path, **_kwargs):
        assert snapshot.resolve() != result.run_dir.resolve()
        _write_json(result.run_dir / "commitment.json", replacement)
        return SimpleNamespace(passed=True, chain=None)

    monkeypatch.setattr(web_module, "verify_run", verify_then_replace)

    with TestClient(create_app(settings)) as client:
        response = client.get("/api/evidence/snapshot-receipt/public-receipt")

    assert response.status_code == 200
    assert response.json()["commitment"] == original["commitment"]
    assert response.json()["commitment"] != replacement["commitment"]


def test_public_receipt_omits_media_urls_labels_and_salt(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    result = _fake_result(
        settings.output_dir,
        "public-run",
        selected_url="https://x.com/private-name/status/123",
    )
    _write_json(
        result.run_dir / "manifest.json",
        {
            "artifacts": [],
            "metadata": {"query_face": {"model_fingerprints": {"detector": "abc"}}},
        },
    )
    monkeypatch.setattr(
        web_module,
        "verify_run",
        lambda *_args, **_kwargs: SimpleNamespace(passed=True),
    )

    with TestClient(create_app(settings)) as client:
        response = client.get("/api/evidence/public-run/public-receipt")

    assert response.status_code == 200
    receipt = response.json()
    serialized = json.dumps(receipt)
    assert receipt["schema"] == "faceproof-public-receipt/v1"
    assert receipt["status"] == "discovered-verified"
    assert receipt["live_provider_response"] is True
    assert receipt["live_no_cache"] is True
    assert receipt["provider_mode"] == "no-cache"
    assert "private-name" not in serialized
    assert "Consented public post" not in serialized
    assert "salt" not in receipt
    assert "media" not in receipt


def test_public_receipt_does_not_call_fresh_bluesky_response_no_cache(
    tmp_path: Path, monkeypatch
) -> None:
    settings = _settings(tmp_path)
    result = _fake_result(
        settings.output_dir,
        "public-bluesky-run",
        selected_url="https://bsky.app/profile/did:plc:private/post/3abc",
    )
    provider_path = result.run_dir / "search" / "provider-response.json"
    provider = json.loads(provider_path.read_text(encoding="utf-8"))
    provider.update(
        {
            "provider": "bluesky-public-api",
            "provider_mode": "public-no-auth-consented-account",
        }
    )
    _write_json(provider_path, provider)
    _write_json(
        result.run_dir / "manifest.json",
        {
            "artifacts": [],
            "metadata": {"query_face": {"model_fingerprints": {"detector": "abc"}}},
        },
    )
    monkeypatch.setattr(
        web_module,
        "verify_run",
        lambda *_args, **_kwargs: SimpleNamespace(passed=True),
    )

    with TestClient(create_app(settings)) as client:
        response = client.get("/api/evidence/public-bluesky-run/public-receipt")

    receipt = response.json()
    assert response.status_code == 200
    assert receipt["live_provider_response"] is True
    assert receipt["live_no_cache"] is None
    assert receipt["provider_mode"] == "public-no-auth-consented-account"
    assert receipt["search_id"] is None
    assert "did:plc:private" not in json.dumps(receipt)


def test_public_receipt_refuses_an_unresolved_anchor_submission(
    tmp_path: Path, monkeypatch
) -> None:
    settings = _settings(tmp_path)
    _fake_result(
        settings.output_dir,
        "pending-public-run",
        selected_url="https://x.com/volunteer/status/123",
    )
    anchor_submission_path = settings.output_dir / ".pending-public-run.anchor-submission.json"
    anchor_submission_path.write_text('{"schema":"pending"}', encoding="utf-8")
    monkeypatch.setattr(
        web_module,
        "verify_run",
        lambda *_args, **_kwargs: SimpleNamespace(passed=True),
    )

    with TestClient(create_app(settings)) as client:
        response = client.get("/api/evidence/pending-public-run/public-receipt")

    assert response.status_code == 409
    assert "pending recovery" in response.json()["detail"]


def test_public_receipt_rechecks_pending_state_after_verification(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = _settings(tmp_path)
    result = _fake_result(
        settings.output_dir,
        "racing-public-run",
        selected_url="https://x.com/volunteer/status/123",
    )

    def racing_verify(*_args, **_kwargs):
        (settings.output_dir / ".racing-public-run.anchor-submission.json").write_text(
            '{"schema":"pending"}',
            encoding="utf-8",
        )
        return SimpleNamespace(passed=True, chain=None)

    monkeypatch.setattr(web_module, "verify_run", racing_verify)

    with TestClient(create_app(settings)) as client:
        response = client.get("/api/evidence/racing-public-run/public-receipt")

    assert result.run_dir.is_dir()
    assert response.status_code == 409
    assert "pending recovery" in response.json()["detail"]


def test_unanchored_verify_reports_evidence_only_not_chain_pass(
    tmp_path: Path, monkeypatch
) -> None:
    settings = _settings(tmp_path)
    _fake_result(
        settings.output_dir,
        "unanchored-verify-run",
        selected_url="https://x.com/volunteer/status/123",
    )
    monkeypatch.setattr(
        web_module,
        "verify_run",
        lambda *_args, **_kwargs: SimpleNamespace(
            passed=True,
            evidence_ok=True,
            canonical_sidecar_ok=True,
            external_anchor_ok=None,
            evidence_detail={"errors": []},
            chain=None,
        ),
    )

    with TestClient(create_app(settings)) as client:
        token = _csrf(client)
        response = client.post(
            "/api/evidence/unanchored-verify-run/verify",
            headers={"X-FaceProof-CSRF": token},
        )

    assert response.status_code == 200
    assert response.json()["passed"] is True
    assert response.json()["scope"] == "evidence-only"
    assert response.json()["chain"] is None


def test_verify_replays_when_chain_receipt_appears_during_verification(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = _settings(tmp_path)
    result = _fake_result(
        settings.output_dir,
        "racing-verify-run",
        selected_url="https://x.com/volunteer/status/123",
    )
    require_chain_calls: list[bool] = []

    def racing_verify(*_args, **kwargs):
        require_chain = bool(kwargs["require_chain"])
        require_chain_calls.append(require_chain)
        if not require_chain:
            _write_json(result.run_dir / "chain-receipt.json", {"transaction_hash": "0x1"})
        return SimpleNamespace(
            passed=True,
            evidence_ok=True,
            canonical_sidecar_ok=True,
            external_anchor_ok=None,
            evidence_detail={"errors": []},
            chain=(
                SimpleNamespace(passed=True, detail="verified on chain") if require_chain else None
            ),
        )

    monkeypatch.setattr(web_module, "verify_run", racing_verify)

    with TestClient(create_app(settings)) as client:
        token = _csrf(client)
        response = client.post(
            "/api/evidence/racing-verify-run/verify",
            headers={"X-FaceProof-CSRF": token},
        )

    assert response.status_code == 200
    assert require_chain_calls == [False, True]
    assert response.json()["scope"] == "chain-and-evidence"
    assert response.json()["chain"]["passed"] is True


def test_preflight_rejects_pillow_decompression_bomb_as_bad_request(
    tmp_path: Path, monkeypatch
) -> None:
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
    monkeypatch.setattr(
        web_module,
        "verify_default_models",
        lambda _path: {"detector": "ok", "recognizer": "ok"},
    )

    with TestClient(create_app(_settings(tmp_path))) as client:
        token = _csrf(client)
        response = client.post(
            "/api/preflight",
            data={"consent_adult": "true", "consent_authorized": "true"},
            files={"image": ("bomb.bmp", raw, "image/bmp")},
            headers={"X-FaceProof-CSRF": token},
        )

    assert response.status_code == 400
    assert "valid image" in response.json()["detail"]


def test_recover_anchor_endpoint_is_csrf_protected_and_returns_verified_result(
    tmp_path: Path, monkeypatch
) -> None:
    settings = _settings(tmp_path, chain=True)
    run_dir = settings.output_dir / "pending-run"
    run_dir.mkdir(parents=True)
    transaction_hash = "0x" + "44" * 32
    recovered = SimpleNamespace(
        receipt=SimpleNamespace(transaction_hash=transaction_hash, block_number=12),
        verification=SimpleNamespace(to_dict=lambda: {"passed": True}),
    )
    monkeypatch.setattr(
        web_module,
        "recover_pending_anchor",
        lambda *_args, **_kwargs: recovered,
    )

    with TestClient(create_app(settings)) as client:
        token = _csrf(client)
        denied = client.post("/api/evidence/pending-run/recover-anchor")
        response = client.post(
            "/api/evidence/pending-run/recover-anchor",
            headers={"X-FaceProof-CSRF": token},
        )

    assert denied.status_code == 403
    assert response.status_code == 200
    assert response.json()["recovered"] is True
    assert response.json()["transaction_hash"] == transaction_hash
