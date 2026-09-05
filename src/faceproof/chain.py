from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import urlsplit

from web3 import Web3
from web3.contract import Contract
from web3.exceptions import TimeExhausted

EVIDENCE_REGISTRY_ABI: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "anchor",
        "stateMutability": "nonpayable",
        "inputs": [{"name": "commitment", "type": "bytes32"}],
        "outputs": [],
    },
    {
        "type": "function",
        "name": "verify",
        "stateMutability": "view",
        "inputs": [{"name": "commitment", "type": "bytes32"}],
        "outputs": [{"name": "", "type": "bool"}],
    },
    {
        "type": "function",
        "name": "getRecord",
        "stateMutability": "view",
        "inputs": [{"name": "commitment", "type": "bytes32"}],
        "outputs": [
            {
                "name": "",
                "type": "tuple",
                "components": [
                    {"name": "submitter", "type": "address"},
                    {"name": "timestamp", "type": "uint256"},
                    {"name": "blockNumber", "type": "uint256"},
                ],
            }
        ],
    },
    {
        "type": "event",
        "name": "EvidenceAnchored",
        "anonymous": False,
        "inputs": [
            {"indexed": True, "name": "commitment", "type": "bytes32"},
            {"indexed": True, "name": "submitter", "type": "address"},
            {"indexed": False, "name": "timestamp", "type": "uint256"},
            {"indexed": False, "name": "blockNumber", "type": "uint256"},
        ],
    },
]

RECEIPT_SCHEMA = "faceproof-chain-receipt-v1"
SUBMISSION_SCHEMA = "faceproof-chain-submission-v1"
MAX_ANCHOR_GAS = 250_000
MAX_FEE_PER_GAS_WEI = 100_000_000_000  # 100 gwei
MAX_PRIORITY_FEE_PER_GAS_WEI = 5_000_000_000  # 5 gwei
MAX_TOTAL_ANCHOR_FEE_WEI = 25_000_000_000_000_000  # 0.025 ETH
_FALLBACK_PRIORITY_FEE_WEI = 1_000_000  # 0.001 gwei


class ChainError(RuntimeError):
    """Raised when an evidence anchor cannot be written or independently read."""


@dataclass(frozen=True, slots=True)
class AnchorSubmission:
    """Non-secret state needed to recover one exact signed transaction."""

    schema: str
    chain_id: int
    contract_address: str
    commitment: str
    transaction_hash: str
    submitter: str
    nonce: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class AnchorPendingError(ChainError):
    """Raised after signing when callers must recover instead of rebroadcasting."""

    def __init__(self, message: str, submission: AnchorSubmission) -> None:
        self.submission = submission
        super().__init__(
            f"{message}; recover signed transaction {submission.transaction_hash} "
            "without rebroadcasting"
        )


@dataclass(frozen=True, slots=True)
class AnchorReceipt:
    schema: str
    chain_id: int
    rpc_network: str
    contract_address: str
    commitment: str
    transaction_hash: str
    block_number: int
    block_hash: str
    transaction_status: int
    submitter: str
    chain_timestamp: int
    confirmations_observed: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ChainVerification:
    connected: bool
    chain_id_matches: bool
    anchored: bool
    commitment: str
    submitter: str | None = None
    chain_timestamp: int | None = None
    block_number: int | None = None
    confirmations_observed: int | None = None
    confirmations_required: int = 1
    confirmations_satisfied: bool | None = None
    receipt_consistent: bool | None = None
    detail: str = ""

    @property
    def passed(self) -> bool:
        receipt_ok = self.receipt_consistent is not False
        confirmations_ok = self.confirmations_satisfied is True
        return (
            self.connected
            and self.chain_id_matches
            and self.anchored
            and confirmations_ok
            and receipt_ok
        )

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "passed": self.passed}


@dataclass(frozen=True, slots=True)
class _CanonicalAnchor:
    transaction_hash: bytes
    block_number: int
    block_hash: bytes
    status: int
    submitter: str
    timestamp: int


def make_web3(rpc_url: str, *, timeout_seconds: float = 30) -> Web3:
    return Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": timeout_seconds}))


def registry_contract(
    web3: Web3,
    address: str,
    *,
    expected_code_hash: str | bytes | None = None,
) -> Contract:
    try:
        checksum = Web3.to_checksum_address(address)
    except (TypeError, ValueError) as exc:
        raise ChainError("Invalid registry contract address") from exc

    try:
        code = bytes(web3.eth.get_code(checksum))
    except Exception as exc:
        raise ChainError(f"Could not read registry bytecode ({type(exc).__name__})") from exc
    if not code:
        raise ChainError("Registry address has no deployed contract code")

    if expected_code_hash is not None:
        expected = _parse_bytes32(expected_code_hash, field="Expected code hash")
        actual = bytes(Web3.keccak(code))
        if actual != expected:
            raise ChainError("Registry runtime bytecode hash does not match the trusted value")

    return web3.eth.contract(address=checksum, abi=EVIDENCE_REGISTRY_ABI)


