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

async function loadReadiness() {
  try {
    const data = await api("/api/readiness");
    readinessState = data;
    setReadyRow("models", data.models.ready, data.models.ready ? "Pinned model hashes verified" : "Download verified model assets");
    setReadyRow("search", data.search.ready, data.search.detail);
    setReadyRow("chain", data.blockchain.ready, data.blockchain.detail);
    const coreReady = data.models.ready && data.search.ready;
    const overall = $("#overall-status");
    overall.textContent = data.blockchain.ready && coreReady ? "Demo ready" : coreReady ? "Discovery ready" : "Setup needed";
    overall.className = `status-orb ${coreReady ? (data.blockchain.ready ? "ready" : "partial") : "partial"}`;
    if (Number.isFinite(data.search.remaining) && Number.isFinite(data.search.monthly)) {
      $("#quota-value").textContent = `${data.search.remaining} / ${data.search.monthly}`;
      $("#quota-bar").style.width = `${Math.max(0, Math.min(100, data.search.remaining / data.search.monthly * 100))}%`;
    } else {
      $("#quota-value").textContent = "Unavailable";
    }
    $("#metric-accuracy").textContent = `${(data.accuracy.scored_pair_accuracy * 100).toFixed(2)}%`;
    $("#metric-coverage").textContent = `${(data.accuracy.pair_coverage * 100).toFixed(2)}%`;
    $("#metric-scope").textContent = data.accuracy.scope;
    const anchorChoice = $("#anchor-choice");
    anchorChoice.classList.toggle("disabled", !data.blockchain.ready);
    $("input[name='mode'][value='anchor']").disabled = !data.blockchain.ready;
  } catch (error) {
    $("#overall-status").textContent = "Offline";
    $("#overall-status").className = "status-orb partial";
    toast(error.message);
  }
}

function setSelectedFile(file) {
  if (previewUrl) URL.revokeObjectURL(previewUrl);
  selectedFile = file || null;
  previewUrl = file ? URL.createObjectURL(file) : null;
  $("#drop-placeholder").classList.toggle("hidden", Boolean(file));
  $("#file-preview").classList.toggle("hidden", !file);
  if (file) {
    $("#preview-image").src = previewUrl;
    $("#file-name").textContent = file.name;
    $("#file-size").textContent = formatBytes(file.size);
  } else {
    $("#image-input").value = "";
    $("#preview-image").removeAttribute("src");
  }
}

function selectedMode() {
  return $("input[name='mode']:checked").value;
}

function updateMode() {
  const anchor = selectedMode() === "anchor";
  $("#anchor-fields").classList.toggle("hidden", !anchor);
  $("#run-button span:first-child").textContent = anchor ? "Run fresh search & anchor" : "Run live discovery";
  if (!anchor) {
    reviewedRunId = null;
    $("#approved-url").value = "";
  }
}

function formError(message) {
  const element = $("#form-error");
  element.textContent = message || "";
  element.classList.toggle("hidden", !message);
}

