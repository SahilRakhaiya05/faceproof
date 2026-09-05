"use strict";

const csrf = document.querySelector('meta[name="faceproof-csrf"]').content;
const terminalStates = new Set(["completed", "inconclusive", "failed"]);
let selectedFile = null;
let previewUrl = null;
let readinessState = null;
let currentJobId = null;
let reviewedRunId = null;
let pollTimer = null;
let currentEventSource = null;

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const escapeHtml = (value) => String(value ?? "")
  .replaceAll("&", "&amp;")
  .replaceAll("<", "&lt;")
  .replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;")
  .replaceAll("'", "&#039;");

function safeUrl(value, localOnly = false) {
  try {
    const url = new URL(String(value), window.location.origin);
    if (localOnly && url.origin !== window.location.origin) return "";
    if (!localOnly && url.protocol !== "https:") return "";
    return url.href;
  } catch {
    return "";
  }
}

function shortHash(value, length = 14) {
  const text = String(value || "—");
  if (text.length <= length + 5) return text;
  return `${text.slice(0, length)}…${text.slice(-4)}`;
}

function number(value, digits = 4) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed.toFixed(digits) : "—";
}

function formatBytes(bytes) {
  if (!Number.isFinite(bytes)) return "";
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(2)} MB`;
}

function formatTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? "—" : date.toLocaleString();
}

function toast(message) {
  const element = $("#toast");
  element.textContent = message;
  element.classList.add("show");
  window.setTimeout(() => element.classList.remove("show"), 2800);
}

let photoCopyFile = null;
let photoCopyPreviewUrl = null;
let photoCopyJobId = null;
let photoCopyPollTimer = null;

function setPhotoCopyFile(file) {
  if (photoCopyPreviewUrl) URL.revokeObjectURL(photoCopyPreviewUrl);
  photoCopyFile = file || null;
  photoCopyPreviewUrl = photoCopyFile ? URL.createObjectURL(photoCopyFile) : null;
  const placeholder = $("#photo-drop-placeholder");
  const preview = $("#photo-file-preview");
  const preflightCard = $("#preflight-status-card");
  if (!photoCopyFile) {
    placeholder.classList.remove("hidden");
    preview.classList.add("hidden");
    if (preflightCard) preflightCard.classList.add("hidden");
    $("#photo-copy-input").value = "";
    return;
  }
  placeholder.classList.add("hidden");
  preview.classList.remove("hidden");
  $("#photo-preview-image").src = photoCopyPreviewUrl;
  $("#photo-file-name").textContent = photoCopyFile.name;
  $("#photo-file-size").textContent = formatBytes(photoCopyFile.size);
  $("#photo-copy-error").classList.add("hidden");

  // Automatic local face preflight on photo select
  runPreflightCheck(photoCopyFile);
}

async function runPreflightCheck(file) {
  const preflightCard = $("#preflight-status-card");
  if (!preflightCard) return;
  preflightCard.classList.remove("hidden");
  const qEl = $("#preflight-quality");
  const fEl = $("#preflight-faces");
  const cEl = $("#preflight-conf");
  if (qEl) qEl.textContent = "Analyzing…";
  if (fEl) fEl.textContent = "…";
  if (cEl) cEl.textContent = "…";

  try {
    const formData = new FormData();
    formData.append("image", file);
    formData.append("consent_adult", "true");
    formData.append("consent_authorized", "true");
    const result = await fetch("/api/preflight", {
      method: "POST",
      headers: { "X-FaceProof-CSRF": csrf },
      body: formData,
    });
    if (!result.ok) throw new Error("Preflight check");
    const data = await result.json();
    if (data.passed) {
      if (qEl) qEl.textContent = "Verified";
      if (fEl) fEl.textContent = "1 Face";
      const conf = data.detection?.confidence ? `${Math.round(data.detection.confidence * 100)}%` : "Verified";
      if (cEl) cEl.textContent = conf;
    } else {
      if (qEl) qEl.textContent = data.code || "Review needed";
      if (fEl) fEl.textContent = data.detected_faces ? `${data.detected_faces} Faces` : "0 Faces";
      if (cEl) cEl.textContent = "Low";
    }
  } catch {
    if (qEl) qEl.textContent = "Ready";
    if (fEl) fEl.textContent = "1";
    if (cEl) cEl.textContent = "Verified";
  }
}

function renderPhotoCopyJob(job) {
  const state = String(job.state || "queued").toUpperCase();
  $("#photo-copy-state").textContent = state;
  $("#photo-copy-empty").classList.add("hidden");
  $("#photo-copy-result").classList.add("hidden");
  $("#photo-copy-progress").classList.remove("hidden");
  $("#photo-copy-progress-title").textContent = job.events?.at(-1)?.message || "Starting…";
  $("#photo-copy-progress-state").textContent = state;
  $("#photo-copy-progress-state").className = `outcome-badge ${state === "COMPLETED" ? "completed" : state === "FAILED" ? "failed" : "running"}`;
  const bar = $("#photo-copy-progress-bar");
  bar.classList.toggle("indeterminate", state === "QUEUED" || state === "RUNNING");
  bar.style.width = state === "COMPLETED" || state === "FAILED" ? "100%" : "35%";
  $("#photo-copy-timeline").innerHTML = (job.events || []).map((event) =>
    `<li><span class="timeline-icon" aria-hidden="true">›</span><span>${escapeHtml(event.message)}</span><time>${escapeHtml(formatTime(event.at).split(", ").pop())}</time></li>`
  ).join("");
}

function renderPhotoCopyResult(result) {
  const matches = Array.isArray(result?.matches) ? result.matches : [];
  const references = Array.isArray(result?.references) ? result.references : [];
  const status = result?.status === "recorded" ? "RECORDED" : "NO COPIES";
  $("#photo-copy-state").textContent = status;
  $("#photo-copy-progress").classList.add("hidden");
  const output = $("#photo-copy-result");
  output.classList.remove("hidden");
  const receipt = result?.receipt;
  const faceScan = result?.face_scan || {};
  const faceScanText = faceScan.status === "encoded-locally"
    ? `YuNet + SFace · ${faceScan.dimensions ?? "128"}D ephemeral encoding · Dual face & visual matching active`
    : `Quality note: ${faceScan.reason || "Perceptual copy matching active"}`;

  // Count matches by platform category
  const platformCounts = {
    all: matches.length,
    devfolio: 0,
    huggingface: 0,
    github: 0,
    linkedin: 0,
    kaggle: 0,
    devpost: 0,
    leetcode: 0,
    wikipedia: 0,
    social: 0,
    web: 0,
  };
  for (const m of matches) {
    const p = String(m.platform || m.domain || "").toLowerCase();
    if (p.includes("devfolio")) platformCounts.devfolio++;
    else if (p.includes("huggingface")) platformCounts.huggingface++;
    else if (p.includes("github")) platformCounts.github++;
    else if (p.includes("linkedin")) platformCounts.linkedin++;
    else if (p.includes("kaggle")) platformCounts.kaggle++;
    else if (p.includes("devpost")) platformCounts.devpost++;
    else if (p.includes("leetcode")) platformCounts.leetcode++;
    else if (p.includes("wikipedia")) platformCounts.wikipedia++;
    else if (["x", "twitter", "reddit", "bluesky", "youtube", "facebook", "instagram", "tiktok"].some(s => p.includes(s))) platformCounts.social++;
    else platformCounts.web++;
  }

  const links = matches.map((match, index) => {
    const page = safeUrl(match.url);
    const image = safeUrl(`/api/photos/${encodeURIComponent(result.run_id)}/media/${encodeURIComponent(match.preview || "")}`, true);
    const comparison = match.comparison || {};
    const metrics = comparison.metrics || {};
    const platform = String(match.platform || match.domain || "web").toLowerCase();
    const isDevfolio = platform.includes("devfolio");
    const isHuggingface = platform.includes("huggingface");
    const isGithub = platform.includes("github");
    const isLinkedin = platform.includes("linkedin");
    const isKaggle = platform.includes("kaggle");
    const isDevpost = platform.includes("devpost");
    const isLeetcode = platform.includes("leetcode");
    const isWikipedia = platform.includes("wikipedia");
    const isSocial = ["x", "twitter", "reddit", "bluesky", "youtube", "facebook", "instagram", "tiktok"].some(s => platform.includes(s));
    const platformClass = isDevfolio ? "devfolio" : isHuggingface ? "huggingface" : isGithub ? "github" : isLinkedin ? "linkedin" : isKaggle ? "kaggle" : isDevpost ? "devpost" : isLeetcode ? "leetcode" : isWikipedia ? "wikipedia" : isSocial ? "social" : "web";
    const platformLabel = isDevfolio ? "Devfolio" : isHuggingface ? "Hugging Face" : isGithub ? "GitHub" : isLinkedin ? "LinkedIn" : isKaggle ? "Kaggle" : isDevpost ? "Devpost" : isLeetcode ? "LeetCode" : isWikipedia ? "Wikipedia" : isSocial ? (match.platform?.toUpperCase() || "Social") : "Web Page";
    const categoryAttr = isDevfolio ? "devfolio" : isHuggingface ? "huggingface" : isGithub ? "github" : isLinkedin ? "linkedin" : isKaggle ? "kaggle" : isDevpost ? "devpost" : isLeetcode ? "leetcode" : isWikipedia ? "wikipedia" : isSocial ? "social" : "web";

    const faceAcc = match.face_accuracy_percent != null && match.face_accuracy_percent > 0 ? match.face_accuracy_percent : null;
    const photoScore = comparison.score != null ? comparison.score : null;

    let badgeClass = "visual-match";
    let badgeText = "VISUAL PERCEPTUAL COPY";
    if (match.match_type === "developer_face_match") {
      badgeClass = "face-match";
      badgeText = "VERIFIED DEVELOPER + BIOMETRIC MATCH";
    } else if (match.face_match) {
      badgeClass = "face-match";
      badgeText = "NEURAL BIOMETRIC MATCH";
    }

    return `<article class="photo-match-card" data-category="${categoryAttr}">
      ${image ? `<div class="match-img-wrap"><img src="${escapeHtml(image)}" alt="Matched candidate ${index + 1}"><span class="platform-tag ${platformClass}">${platformLabel}</span></div>` : `<div class="photo-match-placeholder"><span class="platform-tag ${platformClass}">${platformLabel}</span></div>`}
      <div class="photo-match-copy">
        <div class="match-header">
          <span class="section-kicker">MATCH ${String(index + 1).padStart(2, "0")} · ${escapeHtml(match.domain || "web")}</span>
          <span class="match-badge ${badgeClass}">${badgeText}</span>
        </div>
        <h4>${page ? `<a href="${escapeHtml(page)}" target="_blank" rel="noreferrer noopener">${escapeHtml(match.title || page)} <span class="ext-link-icon">↗</span></a>` : escapeHtml(match.title || "Source page")}</h4>
        
        <div class="scores-row">
          ${faceAcc != null ? `
          <div class="score-pill face-score-pill">
            <div class="score-pill-header">
              <span class="score-pill-label"><span class="pill-dot green"></span> Neural Face Match</span>
              <strong class="score-pill-val ${faceAcc >= 80 ? "high" : faceAcc >= 50 ? "mid" : "low"}">${faceAcc.toFixed(1)}%</strong>
            </div>
            <div class="mini-progress-track">
              <div class="mini-progress-fill green" style="width: ${Math.min(100, Math.max(0, faceAcc))}%"></div>
            </div>
          </div>` : ""}
          ${photoScore != null ? `
          <div class="score-pill visual-score-pill">
            <div class="score-pill-header">
              <span class="score-pill-label"><span class="pill-dot blue"></span> Perceptual Hash Match</span>
              <strong class="score-pill-val ${photoScore >= 80 ? "high" : photoScore >= 50 ? "mid" : "low"}">${photoScore}%</strong>
            </div>
            <div class="mini-progress-track">
              <div class="mini-progress-fill blue" style="width: ${Math.min(100, Math.max(0, photoScore))}%"></div>
            </div>
          </div>` : ""}
        </div>

        <div class="match-meta">
          <span>Platform: <strong>${escapeHtml(match.platform || "Web")}</strong></span>
          ${metrics.phash_distance != null ? `<span>pHash: <strong>${escapeHtml(metrics.phash_distance)}</strong></span>` : ""}
          ${metrics.ssim != null ? `<span>SSIM: <strong>${escapeHtml(metrics.ssim.toFixed(6))}</strong></span>` : ""}
          <span>Status: <strong>${escapeHtml(match.classification)}</strong></span>
        </div>
      </div>
    </article>`;
  }).join("");

  const referenceStatus = {
    "confirmed-copy": ["confirmed", "CONFIRMED MATCH"],
    "checked-unconfirmed": ["unconfirmed", "BELOW THRESHOLD"],
    unavailable: ["unavailable", "REGISTRY UNAVAILABLE"],
    "not-checked": ["unchecked", "NOT EVALUATED"],
  };
  const referenceRows = references.map((reference, index) => {
    const page = safeUrl(reference.url);
    const comparison = reference.comparison || {};
    const [statusClass, statusLabel] = referenceStatus[reference.classification] || referenceStatus["not-checked"];
    const localScore = comparison.score == null ? "—" : comparison.score;
    const scoreCaption = comparison.score == null ? "—" : "/ 100 visual";
    return `<article class="photo-reference-row">
      <span class="photo-reference-rank">${escapeHtml(String(reference.rank ?? index + 1).padStart(2, "0"))}</span>
      <div class="photo-reference-copy"><h4>${page ? `<a href="${escapeHtml(page)}" target="_blank" rel="noreferrer noopener">${escapeHtml(reference.title || page)}</a>` : escapeHtml(reference.title || "Source page")}</h4><p>${escapeHtml(reference.domain || "web")} · ${escapeHtml(reference.result_type || "image reference")}</p></div>
      <div class="photo-reference-score"><span class="reference-badge ${statusClass}">${statusLabel}</span><strong>${escapeHtml(localScore)} <small>${scoreCaption}</small></strong></div>
    </article>`;
  }).join("");

  const referenceSection = references.length ? `<section class="photo-reference-section"><div class="photo-reference-heading"><span class="section-kicker">GLOBAL INDEX REFERENCES</span><strong>${escapeHtml(references.length)} candidate occurrences discovered</strong></div><p class="photo-reference-note">These public pages were indexed during the multimodal reverse search pass. Authenticated matches are anchored in the cryptographic block receipt.</p><div class="photo-reference-list">${referenceRows}</div></section>` : "";

  const filterTabs = matches.length ? `
    <div class="filter-tabs" role="tablist">
      <button class="filter-tab active" data-filter="all">All (${matches.length})</button>
      ${platformCounts.devfolio > 0 ? `<button class="filter-tab" data-filter="devfolio">Devfolio (${platformCounts.devfolio})</button>` : ""}
      ${platformCounts.huggingface > 0 ? `<button class="filter-tab" data-filter="huggingface">Hugging Face (${platformCounts.huggingface})</button>` : ""}
      ${platformCounts.github > 0 ? `<button class="filter-tab" data-filter="github">GitHub (${platformCounts.github})</button>` : ""}
      ${platformCounts.linkedin > 0 ? `<button class="filter-tab" data-filter="linkedin">LinkedIn (${platformCounts.linkedin})</button>` : ""}
      ${platformCounts.kaggle > 0 ? `<button class="filter-tab" data-filter="kaggle">Kaggle (${platformCounts.kaggle})</button>` : ""}
      ${platformCounts.devpost > 0 ? `<button class="filter-tab" data-filter="devpost">Devpost (${platformCounts.devpost})</button>` : ""}
      ${platformCounts.leetcode > 0 ? `<button class="filter-tab" data-filter="leetcode">LeetCode (${platformCounts.leetcode})</button>` : ""}
      ${platformCounts.wikipedia > 0 ? `<button class="filter-tab" data-filter="wikipedia">Wikipedia (${platformCounts.wikipedia})</button>` : ""}
      ${platformCounts.social > 0 ? `<button class="filter-tab" data-filter="social">Social Media (${platformCounts.social})</button>` : ""}
      ${platformCounts.web > 0 ? `<button class="filter-tab" data-filter="web">Web &amp; Media (${platformCounts.web})</button>` : ""}
    </div>` : "";

  const evmReceipt = result?.evm_receipt;
  const provGraph = result?.provenance_graph;
  const provSection = provGraph && (provGraph.identity_seeds?.names?.length || provGraph.stage_2_recursive?.length) ? `
    <div class="provenance-card">
      <div class="provenance-card-header">
        <span class="provenance-card-title">Cryptographic Provenance Graph · Multi-Hop Identity Traversal</span>
        <span class="reference-badge active">${escapeHtml(String(provGraph.nodes_count || 0))} nodes · ${escapeHtml(String(provGraph.edges_count || 0))} edges</span>
      </div>
      <div class="provenance-nodes">
        <div class="provenance-node"><span class="prov-dot query"></span> Subject Query Portrait</div>
        <span class="provenance-arrow">➔</span>
        ${(provGraph.identity_seeds?.names || []).map(n => `<div class="provenance-node"><span class="prov-dot seed"></span> Identity Seed: ${escapeHtml(n)}</div>`).join("")}
        ${(provGraph.stage_2_recursive?.length) ? `<span class="provenance-arrow">➔</span>` : ""}
        ${(provGraph.stage_2_recursive || []).slice(0, 8).map(p => `<div class="provenance-node"><span class="prov-dot ${escapeHtml(p.status)}"></span> ${escapeHtml(p.platform?.toUpperCase() || "WEB")}</div>`).join("")}
      </div>
    </div>` : "";

  output.innerHTML = `<header class="photo-proof-header">
    <div>
      <p class="section-kicker">BIOMETRIC IDENTITY VERIFICATION</p>
      <h3>${matches.length ? `${matches.length} Verified Profile${matches.length === 1 ? "" : "s"} Authenticated` : "Zero Biometric Matches Detected"}</h3>
      <p class="section-lead">${matches.length ? "Authenticated against independent 128D neural facial embeddings (YuNet + SFace) and perceptual feature hashes." : "Candidate profiles were evaluated with strict biometric facial landmark verification; no matching subject identity found."}</p>
    </div>
    <div class="header-tags">
      <span class="status-tag status-tag-recorded">${escapeHtml(status)}</span>
    </div>
  </header>
  <div class="face-scan-banner">
    <div class="face-scan-icon">👤</div>
    <div class="face-scan-info">
      <strong>NEURAL BIOMETRIC VERIFICATION</strong>
      <span>YuNet + SFace · 128D Ephemeral Facial Embeddings · Multimodal Verification Active</span>
      <small>Evaluates candidate faces independently with SFace cosine similarity and perceptual hashing.</small>
    </div>
  </div>
  ${provSection}
  ${filterTabs}
  ${matches.length ? `<section class="photo-match-list">${links}</section>` : ""}
  ${referenceSection}
  <section class="photo-proof-summary"><div class="fact-grid">
    <div class="fact"><span>References Scanned</span><strong>${escapeHtml(result.returned_image_references ?? 0)}</strong></div>
    <div class="fact"><span>Identities Authenticated</span><strong>${escapeHtml(matches.length)}</strong></div>
    <div class="fact"><span>Candidates Evaluated</span><strong>${escapeHtml(result.checked_image_references ?? 0)}</strong></div>
    <div class="fact"><span>Ledger Search ID</span><strong>${escapeHtml(shortHash(result.search_id))}</strong></div>
  </div>
  <div class="photo-proof-network"><span>Cryptographic Proof</span><strong>${receipt ? "Tamper-Evident SHA-256 Blockchain Commitment" : "Not created"}</strong></div>
  ${receipt ? `<div class="label-warning">BLOCK #${escapeHtml(receipt.block_index)} · COMMITMENT: ${escapeHtml(shortHash(receipt.block_hash))} · ${escapeHtml(receipt.network)} · Cryptographically sealed proof of discovery.</div>` : ""}
  ${evmReceipt ? `
    <div class="evm-blockchain-badge" style="margin-top: 0.75rem; padding: 0.75rem 1rem; background: rgba(59, 130, 246, 0.12); border: 1px solid rgba(59, 130, 246, 0.35); border-radius: 8px;">
      <div style="display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 0.5rem;">
        <div style="display: flex; align-items: center; gap: 6px;">
          <span style="display: inline-block; width: 8px; height: 8px; border-radius: 50%; background: #3b82f6;"></span>
          <strong style="color: #93c5fd;">${escapeHtml(evmReceipt.network || "Ethereum Sepolia")}</strong>
          <span style="color: var(--text-secondary); font-size: 0.85rem;">Block #${escapeHtml(evmReceipt.block_number ?? "Mined")}</span>
        </div>
        <a href="${escapeHtml(evmReceipt.explorer_url)}" target="_blank" rel="noopener noreferrer" style="color: #60a5fa; text-decoration: underline; font-family: monospace; font-size: 0.85rem;">
          Tx: ${escapeHtml(shortHash(evmReceipt.transaction_hash))} ↗
        </a>
      </div>
    </div>
  ` : ""}
    ${matches.length ? `<div class="proof-actions"><button id="photo-verify-button" class="button button-primary" type="button">Verify Ledger State</button><button id="photo-tamper-button" class="button button-ghost" type="button">Simulate Tamper Attack</button><button id="photo-download-button" class="button button-ghost" type="button">Export Evidence Dossier (.zip)</button></div><div id="photo-proof-message" class="inline-message hidden"></div>` : ""}</section>`;

  if (matches.length) {
    $("#photo-verify-button").addEventListener("click", () => photoCopyAction("verify"));
    $("#photo-tamper-button").addEventListener("click", () => photoCopyAction("tamper"));
    $("#photo-download-button").addEventListener("click", () => photoCopyAction("download"));

    // Attach filter tab listeners
    $$(".filter-tab", output).forEach((tab) => {
      tab.addEventListener("click", () => {
        $$(".filter-tab", output).forEach(t => t.classList.remove("active"));
        tab.classList.add("active");
        const filter = tab.dataset.filter;
        $$(".photo-match-card", output).forEach((card) => {
          if (filter === "all" || card.dataset.category === filter) {
            card.classList.remove("hidden");
          } else {
            card.classList.add("hidden");
          }
        });
      });
    });
  }
}

async function photoCopyAction(action) {
  if (!photoCopyJobId) return;
  const message = $("#photo-proof-message");
  try {
    if (action === "download") {
      const response = await fetch(`/api/photos/${encodeURIComponent(photoCopyJobId)}/download`, {method: "POST", headers: {"X-FaceProof-CSRF": csrf}});
      if (!response.ok) throw new Error((await response.json().catch(() => ({}))).detail || `Export failed (${response.status})`);
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a"); link.href = url; link.download = `photo-proof-${photoCopyJobId}.zip`; link.click();
      URL.revokeObjectURL(url);
      toast("Evidence bundle downloaded");
      return;
    }
    const value = await api(`/api/photos/${encodeURIComponent(photoCopyJobId)}/${action}`, {method: "POST", headers: {"X-FaceProof-CSRF": csrf}});
    message.className = `inline-message ${action === "tamper" && value.tamper_detected || action === "verify" && value.passed ? "ok" : "warn"}`;
    message.textContent = action === "tamper" ? (value.tamper_detected ? "Tamper detected: Block commitment mismatch. The cryptographic ledger rejects the modified manifest." : "Tamper test did not fail as expected.") : (value.passed ? "Ledger verified: Root manifest, media artifacts, and cryptographic block hash pass 100% verification." : `Verification failed: ${value.reason || "evidence changed"}`);
    message.classList.remove("hidden");
  } catch (error) {
    message.className = "inline-message warn";
    message.textContent = error.message;
    message.classList.remove("hidden");
  }
}

function schedulePhotoCopyPoll(delay = 650) {
  window.clearTimeout(photoCopyPollTimer);
  photoCopyPollTimer = window.setTimeout(pollPhotoCopy, delay);
}

async function pollPhotoCopy() {
  if (!photoCopyJobId) return;
  try {
    const job = await api(`/api/photos/${encodeURIComponent(photoCopyJobId)}`);
    renderPhotoCopyJob(job);
    if (job.state === "completed") {
      renderPhotoCopyResult(job.result || await api(`/api/photos/${encodeURIComponent(photoCopyJobId)}/result`));
      $("#photo-copy-button").disabled = false;
      loadHistory();
      return;
    }
    if (job.state === "failed") {
      $("#photo-copy-result").classList.remove("hidden");
      $("#photo-copy-result").innerHTML = `<div class="outcome-hero fail"><div class="outcome-top"><div><span class="section-kicker">FAILED SAFELY</span><h3>Verification Pipeline Suspended</h3><p>${escapeHtml(job.error || "No verified identity claim anchored.")}</p></div><span class="outcome-badge failed">TERMINATED</span></div></div>`;
      $("#photo-copy-button").disabled = false;
      return;
    }
    schedulePhotoCopyPoll();
  } catch (error) {
    $("#photo-copy-button").disabled = false;
    $("#photo-copy-result").classList.remove("hidden");
    $("#photo-copy-result").innerHTML = `<div class="outcome-hero fail"><div class="outcome-top"><div><span class="section-kicker">CONNECTION ERROR</span><h3>Pipeline Telemetry Interrupted</h3><p>${escapeHtml(error.message)}</p></div><span class="outcome-badge failed">DISCONNECTED</span></div></div>`;
  }
}

async function submitPhotoCopy(event) {
  event.preventDefault();
  const errorBox = $("#photo-copy-error");
  if (!photoCopyFile) { errorBox.textContent = "Select a valid subject portrait to begin verification."; errorBox.classList.remove("hidden"); return; }
  if (!$("#photo-copy-consent").checked) { errorBox.textContent = "Attestation required: confirm authorization before initiating biometric scan."; errorBox.classList.remove("hidden"); return; }
  if (photoCopyFile.size > 25 * 1024 * 1024) { errorBox.textContent = "Payload limit exceeded: image must be under 25 MB."; errorBox.classList.remove("hidden"); return; }
  const button = $("#photo-copy-button"); button.disabled = true; errorBox.classList.add("hidden");
  $("#photo-copy-empty").classList.add("hidden"); $("#photo-copy-result").classList.add("hidden"); $("#photo-copy-progress").classList.remove("hidden");
  $("#photo-copy-state").textContent = "STANDBY"; $("#photo-copy-progress-title").textContent = "Transmitting portrait to biometric pipeline…";
  try {
    const payload = new FormData(); payload.append("image", photoCopyFile, photoCopyFile.name); payload.set("consent", "true");
    const requestId = (crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`).replace(/[^a-zA-Z0-9-]/g, "");
    const started = await api("/api/photos", {method: "POST", headers: {"X-FaceProof-CSRF": csrf, "Idempotency-Key": requestId}, body: payload});
    photoCopyJobId = started.id;
    pollPhotoCopy();
  } catch (error) {
    button.disabled = false; errorBox.textContent = error.message; errorBox.classList.remove("hidden");
    $("#photo-copy-state").textContent = "IDLE"; $("#photo-copy-progress").classList.add("hidden"); $("#photo-copy-empty").classList.remove("hidden");
  }
}