def anchor_commitment(
    commitment: str | bytes,
    *,
    rpc_url: str,
    expected_chain_id: int,
    contract_address: str,
    private_key: str,
    confirmations: int = 1,
    timeout_seconds: float = 120,
    expected_code_hash: str | bytes | None = None,
    gas_limit_cap: int = MAX_ANCHOR_GAS,
    fee_per_gas_cap: int = MAX_FEE_PER_GAS_WEI,
    priority_fee_cap: int = MAX_PRIORITY_FEE_PER_GAS_WEI,
    on_submission: Callable[[AnchorSubmission], None] | None = None,
) -> AnchorReceipt:
    """Sign, persist recoverable metadata, broadcast, and verify an anchor.

    ``on_submission`` runs after signing but before broadcast. A caller can use
    it to atomically persist :class:`AnchorSubmission`; if persistence fails,
    no transaction is sent. After an ambiguous broadcast, receipt timeout, or
    confirmation timeout, :class:`AnchorPendingError` exposes the same state so
    the exact transaction can be recovered with :func:`recover_anchor_receipt`.
    """

    commitment_bytes = parse_bytes32(commitment)
    _require_positive_int(expected_chain_id, "expected_chain_id")
    _require_positive_int(confirmations, "confirmations")
    _require_positive_number(timeout_seconds, "timeout_seconds")
    _validate_fee_caps(gas_limit_cap, fee_per_gas_cap, priority_fee_cap)

    rpc_label = _redacted_rpc_url(rpc_url)
    try:
        web3 = make_web3(rpc_url, timeout_seconds=min(timeout_seconds, 30))
        connected = web3.is_connected()
        actual_chain_id = int(web3.eth.chain_id) if connected else 0
    except Exception as exc:
        raise ChainError(f"Could not connect to RPC {rpc_label} ({type(exc).__name__})") from exc
    if not connected:
        raise ChainError(f"Could not connect to RPC {rpc_label}")
    if actual_chain_id != expected_chain_id:
        raise ChainError(
            f"Wrong chain: RPC returned {actual_chain_id}, expected {expected_chain_id}"
        )

    try:
        account = web3.eth.account.from_key(private_key)
    except Exception as exc:  # eth-account exposes several key parsing exceptions.
        raise ChainError("FACEPROOF_PRIVATE_KEY is not a valid EVM private key") from exc
    contract = registry_contract(web3, contract_address, expected_code_hash=expected_code_hash)

    try:
        if contract.functions.verify(commitment_bytes).call():
            raise ChainError("This commitment is already anchored")
        nonce = int(web3.eth.get_transaction_count(account.address, "pending"))
        latest = web3.eth.get_block("latest")
        fee_fields = _bounded_fee_fields(
            web3,
            latest,
            fee_per_gas_cap=fee_per_gas_cap,
            priority_fee_cap=priority_fee_cap,
        )
    except ChainError:
        raise
    except Exception as exc:
        raise ChainError(
            f"Could not prepare anchor transaction via {rpc_label} ({type(exc).__name__})"
        ) from exc

    transaction: dict[str, Any] = {
        "from": account.address,
        "nonce": nonce,
        "chainId": actual_chain_id,
        "value": 0,
        **fee_fields,
    }

    try:
        estimate = contract.functions.anchor(commitment_bytes).estimate_gas(transaction)
        transaction["gas"] = _bounded_gas_limit(estimate, gas_limit_cap)
        effective_fee = int(transaction.get("maxFeePerGas", transaction.get("gasPrice", 0)))
        if transaction["gas"] * effective_fee > MAX_TOTAL_ANCHOR_FEE_WEI:
            raise ChainError("Anchor transaction exceeds the maximum total fee")
        built = contract.functions.anchor(commitment_bytes).build_transaction(transaction)
        _validate_built_anchor_transaction(
            built,
            contract=contract,
            commitment=commitment_bytes,
            sender=account.address,
            chain_id=actual_chain_id,
            nonce=nonce,
            gas=int(transaction["gas"]),
            fee_fields=fee_fields,
        )
        signed = account.sign_transaction(built)
        raw_transaction = getattr(signed, "raw_transaction", None) or signed.rawTransaction
        signed_hash = _parse_bytes32(signed.hash, field="Signed transaction hash")
        raw_hash = bytes(Web3.keccak(bytes(raw_transaction)))
        if raw_hash != signed_hash:
            raise ChainError(
                "Signed transaction hash does not match the serialized transaction bytes"
            )
        submission = AnchorSubmission(
            schema=SUBMISSION_SCHEMA,
            chain_id=actual_chain_id,
            contract_address=contract.address,
            commitment=bytes32_hex(commitment_bytes),
            transaction_hash=bytes32_hex(raw_hash),
            submitter=Web3.to_checksum_address(account.address),
            nonce=nonce,
        )
    except ChainError:
        raise
    except Exception as exc:
        raise ChainError(
            f"Could not build and sign anchor transaction via {rpc_label} ({type(exc).__name__})"
        ) from exc

    if on_submission is not None:
        try:
            on_submission(submission)
        except Exception as exc:
            raise ChainError(
                "Could not persist recoverable anchor state "
                f"({type(exc).__name__}); transaction was not broadcast"
            ) from None

    try:
        returned_hash = web3.eth.send_raw_transaction(raw_transaction)
    except Exception as exc:
        raise AnchorPendingError(
            f"Anchor broadcast outcome is unknown via {rpc_label} ({type(exc).__name__})",
            submission,
        ) from None
    try:
        returned_hash_bytes = _parse_bytes32(returned_hash, field="RPC transaction hash")
    except ChainError:
        raise AnchorPendingError("RPC returned a malformed transaction hash", submission) from None
    if returned_hash_bytes != raw_hash:
        raise AnchorPendingError(
            "RPC returned a transaction hash different from the signed payload",
            submission,
        )

    return _wait_and_finalize_submission(
        web3=web3,
        contract=contract,
        commitment=commitment_bytes,
        submission=submission,
        confirmations=confirmations,
        timeout_seconds=timeout_seconds,
    )


