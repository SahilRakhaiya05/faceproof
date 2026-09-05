#!/usr/bin/env bash
# Fetch the Goa Hacker House brand webfonts (latin subset) for self-hosting.
# Emits flat woff2 files into src/faceproof/web_assets/ plus a _fonts.css fragment.
set -u

DEST="E:/hhgoa t3 blockchain/src/faceproof/web_assets/fonts"
UA="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
mkdir -p "$DEST"
OUT="$DEST/_fonts.css"
: > "$OUT"

# family-query|local-basename
SPECS="
Space+Grotesk:wght@400;500;600;700|spacegrotesk
Bebas+Neue|bebasneue
JetBrains+Mono:wght@400;500;700|jetbrainsmono
Imbue:opsz,wght@10..100,400;10..100,600|imbue
"

echo "$SPECS" | while IFS='|' read -r query base; do
  [ -z "${query:-}" ] && continue
  css=$(curl -sS -m 30 -A "$UA" "https://fonts.googleapis.com/css2?family=${query}&display=swap")
  if [ -z "$css" ]; then echo "FAIL fetch $base"; continue; fi
  # Keep only the latin subset blocks: the comment precedes each @font-face.
  echo "$css" | awk -v base="$base" -v dest="$DEST" '
    /^\/\* / { subset=$0; gsub(/[^a-z0-9-]/,"",subset); next }
    /@font-face/ { inface=1; buf=$0"\n"; next }
    inface { buf=buf $0"\n" }
    inface && /}/ {
      inface=0
      if (subset=="latin") {
        n++
        printf "%s\n===SPLIT===\n", buf
      }
      buf=""
    }
  ' > /tmp/faces-$base.txt
  i=0
  # Split the accumulated faces and download each latin woff2.
  awk 'BEGIN{RS="===SPLIT===\n"} NF{print $0"\n---END---"}' /tmp/faces-$base.txt |
  while IFS= read -r line; do
    printf '%s\n' "$line"
  done > /tmp/faces-flat-$base.txt

  python - "$base" "$DEST" <<'PY'
import re, sys, urllib.request, pathlib
base, dest = sys.argv[1], pathlib.Path(sys.argv[2])
raw = pathlib.Path(f"/tmp/faces-{base}.txt").read_text(encoding="utf-8", errors="replace")
blocks = [b for b in raw.split("===SPLIT===") if "@font-face" in b]
ua = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0 Safari/537.36"}
out = []
for b in blocks:
    url = re.search(r"url\((https://fonts\.gstatic\.com/[^)]+\.woff2)\)", b)
    wght = re.search(r"font-weight:\s*([^;]+);", b)
    style = re.search(r"font-style:\s*([^;]+);", b)
    fam = re.search(r"font-family:\s*'([^']+)'", b)
    stretch = re.search(r"font-stretch:\s*([^;]+);", b)
    if not (url and fam):
        continue
    w = (wght.group(1).strip() if wght else "400").replace(" ", "")
    tag = w.replace("..", "-")
    fname = f"{base}-{tag}.woff2"
    target = dest / fname
    if not target.exists():
        try:
            req = urllib.request.Request(url.group(1), headers=ua)
            target.write_bytes(urllib.request.urlopen(req, timeout=45).read())
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL {fname}: {exc}")
            continue
    decl = [
        "@font-face {",
        f"  font-family: '{fam.group(1)}';",
        f"  font-style: {style.group(1).strip() if style else 'normal'};",
        f"  font-weight: {w};",
    ]
    if stretch:
        decl.append(f"  font-stretch: {stretch.group(1).strip()};")
    decl += [
        "  font-display: swap;",
        f"  src: url('/assets/{fname}') format('woff2');",
        "}",
    ]
    out.append("\n".join(decl))
    print(f"OK {fname} ({target.stat().st_size} bytes)")
(dest / "_fonts.css").write_text("\n".join(out) + "\n", encoding="utf-8")
PY
done

echo "--- files ---"
ls -la "$DEST" 2>/dev/null
echo "--- total ---"
du -sh "$DEST" 2>/dev/null
