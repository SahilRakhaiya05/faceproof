# FaceProof

**Proof of discovery, not proof of identity or truth.**

FaceProof is an end-to-end, consent-first pipeline for HH Goa 2026 Shortlisting
Task 3. It detects and encodes one face, performs a genuine live web search for
public social-media candidates, independently compares the returned face,
captures a deterministic evidence bundle, anchors a privacy-preserving
commitment on an EVM blockchain, and re-verifies the result from the original
files and configured chain state.

> **Status:** the offline pipeline, security gates, contract, and a real local
> EVM deploy/anchor/re-verify/tamper path are tested. To complete the final live
> demo record, use a consented indexed post or the volunteer's public Bluesky
> feed and preserve its end-to-end receipt. Base Sepolia is optional but
> provides a stronger durable, publicly queryable proof than the rubric-valid
> local chain.

## What the pipeline proves

FaceProof makes four narrow, auditable claims:

1. A specific model detected and encoded a face from supplied bytes.
2. The client recorded newly fetched HTTPS responses from the configured live
   source at a recorded time. Lens responses contain result URLs; Bluesky
   responses contain AT records and asset CIDs, from which FaceProof derives a
   human-readable `bsky.app` display permalink locally. The AT URI is the
   canonical protocol identifier.
3. The client preserved sanitized response records, exact-body digests, and the
   particular media and metadata bytes used in the decision.
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
  -> live source choice
       -> SerpApi Lens: full-image exact + face-crop visual query
       -> Bluesky: runtime consented author-feed scan (no key/query upload)
  -> stable social-post permalink filtering
  -> SFace re-match against returned candidate media
  -> public post identity/media capture and validation
  -> explicit human approval of the exact permalink and captured content version
  -> RFC 8785 evidence manifest + SHA-256
  -> salted Keccak commitment
  -> EvidenceRegistry on local Anvil or Base Sepolia
  -> independent verifier and tamper test
