# FaceProof setup and live demo

This is the shortest submission-grade path. It uses one free-tier search API.
The blockchain can be a zero-cost local Anvil chain or Base Sepolia test ETH.
No Gemini or paid face-search account is required.

Install [Foundry](https://getfoundry.sh/getting-started/installation) before
the blockchain steps; it supplies `forge`, `cast`, and the local `anvil` node.

## 1. Protect credentials

Any API key pasted into chat or a public issue must be revoked and regenerated.
Never use a personal/mainnet wallet. Create a dedicated throwaway Base Sepolia
wallet and keep its private key only in the ignored `.env` file.

The project never prints keys and excludes `.env`, live evidence, downloaded
models, Foundry broadcasts, and local chain state from Git.

## 2. Install and configure the free search

1. Create a [SerpApi account](https://serpapi.com/users/sign_up).
2. Select the [$0 plan](https://serpapi.com/pricing), currently 250 searches
   per month.
3. Copy the key from [Manage API Key](https://serpapi.com/manage-api-key).
4. From the repository root:

```powershell
uv sync --locked --python 3.11 --extra dev
if (-not (Test-Path -LiteralPath .env)) {
  Copy-Item -LiteralPath .env.example -Destination .env
}
```

If `.env` already exists, do not overwrite it. Edit only this value:

```dotenv
SERPAPI_API_KEY=replace_with_your_regenerated_serpapi_key
```

Then download the pinned face models and validate the free account/quota. The
account check itself does not consume a search credit.

```powershell
uv run faceproof models download
uv run faceproof doctor --no-check-rpc
```

FaceProof offers two live Lens modes:

- **Standard** makes one `all` request and uses one SerpApi search credit.
- **Deep** makes separate `exact_matches` and `visual_matches` requests and uses
  two SerpApi search credits.

Every anchor attempt repeats the reviewed discovery as a fresh, cache-disabled
search, so budget another one or two credits for the second pass. Deep mode can
increase candidate coverage but does not guarantee a match. The image upload
and account/quota check do not themselves consume a Lens search credit.

## 3. Prepare a lawful, discoverable test

Use an adult volunteer who gave purpose-specific consent. Put a clear,
front-facing scan in `samples/consented-person.jpg`. The image must contain
exactly one sufficiently large, sharp face.

Use a real public X, Reddit, YouTube, or Bluesky post controlled or authorized
by the volunteer. Those platforms have supported public capture paths and can
complete post validation when the public response exposes the required
permalink and media. The post should contain the same image, a crop, or a
compressed copy, and Google must already have indexed it. A private,
login-only, or newly published post is not a reliable demo target.

LinkedIn, Instagram, Facebook, and TikTok may still appear in genuine Lens
results, but FaceProof treats them as **discovery leads only**. It does not log
in, bypass access controls, or automate those restricted pages, so their posts
cannot satisfy or anchor the Task 3 matching-post claim. For the most reliable
anchored demo, prepare a consented public Reddit, YouTube, or Bluesky post and
keep a second indexed example available.

The post URL is not built into the code. SerpApi directly uploads the input,
runs a fresh Google Lens query with cache disabled, and returns candidates.
FaceProof then independently downloads and compares the candidate media with
the local SFace embedding.

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
console presents consent, readiness, Standard/Deep selection, live progress,
candidate evidence, verification, private bundle download, and the tamper test
in one workflow.

Choose **Discover** first. Review the selected permalink and evidence, then use
**Review & prepare anchor**. After the chain setup in the next section, the
console runs a second fresh search using the reviewed discovery's search policy
and exact input image. The URL must reappear and independently pass the face,
permalink, capture, and post-media gates before any blockchain write occurs.

Public LinkedIn profile leads are disabled by default. The separate opt-in
checks only thumbnails returned by Lens; it never logs into or scrapes LinkedIn.
A matching profile thumbnail remains an unverified lead and can never count as
the required post or become anchor-eligible.

The CLI remains available for scripted runs. Run discovery first:

```powershell
uv run faceproof run --image .\samples\consented-person.jpg `
  --live --i-have-consent --skip-anchor `
  --search-mode standard
```

Use `--search-mode deep` for the two-credit exact-plus-visual search. Add
`--check-linkedin-profiles` only when the volunteer explicitly authorized that
extra profile-lead check; it cannot change an inconclusive post result into a
successful one.

Review the returned stable post permalink and its evidence directory. An
`INCONCLUSIVE` result means the live index or public capture did not produce an
eligible face match; it never falls back to a fixture or hardcoded URL.

The result table also shows any label supplied by Google Lens, the post title
and source, the downloaded matched-image path, and the local similarity versus
the frozen threshold. A displayed name remains an unverified web search hint;
it is not a face-model or legal-identity claim.

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

Use `--port 8790` if port 8787 is busy, or `--no-open-browser` when you do not
want it to open a browser automatically.

Live web discovery still needs the free SerpApi key from section 2. For a public
record that another judge can query later, continue with Base Sepolia below.

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

For the Foundry deployment command only, set the same two values in the current
PowerShell session (they disappear when that terminal closes):

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

## 5. Run, anchor, and independently verify

The recommended console flow is:

1. Complete an unanchored **Discover** run.
2. Open the selected public post and review its exact permalink and evidence.
3. Choose **Review & prepare anchor** and acknowledge the irreversible opaque
   commitment.
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

Copy the printed run directory, commitment, and transaction hash. Verify from a
new process using the independently copied on-chain values, then demonstrate a
one-byte modification being rejected:

```powershell
uv run faceproof verify .\evidence\RUN_ID `
  --expected-commitment 0xCOMMITMENT `
  --expected-tx 0xTRANSACTION_HASH
uv run faceproof tamper-demo .\evidence\RUN_ID
```

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
| `SERPAPI_API_KEY` | SerpApi free account dashboard | $0 plan |
| `FACEPROOF_RPC_URL` | Base public endpoint (already defaulted) | $0, rate-limited |
| `FACEPROOF_CHAIN_ID` | Base Sepolia (`84532`, already defaulted) | $0 |
| `FACEPROOF_PRIVATE_KEY` | Dedicated testnet wallet you create | $0 |
| `FACEPROOF_CONTRACT_ADDRESS` | Output of `forge create` | Test ETH only |
| `FACEPROOF_CONTRACT_CODE_HASH` | Output of `cast keccak` | $0 |
| `FACEPROOF_SOURCE_REVISION` | Output of `git rev-parse HEAD` | $0 |

Gemini is deliberately absent. Its image understanding can describe an input,
but it is not a reverse-face web index, and Google's current image-search
grounding documentation says searches for people are unsupported.
