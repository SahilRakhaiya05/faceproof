# FaceProof research brief

**Purpose:** support the HH Goa 2026 Task 3 implementation and judge demo with primary sources, defensible terminology, and explicit limits. Links were checked on **2026-09-03**. Vendor documentation is authoritative for an interface or stated limitation, not independent evidence of accuracy, coverage, or legal compliance.

## What this pipeline actually does

| Stage | Question answered | Practical component | It does **not** prove |
|---|---|---|---|
| Face detection | “Where is a face in this image?” | OpenCV YuNet | Who the person is |
| Face comparison | “Are these two face crops similar enough to review as the same person?” | OpenCV SFace, recorded threshold, human approval | Legal identity, authorship, or a universally correct match |
| Web/social discovery | “What candidate media does the selected live source return?” | SerpApi Lens for indexed exact/visual matches, or a runtime-supplied consented Bluesky author feed | Same-person recognition across unrelated photographs, complete web coverage, platform endorsement, or truth |
| Evidence capture | “What bytes and metadata did this run observe?” | Manifest plus captured response/page artifacts; WACZ is a stronger optional archive | That the publisher created or still endorses the content |
| Blockchain anchor | “Does this evidence bundle reproduce the commitment recorded in this transaction?” | Salted Keccak commitment on disposable local Anvil or persistent Base Sepolia | Identity, truth, ownership, source authenticity, durable availability (for Anvil), or original publication time |

In biometric terminology, **identification** is normally a 1:N search against an enrolled gallery; **verification** is a 1:1 comparison. This project does not crawl the web into its own biometric gallery. It asks an external index for candidates, then performs local 1:1 SFace comparisons and requires a reviewer to accept the candidate. Reverse-image search is also distinct: it can find copies or visually similar images without recognizing the person.

## FaceCheck: useful public blueprint, not a clone target

FaceCheck's own [algorithm overview](https://facecheck.id/en/topics/Facial-Recognition-Algorithms) describes the standard architecture: detect a face, align it, generate an embedding, search an indexed face-vector database, then rank and filter candidates. Its [face-search overview](https://facecheck.id/en/topics/Face-Search) says the query embedding is compared with faces extracted from a large pre-indexed web corpus. Its [privacy page](https://facecheck.id/Face-Search/Privacy), however, says it does not create, store, or process biometric templates and performs only visual-similarity analysis. These public descriptions are difficult to reconcile and remain vendor assertions, not a reproducible technical specification or independent confirmation of corpus size, model accuracy, retention, or lawful coverage.

That public blueprint can be implemented generically, but FaceCheck itself cannot be faithfully “recreated from scratch” from those pages:

- The exact detector, embedding model, training data, crawling/indexing policy, deduplication logic, vector index, ranking features, and thresholds are not published as a reproducible system.
- Its principal advantage is a continuously maintained web-scale face corpus. A detector plus FAISS/HNSW does not create that corpus, and a hackathon-scale crawler cannot reproduce its breadth, freshness, takedown process, or legal governance.
- FaceCheck's [Terms](https://facecheck.id/Face-Search/Terms) prohibit unauthorized automated access, copying/adapting site software, bypassing controls, and reverse engineering. This project therefore neither scrapes nor automates FaceCheck. Its documented [API](https://facecheck.id/en/Face-Search/API) is paid for useful searches; the free testing mode scans only a limited gallery and the vendor explicitly says its results are not meaningful.
- Untargeted expansion of a facial-recognition database creates serious consent, platform-terms, and regulatory risk. The independent design is limited to a consented query and public candidates returned by supported providers; it does not retain a reusable face gallery.

