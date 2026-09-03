from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from typer.testing import CliRunner

import faceproof.cli as cli_module
from faceproof import __version__
from faceproof.pipeline import LocalVerificationResult, PipelineResult

runner = CliRunner()


def _input_file(tmp_path: Path) -> Path:
    image = tmp_path / "consented.jpg"
    image.write_bytes(b"isolated-cli-test")
    return image


def test_version_reports_package_version() -> None:
    result = runner.invoke(cli_module.app, ["version"])

    assert result.exit_code == 0
    assert f"FaceProof {__version__}" in result.output


def test_run_refuses_to_start_without_consent(tmp_path: Path, monkeypatch) -> None:
    settings_called = False

    def unexpected_settings() -> object:
        nonlocal settings_called
        settings_called = True
        raise AssertionError("settings must not be loaded before consent")

    monkeypatch.setattr(cli_module, "_settings", unexpected_settings)
    monkeypatch.setattr(
        cli_module,
        "run_pipeline",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("pipeline must not run")),
    )

    result = runner.invoke(
        cli_module.app,
        ["run", "--image", str(_input_file(tmp_path)), "--live"],
    )

    assert result.exit_code == 2
    assert "Stopped:" in result.output
    assert "explicitly authorized" in result.output
    assert settings_called is False


def test_scan_refuses_untrusted_model_files(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        cli_module,
        "_settings",
        lambda: SimpleNamespace(
            model_dir=tmp_path / "models",
            yunet_model=tmp_path / "models" / "yunet.onnx",
            sface_model=tmp_path / "models" / "sface.onnx",
        ),
    )
    monkeypatch.setattr(
        cli_module,
        "verify_default_models",
        lambda _directory: {"sface.onnx": "SHA-256 mismatch"},
    )
    monkeypatch.setattr(
        cli_module,
        "OpenCVFaceBackend",
        lambda *_args: (_ for _ in ()).throw(AssertionError("backend must not load")),
    )

    result = runner.invoke(
        cli_module.app,
        ["scan", "--image", str(_input_file(tmp_path)), "--i-have-consent"],
    )

    assert result.exit_code == 2
    assert "model integrity" in result.output


