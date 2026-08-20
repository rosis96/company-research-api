#!/usr/bin/env bash
# One-command smoke test against the deployed API.
#
#   ./scripts/first_test.sh                        # tests python.org
#   ./scripts/first_test.sh acme.com othercorp.com # tests those instead
#
# The API key is found in this order, so you are never asked to paste into a
# silent prompt (which macOS Terminal does not allow):
#   1. $CRA_API_KEY environment variable
#   2. ~/.cra_key file          <- recommended: set once, works forever
#   3. your clipboard, with confirmation
#
# Output is printed AND saved to last_test_output.txt so it is easy to send on.

set -uo pipefail

BASE="${CRA_BASE_URL:-https://company-research-api-production.up.railway.app}"
OUT="last_test_output.txt"

strip() { tr -d '[:space:]'; }

# Accept anything you might plausibly copy out of Railway and reduce it to the
# bare key.  A token_urlsafe key only ever contains A-Za-z0-9_- so stripping
# ":scope", a second entry after a comma, and a leading "CRA_API_KEYS=" is safe.
normalise_key() {
  local raw="$1"
  raw="${raw#CRA_API_KEYS=}"
  raw="${raw#*=}"
  raw="${raw%%,*}"
  raw="${raw%%:*}"
  printf '%s' "$raw"
}

# Describe a key without revealing it.
describe_key() {
  local k="$1"
  printf '%d characters' "${#k}"
  case "$k" in
    *:*) printf ', contains ":"' ;;
  esac
  case "$k" in
    *,*) printf ', contains ","' ;;
  esac
  case "$k" in
    *"{"*|*'"'*) printf ', looks like JSON' ;;
  esac
}

KEY=""
SOURCE=""

if [ -n "${CRA_API_KEY:-}" ]; then
  KEY=$(normalise_key "$(printf '%s' "$CRA_API_KEY" | strip)"); SOURCE="CRA_API_KEY environment variable"
elif [ -f "$HOME/.cra_key" ]; then
  KEY=$(normalise_key "$(cat "$HOME/.cra_key" | strip)"); SOURCE="~/.cra_key"
elif command -v pbpaste >/dev/null 2>&1; then
  CLIP=$(pbpaste | strip)
  if [ ${#CLIP} -ge 20 ] && [ ${#CLIP} -le 200 ] && [[ "$CLIP" != *" "* ]]; then
    echo "Found something on your clipboard that looks like an API key:"
    echo "    ${CLIP:0:6}...${CLIP: -4}   (${#CLIP} characters)"
    printf 'Use it? [y/N] '
    read -r ANSWER
    if [ "$ANSWER" = "y" ] || [ "$ANSWER" = "Y" ]; then
      KEY=$(normalise_key "$CLIP"); SOURCE="clipboard"
    fi
  fi
fi

if [ -z "$KEY" ]; then
  cat <<'MSG'

No API key found.

Do this once and you will never be asked again:

  1. Copy your STANDARD API key (Railway -> Variables -> CRA_API_KEYS,
     it is the part before ":standard")
  2. Run:

       pbpaste > ~/.cra_key && chmod 600 ~/.cra_key

  3. Run this script again.

MSG
  exit 1
fi

if ! printf '%s' "$KEY" | grep -Eq '^[A-Za-z0-9_-]{20,120}$'; then
  echo ""
  echo "The key loaded from $SOURCE does not look like an API key."
  echo "  detected: $(describe_key "$KEY")"
  echo "  expected: 30-50 characters, letters/digits/_/- only"
  echo ""
  echo "Most likely your clipboard held something else when you saved it."
  echo "Copy ONLY the key value from Railway -> Variables -> CRA_API_KEYS"
  echo "(the part before \":standard\"), then run:"
  echo ""
  echo "    pbpaste > ~/.cra_key && chmod 600 ~/.cra_key"
  echo ""
  exit 1
fi

{
echo "API : $BASE"
echo "Key : loaded from $SOURCE ($(describe_key "$KEY"))"
echo "Time: $(date '+%Y-%m-%d %H:%M:%S')"
} | tee "$OUT"

DOMAINS=("$@")
if [ ${#DOMAINS[@]} -eq 0 ]; then DOMAINS=("https://www.python.org"); fi

{
echo "Testing ${#DOMAINS[@]} site(s). Each takes 3-35 seconds."
echo "=================================================================="
} | tee -a "$OUT"

for d in "${DOMAINS[@]}"; do
  { echo ""; echo ">>> $d"; } | tee -a "$OUT"
  start=$(date +%s)
  code=$(curl -s -o /tmp/cra_result.json -w "%{http_code}" --max-time 90 \
    -X POST "$BASE/v1/research" \
    -H "Authorization: Bearer $KEY" \
    -H "Content-Type: application/json" \
    -d "{\"url\":\"$d\"}")
  end=$(date +%s)

  if [ "$code" = "401" ]; then
    {
      echo "    HTTP 401 - the server rejected this key."
      echo ""
      echo "    The key was $(describe_key "$KEY"), loaded from $SOURCE."
      echo "    It is well-formed, so it simply is not one of the keys Railway knows."
      echo ""
      echo "    Check Railway -> Variables -> CRA_API_KEYS and confirm this key"
      echo "    appears there. If you rotated keys, make sure the newest deploy"
      echo "    finished (Deployments tab shows green) before retrying."
    } | tee -a "$OUT"
    break
  fi

  if [ "$code" != "200" ]; then
    { echo "    HTTP $code  <-- not 200"; cat /tmp/cra_result.json; echo; } | tee -a "$OUT"
    continue
  fi

  python3 - <<'PY' | tee -a "$OUT"
import json
d = json.load(open('/tmp/cra_result.json'))
s, site, cov = d.get('crawl_summary', {}), d.get('site', {}), d.get('coverage', {})
print(f"    status   : {d.get('status')}  evidence_sufficient={d.get('evidence_sufficient')}"
      + (f"  reason={d.get('status_reason')}" if d.get('status_reason') else ""))
print(f"    site     : {site.get('site_name')}  [{site.get('canonical_domain')}]")
if site.get('tagline'):
    print(f"    tagline  : {site['tagline'][:90]}")
print(f"    crawl    : pages={s.get('pages_scraped')} failed={s.get('pages_failed')} "
      f"renders={s.get('browser_renders')} depth={s.get('max_depth_reached')}")
print(f"    volume   : words={s.get('total_words')} chars={s.get('content_chars')} "
      f"time={s.get('elapsed_ms')}ms stopped={s.get('stopped_reason')}")
print(f"    found    : {', '.join(cov.get('found', [])) or '-'}")
print(f"    missing  : {', '.join(cov.get('missing', [])) or '-'}")
for p in d.get('pages', []):
    print(f"      {p['page_type']:<20} {p['word_count']:>5}w  {p['url'][:78]}")
for f in d.get('failed_pages', [])[:6]:
    print(f"      FAILED {f['reason_code']:<18} {f['url'][:74]}")
PY
  echo "    wall clock: $((end-start))s" | tee -a "$OUT"
done

{
echo ""
echo "=================================================================="
echo "Saved to $OUT"
} | tee -a "$OUT"
rm -f /tmp/cra_result.json