A future self-indexed mode is technically straightforward only for an **authorized** corpus: ingest permitted media, detect/align/embed, store model-versioned vectors, retrieve top-K with [FAISS](https://github.com/facebookresearch/faiss), [HNSW](https://doi.org/10.1109/TPAMI.2018.2889473), or [pgvector](https://github.com/pgvector/pgvector), and rerank with exact cosine similarity. Corpus rights, deletion, freshness, and representative evaluation—not the nearest-neighbor library—remain the hard part.

## Face detection and comparison: directly relevant research

- **YuNet detector.** OpenCV's official [`FaceDetectorYN` / `FaceRecognizerSF` tutorial](https://docs.opencv.org/4.x/d0/dd4/tutorial_dnn_face.html) documents the exact APIs, alignment flow, similarity metrics, and example thresholds used by this implementation. The official [YuNet model directory](https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet) contains the pinned `face_detection_yunet_2023mar.onnx` model and WIDER Face results. The peer-reviewed [YuNet paper](https://link.springer.com/article/10.1007/s11633-023-1423-y) describes a small face **detector**, not an identity model.
- **SFace recognizer.** The official [SFace model directory](https://github.com/opencv/opencv_zoo/tree/main/models/face_recognition_sface) supplies `face_recognition_sface_2021dec.onnx` and identifies the MobileFaceNet/SFace-loss design. The [SFace paper](https://ieeexplore.ieee.org/document/9318547) evaluates the method on face-recognition benchmarks including LFW, MegaFace, and IJB-C.
- **Why this baseline stays pinned.** YuNet/SFace is CPU-friendly, available through OpenCV's stable DNN APIs, small enough to package for a judge demo, and distributed with explicit upstream model metadata. It gives one reproducible embedding space and threshold rather than silently changing models between runs.
- **Stronger research families are not drop-in licensed products.** [ArcFace](https://openaccess.thecvf.com/content_CVPR_2019/html/Deng_ArcFace_Additive_Angular_Margin_Loss_for_Deep_Face_Recognition_CVPR_2019_paper.html) introduces an additive angular-margin training loss; [AdaFace](https://openaccess.thecvf.com/content/CVPR2022/papers/Kim_AdaFace_Quality_Adaptive_Margin_for_Face_Recognition_CVPR_2022_paper.pdf) adapts the margin using image quality; and [MagFace](https://openaccess.thecvf.com/content/CVPR2021/html/Meng_MagFace_A_Universal_Representation_for_Face_Recognition_and_Quality_Assessment_CVPR_2021_paper.html) makes embedding magnitude informative about recognition quality. Their papers justify future controlled evaluation, not an accuracy claim for arbitrary third-party weights. In particular, InsightFace's [package license notice](https://pypi.org/project/insightface/) says its code is MIT but the pretrained models it provides—including auto-downloaded packages—are restricted to non-commercial research unless separately licensed. This is why `buffalo_l` is not silently substituted for SFace.
- **Thresholds need calibration.** OpenCV reports an LFW cosine threshold of `0.363` and LFW accuracy of `99.60%`; that is a benchmark operating point, **not** a universal threshold for open-web 1:N search. NIST's [FRTE 1:N evaluation](https://pages.nist.gov/frvt/html/frvt1N.html) defines false-positive identification rate (FPIR), false-negative identification rate (FNIR), rank, and gallery-size-dependent evaluation. NIST also documents [demographic and image-quality effects](https://pages.nist.gov/frvt/html/frvt_demographics.html). Therefore the demo should report the score, threshold, face quality, and reviewer decision—not “AI proved identity.”
- **Local reproducible baseline.** The project's [aggregate LFW validation](ACCURACY_VALIDATION.md) records 99.20% conditional accuracy at `0.363` with 68.77% pair coverage and 68.22% correct-and-scored yield over all protocol pairs. It pins the dataset tree, pair list, models, and FaceProof 0.2.0 benchmark source and explicitly does not claim exact-source 0.4.0 reproduction or web-search accuracy.
- **Benchmark context.** WIDER Face is a detection benchmark, not a recognition benchmark; its paper is available through the [CVPR DOI](https://doi.org/10.1109/CVPR.2016.596). LFW-style accuracy cannot establish performance on compressed, occluded, aged, or profile social-media faces.

### Accuracy must be reported as four separate layers

| Layer | Appropriate measures | What a good number would establish |
|---|---|---|
| Face detection/quality gate | Detection coverage, failure-to-extract rate, and coverage by pose/size/quality | Whether the pipeline obtains a usable face at all |
| Local 1:1 comparison | False match rate (FMR), false non-match rate (FNMR), ROC/TAR at stated thresholds | Pairwise verification performance in the evaluated model/domain |
| Open-set 1:N identification | FPIR, FNIR, rank/top-K, gallery size, and operating threshold | Search behavior when an enrolled mate may be absent |
| Web discovery and end-to-end pipeline | Search success@K, candidate-download coverage, rerank pass rate, review acceptance, and final anchored-evidence yield | Whether a provider can discover and the pipeline can verify relevant public content |

LFW is a controlled **1:1 pair protocol**. The local result above is conditional on the detector producing both faces, so its 99.20% figure must always be shown beside 68.77% pair coverage and 68.22% all-pair yield. None of those values measures Google/Bluesky index coverage, web discovery success, social-post availability, or end-to-end accuracy. A complete evaluation therefore needs a consented web test set with known discoverable and non-discoverable posts; until then, the project reports run-level evidence rather than a universal “accuracy percentage.”

## Genuine search options and their limits

| Option | Search actually performed | Fit for this task | Material limitation |
|---|---|---|---|
| [Google Lens through SerpApi](https://serpapi.com/google-lens-api) | Uploads the scan and requests Google Lens `exact_matches`, `visual_matches`, or `all` results | **Primary web-wide lane.** Genuine automated search for the same, cropped, resized, or reposted image; `no_cache=true` bypasses SerpApi's one-hour cache. | Reverse-image/visual search, **not same-person recognition across unrelated photographs**. It depends on two vendors and changing upstream indexes; an empty lane is not proof that no matching post exists. |
| [Bluesky public author feed](https://docs.bsky.app/docs/api/app-bsky-feed-get-author-feed) | Fetches media posts from a runtime-supplied public handle/DID, then performs local face reranking | **Deterministic consented social lane.** No paid search API or query-image upload; useful when a volunteer controls a public post. | Searches one declared public feed, not the whole web or all Bluesky. It cannot discover an unknown person's account. |
| [Google Cloud Vision Web Detection](https://docs.cloud.google.com/vision/docs/internet-detection) | Returns pages containing matches, full/partial image matches, visually similar images, and web entities | Optional managed alternative for locating copies/matching pages. Current [pricing](https://cloud.google.com/vision/pricing) lists the first 1,000 units/month as free, including Web Detection. | Requires a Google Cloud project/authentication and may require billing setup; other cloud resources can cost money. It is not an identity search or social-platform-specific. Google's [face-detection documentation](https://cloud.google.com/vision/docs/detecting-faces) says specific-individual recognition is unsupported. |
| [Gemini Google Image Search grounding](https://ai.google.dev/gemini-api/docs/image-generation#grounding-with-google-search) | Retrieves visual web context for image generation | Not used | The official feature is not documented as a reverse-image/person-lookup endpoint and does not document using an uploaded face as the retrieval query. Gemini image understanding can analyze an input, but analysis alone does not supply the required discovery index. |
| [TinEye](https://help.tineye.com/article/235-can-tineye-find-similar-images-does-tineye-do-facial-recognition) | Finds exact or altered copies | Useful for provenance of a known image | TinEye explicitly says it does not perform facial recognition and cannot find different images of the same person. |
| [AWS Rekognition `SearchFacesByImage`](https://docs.aws.amazon.com/rekognition/latest/APIReference/API_SearchFacesByImage.html) / [Azure Face identification](https://learn.microsoft.com/en-us/azure/ai-services/face/concept-face-recognition) | 1:N search against a customer-enrolled collection/person group | Useful only if the team owns a lawful gallery | Neither searches the public web. Calling either alone the Task 3 “web/social search” would be inaccurate. |

**Selected routing:** use SerpApi Lens for a broad indexed-web query or Bluesky for a consented, reproducible public-feed scan; then download candidate media and independently rerank it with SFace. Preserve a sanitized parsed response, the exact HTTP-body digest, retrieval time, and source-specific identifiers. Lens returns result URLs and provider search IDs. Bluesky returns AT records/CIDs; FaceProof derives the `bsky.app` permalink and hash-based capture/page IDs locally. Neither form is a signed statement that the face match or post claim is true.

### SerpApi Lens upload and failure semantics

SerpApi added direct file upload for Lens in August 2026. Its current [Image API](https://serpapi.com/image-api) accepts a JPEG/PNG/WebP up to 500 KB at `POST https://serpapi.com/image`, returns a short-lived `image_id`, and documents expiry after ten minutes. The [Lens API](https://serpapi.com/google-lens-api) then accepts that ID and a required search type such as `exact_matches`, `visual_matches`, or `all`. The pipeline records the digest and byte size of every uploaded derivative and the lane that used it; uploads are explicit consent events.

The ten-minute ID expiry must not be overread as a deletion guarantee. SerpApi's
[standard policy](https://serpapi.com/legal) says search data is retained for 31
days, while no-retention [ZeroTrace mode](https://serpapi.com/zero-trace-mode)
is Enterprise-only. Lens therefore requires explicit consent for third-party
image processing and applicable data-transfer/retention terms. The Bluesky lane
is the privacy-first choice because it never receives the query portrait.

No-results handling must distinguish **semantic emptiness** from a failed search. SerpApi's official [Lens release notes](https://serpapi.com/google-lens-api/release-notes) document recurring empty/inconsistent results, including valid searches returning no JSON results and `type=all` omitting exact matches. Therefore an allowlisted “no results for this query” response is preserved as a soft-empty lane so another requested lane can continue. Authentication, quota, malformed-response, transport, and unexpected provider-search errors remain hard failures. If every lane is soft-empty, the honest result is “search completed with zero candidates,” which the pipeline records as `INCONCLUSIVE`—not “person not found” and not a fabricated match. After search, no eligible permalink, no threshold pass, or no independently capturable matching post is inconclusive; candidate-specific media/capture failures are recorded while other candidates continue. Input/quality, configuration, provider-search, and blockchain faults fail explicitly.

`no_cache=true` is a Lens request parameter that asks SerpApi to bypass its
documented result cache; it is not a universal guarantee about every upstream
cache between the client and Google. The reviewed anchor pass always makes a
new HTTP request. On Bluesky it refetches the author feed, but no equivalent
cache-disable parameter is claimed.

### Bluesky provenance lane

Bluesky documents `app.bsky.feed.getAuthorFeed` as a no-auth public read against `https://public.api.bsky.app`; the connector scans `posts_with_media` for a handle or DID supplied at run time. Its fixed admission budget is two pages of 50 feed entries and at most 100 image candidates. The operator's 1–20 `max-candidates` limit counts unique post permalinks, while all distinct CID-bound images belonging to each admitted Bluesky post remain eligible for local comparison. It rejects malformed or non-image embeds and requires each record blob CID to agree with the corresponding AppView full-size/thumbnail CDN path. It recaptures the selected post through `app.bsky.feed.getPosts`; on a provider-side 5xx/transport failure only, it falls back to `app.bsky.feed.getPostThread`. Both paths validate the DID/collection/record key in the AT URI, exact post CID, selected image CID, and media CDN CID before accepting the response, then hash the exact API response and downloaded bytes. The AppView response supplies AT records and assets, not a canonical web permalink or provider search/page ID: FaceProof derives a human-readable `https://bsky.app/profile/{did}/post/{rkey}` display permalink and its hash-based capture identifiers locally; the AT URI is the canonical protocol identifier. Because candidates are discovered dynamically and selected by the local face score, this is a genuine search step rather than a hardcoded result—but its scope is that one declared feed.

AT Protocol repositories are [content-addressed Merkle trees with signed commits](https://atproto.com/specs/repository); [AT URIs identify records and CIDs fingerprint record/blob content](https://atproto.com/guides/data-repos). Capturing an AT URI and CIDs materially improves provenance and later recapture, but an AppView JSON response alone is not a cryptographic inclusion proof. A stronger upgrade would fetch the authoritative repository record/commit or proof chain and verify its signature against the account DID document before anchoring.

The broader `app.bsky.feed.searchPosts` and `searchPostsV2` endpoints are not used as an anonymous dependency: unauthenticated calls returned HTTP 403 during the 2026-09-03 integration check, even though most Bluesky GET APIs are public. Provider deployment/auth policy can differ from the Lexicon shape. The author-feed connector is consequently the reliable no-token demonstration route; network-wide post search would require an authenticated, separately tested connector.

## Evidence, provenance, and anchoring

| Standard/product | What it contributes | Recommended use |
|---|---|---|
| [RFC 8785 JSON Canonicalization Scheme](https://www.rfc-editor.org/rfc/rfc8785.html) + [NIST SHA-256](https://csrc.nist.gov/pubs/fips/180-4/upd1/final) | Deterministic manifest bytes and artifact digests | **Build.** Canonicalize the pre-anchor manifest, hash every artifact, and fail closed if the required canonicalizer is unavailable. |
| [Solidity ABI encoding](https://docs.soliditylang.org/en/latest/abi-spec.html) + [Anvil](https://getfoundry.sh/anvil/overview) + [Base network/RPC reference](https://docs.base.org/base-chain/api-reference/rpc-overview) | Reproducible `keccak256(abi.encode(bytes32 manifestSha256, bytes32 salt))` commitment on a real disposable local chain or public Base Sepolia (chain ID 84532) | **Build.** Generate a fresh 32-byte random salt, keep it and the receipt outside the pre-anchor manifest, recompute locally, read the contract/event, and compare all values. Anvil proves the EVM flow but disappears when stopped; a testnet transaction is durable/queryable but does not prove the off-chain claim. |
| [C2PA specification 2.4](https://spec.c2pa.org/specifications/specifications/2.4/specs/C2PA_Specification.html) and [`c2pa-python`](https://github.com/contentauth/c2pa-python) | Signed, asset-bound provenance assertions and credential validation | **Verify when present.** C2PA is not a blockchain and cannot retroactively authenticate an unsigned social post. Valid credentials attest signer-bound assertions; the specification does not determine whether content is “true.” Absence of credentials is not evidence of fakery. |
| [WACZ 1.1.1](https://specs.webrecorder.net/wacz/1.1.1/), [Browsertrix Crawler](https://github.com/webrecorder/browsertrix-crawler), and [`warcio`](https://github.com/webrecorder/warcio) | Replayable WARC content, metadata, indexes, and fixity information | **Optional upgrade.** Prefer WACZ for a durable page capture. Browsertrix offers higher-fidelity browser capture but adds Docker/runtime weight and still cannot bypass lawful access controls; `warcio` is lighter for HTTP capture. |
| [Ethereum Attestation Service contracts](https://github.com/ethereum-attestation-service/eas-contracts) | Reusable schemas and on-chain/off-chain attestations | **Buy/reuse alternative.** Useful for interoperable claims, but a minimal purpose-built registry is easier to explain and verify in a short demo. Define revocation semantics if EAS is adopted. |
| [OpenTimestamps](https://opentimestamps.org/) / [client repository](https://github.com/opentimestamps/opentimestamps-client) | Independently verifiable Bitcoin-backed timestamp proofs | Credible alternative when Bitcoin anchoring matters; confirmation and calendar-proof upgrade latency weaken an instant demo. |
| [IPFS content addressing](https://docs.ipfs.tech/concepts/content-addressing/) | Content-addressed retrieval | Optional storage layer only. A CID is not generally the raw file SHA-256, IPFS is not a blockchain, and availability requires pinning. Do not publish biometric/copyrighted evidence merely to improve the demo. |

The defensible claim is: **“These exact evidence bytes plus the recorded random salt reproduce the commitment included in this EVM transaction no later than its block time.”** On Base Sepolia the record remains publicly queryable; on `local-demo` it exists only while the disposable Anvil process is alive. The salt is generated with a cryptographic RNG and stored in `commitment.json` outside the pre-anchor manifest; it need not remain secret once a verifier receives the bundle. This is not “the blockchain verified the person/post.” Raw media, embedding vectors, names, post text, URLs, API tokens, and salts remain off-chain.

The off-chain privacy cost is explicit: FaceProof keeps the query image,
detected-face preview/crop, and downloaded candidate/post media as plaintext in
the evidence directory so exact bytes can be re-hashed. It stores only an
embedding fingerprint/dimension, not the raw vector. Git ignores these files,
but Git-ignore is not encryption or deletion; an operator must restrict access,
encrypt transfers/storage, and enforce the consented retention date.

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
| Consented public social search | **Build with Bluesky public author-feed API** | Runtime handle/DID, no query-image upload, exact AT URI/CID capture, and local all-candidate face reranking create a deterministic live demo without hardcoding a post. |
| Social-post evidence capture | **Build minimal capture; add WACZ if reliable** | Save the fetched bytes, response metadata, screenshots/oEmbed where permitted, and every digest. A screenshot alone is weak evidence. Use a prepared, consented public post to reduce login/anti-bot failure—not a hardcoded search result. |
| Evidence canonicalization and tamper checks | **Build** | Small, security-critical, and directly demonstrates engineering depth: RFC 8785, per-artifact SHA-256, salted commitment, negative tamper test. |
| On-chain registry | **Build minimal contract; run on Anvil or Base Sepolia** | Disposable Anvil is zero-cost and rubric-valid for a live deploy/anchor/read-back/tamper demo. Base Sepolia adds durable public lookup. EAS or OpenTimestamps are credible alternatives, not required dependencies. |
| Content Credentials | **Verify if present; do not mint retroactive provenance** | C2PA adds value only when a discoverable asset already carries a trusted credential or when the team controls capture/signing. |

## Legal, ethical, and judge-demo guardrails

- Use one **adult volunteer**, written purpose-specific consent, and preferably a public post the volunteer controls. Never demonstrate on strangers, minors, intimate/sensitive contexts, or for employment, housing, credit, insurance, policing, or immigration decisions.
- India’s official [Digital Personal Data Protection Act, 2023](https://www.indiacode.nic.in/indiacode/handle/123456789/22037?view_type=browse) and the notified [DPDP Rules, 2025](https://www.meity.gov.in/documents/act-and-policies/digital-personal-data-protection-rules-2025-gDOxUjMtQWa?pageTitle=Digital-Personal-Data-Protection-Rules-2025%3B) make notice, purpose, consent/other lawful grounds, safeguards, and deletion planning material; commencement is phased. Obtain jurisdiction-specific advice before deployment.
- In the EU, GDPR [Article 9](https://eur-lex.europa.eu/eli/reg/2016/679/oj) treats biometric data used for unique identification as special-category data. The [EU AI Act](https://eur-lex.europa.eu/eli/reg/2024/1689/oj/eng) prohibits creating or expanding facial-recognition databases through untargeted scraping. This prototype must remain targeted and consented, with no retained face gallery.
- Keep raw embedding vectors ephemeral. Because exact re-verification requires the evidence directory to retain plaintext query/candidate images, access-control and encrypt that directory, set and enforce a retention/deletion date, exclude secrets and unnecessary personal data from logs, and place only a salted 32-byte commitment on-chain. A public chain cannot honor erasure of data placed directly on it.
- Respect provider/platform terms, robots policies, rate limits, copyright, and access controls. Do not bypass login, CAPTCHA, or private-account boundaries. API access does not itself grant permission to republish content.
- Require human approval and show alternative candidates, thresholds, and failure states. A false positive is safety-relevant. Demonstrate a **tampered bundle failing verification** and an **untampered bundle passing**, then open the transaction/contract readback.

### Judge-safe one-sentence narrative

> With a volunteer's consent, FaceProof performs either a real indexed Lens search or a live scan of that volunteer's declared public Bluesky feed, independently rechecks the discovered candidate with a pinned local face model, captures the observed evidence, and proves later that the bundle has not changed by recomputing a privacy-preserving EVM commitment—without claiming that a blockchain proves identity or truth.

This is a technical research brief, not legal advice.
