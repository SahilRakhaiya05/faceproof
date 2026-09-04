from __future__ import annotations

import base64
import hashlib
import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest
from PIL import Image

import faceproof.pipeline as pipeline_module
from faceproof.capture import CapturedFile, PostCapture
from faceproof.chain import AnchorReceipt, AnchorSubmission, ChainError, ChainVerification
from faceproof.config import Settings
from faceproof.evidence import build_manifest, canonical_manifest_bytes, compute_commitment
from faceproof.face import (
    BoundingBox,
    FaceDetection,
    FaceEncoding,
    FaceQualityError,
    FaceQualityMetrics,
    ModelFingerprints,
    QualityPolicy,
)
from faceproof.pipeline import (
    InconclusiveError,
    PipelineError,
    ReviewedContentIdentity,
    anchor_submission_path,
    recover_pending_anchor,
    run_pipeline,
    run_tamper_demo,
    verify_run,
)
from faceproof.search.base import SearchCandidate, SearchRun


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        serpapi_api_key="synthetic-test-key",
        model_dir=tmp_path / "models",
        output_dir=tmp_path / "evidence",
        rpc_url="http://127.0.0.1:8545",
        chain_id=31337,
        contract_address=None,
        private_key=None,
        confirmations=1,
        http_timeout_seconds=1,
    )


