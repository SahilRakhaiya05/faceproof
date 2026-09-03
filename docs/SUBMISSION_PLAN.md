# HH Goa Task 3 submission plan

No plan can guarantee a win, but this sequence maximizes demonstrability,
technical integrity, and judge confidence.

## Rubric-to-proof matrix

| Requirement | Implementation | Evidence shown to judges |
|---|---|---|
| Face identification | OpenCV YuNet detection/quality gate and SFace embedding, with pinned model hashes | Detection preview, face geometry, quality metrics, model fingerprints, local re-match score |
| Genuine web/social search | Live SerpApi Google Lens direct-image query with cache disabled; no runtime fixture fallback | Provider request/search ID, capture time, sanitized parsed response, exact-body SHA-256, dynamically returned post candidates |
| Matching social post | Platform-specific permalink IDs, provider-image re-match, public post identity capture, post-media re-match when exposed | Stable permalink, post ID, capture artifact, linkage level, selected/captured media hashes |
| Blockchain upload | Salted commitment to the canonical evidence manifest, written to `EvidenceRegistry` | Base Sepolia contract, transaction, block, sender, emitted event and explorer page |
| Re-verification | Local artifact re-hash plus trusted-chain, bytecode, transaction, receipt, event and registry-state checks | `faceproof verify` PASS from a fresh process; one-byte tamper demo FAIL |
| GitHub repository | Reproducible Python/Solidity source, lockfile, CI, tests and documentation | Public repository, green CI, tagged release and exact demo commit |

## Completion gates

1. **Consent and demo subject** — obtain written, revocable consent from an
   adult volunteer. Use a public post they control or expressly authorize.
   Keep the consent document private and record only a non-sensitive reference.
2. **Search reliability** — test SerpApi Lens on two or three consented,
   already-indexed posts using the same image, a crop, and a compressed copy.
   Record search success separately from local face-verification accuracy.
3. **Threshold calibration** — retain the reproducible aggregate LFW baseline,
   then create a small consented in-domain validation set with positive and
   negative pairs under realistic compression/crop conditions. Freeze the
   chosen threshold and publish aggregate results, not face images.
4. **Public chain deployment** — deploy the immutable registry to Base
   Sepolia, verify the source on BaseScan, publish chain ID/address/runtime-code
   hash, and fund a dedicated testnet wallet. Keep a second RPC endpoint ready.
5. **Two-pass review** — run unanchored discovery, inspect the post, then run a
   fresh live search with `--approve-post-url`. Prefer a result labelled
   `captured-post-media-rematched`; anchored runs reject weaker
   provider-associated linkage. YouTube, Reddit, and Bluesky are the most
   practical prepared-demo targets; X often confirms identity through oEmbed
   without exposing eligible post media.
6. **Independent verification** — copy the commitment and transaction hash to
   the submission notes/QR. Verify from a clean process using those out-of-band
   values, then run the tamper demo.
7. **Publication** — push the exact tested commit to GitHub, enable CI, add the
   deployed address and one privacy-safe demo record to the README, and tag the
   submitted version.

## Five-minute demo

1. State the narrow claim: proof that a live provider returned a face-matched
   public post and that the captured evidence has not changed—not legal identity
   or truth.
2. Show consent scope, the input image, current time, Git commit and green CI.
3. Run the fresh approved live pipeline; point out the provider search ID and
   absence of hardcoded candidates.
4. Open the returned permalink and show the independent local face score and
   linkage level.
5. Open the BaseScan transaction/event and compare its commitment with the CLI.
6. In a new process, verify with the independently copied commitment/tx hash.
7. Change one byte through `tamper-demo` and show the expected failure.
8. Close with limitations and the consent/privacy controls.

## Remaining external inputs

- a SerpApi key with free-plan quota remaining;
- a consented test image and a discoverable public post;
- a funded Base Sepolia wallet and deployed registry address;
- approval to create/publish the repository under the owner's GitHub account.

These are intentionally not hardcoded or fabricated by the project.
