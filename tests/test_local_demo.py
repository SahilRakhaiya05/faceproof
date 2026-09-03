from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner
from web3 import Web3

import faceproof.cli as cli_module
import faceproof.local_demo as demo
from faceproof.config import Settings


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        serpapi_api_key="free-plan-test-key",
        model_dir=tmp_path / "models",
        output_dir=tmp_path / "evidence",
        rpc_url="https://public-chain.invalid",
        chain_id=84532,
        contract_address=None,
        private_key=None,
        confirmations=3,
        http_timeout_seconds=30,
        contract_code_hash=None,
        source_revision=None,
    )


def test_find_foundry_tool_prefers_explicit_override(tmp_path: Path, monkeypatch) -> None:
    explicit = tmp_path / "custom" / "forge.exe"
    explicit.parent.mkdir()
    explicit.touch()
    monkeypatch.setattr(demo.shutil, "which", lambda _name: "C:/PATH/forge.exe")

    found = demo.find_foundry_tool(
        "forge",
        repository_root=tmp_path,
        environ={"FACEPROOF_FORGE_PATH": str(explicit)},
    )

    assert found == explicit.resolve()


def test_find_foundry_tool_uses_newest_bundled_version(tmp_path: Path, monkeypatch) -> None:
    older = tmp_path / ".tools" / "foundry" / "v1.2.0" / "anvil.exe"
    newer = tmp_path / ".tools" / "foundry" / "v1.10.0" / "anvil.exe"
    older.parent.mkdir(parents=True)
    newer.parent.mkdir(parents=True)
    older.touch()
    newer.touch()
    monkeypatch.setattr(demo.shutil, "which", lambda _name: None)

    found = demo.find_foundry_tool("anvil", repository_root=tmp_path, environ={})

    assert found == newer.resolve()


def test_local_runtime_injects_chain_only_in_memory_and_stops_anvil(
    tmp_path: Path, monkeypatch
) -> None:
    class FakeProcess:
        def __init__(self) -> None:
            self.returncode: int | None = None
            self.terminated = False

        def poll(self) -> int | None:
            return self.returncode

        def terminate(self) -> None:
            self.terminated = True
            self.returncode = 0

        def kill(self) -> None:
            self.returncode = -9

        def wait(self, timeout: float | None = None) -> int:
            assert timeout is not None
            return int(self.returncode or 0)

    process = FakeProcess()
    private_key = bytes.fromhex("01" * 32)
    account = SimpleNamespace(
        address=Web3.to_checksum_address("0x" + "12" * 20),
        key=private_key,
    )
    revision = "a" * 40
    contract = Web3.to_checksum_address("0x" + "34" * 20)
    code_hash = "0x" + "56" * 32
    base = _settings(tmp_path)
    environment_before = dict(os.environ)
    env_file = tmp_path / ".env"
    env_file.write_text("FACEPROOF_RPC_URL=https://keep.invalid\n", encoding="utf-8")
    env_before = env_file.read_bytes()
    chain_port = demo._available_port()
    web_port = demo._available_port()

    monkeypatch.setattr(demo, "_repository_root", lambda _explicit: tmp_path)
    monkeypatch.setattr(demo, "find_foundry_tool", lambda *_args, **_kwargs: tmp_path / "tool")
    monkeypatch.setattr(demo, "_compile_registry", lambda *_args: tmp_path / "artifact.json")
    monkeypatch.setattr(demo, "_current_clean_revision", lambda _root: revision)
    monkeypatch.setattr(demo, "_new_ephemeral_account", lambda: ("runtime words", account))
    monkeypatch.setattr(demo, "_start_anvil", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(demo, "_wait_for_anvil", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(demo, "_deploy_registry", lambda *_args: (contract, code_hash))

    with demo.local_demo_runtime(
        base,
        web_port=web_port,
        repository_root=tmp_path,
        chain_port=chain_port,
    ) as runtime:
        assert runtime.settings.rpc_url == f"http://127.0.0.1:{chain_port}"
        assert runtime.settings.chain_id == 31337
        assert runtime.settings.contract_address == contract
        assert runtime.settings.private_key == Web3.to_hex(private_key)
        assert runtime.settings.contract_code_hash == code_hash
        assert runtime.settings.source_revision == revision
        assert runtime.settings.serpapi_api_key == base.serpapi_api_key
        assert base.private_key is None
        assert env_file.read_bytes() == env_before
        assert dict(os.environ) == environment_before

    assert process.terminated is True
    assert env_file.read_bytes() == env_before


def test_local_runtime_stops_anvil_when_startup_fails(tmp_path: Path, monkeypatch) -> None:
    class FakeProcess:
        returncode: int | None = None
        terminated = False

        def poll(self) -> int | None:
            return self.returncode

        def terminate(self) -> None:
            self.terminated = True
            self.returncode = 0

        def kill(self) -> None:
            self.returncode = -9

        def wait(self, timeout: float | None = None) -> int:
            return int(self.returncode or 0)

    process = FakeProcess()
    account = SimpleNamespace(address="0x" + "12" * 20, key=bytes.fromhex("01" * 32))
    monkeypatch.setattr(demo, "_repository_root", lambda _explicit: tmp_path)
    monkeypatch.setattr(demo, "find_foundry_tool", lambda *_args, **_kwargs: tmp_path / "tool")
    monkeypatch.setattr(demo, "_compile_registry", lambda *_args: tmp_path / "artifact.json")
    monkeypatch.setattr(demo, "_current_clean_revision", lambda _root: "a" * 40)
    monkeypatch.setattr(demo, "_new_ephemeral_account", lambda: ("runtime words", account))
    monkeypatch.setattr(demo, "_start_anvil", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(
        demo,
        "_wait_for_anvil",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(demo.LocalDemoError("not ready")),
    )

    with (
        pytest.raises(demo.LocalDemoError, match="not ready"),
        demo.local_demo_runtime(
            _settings(tmp_path),
            web_port=demo._available_port(),
            repository_root=tmp_path,
            chain_port=demo._available_port(),
        ),
    ):
        raise AssertionError("runtime must not be yielded")

    assert process.terminated is True


def test_local_demo_cli_labels_ephemeral_semantics(tmp_path: Path, monkeypatch) -> None:
    observed: dict[str, object] = {}

    def fake_serve(settings: Settings, **kwargs) -> None:
        observed["settings"] = settings
        observed.update(kwargs)

    settings = _settings(tmp_path)
    monkeypatch.setattr(cli_module, "_settings", lambda: settings)
    monkeypatch.setattr(demo, "serve_local_demo", fake_serve)

    result = CliRunner().invoke(
        cli_module.app,
        ["local-demo", "--port", "8899", "--no-open-browser"],
    )

    assert result.exit_code == 0, result.output
    assert "ephemeral blockchain rehearsal" in result.output
    assert "not public-chain proof" in result.output
    assert "No private key" in result.output
    assert observed["settings"] is settings
    assert observed["port"] == 8899
    assert observed["open_browser"] is False
