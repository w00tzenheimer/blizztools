#!/usr/bin/env bash
#
# find-leaks.sh — survey Blizzard's CDN for accidentally-published artifacts.
#
# Reports only by default (no downloads). Categorizes findings, filters out
# third-party middleware, and keeps genuine LEAKS separate from RE-interesting
# files (loaders, client exes) so the two never get muddled.
#
# Usage:
#   ./find-leaks.sh                 # scan all products, categorized report
#   RE=1 ./find-leaks.sh            # also surface RE targets (loaders, client exes)
#   GRAB=1 ./find-leaks.sh          # download HIGH-value leaks (incl .git) to ./leaks
#   GRAB=symbols ./find-leaks.sh    # HIGH leaks + PDBs/dSYMs
#   RE=1 GRAB=re ./find-leaks.sh    # download the RE targets instead
#   GRAB=all RE=1 ./find-leaks.sh   # everything
#   DEST=./leaks ./find-leaks.sh    # download dir (default ./leaks, LOCAL not SMB)
#   PRODUCTS_ARG='--product wow' ./find-leaks.sh   # scope the scan
#   CONCURRENCY=16 ./find-leaks.sh
#
set -euo pipefail
cd "$(dirname "$0")"

DEST="${DEST:-./leaks}"
CONCURRENCY="${CONCURRENCY:-8}"
HITS="${HITS:-hits.json}"
PRODUCTS_ARG="${PRODUCTS_ARG:---all-products}"
GRAB="${GRAB:-0}"
RE="${RE:-0}"
BT="python -m blizztools.main"

# The leak net. Note .git/.svn/.hg paths and .gitignore are in here.
BIG='\.(?:pdb|dsym|sym|dbg|debug|pch|natvis|old|orig|bak|tmp|temp|swp|swo|zip|7z|rar|tar|gz|tgz|cab|pem|key|p12|pfx|jks|keystore|env|cpp|cc|cxx|c|h|hpp|inl|cs|lua|py|pl|rb|sh|ps1|bat)$|[_-]d\.(?:dll|exe)$|(?:debug|internal|staging)\.(?:dll|exe|bundle)$|~$|(?:^|/)\.env$|\.git(?:ignore|attributes|modules)$|(?:^|/)\.(?:git|svn|hg)(?:/|$)|secret|token|password|credential|api[_-]?key|private[_-]?key'

# RE targets: loaders and client executables you care about for reversing.
RE_NET='_loader\.dll$|^(?!WowVoiceProxy(?:-China|T|\.exe$))Wow.*\.exe$|World.of.Warcraft$'

SCAN_NET="$BIG"
[[ "$RE" != "0" ]] && SCAN_NET="$BIG|$RE_NET"

echo "==> [1/2] Surveying products (read-only, no downloads)..."
# shellcheck disable=SC2086
$BT scan $PRODUCTS_ARG -p "$SCAN_NET" --concurrency "$CONCURRENCY" --json --sort size > "$HITS"

# LLM triage: ask Claude only about new, ambiguous paths (cached forever).
if [[ "${TRIAGE:-0}" != "0" ]]; then
  echo "==> [2/2] LLM triage (regex for the obvious, Claude for new ambiguous):"
  python3 triage.py "$HITS" ${RECHECK:+--recheck}
  echo ""
  echo "==> Triage report only. Cache in .leak-cache.json. Survey in $HITS."
  exit 0
fi

