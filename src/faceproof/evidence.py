"""Deterministic evidence manifests and privacy-preserving commitments.

The pre-anchor manifest deliberately contains no blockchain receipt.  A caller
should persist the return value of :func:`compute_commitment` and any eventual
transaction receipt as separate sidecar files.  This avoids the circular
construction in which a receipt changes the bytes whose commitment produced
that receipt.

RFC 8785 is the default and preferred canonicalization algorithm.  If the
optional ``rfc8785`` package is unavailable, strict mode fails explicitly.
Callers may opt in to the deliberately narrower
``faceproof-deterministic-json-v1`` fallback.  The fallback rejects floating
point numbers and is labelled in the manifest; it must never be represented as
RFC 8785 output.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MANIFEST_SCHEMA = "faceproof-evidence-manifest-v1"
MANIFEST_VERSION = 1
RFC8785 = "RFC8785"
DETERMINISTIC_JSON_V1 = "faceproof-deterministic-json-v1"
COMMITMENT_SCHEME = "keccak256(abi.encode(bytes32 manifestSha256,bytes32 salt))"

_MAX_IJSON_INTEGER = (1 << 53) - 1
_HEX_32_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_RESERVED_RECEIPT_KEYS = {
    "receipt",
    "anchor_receipt",
    "blockchain_receipt",
    "transaction_receipt",
    "tx_receipt",
}


class EvidenceError(ValueError):
    """Base class for evidence construction and verification errors."""


class CanonicalizationError(EvidenceError):
    """Raised when JSON cannot be represented by the selected algorithm."""


class CanonicalizationUnavailable(CanonicalizationError):
    """Raised when strict RFC 8785 canonicalization is unavailable."""


class KeccakUnavailable(EvidenceError):
    """Raised when no Ethereum-compatible Keccak-256 implementation exists."""


def _import_rfc8785() -> Any | None:
    try:
        import rfc8785  # type: ignore[import-not-found]
    except ImportError:
        return None
    return rfc8785


def select_canonicalization_algorithm(*, allow_non_rfc8785_fallback: bool = False) -> str:
    """Select RFC 8785, or an explicitly permitted and labelled fallback."""

    if _import_rfc8785() is not None:
        return RFC8785
    if allow_non_rfc8785_fallback:
        return DETERMINISTIC_JSON_V1
    raise CanonicalizationUnavailable(
        "RFC 8785 canonicalization requires the optional 'rfc8785' package. "
        "Install it, or explicitly pass allow_non_rfc8785_fallback=True to "
        "use the labelled, float-free FaceProof fallback."
    )


def _validate_json_value(value: Any, *, path: str = "$", allow_float: bool) -> None:
    if value is None or isinstance(value, (str, bool)):
        if isinstance(value, str):
            try:
                value.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise CanonicalizationError(
                    f"{path} contains an invalid Unicode surrogate"
                ) from exc
        return

    # bool is an int subclass, so it must be handled first.
    if isinstance(value, int):
        if abs(value) > _MAX_IJSON_INTEGER:
            raise CanonicalizationError(f"{path} integer is outside the interoperable I-JSON range")
        return

    if isinstance(value, float):
        if not math.isfinite(value):
            raise CanonicalizationError(f"{path} contains NaN or infinity")
        if not allow_float:
            raise CanonicalizationError(
                f"{path} contains a float; the non-RFC fallback accepts only "
                "integers or decimal strings to prevent cross-runtime drift"
            )
        return

    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_json_value(item, path=f"{path}[{index}]", allow_float=allow_float)
        return

    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise CanonicalizationError(f"{path} has a non-string object key")
            _validate_json_value(key, path=f"{path}.<key>", allow_float=allow_float)
            _validate_json_value(item, path=f"{path}.{key}", allow_float=allow_float)
        return

    raise CanonicalizationError(f"{path} has unsupported JSON type {type(value).__name__}")


def canonicalize_json(
    value: Any,
    *,
    algorithm: str | None = None,
    allow_non_rfc8785_fallback: bool = False,
) -> bytes:
    """Return canonical UTF-8 bytes without silently changing algorithms.

    ``algorithm=None`` prefers RFC 8785.  If RFC 8785 is unavailable, the
    fallback is selected only when ``allow_non_rfc8785_fallback`` is true.
    Existing fallback-labelled manifests likewise require that explicit flag.
    """

    selected = algorithm or select_canonicalization_algorithm(
        allow_non_rfc8785_fallback=allow_non_rfc8785_fallback
    )

    if selected == RFC8785:
        implementation = _import_rfc8785()
        if implementation is None:
            raise CanonicalizationUnavailable(
                "This manifest declares RFC8785, but the 'rfc8785' package is "
                "not installed. Refusing to change its canonicalization."
            )
        _validate_json_value(value, allow_float=True)
        try:
            encoded = implementation.dumps(value)
        except (TypeError, ValueError) as exc:
            raise CanonicalizationError(f"RFC 8785 canonicalization failed: {exc}") from exc
        if isinstance(encoded, str):
            return encoded.encode("utf-8")
        if isinstance(encoded, (bytes, bytearray, memoryview)):
            return bytes(encoded)
        raise CanonicalizationError("rfc8785.dumps returned an unsupported value")

    if selected == DETERMINISTIC_JSON_V1:
        if not allow_non_rfc8785_fallback:
            raise CanonicalizationUnavailable(
                "This manifest uses the non-RFC fallback. Pass "
                "allow_non_rfc8785_fallback=True to acknowledge it explicitly."
            )
        _validate_json_value(value, allow_float=False)
        try:
            return json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeEncodeError) as exc:
            raise CanonicalizationError(
                f"FaceProof fallback canonicalization failed: {exc}"
            ) from exc

    raise CanonicalizationError(f"Unsupported canonicalization algorithm: {selected!r}")


def _normalise_logical_path(path: str) -> str:
    if not isinstance(path, str) or not path:
        raise EvidenceError("Artifact logical paths must be non-empty strings")
    path = path.replace("\\", "/")
    if path.startswith("/") or _WINDOWS_DRIVE_RE.match(path):
        raise EvidenceError(f"Artifact path must be relative: {path!r}")
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise EvidenceError(f"Artifact path is not canonical or is unsafe: {path!r}")
    if any(any(ord(character) < 0x20 for character in part) for part in parts):
        raise EvidenceError(f"Artifact path contains a control character: {path!r}")
    try:
        path.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise EvidenceError(f"Artifact path is not valid Unicode: {path!r}") from exc
    return "/".join(parts)


def _utf16_sort_key(value: str) -> bytes:
    # RFC 8785 compares object names as UTF-16 code units.  Using the same key
    # for the artifact array gives a stable order even for non-BMP names.
    return value.encode("utf-16-be")


def _read_artifact(source: Any) -> bytes:
    if isinstance(source, (bytes, bytearray, memoryview)):
        return bytes(source)
    if isinstance(source, (str, os.PathLike)):
        try:
            return Path(source).read_bytes()
        except OSError as exc:
            raise EvidenceError(f"Unable to read artifact {source!r}: {exc}") from exc
    raise EvidenceError(
        "Artifact values must be bytes-like objects or filesystem paths; "
        "encode text explicitly so its byte representation is unambiguous"
    )


def sha256_bytes(data: bytes | bytearray | memoryview) -> str:
    """Return a lowercase, non-prefixed SHA-256 digest."""

    return hashlib.sha256(bytes(data)).hexdigest()


def sha256_file(path: str | os.PathLike[str]) -> str:
    """Stream a file into SHA-256 without loading it all into memory."""

    digest = hashlib.sha256()
    try:
        with Path(path).open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise EvidenceError(f"Unable to hash artifact {path!r}: {exc}") from exc
    return digest.hexdigest()


def _format_observed_at(value: datetime | str | None) -> str:
    if value is None:
        parsed = datetime.now(UTC)
    elif isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise EvidenceError("observed_at must be an RFC 3339 timestamp") from exc
    else:
        raise EvidenceError("observed_at must be a datetime, RFC 3339 string, or None")

    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise EvidenceError("observed_at must include a timezone")
    parsed = parsed.astimezone(UTC)
    timespec = "microseconds" if parsed.microsecond else "seconds"
    return parsed.isoformat(timespec=timespec).replace("+00:00", "Z")


def _normalise_reserved_key(key: str) -> str:
    return key.casefold().replace("-", "_").replace(" ", "_")


def _assert_no_receipt_fields(value: Any, *, path: str = "$") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if isinstance(key, str) and _normalise_reserved_key(key) in _RESERVED_RECEIPT_KEYS:
                raise EvidenceError(
                    f"{path}.{key} is a receipt field. Keep blockchain receipts "
                    "outside the pre-anchor manifest."
                )
            _assert_no_receipt_fields(item, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _assert_no_receipt_fields(item, path=f"{path}[{index}]")


def _validate_manifest_structure(manifest: Mapping[str, Any]) -> None:
    required = {
        "schema",
        "version",
        "run_id",
        "observed_at",
        "canonicalization",
        "hash_algorithm",
        "artifacts",
        "metadata",
    }
    unknown = set(manifest) - required
    missing = required - set(manifest)
    if missing:
        raise EvidenceError(f"Manifest is missing fields: {sorted(missing)!r}")
    if unknown:
        raise EvidenceError(f"Manifest has unsupported fields: {sorted(unknown)!r}")
    if manifest["schema"] != MANIFEST_SCHEMA or manifest["version"] != MANIFEST_VERSION:
        raise EvidenceError("Unsupported evidence manifest schema or version")
    if not isinstance(manifest["run_id"], str) or not manifest["run_id"].strip():
        raise EvidenceError("Manifest run_id must be a non-empty string")
    if len(manifest["run_id"]) > 128:
        raise EvidenceError("Manifest run_id must be at most 128 characters")
    _format_observed_at(manifest["observed_at"])
    if manifest["hash_algorithm"] != "SHA-256":
        raise EvidenceError("Only SHA-256 artifact manifests are supported")

    canonicalization = manifest["canonicalization"]
    if not isinstance(canonicalization, Mapping) or set(canonicalization) != {"algorithm"}:
        raise EvidenceError("canonicalization must contain only an algorithm field")
    if canonicalization["algorithm"] not in {RFC8785, DETERMINISTIC_JSON_V1}:
        raise EvidenceError("Manifest declares an unsupported canonicalization algorithm")

    if not isinstance(manifest["metadata"], Mapping):
        raise EvidenceError("Manifest metadata must be an object")
    artifacts = manifest["artifacts"]
    if not isinstance(artifacts, list) or not artifacts:
        raise EvidenceError("Manifest must contain at least one artifact")

    paths: list[str] = []
    for index, artifact in enumerate(artifacts):
        if not isinstance(artifact, Mapping):
            raise EvidenceError(f"Artifact record {index} must be an object")
        allowed = {"path", "size", "sha256", "media_type"}
        if set(artifact) - allowed or not {"path", "size", "sha256"} <= set(artifact):
            raise EvidenceError(f"Artifact record {index} has invalid fields")
        path = _normalise_logical_path(artifact["path"])
        if path != artifact["path"]:
            raise EvidenceError(f"Artifact record {index} path is not canonical")
        if (
            not isinstance(artifact["size"], int)
            or isinstance(artifact["size"], bool)
            or artifact["size"] < 0
        ):
            raise EvidenceError(f"Artifact record {index} has an invalid size")
        if not isinstance(artifact["sha256"], str) or not _HEX_32_RE.fullmatch(artifact["sha256"]):
            raise EvidenceError(f"Artifact record {index} has an invalid SHA-256")
        if "media_type" in artifact and (
            not isinstance(artifact["media_type"], str) or not artifact["media_type"]
        ):
            raise EvidenceError(f"Artifact record {index} has an invalid media_type")
        paths.append(path)

    if len(paths) != len(set(paths)):
        raise EvidenceError("Manifest contains duplicate artifact paths")
    if paths != sorted(paths, key=_utf16_sort_key):
        raise EvidenceError("Manifest artifact records are not in canonical path order")
    _assert_no_receipt_fields(manifest)


def build_manifest(
    artifacts: Mapping[str, Any],
    *,
    run_id: str | None = None,
    observed_at: datetime | str | None = None,
    metadata: Mapping[str, Any] | None = None,
    media_types: Mapping[str, str] | None = None,
    allow_non_rfc8785_fallback: bool = False,
) -> dict[str, Any]:
    """Build an artifact SHA-256 manifest.

    Mapping keys are stable logical paths. Values are bytes-like objects or
    filesystem paths. Text must be encoded by the caller. No artifact content,
    salt, commitment, or blockchain receipt is inserted into the manifest.
    """

    if not isinstance(artifacts, Mapping) or not artifacts:
        raise EvidenceError("artifacts must be a non-empty mapping")
    selected = select_canonicalization_algorithm(
        allow_non_rfc8785_fallback=allow_non_rfc8785_fallback
    )

    normalised_media_types: dict[str, str] = {}
    if media_types is not None:
        if not isinstance(media_types, Mapping):
            raise EvidenceError("media_types must be a mapping")
        for logical_path, media_type in media_types.items():
            normalised = _normalise_logical_path(logical_path)
            if not isinstance(media_type, str) or not media_type:
                raise EvidenceError(f"Invalid media type for {logical_path!r}")
            normalised_media_types[normalised] = media_type

    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for logical_path, source in artifacts.items():
        normalised = _normalise_logical_path(logical_path)
        if normalised in seen:
            raise EvidenceError(f"Duplicate normalized artifact path: {normalised!r}")
        seen.add(normalised)
        payload = _read_artifact(source)
        record: dict[str, Any] = {
            "path": normalised,
            "size": len(payload),
            "sha256": sha256_bytes(payload),
        }
        if normalised in normalised_media_types:
            record["media_type"] = normalised_media_types[normalised]
        records.append(record)
    records.sort(key=lambda record: _utf16_sort_key(record["path"]))

    metadata_copy: dict[str, Any]
    if metadata is None:
        metadata_copy = {}
    elif isinstance(metadata, Mapping):
        metadata_copy = copy.deepcopy(dict(metadata))
    else:
        raise EvidenceError("metadata must be a mapping")
    _assert_no_receipt_fields(metadata_copy, path="$.metadata")

    identifier = run_id or str(uuid.uuid4())
    if not isinstance(identifier, str) or not identifier.strip() or len(identifier) > 128:
        raise EvidenceError("run_id must be a non-empty string of at most 128 characters")

    manifest: dict[str, Any] = {
        "schema": MANIFEST_SCHEMA,
        "version": MANIFEST_VERSION,
        "run_id": identifier,
        "observed_at": _format_observed_at(observed_at),
        "canonicalization": {"algorithm": selected},
        "hash_algorithm": "SHA-256",
        "artifacts": records,
        "metadata": metadata_copy,
    }
    _validate_manifest_structure(manifest)
    # Validate representability now rather than failing only at anchor time.
    canonical_manifest_bytes(
        manifest,
        allow_non_rfc8785_fallback=allow_non_rfc8785_fallback,
    )
    return manifest


create_manifest = build_manifest


def canonical_manifest_bytes(
    manifest: Mapping[str, Any],
    *,
    allow_non_rfc8785_fallback: bool = False,
) -> bytes:
    """Canonicalize a structurally valid manifest using its declared algorithm."""

    if not isinstance(manifest, Mapping):
        raise EvidenceError("manifest must be an object")
    _validate_manifest_structure(manifest)
    algorithm = manifest["canonicalization"]["algorithm"]
    return canonicalize_json(
        manifest,
        algorithm=algorithm,
        allow_non_rfc8785_fallback=allow_non_rfc8785_fallback,
    )


def manifest_sha256(
    manifest: Mapping[str, Any],
    *,
    allow_non_rfc8785_fallback: bool = False,
) -> str:
    """Return SHA-256 of the selected canonical manifest bytes."""

    return sha256_bytes(
        canonical_manifest_bytes(
            manifest,
            allow_non_rfc8785_fallback=allow_non_rfc8785_fallback,
        )
    )


def generate_salt() -> bytes:
    """Generate the 32-byte salt used by the ABI commitment."""

    return secrets.token_bytes(32)


def _parse_bytes32(value: bytes | bytearray | memoryview | str, *, label: str) -> bytes:
    if isinstance(value, str):
        value = value[2:] if value.startswith(("0x", "0X")) else value
        if not _HEX_32_RE.fullmatch(value):
            raise EvidenceError(f"{label} must be exactly 32 bytes of hexadecimal")
        return bytes.fromhex(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        parsed = bytes(value)
        if len(parsed) != 32:
            raise EvidenceError(f"{label} must be exactly 32 bytes")
        return parsed
    raise EvidenceError(f"{label} must be bytes32 or hexadecimal")


def _abi_encode_bytes32_pair(left: bytes, right: bytes) -> tuple[bytes, str]:
    try:
        from eth_abi import encode as abi_encode  # type: ignore[import-not-found]
    except ImportError:
        # For two static bytes32 values, Solidity ABI encoding is exactly the
        # concatenation of the two 32-byte words. This is not abi.encodePacked.
        return left + right, "static-bytes32"
    encoded = bytes(abi_encode(["bytes32", "bytes32"], [left, right]))
    if len(encoded) != 64:
        raise EvidenceError("eth_abi returned an invalid bytes32 pair encoding")
    return encoded, "eth_abi"


def _ethereum_keccak256(data: bytes) -> tuple[bytes, str]:
    try:
        from eth_hash.auto import keccak  # type: ignore[import-not-found]
    except ImportError:
        pass
    else:
        return bytes(keccak(data)), "eth_hash"

    try:
        from web3 import Web3  # type: ignore[import-not-found]
    except ImportError:
        pass
    else:
        return bytes(Web3.keccak(data)), "web3"

    raise KeccakUnavailable(
        "An Ethereum-compatible Keccak-256 implementation is required. Install "
        "eth-hash or web3. hashlib.sha3_256 is not "
        "a safe substitute because Ethereum uses pre-standard Keccak-256."
    )


def compute_commitment(
    manifest_or_sha256: Mapping[str, Any] | bytes | bytearray | memoryview | str,
    *,
    salt: bytes | bytearray | memoryview | str | None = None,
    allow_non_rfc8785_fallback: bool = False,
) -> dict[str, str]:
    """Create an Ethereum-compatible salted commitment sidecar.

    The result, especially ``salt``, must remain outside the pre-anchor
    manifest. On chain, publish only ``commitment`` unless the use case has a
    documented reason to reveal more.
    """

    if isinstance(manifest_or_sha256, Mapping):
        manifest_digest = bytes.fromhex(
            manifest_sha256(
                manifest_or_sha256,
                allow_non_rfc8785_fallback=allow_non_rfc8785_fallback,
            )
        )
    else:
        manifest_digest = _parse_bytes32(manifest_or_sha256, label="manifest_sha256")
    salt_bytes = generate_salt() if salt is None else _parse_bytes32(salt, label="salt")
    abi_encoded, abi_backend = _abi_encode_bytes32_pair(manifest_digest, salt_bytes)
    commitment, keccak_backend = _ethereum_keccak256(abi_encoded)
    return {
        "scheme": COMMITMENT_SCHEME,
        "manifest_sha256": "0x" + manifest_digest.hex(),
        "salt": "0x" + salt_bytes.hex(),
        "commitment": "0x" + commitment.hex(),
        "abi_backend": abi_backend,
        "keccak_backend": keccak_backend,
    }


commit_manifest = compute_commitment


def _artifact_sources_from_root(
    manifest: Mapping[str, Any], root: str | os.PathLike[str]
) -> dict[str, Path]:
    root_path = Path(root).resolve()
    sources: dict[str, Path] = {}
    for record in manifest.get("artifacts", []):
        logical_path = _normalise_logical_path(record["path"])
        candidate = (root_path / Path(*logical_path.split("/"))).resolve()
        try:
            candidate.relative_to(root_path)
        except ValueError as exc:
            raise EvidenceError(
                f"Artifact path escapes verification root: {logical_path!r}"
            ) from exc
        sources[logical_path] = candidate
    return sources


def verify_manifest(
    manifest: Mapping[str, Any],
    artifacts: Mapping[str, Any] | str | os.PathLike[str],
    *,
    salt: bytes | bytearray | memoryview | str | None = None,
    expected_commitment: bytes | bytearray | memoryview | str | None = None,
    expected_manifest_sha256: bytes | bytearray | memoryview | str | None = None,
    allow_non_rfc8785_fallback: bool = False,
    allow_extra_artifacts: bool = False,
) -> dict[str, Any]:
    """Re-hash artifacts and optionally verify the public-chain commitment.

    Verification is non-throwing for ordinary tampering or malformed evidence;
    callers receive ``ok=False`` and human-readable errors. Programming errors,
    such as an unsupported ``artifacts`` argument type, are also reported in the
    same result so command-line verifiers can fail closed.
    """

    errors: list[str] = []
    computed_manifest_hash: str | None = None
    computed_commitment: str | None = None
    checked = 0

    try:
        canonical = canonical_manifest_bytes(
            manifest,
            allow_non_rfc8785_fallback=allow_non_rfc8785_fallback,
        )
        computed_manifest_hash = sha256_bytes(canonical)
    except (EvidenceError, TypeError, KeyError) as exc:
        errors.append(f"manifest: {exc}")

    if computed_manifest_hash is not None and expected_manifest_sha256 is not None:
        try:
            expected_hash = _parse_bytes32(
                expected_manifest_sha256, label="expected_manifest_sha256"
            ).hex()
            if not hmac.compare_digest(computed_manifest_hash, expected_hash):
                errors.append("manifest SHA-256 does not match the expected digest")
        except EvidenceError as exc:
            errors.append(str(exc))

    try:
        if isinstance(artifacts, Mapping):
            supplied: dict[str, Any] = {}
            for logical_path, source in artifacts.items():
                normalised = _normalise_logical_path(logical_path)
                if normalised in supplied:
                    raise EvidenceError(f"Duplicate normalized supplied artifact: {normalised!r}")
                supplied[normalised] = source
        elif isinstance(artifacts, (str, os.PathLike)):
            supplied = _artifact_sources_from_root(manifest, artifacts)
        else:
            raise EvidenceError("artifacts must be a mapping or verification root path")

        records = {
            record["path"]: record
            for record in manifest.get("artifacts", [])
            if isinstance(record, Mapping) and "path" in record
        }
        for logical_path, record in records.items():
            if logical_path not in supplied:
                errors.append(f"artifact missing: {logical_path}")
                continue
            try:
                payload = _read_artifact(supplied[logical_path])
            except EvidenceError as exc:
                errors.append(f"artifact unreadable ({logical_path}): {exc}")
                continue
            checked += 1
            actual_hash = sha256_bytes(payload)
            if not hmac.compare_digest(actual_hash, str(record.get("sha256", ""))):
                errors.append(f"artifact SHA-256 mismatch: {logical_path}")
            if len(payload) != record.get("size"):
                errors.append(f"artifact size mismatch: {logical_path}")

        if not allow_extra_artifacts:
            for logical_path in sorted(set(supplied) - set(records), key=_utf16_sort_key):
                errors.append(f"unexpected artifact: {logical_path}")
    except (EvidenceError, TypeError, KeyError) as exc:
        errors.append(f"artifacts: {exc}")

    if expected_commitment is not None:
        if computed_manifest_hash is None:
            errors.append("commitment cannot be checked because the manifest is invalid")
        elif salt is None:
            errors.append("salt is required when expected_commitment is provided")
        else:
            try:
                expected = _parse_bytes32(expected_commitment, label="expected_commitment")
                commitment_record = compute_commitment(
                    bytes.fromhex(computed_manifest_hash), salt=salt
                )
                computed_commitment = commitment_record["commitment"]
                actual = bytes.fromhex(computed_commitment[2:])
                if not hmac.compare_digest(actual, expected):
                    errors.append("salted commitment does not match the expected value")
            except EvidenceError as exc:
                errors.append(f"commitment: {exc}")
    elif salt is not None:
        errors.append("salt was supplied without an expected_commitment")

    ok = not errors
    return {
        "ok": ok,
        "valid": ok,
        "errors": errors,
        "artifacts_checked": checked,
        "manifest_sha256": (
            "0x" + computed_manifest_hash if computed_manifest_hash is not None else None
        ),
        "commitment": computed_commitment,
    }


verify_evidence = verify_manifest


__all__ = [
    "COMMITMENT_SCHEME",
    "CanonicalizationError",
    "CanonicalizationUnavailable",
    "DETERMINISTIC_JSON_V1",
    "EvidenceError",
    "KeccakUnavailable",
    "MANIFEST_SCHEMA",
    "MANIFEST_VERSION",
    "RFC8785",
    "build_manifest",
    "canonical_manifest_bytes",
    "canonicalize_json",
    "commit_manifest",
    "compute_commitment",
    "create_manifest",
    "generate_salt",
    "manifest_sha256",
    "select_canonicalization_algorithm",
    "sha256_bytes",
    "sha256_file",
    "verify_evidence",
    "verify_manifest",
]
