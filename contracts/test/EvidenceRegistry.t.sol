// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {EvidenceRegistry} from "../src/EvidenceRegistry.sol";

interface Vm {
    struct Log {
        bytes32[] topics;
        bytes data;
        address emitter;
    }

    function expectRevert(bytes4 revertData) external;
    function expectRevert(bytes calldata revertData) external;
    function getRecordedLogs() external returns (Log[] memory);
    function prank(address msgSender) external;
    function recordLogs() external;
    function roll(uint256 newHeight) external;
    function warp(uint256 newTimestamp) external;
}

contract EvidenceRegistryTest {
    Vm private constant vm =
        Vm(address(uint160(uint256(keccak256("hevm cheat code")))));

    EvidenceRegistry private registry;

    address private constant ALICE = address(0xA11CE);
    bytes32 private constant COMMITMENT =
        0xf747a9cb4ae2e230e498c22b2e9bdaedf4821b216ca3898e94d4c8e4b1e6a77a;

    function setUp() public {
        registry = new EvidenceRegistry();
    }

    function testAnchorStoresImmutableMetadata() public {
        vm.warp(1_800_000_000);
        vm.roll(123_456);
        vm.prank(ALICE);
        registry.anchor(COMMITMENT);

        EvidenceRegistry.Record memory record = registry.getRecord(COMMITMENT);
        _assertEq(record.submitter, ALICE, "wrong submitter");
        _assertEq(record.timestamp, 1_800_000_000, "wrong timestamp");
        _assertEq(record.blockNumber, 123_456, "wrong block number");
        _assertTrue(registry.verify(COMMITMENT), "commitment not verified");
    }

    function testRejectsZeroCommitment() public {
        vm.expectRevert(EvidenceRegistry.ZeroCommitment.selector);
        registry.anchor(bytes32(0));
    }

    function testRejectsDuplicateCommitmentAndPreservesFirstSubmitter() public {
        vm.prank(ALICE);
        registry.anchor(COMMITMENT);

        vm.expectRevert(
            abi.encodeWithSelector(
                EvidenceRegistry.CommitmentAlreadyAnchored.selector,
                COMMITMENT
            )
        );
        registry.anchor(COMMITMENT);

        EvidenceRegistry.Record memory record = registry.getRecord(COMMITMENT);
        _assertEq(record.submitter, ALICE, "first submitter was changed");
    }

    function testUnknownCommitmentDoesNotVerify() public view {
        _assertTrue(!registry.verify(COMMITMENT), "unknown commitment verified");
    }

    function testGetUnknownCommitmentReverts() public {
        vm.expectRevert(
            abi.encodeWithSelector(
                EvidenceRegistry.CommitmentNotFound.selector,
                COMMITMENT
            )
        );
        registry.getRecord(COMMITMENT);
    }

    function testEventIndexesCommitmentAndSubmitter() public {
        vm.recordLogs();
        vm.prank(ALICE);
        registry.anchor(COMMITMENT);
        Vm.Log[] memory logs = vm.getRecordedLogs();

        _assertEq(logs.length, 1, "unexpected event count");
        _assertEq(logs[0].emitter, address(registry), "wrong emitter");
        _assertEq(
            logs[0].topics[0],
            keccak256("EvidenceAnchored(bytes32,address,uint256,uint256)"),
            "wrong event signature"
        );
        _assertEq(logs[0].topics[1], COMMITMENT, "commitment not indexed");
        _assertEq(
            logs[0].topics[2],
            bytes32(uint256(uint160(ALICE))),
            "submitter not indexed"
        );

        (uint256 timestamp, uint256 blockNumber) = abi.decode(
            logs[0].data,
            (uint256, uint256)
        );
        _assertEq(timestamp, block.timestamp, "wrong event timestamp");
        _assertEq(blockNumber, block.number, "wrong event block number");
    }

    function _assertTrue(bool condition, string memory message) private pure {
        require(condition, message);
    }

    function _assertEq(
        address actual,
        address expected,
        string memory message
    ) private pure {
        require(actual == expected, message);
    }

    function _assertEq(
        bytes32 actual,
        bytes32 expected,
        string memory message
    ) private pure {
        require(actual == expected, message);
    }

    function _assertEq(
        uint256 actual,
        uint256 expected,
        string memory message
    ) private pure {
        require(actual == expected, message);
    }
}
