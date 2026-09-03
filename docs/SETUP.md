# FaceProof setup and live demo

This is the shortest submission-grade path. It uses one free-tier search API
and Base Sepolia test ETH. No Gemini or paid face-search account is required.

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

## 3. Prepare a lawful, discoverable test

Use an adult volunteer who gave purpose-specific consent. Put a clear,
front-facing scan in `samples/consented-person.jpg`. The image must contain
exactly one sufficiently large, sharp face.

Use a real public Reddit, Bluesky, YouTube, Instagram, TikTok, LinkedIn,
Facebook, or X post controlled or authorized by the volunteer. The post should
contain the same image, a crop, or a compressed copy. Google must already have
indexed it. A private, login-only, or newly published post is not a reliable
demo target.

The post URL is not built into the code. SerpApi directly uploads the input,
runs a fresh Google Lens query with cache disabled, and returns candidates.
FaceProof then independently downloads and compares the candidate media with
the local SFace embedding.

Run discovery first:

```powershell
uv run faceproof run --image .\samples\consented-person.jpg `
  --live --i-have-consent --skip-anchor
```

Review the returned stable post permalink and its evidence directory. An
`INCONCLUSIVE` result means the live index or public capture did not produce an
eligible face match; it never falls back to a fixture or hardcoded URL.

The result table also shows any label supplied by Google Lens, the post title
and source, the downloaded matched-image path, and the local similarity versus
the frozen threshold. A displayed name remains an unverified web search hint;
it is not a face-model or legal-identity claim.

## 4. Configure Base Sepolia

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

Run a fresh search and approve only the permalink returned by the discovery
run. The command still fails unless that URL is returned and face-matched again
in this fresh request.

```powershell
uv run faceproof run --image .\samples\consented-person.jpg `
  --live --i-have-consent `
  --consent-reference "volunteer-a-2026-09" `
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

## Required values

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