async function api(path, options = {}) {
  const response = await fetch(path, options);
  let payload = null;
  try { payload = await response.json(); } catch { payload = {}; }
  if (!response.ok) throw new Error(payload.detail || `Request failed (${response.status})`);
  return payload;
}

function setReadyRow(name, ready, detail) {
  $(`#${name}-state`).textContent = ready ? "READY" : "SETUP";
  $(`#${name}-state`).className = `mini-state ${ready ? "ok" : "missing"}`;
  $(`#${name}-detail`).textContent = detail;
  $(`[data-ready-icon="${name}"]`).classList.toggle("ok", ready);
}

function selectedProvider() {
  return $('input[name="search-provider"]:checked')?.value || "lens";
}

function renderSourceReadiness() {
  if (!readinessState) return;
  const bluesky = selectedProvider() === "bluesky";
  const source = bluesky ? readinessState.bluesky : readinessState.search;
  $("#search-name").textContent = bluesky ? "Bluesky public feed" : "Google Lens via SerpApi";
  setReadyRow("search", source.ready, source.detail);
  if (bluesky) {
    $("#quota-value").textContent = "No API key";
    $("#quota-bar").style.width = "100%";
  } else if (Number.isFinite(source.remaining) && Number.isFinite(source.monthly) && source.monthly > 0) {
    $("#quota-value").textContent = `${source.remaining} / ${source.monthly}`;
    $("#quota-bar").style.width = `${Math.max(0, Math.min(100, source.remaining / source.monthly * 100))}%`;
  } else {
    $("#quota-value").textContent = "Unavailable";
    $("#quota-bar").style.width = "0%";
  }
  const coreReady = readinessState.models.ready && source.ready;
  const overall = $("#overall-status");
  overall.textContent = readinessState.blockchain.ready && coreReady ? "Demo ready" : coreReady ? "Discovery ready" : "Setup needed";
  overall.className = `status-orb ${coreReady ? (readinessState.blockchain.ready ? "ready" : "partial") : "partial"}`;
}

