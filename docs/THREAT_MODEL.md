# FaceProof threat model

FaceProof is an evidence-integrity demonstration, not an autonomous identity
system. Its design assumes the local machine is controlled by the operator at
capture time and makes that trust boundary explicit.

## Protected assets

- query images and derived biometric embeddings;
- provider API tokens and the testnet signing key;
- provider responses, discovered media, post metadata, and timestamps;
- the canonical manifest, salt, blockchain commitment, and transaction receipt.

## Trust boundaries

1. **Operator to local pipeline:** consent and the input bytes are operator
   assertions. The software cannot independently prove consent.
2. **Pipeline to search provider:** HTTPS protects transport, but the provider
   does not cryptographically sign its result. The parsed response is sanitized
   before storage; a SHA-256 digest preserves the identity of the exact HTTP
   response bytes without retaining secrets copied into that body.
3. **Pipeline to social platform:** public metadata capture records what this
   client observed. It does not prove the platform authored or signed the page.
4. **Pipeline to blockchain:** the transaction publicly binds the submitting
   wallet to one opaque commitment. It does not validate any off-chain claim.
5. **Verifier to RPC:** chain ID and registry address come from trusted local
   configuration, never from the bundle. Pinned deployed code, exact
   transaction input/sender/target/value, canonical block, target-address
   event, saved receipt, and current contract state are cross-checked.
6. **Reviewer to anchor:** an unanchored discovery run may rank candidates, but
   a write requires the human-approved exact permalink to reappear and pass all
   checks in a fresh live run.

## Principal threats and controls

| Threat | Control | Residual risk |
|---|---|---|
| Hardcoded or cached search result | No fixture provider in the runtime CLI; explicit `--live`; SerpApi `no_cache=true`; provider search ID, sanitized parsed response, and exact-body digest retained | Provider itself may cache or change behavior |
| False facial match | Independent local SFace comparison, recorded winning face/geometry, threshold and model hashes, explicit permalink approval, inconclusive state | Thresholds and models still have false matches and demographic/quality effects |
| Profile or login page presented as a post | Platform-specific permalink/ID validation, redirect and metadata identity checks, successful public capture required before anchoring | X oEmbed can confirm post identity without exposing the original media bytes |
| Provider thumbnail misattributed to a URL | Captured post media is downloaded and independently re-matched when available; weaker provider-associated linkage is labelled | Some platform APIs do not expose media without privileged access |
| Evidence changed after discovery | SHA-256 every artifact, RFC 8785 manifest, salted Keccak commitment, immutable contract entry | Malicious bytes prepared before anchoring remain malicious but unchanged |
| Private-network fetch/SSRF or transport downgrade | HTTPS-only remote-evidence checks, credential rejection, DNS address checks, redirect revalidation and response-size limits | DNS rebinding cannot be completely eliminated without a controlled egress proxy |
| API or wallet secret disclosure | Environment-only configuration, `.env` ignored, secrets never included in evidence or console output | Screen-recording or host compromise can still leak secrets |
| Public biometric/PII leakage | Raw evidence remains local; only an opaque salted commitment is on-chain; embeddings are not persisted | Sharing the evidence directory or salt may reveal personal data |
| Blockchain/network confusion | Expected chain ID, exact trusted contract address, required runtime-code hash, and trusted confirmation depth checked before writes and reads; bundle claims are not trust inputs | Public testnets and RPC services can be unavailable, reorganize, or be retired |
| Replacement bundle freshly anchored by an attacker | Verifier accepts an out-of-band expected commitment and transaction hash; receipt/contract/event fields are cross-checked | An operator who omits the out-of-band values proves inclusion, but not that this is the previously published bundle |
| Duplicate or overwritten record | Registry rejects zero and duplicate commitments and has no owner, delete, or upgrade path | A different salted commitment can still be submitted for the same content |
| Timestamp overclaim | UI describes block inclusion as “no later than this block,” not original post time | Block timestamps are consensus metadata and not precision clocks |

## Out of scope

- determining a person's legal identity;
- searching private/login-only content or bypassing platform controls;
- proving authorship, truth, ownership, copyright status, or consent;
- exhaustive web coverage;
- legal-grade long-term preservation on a public testnet.
