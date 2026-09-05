"""A local, simulated SHA-256 chain for generic photo-copy evidence digests.

This is not a public blockchain or an independently trusted timestamp service.
An external saved receipt detects changes to its block; low-cost proof of work
and local storage do not provide distributed consensus or prevent an operator
from rebuilding history. Only manifest digests are stored, never images.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any

SCHEMA = "local-simulated-sha256-v1"
NETWORK = "local-simulated-sha256"
POW_PREFIX = "000"
_ZERO_HASH = "0" * 64
_MAX_INT = (1 << 63) - 1
_MAX_NONCE = 10_000_000
_BLOCK_FIELDS = (
    "block_index",
    "previous_hash",
    "timestamp_ns",
    "nonce",
    "manifest_sha256",
    "block_hash",
)
_RECEIPT_FIELDS = frozenset((*_BLOCK_FIELDS, "schema", "network", "chain_id", "pow_prefix"))
_DDL = {
    "chain_metadata": """CREATE TABLE chain_metadata (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        schema TEXT NOT NULL,
        network TEXT NOT NULL,
        chain_id TEXT NOT NULL,
        pow_prefix TEXT NOT NULL
    )""",
    "blocks": """CREATE TABLE blocks (
        block_index INTEGER PRIMARY KEY CHECK (block_index >= 0),
        previous_hash TEXT NOT NULL,
        timestamp_ns INTEGER NOT NULL CHECK (timestamp_ns > 0),
        nonce INTEGER NOT NULL CHECK (nonce >= 0),
        manifest_sha256 TEXT NOT NULL,
        block_hash TEXT NOT NULL UNIQUE
    )""",
    "blocks_no_update": """CREATE TRIGGER blocks_no_update BEFORE UPDATE ON blocks
        BEGIN SELECT RAISE(ABORT, 'The local chain is append-only'); END""",
    "blocks_no_delete": """CREATE TRIGGER blocks_no_delete BEFORE DELETE ON blocks
        BEGIN SELECT RAISE(ABORT, 'The local chain is append-only'); END""",
    "metadata_no_update": """CREATE TRIGGER metadata_no_update BEFORE UPDATE ON chain_metadata
        BEGIN SELECT RAISE(ABORT, 'The local chain metadata is immutable'); END""",
    "metadata_no_delete": """CREATE TRIGGER metadata_no_delete BEFORE DELETE ON chain_metadata
        BEGIN SELECT RAISE(ABORT, 'The local chain metadata is immutable'); END""",
}


class PhotoChainError(RuntimeError):
    """The local evidence chain could not safely accept an anchor."""


def _require_digest(value: object, field: str) -> None:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise PhotoChainError(f"{field} must be a lowercase, 64-character SHA-256 digest")


def _require_int(value: object, field: str, *, minimum: int = 0) -> None:
    if type(value) is not int or not minimum <= value <= _MAX_INT:
        raise PhotoChainError(f"{field} must be an integer between {minimum} and {_MAX_INT}")


def _validate_receipt(receipt: object) -> None:
    if not isinstance(receipt, dict) or receipt.keys() != _RECEIPT_FIELDS:
        raise PhotoChainError("Receipt fields do not match the local simulated chain schema")
    if receipt["schema"] != SCHEMA or receipt["network"] != NETWORK:
        raise PhotoChainError("Receipt must identify the local-simulated-sha256 network and schema")
    if receipt["pow_prefix"] != POW_PREFIX:
        raise PhotoChainError("Receipt has an unsupported proof-of-work target")
    if (
        not isinstance(receipt["chain_id"], str)
        or re.fullmatch(r"[0-9a-f]{32}", receipt["chain_id"]) is None
    ):
        raise PhotoChainError("Receipt has a malformed local chain identifier")
    for field in ("manifest_sha256", "previous_hash", "block_hash"):
        _require_digest(receipt[field], field)
    _require_int(receipt["block_index"], "block_index")
    _require_int(receipt["timestamp_ns"], "timestamp_ns", minimum=1)
    _require_int(receipt["nonce"], "nonce")


def _block_hash(block: dict[str, Any]) -> str:
    payload = {key: value for key, value in block.items() if key != "block_hash"}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def _schema_objects(connection: sqlite3.Connection) -> dict[str, str]:
    return dict(
        connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        ).fetchall()
    )


def _read_chain(connection: sqlite3.Connection) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
        raise PhotoChainError("Local chain database integrity check failed")
    if _schema_objects(connection) != _DDL:
        raise PhotoChainError("Local chain database schema is missing or modified")
    metadata_rows = connection.execute("SELECT * FROM chain_metadata").fetchall()
    if len(metadata_rows) != 1:
        raise PhotoChainError("Local chain metadata is missing or duplicated")
    singleton, schema, network, chain_id, pow_prefix = metadata_rows[0]
    context = {"schema": schema, "network": network, "chain_id": chain_id, "pow_prefix": pow_prefix}
    if singleton != 1:
        raise PhotoChainError("Local chain metadata is invalid")
    blocks = []
    previous_hash = _ZERO_HASH
    previous_timestamp = 0
    rows = connection.execute(
        "SELECT block_index, previous_hash, timestamp_ns, nonce, manifest_sha256, block_hash "
        "FROM blocks ORDER BY block_index"
    )
    for index, row in enumerate(rows):
        block = {**context, **dict(zip(_BLOCK_FIELDS, row, strict=True))}
        _validate_receipt(block)
        if block["block_index"] != index:
            raise PhotoChainError("Local chain block indexes are not contiguous")
        if block["previous_hash"] != previous_hash:
            raise PhotoChainError("Local chain previous-hash linkage is invalid")
        if block["timestamp_ns"] <= previous_timestamp:
            raise PhotoChainError("Local chain block timestamps are not increasing")
        if _block_hash(block) != block["block_hash"]:
            raise PhotoChainError("Local chain block hash does not match its contents")
        if not block["block_hash"].startswith(POW_PREFIX):
            raise PhotoChainError("Local chain block does not satisfy proof of work")
        blocks.append(block)
        previous_hash = block["block_hash"]
        previous_timestamp = block["timestamp_ns"]
    # An established database without any blocks must not be treated as a new chain.
    if not blocks:
        raise PhotoChainError("Local chain contains no anchored blocks")
    return context, blocks


class PhotoChain:
    """Append digests to a persistent local demonstration chain.

    Construction performs no I/O. ``anchor`` creates a new chain for a missing
    or empty database and raises ``PhotoChainError`` on failure. ``verify`` is
    read-only, never creates storage, and returns a fail-closed result for any
    invalid input or unreadable/inconsistent chain. Preserve receipts separately
    from the database to compare against the originally returned block hashes.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()

    def anchor(self, manifest_sha256: str) -> dict[str, Any]:
        """Atomically mine and append one block containing only the supplied digest."""
        _require_digest(manifest_sha256, "manifest_sha256")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(self.path, timeout=30, isolation_level=None)) as db:
                db.execute("BEGIN IMMEDIATE")
                try:
                    if not _schema_objects(db):
                        for ddl in _DDL.values():
                            db.execute(ddl)
                        context = {
                            "schema": SCHEMA,
                            "network": NETWORK,
                            "chain_id": uuid.uuid4().hex,
                            "pow_prefix": POW_PREFIX,
                        }
                        db.execute(
                            "INSERT INTO chain_metadata VALUES (1, ?, ?, ?, ?)",
                            tuple(context.values()),
                        )
                        blocks: list[dict[str, Any]] = []
                    else:
                        context, blocks = _read_chain(db)
                    previous = blocks[-1] if blocks else None
                    block = {
                        **context,
                        "block_index": len(blocks),
                        "previous_hash": previous["block_hash"] if previous else _ZERO_HASH,
                        "timestamp_ns": max(
                            time.time_ns(), previous["timestamp_ns"] + 1 if previous else 1
                        ),
                        "nonce": 0,
                        "manifest_sha256": manifest_sha256,
                    }
                    for nonce in range(_MAX_NONCE):
                        block["nonce"] = nonce
                        candidate_hash = _block_hash(block)
                        if candidate_hash.startswith(POW_PREFIX):
                            block["block_hash"] = candidate_hash
                            break
                    else:
                        raise PhotoChainError("Local proof-of-work attempt limit reached")
                    _validate_receipt(block)
                    db.execute(
                        "INSERT INTO blocks VALUES (?, ?, ?, ?, ?, ?)",
                        tuple(block[key] for key in _BLOCK_FIELDS),
                    )
                    db.execute("COMMIT")
                    return block
                except Exception:
                    if db.in_transaction:
                        db.execute("ROLLBACK")
                    raise
        except (OSError, sqlite3.Error) as exc:
            raise PhotoChainError("Could not safely write the local chain database") from exc

    def verify(self, manifest_sha256: str, receipt: dict[str, Any]) -> dict[str, Any]:
        """Validate the entire current chain and its exact receipt-bound target block."""

        def failed(status: str, reason: str) -> dict[str, Any]:
            return {"passed": False, "status": status, "reason": reason, "network": NETWORK}

        try:
            _require_digest(manifest_sha256, "manifest_sha256")
            _validate_receipt(receipt)
        except PhotoChainError as exc:
            return failed("invalid_input", str(exc))
        if manifest_sha256 != receipt["manifest_sha256"]:
            return failed("manifest_mismatch", "Manifest digest does not match the saved receipt")
        try:
            with closing(
                sqlite3.connect(
                    self.path.as_uri() + "?mode=ro", uri=True, timeout=30, isolation_level=None
                )
            ) as db:
                db.execute("BEGIN")
                context, blocks = _read_chain(db)
                if context["chain_id"] != receipt["chain_id"]:
                    return failed("receipt_mismatch", "Receipt belongs to a different local chain")
                index = receipt["block_index"]
                if index >= len(blocks):
                    return failed("receipt_mismatch", "The receipt's block is missing")
                if blocks[index] != receipt:
                    return failed(
                        "receipt_mismatch", "Saved receipt does not match the stored block"
                    )
                return {
                    "passed": True,
                    "status": "verified",
                    "reason": "Manifest digest, saved receipt, and complete local chain agree",
                    "network": NETWORK,
                    "block_index": index,
                    "block_hash": receipt["block_hash"],
                    "blocks_verified": len(blocks),
                }
        except PhotoChainError as exc:
            return failed("invalid_chain", str(exc))
        except (OSError, sqlite3.Error):
            return failed("unavailable", "Local chain database is missing, unreadable, or corrupt")