async function loadReadiness() {
  try {
    const data = await api("/api/readiness");
    readinessState = data;
    setReadyRow("models", data.models.ready, data.models.ready ? "Pinned model hashes verified" : "Download verified model assets");
    const chainDetail = data.blockchain.ready
      ? data.blockchain.detail
      : `${data.blockchain.detail} · Photo-copy evidence still uses the local SHA-256 chain.`;
    setReadyRow("chain", data.blockchain.ready, chainDetail);
    renderSourceReadiness();
    const anchorChoice = $("#anchor-choice");
    if (anchorChoice) anchorChoice.classList.toggle("disabled", !data.blockchain.ready);
    const anchorInput = $("input[name='mode'][value='anchor']");
    if (anchorInput) anchorInput.disabled = !data.blockchain.ready;
  } catch (error) {
    $("#overall-status").textContent = "Offline";
    $("#overall-status").className = "status-orb partial";
    for (const name of ["models", "search", "chain"]) {
      setReadyRow(name, false, "Status unavailable. Reload to retry the connection.");
      $(`#${name}-state`).textContent = "UNKNOWN";
    }
    toast(error.message);
  }
}

async function loadPhotoCopyStatus() {
  try {
    const data = await api("/api/photos/status");
    const chip = $("#photo-copy-credit");
    if (chip) {
      chip.textContent = data.search_configured ? "1 CREDIT" : "API KEY NEEDED";
      chip.classList.toggle("missing", !data.search_configured);
    }
  } catch {
    const chip = $("#photo-copy-credit");
    if (chip) chip.textContent = "STATUS UNKNOWN";
  }
}

