# FaceProof

**Proof of discovery, not proof of identity or truth.**

FaceProof is an end-to-end, consent-first pipeline for HH Goa 2026 Shortlisting
Task 3. It detects and encodes one face, performs a genuine live web search for
public social-media candidates, independently compares the returned face,
captures a deterministic evidence bundle, anchors a privacy-preserving
commitment on an EVM blockchain, and re-verifies the result from the original
files and public chain state.

> **Status:** the offline pipeline, security gates, contract, and a real local
> EVM deploy/anchor/re-verify/tamper path are tested. A valid provider key,
> consented discoverable post, funded testnet wallet, and public deployment are
> still required for a submission-grade live run.

## What the pipeline proves

FaceProof makes four narrow, independently testable claims:

1. A specific model detected and encoded a face from supplied bytes.
2. A named live provider returned a candidate public URL at a recorded time.
3. The client preserved a sanitized provider record, its exact-body digest,
   and particular media and metadata bytes.
4. A wallet committed to that exact evidence no later than an on-chain block.

It does **not** prove a person's legal identity, post authorship, truth,
original publication time, consent, or completeness of web search. Search and
face-similarity scores are investigative candidates and require human review.
When Lens supplies a related label (for example, a public name), FaceProof
records and displays it only as an **unverified web-derived search hint**. The
Task 3 rubric requires a matching post, not automatic legal-name identification.

## Architecture

```text
consented image
  -> YuNet face detection / quality gate
  -> SFace aligned embedding
  -> SerpApi Google Lens live query (cache disabled)
  -> stable social-post permalink filtering
  -> SFace re-match against returned candidate media
  -> public post identity/media capture and validation
  -> explicit human approval of the exact permalink
  -> RFC 8785 evidence manifest + SHA-256
  -> salted Keccak commitment
  -> EvidenceRegistry on Base Sepolia
  -> independent verifier and tamper test
```

The blockchain stores only an opaque 32-byte commitment. Raw face images,
biometric embeddings, usernames, post text, URLs, and API responses remain
off-chain.

## Requirements

