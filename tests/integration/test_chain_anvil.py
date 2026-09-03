"""Opt-in deploy/anchor/verify integration test against a local Anvil node.

Set ``FACEPROOF_RUN_ANVIL_INTEGRATION=1`` and ensure ``forge`` and ``anvil``
are on PATH. The test never connects to a public RPC endpoint.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import socket
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from eth_account import Account
from web3 import Web3

from faceproof.chain import (
    ChainError,
    anchor_commitment,
    verify_commitment_on_chain,
)
from faceproof.config import Settings
from faceproof.evidence import build_manifest, canonical_manifest_bytes, compute_commitment
from faceproof.pipeline import verify_run

if os.getenv("FACEPROOF_RUN_ANVIL_INTEGRATION") != "1":
    pytest.skip(
        "set FACEPROOF_RUN_ANVIL_INTEGRATION=1 to run the local-EVM integration test",
        allow_module_level=True,
    )


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONTRACTS_ROOT = PROJECT_ROOT / "contracts"
ARTIFACT = CONTRACTS_ROOT / "out" / "EvidenceRegistry.sol" / "EvidenceRegistry.json"
CHAIN_ID = 31_337
MNEMONIC = "test test test test test test test test test test test junk"


def _required_tool(name: str) -> str:
    executable = shutil.which(name)
    if executable is None:
        pytest.fail(f"{name} is required when FACEPROOF_RUN_ANVIL_INTEGRATION=1")
    return executable


def _free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _compile_contract(forge: str) -> tuple[list[dict[str, object]], str]:
    completed = subprocess.run(
        [forge, "build"],
        cwd=CONTRACTS_ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    if completed.returncode != 0:
        pytest.fail(f"forge build failed:\n{completed.stdout}\n{completed.stderr}")
    artifact = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    return artifact["abi"], artifact["bytecode"]["object"]


@contextmanager
def _local_anvil(anvil: str) -> Iterator[tuple[Web3, str]]:
    port = _free_local_port()
    rpc_url = f"http://127.0.0.1:{port}"
    creation_flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    process = subprocess.Popen(
        [
            anvil,
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--chain-id",
            str(CHAIN_ID),
            "--mnemonic",
            MNEMONIC,
            "--silent",
        ],
        cwd=PROJECT_ROOT,
        # Anvil can emit enough Unicode diagnostics on Windows to fill an
        # unread pipe and block the JSON-RPC server. The integration result is
        # captured by pytest, so discard node chatter instead.
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creation_flags,
    )
    web3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 2}))
    deadline = time.monotonic() + 15
    try:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                pytest.fail(f"anvil exited during startup with code {process.returncode}")
            try:
                if web3.is_connected() and int(web3.eth.chain_id) == CHAIN_ID:
                    break
            except Exception:
                pass
            time.sleep(0.1)
        else:
            pytest.fail("anvil did not become ready within 15 seconds")
        yield web3, rpc_url
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def test_deploy_anchor_fresh_verify_and_tamper_rejection(tmp_path: Path) -> None:
    forge = _required_tool("forge")
    anvil = _required_tool("anvil")
    abi, bytecode = _compile_contract(forge)

    with _local_anvil(anvil) as (deployment_web3, rpc_url):
        # Derive the disposable key at runtime so no private-key literal is
        # stored in the repository. This mnemonic is Anvil test data only.
        Account.enable_unaudited_hdwallet_features()
        deployer = Account.from_mnemonic(MNEMONIC)
        local_private_key = Web3.to_hex(deployer.key)
        assert deployer.address == deployment_web3.eth.accounts[0]

        factory = deployment_web3.eth.contract(abi=abi, bytecode=bytecode)
        deployment_hash = factory.constructor().transact({"from": deployer.address})
        deployment_receipt = deployment_web3.eth.wait_for_transaction_receipt(
            deployment_hash, timeout=15
        )
        assert int(deployment_receipt["status"]) == 1
        contract_address = deployment_receipt["contractAddress"]
        runtime_code = bytes(deployment_web3.eth.get_code(contract_address))
        assert runtime_code
        expected_code_hash = bytes(Web3.keccak(runtime_code))

        run_dir = tmp_path / "evidence-run"
        artifact = run_dir / "search" / "provider-response.json"
        artifact.parent.mkdir(parents=True)
        artifact.write_bytes(b'{"provider":"synthetic-local-integration"}\n')
        manifest = build_manifest(
            {"search/provider-response.json": artifact},
            run_id="local-evm-integration",
            observed_at="2026-09-03T00:00:00Z",
            metadata={"scope": "offline integration; not a live provider claim"},
        )
        commitment_record = compute_commitment(manifest, salt=b"\x7b" * 32)
        (run_dir / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        (run_dir / "manifest.canonical.json").write_bytes(canonical_manifest_bytes(manifest))
        (run_dir / "commitment.json").write_text(
            json.dumps(commitment_record, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        commitment = bytes.fromhex(commitment_record["commitment"][2:])
        anchor_receipt = anchor_commitment(
            commitment,
            rpc_url=rpc_url,
            expected_chain_id=CHAIN_ID,
            contract_address=contract_address,
            private_key=local_private_key,
            confirmations=1,
            timeout_seconds=15,
            expected_code_hash=expected_code_hash,
        )
        saved_receipt = anchor_receipt.to_dict()
        (run_dir / "chain-receipt.json").write_text(
            json.dumps(saved_receipt, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        settings = Settings(
            serpapi_api_key=None,
            model_dir=tmp_path / "models",
            output_dir=tmp_path / "unused-output",
            rpc_url=rpc_url,
            chain_id=CHAIN_ID,
            contract_address=contract_address,
            private_key=None,
            confirmations=1,
            http_timeout_seconds=15,
            contract_code_hash=Web3.to_hex(expected_code_hash),
        )
        bundle_verification = verify_run(
            run_dir,
            settings=settings,
            expected_commitment=commitment_record["commitment"],
            expected_transaction_hash=anchor_receipt.transaction_hash,
        )
        assert bundle_verification.passed
        assert bundle_verification.evidence_ok
        assert bundle_verification.chain is not None
        assert bundle_verification.chain.passed

        # This call constructs a fresh HTTPProvider/Web3 instance internally.
        verified = verify_commitment_on_chain(
            commitment,
            rpc_url=rpc_url,
            expected_chain_id=CHAIN_ID,
            contract_address=contract_address,
            receipt=saved_receipt,
            timeout_seconds=15,
            expected_code_hash=expected_code_hash,
        )
        assert verified.passed
        assert verified.anchored
        assert verified.receipt_consistent is True
        assert verified.submitter == deployer.address
        assert verified.block_number == anchor_receipt.block_number
        assert verified.chain_timestamp == anchor_receipt.chain_timestamp

        fresh_contract = deployment_web3.eth.contract(address=contract_address, abi=abi)
        assert fresh_contract.functions.verify(commitment).call() is True
        submitter, timestamp, block_number = fresh_contract.functions.getRecord(commitment).call()
        assert submitter == deployer.address
        assert int(timestamp) == anchor_receipt.chain_timestamp
        assert int(block_number) == anchor_receipt.block_number

        tampered_receipt = copy.deepcopy(saved_receipt)
        tampered_receipt["chain_timestamp"] += 1
        tampered = verify_commitment_on_chain(
            commitment,
            rpc_url=rpc_url,
            expected_chain_id=CHAIN_ID,
            contract_address=contract_address,
            receipt=tampered_receipt,
            timeout_seconds=15,
            expected_code_hash=expected_code_hash,
        )
        assert tampered.anchored
        assert tampered.receipt_consistent is False
        assert not tampered.passed

        absent_commitment = bytes(Web3.keccak(text="faceproof-not-anchored"))
        absent = verify_commitment_on_chain(
            absent_commitment,
            rpc_url=rpc_url,
            expected_chain_id=CHAIN_ID,
            contract_address=contract_address,
            timeout_seconds=15,
            expected_code_hash=expected_code_hash,
        )
        assert not absent.anchored
        assert not absent.passed

        with pytest.raises(ChainError, match="already anchored"):
            anchor_commitment(
                commitment,
                rpc_url=rpc_url,
                expected_chain_id=CHAIN_ID,
                contract_address=contract_address,
                private_key=local_private_key,
                timeout_seconds=15,
                expected_code_hash=expected_code_hash,
            )
        with pytest.raises(ChainError, match="must not be zero"):
            anchor_commitment(
                bytes(32),
                rpc_url=rpc_url,
                expected_chain_id=CHAIN_ID,
                contract_address=contract_address,
                private_key=local_private_key,
                timeout_seconds=15,
            )
        with pytest.raises(ChainError, match="bytecode hash"):
            verify_commitment_on_chain(
                commitment,
                rpc_url=rpc_url,
                expected_chain_id=CHAIN_ID,
                contract_address=contract_address,
                timeout_seconds=15,
                expected_code_hash=bytes(Web3.keccak(text="wrong-runtime-code")),
            )

        artifact.write_bytes(b'{"provider":"tampered-after-anchor"}\n')
        tampered_bundle = verify_run(run_dir, settings=settings)
        assert not tampered_bundle.evidence_ok
        assert not tampered_bundle.passed
        assert tampered_bundle.chain is not None
        assert tampered_bundle.chain.passed