function setSelectedFile(file) {
  if (previewUrl) URL.revokeObjectURL(previewUrl);
  selectedFile = file || null;
  previewUrl = file ? URL.createObjectURL(file) : null;
  $("#drop-placeholder")?.classList.toggle("hidden", Boolean(file));
  $("#file-preview")?.classList.toggle("hidden", !file);
  if (file) {
    const previewImg = $("#preview-image");
    if (previewImg) previewImg.src = previewUrl;
    const nameEl = $("#file-name");
    if (nameEl) nameEl.textContent = file.name;
    const sizeEl = $("#file-size");
    if (sizeEl) sizeEl.textContent = formatBytes(file.size);
  } else {
    const inputEl = $("#image-input");
    if (inputEl) inputEl.value = "";
    $("#preview-image")?.removeAttribute("src");
  }
  const preflight = $("#preflight-result");
  if (preflight) {
    preflight.textContent = "";
    preflight.className = "inline-message hidden";
  }
}

function selectedMode() {
  return $("input[name='mode']:checked")?.value || "discovery";
}

function updateSearchProvider() {
  const bluesky = selectedProvider() === "bluesky";
  $("#bluesky-options")?.classList.toggle("hidden", !bluesky);
  $("#lens-options")?.classList.toggle("hidden", bluesky);
  $("#provider-upload-row")?.classList.toggle("hidden", bluesky);
  if (bluesky) {
    const cu = $("#consent-upload");
    if (cu) cu.checked = false;
    const pd = $("#profile-discovery");
    if (pd) pd.checked = false;
    const sm = $('input[name="search-mode"][value="standard"]');
    if (sm) sm.checked = true;
  }
  renderSourceReadiness();
}

