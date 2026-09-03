from __future__ import annotations

import io
import json
import re
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fastapi.testclient import TestClient
from PIL import Image

import faceproof.web as web_module
from faceproof.config import Settings
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
            max_candidates=7,
            max_profile_candidates=0,
            profile_discovery_authorized=False,
            profile_platforms=None,
            manifest_sha256=review_manifest,
            commitment=review_commitment,
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
    assert observed["platforms"] == frozenset({"x"})
    assert observed["max_candidates"] == 7
    assert observed["review_manifest_sha256"] == review_manifest
    assert observed["review_commitment"] == review_commitment


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
    assert "private-name" not in serialized
    assert "Consented public post" not in serialized
    assert "salt" not in receipt
    assert "media" not in receipt


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
