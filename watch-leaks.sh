#!/usr/bin/env bash
#
# watch-leaks.sh — poll the CDN for newly-published secrets / VCS dirs / backups
# and alert only on content never seen before. Pure regex (no LLM cost).
#
# Usage:
#   ./watch-leaks.sh                      # loop forever, scan every 5 min
#   INTERVAL=600 ./watch-leaks.sh         # every 10 min
#   ONCE=1 ./watch-leaks.sh               # single pass (for cron/launchd)
#   PRODUCTS_ARG='--product wow' ./watch-leaks.sh
#
# Run it detached:  nohup ./watch-leaks.sh >/dev/null 2>&1 &
# Or schedule ONCE=1 via launchd/cron (see watch-leaks.plist).
#
set -euo pipefail
cd "$(dirname "$0")"

INTERVAL="${INTERVAL:-300}"
PRODUCTS_ARG="${PRODUCTS_ARG:---all-products}"
CONCURRENCY="${CONCURRENCY:-8}"
STATE="${STATE:-.seen-secrets.json}"
LOG="${LOG:-watch-leaks.log}"
BT="python -m blizztools.main"

# High-value, UNAMBIGUOUS leak signals — no LLM needed to judge these.
SECRETS='(?:^|/)\.env$|\.(?:pem|key|p12|pfx|jks|keystore)$|(secret|token|password|credential|api[_-]?key|private[_-]?key)|(?:^|/)\.(?:git|svn|hg)(?:/|$)|\.git(?:ignore|attributes|modules)$|\.(?:bak|old|orig|swp)$'

notify() {
  local title="$1" body="$2"
  echo "$(date '+%Y-%m-%d %H:%M:%S')  $title — $body" | tee -a "$LOG"
  printf '\a'  # terminal bell
  if command -v osascript >/dev/null 2>&1; then
    osascript -e "display notification \"${body//\"/}\" with title \"${title//\"/}\"" 2>/dev/null || true
  fi
}

one_pass() {
  local tmp; tmp="$(mktemp)"
  # shellcheck disable=SC2086
  if ! $BT scan $PRODUCTS_ARG -p "$SECRETS" --concurrency "$CONCURRENCY" --json >"$tmp" 2>/dev/null; then
    echo "$(date '+%H:%M:%S')  scan failed, will retry next cycle" | tee -a "$LOG"
    rm -f "$tmp"; return 0
  fi
  # Diff against seen CKeys; alert on genuinely new content; update state.
  STATE="$STATE" python3 - "$tmp" <<'PY'
import json, os, sys, re
state_path = os.environ["STATE"]
hits = json.load(open(sys.argv[1]))
# Filter out obvious third-party middleware so a vendored .env etc. doesn't spam.
MW = re.compile(r"monobleedingedge|aksoundengine|wwise|libcef|cef\.depends|netease|mpay|unisdk|orbitsdk|xyvodsdk|chromium|swiftshader", re.I)
hits = [h for h in hits if not MW.search(h["name"])]
seen = set()
if os.path.exists(state_path):
    try: seen = set(json.load(open(state_path)))
    except Exception: seen = set()
new = [h for h in hits if h["ckey"] not in seen]
for h in new:
    print(f"NEW\t{h['product']}\t{h['version']}\t{h['size']}\t{h['name']}")
# persist union
json.dump(sorted(seen | {h["ckey"] for h in hits}), open(state_path, "w"))
PY
  rm -f "$tmp"
}

run_and_alert() {
  local out; out="$(one_pass)"
  if [[ -n "$out" ]]; then
    local n; n=$(echo "$out" | grep -c '^NEW' || true)
    notify "🔑 $n new CDN leak(s)" "$(echo "$out" | sed 's/^NEW\t//' | awk -F'\t' '{print $1": "$4}' | head -5 | paste -sd'; ' -)"
    echo "$out" | sed 's/^NEW\t//' | awk -F'\t' '{printf "    %-16s %-16s %12s  %s\n",$1,$2,$3,$4}' | tee -a "$LOG"
  else
    echo "$(date '+%H:%M:%S')  no new leaks" >> "$LOG"
  fi
}

if [[ "${ONCE:-0}" == "1" ]]; then
  run_and_alert
  exit 0
fi

echo "watch-leaks: polling $PRODUCTS_ARG every ${INTERVAL}s. State: $STATE, log: $LOG. Ctrl-C to stop."
while true; do
  run_and_alert
  sleep "$INTERVAL"
done