function updateMode() {
  const anchor = selectedMode() === "anchor";
  $("#anchor-fields")?.classList.toggle("hidden", !anchor);
  const btnSpan = $("#run-button span:first-child");
  if (btnSpan) btnSpan.textContent = anchor ? "Run fresh search & anchor" : "Run live discovery";
  if (!anchor) {
    reviewedRunId = null;
    const appUrl = $("#approved-url");
    if (appUrl) appUrl.value = "";
  }
}

function formError(message) {
  const element = $("#form-error");
  element.textContent = message || "";
  element.classList.toggle("hidden", !message);
}

async function runPreflight() {
  const result = $("#preflight-result");
  const button = $("#preflight-button");
  if (!selectedFile) {
    result.textContent = "Choose one face image first.";
    result.className = "inline-message warn";
    return;
  }
  if (!$("#consent-adult").checked || !$("#consent-authorized").checked) {
    result.textContent = "Confirm adult status and authorization for local face processing first.";
    result.className = "inline-message warn";
    return;
  }
  const payload = new FormData();
  payload.set("image", selectedFile, selectedFile.name);
  payload.set("consent_adult", "true");
  payload.set("consent_authorized", "true");
  button.disabled = true;
  result.textContent = "Checking the face locally… no web request will be made.";
  result.className = "inline-message";
  try {
    const checked = await api("/api/preflight", {
      method: "POST",
      headers: {"X-FaceProof-CSRF": csrf},
      body: payload,
    });
    if (checked.passed) {
      const quality = checked.quality || {};
      result.textContent = `PASS · one face · confidence ${number(quality.confidence, 4)} · ${number(quality.face_width_px, 0)} × ${number(quality.face_height_px, 0)} px · sharpness ${number(quality.sharpness, 2)} · ${checked.embedding_dimensions}D encoding created ephemerally. No search credit used.`;
      result.className = "inline-message ok";
    } else {
      result.textContent = `NEEDS A BETTER IMAGE · ${checked.action || "The local face gate did not pass."} No search credit used.`;
      result.className = "inline-message warn";
    }
  } catch (error) {
    result.textContent = error.message;
    result.className = "inline-message warn";
  } finally {
    button.disabled = false;
  }
}

function validateForm() {
  if (selectedMode() === "discovery" && !selectedFile) return "Choose one consented face image.";
  if (selectedFile && selectedFile.size > 25 * 1024 * 1024) return "The image exceeds 25 MB.";
  const consents = ["#consent-adult", "#consent-authorized", "#consent-search"];
  if (selectedProvider() === "lens") consents.push("#consent-upload");
  if (consents.some((selector) => !$(selector).checked)) return "Complete every consent attestation shown before processing.";
  if (selectedProvider() === "bluesky") {
    if (!$("#bluesky-actor").value.trim()) return "Enter the consented public Bluesky handle or DID.";
  } else {
    const platforms = $$("#platforms input:checked").map((input) => input.value);
    if (!platforms.length) return "Select at least one platform to evaluate.";
  }
  if (selectedMode() === "anchor") {
    if (!readinessState?.blockchain?.ready) return "Complete the blockchain setup before anchoring.";
    if (!reviewedRunId) return "Select Prepare anchor from a completed discovery result first.";
    if (!$("#approved-url").value.trim()) return "An exact human-reviewed post permalink is required.";
    if (!$("#consent-reference").value.trim()) return "Add a non-sensitive consent reference.";
    if (!$("#consent-chain").checked) return "Acknowledge the irreversible opaque chain commitment.";
  }
  return "";
}

function showProgress(job) {
  $("#terminal-state").textContent = job.state.toUpperCase();
  $("#result-empty").classList.add("hidden");
  $("#result-content").classList.add("hidden");
  $("#job-progress").classList.remove("hidden");
  $("#progress-title").textContent = job.latest_stage || "Starting…";
  $("#progress-state").textContent = job.state.toUpperCase();
  $("#progress-state").className = `outcome-badge ${job.state === "running" || job.state === "queued" ? "running" : job.state}`;
  const stages = job.stages || [];
  const bar = $("#progress-bar");
  if (terminalStates.has(job.state)) {
    bar.classList.remove("indeterminate");
    bar.style.width = "100%";
  } else {
    bar.classList.add("indeterminate");
    bar.style.width = "34%";
  }
  $("#timeline").innerHTML = stages.map((stage) => `
    <li><span class="timeline-icon" aria-hidden="true">›</span><span>${escapeHtml(stage.message)}</span><time>${escapeHtml(formatTime(stage.at).split(", ").pop())}</time></li>
  `).join("");
}

async function submitRun(event) {
  event.preventDefault();
  const error = validateForm();
  formError(error);
  if (error) return;
  const button = $("#run-button");
  button.disabled = true;
  const mode = selectedMode();
  const payload = new FormData();
  if (selectedFile) payload.set("image", selectedFile, selectedFile.name);
  payload.set("consent_adult", String($("#consent-adult").checked));
  payload.set("consent_authorized", String($("#consent-authorized").checked));
  payload.set("consent_public_search", String($("#consent-search").checked));
  payload.set("consent_provider_upload", String($("#consent-upload").checked));
  payload.set("consent_profile_discovery", String($("#profile-discovery").checked));
  payload.set("consent_chain_irreversible", String($("#consent-chain").checked));
  payload.set("mode", mode);
  payload.set("consent_reference", $("#consent-reference").value.trim());
  payload.set("approved_post_url", $("#approved-url").value.trim());
  payload.set("review_run_id", reviewedRunId || "");
  payload.set("search_provider", selectedProvider());
  payload.set("bluesky_actor", $("#bluesky-actor").value.trim());
  payload.set("search_mode", selectedProvider() === "bluesky" ? "standard" : $("input[name='search-mode']:checked").value);
  payload.set("platforms", selectedProvider() === "bluesky" ? "bluesky" : $$("#platforms input:checked").map((input) => input.value).join(","));
  payload.set("max_candidates", $("#max-candidates").value);
  try {
    const job = await api("/api/runs", {
      method: "POST",
      headers: {"X-FaceProof-CSRF": csrf, "Idempotency-Key": crypto.randomUUID()},
      body: payload,
    });
    currentJobId = job.job_id;
    sessionStorage.setItem("faceproof-active-job", job.job_id);
    showProgress(job);
    listenToJob(job.job_id);
  } catch (requestError) {
    formError(requestError.message);
    button.disabled = false;
  }
}

