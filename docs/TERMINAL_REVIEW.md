# Terminal redesign and feasibility review

Reviewed on 2026-09-05. This review distinguishes source documentation from
behavior exercised on this machine. It does not certify competing applications.

## Why an uploaded photo does not guarantee a profile

Face detection answers whether usable facial features are present. Comparing
two authorized images measures model similarity. Neither operation supplies a
person's social account. A reverse-image provider can return indexed occurrences
or visually related images, but it cannot promise that every uploaded photo has
an indexed matching post. A newly taken selfie and a previously published photo
are different retrieval cases. Missing content must remain an inconclusive
result, never a fabricated profile or an invented confidence percentage.

The existing 99.20% number is a historical, conditional LFW pair-verification
measurement. Its scored-pair coverage was 68.77%. It is not a measurement of
profile discovery, current-source reproduction, or the probability that any
particular result is correct. See [the benchmark record](ACCURACY_VALIDATION.md).

The supported development direction is consent-based matching against enrolled
profiles, or locating copies of an authorized image. This change does not add
open-web identification of people from their faces.

## What the GitHub examples establish

| Source inspected | Documented approach | What to learn from it |
| --- | --- | --- |
| [devesh1905/HHgoaTask3](https://github.com/devesh1905/HHgoaTask3) | Describes SerpApi retrieval, an offline development fallback, canonical hashing, and EVM transaction calldata. | A fallback demonstration must be distinguished from a live retrieval result. |
| [sillanaresh/hhgoa-task3](https://github.com/sillanaresh/hhgoa-task3) | Documents reviewed evidence, a Base Sepolia fingerprint transaction, read-back, and a public example receipt. | An inspectable receipt is stronger evidence than a success badge. The cited public run is a repository claim; its transaction was not independently audited in this review. |
| [alfacodermbp/Task3HHGoa](https://github.com/alfacodermbp/Task3HHGoa) | Describes multiple search providers, a local dataset fallback, a built-in proof ledger, and an optional EVM connector. | Show which provider and chain actually executed. A local hash ledger and a publicly queryable EVM record have different persistence and trust assumptions. |
| [kartik1525/hhgoa-task3](https://github.com/kartik1525/hhgoa-task3) | The inspected repository listing exposes app, samples, tests, and configuration files without a rendered README. | A file listing alone does not establish that an end-to-end run works. |

These are sampled results from the [requested repository search](https://github.com/search?q=hhgoa-task-3&type=repositories).
No competing application was installed or executed. The [official SerpApi Lens
documentation](https://serpapi.com/google-lens-api) distinguishes exact and visual
match results; this is retrieval documentation, not an identity-accuracy claim.

## Findings on this machine

- The pinned local model assets validate.
- The account check succeeds; the initial check reported 241 of 250 searches
  remaining. This is a point-in-time account result, not a completed image search.
- The configured Base Sepolia RPC responds with the expected chain ID.
- Public-chain anchoring is not configured: the registry address, signing key,
  and deployed code hash are missing. RPC connectivity alone is insufficient.
- 111 existing web, evidence, chain, and local-demo tests passed before editing.
- The real Anvil integration test passed, covering deployment, anchoring,
  read-back, recovery, and tamper rejection with test data. All six Solidity
  contract tests passed. This establishes the local chain path, not a public
  deployment or a live matching-post demonstration.

## Changes delivered

The console now opens into a compact monospace terminal layout. The input form
and evidence output precede the archive. At the user's request, the Protocol /
Reference section, navigation item, and historical benchmark strip were removed.
Connection diagnostics wrap instead of truncating essential setup
information. The empty terminal explicitly says no result has been generated.
Log messages use an event marker instead of universally showing a success tick.

Keyboard focus, a skip link, mobile stacking, and reduced-motion support are
included. Missing readiness responses show UNKNOWN rather than leaving checking
placeholders indefinitely. Zero monthly search capacity cannot divide by zero.

The UI redesign does not change matching thresholds, search implementations,
evidence canonicalization, transaction signing, or source-provenance rules.
The disposable local-demo launcher still requires a clean committed checkout;
these edits are left uncommitted for review.

The new Photo copies workspace adds a separate one-upload path. It retains the
unique HTTPS page references returned by a live Lens `all` search, checks up to
24 downloadable images with a whole-photo matcher, and labels every retained
reference by its actual state. Only confirmed copies are eligible for the
local SHA-256 evidence chain. The face encoder is run locally for rubric
coverage after the form's authorization attestation, but is explicitly excluded
from retrieval and copy scoring.

Browser checks passed at 1440px desktop and 390px/320px mobile widths. A long
archive title exposed an intrinsic grid-width overflow, corrected with a zero
minimum width on archive cards. The final mobile document width equals the
viewport width. Keyboard navigation reaches a visible skip link, preflight
without an image displays actionable guidance, and an injected HTTP 503 renders
all three readiness rows as UNKNOWN. The injected route was removed afterward.
The normal page reported no browser errors. JavaScript syntax, whitespace, and
the full automated suite passed after the redesign and photo-copy workflow
were added.

## Follow-up on the reported failed run

Run `20260904T033705Z-893821` contains 302 saved provider candidate entries.
Its recorded stop stage is `social-filter`: none entered the social-post
comparison stage. This is not evidence that Google has no image of the subject.
No social-post match score or blockchain record was produced by that run.
The result and archive headings now describe that distinction. Search retrieval
and filtering behavior were not changed. The 28 existing web and reporting
tests passed after these presentation changes.

An error-rendering bug was also corrected: when a job fails without producing
a summary, the terminal status no longer dereferences a null summary before
displaying the error.

A follow-up documentation review found that
[devesh1905/HHgoaTask3](https://github.com/devesh1905/HHgoaTask3) describes broad
matching but does not establish a representative account-discovery benchmark;
[Anbi105's entry](https://github.com/Anbi105/HHGoa2026-Task3-FaceID-Blockchain)
provides only a brief description. Neither was executed in this review.
[Gemini Google Search grounding](https://ai.google.dev/gemini-api/docs/google-search)
documents searching and citations. It does not establish social-account
ownership verification. A generated citation is not proof that two images show
the same person. No Gemini request or credential storage was performed.

## Remaining proof

A working local UI and a passing chain test are not a completed live
submission. No new user portrait was searched, no profile was identified, and
no public transaction was sent during this change. Public-chain configuration
and a permitted real-data demonstration remain separate work. No universal
matching or perfect accuracy claim is supported; the provider's retained
references and the local comparison scores are the inspectable evidence.
