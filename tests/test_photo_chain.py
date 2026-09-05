from __future__ import annotations

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from faceproof import photo_chain
from faceproof.photo_chain import NETWORK, POW_PREFIX, SCHEMA, PhotoChain, PhotoChainError


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _modify_block(path: Path, column: str, value: object, *, index: int = 0) -> None:
    # Simulate an out-of-band editor bypassing the append-only SQL trigger.
    assert column in photo_chain._BLOCK_FIELDS
    with sqlite3.connect(path) as db:
        trigger = db.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'blocks_no_update'"
        ).fetchone()[0]
        db.execute("DROP TRIGGER blocks_no_update")
        db.execute(f"UPDATE blocks SET {column} = ? WHERE block_index = ?", (value, index))
        db.execute(trigger)


def test_anchor_persists_and_receipts_verify_after_restart(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "chain.sqlite3"
    chain = PhotoChain(path)
    assert not path.exists()
    receipts = [chain.anchor(_digest(value)) for value in ("first manifest", "second manifest")]
    assert receipts[0]["block_index"] == 0
    assert receipts[0]["previous_hash"] == "0" * 64
    assert receipts[1]["block_index"] == 1
    assert receipts[1]["previous_hash"] == receipts[0]["block_hash"]
    assert receipts[1]["timestamp_ns"] > receipts[0]["timestamp_ns"]
    for receipt in receipts:
        assert receipt["schema"] == SCHEMA
        assert receipt["network"] == NETWORK
        assert receipt["block_hash"].startswith(POW_PREFIX)
        assert "transaction_hash" not in receipt
        result = PhotoChain(path).verify(receipt["manifest_sha256"], receipt)
        assert result["passed"] is True
        assert result["status"] == "verified"
        assert result["blocks_verified"] == 2
    # The block's PoW is reproducible using ordinary canonical JSON and SHA-256.
    payload = {key: value for key, value in receipts[0].items() if key != "block_hash"}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    assert hashlib.sha256(canonical.encode("ascii")).hexdigest() == receipts[0]["block_hash"]
    assert b"first manifest" not in path.read_bytes()


def test_empty_file_can_be_initialized_only_by_anchor(tmp_path: Path) -> None:
    path = tmp_path / "chain.sqlite3"
    path.touch()
    receipt = PhotoChain(path).anchor(_digest("manifest"))
    assert PhotoChain(path).verify(_digest("manifest"), receipt)["passed"] is True


def test_changed_manifest_fails(tmp_path: Path) -> None:
    chain = PhotoChain(tmp_path / "chain.sqlite3")
    receipt = chain.anchor(_digest("original manifest"))
    result = chain.verify(_digest("modified manifest"), receipt)
    assert result["passed"] is False
    assert result["status"] == "manifest_mismatch"


@pytest.mark.parametrize("replacement", ["missing", "empty", "garbage"])
def test_removed_or_replaced_database_fails_closed(tmp_path: Path, replacement: str) -> None:
    path = tmp_path / "chain.sqlite3"
    chain = PhotoChain(path)
    receipt = chain.anchor(_digest("manifest"))
    path.unlink()
    if replacement != "missing":
        path.write_bytes(b"" if replacement == "empty" else b"not a database")
    before = path.read_bytes() if path.exists() else None
    result = PhotoChain(path).verify(_digest("manifest"), receipt)
    assert result["passed"] is False
    assert (path.read_bytes() if path.exists() else None) == before


def test_recreated_chain_does_not_validate_old_receipt(tmp_path: Path) -> None:
    path = tmp_path / "chain.sqlite3"
    digest = _digest("manifest")
    saved = PhotoChain(path).anchor(digest)
    path.unlink()
    replacement = PhotoChain(path).anchor(digest)
    assert saved["chain_id"] != replacement["chain_id"]
    assert PhotoChain(path).verify(digest, saved)["passed"] is False


@pytest.mark.parametrize("field", photo_chain._BLOCK_FIELDS)
def test_each_stored_block_field_is_checked(tmp_path: Path, field: str) -> None:
    path = tmp_path / "chain.sqlite3"
    chain = PhotoChain(path)
    receipt = chain.anchor(_digest("manifest"))
    value = receipt[field] + 1 if type(receipt[field]) is int else _digest("modified")
    _modify_block(path, field, value)
    assert chain.verify(receipt["manifest_sha256"], receipt)["passed"] is False
    with pytest.raises(PhotoChainError):
        chain.anchor(_digest("another manifest"))


def test_whole_chain_is_verified_even_after_the_receipt_block(tmp_path: Path) -> None:
    path = tmp_path / "chain.sqlite3"
    chain = PhotoChain(path)
    first = chain.anchor(_digest("first"))
    second = chain.anchor(_digest("second"))
    _modify_block(path, "nonce", second["nonce"] + 1, index=1)
    assert chain.verify(first["manifest_sha256"], first)["passed"] is False


def test_valid_hash_without_proof_of_work_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "chain.sqlite3"
    chain = PhotoChain(path)
    original = chain.anchor(_digest("manifest"))
    changed = dict(original)
    while True:
        changed["nonce"] += 1
        changed["block_hash"] = photo_chain._block_hash(changed)
        if not changed["block_hash"].startswith(POW_PREFIX):
            break
    _modify_block(path, "nonce", changed["nonce"])
    _modify_block(path, "block_hash", changed["block_hash"])
    result = chain.verify(changed["manifest_sha256"], changed)
    assert result["passed"] is False
    assert "proof of work" in result["reason"]


def test_remined_target_must_still_match_external_receipt(tmp_path: Path) -> None:
    path = tmp_path / "chain.sqlite3"
    chain = PhotoChain(path)
    original = chain.anchor(_digest("manifest"))
    changed = dict(original)
    changed["timestamp_ns"] += 1
    while True:
        changed["nonce"] += 1
        changed["block_hash"] = photo_chain._block_hash(changed)
        if changed["block_hash"].startswith(POW_PREFIX):
            break
    for field in ("timestamp_ns", "nonce", "block_hash"):
        _modify_block(path, field, changed[field])
    assert chain.verify(original["manifest_sha256"], original)["status"] == "receipt_mismatch"


@pytest.mark.parametrize("field", sorted(photo_chain._RECEIPT_FIELDS))
def test_every_receipt_field_is_bound(tmp_path: Path, field: str) -> None:
    chain = PhotoChain(tmp_path / "chain.sqlite3")
    receipt = chain.anchor(_digest("manifest"))
    changed = dict(receipt)
    changed[field] = (
        receipt[field] + 1 if type(receipt[field]) is int else "f" * len(receipt[field])
    )
    assert chain.verify(receipt["manifest_sha256"], changed)["passed"] is False


@pytest.mark.parametrize("digest", [None, 123, b"a" * 64, "", "a" * 63, "G" * 64, "A" * 64])
def test_malformed_digest_never_creates_storage(tmp_path: Path, digest: object) -> None:
    path = tmp_path / "chain.sqlite3"
    chain = PhotoChain(path)
    with pytest.raises(PhotoChainError):
        chain.anchor(digest)  # type: ignore[arg-type]
    assert chain.verify(digest, {})["status"] == "invalid_input"  # type: ignore[arg-type]
    assert not path.exists()


@pytest.mark.parametrize("receipt", [None, [], {}, {"schema": SCHEMA}])
def test_malformed_receipt_fails_closed(tmp_path: Path, receipt: object) -> None:
    path = tmp_path / "chain.sqlite3"
    result = PhotoChain(path).verify(_digest("manifest"), receipt)  # type: ignore[arg-type]
    assert result["passed"] is False
    assert result["status"] == "invalid_input"
    assert not path.exists()


def test_bool_receipt_integer_is_rejected(tmp_path: Path) -> None:
    chain = PhotoChain(tmp_path / "chain.sqlite3")
    receipt = chain.anchor(_digest("manifest"))
    receipt["block_index"] = False
    assert chain.verify(receipt["manifest_sha256"], receipt)["status"] == "invalid_input"


def test_concurrent_anchors_form_one_contiguous_chain(tmp_path: Path) -> None:
    path = tmp_path / "chain.sqlite3"
    digests = [_digest(f"manifest {index}") for index in range(12)]
    with ThreadPoolExecutor(max_workers=6) as executor:
        receipts = list(executor.map(lambda digest: PhotoChain(path).anchor(digest), digests))
    assert sorted(receipt["block_index"] for receipt in receipts) == list(range(12))
    assert len({receipt["chain_id"] for receipt in receipts}) == 1
    for digest, receipt in zip(digests, receipts, strict=True):
        result = PhotoChain(path).verify(digest, receipt)
        assert result["passed"] is True
        assert result["blocks_verified"] == 12


def test_sql_updates_and_deletes_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "chain.sqlite3"
    PhotoChain(path).anchor(_digest("manifest"))
    statements = (
        "UPDATE blocks SET nonce = nonce + 1",
        "DELETE FROM blocks",
        "UPDATE chain_metadata SET network = 'other'",
        "DELETE FROM chain_metadata",
    )
    with sqlite3.connect(path) as db:
        for statement in statements:
            with pytest.raises(sqlite3.IntegrityError):
                db.execute(statement)


def test_schema_tampering_is_not_silently_repaired(tmp_path: Path) -> None:
    path = tmp_path / "chain.sqlite3"
    chain = PhotoChain(path)
    receipt = chain.anchor(_digest("manifest"))
    with sqlite3.connect(path) as db:
        db.execute("DROP TRIGGER blocks_no_delete")
    assert chain.verify(receipt["manifest_sha256"], receipt)["status"] == "invalid_chain"
    with pytest.raises(PhotoChainError, match="schema"):
        chain.anchor(_digest("second"))


@pytest.mark.parametrize("delete_all", [False, True])
def test_deleted_anchor_or_emptied_chain_fails_closed(tmp_path: Path, delete_all: bool) -> None:
    path = tmp_path / "chain.sqlite3"
    chain = PhotoChain(path)
    chain.anchor(_digest("first"))
    receipt = chain.anchor(_digest("second"))
    with sqlite3.connect(path) as db:
        trigger = db.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'blocks_no_delete'"
        ).fetchone()[0]
        db.execute("DROP TRIGGER blocks_no_delete")
        if delete_all:
            db.execute("DELETE FROM blocks")
        else:
            db.execute("DELETE FROM blocks WHERE block_index = 1")
        db.execute(trigger)
    assert chain.verify(receipt["manifest_sha256"], receipt)["passed"] is False
    if delete_all:
        with pytest.raises(PhotoChainError, match="no anchored blocks"):
            chain.anchor(_digest("replacement"))


def test_mining_failure_rolls_back_initialization(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = tmp_path / "chain.sqlite3"
    monkeypatch.setattr(photo_chain, "_MAX_NONCE", 0)
    with pytest.raises(PhotoChainError, match="attempt limit"):
        PhotoChain(path).anchor(_digest("manifest"))
    with sqlite3.connect(path) as db:
        assert photo_chain._schema_objects(db) == {}