function validateForm() {
  if (selectedMode() === "discovery" && !selectedFile) return "Choose one consented face image.";
  if (selectedFile && selectedFile.size > 25 * 1024 * 1024) return "The image exceeds 25 MB.";
  const consents = ["#consent-adult", "#consent-authorized", "#consent-search", "#consent-upload"];
  if (consents.some((selector) => !$(selector).checked)) return "Complete all four consent attestations before processing.";
  const platforms = $$("#platforms input:checked").map((input) => input.value);
  if (!platforms.length) return "Select at least one platform to evaluate.";
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
    <li><span class="timeline-icon">✓</span><span>${escapeHtml(stage.message)}</span><time>${escapeHtml(formatTime(stage.at).split(", ").pop())}</time></li>
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
  payload.set("search_mode", $("input[name='search-mode']:checked").value);
  payload.set("platforms", $$("#platforms input:checked").map((input) => input.value).join(","));
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
  if (jobState === "inconclusive" || summary?.status === "inconclusive") return ["inconclusive", "No verified social-post match", "The search was genuine, but no returned public post passed every local and capture gate. This is an honest inconclusive result—not an identity finding."];
  return ["failed", "Pipeline stopped safely", "A required gate failed before a verified result could be claimed. No fallback result or blockchain write was substituted."];
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
  $("#result-empty").classList.add("hidden");
  $("#job-progress").classList.add("hidden");
  const result = $("#result-content");
  result.classList.remove("hidden");
  if (!summary) {
    result.innerHTML = `<div class="outcome-hero fail"><div class="outcome-top"><div><span class="section-kicker">FAILED SAFELY</span><h3>Nothing was claimed</h3><p>${escapeHtml(jobError?.message || "No evidence directory was produced.")}</p></div><span class="outcome-badge failed">FAILED</span></div></div>`;
    return;
  }
  const [tone, title, description] = statusCopy(summary, jobState);
  result.innerHTML = `
    <div class="outcome-hero ${tone === "inconclusive" ? "warn" : tone === "failed" ? "fail" : ""}">
      <div class="outcome-top"><div><span class="section-kicker">RUN ${escapeHtml(summary.run_id)}</span><h3>${escapeHtml(title)}</h3><p>${escapeHtml(summary.error?.message || description)}</p></div><span class="outcome-badge ${escapeHtml(tone)}">${escapeHtml(tone)}</span></div>
    </div>
    ${renderFacts(summary)}${renderSelected(summary)}${renderProfiles(summary)}${renderCandidateTable(summary)}${renderIntegrity(summary)}
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
      showProofMessage(result.passed ? "PASS: every artifact, canonical byte, commitment, and configured chain record verified." : `FAIL: ${result.errors.join(" · ")}`, result.passed);
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
    $("#approved-url").value = summary.selected.url;
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
  return run.selected?.title || run.error?.message || "Evidence run";
}

async function loadHistory() {
  const grid = $("#history-grid");
  try {
    const data = await api("/api/history?limit=12");
    if (!data.runs.length) {
      grid.innerHTML = '<div class="history-loading">No local runs yet. Your first consented analysis will appear here.</div>';
      return;
    }
    grid.innerHTML = data.runs.map((run) => `
      <button class="history-card" type="button" data-run-id="${escapeHtml(run.run_id)}">
        <span class="history-card-top"><span class="section-kicker">${escapeHtml(run.search?.provider || "LOCAL")}</span><span class="outcome-badge ${escapeHtml(run.status)}">${escapeHtml(run.status)}</span></span>
        <h3>${escapeHtml(historyTitle(run))}</h3>
        <p>${escapeHtml(run.selected ? `${run.selected.platform || run.selected.source || "web"} · similarity ${number(run.selected.similarity, 6)}` : `${run.search?.candidate_count || 0} candidates · ${run.error?.stage || "partial evidence"}`)}</p>
        <span class="history-card-footer"><span>${escapeHtml(run.run_id)}</span><span>Inspect →</span></span>
      </button>`).join("");
    $$(".history-card", grid).forEach((card) => card.addEventListener("click", async () => {
      try {
        const run = await api(`/api/evidence/${encodeURIComponent(card.dataset.runId)}`);
        renderSummary(run);
        $("#result-panel").scrollIntoView({behavior: "smooth", block: "start"});
      } catch (error) { toast(error.message); }
    }));
  } catch (error) {
    grid.innerHTML = `<div class="history-loading">${escapeHtml(error.message)}</div>`;
  }
}

const dropZone = $("#drop-zone");
$("#image-input").addEventListener("change", (event) => setSelectedFile(event.target.files[0]));
$("#remove-file").addEventListener("click", (event) => { event.preventDefault(); event.stopPropagation(); setSelectedFile(null); });
["dragenter", "dragover"].forEach((name) => dropZone.addEventListener(name, (event) => { event.preventDefault(); dropZone.classList.add("dragging"); }));
["dragleave", "drop"].forEach((name) => dropZone.addEventListener(name, (event) => { event.preventDefault(); dropZone.classList.remove("dragging"); }));
dropZone.addEventListener("drop", (event) => { if (event.dataTransfer.files.length) setSelectedFile(event.dataTransfer.files[0]); });
$$('input[name="mode"]').forEach((input) => input.addEventListener("change", updateMode));
$("#max-candidates").addEventListener("input", (event) => { $("#candidate-count").textContent = event.target.value; });
$("#run-form").addEventListener("submit", submitRun);
$("#refresh-history").addEventListener("click", loadHistory);

updateMode();
loadReadiness();
loadHistory();
resumeActiveJob();