def recover_anchor_receipt(
    commitment: str | bytes,
    *,
    submission: AnchorSubmission | Mapping[str, Any],
    rpc_url: str,
    expected_chain_id: int,
    contract_address: str,
    expected_submitter: str,
    confirmations: int = 1,
    timeout_seconds: float = 120,
    expected_code_hash: str | bytes | None = None,
) -> AnchorReceipt:
    """Recover and verify one exact previously signed transaction without sending.

    ``expected_submitter`` must come from trusted configuration, rather than the
    recoverable state itself. Recovery binds the saved transaction hash, nonce,
    signer, chain, registry, commitment, calldata, receipt, event, and registry
    record. It never calls ``send_raw_transaction`` and never falls back to an
    arbitrary transaction merely because the commitment exists in registry
    state.
    """

    commitment_bytes = parse_bytes32(commitment)
    _require_positive_int(expected_chain_id, "expected_chain_id")
    _require_positive_int(confirmations, "confirmations")
    _require_positive_number(timeout_seconds, "timeout_seconds")
    recovered = _validated_submission(
        submission,
        commitment=commitment_bytes,
        expected_chain_id=expected_chain_id,
        contract_address=contract_address,
        expected_submitter=expected_submitter,
    )

    rpc_label = _redacted_rpc_url(rpc_url)
    try:
        web3 = make_web3(rpc_url, timeout_seconds=min(timeout_seconds, 30))
        connected = web3.is_connected()
        actual_chain_id = int(web3.eth.chain_id) if connected else 0
    except Exception as exc:
        raise AnchorPendingError(
            f"Could not connect to RPC {rpc_label} ({type(exc).__name__})",
            recovered,
        ) from None
    if not connected:
        raise AnchorPendingError(f"Could not connect to RPC {rpc_label}", recovered)
    if actual_chain_id != expected_chain_id:
        raise ChainError(
            f"Wrong chain: RPC returned {actual_chain_id}, expected {expected_chain_id}"
        )

    contract = registry_contract(web3, contract_address, expected_code_hash=expected_code_hash)
    return _wait_and_finalize_submission(
        web3=web3,
        contract=contract,
        commitment=commitment_bytes,
        submission=recovered,
        confirmations=confirmations,
        timeout_seconds=timeout_seconds,
    )


def verify_commitment_on_chain(
    commitment: str | bytes,
    *,
    rpc_url: str,
    expected_chain_id: int,
    contract_address: str,
    receipt: dict[str, Any] | None = None,
    required_confirmations: int = 1,
    timeout_seconds: float = 30,
    expected_code_hash: str | bytes | None = None,
) -> ChainVerification:
    commitment_bytes = parse_bytes32(commitment)
    _require_positive_int(expected_chain_id, "expected_chain_id")
    _require_positive_int(required_confirmations, "required_confirmations")
    _require_positive_number(timeout_seconds, "timeout_seconds")
    commitment_hex = bytes32_hex(commitment_bytes)
    rpc_label = _redacted_rpc_url(rpc_url)
    try:
        web3 = make_web3(rpc_url, timeout_seconds=timeout_seconds)
        connected = web3.is_connected()
        actual_chain_id = int(web3.eth.chain_id) if connected else 0
    except Exception as exc:
        raise ChainError(f"Could not connect to RPC {rpc_label} ({type(exc).__name__})") from exc
    if not connected:
        return ChainVerification(
            connected=False,
            chain_id_matches=False,
            anchored=False,
            commitment=commitment_hex,
            confirmations_required=required_confirmations,
            detail=f"Could not connect to RPC {rpc_label}",
        )
    if actual_chain_id != expected_chain_id:
        return ChainVerification(
            connected=True,
            chain_id_matches=False,
            anchored=False,
            commitment=commitment_hex,
            confirmations_required=required_confirmations,
            detail=f"RPC chain {actual_chain_id} does not match expected {expected_chain_id}",
        )

    contract = registry_contract(web3, contract_address, expected_code_hash=expected_code_hash)
    try:
        anchored = bool(contract.functions.verify(commitment_bytes).call())
    except Exception as exc:
        raise ChainError(f"Registry verify call failed ({type(exc).__name__})") from exc
    if not anchored:
        return ChainVerification(
            connected=True,
            chain_id_matches=True,
            anchored=False,
            commitment=commitment_hex,
            confirmations_required=required_confirmations,
            detail="Commitment is absent from the registry",
        )

    try:
        record = contract.functions.getRecord(commitment_bytes).call()
        submitter, chain_timestamp, block_number = _record_values(record)
    except ChainError:
        raise
    except Exception as exc:
        raise ChainError(f"Registry record read failed ({type(exc).__name__})") from exc
    try:
        confirmations_observed = max(0, int(web3.eth.block_number) - block_number + 1)
    except Exception as exc:
        raise ChainError(
            f"Could not read current confirmation depth ({type(exc).__name__})"
        ) from exc
    confirmations_satisfied = confirmations_observed >= required_confirmations
    receipt_consistent = _verify_saved_receipt(
        web3=web3,
        contract=contract,
        commitment=commitment_bytes,
        expected_chain_id=expected_chain_id,
        saved=receipt,
        expected_record=(submitter, chain_timestamp, block_number),
        required_confirmations=required_confirmations,
    )
    return ChainVerification(
        connected=True,
        chain_id_matches=True,
        anchored=True,
        commitment=commitment_hex,
        submitter=submitter,
        chain_timestamp=chain_timestamp,
        block_number=block_number,
        confirmations_observed=confirmations_observed,
        confirmations_required=required_confirmations,
        confirmations_satisfied=confirmations_satisfied,
        receipt_consistent=receipt_consistent,
        detail=(
            f"Commitment exists, but only {confirmations_observed} confirmation(s) are present; "
            f"{required_confirmations} required"
            if not confirmations_satisfied
            else (
                "Commitment is present in registry state"
                if receipt_consistent is not False
                else "Commitment exists, but the saved receipt is inconsistent"
            )
        ),
    )


