// Screenshot the RESULT state by clicking the first archive card.
// node scripts/shot-result.js <url> <outPrefix>
const path = require("path");
const fs = require("fs");
const PW = path.join(
  __dirname,
  "..",
  ".tools",
  "npm-cache-playwright",
  "_npx",
  "31e32ef8478fbf80",
  "node_modules",
  "playwright"
);
const { chromium } = require(PW);

function resolveExecutable() {
  const root = path.join(process.env.LOCALAPPDATA || "", "ms-playwright");
  if (!fs.existsSync(root)) return undefined;
  const builds = fs
    .readdirSync(root)
    .filter((d) => d.startsWith("chromium_headless_shell-"))
    .map((d) => ({ dir: d, rev: Number(d.split("-")[1]) }))
    .sort((a, b) => b.rev - a.rev);
  for (const b of builds) {
    const exe = path.join(root, b.dir, "chrome-headless-shell-win64", "chrome-headless-shell.exe");
    if (fs.existsSync(exe)) return exe;
  }
  return undefined;
}

const url = process.argv[2] || "http://127.0.0.1:8791/";
const prefix = process.argv[3] || "result";
const outDir = path.join(__dirname, "..", "output", "ui-review");

const VIEWPORTS = [
  { name: "desktop", width: 1440, height: 1000 },
  { name: "band-740", width: 740, height: 900 },
  { name: "mobile", width: 390, height: 844 },
];

(async () => {
  const browser = await chromium.launch({ executablePath: resolveExecutable() });
  const errors = [];
  for (const vp of VIEWPORTS) {
    const page = await browser.newPage({ viewport: { width: vp.width, height: vp.height } });
    page.on("console", (m) => {
      if (m.type() === "error") errors.push(`[${vp.name}] console: ${m.text()}`);
    });
    page.on("pageerror", (e) => errors.push(`[${vp.name}] pageerror: ${e.message}`));
    await page.goto(url, { waitUntil: "networkidle" });
    // Pick the archive session with the most matches so the result view is densely populated.
    await page.waitForSelector(".history-card", { timeout: 15000 }).catch(() => {});
    const card = await page.evaluate(() => {
      const cards = [...document.querySelectorAll(".history-card")];
      let best = null;
      let bestN = -1;
      cards.forEach((c, i) => {
        const m = (c.textContent || "").match(/(\d+)\s+matching web link/);
        const n = m ? Number(m[1]) : 0;
        if (n > bestN) {
          bestN = n;
          best = i;
        }
      });
      return { index: best, count: bestN };
    });
    if (card.index != null) {
      await page.evaluate((i) => document.querySelectorAll(".history-card")[i].click(), card.index);
      await page.waitForSelector("#photo-copy-result:not(.hidden)", { timeout: 20000 }).catch(() => {});
      await page.waitForTimeout(1800);
    }
    const out = path.join(outDir, `${prefix}-${vp.name}.png`);
    await page.screenshot({ path: out, fullPage: true });
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth
    );
    // Report which result-state classes render with no author styles at all.
    const unstyled = await page.evaluate(() => {
      const probe = [
        "face-scan-banner", "face-scan-icon", "face-scan-info", "face-score-pill",
        "visual-score-pill", "match-badge", "match-header", "match-meta", "header-tags",
        "mini-progress-track", "mini-progress-fill", "photo-proof-header", "pill-dot",
        "score-pill-header", "score-pill-label", "score-pill-val", "section-lead",
        "status-tag", "table-scroll", "evm-blockchain-badge", "provenance-node",
      ];
      const out = [];
      for (const cls of probe) {
        const el = document.querySelector("." + cls);
        if (!el) { out.push(`${cls}: ABSENT`); continue; }
        let matched = 0;
        for (const sheet of document.styleSheets) {
          let rules;
          try { rules = sheet.cssRules; } catch { continue; }
          for (const r of rules) {
            if (r.selectorText && r.selectorText.split(",").some((s) => s.trim().includes("." + cls))) matched++;
          }
        }
        out.push(`${cls}: ${matched} rule(s)`);
      }
      return out;
    });
    console.log(`\n=== ${vp.name} ${vp.width}px (session with ${card.count} matches) overflowX=${overflow}px -> ${path.basename(out)}`);
    if (vp.name === "desktop") unstyled.forEach((u) => console.log("   " + u));
    await page.close();
  }
  await browser.close();
  console.log(errors.length ? "\n--- errors ---\n" + errors.join("\n") : "\nno page or console errors");
})();
