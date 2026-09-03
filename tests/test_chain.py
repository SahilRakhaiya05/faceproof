from __future__ import annotations

import copy
import time
from types import SimpleNamespace
from typing import Any

import pytest
from web3 import Web3

import faceproof.chain as chain
from faceproof.chain import (
    ChainError,
    bytes32_hex,
    explorer_transaction_url,
    parse_bytes32,
    registry_contract,
)

COMMITMENT = bytes(range(1, 33))
OTHER_COMMITMENT = bytes(range(33, 65))
TX_HASH = bytes.fromhex("11" * 32)
OTHER_TX_HASH = bytes.fromhex("12" * 32)
BLOCK_HASH = bytes.fromhex("22" * 32)
OTHER_BLOCK_HASH = bytes.fromhex("23" * 32)
CONTRACT_ADDRESS = Web3.to_checksum_address("0x" + "33" * 20)
OTHER_ADDRESS = Web3.to_checksum_address("0x" + "44" * 20)
SUBMITTER = Web3.to_checksum_address("0x" + "55" * 20)
BLOCK_NUMBER = 123_456
BLOCK_TIMESTAMP = 1_800_000_000
ANCHOR_CALLDATA = bytes(Web3.keccak(text="anchor(bytes32)")[:4]) + COMMITMENT


class _FakeCall:
    def __init__(self, value: Any) -> None:
        self.value = value

    def call(self) -> Any:
        return self.value


class _FakeFunctions:
    def __init__(self, state: dict[str, Any]) -> None:
        self.state = state

    def getRecord(self, commitment: bytes) -> _FakeCall:  # noqa: N802 - ABI name
        assert commitment == COMMITMENT
        return _FakeCall(self.state["record"])


class _FakeEventProcessor:
    def __init__(self, state: dict[str, Any]) -> None:
        self.state = state

    def process_receipt(self, _receipt: dict[str, Any]) -> list[dict[str, Any]]:
        return self.state["events"]


class _FakeEvents:
    def __init__(self, state: dict[str, Any]) -> None:
        self.state = state

    def EvidenceAnchored(self) -> _FakeEventProcessor:  # noqa: N802 - ABI name
        return _FakeEventProcessor(self.state)


class _FakeContract:
    def __init__(self, state: dict[str, Any]) -> None:
        self.address = CONTRACT_ADDRESS
        self.functions = _FakeFunctions(state)
        self.events = _FakeEvents(state)

    def encode_abi(self, name: str, *, args: list[bytes]) -> str:
        assert name == "anchor"
        assert args == [COMMITMENT]
        return "0x" + ANCHOR_CALLDATA.hex()


class _FakeEth:
    def __init__(self, state: dict[str, Any]) -> None:
        self.state = state

    @property
    def block_number(self) -> int:
        return self.state["head"]

    def get_transaction_receipt(self, _transaction_hash: bytes) -> dict[str, Any]:
        return self.state["receipt"]

    def get_transaction(self, _transaction_hash: bytes) -> dict[str, Any]:
        return self.state["transaction"]

    def get_block(self, _block_identifier: int) -> dict[str, Any]:
        return self.state["block"]


def _chain_fixture() -> tuple[Any, _FakeContract, dict[str, Any], dict[str, Any]]:
    receipt = {
        "transactionHash": TX_HASH,
        "status": 1,
        "blockNumber": BLOCK_NUMBER,
        "blockHash": BLOCK_HASH,
        "from": SUBMITTER,
        "to": CONTRACT_ADDRESS,
    }
    transaction = {
        "hash": TX_HASH,
        "blockNumber": BLOCK_NUMBER,
        "blockHash": BLOCK_HASH,
        "chainId": 84532,
        "from": SUBMITTER,
        "to": CONTRACT_ADDRESS,
        "value": 0,
        "input": ANCHOR_CALLDATA,
    }
    block = {
        "number": BLOCK_NUMBER,
        "hash": BLOCK_HASH,
        "timestamp": BLOCK_TIMESTAMP,
    }
    event = {
        "address": CONTRACT_ADDRESS,
        "transactionHash": TX_HASH,
        "blockNumber": BLOCK_NUMBER,
        "blockHash": BLOCK_HASH,
        "args": {
            "commitment": COMMITMENT,
            "submitter": SUBMITTER,
            "timestamp": BLOCK_TIMESTAMP,
            "blockNumber": BLOCK_NUMBER,
        },
    }
    state = {
        "receipt": receipt,
        "transaction": transaction,
        "block": block,
        "events": [event],
        "record": (SUBMITTER, BLOCK_TIMESTAMP, BLOCK_NUMBER),
        "head": BLOCK_NUMBER + 10,
    }
    saved = {
        "schema": chain.RECEIPT_SCHEMA,
        "chain_id": 84532,
        "rpc_network": "base-sepolia",
        "contract_address": CONTRACT_ADDRESS,
        "commitment": bytes32_hex(COMMITMENT),
        "transaction_hash": bytes32_hex(TX_HASH),
        "block_number": BLOCK_NUMBER,
        "block_hash": bytes32_hex(BLOCK_HASH),
        "transaction_status": 1,
        "submitter": SUBMITTER,
        "chain_timestamp": BLOCK_TIMESTAMP,
        "confirmations_observed": 1,
    }
    return SimpleNamespace(eth=_FakeEth(state)), _FakeContract(state), saved, state