def parse_bytes32(value: str | bytes) -> bytes:
    return _parse_bytes32(value, field="Commitment")


def bytes32_hex(value: bytes) -> str:
    return f"0x{parse_bytes32(value).hex()}"


def explorer_transaction_url(chain_id: int, transaction_hash: str) -> str | None:
    explorers = {
        84532: "https://sepolia.basescan.org/tx/",
        11155111: "https://sepolia.etherscan.io/tx/",
        80002: "https://amoy.polygonscan.com/tx/",
    }
    prefix = explorers.get(chain_id)
    return f"{prefix}{transaction_hash}" if prefix else None


def _wait_for_confirmations(
    web3: Web3, block_number: int, requested: int, *, deadline: float
) -> int:
    _require_positive_int(requested, "requested confirmations")
    while True:
        try:
            latest = int(web3.eth.block_number)
        except Exception as exc:
            raise ChainError(f"Could not read the latest block ({type(exc).__name__})") from exc
        observed = max(0, latest - block_number + 1)
        if observed >= requested:
            return observed
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ChainError(
                f"Timed out waiting for confirmations: observed {observed}, required {requested}"
            )
        time.sleep(min(0.5, remaining))


def _wait_and_finalize_submission(
    *,
    web3: Web3,
    contract: Contract,
    commitment: bytes,
    submission: AnchorSubmission,
    confirmations: int,
    timeout_seconds: float,
) -> AnchorReceipt:
    transaction_hash = _parse_bytes32(
        submission.transaction_hash, field="Submitted transaction hash"
    )
    try:
        receipt = web3.eth.wait_for_transaction_receipt(
            transaction_hash, timeout=timeout_seconds, poll_latency=0.5
        )
    except TimeExhausted as exc:
        raise AnchorPendingError(
            "Timed out waiting for the anchor transaction receipt", submission
        ) from exc
    except Exception as exc:
        raise AnchorPendingError(
            f"Could not read the anchor transaction receipt ({type(exc).__name__})",
            submission,
        ) from None

    try:
        if (
            _parse_bytes32(receipt["transactionHash"], field="Receipt transaction hash")
            != transaction_hash
        ):
            raise ChainError("Receipt transaction hash did not match the signed transaction")
        receipt_status = _strict_int(receipt["status"], "receipt status", minimum=0)
        receipt_block_number = _strict_int(
            receipt["blockNumber"], "receipt block number", minimum=0
        )
        receipt_block_hash = _parse_bytes32(receipt["blockHash"], field="Receipt block hash")
    except (ChainError, KeyError, TypeError, ValueError) as exc:
        raise AnchorPendingError(
            "RPC returned a malformed transaction receipt", submission
        ) from exc
    if receipt_status == 0:
        raise ChainError(f"Anchor transaction reverted: {submission.transaction_hash}")
    if receipt_status != 1:
        raise AnchorPendingError("RPC returned an unknown receipt status", submission)

    try:
        confirmations_observed = _wait_for_confirmations(
            web3,
            receipt_block_number,
            confirmations,
            deadline=time.monotonic() + timeout_seconds,
        )
    except ChainError as exc:
        raise AnchorPendingError(str(exc), submission) from exc

    try:
        canonical = _read_canonical_anchor(
            web3=web3,
            contract=contract,
            commitment=commitment,
            transaction_hash=transaction_hash,
            expected_chain_id=submission.chain_id,
            expected_sender=submission.submitter,
            expected_nonce=submission.nonce,
        )
    except ChainError as exc:
        raise AnchorPendingError(f"Canonical anchor validation failed: {exc}", submission) from exc
    except Exception as exc:
        raise AnchorPendingError(
            f"Could not validate mined anchor ({type(exc).__name__})", submission
        ) from None
    if canonical.block_number != receipt_block_number or canonical.block_hash != receipt_block_hash:
        raise AnchorPendingError("Anchor receipt changed after a chain reorganization", submission)

    return AnchorReceipt(
        schema=RECEIPT_SCHEMA,
        chain_id=submission.chain_id,
        rpc_network=_network_name(submission.chain_id),
        contract_address=submission.contract_address,
        commitment=submission.commitment,
        transaction_hash=bytes32_hex(canonical.transaction_hash),
        block_number=canonical.block_number,
        block_hash=bytes32_hex(canonical.block_hash),
        transaction_status=canonical.status,
        submitter=canonical.submitter,
        chain_timestamp=canonical.timestamp,
        confirmations_observed=confirmations_observed,
    )