```

The blockchain stores only an opaque 32-byte commitment. Raw face images,
usernames, post text, URLs, and API responses remain off-chain. However, a run
directory deliberately retains plaintext copies of the query image, detected
face preview/crop, and candidate/post media so it can be re-verified. Raw
embedding vectors are not persisted; only their dimensions and SHA-256
fingerprints are recorded. Treat every `evidence/` bundle as sensitive: keep it
access-controlled, encrypt it for storage/transfer, and delete it on the agreed
retention date.

### Search sources, modes, and platform capability

The local console and CLI expose two genuine, non-hardcoded discovery paths:

- **Bluesky public feed (zero-key):** scans `posts_with_media` for a consented
  handle or DID supplied at run time. It fetches at most two pages of 50 feed
  entries and emits at most 100 strictly CID-bound image candidates. The
  user-selected 1–20 `max-candidates` limit counts unique post permalinks; once
  a Bluesky post is admitted, every distinct CID-bound image on it remains
  eligible for local matching. AT URI, post CID, image CID, and response digests
  are bound into the evidence; the human-facing
  `bsky.app` permalink is derived locally from the AT record. Selected media is
  recaptured through `getPosts`, with a strict `getPostThread` fallback for
  transient AppView 5xx/transport failures, and the query portrait is never
  transmitted to Bluesky. This is the deterministic judge-demo path, not a
  network-wide person search.
- **SerpApi Google Lens (free quota):** searches the indexed web. **Standard**
  sends one `all` request. **Deep** sends `exact_matches` using the full image
  and `visual_matches` using the detected face crop. Deep uses two Lens search
  credits; Standard uses one. Distinct full-image and thumbnail references for
  the same post are evaluated separately so a useful face is not discarded by
  URL de-duplication.

The Lens image upload and account/quota check do not consume a Lens search
credit. Neither source guarantees a match: the content must actually be public
and present in the chosen source. A completed search with no eligible candidate,
no local threshold pass, or no independently capturable matching post is an
honest `INCONCLUSIVE` run. Authentication, quota, transport, malformed-response,
input/quality, configuration, and blockchain errors at their respective stages
are hard failures. A candidate-specific media download/capture rejection is
recorded and evaluation continues; it becomes inconclusive only if no eligible
verified post remains. No outcome triggers a fixture fallback.

The privacy trade-off differs materially. The zero-key Bluesky route does not
receive the query portrait. Lens uploads a metadata-stripped derivative to
SerpApi/Google; SerpApi's standard policy says search data is retained for 31
days, while its no-retention ZeroTrace mode is Enterprise-only. A ten-minute
`image_id` expiry is not a promise that every uploaded byte is deleted then.
Obtain consent for this third-party processing before choosing Lens.

### Quick photo-copy search

The local console also has a one-upload path for the Task 3 “find a real
matching post” demonstration. It sends the complete, metadata-stripped photo
to one live Google Lens `all` search, retains every unique HTTPS page reference
returned by the provider, and checks up to 24 downloadable images as whole
photographs. The UI labels each retained reference as confirmed, unconfirmed,
unavailable, or not checked. The matcher reports a 0–100 whole-photo similarity
index; that number is not an identity probability and the workflow does not
infer a person or account owner. The checked-image cap means the result does
not claim to cover every page on the internet.

The form requires one explicit authorization checkbox before it sends the
metadata-stripped image to SerpApi or runs the local face encoder.

When one or more copies pass, the workflow writes the canonical evidence
digest to `evidence/.photo-copies/chain.sqlite3`, a local append-only SHA-256
demonstration chain. The UI can independently re-verify the artifact hashes,
receipt, and complete chain, run a one-byte tamper check, and export a bundle.
The local chain is suitable for a screen-recorded demo; it is not a public
blockchain or a trusted timestamp service. Set `SERPAPI_API_KEY` before using
the quick path. If no image copy passes, the run stays `no-copies` and no block
is created.

X, Reddit, YouTube, and Bluesky have supported public post-capture paths and can
complete the post-validation stage when their public response supplies the
required permalink and media evidence. LinkedIn, Instagram, Facebook, and
TikTok are **discovery leads only** because FaceProof deliberately does not log
in, scrape around access controls, or automate their restricted pages. Results
from those four platforms cannot satisfy the matching-post requirement or be
anchored by this implementation.

LinkedIn profile checking is a separate, explicit opt-in. It evaluates only a
Lens-returned public profile thumbnail as an investigative lead. A profile lead
is never treated as identity proof, a matching social-media post, or
anchor-eligible evidence.

## Requirements

- Python 3.11 or newer
- [`uv`](https://docs.astral.sh/uv/) **0.11.8** (enforced by `pyproject.toml`)
- No search API key for the Bluesky author-feed path.
- Optionally, a [SerpApi Google Lens](https://serpapi.com/google-lens-api)
  account for broader web discovery. Its free-plan quota is displayed before a
  run; a paid face-search product is not required.
- A clean, tracked Git checkout whose exact commit is recorded for anchored runs
- For the zero-cost local demo: Foundry (`forge` and `anvil`); no wallet or
  faucet is needed
- For a public proof: a dedicated testnet-only EVM wallet, Base Sepolia test
  ETH, and a deployed `EvidenceRegistry`

Only use an adult volunteer who explicitly consented and controls or authorized
the public post being searched. Do not use this project to identify strangers,
minors, private people, or people in sensitive contexts.

## Quick start

```powershell
uv --version
uv sync --locked --python 3.11 --extra dev
if (-not (Test-Path -LiteralPath .env)) {
  Copy-Item -LiteralPath .env.example -Destination .env
}
uv run faceproof models download
uv run faceproof doctor
```

The conditional copy intentionally never overwrites an existing `.env`.

The zero-key Bluesky plus local-chain path needs no secret in `.env`. Add
SerpApi or public-chain settings only for those optional routes; never commit
that file. See [`docs/SETUP.md`](docs/SETUP.md) for both demo paths.
For a Base Sepolia demo, set `FACEPROOF_SOURCE_REVISION` to the exact output of
`git rev-parse HEAD`, then require every persistent-chain readiness gate with
`uv run faceproof doctor --demo`. The zero-cost `local-demo` launcher instead
checks the clean revision and creates/verifies its disposable chain itself; a
normal `uv run faceproof doctor` is sufficient before launching it.

### Local judge console

The console uses a compact terminal workspace with connection diagnostics,
an event output panel, and a local evidence archive. See the
[terminal and feasibility review](docs/TERMINAL_REVIEW.md) for the September 5
repository comparison, verified checks, and remaining live-demo limitations.

Launch the polished local workflow from the repository root:

```powershell
uv run faceproof web
```

The console opens at `http://127.0.0.1:8787` and binds only to this computer.
Do not proxy it or expose it to a LAN or the public internet. It guides the
operator through consent, a zero-credit local face preflight, source selection,
live search, candidate review, evidence verification, download, and tamper
test.