def _verify_fixture(
    web3: Any,
    contract: _FakeContract,
    saved: dict[str, Any],
) -> bool | None:
    return chain._verify_saved_receipt(
        web3=web3,
        contract=contract,
        commitment=COMMITMENT,
        expected_chain_id=84532,
        saved=saved,
        expected_record=(SUBMITTER, BLOCK_TIMESTAMP, BLOCK_NUMBER),
    )


def test_bytes32_round_trip() -> None:
    encoded = bytes32_hex(COMMITMENT)
    assert encoded.startswith("0x")
    assert parse_bytes32(encoded) == COMMITMENT
    assert parse_bytes32(encoded[2:]) == COMMITMENT


@pytest.mark.parametrize(
    "value",
    [
        "00",
        "0x1234",
        b"short",
        "not-hex",
        bytes(32),
        "0x" + "00" * 32,
        bytearray(COMMITMENT),
        memoryview(COMMITMENT),
        32,
        True,
        None,
    ],
)
def test_invalid_or_zero_commitment_rejected(value: object) -> None:
    with pytest.raises(ChainError):
        parse_bytes32(value)  # type: ignore[arg-type]


def test_bytes32_hex_enforces_commitment_rules() -> None:
    with pytest.raises(ChainError):
        bytes32_hex(b"short")
    with pytest.raises(ChainError):
        bytes32_hex(bytes(32))


def test_base_sepolia_explorer_url() -> None:
    assert explorer_transaction_url(84532, "0xabc") == "https://sepolia.basescan.org/tx/0xabc"
    assert explorer_transaction_url(31337, "0xabc") is None


class _CodeEth:
    def __init__(self, code: bytes, contract: object) -> None:
        self.code = code
        self.contract_value = contract

    def get_code(self, _address: str) -> bytes:
        return self.code

    def contract(self, **_kwargs: Any) -> object:
        return self.contract_value


def test_registry_requires_code_and_optionally_checks_runtime_hash() -> None:
    expected_contract = object()
    code = b"\x60\x00\x60\x00"
    web3 = SimpleNamespace(eth=_CodeEth(code, expected_contract))
    code_hash = bytes(Web3.keccak(code))

    assert (
        registry_contract(
            web3,
            CONTRACT_ADDRESS,
            expected_code_hash=bytes32_hex(code_hash),
        )
        is expected_contract
    )

    with pytest.raises(ChainError, match="bytecode hash"):
        registry_contract(web3, CONTRACT_ADDRESS, expected_code_hash=OTHER_BLOCK_HASH)
    with pytest.raises(ChainError, match="no deployed contract code"):
        registry_contract(SimpleNamespace(eth=_CodeEth(b"", expected_contract)), CONTRACT_ADDRESS)


def test_rpc_connection_error_redacts_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        chain,
        "make_web3",
        lambda *_args, **_kwargs: SimpleNamespace(is_connected=lambda: False),
    )
    rpc_url = "https://user:password@rpc.example/v2/secret-token?apiKey=also-secret"

    with pytest.raises(ChainError) as raised:
        chain.anchor_commitment(
            COMMITMENT,
            rpc_url=rpc_url,
            expected_chain_id=84532,
            contract_address=CONTRACT_ADDRESS,
            private_key="not-reached",
        )

    message = str(raised.value)
    assert "https://rpc.example" in message
    for secret in ("user", "password", "secret-token", "also-secret", "apiKey"):
        assert secret not in message


def test_confirmation_timeout_is_a_failure() -> None:
    web3 = SimpleNamespace(eth=SimpleNamespace(block_number=BLOCK_NUMBER))
    with pytest.raises(ChainError, match="observed 1, required 2"):
        chain._wait_for_confirmations(
            web3,
            BLOCK_NUMBER,
            2,
            deadline=time.monotonic() - 1,
        )


def test_gas_limit_is_buffered_and_bounded() -> None:
    assert chain._bounded_gas_limit(50_000, chain.MAX_ANCHOR_GAS) == 80_000
    with pytest.raises(ChainError, match="Gas estimate exceeds"):
        chain._bounded_gas_limit(chain.MAX_ANCHOR_GAS, chain.MAX_ANCHOR_GAS)


