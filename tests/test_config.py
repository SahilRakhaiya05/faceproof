from pathlib import Path

from faceproof.config import Settings


def test_settings_repr_redacts_secrets(tmp_path: Path) -> None:
    settings = Settings(
        serpapi_api_key="serp-secret",
        model_dir=tmp_path,
        output_dir=tmp_path,
        rpc_url="https://example.invalid",
        chain_id=1,
        contract_address=None,
        private_key="wallet-secret",
        confirmations=1,
        http_timeout_seconds=1,
    )

    rendered = repr(settings)
    assert "serp-secret" not in rendered
    assert "wallet-secret" not in rendered


def test_source_revision_and_code_hash_load_from_environment(monkeypatch) -> None:
    monkeypatch.setenv("FACEPROOF_SOURCE_REVISION", "abc123")
    monkeypatch.setenv("FACEPROOF_CONTRACT_CODE_HASH", "0x" + "11" * 32)

    settings = Settings.from_env()
    assert settings.source_revision == "abc123"
    assert settings.contract_code_hash == "0x" + "11" * 32
