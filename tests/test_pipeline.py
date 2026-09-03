from __future__ import annotations

import base64
import json
from dataclasses import replace
from pathlib import Path

import pytest
from PIL import Image

import faceproof.pipeline as pipeline_module
from faceproof.capture import PostCapture
from faceproof.chain import AnchorReceipt, AnchorSubmission, ChainError, ChainVerification
from faceproof.config import Settings
from faceproof.evidence import build_manifest, canonical_manifest_bytes, compute_commitment
from faceproof.face import (
    BoundingBox,
    FaceDetection,
    FaceEncoding,
    FaceQualityMetrics,
    ModelFingerprints,
)
from faceproof.pipeline import (
    PipelineError,
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
        run_id="test-run",
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
        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def search(self, _image: Path) -> SearchRun:
            return search_run

    def fake_capture(_candidate, destination: Path, **_kwargs) -> PostCapture:
        (destination / "capture_status.json").write_text(
            '{"synthetic_test_only":true}\n', encoding="utf-8"
        )
        return PostCapture(
            "captured",
            "synthetic-test-only",
            (),
            {"post_identity_verified": True},
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
        threshold=0.5,
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
    verified = verify_run(result.run_dir, settings=_settings(tmp_path), require_chain=False)
    assert verified.passed


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