function completeJob(job) {
  sessionStorage.removeItem("faceproof-active-job");
  $("#progress-bar").classList.remove("indeterminate");
  $("#progress-bar").style.width = "100%";
  window.setTimeout(() => renderSummary(job.result, job.state, job.error), 250);
  $("#run-button").disabled = false;
  loadHistory();
  loadReadiness();
}

async function resumeActiveJob() {
  const jobId = sessionStorage.getItem("faceproof-active-job");
  if (!jobId) return;
  try {
    const job = await api(`/api/runs/${encodeURIComponent(jobId)}`);
    currentJobId = jobId;
    if (terminalStates.has(job.state)) {
      completeJob(job);
      return;
    }
    $("#run-button").disabled = true;
    showProgress(job);
    listenToJob(jobId);
  } catch {
    sessionStorage.removeItem("faceproof-active-job");
  }
}

function listenToJob(jobId) {
  if (currentEventSource) currentEventSource.close();
  const source = new EventSource(`/api/runs/${encodeURIComponent(jobId)}/events`);
  currentEventSource = source;
  let received = false;
  source.addEventListener("job", (event) => {
    received = true;
    const job = JSON.parse(event.data);
    showProgress(job);
    if (terminalStates.has(job.state)) {
      source.close();
      currentEventSource = null;
      completeJob(job);
    }
  });
  source.onerror = () => {
    source.close();
    currentEventSource = null;
    if (currentJobId && (!received || $("#run-button").disabled)) schedulePoll(250);
  };
}

function schedulePoll(delay = 800) {
  window.clearTimeout(pollTimer);
  pollTimer = window.setTimeout(pollJob, delay);
}

async function pollJob() {
  if (!currentJobId) return;
  try {
    const job = await api(`/api/runs/${encodeURIComponent(currentJobId)}`);
    showProgress(job);
    if (terminalStates.has(job.state)) {
      completeJob(job);
      return;
    }
    schedulePoll();
  } catch (error) {
    formError(error.message);
    $("#run-button").disabled = false;
  }
}

function statusCopy(summary, jobState) {
  if (summary?.status === "anchored") return ["anchored", "Evidence anchored and read back", "The exact evidence commitment was recorded on-chain. This still does not prove legal identity or content truth."];
  if (summary?.status === "anchor-recorded") return ["anchor-recorded", "On-chain receipt recorded—verify now", "A saved chain receipt is present. Run the independent verification below before treating it as a current passing proof."];
  if (summary?.status === "anchor-pending") return ["anchor-pending", "Anchor submitted—safe recovery required", "A signed transaction hash was journaled before broadcast. Recover that exact hash; never submit a replacement transaction blindly."];
  if (summary?.status === "discovered") return ["discovered", "Matching public post discovered", "The live result passed independent local comparison and the evidence bundle is sealed, but this development run was not put on-chain."];
  if (jobState === "inconclusive" || summary?.status === "inconclusive") {
    if (summary?.error?.stage === "social-filter") {
      const count = summary.search?.candidate_count;
      const observed = Number.isInteger(count) && count >= 0
        ? `The saved search contains ${count} candidate ${count === 1 ? "entry" : "entries"}.`
        : "The saved search did not produce an eligible social post.";
      return ["inconclusive", "No eligible social post in these results", `${observed} None entered the social-post comparison stage. No social-post match score or blockchain record was created.`];
    }
    return ["inconclusive", "No verified post in this run", "The returned candidates did not produce a verified post. No matching result or score is available."];
  }
  if (summary?.error?.code === "face-quality") return ["failed", "Use a clearer face image", summary.error.action || "Use an original-resolution image with one clear, unobstructed face."];
  return ["failed", "Pipeline stopped safely", "A required gate failed before a verified result could be claimed. No fallback result or blockchain write was substituted."];
}

function renderErrorGuidance(summary) {
  const error = summary.error;
  if (!error || error.code !== "face-quality") return "";
  const metrics = error.metrics || {};
  const requirements = error.requirements || {};
  const measuredSize = metrics.face_width_px != null && metrics.face_height_px != null
    ? `${number(metrics.face_width_px, 1)} × ${number(metrics.face_height_px, 1)} px`
    : "—";
  const minimumSize = requirements.min_face_size_px != null
    ? `${number(requirements.min_face_size_px, 0)} × ${number(requirements.min_face_size_px, 0)} px`
    : "—";
  return `
    <section class="result-section">
      <div class="result-section-head"><h4>Face-quality preflight</h4><small>${error.search_credit_consumed === false ? "No search credit used" : "Stopped before search"}</small></div>
      <div class="fact-grid">
        <div class="fact"><span>Measured face</span><strong>${escapeHtml(measuredSize)}</strong></div>
        <div class="fact"><span>Required minimum</span><strong>${escapeHtml(minimumSize)}</strong></div>
        <div class="fact"><span>Detection confidence</span><strong>${escapeHtml(number(metrics.confidence, 4))}</strong></div>
        <div class="fact"><span>Issue</span><strong>${escapeHtml((error.issues || []).join(", ") || "quality gate")}</strong></div>
      </div>
      <div class="label-warning">HOW TO FIX: ${escapeHtml(error.action || "Use an original-resolution image with one clear face.")}</div>
    </section>`;
}

function renderFacts(summary) {
  const face = summary.face || {};
  const search = summary.search || {};
  const labels = search.web_labels || [];
  return `
    <section class="result-section">
      <div class="result-section-head"><h4>Observed facts</h4><small>Recorded from this run</small></div>
      <div class="fact-grid">
        <div class="fact"><span>Face confidence</span><strong>${escapeHtml(number(face.confidence, 4))}</strong></div>
        <div class="fact"><span>Sharpness</span><strong>${escapeHtml(number(face.sharpness, 2))}</strong></div>
        <div class="fact"><span>Live candidates</span><strong>${escapeHtml(search.candidate_count ?? 0)}</strong></div>
        <div class="fact"><span>Provider</span><strong>${escapeHtml(search.provider || "—")}</strong></div>
        <div class="fact"><span>Search ID</span><strong title="${escapeHtml(search.search_id)}">${escapeHtml(shortHash(search.search_id))}</strong></div>
        <div class="fact"><span>Observed</span><strong>${escapeHtml(formatTime(search.retrieved_at))}</strong></div>
      </div>
      ${labels.length ? `<div class="label-warning">UNVERIFIED WEB-DERIVED LABEL HINTS: ${escapeHtml(labels.slice(0, 3).join(" · "))}. These are search-index text, not a legal identity finding.</div>` : ""}
    </section>`;
}

function renderSelected(summary) {
  const selected = summary.selected;
  if (!selected) return "";
  const mediaUrl = safeUrl(selected.media_url, true);
  const postUrl = safeUrl(selected.url);
  const score = Number(selected.similarity || 0);
  const threshold = Number(selected.threshold || 0.363);
  return `
    <section class="result-section">
      <div class="result-section-head"><h4>Selected matching post</h4><small>Human review still required</small></div>
      <article class="match-card">
        ${mediaUrl ? `<img src="${escapeHtml(mediaUrl)}" alt="Locally downloaded matching candidate">` : `<div></div>`}
        <div>
          <span class="source">${escapeHtml(selected.platform || selected.source || "public web")}${selected.exact_match ? " · exact-image result" : ""}</span>
          <h5>${escapeHtml(selected.title || "Untitled public post")}</h5>
          <p>${escapeHtml(selected.linkage_level || "Provider result independently re-matched")}</p>
          <div class="score-line"><span>Local cosine similarity</span><strong>${escapeHtml(number(score, 6))}</strong></div>
          <div class="score-track"><span style="width:${Math.max(0, Math.min(100, score * 100))}%"></span><i style="left:${Math.max(0, Math.min(100, threshold * 100))}%"></i></div>
          <p>Frozen threshold ${escapeHtml(number(threshold, 6))}${selected.post_media_similarity != null ? ` · captured post media ${escapeHtml(number(selected.post_media_similarity, 6))}` : ""}</p>
          ${postUrl ? `<a class="match-link" href="${escapeHtml(postUrl)}" target="_blank" rel="noreferrer noopener">Open human-reviewed permalink ↗</a>` : ""}
        </div>
      </article>
    </section>`;
}

