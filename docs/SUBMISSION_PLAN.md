# HH Goa Task 3 submission plan

No plan can guarantee a win, but this sequence maximizes demonstrability,
technical integrity, and judge confidence.

## Rubric-to-proof matrix

| Requirement | Implementation | Evidence shown to judges |
|---|---|---|
| Face identification | OpenCV YuNet detection/quality gate and SFace embedding, with pinned model hashes | Detection preview, face geometry, quality metrics, model fingerprints, local re-match score |
| Genuine web/social search | Live source choice: SerpApi Lens direct-image query or runtime consented Bluesky public author-feed scan; no runtime fixture fallback | Lens provider search IDs, or Bluesky AT URI/CIDs plus locally derived response-hash capture IDs/permalink; capture time, sanitized parsed response, exact-body SHA-256, dynamically returned candidates |
| Matching social post | Platform-specific permalink IDs, provider-image re-match, public post identity capture, post-media re-match when exposed | Stable permalink, post ID, capture artifact, linkage level, selected/captured media hashes |
| Blockchain upload | Salted commitment to the canonical evidence manifest, written to `EvidenceRegistry` on local Anvil or Base Sepolia | Contract, transaction, block, sender, emitted event and verified registry read-back; explorer page when public |
| Re-verification | Local artifact re-hash plus trusted-chain, bytecode, transaction, receipt, event and registry-state checks | Console PASS while disposable `local-demo` is running, or `faceproof verify` PASS from a fresh process against a persistent configured chain; one-byte tamper demo FAIL |
| GitHub repository | Reproducible Python/Solidity source, lockfile, CI, tests and documentation | Public repository, green CI, tagged release and exact demo commit |

## Completion gates

1. **Consent and demo subject** — obtain written, revocable consent from an
   adult volunteer. Use a public post they control or expressly authorize.
   Keep the consent document private and record only a non-sensitive reference.
   The evidence bundle still contains plaintext query/crop/candidate media: set
   an access, encryption, sharing, retention, and deletion policy before the
   run. If using Lens, separately disclose that the image derivative is sent to
   SerpApi/Google, standard SerpApi data is retained for 31 days, and its
   no-retention ZeroTrace mode is Enterprise-only.
2. **Search reliability** — prepare a live post on the volunteer's public
   Bluesky feed for the deterministic no-key path, plus two consented
   already-indexed Lens cases if quota is available. Record discovery success
   separately from local face-verification accuracy.
3. **Threshold calibration** — retain the reproducible aggregate LFW baseline,
   then create a small consented in-domain validation set with positive and
   negative pairs under realistic compression/crop conditions. Freeze the
   chosen threshold and publish aggregate results, not face images.
4. **Public-chain upgrade (strongly recommended)** — the local Anvil route
   satisfies the stated blockchain requirement. For stronger durable proof,
   deploy the immutable registry to Base Sepolia, publish its chain ID, address,
   runtime-code hash and transaction, and fund a dedicated testnet wallet. Keep
   a second RPC endpoint ready. BaseScan source verification is optional; claim
   it only after the actual address shows matching verified source/compiler
   inputs, not merely a successful transaction.
5. **Two-pass review** — run unanchored discovery, inspect the post and captured
   content identity, then run a fresh provider request with
   `--approve-post-url`. Lens sends
   `no_cache=true`; Bluesky refetches its public author feed without claiming an
   equivalent cache-disable parameter. Prefer a result labelled
   `captured-post-media-rematched`; anchored runs reject weaker
   provider-associated linkage and reject content changed behind the same URL.
   YouTube, Reddit, and Bluesky are the most
   practical prepared-demo targets; X often confirms identity through oEmbed
   without exposing eligible post media.
6. **Independent verification** — copy the commitment and transaction hash to
   the submission notes/QR. For Base Sepolia or another persistent configured
   chain, verify from a clean process using those out-of-band values. For
   `local-demo`, run Verify and Tamper test in the Console before stopping its
   ephemeral Anvil process.
7. **Publication** — push the exact tested commit to GitHub, enable CI, add the
   deployed address and one privacy-safe demo record to the README, and tag the
   submitted version.

## Five-minute demo

1. State the narrow claim: proof that the client recorded live source data from
   which it evaluated a face-matched public post and that the captured evidence
   has not changed—not legal identity or truth. Lens returns result URLs;
   Bluesky returns AT records/CIDs and FaceProof derives the web permalink.
2. Show consent scope, the input image, current time, Git commit and green CI.
3. Run the fresh approved live pipeline; point out either the Lens provider
   search ID or Bluesky AT URI/CIDs and response-hash-derived capture IDs, plus
   the absence of hardcoded candidates.
4. Open the returned permalink and show the independent local face score and
   linkage level.
5. Show the transaction/event and registry read-back; open BaseScan when using
   a public deployment, without claiming source verification unless displayed.
6. With `local-demo`, use Console Verify while Anvil is running. With Base
   Sepolia/persistent Anvil, use a new process and independently copied
   commitment/transaction hash.
7. Change one byte through `tamper-demo` (or the Console action) and show the
   expected failure.
8. Close with limitations and the consent/privacy controls.

## Remaining external inputs

- a consented test image and either a public Bluesky actor or an indexed post;
- a SerpApi key with free-plan quota remaining for at least one consented Lens
  receipt if strict face-driven, indexed-web proof is required by the judges;
- optionally, for durable public proof, a funded Base Sepolia wallet and
  deployed registry address;
- approval to create/publish the repository under the owner's GitHub account.

These are intentionally not hardcoded or fabricated by the project.

## Release-evidence gate

Before submission, record the exact `uv` 0.11.8 locked install, Python tests and
coverage, Ruff, Bandit, `pip-audit`, package build, `forge fmt --check`,
`forge test -vv`, and the opt-in real-Anvil integration test. The copyable
commands are in [SETUP.md](SETUP.md#6-reproduce-the-release-checks). Do not
describe mocked unit tests as blockchain integration evidence.