For the simplest Task 3 demonstration, open **Photo copies** at the top of the
console and upload one JPEG, PNG, or WebP. With `SERPAPI_API_KEY` configured,
the app runs one live Google Lens `all` search, shows every unique HTTPS page
reference returned, compares up to 24 downloadable images as complete photos,
and anchors only confirmed copies to the local simulated SHA-256 chain. This
path gives transparent links and scores without claiming that a page belongs
to the person in the uploaded image.

Anchoring in the console is intentionally a two-pass operation:

1. Run an unanchored live discovery and inspect the dynamically returned post.
2. Select **Review & prepare anchor** for that exact permalink and captured
   content/media identity.
3. Re-acknowledge the irreversible commitment and run a second fresh provider
   request. Lens explicitly sends `no_cache=true`; Bluesky refetches the public
   author feed and does not claim an upstream cache-control switch.
4. Anchor only if the approved URL reappears and the exact reviewed content
   identity also matches: Bluesky binds AT URI/post/image CIDs; Lens binds the
   candidate and captured media hashes plus stable captured post metadata.

The second pass replays the sealed source, resolved Bluesky DID/filter, search
mode, and face threshold. It is
still zero-key on Bluesky; Lens consumes another one or two credits. Closing
the console does not make an incomplete run successful.

For a complete blockchain demo with no wallet, faucet, or chain API key, use:

```powershell
uv run faceproof local-demo
```

This generates a fresh disposable wallet in memory, starts a localhost Anvil
chain, compiles and deploys `EvidenceRegistry`, pins the deployed bytecode,
launches the same console, and stops Anvil when the command exits. It requires
a clean committed checkout and never edits `.env`. Keep the command running
through discovery, reviewed anchoring, verification, and the tamper test because
the local chain is intentionally ephemeral. Use Base Sepolia for a public,
third-party-verifiable submission record.

For this disposable mode, run **Verify** and **Tamper test** inside the Console
while `local-demo` is still running; that server alone holds the generated RPC,
wallet, and contract settings. A separate CLI process can independently verify
only a persistent chain whose trusted RPC, chain ID, contract address, and code
hash are configured in its environment (for example Base Sepolia or an Anvil
node you started and kept running yourself).

### Discover, review, then run the full pipeline

Put a consented local image at `samples/consented-person.jpg` (the directory is
Git-ignored). First check it locally; this does not search, upload, persist an
embedding, or consume a credit:

```powershell
uv run faceproof scan --image .\samples\consented-person.jpg --i-have-consent
```

For the no-key route, publish the volunteer image in a real public post on an
account they control, then scan that live account feed. The handle is supplied
at run time and the URL is not preselected:

```powershell
uv run faceproof run --image .\samples\consented-person.jpg `
  --live --i-have-consent --skip-anchor `
  --search-provider bluesky `
  --bluesky-actor volunteer.bsky.social
```

For broader indexed-web discovery, select Lens:

```powershell
uv run faceproof run --image .\samples\consented-person.jpg `
  --live --i-have-consent --skip-anchor `
  --search-provider lens `
  --search-mode standard
```

After reviewing the selected post and evidence directory, run a fresh live
search and approve that exact URL for anchoring. The command fails if the URL is
not returned, does not independently face-match, or cannot be captured as a
real post permalink.

The anchor command reuses the reviewed run's sealed provider, resolved actor
DID/filter, input image, mode, and face threshold, regardless of new CLI values:

