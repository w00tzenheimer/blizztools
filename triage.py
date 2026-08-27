#!/usr/bin/env python3
"""
triage.py — classify scan findings as LEAK vs BENIGN, asking Claude only about
genuinely new, ambiguous paths.

Pipeline:
  1. Cheap regex pass auto-labels the obvious (middleware -> benign; .git,
     secrets, source, backups, debug builds -> leak). No LLM cost.
  2. Whatever is left ambiguous (PDBs, odd names) is normalized to a signature
     (digits collapsed, so the same file across versions/products is one key)
     and looked up in a persistent cache (.leak-cache.json).
  3. Only signatures never seen before are sent to `claude -p --model sonnet`,
     in one batch. Verdicts are cached forever.
  4. Reports LEAKs, flagging which are NEW this run.

Usage:  python triage.py hits.json            # from `blizztools scan --json`
        python triage.py hits.json --recheck  # ignore cache, re-ask everything
"""
import argparse
import json
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

CACHE_PATH = Path(".leak-cache.json")
CLAUDE_MODEL = "sonnet"
BATCH = 60

MIDDLEWARE = re.compile(
    r"monobleedingedge|[\\/]mono[\\/]|aksoundengine|wwise|libcef|cef[\\/]|cef\.depends"
    r"|cef\d|chromium|ngwebview|webview|netease|mpay|unisdk|orbitsdk|xyvodsdk|ntunisdk"
    r"|swiftshader|d3dcompiler|vcruntime|ucrtbase|icudt|vulkan|dxil", re.I)
VCS = re.compile(r"(?:^|[\\/])\.(?:git|svn|hg)(?:[\\/]|$)|\.git(?:ignore|attributes|modules)$", re.I)


def base(n):
    return n.replace("\\", "/").rsplit("/", 1)[-1]


def ext(n):
    b = base(n)
    return b.rsplit(".", 1)[-1].lower() if "." in b else ""


def signature(name):
    """Version/product-independent key: lowercased path with digit runs collapsed."""
    return re.sub(r"\d+", "#", name.replace("\\", "/").lower())


def quick_verdict(h):
    """Return ('leak'|'benign', category, reason) for the obvious, else None."""
    n, b, e = h["name"], base(h["name"]).lower(), ext(h["name"])
    if VCS.search(n):
        return ("leak", "vcs", "version-control directory published")
    if MIDDLEWARE.search(n):
        return ("benign", "middleware", "third-party middleware")
    if e in ("env", "pem", "key", "p12", "pfx", "jks", "keystore") or \
       any(k in b for k in ("secret", "token", "password", "credential", "apikey", "privatekey")):
        return ("leak", "secret", "credential/secret material")
    if e in ("bak", "old", "orig", "tmp", "temp", "swp", "swo") or b.endswith("~"):
        return ("leak", "backup", "backup/editor artifact")
    if e in ("cpp", "cc", "cxx", "c", "h", "hpp", "inl", "cs", "lua", "py", "pl", "rb", "sh", "ps1", "bat"):
        return ("leak", "source", "source code")
    if re.search(r"[_-]d\.(?:dll|exe)$", b) or re.search(r"(?:debug|internal|staging)\.(?:dll|exe|bundle)$", b):
        return ("leak", "debugbuild", "debug/internal build")
    return None  # ambiguous -> ask Claude


def ask_claude(paths):
    """Classify a batch of paths via `claude -p`. Returns {path: (verdict, reason)}."""
    listing = "\n".join(f"{i+1}. {p}" for i, p in enumerate(paths))
    prompt = (
        "You are triaging files published to a large game company's content-delivery "
        "CDN, hunting for ACCIDENTAL leaks. Classify each path.\n"
        "LEAK = the company's own source code, secrets/keys, backups, debug/internal "
        "builds, version-control dirs, or the company's OWN debug symbols carrying "
        "internal build paths.\n"
        "BENIGN = third-party middleware/SDKs, normal game assets, standard runtime "
        "files, import stubs, or config meant to ship.\n"
        "Output ONLY a JSON array of {\"path\":..., \"verdict\":\"LEAK\"|\"BENIGN\", "
        "\"reason\":\"<short>\"}, no prose.\n\nPaths:\n" + listing
    )
    try:
        out = subprocess.run(
            ["claude", "-p", prompt, "--model", CLAUDE_MODEL],
            capture_output=True, text=True, timeout=300,
        ).stdout
    except Exception as e:
        print(f"  ! claude call failed: {e}", file=sys.stderr)
        return {}
    m = re.search(r"\[.*\]", out, re.S)
    if not m:
        return {}
    try:
        rows = json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}
    result = {}
    for r in rows:
        p = r.get("path", "")
        v = "leak" if str(r.get("verdict", "")).upper().startswith("LEAK") else "benign"
        result[p] = (v, r.get("reason", ""))
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("hits", help="JSON from `blizztools scan --json`")
    ap.add_argument("--recheck", action="store_true", help="ignore cache")
    args = ap.parse_args()

    hits = json.loads(Path(args.hits).read_text())
    cache = {} if args.recheck else (
        json.loads(CACHE_PATH.read_text()) if CACHE_PATH.exists() else {})

    verdicts = {}          # id(h) -> (verdict, category, reason, is_new)
    ambiguous = {}         # signature -> representative path
    for h in hits:
        q = quick_verdict(h)
        if q:
            verdicts[id(h)] = (*q, False)
        else:
            sig = signature(h["name"])
            if sig in cache:
                c = cache[sig]
                verdicts[id(h)] = (c["verdict"], c.get("category", "symbols"), c["reason"], False)
            else:
                ambiguous.setdefault(sig, h["name"])

    if ambiguous:
        sigs = list(ambiguous)
        print(f"  asking Claude about {len(sigs)} new ambiguous path(s)...", file=sys.stderr)
        for i in range(0, len(sigs), BATCH):
            chunk = sigs[i:i + BATCH]
            answers = ask_claude([ambiguous[s] for s in chunk])
            for s in chunk:
                path = ambiguous[s]
                v, reason = answers.get(path, ("benign", "unclassified"))
                cache[s] = {"verdict": v, "category": "symbols", "reason": reason}
        CACHE_PATH.write_text(json.dumps(cache, indent=1, sort_keys=True))
        # backfill this run's verdicts + mark them new
        for h in hits:
            if id(h) not in verdicts:
                c = cache[signature(h["name"])]
                verdicts[id(h)] = (c["verdict"], "symbols", c["reason"], True)

    leaks = [(h, verdicts[id(h)]) for h in hits if verdicts[id(h)][0] == "leak"]
    leaks.sort(key=lambda t: (not t[1][3], -t[0]["size"]))  # new first, then by size

    if not leaks:
        print("\n✅ No leaks. All findings are benign (middleware / normal assets).")
        return
    print(f"\n🔎 {len(leaks)} leak(s):")
    for h, (v, cat, reason, is_new) in leaks:
        tag = "🆕 NEW " if is_new else "      "
        print(f"  {tag}[{cat:10}] {h['size']:>12,}  {h['product']:14} {h['name']}")
        if is_new:
            print(f"              ↳ {reason}")


if __name__ == "__main__":
    main()
