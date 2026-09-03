// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

/// @title EvidenceRegistry
/// @notice Anchors immutable 32-byte evidence commitments to an EVM chain.
/// @dev The contract records existence and submitter metadata; it does not
///      establish the truth, authorship, or ownership of the underlying data.
contract EvidenceRegistry {
    struct Record {
        address submitter;
        uint256 timestamp;
        uint256 blockNumber;
    }

    error ZeroCommitment();
    error CommitmentAlreadyAnchored(bytes32 commitment);
    error CommitmentNotFound(bytes32 commitment);

    event EvidenceAnchored(
        bytes32 indexed commitment,
        address indexed submitter,
        uint256 timestamp,
        uint256 blockNumber
    );

    mapping(bytes32 commitment => Record record) private _records;

    /// @notice Permanently records a previously unseen evidence commitment.
    /// @param commitment A non-zero hash or Merkle root computed off-chain.
    function anchor(bytes32 commitment) external {
        if (commitment == bytes32(0)) revert ZeroCommitment();
        if (_records[commitment].submitter != address(0)) {
            revert CommitmentAlreadyAnchored(commitment);
        }

        Record memory record = Record({
            submitter: msg.sender, timestamp: block.timestamp, blockNumber: block.number
        });

        _records[commitment] = record;
        emit EvidenceAnchored(
            commitment, record.submitter, record.timestamp, record.blockNumber
        );
    }

    /// @notice Returns true when a commitment has been anchored.
    function verify(bytes32 commitment) external view returns (bool) {
        return _records[commitment].submitter != address(0);
    }

    /// @notice Returns the immutable metadata for an anchored commitment.
    /// @dev Reverts for an unknown commitment to avoid returning an ambiguous
    ///      all-zero record.
    function getRecord(bytes32 commitment) external view returns (Record memory) {
        Record memory record = _records[commitment];
        if (record.submitter == address(0)) {
            revert CommitmentNotFound(commitment);
        }
        return record;
    }
}
