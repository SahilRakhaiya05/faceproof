# FaceProof research brief

**Purpose:** support the HH Goa 2026 Task 3 implementation and judge demo with primary sources, defensible terminology, and explicit limits. Links were checked on **2026-09-03**. Vendor documentation is authoritative for an interface or stated limitation, not independent evidence of accuracy, coverage, or legal compliance.

## What this pipeline actually does

| Stage | Question answered | Practical component | It does **not** prove |
|---|---|---|---|
| Face detection | “Where is a face in this image?” | OpenCV YuNet | Who the person is |
| Face comparison | “Are these two face crops similar enough to review as the same person?” | OpenCV SFace, recorded threshold, human approval | Legal identity, authorship, or a universally correct match |
| Web discovery | “Where has this face-scan image or a visually related copy appeared in an indexed public page?” | Google Lens through SerpApi for exact/cropped/reposted imagery | Same-person recognition across unrelated photographs, complete web coverage, platform endorsement, or truth |
| Evidence capture | “What bytes and metadata did this run observe?” | Manifest plus captured response/page artifacts; WACZ is a stronger optional archive | That the publisher created or still endorses the content |
| Blockchain anchor | “Does this evidence bundle reproduce the commitment recorded in this transaction?” | Salted Keccak commitment on Base Sepolia | Identity, truth, ownership, source authenticity, or original publication time |

In biometric terminology, **identification** is normally a 1:N search against an enrolled gallery; **verification** is a 1:1 comparison. This project does not crawl the web into its own biometric gallery. It asks an external index for candidates, then performs local 1:1 SFace comparisons and requires a reviewer to accept the candidate. Reverse-image search is also distinct: it can find copies or visually similar images without recognizing the person.

## Face detection and comparison: directly relevant research