def _validated_submission(
    submission: AnchorSubmission | Mapping[str, Any],
    *,
    commitment: bytes,
    expected_chain_id: int,
    contract_address: str,
    expected_submitter: str,
) -> AnchorSubmission:
    if isinstance(submission, AnchorSubmission):
        saved = submission.to_dict()
    elif isinstance(submission, Mapping):
        saved = dict(submission)
    else:
        raise ChainError("Recoverable anchor state must be an object")

    required = set(AnchorSubmission.__dataclass_fields__)
    if set(saved) != required:
        raise ChainError("Recoverable anchor state has missing or unexpected fields")
    if _saved_str(saved, "schema") != SUBMISSION_SCHEMA:
        raise ChainError("Recoverable anchor state uses an unsupported schema")
    if _saved_int(saved, "chain_id", minimum=1) != expected_chain_id:
        raise ChainError("Recoverable anchor state does not match the expected chain")
    trusted_contract = _checksum_address(contract_address, "Expected contract address")
    saved_contract = _checksum_address(
        _saved_str(saved, "contract_address"), "Recoverable contract address"
    )
    if saved_contract != trusted_contract:
        raise ChainError("Recoverable anchor state does not match the trusted registry")
    if parse_bytes32(_saved_str(saved, "commitment")) != commitment:
        raise ChainError("Recoverable anchor state does not match the commitment")

    trusted_submitter = _checksum_address(expected_submitter, "Expected submitter")
    saved_submitter = _checksum_address(_saved_str(saved, "submitter"), "Recoverable submitter")
    if saved_submitter != trusted_submitter:
        raise ChainError("Recoverable anchor state does not match the expected submitter")

    transaction_hash = _parse_bytes32(
        _saved_str(saved, "transaction_hash"), field="Recoverable transaction hash"
    )
    nonce = _saved_int(saved, "nonce", minimum=0)
    return AnchorSubmission(
        schema=SUBMISSION_SCHEMA,
        chain_id=expected_chain_id,
        contract_address=trusted_contract,
        commitment=bytes32_hex(commitment),
        transaction_hash=bytes32_hex(transaction_hash),
        submitter=trusted_submitter,
        nonce=nonce,
    )


def _verify_saved_receipt(
    *,
    web3: Web3,
    contract: Contract,
    commitment: bytes,
    expected_chain_id: int,
    saved: dict[str, Any] | None,
    expected_record: tuple[str, int, int] | None = None,
    required_confirmations: int = 1,
) -> bool | None:
    if saved is None:
        return None
    try:
        required = set(AnchorReceipt.__dataclass_fields__)
        if not isinstance(saved, dict) or set(saved) != required:
            return False
        if _saved_str(saved, "schema") != RECEIPT_SCHEMA:
            return False
        if _saved_int(saved, "chain_id", minimum=1) != expected_chain_id:
            return False
        if _saved_str(saved, "rpc_network") != _network_name(expected_chain_id):
            return False
        if Web3.to_checksum_address(_saved_str(saved, "contract_address")) != contract.address:
            return False
        if parse_bytes32(_saved_str(saved, "commitment")) != commitment:
            return False
        transaction_hash = _parse_bytes32(
            _saved_str(saved, "transaction_hash"), field="Saved transaction hash"
        )
        saved_submitter = Web3.to_checksum_address(_saved_str(saved, "submitter"))
        canonical = _read_canonical_anchor(
            web3=web3,
            contract=contract,
            commitment=commitment,
            transaction_hash=transaction_hash,
            expected_chain_id=expected_chain_id,
            expected_sender=saved_submitter,
        )
        if canonical.transaction_hash != transaction_hash:
            return False
        if canonical.status != _saved_int(saved, "transaction_status", minimum=0):
            return False
        if canonical.status != 1:
            return False
        if canonical.block_number != _saved_int(saved, "block_number", minimum=0):
            return False
        if canonical.block_hash != _parse_bytes32(
            _saved_str(saved, "block_hash"), field="Saved block hash"
        ):
            return False
        if canonical.submitter != saved_submitter:
            return False
        if canonical.timestamp != _saved_int(saved, "chain_timestamp", minimum=0):
            return False
        if (
            expected_record is not None
            and (
                canonical.submitter,
                canonical.timestamp,
                canonical.block_number,
            )
            != expected_record
        ):
            return False
        observed_at_write = _saved_int(saved, "confirmations_observed", minimum=1)
        current_observed = max(0, int(web3.eth.block_number) - canonical.block_number + 1)
        return observed_at_write <= current_observed and current_observed >= required_confirmations
    except Exception:
        return False


