from __future__ import annotations

import copy
import hashlib
import json
import sys
from collections import OrderedDict
from datetime import UTC, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from faceproof import evidence  # noqa: E402

FIXED_TIME = datetime(2026, 9, 3, 12, 30, tzinfo=UTC)
FIXED_SALT = bytes(range(32))


def _fallback_manifest(monkeypatch: pytest.MonkeyPatch) -> dict:
    monkeypatch.setattr(evidence, "_import_rfc8785", lambda: None)
    return evidence.build_manifest(
        {
            "post/page.html": b"<p>observed post</p>",
            "search/response.json": b'{"score":91}',
        },
        run_id="test-run-001",
        observed_at=FIXED_TIME,
        metadata={"provider": "test", "score_basis_points": 9100},
        media_types={
            "post/page.html": "text/html",
            "search/response.json": "application/json",
        },
        allow_non_rfc8785_fallback=True,
    )


def test_strict_rfc8785_failure_and_explicit_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(evidence, "_import_rfc8785", lambda: None)

    with pytest.raises(evidence.CanonicalizationUnavailable, match="rfc8785"):
        evidence.build_manifest(
            {"artifact.bin": b"evidence"},
            run_id="strict-run",
            observed_at=FIXED_TIME,
        )

    manifest = evidence.build_manifest(
        {"artifact.bin": b"evidence"},
        run_id="fallback-run",
        observed_at=FIXED_TIME,
        allow_non_rfc8785_fallback=True,
    )
    assert manifest["canonicalization"]["algorithm"] == evidence.DETERMINISTIC_JSON_V1

    with pytest.raises(evidence.CanonicalizationUnavailable, match="explicit"):
        evidence.canonical_manifest_bytes(manifest)


def test_rfc8785_is_selected_and_used_when_dependency_is_installed() -> None:
    implementation = evidence._import_rfc8785()
    if implementation is None:
        pytest.skip("rfc8785 is not installed in this interpreter")

    manifest = evidence.build_manifest(
        {"artifact.bin": b"evidence"},
        run_id="rfc8785-run",
        observed_at=FIXED_TIME,
        metadata={"similarity": 0.91},
    )
    canonical = evidence.canonical_manifest_bytes(manifest)
    reference = implementation.dumps(manifest)
    if isinstance(reference, str):
        reference = reference.encode("utf-8")

    assert manifest["canonicalization"]["algorithm"] == evidence.RFC8785
    assert canonical == reference


def test_manifest_is_deterministic_and_hashes_artifact_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(evidence, "_import_rfc8785", lambda: None)
    first_sources = OrderedDict([("z-last.bin", b"last"), ("a-first.bin", b"first")])
    second_sources = OrderedDict(reversed(list(first_sources.items())))
    options = {
        "run_id": "deterministic-run",
        "observed_at": "2026-09-03T18:00:00+05:30",
        "metadata": OrderedDict([("z", 2), ("a", 1)]),
        "allow_non_rfc8785_fallback": True,
    }

    first = evidence.build_manifest(first_sources, **options)
    second = evidence.build_manifest(second_sources, **options)
    first_bytes = evidence.canonical_manifest_bytes(first, allow_non_rfc8785_fallback=True)
    second_bytes = evidence.canonical_manifest_bytes(second, allow_non_rfc8785_fallback=True)

    assert first_bytes == second_bytes
    assert [item["path"] for item in first["artifacts"]] == [
        "a-first.bin",
        "z-last.bin",
    ]
    assert first["observed_at"] == "2026-09-03T12:30:00Z"
    assert first["artifacts"][0] == {
        "path": "a-first.bin",
        "size": 5,
        "sha256": hashlib.sha256(b"first").hexdigest(),
    }
    assert (
        evidence.manifest_sha256(first, allow_non_rfc8785_fallback=True)
        == hashlib.sha256(first_bytes).hexdigest()
    )


def test_fallback_rejects_float_instead_of_claiming_cross_runtime_stability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(evidence, "_import_rfc8785", lambda: None)
    with pytest.raises(evidence.CanonicalizationError, match="float"):
        evidence.build_manifest(
            {"artifact.bin": b"value"},
            run_id="float-run",
            observed_at=FIXED_TIME,
            metadata={"similarity": 0.91},
            allow_non_rfc8785_fallback=True,
        )


