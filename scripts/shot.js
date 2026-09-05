// Screenshot helper for UI review: node scripts/shot.js <url> <outPrefix>
const path = require("path");
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

// The cached Playwright build wants a headless shell revision that is not installed;
// fall back to the newest chromium_headless_shell present under ms-playwright.
function resolveExecutable() {
  const fs = require("fs");
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
const prefix = process.argv[3] || "shot";
const outDir = path.join(__dirname, "..", "output", "ui-review");

const VIEWPORTS = [
  { name: "desktop", width: 1440, height: 1000 },
  { name: "laptop", height: 900, width: 1180 },
  { name: "band-980", width: 980, height: 900 },
  { name: "band-740", width: 740, height: 900 },
  { name: "mobile", width: 390, height: 844 },
];

(async () => {
  const browser = await chromium.launch({ executablePath: resolveExecutable() });
  const errors = [];
  for (const vp of VIEWPORTS) {
    const page = await browser.newPage({
      viewport: { width: vp.width, height: vp.height },
      deviceScaleFactor: 1,
    });
    page.on("console", (m) => {
      if (m.type() === "error") errors.push(`[${vp.name}] console: ${m.text()}`);
    });
    page.on("pageerror", (e) => errors.push(`[${vp.name}] pageerror: ${e.message}`));
    await page.goto(url, { waitUntil: "networkidle" }).catch((e) => errors.push(`[${vp.name}] goto: ${e.message}`));
    await page.waitForTimeout(900);
    const out = path.join(outDir, `${prefix}-${vp.name}.png`);
    await page.screenshot({ path: out, fullPage: true });
    // Report horizontal overflow, which body{overflow-x:hidden} would otherwise mask.
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth
    );
    console.log(`${vp.name.padEnd(10)} ${String(vp.width).padStart(5)}px  overflowX=${overflow}px  -> ${path.basename(out)}`);
    await page.close();
  }
  await browser.close();
  if (errors.length) {
    console.log("\n--- page/console errors ---");
    for (const e of errors) console.log(e);
  } else {
    console.log("\nno page or console errors");
  }
})();