def test_eip1559_fees_are_bounded() -> None:
    web3 = SimpleNamespace(eth=SimpleNamespace(max_priority_fee=1_000_000))
    assert chain._bounded_fee_fields(
        web3,
        {"baseFeePerGas": 10_000_000},
        fee_per_gas_cap=chain.MAX_FEE_PER_GAS_WEI,
        priority_fee_cap=chain.MAX_PRIORITY_FEE_PER_GAS_WEI,
    ) == {"maxPriorityFeePerGas": 1_000_000, "maxFeePerGas": 21_000_000}

    with pytest.raises(ChainError, match="fee estimate exceeds"):
        chain._bounded_fee_fields(
            web3,
            {"baseFeePerGas": chain.MAX_FEE_PER_GAS_WEI},
            fee_per_gas_cap=chain.MAX_FEE_PER_GAS_WEI,
            priority_fee_cap=chain.MAX_PRIORITY_FEE_PER_GAS_WEI,
        )


def test_legacy_gas_price_is_bounded() -> None:
    web3 = SimpleNamespace(eth=SimpleNamespace(gas_price=chain.MAX_FEE_PER_GAS_WEI + 1))
    with pytest.raises(ChainError, match="gas price exceeds"):
        chain._bounded_fee_fields(
            web3,
            {},
            fee_per_gas_cap=chain.MAX_FEE_PER_GAS_WEI,
            priority_fee_cap=chain.MAX_PRIORITY_FEE_PER_GAS_WEI,
        )


def test_complete_canonical_receipt_is_accepted() -> None:
    web3, contract, saved, _state = _chain_fixture()
    assert _verify_fixture(web3, contract, saved) is True


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("schema", "unknown-schema"),
        ("chain_id", 1),
        ("rpc_network", "chain-84532"),
        ("contract_address", OTHER_ADDRESS),
        ("commitment", bytes32_hex(OTHER_COMMITMENT)),
        ("transaction_hash", bytes32_hex(OTHER_TX_HASH)),
        ("block_number", BLOCK_NUMBER + 1),
        ("block_hash", bytes32_hex(OTHER_BLOCK_HASH)),
        ("transaction_status", 0),
        ("submitter", OTHER_ADDRESS),
        ("chain_timestamp", BLOCK_TIMESTAMP + 1),
        ("confirmations_observed", 0),
        ("confirmations_observed", 100),
    ],
)
def test_every_saved_receipt_field_is_checked(field: str, replacement: object) -> None:
    web3, contract, saved, _state = _chain_fixture()
    saved[field] = replacement
    assert _verify_fixture(web3, contract, saved) is False


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("hash", OTHER_TX_HASH),
        ("blockNumber", BLOCK_NUMBER + 1),
        ("blockHash", OTHER_BLOCK_HASH),
        ("chainId", 1),
        ("from", OTHER_ADDRESS),
        ("to", OTHER_ADDRESS),
        ("value", 1),
        ("input", b"\x12\x34"),
    ],
)
def test_transaction_is_bound_exactly(field: str, replacement: object) -> None:
    web3, contract, saved, state = _chain_fixture()
    state["transaction"][field] = replacement
    assert _verify_fixture(web3, contract, saved) is False


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("transactionHash", OTHER_TX_HASH),
        ("status", 0),
        ("blockNumber", BLOCK_NUMBER + 1),
        ("blockHash", OTHER_BLOCK_HASH),
        ("from", OTHER_ADDRESS),
        ("to", OTHER_ADDRESS),
    ],
)
def test_receipt_is_bound_exactly(field: str, replacement: object) -> None:
    web3, contract, saved, state = _chain_fixture()
    state["receipt"][field] = replacement
    assert _verify_fixture(web3, contract, saved) is False


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("address", OTHER_ADDRESS),
        ("transactionHash", OTHER_TX_HASH),
        ("blockNumber", BLOCK_NUMBER + 1),
        ("blockHash", OTHER_BLOCK_HASH),
    ],
)
def test_event_metadata_is_bound_exactly(field: str, replacement: object) -> None:
    web3, contract, saved, state = _chain_fixture()
    state["events"][0][field] = replacement
    assert _verify_fixture(web3, contract, saved) is False


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("commitment", OTHER_COMMITMENT),
        ("submitter", OTHER_ADDRESS),
        ("timestamp", BLOCK_TIMESTAMP + 1),
        ("blockNumber", BLOCK_NUMBER + 1),
    ],
)
def test_event_arguments_are_bound_exactly(field: str, replacement: object) -> None:
    web3, contract, saved, state = _chain_fixture()
    state["events"][0]["args"][field] = replacement
    assert _verify_fixture(web3, contract, saved) is False


def test_duplicate_matching_registry_event_is_rejected() -> None:
    web3, contract, saved, state = _chain_fixture()
    state["events"].append(copy.deepcopy(state["events"][0]))
    assert _verify_fixture(web3, contract, saved) is False


def test_noncanonical_block_is_rejected() -> None:
    web3, contract, saved, state = _chain_fixture()
    state["block"]["hash"] = OTHER_BLOCK_HASH
    assert _verify_fixture(web3, contract, saved) is False


def test_registry_record_must_match_transaction_event_and_block() -> None:
    web3, contract, saved, state = _chain_fixture()
    state["record"] = (OTHER_ADDRESS, BLOCK_TIMESTAMP, BLOCK_NUMBER)
    assert _verify_fixture(web3, contract, saved) is False
