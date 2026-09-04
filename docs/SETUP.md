# FaceProof setup and live demo

This is the shortest submission-grade path. The default judge demo uses the
public Bluesky API with no API key and a zero-cost local Anvil chain. SerpApi
Lens and Base Sepolia are optional upgrades for broader web discovery and a
durable public transaction. No Gemini or paid face-search account is required.

Install [Foundry](https://getfoundry.sh/getting-started/installation) before
the blockchain steps; it supplies `forge`, `cast`, and the local `anvil` node.
CI pins Foundry v1.8.1. After installation, open a new terminal and confirm
`forge --version`, `cast --version`, and `anvil --version` work. `local-demo`
looks first at `FACEPROOF_FORGE_PATH` / `FACEPROOF_ANVIL_PATH`, then `PATH`, then
an optional repository-local `.tools/foundry/**` installation. Manual deployment
commands still require `forge` and `cast` on `PATH`. The CLI loads overrides
from `.env`; direct pytest invocation does not, so export the same absolute
variables in that shell when running the Anvil integration test.

## 1. Protect credentials

Any API key pasted into chat or a public issue must be revoked and regenerated.
Never use a personal/mainnet wallet. Create a dedicated throwaway Base Sepolia
wallet and keep its private key only in the ignored `.env` file.

The project never prints keys and excludes `.env`, live evidence, downloaded
models, Foundry broadcasts, and local chain state from Git.

## 2. Install and choose a live search source

FaceProof pins `uv` **0.11.8** in `pyproject.toml`. Install that version and use
the locked dependency graph:

```powershell
uv --version
uv sync --locked --python 3.11 --extra dev
if (-not (Test-Path -LiteralPath .env)) {
  Copy-Item -LiteralPath .env.example -Destination .env
}
uv run faceproof models download
uv run faceproof doctor --no-check-rpc
```

The conditional copy does not overwrite an existing `.env`; preserve and edit
the file you already have. If Foundry is installed outside `PATH`, set its
absolute launcher paths there as documented in `.env.example`.

The **Bluesky public-feed route needs no key**. At run time, supply the adult
volunteer's public handle or DID. FaceProof scans that real feed, selects posts
by local SFace similarity, and never uploads the query portrait to Bluesky.

SerpApi is optional for a broader Google Lens search:

1. Create a [SerpApi account](https://serpapi.com/users/sign_up).
2. Select the [$0 plan](https://serpapi.com/pricing), currently 250 searches
   per month.
3. Copy the key from [Manage API Key](https://serpapi.com/manage-api-key).

If `.env` already exists, do not overwrite it. Edit only this value:

```dotenv
SERPAPI_API_KEY=replace_with_your_regenerated_serpapi_key
```

Before choosing Lens, obtain consent for sending a metadata-stripped image
derivative to SerpApi/Google and for the applicable third-party/cross-border
processing. SerpApi's [standard policy](https://serpapi.com/legal) says search
data is retained for 31 days; [ZeroTrace](https://serpapi.com/zero-trace-mode)
is Enterprise-only. The Image API's ten-minute `image_id` expiry is not
documented as deletion of every uploaded byte. Prefer Bluesky when avoiding a
query-image upload is important.

Validate the free account/quota. The account check itself does not consume a
search credit.

```powershell
uv run faceproof doctor --no-check-rpc
```

FaceProof offers two live Lens modes:

- **Standard** makes one `all` request and uses one SerpApi search credit.
- **Deep** sends the full image to `exact_matches` and the detected face crop to
  `visual_matches`, using two SerpApi search credits.

Every anchor attempt repeats the reviewed discovery as a fresh provider request.
Lens explicitly sends `no_cache=true`, so budget another one or two credits for
its second pass. Bluesky makes a new author-feed request, but FaceProof does not
claim that this public endpoint provides an equivalent cache-disable option.
Deep mode can increase candidate coverage but does not guarantee a match. The
Lens image upload and account/quota check do not themselves consume a search
credit.

## 3. Prepare a lawful, discoverable test

Use an adult volunteer who gave purpose-specific consent. Put a clear,
front-facing scan in `samples/consented-person.jpg`. The image must contain
exactly one sufficiently large, sharp face.

Use a real public post controlled or authorized by the volunteer. It should
contain the same image, a crop, or a compressed copy.

For the most deterministic zero-key demo, post it to the volunteer's public
Bluesky feed. FaceProof enumerates that live feed through
`app.bsky.feed.getAuthorFeed`, verifies the selected AT URI/post CID/image CID
with `app.bsky.feed.getPosts` (or a strict `getPostThread` fallback when that
endpoint has a transient 5xx/transport failure), and re-matches the downloaded media locally. The
handle is supplied at run time; neither a result URL nor fixture is hardcoded.
The connector reads at most two pages of 50 feed entries and admits at most 100
strictly validated image candidates. It accepts only aligned record/view image
data whose blob CID matches the allowlisted CDN URL. The displayed `bsky.app`
permalink and capture/page IDs are FaceProof-derived from AT records and response
digests—not URLs or IDs asserted by the AppView response. The UI/CLI
`max-candidates` limit (1–20) counts admitted unique post permalinks. Every
distinct CID-bound image on an admitted Bluesky post is still evaluated, so the
number of image comparisons can be greater than the post limit (but never above
the connector's 100-image admission cap).

Lens results can expose both a full-image URL and a different result thumbnail.
FaceProof keeps these as separate media variants under the same unique-post
budget and locally evaluates both; identical media reported by multiple lanes
keeps the earlier exact-match provenance.

For web-wide Lens, use a public X, Reddit, YouTube, or Bluesky post already
indexed by Google. Those platforms have supported capture paths when their
public response exposes the required permalink and media. A private,
login-only, robots-excluded, or newly published post is not a reliable Lens
demo target.

LinkedIn, Instagram, Facebook, and TikTok may still appear in genuine Lens
results, but FaceProof treats them as **discovery leads only**. It does not log
in, bypass access controls, or automate those restricted pages, so their posts
cannot satisfy or anchor the Task 3 matching-post claim. For the most reliable
anchored demo, prepare a consented public Reddit, YouTube, or Bluesky post and
keep a second indexed example available.

The post URL is not built into either route. SerpApi uploads a
metadata-stripped input and runs Lens with cache disabled; Bluesky returns the
current posts for the runtime actor without receiving the query portrait.
FaceProof independently downloads and compares candidate media in both cases.

Every run copies the query image and writes a detected-face preview, face crop,
and downloaded candidate/post media into its plaintext `evidence/<run-id>/`
directory. It does not persist raw embedding vectors, only their dimension and
fingerprint. The directory is Git-ignored but not encrypted: restrict access,
encrypt it before storage or transfer, and delete it on the consented retention
date. Deleting it does not erase an already published opaque chain commitment.

Lens searches globally. The platform choices in FaceProof filter returned URLs
for local evaluation; they do not restrict what Google Lens searches or what
may appear in the preserved provider response.

### Recommended: use the localhost judge console

From the repository root, launch:

```powershell
uv run faceproof web
```

It opens `http://127.0.0.1:8787` and listens only on this computer. Keep it
local; do not expose it through a proxy, LAN binding, or public tunnel. The
console presents consent, a zero-credit local face preflight, Bluesky/Lens
selection, live progress, candidate evidence, verification, private bundle
download, and the tamper test in one workflow.

Choose **Discover** first. Review the selected permalink and evidence, then use
**Review & prepare anchor**. After the chain setup in the next section, the
console runs a second fresh search using the reviewed discovery's search policy
and exact input image. The URL must reappear and independently pass the face,
permalink, capture, and post-media gates before any blockchain write occurs.

Public LinkedIn profile leads are disabled by default. The separate opt-in
checks only thumbnails returned by Lens; it never logs into or scrapes LinkedIn.
A matching profile thumbnail remains an unverified lead and can never count as
the required post or become anchor-eligible.

The CLI remains available for scripted runs. Zero-key Bluesky discovery:

```powershell
uv run faceproof run --image .\samples\consented-person.jpg `
  --live --i-have-consent --skip-anchor `
  --search-provider bluesky `
  --bluesky-actor volunteer.bsky.social
```

Lens discovery:

```powershell
uv run faceproof run --image .\samples\consented-person.jpg `
  --live --i-have-consent --skip-anchor `
  --search-provider lens `
  --search-mode standard
```

Use `--search-mode deep` for the two-credit exact-plus-visual search. Add
`--check-linkedin-profiles` only when the volunteer explicitly authorized that
extra profile-lead check; it cannot change an inconclusive post result into a
successful one.

Review the returned stable post permalink and its evidence directory. An
`INCONCLUSIVE` result means the live index or public capture did not produce an
eligible face match after a successful provider request. Provider authentication,
quota, request-transport, malformed-response, input/face-quality, configuration,
and blockchain problems are hard failures. Candidate-specific media or public
post capture failures are recorded and evaluation continues; if no eligible
verified post remains, the result is inconclusive. Neither outcome falls back
to a fixture or hardcoded URL.

To check an unanchored discovery bundle from the CLI, explicitly allow the
missing chain receipt:

```powershell
uv run faceproof verify .\evidence\DISCOVERY_RUN_ID --allow-unanchored
```

This checks artifact/manifest integrity only and does not claim an on-chain
verification pass.

The result table also shows any label supplied by Google Lens, the post title
and source, the downloaded matched-image path, and the local similarity versus
the frozen threshold. A displayed name remains an unverified web search hint;
it is not a face-model or legal-identity claim.

One discovery can authorize only one anchor attempt. FaceProof atomically
claims it before the second live search and retains that claim after success,
failure, or an unresolved transaction. If the attempt does not complete, make
a new discovery rather than reusing the old review; this prevents duplicate
blockchain submissions.

The claim sidecar is a local workflow guard, not a global authorization ledger.
Keep the full evidence root intact: copying a discovery to another root or
deleting sibling claim files can bypass the single-use check. The supported
CLI/web anchor flow performs semantic provider replay checks and binds the
reviewed input hash; low-level Python calls assume a trusted operator.

## 4A. Zero-cost local blockchain demo

After installing Foundry, one command supplies every blockchain value without
editing `.env`, creating a wallet, or visiting a faucet:

```powershell
uv run faceproof local-demo
```

FaceProof requires a clean committed checkout, generates a fresh disposable
wallet and mnemonic in memory, starts Anvil on localhost, compiles and deploys
`EvidenceRegistry`, pins its runtime code hash, and opens the judge console at
`http://127.0.0.1:8787`. Keep this command running while you complete Discover,
Review & prepare anchor, Verify, and Tamper test. Press `Ctrl+C` afterward to
stop the console and local chain. The command never changes `.env` and the
ephemeral wallet and chain are not suitable for real funds or durable public
proof.

Complete **Verify** and **Tamper test** in that Console before stopping
`local-demo`. Its RPC, private key, and deployed contract settings exist only in
the server process, so a separately launched CLI does not inherit them. A true
fresh-process CLI verification requires a persistent chain that is still
running and the same trusted RPC/chain/contract/code-hash settings in that new
process; Base Sepolia is the simplest submission path for that demonstration.

Use `--port 8790` if port 8787 is busy, or `--no-open-browser` when you do not
want it to open a browser automatically.

Live discovery still needs either a runtime Bluesky handle/DID or the optional
SerpApi key from section 2. For a public record that another judge can query
later, continue with Base Sepolia below.

## 4B. Configure Base Sepolia

The recommended public record uses Base Sepolia, chain ID `84532`, and the
free public RPC `https://sepolia.base.org`. Obtain test ETH from the
[Coinbase Developer Platform faucet](https://www.coinbase.com/developer-platform/products/faucet).

Put the dedicated testnet wallet key in `.env`:

```dotenv
FACEPROOF_RPC_URL=https://sepolia.base.org
FACEPROOF_CHAIN_ID=84532
FACEPROOF_PRIVATE_KEY=0x_replace_with_dedicated_testnet_key
```

For the Foundry deployment command only, ensure `forge` and `cast` are on
`PATH`, then set the same two values in the current PowerShell session (they
disappear when that terminal closes):

```powershell
$env:FACEPROOF_RPC_URL = "https://sepolia.base.org"
$env:FACEPROOF_PRIVATE_KEY = "0x_replace_with_dedicated_testnet_key"
```

Deploy the immutable registry:

```powershell
Set-Location contracts
forge test -vv
forge create src/EvidenceRegistry.sol:EvidenceRegistry `
  --rpc-url $env:FACEPROOF_RPC_URL `
  --private-key $env:FACEPROOF_PRIVATE_KEY `
  --broadcast
Set-Location ..
```

Copy the `Deployed to` address into `FACEPROOF_CONTRACT_ADDRESS`, then pin the
exact runtime bytecode:

```powershell
$env:FACEPROOF_CONTRACT_ADDRESS = "0x_deployed_registry_address"
$runtimeCode = cast code $env:FACEPROOF_CONTRACT_ADDRESS `
  --rpc-url $env:FACEPROOF_RPC_URL
cast keccak $runtimeCode
git rev-parse HEAD
```

Copy the two outputs into `.env`:

```dotenv
FACEPROOF_CONTRACT_ADDRESS=0x_deployed_registry_address
FACEPROOF_CONTRACT_CODE_HASH=0x_runtime_keccak_hash
FACEPROOF_SOURCE_REVISION=the_exact_clean_git_commit
```

Require every submission gate:

```powershell
uv run faceproof doctor --demo
```

Publishing and verifying the exact deployed Solidity source on BaseScan is an
optional transparency upgrade, not a completed fact or a Task 3 requirement.
Claim “source verified” only after the explorer page for your actual contract
address shows the matching compiler inputs/source; a successful transaction or
non-empty bytecode alone does not establish source verification.

## 5. Run, anchor, and independently verify

The recommended console flow is:

1. Complete an unanchored **Discover** run.
2. Open the selected public post and review its exact permalink, content/media
   identity, and evidence.
3. Choose **Review & prepare anchor** and acknowledge the irreversible opaque
   commitment. The fresh pass must reproduce that reviewed content identity,
   not merely the same URL.
4. Run the second fresh search. FaceProof writes to the chain only if the same
   post is returned and all local and public-capture checks pass again.

For the CLI alternative, run a fresh search and approve only the permalink
returned by the discovery run. Supply that discovery run ID as provenance. The
command still fails unless the approved URL is returned and face-matched again
in this fresh request.

```powershell
uv run faceproof run --image .\samples\consented-person.jpg `
  --live --i-have-consent `
  --consent-reference "volunteer-a-2026-09" `
  --review-run-id "DISCOVERY_RUN_ID" `
  --approve-post-url "https://the-real-discovered-social-post"
```

For Base Sepolia (or another persistent configured chain), copy the printed run
directory, commitment, and transaction hash. Verify from a new process using
the independently copied on-chain values, then demonstrate a one-byte
modification being rejected:

```powershell
uv run faceproof verify .\evidence\RUN_ID `
  --expected-commitment 0xCOMMITMENT `
  --expected-tx 0xTRANSACTION_HASH
uv run faceproof tamper-demo .\evidence\RUN_ID
```

For a disposable `local-demo` run, perform both actions in the Console while it
is running instead of using this fresh-process CLI sequence.

If an RPC timeout occurs after the transaction was signed, FaceProof reports
`anchor-pending` and preserves a non-secret recovery journal beside the run
directory. Do **not** repeat the anchor command. In the console choose
**Recover exact transaction**, or run:

```powershell
uv run faceproof recover-anchor .\evidence\RUN_ID
```

This path only polls and validates the journaled transaction hash; it cannot
sign or rebroadcast a replacement transaction. The journal is removed only
after the recovered receipt passes an independent chain read-back.

## Required values

The local blockchain demo needs none of the `FACEPROOF_*` chain values below;
it creates and injects them only for that process. A public Base Sepolia run
uses this table:

| Setting | Where it comes from | Cost |
|---|---|---:|
| `SERPAPI_API_KEY` (optional) | SerpApi free account dashboard; Lens only | $0 plan |
| `FACEPROOF_RPC_URL` | Base public endpoint (already defaulted) | $0, rate-limited |
| `FACEPROOF_CHAIN_ID` | Base Sepolia (`84532`, already defaulted) | $0 |
| `FACEPROOF_PRIVATE_KEY` | Dedicated testnet wallet you create | $0 |
| `FACEPROOF_CONTRACT_ADDRESS` | Output of `forge create` | Test ETH only |
| `FACEPROOF_CONTRACT_CODE_HASH` | Output of `cast keccak` | $0 |
| `FACEPROOF_SOURCE_REVISION` | Output of `git rev-parse HEAD` | $0 |

The Bluesky handle/DID is intentionally a per-run form/CLI value, not an
environment secret. The zero-key Bluesky plus `local-demo` route therefore
needs none of the API, wallet, RPC, or contract values in this table.

Gemini is deliberately not required. Gemini models can analyze or describe
images, but that does not provide the reverse-image/person index required here.
Google's documented Image Search grounding feature retrieves visual context
for image generation; it does not document an endpoint that accepts an uploaded
face as a reverse-image or person-retrieval query. This is not a claim that
Gemini is unable to understand an image.

## 6. Reproduce the release checks

Run the same locked Python, security, Solidity, and real-Anvil integration gates
before tagging a submission:

```powershell
uv sync --locked --python 3.11 --extra dev
uv run ruff check .
uv run ruff format --check .
uv run bandit -q -r src/faceproof
uv export --frozen --extra dev --no-emit-project --no-hashes `
  --output-file audit-requirements.txt
uv run pip-audit --strict --progress-spinner off -r audit-requirements.txt
uv run pytest -q --cov=faceproof --cov-report=term --cov-fail-under=70
uv build

Set-Location contracts
forge fmt --check
forge test -vv
Set-Location ..

$env:FACEPROOF_RUN_ANVIL_INTEGRATION = "1"
uv run pytest -q tests/integration/test_chain_anvil.py
Remove-Item Env:FACEPROOF_RUN_ANVIL_INTEGRATION
```

The final integration test starts a real local Anvil process, deploys the
contract, submits a signed anchor, and reads it back. It is separate from mocked
unit tests. If `forge`/`anvil` are not on `PATH`, set
`$env:FACEPROOF_FORGE_PATH` and `$env:FACEPROOF_ANVIL_PATH` to their absolute
executables in this PowerShell session before running it; values present only in
`.env` are loaded by FaceProof CLI commands, not by pytest itself.