def test_receipt_is_forbidden_inside_pre_anchor_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(evidence, "_import_rfc8785", lambda: None)
    with pytest.raises(evidence.EvidenceError, match="outside the pre-anchor"):
        evidence.build_manifest(
            {"artifact.bin": b"value"},
            run_id="receipt-run",
            observed_at=FIXED_TIME,
            metadata={"blockchain": {"transaction_receipt": {"status": 1}}},
            allow_non_rfc8785_fallback=True,
        )

    manifest = _fallback_manifest(monkeypatch)
    assert "receipt" not in json.dumps(manifest).casefold()
    assert "salt" not in manifest
    assert "commitment" not in manifest


def test_commitment_matches_solidity_abi_bytes32_pair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    crypto_keccak = pytest.importorskip("Crypto.Hash.keccak")
    manifest = _fallback_manifest(monkeypatch)
    canonical = evidence.canonical_manifest_bytes(manifest, allow_non_rfc8785_fallback=True)
    manifest_digest = hashlib.sha256(canonical).digest()

    record = evidence.compute_commitment(
        manifest,
        salt=FIXED_SALT,
        allow_non_rfc8785_fallback=True,
    )
    reference = crypto_keccak.new(digest_bits=256)
    reference.update(manifest_digest + FIXED_SALT)

    assert record["scheme"] == evidence.COMMITMENT_SCHEME
    assert record["manifest_sha256"] == "0x" + manifest_digest.hex()
    assert record["salt"] == "0x" + FIXED_SALT.hex()
    assert record["commitment"] == "0x" + reference.hexdigest()
    assert record["abi_backend"] in {"eth_abi", "static-bytes32"}
    assert record["keccak_backend"] in {"eth_hash", "pycryptodome", "web3"}


def test_random_salt_changes_commitment(monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = _fallback_manifest(monkeypatch)
    first = evidence.compute_commitment(manifest, allow_non_rfc8785_fallback=True)
    second = evidence.compute_commitment(manifest, allow_non_rfc8785_fallback=True)
    assert first["salt"] != second["salt"]
    assert first["commitment"] != second["commitment"]


def test_verifier_detects_artifact_and_manifest_tampering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = _fallback_manifest(monkeypatch)
    proof = evidence.compute_commitment(
        manifest,
        salt=FIXED_SALT,
        allow_non_rfc8785_fallback=True,
    )
    original = {
        "post/page.html": b"<p>observed post</p>",
        "search/response.json": b'{"score":91}',
    }

    valid = evidence.verify_manifest(
        manifest,
        original,
        salt=proof["salt"],
        expected_commitment=proof["commitment"],
        allow_non_rfc8785_fallback=True,
    )
    assert valid["ok"] is True
    assert valid["artifacts_checked"] == 2
    assert valid["errors"] == []

    changed_artifact = dict(original)
    changed_artifact["post/page.html"] = b"<p>tampered post</p>"
    tampered = evidence.verify_manifest(
        manifest,
        changed_artifact,
        salt=proof["salt"],
        expected_commitment=proof["commitment"],
        allow_non_rfc8785_fallback=True,
    )
    assert tampered["ok"] is False
    assert any("SHA-256 mismatch" in error for error in tampered["errors"])

    changed_manifest = copy.deepcopy(manifest)
    changed_manifest["metadata"]["provider"] = "tampered-provider"
    changed = evidence.verify_manifest(
        changed_manifest,
        original,
        salt=proof["salt"],
        expected_commitment=proof["commitment"],
        allow_non_rfc8785_fallback=True,
    )
    assert changed["ok"] is False
    assert any("commitment" in error for error in changed["errors"])


def test_verification_from_directory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(evidence, "_import_rfc8785", lambda: None)
    nested = tmp_path / "capture"
    nested.mkdir()
    (nested / "page.html").write_bytes(b"captured")
    manifest = evidence.build_manifest(
        {"capture/page.html": nested / "page.html"},
        run_id="directory-run",
        observed_at=FIXED_TIME,
        allow_non_rfc8785_fallback=True,
    )
    result = evidence.verify_manifest(
        manifest,
        tmp_path,
        allow_non_rfc8785_fallback=True,
    )
    assert result["ok"] is True


def test_generated_manifest_validates_against_json_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jsonschema = pytest.importorskip("jsonschema")
    manifest = _fallback_manifest(monkeypatch)
    schema = json.loads(
        (ROOT / "schemas" / "evidence-manifest-v1.json").read_text(encoding="utf-8")
    )
    jsonschema.Draft202012Validator(schema).validate(manifest)

    invalid = copy.deepcopy(manifest)
    invalid["receipt"] = {"transactionHash": "0x00"}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(schema).validate(invalid)
