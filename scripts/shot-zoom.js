// Crop a specific element in the RESULT state for close inspection.
// node scripts/shot-zoom.js <url> <selector> <outName> [width]
const path = require("path");
const fs = require("fs");
const PW = path.join(__dirname, "..", ".tools", "npm-cache-playwright", "_npx", "31e32ef8478fbf80", "node_modules", "playwright");
const { chromium } = require(PW);

function resolveExecutable() {
  const root = path.join(process.env.LOCALAPPDATA || "", "ms-playwright");
  if (!fs.existsSync(root)) return undefined;
  const builds = fs.readdirSync(root)
    .filter((d) => d.startsWith("chromium_headless_shell-"))
    .map((d) => ({ dir: d, rev: Number(d.split("-")[1]) }))
    .sort((a, b) => b.rev - a.rev);
  for (const b of builds) {
    const exe = path.join(root, b.dir, "chrome-headless-shell-win64", "chrome-headless-shell.exe");
    if (fs.existsSync(exe)) return exe;
  }
  return undefined;
}

const url = process.argv[2];
const selector = process.argv[3];
const outName = process.argv[4];
const width = Number(process.argv[5] || 1440);
const outDir = path.join(__dirname, "..", "output", "ui-review");

(async () => {
  const browser = await chromium.launch({ executablePath: resolveExecutable() });
  const page = await browser.newPage({ viewport: { width, height: 1000 }, deviceScaleFactor: 2 });
  await page.goto(url, { waitUntil: "networkidle" });
  await page.waitForSelector(".history-card", { timeout: 15000 }).catch(() => {});
  const idx = await page.evaluate(() => {
    const cards = [...document.querySelectorAll(".history-card")];
    let best = 0, bestN = -1;
    cards.forEach((c, i) => {
      const m = (c.textContent || "").match(/(\d+)\s+matching web link/);
      const n = m ? Number(m[1]) : 0;
      if (n > bestN) { bestN = n; best = i; }
    });
    return best;
  });
  await page.evaluate((i) => document.querySelectorAll(".history-card")[i].click(), idx);
  await page.waitForSelector("#photo-copy-result:not(.hidden)", { timeout: 20000 }).catch(() => {});
  await page.waitForTimeout(1500);
  const el = await page.$(selector);
  if (!el) {
    console.log(`selector not found: ${selector}`);
  } else {
    const out = path.join(outDir, `${outName}.png`);
    await el.screenshot({ path: out });
    const box = await el.boundingBox();
    console.log(`${selector} -> ${outName}.png  (${Math.round(box.width)}x${Math.round(box.height)})`);
  }
  await browser.close();
})();