```powershell
uv run faceproof run --image .\samples\consented-person.jpg `
  --live --i-have-consent `
  --consent-reference "volunteer-a-2026-09" `
  --review-run-id "<discovery-run-id>" `
  --approve-post-url "https://www.reddit.com/r/example/comments/abc/example"
```

Use `--search-provider lens --search-mode deep` when the full-image
exact-plus-face-crop visual search is worth two credits. To show optional
LinkedIn profile leads on the Lens route, add
`--check-linkedin-profiles`; those leads remain in a separate, non-anchorable
section and cannot make an otherwise inconclusive run pass.

Development without a blockchain write is deliberately explicit:

```powershell
uv run faceproof run --image .\samples\consented-person.jpg `
  --live --i-have-consent --skip-anchor `
  --search-provider bluesky `
  --bluesky-actor volunteer.bsky.social
```

### Re-verify and demonstrate tampering

```powershell
uv run faceproof verify .\evidence\<unanchored-run-id> --allow-unanchored
uv run faceproof verify .\evidence\<anchored-run-id>
uv run faceproof tamper-demo .\evidence\<run-id>
```

`--allow-unanchored` verifies bundle integrity but deliberately cannot report an
on-chain pass. For a `local-demo` anchor, use the Console verification before
stopping the ephemeral chain. Use the CLI-from-a-fresh-process demonstration
for a persistent configured chain.

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

`faceproof local-demo` supplies a fully real, zero-cost EVM path: a new local
chain and wallet, contract deployment, signed transaction, receipt/event
validation, independent registry read-back, and tamper rejection. It is ideal
for rehearsing or judging functionality, but it is not a durable public record.

Recommended public deployment:

- Network: Base Sepolia
- Chain ID: `84532`
- Public RPC: `https://sepolia.base.org`
- Explorer: `https://sepolia.basescan.org`
- Contract address: set after an optional public deployment; `local-demo`
  creates and pins a new per-session address automatically

After deployment, set `FACEPROOF_CONTRACT_ADDRESS`. Also set
`FACEPROOF_CONTRACT_CODE_HASH` to the Keccak-256 hash of the deployed runtime
bytecode; both are required for anchoring and chain verification. The verifier
never trusts chain ID or registry address supplied by an evidence bundle.

The public RPC is suitable for a demonstration but is rate-limited. Configure
a provider RPC as a backup. During a live demo, distinguish fast Base L2 block
inclusion from later Ethereum L1 batch finality.

## Evidence format

Each `evidence/<run-id>/` directory contains:

- plaintext query-image, detected-face preview/crop, and downloaded candidate
  or post-media artifacts (sensitive; access-control, encrypt, and delete them
  under the volunteer's retention agreement);
- a sanitized parsed provider receipt plus the SHA-256 of the exact HTTP
  response bytes;
- candidate image bytes returned or referenced by the provider;
- source-specific provenance: Lens lane/input hashes and search IDs, or Bluesky
  AT URI/post CID/image CID plus public-API response hashes;
- result title/source and the exact matched-image path/hash; Lens labels, when
  present, are explicitly marked unverified;
- independently validated public-post metadata and, where exposed, post media;
- `manifest.json`, containing hashes and model/search/match metadata;
- `manifest.canonical.json`, the RFC 8785 canonical bytes;
- `commitment.json`, containing the manifest hash, random salt and commitment;
- `chain-receipt.json` after anchoring.

The chain receipt is intentionally excluded from the pre-anchor manifest to
avoid circular hashing.

Before broadcasting, FaceProof atomically journals the signed transaction hash,
signer, nonce, chain, contract, and commitment in a sibling
`.RUN_ID.anchor-submission.json` file. It contains no private key, raw signed
transaction, or RPC credential. If the RPC times out after submission, do not
start another anchor. Use the console's **Recover exact transaction** action or:

```powershell
uv run faceproof recover-anchor .\evidence\RUN_ID
```

Recovery is read-only on-chain: it never signs or broadcasts. It accepts only
the saved transaction hash after checking its signer, nonce, calldata, receipt,
event, registry record, contract bytecode, and confirmations.

Each discovery is single-use for anchoring. Before the fresh anchor pass,
FaceProof atomically creates `.DISCOVERY_RUN_ID.anchor-claim.json` outside the
bundle. It remains after any outcome so an already-used or unresolved review
cannot authorize a duplicate transaction; after a failed attempt, run a fresh
discovery before trying again. This is a local evidence-root control: copying
the bundle to a different root or deleting its sibling claim file defeats it,
so the operator must preserve the entire evidence root.

The raw provider body itself is not retained because it may echo credentials;
the digest identifies those received bytes but cannot reconstruct them.

```text
manifest_hash = SHA256(JCS(manifest))
commitment = keccak256(ABI.encode(manifest_hash, salt32))
```

## Tests

```powershell
uv sync --locked --python 3.11 --extra dev
uv run pytest --cov=faceproof --cov-report=term --cov-fail-under=70
uv run ruff check .
uv run ruff format --check .
uv run bandit -q -r src/faceproof
uv export --frozen --extra dev --no-emit-project --no-hashes `
  --output-file audit-requirements.txt
uv run pip-audit --strict --progress-spinner off -r audit-requirements.txt
uv build
```

Contract tests:

```powershell
Set-Location contracts
forge fmt --check
forge test -vv
Set-Location ..
$env:FACEPROOF_RUN_ANVIL_INTEGRATION = "1"
uv run pytest -q tests/integration/test_chain_anvil.py
```

CI pins Foundry v1.8.1. If `forge`/`anvil` are not on `PATH`, set the optional
absolute launcher overrides shown in `.env.example` for `local-demo`. Export
those same variables in the current PowerShell session for the direct pytest
integration command; manual `forge`/`cast` commands still require their
executable directory on `PATH`.

### Measured face-verification baseline

The pinned FaceProof 0.2.0 benchmark source measured **99.20% accuracy on the
4,126 scored pairs**, but the strict one-face/quality policy scored only
**68.77% of all 6,000 pairs**. The more honest all-pair correct-and-scored yield
is **68.22%**. Version 0.4.0 retains the model/scoring policy but added an input
safety gate, so a new benchmark is required before calling this an exact-source
0.4.0 reproduction. These figures measure 1:1 face verification, not web-search
recall or open-set identification. See [`docs/ACCURACY_VALIDATION.md`](docs/ACCURACY_VALIDATION.md)
for confidence intervals, FAR/FRR, dataset/model hashes, reproduction steps,
and limitations.

Live provider tests are not run in CI because they make real external requests
and the Lens route consumes credits/processes biometric data. Unit tests use
clearly labelled synthetic responses and never fall back to them at runtime.

## Demo checklist

1. Show the consented input and current time.
2. Open `faceproof web`, run discovery, show the reviewed permalink and content
   identity, then run the fresh anchor pass for that exact version.
3. Show detection, quality result, model fingerprints and embedding fingerprint.
4. Show Lens provider search IDs, or the FaceProof-derived Bluesky capture/page
   IDs alongside their response hashes; do not call the latter provider-issued
   IDs. Show the retrieval timestamp and dynamically returned records.
5. Show the selected post title/source, exact matched image, AT CIDs or Lens
   lane provenance, and independent local face score versus its threshold.
6. Show the evidence commitment and successful EVM transaction/read-back.
7. For Base Sepolia, open the explorer transaction; for Anvil, show the local
   receipt and registry verification in the console.
8. For Base Sepolia or another persistent configured chain, run
   `faceproof verify` from a new process with the independently copied
   commitment and transaction hash. For `local-demo`, use Console **Verify**
   while its Anvil process is alive.
9. Run `faceproof tamper-demo` on a persistent-chain bundle, or the Console
   tamper action during `local-demo`, and show the changed copy fail.

Pre-fund the wallet when using Base Sepolia, mask API keys, disable
notifications, and prepare more than one consenting test case. Never present
cached or fixture data as a live search.

## Security and privacy controls

- Explicit consent acknowledgement is required by the CLI.
- Multiple/no-face and low-quality inputs fail closed.
- Provider results are independently re-matched; provider scores are not trusted
  as identity proof.
- Profiles/homepages cannot satisfy Task 3: only recognized, capture-capable
  post permalinks with stable IDs can become anchor-eligible. Opted-in LinkedIn
  profiles remain separately labelled leads.
- An anchored run requires successful public post-identity capture and explicit
  approval of the exact permalink plus the captured content version. A fresh
  pass must reproduce its content identity before any chain write.
- Review authorization, manifest content checks, evidence ZIP export, and public
  receipt generation use immutable snapshots so verified bytes cannot be
  replaced between verification and use.
- Remote evidence requires HTTPS and rejects private/local network addresses.
- Capture pins each validated public DNS answer to the actual TCP peer, rejects
  redirects to other/private targets, and refuses compressed or oversized
  response bodies before decoding.
- Every live run verifies the exact pinned YuNet/SFace file sizes and hashes.
- Anchored runs bind a clean Git commit and pinned registry bytecode hash.
- Public page capture never logs in or bypasses access controls.
- API keys and wallet keys are environment-only and excluded by `.gitignore`.
- Only a random-salted commitment is public on-chain.
- A successfully completed search/capture path with no eligible verified post
  returns `inconclusive`; candidate-specific download/capture rejections can
  contribute to that result. Provider-search, input, configuration, and chain
  faults fail explicitly. Neither path substitutes a hardcoded URL or hidden
  fixture.

The detailed assumptions, trust boundaries, threats, and residual risks are in
[`docs/THREAT_MODEL.md`](docs/THREAT_MODEL.md).

The judge-facing completion gates and demo sequence are in
[`docs/SUBMISSION_PLAN.md`](docs/SUBMISSION_PLAN.md). A sourced technology,
product, paper, and comparable-project review is in
[`docs/RESEARCH.md`](docs/RESEARCH.md). A commit-pinned comparison of HHGoa
Task 3 repositories and the failure patterns avoided here is in
[`docs/COMPETITOR_AUDIT.md`](docs/COMPETITOR_AUDIT.md).

## Known limitations

- Web indexes are incomplete and change continuously. Private, login-only,
  newly published, or robots-excluded posts may not be found.
- The zero-key Bluesky route searches only the explicitly supplied consented
  account's public media feed; it does not discover an unknown account or
  search the whole Bluesky network.
- Face similarity can produce false matches and false non-matches, especially
  with poor image quality, age/pose changes, occlusion, or demographic bias.
- A browser/API capture proves what this client recorded, not that the platform
  cryptographically signed the content.
- Some platforms expose post identity through oEmbed but not the original media;
  unanchored discovery records that weaker provider-associated linkage, while
  anchored submission runs fail closed unless captured post media is
  independently re-matched. X commonly falls into this category; prefer a
  consented public YouTube, Reddit, or Bluesky result for the anchored demo.
- LinkedIn, Instagram, Facebook, and TikTok may appear in genuine Lens results,
  but this implementation treats them only as discovery leads and does not
  automate their restricted post pages. They cannot complete or anchor the
  matching-post claim.
- A testnet can be reset or retired and is not permanent or legal-grade storage.
- Base Sepolia proves public inclusion and integrity, not the truth of off-chain
  claims.
- SerpApi is a third-party Google Lens scraper, not an official Google API.
- Standard SerpApi search data is retained for 31 days under its published
  policy; no-retention ZeroTrace is Enterprise-only. Lens therefore requires
  separate informed consent for third-party image processing.
- The single-use review claim is enforced by the CLI/web workflow inside one
  preserved evidence root. A trusted local operator can defeat that policy by
  copying bundles, deleting sidecars, changing roots, or calling low-level
  Python functions with fabricated lineage values.
- DNS resolution and streamed reads have explicit cumulative deadlines, but an
  in-process deadline may overshoot one blocking socket-read interval and a
  timed-out OS resolver thread can continue in the background until the OS
  returns. A production deployment should still use a controlled egress proxy
  and process boundary for hard resource isolation.
- Raw evidence may contain personal or copyrighted data and must be retained,
  shared, and deleted under an explicit policy.

## Licence

Project source is MIT licensed. Third-party face models and services retain
their own licences and terms. The selected OpenCV YuNet and SFace model
directories currently state MIT and Apache-2.0 licences respectively; model
files are downloaded rather than redistributed, and their SHA-256 fingerprints
are recorded in every run.