- Python 3.11 or newer
- [`uv`](https://docs.astral.sh/uv/) (recommended) or `pip`
- A [SerpApi Google Lens](https://serpapi.com/google-lens-api) account. Its
  free plan currently includes 250 searches per month; no paid search provider
  is required for the demo.
- A clean, tracked Git checkout whose exact commit is recorded for anchored runs
- A dedicated testnet-only EVM wallet for blockchain writes
- Base Sepolia test ETH and a deployed `EvidenceRegistry`

Only use an adult volunteer who explicitly consented and controls or authorized
the public post being searched. Do not use this project to identify strangers,
minors, private people, or people in sensitive contexts.

## Quick start

```powershell
uv sync --python 3.11 --extra dev
Copy-Item .env.example .env
uv run faceproof models download
uv run faceproof doctor
```

Add provider and chain settings to `.env`; never commit that file.
See [`docs/SETUP.md`](docs/SETUP.md) for the complete free-tier key, testnet,
deployment, live-run, and re-verification walkthrough.
For a final demo, set `FACEPROOF_SOURCE_REVISION` to the exact output of
`git rev-parse HEAD`, then require every readiness gate with
`uv run faceproof doctor --demo`.

### Discover, review, then run the full pipeline

Put a consented local image at `samples/consented-person.jpg` (the directory is
Git-ignored). First perform an unanchored discovery run so a human can inspect
the dynamically returned permalink:

```powershell
uv run faceproof run --image .\samples\consented-person.jpg `
  --live --i-have-consent --skip-anchor
```

After reviewing the selected post and evidence directory, run a fresh live
search and approve that exact URL for anchoring. The command fails if the URL is
not returned, does not independently face-match, or cannot be captured as a
real post permalink.

SerpApi Google Lens with cache explicitly disabled:

```powershell
uv run faceproof run --image .\samples\consented-person.jpg `
  --live --i-have-consent `
  --consent-reference "volunteer-a-2026-09" `
  --approve-post-url "https://www.reddit.com/r/example/comments/abc/example"
```

Development without a blockchain write is deliberately explicit:

```powershell
uv run faceproof run --image .\samples\consented-person.jpg `
  --live --i-have-consent --skip-anchor
```

### Re-verify and demonstrate tampering

```powershell
uv run faceproof verify .\evidence\<run-id>
uv run faceproof tamper-demo .\evidence\<run-id>
```

For the strongest independent check, copy the commitment and transaction hash
from the demo record or explorer—not from the evidence directory—and pass them
explicitly:

```powershell
uv run faceproof verify .\evidence\<run-id> `
  --expected-commitment 0x... --expected-tx 0x...
```

The verifier reports artifact integrity, canonical manifest integrity, trusted
chain/registry state, pinned deployed-contract code, transaction/event
consistency, and the optional out-of-band commitment/transaction check. A
tampered artifact must produce a failing verdict.

## Blockchain

The project uses a minimal, immutable, admin-free Solidity registry. It rejects
zero or duplicate commitments and stores the first submitter, block timestamp,
and block number. See [`contracts/README.md`](contracts/README.md) for build,
test, and deployment instructions.

Recommended public deployment:

- Network: Base Sepolia
- Chain ID: `84532`
- Public RPC: `https://sepolia.base.org`
- Explorer: `https://sepolia.basescan.org`
- Contract address: **add after deployment**

After deployment, set `FACEPROOF_CONTRACT_ADDRESS`. Also set
`FACEPROOF_CONTRACT_CODE_HASH` to the Keccak-256 hash of the deployed runtime
bytecode; both are required for anchoring and chain verification. The verifier
never trusts chain ID or registry address supplied by an evidence bundle.

The public RPC is suitable for a demonstration but is rate-limited. Configure
a provider RPC as a backup. During a live demo, distinguish fast Base L2 block
inclusion from later Ethereum L1 batch finality.

## Evidence format

Each `evidence/<run-id>/` directory contains:

- a sanitized parsed provider receipt plus the SHA-256 of the exact HTTP
  response bytes;
- candidate image bytes returned or referenced by the provider;
- Lens web labels, result title/source, and the exact matched-image path/hash,
  with label provenance explicitly marked unverified;
- independently validated public-post metadata and, where exposed, post media;
- `manifest.json`, containing hashes and model/search/match metadata;
- `manifest.canonical.json`, the RFC 8785 canonical bytes;
- `commitment.json`, containing the manifest hash, random salt and commitment;
- `chain-receipt.json` after anchoring.

The chain receipt is intentionally excluded from the pre-anchor manifest to
avoid circular hashing.

The raw provider body itself is not retained because it may echo credentials;
the digest identifies those received bytes but cannot reconstruct them.

```text
manifest_hash = SHA256(JCS(manifest))
commitment = keccak256(ABI.encode(manifest_hash, salt32))
```

## Tests

```powershell
uv run pytest --cov=faceproof --cov-report=term --cov-fail-under=70
uv run ruff check .
uv run ruff format --check .
uv run bandit -q -r src/faceproof
```

Contract tests:

```powershell
Set-Location contracts
forge test -vv
```

### Measured face-verification baseline

The reproducible LFW View 2 run measured **99.20% accuracy on the 4,126
scored pairs**, but the strict one-face/quality policy scored only **68.77% of
all 6,000 pairs**. The more honest all-pair correct-and-scored yield is
**68.22%**. These figures measure 1:1 face verification, not web-search recall
or open-set identification. See [`docs/ACCURACY_VALIDATION.md`](docs/ACCURACY_VALIDATION.md)
for confidence intervals, FAR/FRR, dataset/model hashes, reproduction steps,
and limitations.

Live provider tests are not run in CI because they consume credits and process
biometric data. Unit tests use clearly labelled synthetic responses and never
fall back to those responses from a live run.

## Demo checklist

1. Show the consented input and current time.
2. Show the reviewed permalink, then run the fresh anchor command with `--live`
   and `--approve-post-url`.
3. Show detection, quality result, model fingerprints and embedding fingerprint.
4. Show provider search ID, timestamp and dynamically returned candidates.
5. Show the unverified web label, selected post title/source, exact matched
   image, and independent local face score versus its frozen threshold.
6. Show the evidence commitment and successful Base Sepolia transaction.
7. Open the explorer transaction.
8. Run `faceproof verify` from a new process with the independently copied
   commitment and transaction hash; show a passing result.
9. Run `faceproof tamper-demo` and show the changed copy fail.

Pre-fund the wallet, mask API keys, disable notifications, and prepare more than
one consenting, already-indexed test case. Never present cached or fixture data
as a live search.

## Security and privacy controls

- Explicit consent acknowledgement is required by the CLI.
- Multiple/no-face and low-quality inputs fail closed.
- Provider results are independently re-matched; provider scores are not trusted
  as identity proof.
- Profiles/homepages are rejected: only recognized post permalinks with stable
  IDs are eligible.
- An anchored run requires successful public post-identity capture and explicit
  approval of the exact permalink.
- Remote evidence requires HTTPS and rejects private/local network addresses.
- Every live run verifies the exact pinned YuNet/SFace file sizes and hashes.
- Anchored runs bind a clean Git commit and pinned registry bytecode hash.
- Public page capture never logs in or bypasses access controls.
- API keys and wallet keys are environment-only and excluded by `.gitignore`.
- Only a random-salted commitment is public on-chain.
- Search failure returns `inconclusive`; no hardcoded URL or hidden fixture is
  substituted.

The detailed assumptions, trust boundaries, threats, and residual risks are in
[`docs/THREAT_MODEL.md`](docs/THREAT_MODEL.md).

The judge-facing completion gates and demo sequence are in
[`docs/SUBMISSION_PLAN.md`](docs/SUBMISSION_PLAN.md). A sourced technology,
product, paper, and comparable-project review is in
[`docs/RESEARCH.md`](docs/RESEARCH.md).

## Known limitations

- Web indexes are incomplete and change continuously. Private, login-only,
  newly published, or robots-excluded posts may not be found.
- Face similarity can produce false matches and false non-matches, especially
  with poor image quality, age/pose changes, occlusion, or demographic bias.
- A browser/API capture proves what this client recorded, not that the platform
  cryptographically signed the content.
- Some platforms expose post identity through oEmbed but not the original media;
  unanchored discovery records that weaker provider-associated linkage, while
  anchored submission runs fail closed unless captured post media is
  independently re-matched. X commonly falls into this category; prefer a
  consented public YouTube, Reddit, or Bluesky result for the anchored demo.
- A testnet can be reset or retired and is not permanent or legal-grade storage.
- Base Sepolia proves public inclusion and integrity, not the truth of off-chain
  claims.
- SerpApi is a third-party Google Lens scraper, not an official Google API.
- Raw evidence may contain personal or copyrighted data and must be retained,
  shared, and deleted under an explicit policy.

## Licence

Project source is MIT licensed. Third-party face models and services retain
their own licences and terms. The selected OpenCV YuNet and SFace model
directories currently state MIT and Apache-2.0 licences respectively; model
files are downloaded rather than redistributed, and their SHA-256 fingerprints
are recorded in every run.