def _read_canonical_anchor(
    *,
    web3: Web3,
    contract: Contract,
    commitment: bytes,
    transaction_hash: str | bytes,
    expected_chain_id: int,
    expected_sender: str | None = None,
    expected_nonce: int | None = None,
) -> _CanonicalAnchor:
    tx_hash = _parse_bytes32(transaction_hash, field="Transaction hash")
    receipt = web3.eth.get_transaction_receipt(tx_hash)
    if _parse_bytes32(receipt["transactionHash"], field="Receipt transaction hash") != tx_hash:
        raise ChainError("Receipt transaction hash mismatch")
    status = _strict_int(receipt["status"], "receipt status", minimum=0)
    if status != 1:
        raise ChainError("Anchor transaction receipt is not successful")

    block_number = _strict_int(receipt["blockNumber"], "receipt block number", minimum=0)
    block_hash = _parse_bytes32(receipt["blockHash"], field="Receipt block hash")
    block = web3.eth.get_block(block_number)
    if _parse_bytes32(block["hash"], field="Canonical block hash") != block_hash:
        raise ChainError("Receipt block is not canonical at its recorded height")
    if _strict_int(block["number"], "canonical block number", minimum=0) != block_number:
        raise ChainError("Canonical block number mismatch")
    block_timestamp = _strict_int(block["timestamp"], "block timestamp", minimum=0)

    transaction = web3.eth.get_transaction(tx_hash)
    if _parse_bytes32(transaction["hash"], field="Transaction hash") != tx_hash:
        raise ChainError("Fetched transaction hash mismatch")
    transaction_block_number = _strict_int(
        transaction["blockNumber"], "transaction block number", minimum=0
    )
    if transaction_block_number != block_number:
        raise ChainError("Transaction block number does not match its receipt")
    if _parse_bytes32(transaction["blockHash"], field="Transaction block hash") != block_hash:
        raise ChainError("Transaction block hash does not match its receipt")
    if (
        "chainId" in transaction
        and _strict_int(transaction["chainId"], "transaction chain ID", minimum=1)
        != expected_chain_id
    ):
        raise ChainError("Transaction chain ID mismatch")

    sender = _checksum_address(transaction["from"], "transaction sender")
    if expected_sender is not None and sender != Web3.to_checksum_address(expected_sender):
        raise ChainError("Transaction sender does not match the expected submitter")
    if expected_nonce is not None:
        if "nonce" not in transaction:
            raise ChainError("Transaction nonce is missing")
        if _strict_int(transaction["nonce"], "transaction nonce", minimum=0) != expected_nonce:
            raise ChainError("Transaction nonce does not match the recoverable state")
    if _checksum_address(transaction["to"], "transaction recipient") != contract.address:
        raise ChainError("Transaction was not sent to the trusted registry")
    if _strict_int(transaction["value"], "transaction value", minimum=0) != 0:
        raise ChainError("Anchor transaction transferred value")
    if _data_bytes(transaction["input"], "transaction input") != _anchor_calldata(
        contract, commitment
    ):
        raise ChainError("Transaction input is not the exact anchor call")

    if _checksum_address(receipt["from"], "receipt sender") != sender:
        raise ChainError("Receipt sender does not match the transaction")
    if _checksum_address(receipt["to"], "receipt recipient") != contract.address:
        raise ChainError("Receipt recipient is not the trusted registry")

    matches: list[Mapping[str, Any]] = []
    for item in contract.events.EvidenceAnchored().process_receipt(receipt):
        try:
            same_address = _checksum_address(item["address"], "event emitter") == contract.address
            same_commitment = bytes(item["args"]["commitment"]) == commitment
        except (KeyError, TypeError, ValueError):
            continue
        if same_address and same_commitment:
            matches.append(item)
    if len(matches) != 1:
        raise ChainError("Receipt must contain exactly one matching registry event")

    event = matches[0]
    if _parse_bytes32(event["transactionHash"], field="Event transaction hash") != tx_hash:
        raise ChainError("Event transaction hash mismatch")
    if _parse_bytes32(event["blockHash"], field="Event block hash") != block_hash:
        raise ChainError("Event block hash mismatch")
    if _strict_int(event["blockNumber"], "event block number", minimum=0) != block_number:
        raise ChainError("Event block number mismatch")

    args = event["args"]
    event_submitter = _checksum_address(args["submitter"], "event submitter")
    event_timestamp = _strict_int(args["timestamp"], "event timestamp", minimum=0)
    event_block_number = _strict_int(args["blockNumber"], "event record block number", minimum=0)
    if event_submitter != sender:
        raise ChainError("Event submitter does not match the transaction sender")
    if event_timestamp != block_timestamp:
        raise ChainError("Event timestamp does not match the canonical block")
    if event_block_number != block_number:
        raise ChainError("Event record block number does not match the receipt")

    record = contract.functions.getRecord(commitment).call()
    record_submitter, record_timestamp, record_block_number = _record_values(record)
    if (record_submitter, record_timestamp, record_block_number) != (
        sender,
        block_timestamp,
        block_number,
    ):
        raise ChainError("Registry record does not match the transaction and event")

    return _CanonicalAnchor(
        transaction_hash=tx_hash,
        block_number=block_number,
        block_hash=block_hash,
        status=status,
        submitter=sender,
        timestamp=block_timestamp,
    )


def _parse_bytes32(value: object, *, field: str) -> bytes:
    if isinstance(value, str):
        text = value[2:] if value.startswith(("0x", "0X")) else value
        if len(text) != 64:
            raise ChainError(f"{field} must be exactly 32 bytes")
        try:
            raw = bytes.fromhex(text)
        except ValueError as exc:
            raise ChainError(f"{field} must be hexadecimal") from exc
    elif isinstance(value, bytes):
        raw = bytes(value)
    else:
        raise ChainError(f"{field} must be bytes or a hexadecimal string")
    if len(raw) != 32:
        raise ChainError(f"{field} must be exactly 32 bytes, received {len(raw)}")
    if raw == bytes(32):
        raise ChainError(f"{field} must not be zero")
    return raw