echo "==> [2/2] Findings (middleware filtered out):"
REPORT=$(RE_ON="$RE" python3 - "$HITS" <<'PY'
import json, os, re, sys
from collections import defaultdict

hits = json.load(open(sys.argv[1]))
RE_ON = os.environ.get("RE_ON", "0") != "0"

MIDDLEWARE = re.compile(
    r"monobleedingedge|[\\/]mono[\\/]|aksoundengine|wwise|libcef|cef[\\/]|cef\.depends"
    r"|cef\d|chromium|ngwebview|webview|netease|mpay|unisdk|orbitsdk|xyvodsdk|ntunisdk"
    r"|swiftshader|d3dcompiler|vcruntime|ucrtbase|icudt|vulkan|dxil", re.I)
VCS = re.compile(r"(?:^|[\\/])\.(?:git|svn|hg)(?:[\\/]|$)|\.git(?:ignore|attributes|modules)$", re.I)
RE_TGT = re.compile(r"_loader\.dll$|World of Warcraft$", re.I)

def base(n): return n.replace("\\", "/").rsplit("/", 1)[-1]
def ext(n):
    b = base(n); return b.rsplit(".", 1)[-1].lower() if "." in b else ""

def category(h):
    n, b, e = h["name"], base(h["name"]).lower(), ext(h["name"])
    if VCS.search(n):                       # a published .git/.svn is a big deal
        return "vcs"
    if MIDDLEWARE.search(n):
        return "middleware"
    if e in ("env", "pem", "key", "p12", "pfx", "jks", "keystore") or \
       any(k in b for k in ("secret", "token", "password", "credential", "apikey", "api_key", "api-key", "privatekey", "private_key")):
        return "secret"
    if e in ("bak", "old", "orig", "tmp", "temp", "swp", "swo") or b.endswith("~"):
        return "backup"
    if e in ("cpp", "cc", "cxx", "c", "h", "hpp", "inl", "cs", "lua", "py", "pl", "rb", "sh", "ps1", "bat"):
        return "source"
    if re.search(r"[_-]d\.(?:dll|exe)$", b) or re.search(r"(?:debug|internal|staging)\.(?:dll|exe|bundle)$", b):
        return "debugbuild"
    if e in ("pdb", "dsym"):
        return "symbols"
    if RE_TGT.search(n) or (e == "exe" and b.startswith("wow")):
        return "re"
    return "noise"

bk = defaultdict(list)
for h in hits:
    bk[category(h)].append(h)

def show(title, cats, color="1"):
    rows = sorted((h for c in cats for h in bk[c]), key=lambda x: -x["size"])
    if not rows:
        return
    print(f"\n  \033[{color}m{title}\033[0m ({len(rows)}):")
    for h in rows:
        print(f"    {h['size']:>13,}  {h['product']:16} {h['name']}")

HIGH = ("vcs", "secret", "backup", "source", "debugbuild")
show("🚨 VCS LEAK — published .git/.svn (reconstruct the repo!)", ("vcs",), "1;31")
show("HIGH-VALUE — source / backups / secrets / debug builds", ("secret", "backup", "source", "debugbuild"))
show("SYMBOLS — PDBs/dSYMs (open it; check the build path; often 3rd-party)", ("symbols",))
if RE_ON:
    show("RE TARGETS — loaders / client exes (not leaks; for reversing)", ("re",), "36")

nmid, nnoise = len(bk["middleware"]), len(bk["noise"])
print(f"\n  filtered out: {nmid} third-party middleware, {nnoise} low-value")

print("HIGH_PRODUCTS: " + " ".join(sorted({h["product"] for c in HIGH for h in bk[c]})))
print("SYM_PRODUCTS: " + " ".join(sorted({h["product"] for h in bk["symbols"]})))
print("RE_PRODUCTS: " + " ".join(sorted({h["product"] for h in bk["re"]})))
PY
)
echo "$REPORT" | grep -vE '^(HIGH|SYM|RE)_PRODUCTS:' || true
HIGH_PRODUCTS=$(echo "$REPORT" | sed -n 's/^HIGH_PRODUCTS: //p')
SYM_PRODUCTS=$(echo "$REPORT" | sed -n 's/^SYM_PRODUCTS: //p')
RE_PRODUCTS=$(echo "$REPORT" | sed -n 's/^RE_PRODUCTS: //p')

echo ""
if [[ "$GRAB" == "0" ]]; then
  echo "==> Report only (no downloads). Survey saved to $HITS."
  echo "    GRAB=1 ./find-leaks.sh          # download HIGH-value leaks (incl .git)"
  echo "    GRAB=symbols ./find-leaks.sh    # + PDBs/dSYMs"
  [[ "$RE" != "0" ]] && echo "    GRAB=re ./find-leaks.sh         # download the RE targets"
  exit 0
fi

declare -a PATS=()
TARGETS=""
case "$GRAB" in
  1|high|all)
    TARGETS="$TARGETS $HIGH_PRODUCTS"
    PATS+=(-p '(?:^|/)\.(?:git|svn|hg)(?:/|$)' -p '\.git(?:ignore|attributes|modules)$' \
           -p '\.(?:bak|old|orig|tmp|swp|swo)$' \
           -p '\.(?:cpp|cc|cxx|c|h|hpp|inl|cs|lua|py|pl|rb|sh|ps1|bat)$' \
           -p '(?:^|/)\.env$' -p '(?i)(secret|token|password|credential|private[_-]?key)' \
           -p '[_-]d\.(?:dll|exe)$') ;;&
  symbols|all)
    TARGETS="$TARGETS $HIGH_PRODUCTS $SYM_PRODUCTS"
    PATS+=(-p '\.pdb$' -p '\.dSYM') ;;&
  re|all)
    TARGETS="$TARGETS $RE_PRODUCTS"
    PATS+=(-p '_loader\.dll$' -p '^(?!WowVoiceProxy(?:-China|T|\.exe$))Wow.*\.exe$' -p 'World.of.Warcraft$') ;;
esac
TARGETS=$(echo "$TARGETS" | tr ' ' '\n' | sort -u | grep -v '^$' || true)

if [[ -z "${TARGETS// }" || ${#PATS[@]} -eq 0 ]]; then
  echo "==> Nothing matched GRAB='$GRAB'. Survey saved to $HITS."
  exit 0
fi

echo "==> Downloading to $DEST from: $(echo $TARGETS | tr '\n' ' ')"
PLIST="$(mktemp)"; trap 'rm -f "$PLIST"' EXIT
printf '%s\n' $TARGETS > "$PLIST"
$BT grab -f "$PLIST" "${PATS[@]}" --concurrency "$CONCURRENCY" -d "$DEST"
echo "==> Done. Files under $DEST, full survey in $HITS."
