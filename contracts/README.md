# EvidenceRegistry

`EvidenceRegistry` is a minimal, immutable EVM registry for 32-byte evidence
commitments. A commitment can be a canonical manifest hash or a Merkle root.
The first successful anchor stores the submitting address, block timestamp, and
block number; zero and duplicate commitments are rejected.

There is no owner, administrator, upgrade path, deletion, or overwrite method.

## Interface

- `anchor(bytes32 commitment)` records a new commitment and emits the indexed
  `EvidenceAnchored` event.
- `verify(bytes32 commitment)` reports whether it exists.
- `getRecord(bytes32 commitment)` returns its immutable metadata and reverts if
  it is unknown.

## Test

Install [Foundry](https://getfoundry.sh/getting-started/installation/), then run:

```powershell
cd contracts
forge test -vv
```

The tests are self-contained and do not require `forge-std` or other packages.

## Deploy and query

```powershell
forge create src/EvidenceRegistry.sol:EvidenceRegistry `
  --rpc-url $env:RPC_URL `
  --private-key $env:DEPLOYER_PRIVATE_KEY `
  --broadcast

# Pin the exact deployed runtime bytecode in FaceProof verification.
$runtimeCode = cast code $env:REGISTRY_ADDRESS --rpc-url $env:RPC_URL
cast keccak $runtimeCode

cast send $env:REGISTRY_ADDRESS "anchor(bytes32)" $env:COMMITMENT `
  --rpc-url $env:RPC_URL --private-key $env:DEPLOYER_PRIVATE_KEY

cast call $env:REGISTRY_ADDRESS "verify(bytes32)(bool)" $env:COMMITMENT `
  --rpc-url $env:RPC_URL
```

Never commit a private key. For a public demo, deploy the same bytecode to an
EVM testnet and publish its chain ID, contract address, transaction hash, and
verified source code. Put the `cast keccak` result in
`FACEPROOF_CONTRACT_CODE_HASH`; this prevents an evidence bundle from swapping
the verifier to a different contract implementation.

## Claim boundary

The registry proves only that a particular 32-byte value was submitted no later
than its containing block. It does not prove who created the underlying post,
that a face match is correct, or that the evidence is truthful. Block timestamps
are consensus metadata, not precision capture clocks. Keep personal or biometric
data off-chain and anchor only a carefully defined commitment.