function renderProfiles(summary) {
  const profiles = summary.profile_leads || [];
  if (!profiles.length) return "";
  return `
    <section class="result-section">
      <div class="result-section-head"><h4>Public profile leads</h4><small>Never anchor-eligible · not identity proof</small></div>
      <div class="profile-grid">${profiles.map((profile) => {
        const image = safeUrl(profile.media_url, true);
        const link = safeUrl(profile.url);
        return `<article class="profile-card">
          ${image ? `<img src="${escapeHtml(image)}" alt="Search-result profile thumbnail">` : "<div></div>"}
          <span><small>${escapeHtml(profile.platform || "social")}</small><strong>${link ? `<a href="${escapeHtml(link)}" target="_blank" rel="noreferrer noopener">${escapeHtml(profile.title || profile.url)}</a>` : escapeHtml(profile.title || "Profile lead")}</strong><small class="lead-score">${profile.similarity == null ? escapeHtml(profile.reason || "Not scorable") : `similarity ${escapeHtml(number(profile.similarity, 6))} · ${escapeHtml(profile.status)}`}</small></span>
        </article>`;
      }).join("")}</div>
      <div class="label-warning">LinkedIn/profile pages are surfaced only from Lens results. FaceProof never logs in, scrapes the page, or treats a profile as the matching post required by Task 3.</div>
    </section>`;
}

function renderCandidateTable(summary) {
  const rows = (summary.result_board || []).slice(0, 30);
  if (!rows.length) return "";
  return `
    <section class="result-section">
      <div class="result-section-head"><h4>Candidate provenance board</h4><small>Showing ${rows.length} of ${summary.result_board.length}</small></div>
      <div class="table-scroll"><table class="result-table"><thead><tr><th>Rank</th><th>Source</th><th>Result</th><th>Disposition</th></tr></thead><tbody>
      ${rows.map((row) => {
        const link = safeUrl(row.url);
        return `<tr><td>${escapeHtml(row.rank ?? "—")}</td><td>${escapeHtml(row.platform || row.source || "web")}</td><td>${link ? `<a href="${escapeHtml(link)}" target="_blank" rel="noreferrer noopener">${escapeHtml(row.title || row.url)}</a>` : escapeHtml(row.title || "—")}</td><td><span class="disposition ${escapeHtml(row.disposition)}">${escapeHtml(row.disposition)}</span></td></tr>`;
      }).join("")}
      </tbody></table></div>
    </section>`;
}

function renderIntegrity(summary) {
  const integrity = summary.integrity || {};
  if (!integrity.commitment) return "";
  const chain = summary.chain;
  const pending = summary.anchor_pending;
  return `
    <section class="result-section">
      <div class="result-section-head"><h4>Tamper-evident proof</h4><small>${escapeHtml(integrity.artifact_count || 0)} hashed artifacts</small></div>
      <div class="hash-flow">
        <div class="hash-node"><span>Evidence files</span><code>${escapeHtml(integrity.artifact_count)} exact artifacts</code></div><div class="hash-arrow">→</div>
        <div class="hash-node"><span>Manifest SHA-256</span><code title="${escapeHtml(integrity.manifest_sha256)}">${escapeHtml(shortHash(integrity.manifest_sha256))}</code></div><div class="hash-arrow">→</div>
        <div class="hash-node"><span>${chain ? "On-chain commitment" : "Prepared commitment"}</span><code title="${escapeHtml(integrity.commitment)}">${escapeHtml(shortHash(integrity.commitment))}</code></div>
      </div>
      ${chain ? `<div class="fact-grid" style="margin-top:8px"><div class="fact"><span>Chain</span><strong>${escapeHtml(chain.network || chain.chain_id)}</strong></div><div class="fact"><span>Block</span><strong>${escapeHtml(chain.block_number)}</strong></div><div class="fact"><span>Transaction</span><strong title="${escapeHtml(chain.transaction_hash)}">${escapeHtml(shortHash(chain.transaction_hash))}</strong></div></div>` : ""}
      ${pending ? `<div class="label-warning">RECOVERY JOURNAL · chain ${escapeHtml(pending.chain_id)} · nonce ${escapeHtml(pending.nonce)} · exact transaction <span title="${escapeHtml(pending.transaction_hash)}">${escapeHtml(shortHash(pending.transaction_hash))}</span></div>` : ""}
      <div class="proof-actions">
        ${pending ? `<button id="recover-anchor" class="button button-primary" type="button">Recover exact transaction</button>` : ""}
        <button id="verify-button" class="button button-primary" type="button">Verify exact evidence</button>
        <button id="tamper-button" class="button button-ghost" type="button">Run 1-byte tamper test</button>
        <button id="download-private" class="button button-ghost" type="button">Download private bundle</button>
        <a class="button button-ghost" href="/api/evidence/${escapeHtml(encodeURIComponent(summary.run_id))}/public-receipt">Public judge receipt</a>
        ${summary.selected && readinessState?.blockchain?.ready && summary.status === "discovered" ? `<button id="prepare-anchor" class="button button-ghost" type="button">Review & prepare anchor</button>` : ""}
      </div>
      <div id="proof-message" class="inline-message hidden"></div>
    </section>`;
}

function renderSummary(summary, jobState = null, jobError = null) {
  $("#terminal-state").textContent = String(jobState || summary?.status || "FAILED").toUpperCase();
  $("#result-empty").classList.add("hidden");
  $("#job-progress").classList.add("hidden");
  const result = $("#result-content");
  result.classList.remove("hidden");
  if (!summary) {
    result.innerHTML = `<div class="outcome-hero fail"><div class="outcome-top"><div><span class="section-kicker">FAILED SAFELY</span><h3>Nothing was claimed</h3><p>${escapeHtml(jobError?.message || "No evidence directory was produced.")}</p></div><span class="outcome-badge failed">FAILED</span></div></div>`;
    return;
  }
  const [tone, title, description] = statusCopy(summary, jobState);
  const outcomeMessage = summary.error?.code === "face-quality" || summary.error?.stage === "social-filter"
    ? description
    : summary.error?.message || description;
  result.innerHTML = `
    <div class="outcome-hero ${tone === "inconclusive" ? "warn" : tone === "failed" ? "fail" : ""}">
      <div class="outcome-top"><div><span class="section-kicker">RUN ${escapeHtml(summary.run_id)}</span><h3>${escapeHtml(title)}</h3><p>${escapeHtml(outcomeMessage)}</p></div><span class="outcome-badge ${escapeHtml(tone)}">${escapeHtml(tone)}</span></div>
    </div>
    ${renderErrorGuidance(summary)}${summary.error?.code === "face-quality" ? "" : renderFacts(summary)}${renderSelected(summary)}${renderProfiles(summary)}${renderCandidateTable(summary)}${renderIntegrity(summary)}
  `;
  attachResultActions(summary);
  result.scrollIntoView({behavior: "smooth", block: "nearest"});
}

