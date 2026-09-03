# FaceProof

**Proof of discovery, not proof of identity or truth.**

FaceProof is an end-to-end, consent-first pipeline for HH Goa 2026 Shortlisting
Task 3. It detects and encodes one face, performs a genuine live web search for
public social-media candidates, independently compares the returned face,
captures a deterministic evidence bundle, anchors a privacy-preserving
commitment on an EVM blockchain, and re-verifies the result from the original
files and public chain state.

> **Status:** active build. FaceCheck.ID and SerpApi Google Lens adapters, local
> face matching, deterministic evidence, a Base Sepolia registry, and the
> verifier are included. A valid provider key and funded testnet wallet are
> required for a real end-to-end run.

## What the pipeline proves

FaceProof makes four narrow, independently testable claims:

1. A specific model detected and encoded a face from supplied bytes.
2. A named live provider returned a candidate public URL at a recorded time.
3. The client preserved particular response, media, and metadata bytes.
4. A wallet committed to that exact evidence no later than an on-chain block.

It does **not** prove a person's legal identity, post authorship, truth,
original publication time, consent, or completeness of web search. Search and
face-similarity scores are investigative candidates and require human review.

## Architecture

```text
consented image
  -> YuNet face detection / quality gate
  -> SFace aligned embedding
  -> FaceCheck.ID or SerpApi Lens live query
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
- One search provider account:
  - [FaceCheck.ID API](https://facecheck.id/en/Face-Search/API), recommended for
    a different photo of the same person
  - [SerpApi Google Lens](https://serpapi.com/google-lens-api), recommended for
    exact/cropped/reposted-image discovery
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

### Discover, review, then run the full pipeline

Put a consented local image at `samples/consented-person.jpg` (the directory is
Git-ignored). First perform an unanchored discovery run so a human can inspect
the dynamically returned permalink:

```powershell
uv run faceproof run --image .\samples\consented-person.jpg `
  --provider serpapi --live --i-have-consent --skip-anchor
```

After reviewing the selected post and evidence directory, run a fresh live
search and approve that exact URL for anchoring. The command fails if the URL is
not returned, does not independently face-match, or cannot be captured as a
real post permalink.

FaceCheck production search:

```powershell
uv run faceproof run --image .\samples\consented-person.jpg `
  --provider facecheck --live --i-have-consent `
  --consent-reference "volunteer-a-2026-09" `
  --approve-post-url "https://x.com/example/status/123"
```

SerpApi Google Lens with cache explicitly disabled:

```powershell
uv run faceproof run --image .\samples\consented-person.jpg `
  --provider serpapi --live --i-have-consent `
  --consent-reference "volunteer-a-2026-09" `
  --approve-post-url "https://www.reddit.com/r/example/comments/abc/example"
```

Development without a blockchain write is deliberately explicit:

```powershell
uv run faceproof run --image .\samples\consented-person.jpg `
  --provider serpapi --live --i-have-consent --skip-anchor
```

FaceCheck's reduced testing index can be selected with
`--facecheck-testing`, but vendor documentation states that its results are not
meaningful. It is not acceptable evidence of the required live search.

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
chain/registry state, deployed contract code (when pinned), transaction/event
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
bytecode so verification pins both the address and its code. The verifier never
trusts chain ID or registry address supplied by an evidence bundle.

The public RPC is suitable for a demonstration but is rate-limited. Configure
a provider RPC as a backup. During a live demo, distinguish fast Base L2 block
inclusion from later Ethereum L1 batch finality.

## Evidence format

Each `evidence/<run-id>/` directory contains:

- the exact parsed/raw provider search receipt;
- candidate image bytes returned or referenced by the provider;
- independently validated public-post metadata and, where exposed, post media;
- `manifest.json`, containing hashes and model/search/match metadata;
- `manifest.canonical.json`, the RFC 8785 canonical bytes;
- `commitment.json`, containing the manifest hash, random salt and commitment;
- `chain-receipt.json` after anchoring.

The chain receipt is intentionally excluded from the pre-anchor manifest to
avoid circular hashing.

```text
manifest_hash = SHA256(JCS(manifest))
commitment = keccak256(ABI.encode(manifest_hash, salt32))
```

## Tests

```powershell
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

Contract tests:

```powershell
Set-Location contracts
forge test -vv
```

Live provider tests are not run in CI because they consume credits and process
biometric data. Unit tests use clearly labelled synthetic responses and never
fall back to those responses from a live run.

## Demo checklist

1. Show the consented input and current time.
2. Show the reviewed permalink, then run the fresh anchor command with `--live`
   and `--approve-post-url`.
3. Show detection, quality result, model fingerprints and embedding fingerprint.
4. Show provider search ID, timestamp and dynamically returned candidates.
5. Show the selected public social URL and independent local face score.
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
- Candidate downloads reject private/local network addresses.
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
  those runs explicitly record weaker provider-associated linkage. Prefer a
  result whose captured post media is independently re-matched.
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