def _data_bytes(value: object, field: str) -> bytes:
    if isinstance(value, str):
        try:
            return bytes(Web3.to_bytes(hexstr=value))
        except (TypeError, ValueError) as exc:
            raise ChainError(f"{field} is not valid hexadecimal data") from exc
    if isinstance(value, bytes):
        return bytes(value)
    raise ChainError(f"{field} must be bytes or hexadecimal data")


def _anchor_calldata(contract: Contract, commitment: bytes) -> bytes:
    return _data_bytes(contract.encode_abi("anchor", args=[commitment]), "encoded anchor call")


def _checksum_address(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ChainError(f"{field} must be an address string")
    try:
        return Web3.to_checksum_address(value)
    except (TypeError, ValueError) as exc:
        raise ChainError(f"{field} is not a valid address") from exc


def _record_values(record: object) -> tuple[str, int, int]:
    if not isinstance(record, (list, tuple)) or len(record) != 3:
        raise ChainError("Registry returned a malformed record")
    return (
        _checksum_address(record[0], "record submitter"),
        _strict_int(record[1], "record timestamp", minimum=0),
        _strict_int(record[2], "record block number", minimum=0),
    )


def _strict_int(value: object, field: str, *, minimum: int) -> int:
    if type(value) is not int or value < minimum:
        raise ChainError(f"{field} must be an integer greater than or equal to {minimum}")
    return value


def _saved_int(saved: dict[str, Any], key: str, *, minimum: int) -> int:
    return _strict_int(saved[key], f"saved receipt {key}", minimum=minimum)


def _saved_str(saved: dict[str, Any], key: str) -> str:
    value = saved[key]
    if not isinstance(value, str):
        raise ChainError(f"saved receipt {key} must be a string")
    return value


def _require_positive_int(value: object, field: str) -> None:
    _strict_int(value, field, minimum=1)


def _require_positive_number(value: object, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ChainError(f"{field} must be positive")


def _validate_fee_caps(gas_limit_cap: int, fee_per_gas_cap: int, priority_fee_cap: int) -> None:
    _require_positive_int(gas_limit_cap, "gas_limit_cap")
    _require_positive_int(fee_per_gas_cap, "fee_per_gas_cap")
    _require_positive_int(priority_fee_cap, "priority_fee_cap")
    if gas_limit_cap > MAX_ANCHOR_GAS:
        raise ChainError(f"gas_limit_cap may not exceed {MAX_ANCHOR_GAS}")
    if fee_per_gas_cap > MAX_FEE_PER_GAS_WEI:
        raise ChainError(f"fee_per_gas_cap may not exceed {MAX_FEE_PER_GAS_WEI}")
    if priority_fee_cap > MAX_PRIORITY_FEE_PER_GAS_WEI:
        raise ChainError(f"priority_fee_cap may not exceed {MAX_PRIORITY_FEE_PER_GAS_WEI}")
    if priority_fee_cap > fee_per_gas_cap:
        raise ChainError("priority_fee_cap may not exceed fee_per_gas_cap")


def _bounded_fee_fields(
    web3: Web3,
    latest: Mapping[str, Any],
    *,
    fee_per_gas_cap: int,
    priority_fee_cap: int,
) -> dict[str, int]:
    base_fee = latest.get("baseFeePerGas")
    if base_fee is not None:
        base = _strict_int(base_fee, "base fee", minimum=0)
        try:
            priority = int(web3.eth.max_priority_fee)
        except Exception:
            priority = _FALLBACK_PRIORITY_FEE_WEI
        if priority < 0 or priority > priority_fee_cap:
            raise ChainError("RPC priority fee exceeds the configured safety cap")
        maximum = base * 2 + priority
        if maximum > fee_per_gas_cap:
            raise ChainError("RPC fee estimate exceeds the configured safety cap")
        return {"maxPriorityFeePerGas": priority, "maxFeePerGas": maximum}

    gas_price = int(web3.eth.gas_price)
    if gas_price < 0 or gas_price > fee_per_gas_cap:
        raise ChainError("RPC gas price exceeds the configured safety cap")
    return {"gasPrice": gas_price}


def _bounded_gas_limit(estimate: object, cap: int) -> int:
    estimate_int = _strict_int(estimate, "gas estimate", minimum=1)
    gas = max(estimate_int + estimate_int // 5, 80_000)
    if gas > cap:
        raise ChainError("Gas estimate exceeds the configured safety cap")
    return gas


def _validate_built_anchor_transaction(
    built: Mapping[str, Any],
    *,
    contract: Contract,
    commitment: bytes,
    sender: str,
    chain_id: int,
    nonce: int,
    gas: int,
    fee_fields: Mapping[str, int],
) -> None:
    if _checksum_address(built["from"], "built transaction sender") != Web3.to_checksum_address(
        sender
    ):
        raise ChainError("Built transaction sender mismatch")
    if _checksum_address(built["to"], "built transaction recipient") != contract.address:
        raise ChainError("Built transaction recipient mismatch")
    if _strict_int(built["value"], "built transaction value", minimum=0) != 0:
        raise ChainError("Built anchor transaction contains value")
    if _data_bytes(built["data"], "built transaction data") != _anchor_calldata(
        contract, commitment
    ):
        raise ChainError("Built transaction data is not the exact anchor call")
    expected_numbers = {
        "chainId": chain_id,
        "nonce": nonce,
        "gas": gas,
        **fee_fields,
    }
    for key, expected in expected_numbers.items():
        if _strict_int(built[key], f"built transaction {key}", minimum=0) != expected:
            raise ChainError(f"Built transaction {key} mismatch")


def _network_name(chain_id: int) -> str:
    return "base-sepolia" if chain_id == 84532 else f"chain-{chain_id}"


def _redacted_rpc_url(rpc_url: str) -> str:
    try:
        parsed = urlsplit(rpc_url)
        if not parsed.scheme or not parsed.hostname:
            return "<configured-rpc>"
        host = parsed.hostname
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        port = f":{parsed.port}" if parsed.port is not None else ""
        return f"{parsed.scheme}://{host}{port}"
    except (TypeError, ValueError):
        return "<configured-rpc>"


def explorer_url_for_tx(chain_id: int, tx_hash: str) -> str:
    """Return a public blockchain block-explorer URL for the transaction."""
    tx = tx_hash if tx_hash.startswith("0x") else f"0x{tx_hash}"
    if chain_id == 11155111:
        return f"https://sepolia.etherscan.io/tx/{tx}"
    if chain_id == 84532:
        return f"https://sepolia.basescan.org/tx/{tx}"
    if chain_id == 1:
        return f"https://etherscan.io/tx/{tx}"
    if chain_id == 8453:
        return f"https://basescan.org/tx/{tx}"
    return f"https://sepolia.etherscan.io/tx/{tx}"


def network_name_for_chain_id(chain_id: int) -> str:
    """Return a human-readable network label for known EVM chains."""
    if chain_id == 11155111:
        return "Ethereum Sepolia"
    if chain_id == 84532:
        return "Base Sepolia"
    if chain_id == 1:
        return "Ethereum Mainnet"
    if chain_id == 8453:
        return "Base Mainnet"
    return f"EVM Chain {chain_id}"


def anchor_calldata_transaction(
    commitment: str | bytes,
    *,
    rpc_url: str,
    expected_chain_id: int,
    private_key: str,
    timeout_seconds: float = 60,
) -> dict[str, Any]:
    """Anchor an evidence commitment as direct EVM transaction calldata.

    Enables real public blockchain proofs (e.g. Ethereum Sepolia) without requiring
    a pre-deployed smart contract.
    """
    commitment_bytes = parse_bytes32(commitment)
    _require_positive_int(expected_chain_id, "expected_chain_id")
    _require_positive_number(timeout_seconds, "timeout_seconds")

    rpc_label = _redacted_rpc_url(rpc_url)
    try:
        web3 = make_web3(rpc_url, timeout_seconds=min(timeout_seconds, 30))
        connected = web3.is_connected()
        actual_chain_id = int(web3.eth.chain_id) if connected else 0
    except Exception as exc:
        raise ChainError(f"Could not connect to RPC {rpc_label} ({type(exc).__name__})") from exc
    if not connected:
        raise ChainError(f"Could not connect to RPC {rpc_label}")
    if actual_chain_id != expected_chain_id:
        raise ChainError(
            f"Wrong chain: RPC returned {actual_chain_id}, expected {expected_chain_id}"
        )

    try:
        account = web3.eth.account.from_key(private_key)
    except Exception as exc:
        raise ChainError("FACEPROOF_PRIVATE_KEY is not a valid EVM private key") from exc

    try:
        nonce = int(web3.eth.get_transaction_count(account.address, "pending"))
        latest = web3.eth.get_block("latest")
        fee_fields = _bounded_fee_fields(web3, latest)
    except Exception as exc:
        raise ChainError(
            f"Could not prepare anchor transaction via {rpc_label} ({type(exc).__name__})"
        ) from exc

    tx: dict[str, Any] = {
        "from": account.address,
        "to": account.address,
        "value": 0,
        "nonce": nonce,
        "chainId": actual_chain_id,
        "gas": 40_000,
        "data": commitment_bytes,
        **fee_fields,
    }

    try:
        signed = account.sign_transaction(tx)
        raw_tx = getattr(signed, "raw_transaction", None) or signed.rawTransaction
        tx_hash_bytes = web3.eth.send_raw_transaction(raw_tx)
        tx_hash = (
            tx_hash_bytes.hex() if hasattr(tx_hash_bytes, "hex") else Web3.to_hex(tx_hash_bytes)
        )
        receipt = web3.eth.wait_for_transaction_receipt(tx_hash_bytes, timeout=timeout_seconds)
        block_number = int(receipt["blockNumber"])
        block_hash_raw = receipt["blockHash"]
        block_hash = (
            block_hash_raw.hex() if hasattr(block_hash_raw, "hex") else Web3.to_hex(block_hash_raw)
        )
        tx_status = int(receipt.get("status", 1))
    except Exception as exc:
        raise ChainError(
            f"Could not broadcast or confirm calldata anchor ({type(exc).__name__})"
        ) from exc

    final_tx_hash = tx_hash if tx_hash.startswith("0x") else f"0x{tx_hash}"
    final_block_hash = block_hash if block_hash.startswith("0x") else f"0x{block_hash}"
    return {
        "schema": RECEIPT_SCHEMA,
        "chain_id": actual_chain_id,
        "network": network_name_for_chain_id(actual_chain_id),
        "transaction_hash": final_tx_hash,
        "block_number": block_number,
        "block_hash": final_block_hash,
        "transaction_status": tx_status,
        "submitter": Web3.to_checksum_address(account.address),
        "commitment": bytes32_hex(commitment_bytes),
        "explorer_url": explorer_url_for_tx(actual_chain_id, final_tx_hash),
    }