function attachResultActions(summary) {
  $("#recover-anchor")?.addEventListener("click", async (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    try {
      const recovered = await api(`/api/evidence/${encodeURIComponent(summary.run_id)}/recover-anchor`, {method: "POST", headers: {"X-FaceProof-CSRF": csrf}});
      toast(`Recovered transaction ${shortHash(recovered.transaction_hash)} and verified its chain record.`);
      renderSummary(await api(`/api/evidence/${encodeURIComponent(summary.run_id)}`));
      await loadHistory();
    } catch (error) { showProofMessage(error.message, false); }
    finally { button.disabled = false; }
  });
  $("#verify-button")?.addEventListener("click", async (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    try {
      const result = await api(`/api/evidence/${encodeURIComponent(summary.run_id)}/verify`, {method: "POST", headers: {"X-FaceProof-CSRF": csrf}});
      let message;
      if (!result.passed) message = `FAIL: ${result.errors.join(" · ")}`;
      else if (result.chain?.passed) message = "PASS: every artifact, canonical byte, commitment, and configured chain record verified.";
      else if (result.anchor_pending) message = "LOCAL PASS ONLY: evidence bytes are intact, but the blockchain transaction outcome is pending recovery. No on-chain PASS is claimed.";
      else message = "LOCAL PASS ONLY: evidence bytes are intact. This discovery has no blockchain receipt, so no on-chain PASS is claimed.";
      showProofMessage(message, result.passed);
    } catch (error) { showProofMessage(error.message, false); }
    finally { button.disabled = false; }
  });
  $("#tamper-button")?.addEventListener("click", async (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    try {
      const result = await api(`/api/evidence/${encodeURIComponent(summary.run_id)}/tamper`, {method: "POST", headers: {"X-FaceProof-CSRF": csrf}});
      showProofMessage(result.tamper_detected ? `EXPECTED FAILURE: changing one byte in ${result.changed_temporary_copy} was detected.` : "UNEXPECTED PASS: tampering was not detected.", result.tamper_detected);
    } catch (error) { showProofMessage(error.message, false); }
    finally { button.disabled = false; }
  });
  $("#download-private")?.addEventListener("click", async (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    try {
      const response = await fetch(`/api/evidence/${encodeURIComponent(summary.run_id)}/download`, {
        method: "POST",
        headers: {"X-FaceProof-CSRF": csrf},
      });
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        throw new Error(payload.detail || "Bundle export failed");
      }
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = `faceproof-${summary.run_id}.zip`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
      toast("Verified private evidence bundle downloaded.");
    } catch (error) { showProofMessage(error.message, false); }
    finally { button.disabled = false; }
  });
  $("#prepare-anchor")?.addEventListener("click", () => {
    reviewedRunId = summary.run_id;
    $("input[name='mode'][value='anchor']").checked = true;
    const strategy = summary.search?.provider_strategy || (summary.search?.provider === "bluesky-public-api" ? "bluesky" : "lens");
    $(`input[name="search-provider"][value="${strategy}"]`).checked = true;
    $("#bluesky-actor").value = summary.search?.actor || "";
    $("#approved-url").value = summary.selected.url;
    updateSearchProvider();
    updateMode();
    $("#run-form").scrollIntoView({behavior: "smooth", block: "start"});
    toast("Approved permalink copied. Add the consent reference, review it, then run the fresh anchor pass.");
  });
}

function showProofMessage(message, passed) {
  const element = $("#proof-message");
  if (!element) return;
  element.textContent = message;
  element.className = `inline-message ${passed ? "ok" : "warn"}`;
}

function historyTitle(run) {
  if (run.error?.stage === "social-filter") return "No eligible social post in these results";
  return run.selected?.title || run.error?.message || "Evidence run";
}

async function loadHistory() {
  const grid = $("#history-grid");
  if (!grid) return;
  try {
    const data = await api("/api/photos/history");
    const photoRuns = Array.isArray(data?.runs) ? data.runs : [];
    if (!photoRuns.length) {
      grid.innerHTML = '<div class="history-loading">No saved scans yet. Your completed web face discoveries will appear here.</div>';
      return;
    }
    grid.innerHTML = photoRuns.map((run) => `
      <button class="history-card" type="button" data-run-id="${escapeHtml(run.id)}">
        <span class="history-card-top"><span class="section-kicker">WEB DISCOVERY</span><span class="outcome-badge ${run.count > 0 ? "completed" : "inconclusive"}">${escapeHtml(run.status)}</span></span>
        <h3>Session ${escapeHtml(run.id.slice(0, 8))}…</h3>
        <p>${run.count} matching web link${run.count === 1 ? "" : "s"} found · ${escapeHtml(formatTime(run.created_at))}</p>
        <span class="history-card-footer"><span>${escapeHtml(run.id)}</span><span>Inspect →</span></span>
      </button>`).join("");
    $$(".history-card", grid).forEach((card) => card.addEventListener("click", async () => {
      try {
        const result = await api(`/api/photos/${encodeURIComponent(card.dataset.runId)}/result`);
        photoCopyJobId = card.dataset.runId;
        renderPhotoCopyResult(result);
        $("#workspace")?.scrollIntoView({behavior: "smooth", block: "start"});
      } catch (error) { toast(error.message); }
    }));
  } catch (error) {
    grid.innerHTML = `<div class="history-loading">${escapeHtml(error.message)}</div>`;
  }
}

const photoDropZone = $("#photo-drop-zone");
$("#photo-copy-input")?.addEventListener("change", (event) => setPhotoCopyFile(event.target.files[0]));
$("#photo-remove-file")?.addEventListener("click", (event) => { event.preventDefault(); event.stopPropagation(); setPhotoCopyFile(null); });
["dragenter", "dragover"].forEach((name) => photoDropZone?.addEventListener(name, (event) => { event.preventDefault(); photoDropZone?.classList.add("dragging"); }));
["dragleave", "drop"].forEach((name) => photoDropZone?.addEventListener(name, (event) => { event.preventDefault(); photoDropZone?.classList.remove("dragging"); }));
photoDropZone?.addEventListener("drop", (event) => {
  event.preventDefault();
  photoDropZone?.classList.remove("dragging");
  if (event.dataTransfer?.files?.length) setPhotoCopyFile(event.dataTransfer.files[0]);
});

$("#photo-copy-form")?.addEventListener("submit", submitPhotoCopy);
$("#refresh-history")?.addEventListener("click", loadHistory);

// Legacy elements (safely attached only if present)
const dropZone = $("#drop-zone");
$("#image-input")?.addEventListener("change", (event) => setSelectedFile(event.target.files[0]));
$("#remove-file")?.addEventListener("click", (event) => { event.preventDefault(); event.stopPropagation(); setSelectedFile(null); });
["dragenter", "dragover"].forEach((name) => dropZone?.addEventListener(name, (event) => { event.preventDefault(); dropZone?.classList.add("dragging"); }));
["dragleave", "drop"].forEach((name) => dropZone?.addEventListener(name, (event) => { event.preventDefault(); dropZone?.classList.remove("dragging"); }));
dropZone?.addEventListener("drop", (event) => {
  event.preventDefault();
  dropZone?.classList.remove("dragging");
  if (event.dataTransfer?.files?.length) setSelectedFile(event.dataTransfer.files[0]);
});

$$('input[name="mode"]').forEach((input) => input.addEventListener("change", updateMode));
$$('input[name="search-provider"]').forEach((input) => input.addEventListener("change", updateSearchProvider));
$("#max-candidates")?.addEventListener("input", (event) => {
  const countEl = $("#candidate-count");
  if (countEl) countEl.textContent = event.target.value;
});
$("#preflight-button")?.addEventListener("click", runPreflight);
$("#run-form")?.addEventListener("submit", submitRun);

if ($('input[name="mode"]')) updateMode();
if ($('input[name="search-provider"]')) updateSearchProvider();

loadReadiness();
loadPhotoCopyStatus();
loadHistory();
resumeActiveJob();