- **YuNet detector.** OpenCV's official [`FaceDetectorYN` / `FaceRecognizerSF` tutorial](https://docs.opencv.org/4.x/d0/dd4/tutorial_dnn_face.html) documents the exact APIs, alignment flow, similarity metrics, and example thresholds used by this implementation. The official [YuNet model directory](https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet) contains the pinned `face_detection_yunet_2023mar.onnx` model and WIDER Face results. The peer-reviewed [YuNet paper](https://link.springer.com/article/10.1007/s11633-023-1423-y) describes a small face **detector**, not an identity model.
- **SFace recognizer.** The official [SFace model directory](https://github.com/opencv/opencv_zoo/tree/main/models/face_recognition_sface) supplies `face_recognition_sface_2021dec.onnx` and identifies the MobileFaceNet/SFace-loss design. The [SFace paper](https://ieeexplore.ieee.org/document/9318547) evaluates the method on face-recognition benchmarks including LFW, MegaFace, and IJB-C.
- **Thresholds need calibration.** OpenCV reports an LFW cosine threshold of `0.363` and LFW accuracy of `99.60%`; that is a benchmark operating point, **not** a universal threshold for open-web 1:N search. NIST's [FRTE 1:N evaluation](https://pages.nist.gov/frvt/html/frvt1N.html) defines false-positive and false-negative identification rates and explains why investigation workflows use human review. NIST also documents [demographic and image-quality effects](https://pages.nist.gov/frvt/html/frvt_demographics.html). Therefore the demo should report the score, threshold, face quality, and reviewer decision—not “AI proved identity.”
- **Local reproducible baseline.** The project's [aggregate LFW validation](ACCURACY_VALIDATION.md) records 99.20% conditional accuracy at `0.363` with 68.77% pair coverage and 68.22% correct-and-scored yield over all protocol pairs. It pins the dataset tree, pair list, models, and benchmark source and explicitly does not claim web-search accuracy.
- **Benchmark context.** WIDER Face is a detection benchmark, not a recognition benchmark; its paper is available through the [CVPR DOI](https://doi.org/10.1109/CVPR.2016.596). LFW-style accuracy cannot establish performance on compressed, occluded, aged, or profile social-media faces.

## Genuine search options and their limits

| Option | Search actually performed | Fit for this task | Material limitation |
|---|---|---|---|
| [Google Lens through SerpApi](https://serpapi.com/google-lens-api) | Directly uploads the scan and fetches Google Lens visual/exact-match results | **Selected.** Genuine automated search for the same, cropped, resized, or reposted image; `no_cache=true` bypasses SerpApi's one-hour cache. The current [$0 plan](https://serpapi.com/pricing) includes 250 searches/month. | Reverse-image/visual search, **not same-person recognition across unrelated photographs**. It depends on two vendors and changing upstream indexes. Disabling SerpApi's cache does not guarantee upstream freshness. |
| [Google Cloud Vision Web Detection](https://cloud.google.com/vision/docs/detecting-web) | Returns matching images, pages, and web entities | Managed alternative for locating copies/matching pages | Not an identity search and not social-platform-specific. Google's [face-detection documentation](https://cloud.google.com/vision/docs/detecting-faces) explicitly says specific-individual facial recognition is unsupported. |
| [Gemini Google Image Search grounding](https://ai.google.dev/gemini-api/docs/image-generation#grounding-with-google-search) | Retrieves web images as generation context | Not used | Google explicitly states it cannot search for people. Gemini image understanding can describe an input, but it is not a reverse-face web index and cannot satisfy this discovery step. |
| [TinEye](https://help.tineye.com/article/235-can-tineye-find-similar-images-does-tineye-do-facial-recognition) | Finds exact or altered copies | Useful for provenance of a known image | TinEye explicitly says it does not perform facial recognition and cannot find different images of the same person. |
| [AWS Rekognition `SearchFacesByImage`](https://docs.aws.amazon.com/rekognition/latest/APIReference/API_SearchFacesByImage.html) / [Azure Face identification](https://learn.microsoft.com/en-us/azure/ai-services/face/concept-face-recognition) | 1:N search against a customer-enrolled collection/person group | Useful only if the team owns a lawful gallery | Neither searches the public web. Calling either alone the Task 3 “web/social search” would be inaccurate. |

**Selected routing:** use SerpApi Lens for a live exact/near-duplicate web search, then download each social candidate and independently rerank it with SFace. Preserve a sanitized parsed response, the exact HTTP-body digest, request/search ID, and retrieval time. A result URL is a candidate lead, not a signed statement from the social platform. This intentionally chooses a reliable free-tier rubric path over an unsupported claim that Gemini recognizes people across the public web.

## Evidence, provenance, and anchoring

| Standard/product | What it contributes | Recommended use |
|---|---|---|
| [RFC 8785 JSON Canonicalization Scheme](https://www.rfc-editor.org/rfc/rfc8785.html) + [NIST SHA-256](https://csrc.nist.gov/pubs/fips/180-4/upd1/final) | Deterministic manifest bytes and artifact digests | **Build.** Canonicalize the pre-anchor manifest, hash every artifact, and fail closed if the required canonicalizer is unavailable. |
| [Solidity ABI encoding](https://docs.soliditylang.org/en/latest/abi-spec.html) + [Base network/RPC reference](https://docs.base.org/base-chain/api-reference/rpc-overview) | Reproducible `keccak256(abi.encode(bytes32 manifestSha256, bytes32 salt))` commitment and public Base Sepolia transaction (chain ID 84532) | **Build.** Keep the random salt and receipt outside the pre-anchor manifest, recompute locally, read the contract/event, and compare all values. A testnet proves the engineering path, not economic permanence. |
| [C2PA specification 2.4](https://spec.c2pa.org/specifications/specifications/2.4/specs/C2PA_Specification.html) and [`c2pa-python`](https://github.com/contentauth/c2pa-python) | Signed, asset-bound provenance assertions and credential validation | **Verify when present.** C2PA is not a blockchain and cannot retroactively authenticate an unsigned social post. Valid credentials attest signer-bound assertions; the specification does not determine whether content is “true.” Absence of credentials is not evidence of fakery. |
| [WACZ 1.1.1](https://specs.webrecorder.net/wacz/1.1.1/), [Browsertrix Crawler](https://github.com/webrecorder/browsertrix-crawler), and [`warcio`](https://github.com/webrecorder/warcio) | Replayable WARC content, metadata, indexes, and fixity information | **Optional upgrade.** Prefer WACZ for a durable page capture. Browsertrix offers higher-fidelity browser capture but adds Docker/runtime weight and still cannot bypass lawful access controls; `warcio` is lighter for HTTP capture. |
| [Ethereum Attestation Service contracts](https://github.com/ethereum-attestation-service/eas-contracts) | Reusable schemas and on-chain/off-chain attestations | **Buy/reuse alternative.** Useful for interoperable claims, but a minimal purpose-built registry is easier to explain and verify in a short demo. Define revocation semantics if EAS is adopted. |
| [OpenTimestamps](https://opentimestamps.org/) / [client repository](https://github.com/opentimestamps/opentimestamps-client) | Independently verifiable Bitcoin-backed timestamp proofs | Credible alternative when Bitcoin anchoring matters; confirmation and calendar-proof upgrade latency weaken an instant demo. |
| [IPFS content addressing](https://docs.ipfs.tech/concepts/content-addressing/) | Content-addressed retrieval | Optional storage layer only. A CID is not generally the raw file SHA-256, IPFS is not a blockchain, and availability requires pinning. Do not publish biometric/copyrighted evidence merely to improve the demo. |

The defensible claim is: **“These exact evidence bytes, combined with this secret/random salt, reproduce the commitment included in this Base Sepolia transaction no later than its block time.”** It is not “the blockchain verified the person/post.” Raw media, embeddings, names, post text, URLs, API tokens, and salts should remain off-chain.

## Comparable open-source components

No single repository below is evidence of an end-to-end, consented face-to-public-web discovery plus social-post capture plus blockchain re-verification system; these are reusable component references.

- [`opencv/opencv_zoo`](https://github.com/opencv/opencv_zoo): the exact YuNet/SFace models and examples used here.
- [`serengil/deepface`](https://github.com/serengil/deepface): convenient local face verification and database search across several models, including SFace; it does not provide a public-web index.
- [`JohannesBuchner/imagehash`](https://github.com/JohannesBuchner/imagehash): perceptual and crop-resistant hashes for near-duplicate correlation; not identity recognition. Review maintenance and benchmark fit before adoption.
- [`contentauth/c2pa-rs`](https://github.com/contentauth/c2pa-rs) and [`contentauth/c2pa-python`](https://github.com/contentauth/c2pa-python): production-oriented Content Credentials parsing/signing; provenance, not discovery.
- [`webrecorder/browsertrix-crawler`](https://github.com/webrecorder/browsertrix-crawler) and [`webrecorder/warcio`](https://github.com/webrecorder/warcio): stronger web-evidence capture patterns.
- [`ethereum-attestation-service/eas-contracts`](https://github.com/ethereum-attestation-service/eas-contracts): reusable attestation infrastructure.
- [`opentimestamps/opentimestamps-client`](https://github.com/opentimestamps/opentimestamps-client): independently verifiable timestamp proofs.
- [`guardianproject/proofmode-android`](https://github.com/guardianproject/proofmode-android): useful capture-time signing/metadata pattern, but Android-focused, not web face search, and currently a mirror; do not imply courtroom acceptance.

## Build-versus-buy decision

| Capability | Decision | Rationale / demo fallback |
|---|---|---|
| Face detection, alignment, embeddings, and candidate rerank | **Build locally with pinned YuNet/SFace models** | Reproducible, offline, inspectable, and avoids sending every candidate to another biometric service. Include model SHA-256 values. |
| Web-scale same-person face index | **Do not build or claim** | Building a useful, lawful biometric crawl is outside hackathon scope. The rubric expressly permits reverse-image search, so the product uses that narrower and reproducible route. |
| Exact/near-duplicate web search | **Free-tier API: SerpApi Lens** | Genuine, repeatable query step with direct image upload, cache disabled, sanitized response and exact-body digest. |
| Social-post evidence capture | **Build minimal capture; add WACZ if reliable** | Save the fetched bytes, response metadata, screenshots/oEmbed where permitted, and every digest. A screenshot alone is weak evidence. Use a prepared, consented public post to reduce login/anti-bot failure—not a hardcoded search result. |
| Evidence canonicalization and tamper checks | **Build** | Small, security-critical, and directly demonstrates engineering depth: RFC 8785, per-artifact SHA-256, salted commitment, negative tamper test. |
| On-chain registry | **Build minimal contract on Base Sepolia** | Lowest demo complexity and clearest verification story. EAS or OpenTimestamps are credible alternatives, not required dependencies. |
| Content Credentials | **Verify if present; do not mint retroactive provenance** | C2PA adds value only when a discoverable asset already carries a trusted credential or when the team controls capture/signing. |

## Legal, ethical, and judge-demo guardrails

- Use one **adult volunteer**, written purpose-specific consent, and preferably a public post the volunteer controls. Never demonstrate on strangers, minors, intimate/sensitive contexts, or for employment, housing, credit, insurance, policing, or immigration decisions.
- India’s official [Digital Personal Data Protection Act, 2023](https://www.indiacode.nic.in/indiacode/handle/123456789/22037?view_type=browse) and the notified [DPDP Rules, 2025](https://www.meity.gov.in/documents/act-and-policies/digital-personal-data-protection-rules-2025-gDOxUjMtQWa?pageTitle=Digital-Personal-Data-Protection-Rules-2025%3B) make notice, purpose, consent/other lawful grounds, safeguards, and deletion planning material; commencement is phased. Obtain jurisdiction-specific advice before deployment.
- In the EU, GDPR [Article 9](https://eur-lex.europa.eu/eli/reg/2016/679/oj) treats biometric data used for unique identification as special-category data. The [EU AI Act](https://eur-lex.europa.eu/eli/reg/2024/1689/oj/eng) prohibits creating or expanding facial-recognition databases through untargeted scraping. This prototype must remain targeted and consented, with no retained face gallery.
- Keep query images and embeddings ephemeral; encrypt evidence at rest; set a retention/deletion date; exclude secrets and personal data from logs; and place only a salted 32-byte commitment on-chain. A public chain cannot honor erasure of data placed directly on it.
- Respect provider/platform terms, robots policies, rate limits, copyright, and access controls. Do not bypass login, CAPTCHA, or private-account boundaries. API access does not itself grant permission to republish content.
- Require human approval and show alternative candidates, thresholds, and failure states. A false positive is safety-relevant. Demonstrate a **tampered bundle failing verification** and an **untampered bundle passing**, then open the transaction/contract readback.

### Judge-safe one-sentence narrative

> With a volunteer's consent, FaceProof performs a real indexed search, independently rechecks the discovered candidate with a pinned local face model, captures the observed evidence, and proves later that the bundle has not changed by recomputing a privacy-preserving on-chain commitment—without claiming that a blockchain proves identity or truth.

This is a technical research brief, not legal advice.