def test_successful_unanchored_run_renders_release_evidence(tmp_path: Path, monkeypatch) -> None:
    run_dir = tmp_path / "evidence" / "run-123"
    run_dir.mkdir(parents=True)
    expected = PipelineResult(
        run_id="run-123",
        run_dir=run_dir,
        provider="serpapi",
        search_id="lens-live-123",
        selected_url="https://x.com/volunteer/status/42",
        local_similarity=0.812345,
        manifest_sha256="0x" + "11" * 32,
        commitment="0x" + "22" * 32,
        chain_receipt=None,
        chain_verification=None,
    )
    observed: dict[str, Any] = {}

    def fake_run_pipeline(**kwargs: Any) -> PipelineResult:
        observed.update(kwargs)
        return expected

    monkeypatch.setattr(cli_module, "_settings", lambda: object())
    monkeypatch.setattr(cli_module, "run_pipeline", fake_run_pipeline)

    result = runner.invoke(
        cli_module.app,
        [
            "run",
            "--image",
            str(_input_file(tmp_path)),
            "--live",
            "--i-have-consent",
            "--skip-anchor",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "lens-live-123" in result.output
    assert "https://x.com/volunteer/status/42" in result.output
    assert "0.812345" in result.output
    assert "SKIPPED" in result.output
    assert "development run only" in result.output
    assert observed["live"] is True
    assert observed["consent_acknowledged"] is True
    assert observed["skip_anchor"] is True


def test_verify_pass_renders_verified_verdict(tmp_path: Path, monkeypatch) -> None:
    run_dir = tmp_path / "evidence"
    run_dir.mkdir()
    observed: dict[str, Any] = {}
    verification = LocalVerificationResult(
        evidence_ok=True,
        evidence_detail={"ok": True, "errors": []},
        canonical_sidecar_ok=True,
        chain=None,
        external_anchor_ok=None,
    )

    def fake_verify_run(path: Path, **kwargs: Any) -> LocalVerificationResult:
        observed["path"] = path
        observed.update(kwargs)
        return verification

    monkeypatch.setattr(cli_module, "_settings", lambda: object())
    monkeypatch.setattr(cli_module, "verify_run", fake_verify_run)

    result = runner.invoke(
        cli_module.app,
        ["verify", str(run_dir), "--allow-unanchored"],
    )

    assert result.exit_code == 0, result.output
    assert "Artifact hashes" in result.output
    assert "On-chain anchor" in result.output
    assert "NOT CHECKED" in result.output
    assert "VERIFIED: exact evidence matches the saved commitment" in result.output
    assert observed["path"] == run_dir
    assert observed["require_chain"] is False


def test_verify_fail_exits_nonzero_and_prints_integrity_error(tmp_path: Path, monkeypatch) -> None:
    run_dir = tmp_path / "evidence"
    run_dir.mkdir()
    verification = LocalVerificationResult(
        evidence_ok=False,
        evidence_detail={"ok": False, "errors": ["artifact SHA-256 mismatch: post.html"]},
        canonical_sidecar_ok=True,
        chain=None,
        external_anchor_ok=None,
    )
    monkeypatch.setattr(cli_module, "_settings", lambda: object())
    monkeypatch.setattr(cli_module, "verify_run", lambda *_args, **_kwargs: verification)

    result = runner.invoke(
        cli_module.app,
        ["verify", str(run_dir), "--allow-unanchored"],
    )

    assert result.exit_code == 1
    assert "artifact SHA-256 mismatch: post.html" in result.output
    assert "VERIFICATION FAILED" in result.output


def test_tamper_demo_reports_expected_detection(tmp_path: Path, monkeypatch) -> None:
    run_dir = tmp_path / "evidence"
    run_dir.mkdir()
    monkeypatch.setattr(
        cli_module,
        "run_tamper_demo",
        lambda _path: ("search/provider-response.json", {"ok": False, "errors": ["mismatch"]}),
    )

    result = runner.invoke(cli_module.app, ["tamper-demo", str(run_dir)])

    assert result.exit_code == 0, result.output
    assert "search/provider-response.json" in result.output
    assert "EXPECTED FAILURE: tampering was detected" in result.output
    assert "mismatch" in result.output


def test_tamper_demo_rejects_an_unexpected_pass(tmp_path: Path, monkeypatch) -> None:
    run_dir = tmp_path / "evidence"
    run_dir.mkdir()
    monkeypatch.setattr(
        cli_module,
        "run_tamper_demo",
        lambda _path: ("search/provider-response.json", {"ok": True, "errors": []}),
    )

    result = runner.invoke(cli_module.app, ["tamper-demo", str(run_dir)])

    assert result.exit_code == 1
    assert "UNEXPECTED PASS: tampering was not detected" in result.output


def test_doctor_demo_reports_ready_without_network(tmp_path: Path, monkeypatch) -> None:
    private_key = "private-test-value"
    settings = SimpleNamespace(
        model_dir=tmp_path / "models",
        serpapi_api_key="serpapi-test-value",
        require_serpapi_key=lambda: "serpapi-test-value",
        http_timeout_seconds=30,
        contract_address="0x0000000000000000000000000000000000001234",
        private_key=private_key,
        contract_code_hash="0x" + "ab" * 32,
        rpc_url="https://rpc.invalid/private-path",
        chain_id=84532,
        source_revision="a" * 40,
    )

    class FakeAccountAPI:
        @staticmethod
        def from_key(value: str) -> SimpleNamespace:
            assert value == private_key
            return SimpleNamespace(address="0x0000000000000000000000000000000000005678")

    class FakeEth:
        chain_id = 84532
        account = FakeAccountAPI()

        @staticmethod
        def get_balance(_address: str) -> int:
            return 1

    fake_web3 = SimpleNamespace(is_connected=lambda: True, eth=FakeEth())
    registry_checks: list[tuple[object, str, str]] = []

    def fake_registry_contract(web3: object, address: str, *, expected_code_hash: str) -> object:
        registry_checks.append((web3, address, expected_code_hash))
        return object()

    monkeypatch.setattr(cli_module, "_settings", lambda: settings)
    monkeypatch.setattr(
        cli_module,
        "verify_default_models",
        lambda _directory: {"yunet.onnx": "ok", "sface.onnx": "ok"},
    )
    monkeypatch.setattr(cli_module, "make_web3", lambda *_args, **_kwargs: fake_web3)
    monkeypatch.setattr(
        cli_module,
        "check_serpapi_account",
        lambda *_args, **_kwargs: SimpleNamespace(
            ready=True,
            plan_name="Free",
            searches_left=250,
            searches_per_month=250,
        ),
    )
    monkeypatch.setattr(cli_module, "registry_contract", fake_registry_contract)
    monkeypatch.setattr(
        cli_module,
        "verify_git_source_revision",
        lambda revision: SimpleNamespace(revision=revision),
    )

    result = runner.invoke(cli_module.app, ["doctor", "--demo"])

    assert result.exit_code == 0, result.output
    assert "YuNet + SFace" in result.output
    assert "Registry code pin" in result.output
    assert "wallet funded" in result.output
    assert "Core face/search environment is ready" in result.output
    assert private_key not in result.output
    assert registry_checks == [(fake_web3, settings.contract_address, settings.contract_code_hash)]