def _write_sidecars(run_dir: Path) -> None:
    artifact = run_dir / "source" / "result.txt"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"exact observed bytes")
    manifest = build_manifest(
        {"source/result.txt": artifact},
        run_id=run_dir.name,
        observed_at="2026-09-03T12:00:00Z",
        metadata={"search": {"provider": "synthetic-test-only"}},
    )
    commitment = compute_commitment(manifest, salt=b"\x42" * 32)
    (run_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (run_dir / "manifest.canonical.json").write_bytes(canonical_manifest_bytes(manifest))
    (run_dir / "commitment.json").write_text(
        json.dumps(commitment, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def test_unanchored_local_verification_and_tamper_demo(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_sidecars(run_dir)

    result = verify_run(run_dir, settings=_settings(tmp_path), require_chain=False)
    assert result.passed
    assert result.chain is None

    changed_path, tampered = run_tamper_demo(run_dir)
    assert changed_path == "source/result.txt"
    assert not tampered["ok"]
    assert any("mismatch" in error for error in tampered["errors"])


def test_verification_rejects_a_copied_bundle_under_a_different_run_id(
    tmp_path: Path,
) -> None:
    original = tmp_path / "discovery-original"
    original.mkdir()
    _write_sidecars(original)
    alias = tmp_path / "discovery-alias"
    shutil.copytree(original, alias)

    assert verify_run(original, settings=_settings(tmp_path), require_chain=False).passed
    with pytest.raises(PipelineError, match="run ID does not match"):
        verify_run(alias, settings=_settings(tmp_path), require_chain=False)


def test_anchored_run_requires_code_and_source_pins_before_processing(tmp_path: Path) -> None:
    settings = replace(
        _settings(tmp_path),
        contract_address="0x0000000000000000000000000000000000001234",
        private_key="test-only-key",
    )
    arguments = {
        "image_path": tmp_path / "not-read.jpg",
        "settings": settings,
        "consent_acknowledged": True,
        "consent_reference": "test-consent",
        "live": True,
        "skip_anchor": False,
        "approved_post_url": "https://x.com/example/status/123",
    }

    try:
        run_pipeline(**arguments)
    except PipelineError as exc:
        assert "CONTRACT_CODE_HASH" in str(exc)
    else:
        raise AssertionError("anchored run accepted an unpinned registry")

    arguments["settings"] = replace(settings, contract_code_hash="0x" + "11" * 32)
    try:
        run_pipeline(**arguments)
    except PipelineError as exc:
        assert "SOURCE_REVISION" in str(exc)
    else:
        raise AssertionError("anchored run accepted unpinned source")


def test_canonical_sidecar_tampering_is_detected(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_sidecars(run_dir)
    (run_dir / "manifest.canonical.json").write_bytes(b"{}")

    result = verify_run(run_dir, settings=_settings(tmp_path), require_chain=False)
    assert result.evidence_ok
    assert not result.canonical_sidecar_ok
    assert not result.passed


def test_unlisted_bundle_file_is_detected(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_sidecars(run_dir)
    (run_dir / "injected.txt").write_text("not in the manifest", encoding="utf-8")

    result = verify_run(run_dir, settings=_settings(tmp_path), require_chain=False)
    assert not result.evidence_ok
    assert not result.passed
    assert any("unexpected unlisted" in error for error in result.evidence_detail["errors"])


def test_verifier_uses_trusted_chain_config_not_bundle_claims(tmp_path: Path, monkeypatch) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_sidecars(run_dir)
    commitment = json.loads((run_dir / "commitment.json").read_text(encoding="utf-8"))
    (run_dir / "chain-receipt.json").write_text(
        json.dumps(
            {
                "chain_id": 666,
                "contract_address": "0x0000000000000000000000000000000000000666",
                "transaction_hash": "0x" + "aa" * 32,
            }
        ),
        encoding="utf-8",
    )
    trusted_address = "0x0000000000000000000000000000000000001234"
    settings = replace(
        _settings(tmp_path),
        chain_id=31337,
        contract_address=trusted_address,
        contract_code_hash="0x" + "11" * 32,
    )
    observed: dict[str, object] = {}

    def fake_verify(value, **kwargs):
        observed.update(kwargs)
        return ChainVerification(
            connected=True,
            chain_id_matches=True,
            anchored=True,
            commitment=value,
            confirmations_satisfied=True,
            receipt_consistent=True,
        )

    monkeypatch.setattr(pipeline_module, "verify_commitment_on_chain", fake_verify)
    result = verify_run(
        run_dir,
        settings=settings,
        expected_commitment=commitment["commitment"],
    )

    assert result.passed
    assert observed["expected_chain_id"] == 31337
    assert observed["contract_address"] == trusted_address
    assert observed["expected_code_hash"] == settings.contract_code_hash
    assert observed["required_confirmations"] == settings.confirmations


def test_verification_requires_trusted_contract_code_hash(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_sidecars(run_dir)
    (run_dir / "chain-receipt.json").write_text("{}", encoding="utf-8")
    settings = replace(
        _settings(tmp_path),
        contract_address="0x0000000000000000000000000000000000001234",
    )

    result = verify_run(run_dir, settings=settings)

    assert not result.passed
    assert result.chain is not None
    assert "CONTRACT_CODE_HASH" in result.chain.detail


def test_out_of_band_commitment_mismatch_fails(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_sidecars(run_dir)

    result = verify_run(
        run_dir,
        settings=_settings(tmp_path),
        require_chain=False,
        expected_commitment="0x" + "ff" * 32,
    )
    assert result.external_anchor_ok is False
    assert not result.passed


def test_commitment_sidecar_metadata_tampering_is_detected(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_sidecars(run_dir)
    commitment_path = run_dir / "commitment.json"
    commitment = json.loads(commitment_path.read_text(encoding="utf-8"))
    commitment["abi_backend"] = "fabricated-backend"
    commitment_path.write_text(json.dumps(commitment), encoding="utf-8")

    result = verify_run(run_dir, settings=_settings(tmp_path), require_chain=False)

    assert not result.evidence_ok
    assert not result.passed
    assert any("commitment sidecar" in error for error in result.evidence_detail["errors"])


def test_duplicate_json_keys_are_rejected(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_sidecars(run_dir)
    commitment_path = run_dir / "commitment.json"
    original = commitment_path.read_text(encoding="utf-8").strip()
    commitment_path.write_text(
        '{"scheme":"first","scheme":"second","padding":' + original + "}",
        encoding="utf-8",
    )

    try:
        verify_run(run_dir, settings=_settings(tmp_path), require_chain=False)
    except PipelineError as exc:
        assert "Duplicate JSON key" in str(exc)
    else:
        raise AssertionError("duplicate JSON keys were accepted")


def _encoding(embedding: tuple[float, ...]) -> FaceEncoding:
    return FaceEncoding(
        embedding=embedding,
        detection=FaceDetection(
            box=BoundingBox(10, 10, 100, 100),
            landmarks=((30, 40), (80, 40), (55, 60), (35, 85), (75, 85)),
            confidence=0.99,
        ),
        quality=FaceQualityMetrics(
            confidence=0.99,
            face_width_px=100,
            face_height_px=100,
            face_area_ratio=0.25,
            visible_fraction=1.0,
            sharpness=100,
            brightness=120,
        ),
        aligned_size=(112, 112),
        models=ModelFingerprints("yunet.onnx", "11" * 32, "sface.onnx", "22" * 32),
    )


def test_profile_discovery_requires_explicit_authorization(tmp_path: Path) -> None:
    try:
        run_pipeline(
            image_path=tmp_path / "unused.jpg",
            settings=_settings(tmp_path),
            consent_acknowledged=True,
            live=True,
            skip_anchor=True,
            max_profile_candidates=1,
        )
    except PipelineError as exc:
        assert "profile discovery requires" in str(exc).casefold()
    else:
        raise AssertionError("profile discovery started without explicit authorization")


def test_review_proof_requires_paired_bytes32_values(tmp_path: Path) -> None:
    try:
        run_pipeline(
            image_path=tmp_path / "unused.jpg",
            settings=_settings(tmp_path),
            consent_acknowledged=True,
            live=True,
            skip_anchor=True,
            review_manifest_sha256="0x" + "11" * 32,
        )
    except PipelineError as exc:
        assert "both manifest hash and commitment" in str(exc)
    else:
        raise AssertionError("an incomplete discovery proof was accepted")


def test_review_anchor_claim_is_atomic_and_single_use(tmp_path: Path) -> None:
    output = tmp_path / "evidence"
    output.mkdir()
    manifest_sha256 = "0x" + "11" * 32
    commitment = "0x" + "22" * 32

    claim = pipeline_module._claim_review_for_anchor(
        output,
        discovery_run_id="reviewed-run",
        anchor_run_id="anchor-one",
        manifest_sha256=manifest_sha256,
        commitment=commitment,
        input_sha256="33" * 32,
        content_identity_sha256="44" * 32,
    )

    record = json.loads(claim.read_text(encoding="utf-8"))
    assert record["single_use"] is True
    assert record["anchor_run_id"] == "anchor-one"
    assert record["content_identity_sha256"] == "44" * 32
    with pytest.raises(PipelineError, match="already authorized an anchor attempt"):
        pipeline_module._claim_review_for_anchor(
            output,
            discovery_run_id="reviewed-run",
            anchor_run_id="anchor-two",
            manifest_sha256=manifest_sha256,
            commitment=commitment,
            input_sha256="33" * 32,
            content_identity_sha256="44" * 32,
        )


def test_pipeline_rejects_reviewed_input_changed_after_loader(
    tmp_path: Path,
    monkeypatch,
) -> None:
    image = tmp_path / "reviewed.jpg"
    image.write_bytes(b"replacement-image")
    monkeypatch.setattr(
        pipeline_module,
        "verify_default_models",
        lambda *_args, **_kwargs: {"models": "ok"},
    )

    with pytest.raises(PipelineError, match="changed after discovery verification"):
        run_pipeline(
            image_path=image,
            settings=_settings(tmp_path),
            consent_acknowledged=True,
            live=True,
            skip_anchor=True,
            review_input_sha256="00" * 32,
        )


def test_pipeline_orchestrates_search_rematch_and_evidence(tmp_path: Path, monkeypatch) -> None:
    input_image = tmp_path / "query.jpg"
    Image.new("RGB", (32, 32), color=(100, 110, 120)).save(input_image, "JPEG")
    candidate_buffer = tmp_path / "candidate.png"
    Image.new("RGB", (32, 32), color=(120, 110, 100)).save(candidate_buffer, "PNG")
    encoded_candidate = base64.b64encode(candidate_buffer.read_bytes()).decode()

    class FakeBackend:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def encode_one(self, _image: Path) -> FaceEncoding:
            return _encoding((1.0, 0.0))

        def encode_faces(self, _image: Path) -> tuple[FaceEncoding, ...]:
            return (_encoding((0.99, 0.01)),)

    candidate = SearchCandidate(
        provider="serpapi",
        rank=1,
        page_url="https://x.com/volunteer/status/123",
        normalized_url="https://x.com/volunteer/status/123",
        thumbnail_base64=encoded_candidate,
        provider_score=95,
        post_id="123",
    )
    search_run = SearchRun.create(
        provider="serpapi",
        search_id="request-123",
        candidates=[candidate],
        raw_response={"synthetic_test_only": True},
        web_labels=["Synthetic volunteer"],
        live=True,
        provider_mode="test",
    )

    class FakeProvider:
        name = "serpapi"

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def search(self, _image: Path, *, focus_image_path: Path | None = None) -> SearchRun:
            assert focus_image_path is not None
            return search_run

    def fake_capture(candidate, destination: Path, **_kwargs) -> PostCapture:
        (destination / "capture_status.json").write_text(
            '{"synthetic_test_only":true}\n', encoding="utf-8"
        )
        return PostCapture(
            "captured",
            "public-html",
            (),
            {
                "post_identity_verified": True,
                "page_url": candidate.normalized_url,
                "post_id": candidate.post_id,
                "open_graph": {
                    "og:url": candidate.normalized_url,
                    "og:title": "Consented public post",
                },
            },
        )

    monkeypatch.setattr(pipeline_module, "OpenCVFaceBackend", FakeBackend)
    monkeypatch.setattr(
        pipeline_module,
        "verify_default_models",
        lambda _directory: {"yunet.onnx": "ok", "sface.onnx": "ok"},
    )
    monkeypatch.setattr(pipeline_module, "_make_provider", lambda *_args, **_kwargs: FakeProvider())
    monkeypatch.setattr(pipeline_module, "capture_public_post", fake_capture)

    result = run_pipeline(
        image_path=input_image,
        settings=_settings(tmp_path),
        consent_acknowledged=True,
        live=True,
        skip_anchor=True,
        threshold=0.5000004,
    )

    assert result.search_id == "request-123"
    assert result.selected_url.endswith("/123")
    assert result.selected_title is None
    assert result.web_labels == ("Synthetic volunteer",)
    assert result.similarity_threshold == 0.5
    assert result.selected_media_path.is_file()
    assert (result.run_dir / "manifest.json").is_file()
    manifest = json.loads((result.run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["metadata"]["search"]["platform_filter_mode"] == "all"
    assert manifest["metadata"]["search"]["provider_strategy"] == "lens"
    assert manifest["metadata"]["search"]["consented_actor"] is None
    assert manifest["metadata"]["search"]["resolved_actor_did"] is None
    assert (
        "client_recorded_live_provider_response_with_candidate_record_or_url_identifiers"
        in manifest["metadata"]["claim_boundary"]
    )
    verified = verify_run(result.run_dir, settings=_settings(tmp_path), require_chain=False)
    assert verified.passed


def test_anchor_rejects_same_permalink_when_reviewed_content_was_replaced(
    tmp_path: Path,
    monkeypatch,
) -> None:
    input_image = tmp_path / "query.jpg"
    Image.new("RGB", (32, 32), color=(100, 110, 120)).save(input_image, "JPEG")
    candidate_image = tmp_path / "candidate.png"
    Image.new("RGB", (32, 32), color=(120, 110, 100)).save(candidate_image, "PNG")
    candidate_bytes = candidate_image.read_bytes()
    candidate_hash = hashlib.sha256(candidate_bytes).hexdigest()
    encoded_candidate = base64.b64encode(candidate_bytes).decode()
    permalink = "https://x.com/volunteer/status/123"

    class FakeBackend:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def encode_one(self, _image: Path) -> FaceEncoding:
            return _encoding((1.0, 0.0))

        def encode_faces(self, _image: Path) -> tuple[FaceEncoding, ...]:
            return (_encoding((0.99, 0.01)),)

    candidate = SearchCandidate(
        provider="serpapi",
        rank=1,
        page_url=permalink,
        normalized_url=permalink,
        thumbnail_base64=encoded_candidate,
        provider_item_id="fresh-search:visual_matches:1:thumbnail",
        post_id="123",
    )
    search_run = SearchRun.create(
        provider="serpapi",
        search_id="fresh-search",
        candidates=[candidate],
        raw_response={"synthetic_test_only": True},
        live=True,
        provider_mode="no-cache",
    )

    class FakeProvider:
        name = "serpapi"

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def search(self, _image: Path, *, focus_image_path: Path | None = None) -> SearchRun:
            assert focus_image_path is not None
            return search_run

    def fake_capture(selected_candidate, destination: Path, **_kwargs) -> PostCapture:
        media_path = destination / "post_media.png"
        media_path.write_bytes(candidate_bytes)
        media = CapturedFile(
            relative_path=media_path.name,
            sha256=candidate_hash,
            byte_size=len(candidate_bytes),
            media_type="image/png",
            source_url="https://pbs.twimg.com/media/replacement.png",
        )
        return PostCapture(
            "captured",
            "public-html",
            (media,),
            {
                "post_identity_verified": True,
                "page_url": selected_candidate.normalized_url,
                "post_id": selected_candidate.post_id,
                "open_graph": {
                    "og:url": selected_candidate.normalized_url,
                    "og:title": "Replacement content",
                },
            },
            (media,),
        )

    reviewed_projection = {
        "method": "public-html",
        "normalized_url": permalink,
        "post_id": "123",
        "content": {"og:title": "Original reviewed content", "og:url": permalink},
    }
    reviewed_capture_hash = hashlib.sha256(
        json.dumps(
            reviewed_projection,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    reviewed_identity = ReviewedContentIdentity(
        search_provider="lens",
        normalized_url=permalink,
        post_id="123",
        candidate_media_sha256=candidate_hash,
        capture_method="public-html",
        capture_content_sha256=reviewed_capture_hash,
        capture_media_sha256=(candidate_hash,),
    )

    monkeypatch.setattr(pipeline_module, "OpenCVFaceBackend", FakeBackend)
    monkeypatch.setattr(
        pipeline_module,
        "verify_default_models",
        lambda _directory: {"yunet.onnx": "ok", "sface.onnx": "ok"},
    )
    monkeypatch.setattr(pipeline_module, "_make_provider", lambda **_kwargs: FakeProvider())
    monkeypatch.setattr(pipeline_module, "capture_public_post", fake_capture)
    monkeypatch.setattr(pipeline_module, "verify_git_source_revision", lambda _revision: None)
    anchor_called = False

    def unexpected_anchor(*_args, **_kwargs):
        nonlocal anchor_called
        anchor_called = True
        raise AssertionError("content replacement must be rejected before anchoring")

    monkeypatch.setattr(pipeline_module, "anchor_commitment", unexpected_anchor)
    settings = replace(
        _settings(tmp_path),
        contract_address="0x0000000000000000000000000000000000001234",
        private_key="test-only-private-key",
        contract_code_hash="0x" + "66" * 32,
        source_revision="a" * 40,
    )

    with pytest.raises(InconclusiveError, match="content/version changed") as exc_info:
        run_pipeline(
            image_path=input_image,
            settings=settings,
            consent_acknowledged=True,
            consent_reference="consented-volunteer",
            live=True,
            skip_anchor=False,
            approved_post_url=permalink,
            review_run_id="reviewed-run",
            review_manifest_sha256="0x" + "11" * 32,
            review_commitment="0x" + "22" * 32,
            review_input_sha256=hashlib.sha256(input_image.read_bytes()).hexdigest(),
            reviewed_content_identity=reviewed_identity,
        )

    assert anchor_called is False
    error = json.loads((exc_info.value.run_dir / "run-error.json").read_text())
    assert error["stage"] == "reviewed-content-binding"
    assert error["expected_content_identity_sha256"] == reviewed_identity.sha256
    assert error["observed_content_identity_sha256"] != reviewed_identity.sha256


def test_manifest_rejects_media_swapped_after_review_identity_was_built(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    candidate_dir = run_dir / "candidates" / "01"
    candidate_dir.mkdir(parents=True)
    media_path = candidate_dir / "candidate_media.png"
    original = b"reviewed-media"
    media_path.write_bytes(original)
    artifact = CapturedFile(
        relative_path=media_path.name,
        sha256=hashlib.sha256(original).hexdigest(),
        byte_size=len(original),
        media_type="image/png",
    )
    candidate = SearchCandidate(
        provider="serpapi",
        rank=1,
        page_url="https://x.com/volunteer/status/123",
        normalized_url="https://x.com/volunteer/status/123",
        post_id="123",
    )
    selected = pipeline_module.CandidateEvaluation(
        candidate=candidate,
        directory=candidate_dir,
        media_path=media_path,
        media_artifact=artifact,
        local_similarity=0.9,
        detected_faces=1,
        matched_face=_encoding((1.0, 0.0)),
        matched=True,
    )
    replacement = b"unreviewed-replacement"
    manifest = {
        "artifacts": [
            {
                "path": "candidates/01/candidate_media.png",
                "size": len(replacement),
                "sha256": hashlib.sha256(replacement).hexdigest(),
            }
        ]
    }

    with pytest.raises(PipelineError, match="changed while the evidence manifest"):
        pipeline_module._validate_manifest_content_artifacts(
            manifest,
            run_dir=run_dir,
            selected=selected,
            capture=PostCapture("captured", "public-html", (), {}),
        )


def test_pipeline_fails_closed_when_pinned_models_do_not_verify(
    tmp_path: Path, monkeypatch
) -> None:
    input_image = tmp_path / "query.jpg"
    Image.new("RGB", (32, 32), color=(100, 110, 120)).save(input_image, "JPEG")
    monkeypatch.setattr(
        pipeline_module,
        "verify_default_models",
        lambda _directory: {"face_recognition_sface_2021dec.onnx": "SHA-256 mismatch"},
    )

    try:
        run_pipeline(
            image_path=input_image,
            settings=_settings(tmp_path),
            consent_acknowledged=True,
            live=True,
            skip_anchor=True,
        )
    except PipelineError as exc:
        assert "model integrity" in str(exc)
        assert "SHA-256 mismatch" in str(exc)
    else:
        raise AssertionError("pipeline accepted an untrusted face model")


def test_bluesky_provider_needs_no_serpapi_key_and_receives_runtime_actor(
    tmp_path: Path, monkeypatch
) -> None:
    observed: dict[str, object] = {}
    sentinel = object()

    def fake_bluesky(actor: str, **kwargs: object) -> object:
        observed["actor"] = actor
        observed.update(kwargs)
        return sentinel

    monkeypatch.setattr(pipeline_module, "BlueskyAuthorFeedProvider", fake_bluesky)
    settings = replace(_settings(tmp_path), serpapi_api_key=None)

    provider = pipeline_module._make_provider(
        settings=settings,
        live=True,
        search_mode="standard",
        search_provider="bluesky",
        bluesky_actor="volunteer.bsky.social",
    )

    assert provider is sentinel
    assert observed["actor"] == "volunteer.bsky.social"
    assert observed["consent_confirmed"] is True
    assert observed["timeout_seconds"] == 1


def test_bluesky_pipeline_policy_rejects_missing_actor_before_processing(tmp_path: Path) -> None:
    settings = replace(_settings(tmp_path), serpapi_api_key=None)

    with pytest.raises(PipelineError, match="Bluesky actor"):
        run_pipeline(
            image_path=tmp_path / "unused.jpg",
            settings=settings,
            consent_acknowledged=True,
            live=True,
            skip_anchor=True,
            search_provider="bluesky",
        )


def test_pipeline_records_actionable_structured_face_quality_failure(
    tmp_path: Path, monkeypatch
) -> None:
    input_image = tmp_path / "small-face.jpg"
    Image.new("RGB", (194, 259), color=(100, 110, 120)).save(input_image, "JPEG")
    metrics = FaceQualityMetrics(
        confidence=0.919,
        face_width_px=32.944,
        face_height_px=42.818,
        face_area_ratio=0.028074,
        visible_fraction=1.0,
        sharpness=2610.747,
        brightness=114.077,
    )

    class QualityRejectingBackend:
        def __init__(self, *_args, **_kwargs) -> None:
            self.quality_policy = QualityPolicy()

        def encode_one(self, _image: Path) -> FaceEncoding:
            raise FaceQualityError(("face_too_small",), metrics)

    monkeypatch.setattr(pipeline_module, "OpenCVFaceBackend", QualityRejectingBackend)
    monkeypatch.setattr(
        pipeline_module,
        "verify_default_models",
        lambda _directory: {"yunet.onnx": "ok", "sface.onnx": "ok"},
    )

    with pytest.raises(PipelineError, match="face_too_small"):
        run_pipeline(
            image_path=input_image,
            settings=_settings(tmp_path),
            consent_acknowledged=True,
            live=True,
            skip_anchor=True,
        )

    run_dirs = list((_settings(tmp_path).output_dir).iterdir())
    assert len(run_dirs) == 1
    error = json.loads((run_dirs[0] / "run-error.json").read_text(encoding="utf-8"))
    assert error["error_code"] == "face-quality"
    assert error["issues"] == ["face_too_small"]
    assert error["quality"]["face_width_px"] == "32.94400000"
    assert error["quality"]["face_height_px"] == "42.81800000"
    assert error["requirements"]["min_face_size_px"] == 64
    assert "original-resolution" in error["action"]
    assert error["search_credit_consumed"] is False


def test_zero_candidate_live_search_is_preserved_as_inconclusive(
    tmp_path: Path, monkeypatch
) -> None:
    input_image = tmp_path / "query.jpg"
    Image.new("RGB", (160, 160), color=(100, 110, 120)).save(input_image, "JPEG")

    class FakeBackend:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def encode_one(self, _image: Path) -> FaceEncoding:
            return _encoding((1.0, 0.0))

    search_run = SearchRun.create(
        provider="serpapi",
        search_id="lens-empty",
        search_ids=["lens-empty"],
        search_types=["all"],
        candidates=[],
        raw_response={
            "search_outcomes": {
                "all": {
                    "outcome": "soft-empty",
                    "provider_error": "Google Lens hasn't returned any results for this query.",
                }
            }
        },
        live=True,
        provider_mode="no-cache",
    )

    class FakeProvider:
        name = "serpapi"

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def search(self, _image: Path, *, focus_image_path: Path | None = None) -> SearchRun:
            assert focus_image_path is not None
            return search_run

    monkeypatch.setattr(pipeline_module, "OpenCVFaceBackend", FakeBackend)
    monkeypatch.setattr(
        pipeline_module,
        "verify_default_models",
        lambda _directory: {"yunet.onnx": "ok", "sface.onnx": "ok"},
    )
    monkeypatch.setattr(pipeline_module, "_make_provider", lambda **_kwargs: FakeProvider())

    with pytest.raises(InconclusiveError) as exc_info:
        run_pipeline(
            image_path=input_image,
            settings=_settings(tmp_path),
            consent_acknowledged=True,
            live=True,
            skip_anchor=True,
        )

    run_dir = exc_info.value.run_dir
    provider_record = json.loads(
        (run_dir / "search" / "provider-response.json").read_text(encoding="utf-8")
    )
    run_error = json.loads((run_dir / "run-error.json").read_text(encoding="utf-8"))
    assert provider_record["search_id"] == "lens-empty"
    assert provider_record["candidates"] == []
    assert provider_record["raw_response"]["search_outcomes"]["all"]["outcome"] == ("soft-empty")
    assert run_error["stage"] == "social-filter"


def test_bluesky_pipeline_runs_without_serpapi_and_never_uses_focus_upload(
    tmp_path: Path, monkeypatch
) -> None:
    input_image = tmp_path / "query.jpg"
    Image.new("RGB", (160, 160), color=(100, 110, 120)).save(input_image, "JPEG")

    class FakeBackend:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def encode_one(self, _image: Path) -> FaceEncoding:
            return _encoding((1.0, 0.0))

    search_run = SearchRun.create(
        provider="bluesky-public-api",
        search_id="bsky-empty",
        search_ids=["bsky-page-1"],
        search_types=["posts_with_media"],
        candidates=[],
        raw_response={
            "actor": "volunteer.bsky.social",
            "input_image": {"transmitted_to_bluesky": False},
        },
        live=True,
        provider_mode="public-no-auth-consented-account",
    )

    class FakeProvider:
        name = "bluesky-public-api"

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def search(self, _image: Path) -> SearchRun:
            return search_run

    observed_provider_args: dict[str, object] = {}

    def fake_make_provider(**kwargs: object) -> FakeProvider:
        observed_provider_args.update(kwargs)
        return FakeProvider()

    monkeypatch.setattr(pipeline_module, "OpenCVFaceBackend", FakeBackend)
    monkeypatch.setattr(
        pipeline_module,
        "verify_default_models",
        lambda _directory: {"yunet.onnx": "ok", "sface.onnx": "ok"},
    )
    monkeypatch.setattr(pipeline_module, "_make_provider", fake_make_provider)
    settings = replace(_settings(tmp_path), serpapi_api_key=None)

    with pytest.raises(InconclusiveError) as exc_info:
        run_pipeline(
            image_path=input_image,
            settings=settings,
            consent_acknowledged=True,
            live=True,
            skip_anchor=True,
            search_provider="bluesky",
            bluesky_actor="volunteer.bsky.social",
            search_mode="standard",
        )

    assert observed_provider_args["search_provider"] == "bluesky"
    assert observed_provider_args["bluesky_actor"] == "volunteer.bsky.social"
    provider_record = json.loads(
        (exc_info.value.run_dir / "search" / "provider-response.json").read_text(encoding="utf-8")
    )
    assert provider_record["provider"] == "bluesky-public-api"
    assert provider_record["raw_response"]["input_image"]["transmitted_to_bluesky"] is False


def test_pending_anchor_recovery_writes_receipt_and_removes_journal(
    tmp_path: Path, monkeypatch
) -> None:
    run_dir = tmp_path / "evidence" / "recoverable-run"
    run_dir.mkdir(parents=True)
    _write_sidecars(run_dir)
    contract = "0x" + "33" * 20
    transaction_hash = "0x" + "44" * 32
    submission = AnchorSubmission(
        schema="faceproof-anchor-submission/v1",
        chain_id=31337,
        contract_address=contract,
        commitment=json.loads((run_dir / "commitment.json").read_text())["commitment"],
        transaction_hash=transaction_hash,
        submitter="0x" + "55" * 20,
        nonce=7,
    )
    pending_path = anchor_submission_path(run_dir)
    pending_path.write_text(json.dumps(submission.to_dict()), encoding="utf-8")
    settings = replace(
        _settings(tmp_path),
        contract_address=contract,
        private_key="0x" + "11" * 32,
        contract_code_hash="0x" + "66" * 32,
    )
    receipt = AnchorReceipt(
        schema="faceproof-anchor-receipt/v1",
        chain_id=31337,
        rpc_network="local",
        contract_address=contract,
        commitment=submission.commitment,
        transaction_hash=transaction_hash,
        block_number=12,
        block_hash="0x" + "77" * 32,
        transaction_status=1,
        submitter=submission.submitter,
        chain_timestamp=1_700_000_000,
        confirmations_observed=2,
    )
    verification = ChainVerification(
        connected=True,
        chain_id_matches=True,
        anchored=True,
        commitment=submission.commitment,
        confirmations_satisfied=True,
        receipt_consistent=True,
    )
    observed: dict[str, object] = {}

    def fake_recover(commitment: str, **kwargs):
        observed["commitment"] = commitment
        observed.update(kwargs)
        return receipt

    monkeypatch.setattr(pipeline_module, "recover_anchor_receipt", fake_recover)
    monkeypatch.setattr(
        pipeline_module,
        "verify_commitment_on_chain",
        lambda *_args, **_kwargs: verification,
    )

    recovered = recover_pending_anchor(run_dir, settings=settings)

    assert recovered.receipt == receipt
    assert recovered.verification.passed
    assert observed["submission"] == submission.to_dict()
    assert not pending_path.exists()
    assert (
        json.loads((run_dir / "chain-receipt.json").read_text())["transaction_hash"]
        == transaction_hash
    )


def test_pending_anchor_recovery_retains_journal_while_rpc_is_ambiguous(
    tmp_path: Path, monkeypatch
) -> None:
    run_dir = tmp_path / "evidence" / "pending-run"
    run_dir.mkdir(parents=True)
    _write_sidecars(run_dir)
    commitment = json.loads((run_dir / "commitment.json").read_text())["commitment"]
    submission = AnchorSubmission(
        schema="faceproof-anchor-submission/v1",
        chain_id=31337,
        contract_address="0x" + "33" * 20,
        commitment=commitment,
        transaction_hash="0x" + "44" * 32,
        submitter="0x" + "55" * 20,
        nonce=7,
    )
    pending_path = anchor_submission_path(run_dir)
    pending_path.write_text(json.dumps(submission.to_dict()), encoding="utf-8")
    settings = replace(
        _settings(tmp_path),
        contract_address=submission.contract_address,
        private_key="0x" + "11" * 32,
        contract_code_hash="0x" + "66" * 32,
    )
    monkeypatch.setattr(
        pipeline_module,
        "recover_anchor_receipt",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ChainError("RPC unavailable")),
    )

    with pytest.raises(PipelineError, match="still pending"):
        recover_pending_anchor(run_dir, settings=settings)

    assert pending_path.is_file()
