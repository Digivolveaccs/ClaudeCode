#!/usr/bin/env bash
# Run everything that needs network access to Inform Direct, write the results
# into the repo, and push them - so the rest of the work can carry on without
# anyone at this keyboard.
#
#   export INFORMDIRECT_API_KEY="<sandbox key>"
#   ./scripts/report.sh
#
# Read-only against the API: it authenticates, confirms the endpoint paths and
# samples the company list. Nothing is added or removed. Company names and
# numbers are withheld from the report - only field-coverage counts are kept -
# and the API key never appears in it.

set -uo pipefail
cd "$(dirname "$0")/.."

BASE_URL="${INFORMDIRECT_BASE_URL:-https://sandbox-api.informdirect.co.uk}"
REPORT=config/live-report.txt
BRANCH="$(git rev-parse --abbrev-ref HEAD)"

if [ -z "${INFORMDIRECT_API_KEY:-}" ] && [ ! -f config/settings.json ]; then
  echo "No credentials. export INFORMDIRECT_API_KEY=... first." >&2
  exit 1
fi

{
  echo "Inform Direct live report"
  echo "generated: $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  echo "base_url : $BASE_URL"
  echo
  echo "===== 1. endpoint paths from the live spec ====="
  INFORMDIRECT_BASE_URL="$BASE_URL" python3 scripts/fetch_spec.py \
    --spec "$BASE_URL" --write 2>&1
  echo
  echo "===== 2. spec operations (if the spec was readable) ====="
  INFORMDIRECT_BASE_URL="$BASE_URL" python3 scripts/fetch_spec.py \
    --spec "$BASE_URL" --list 2>&1 | head -60
  echo
  echo "===== 3. resolved settings (secrets masked) ====="
  INFORMDIRECT_BASE_URL="$BASE_URL" python3 -m informdirect config 2>&1
  echo
  echo "===== 4. live check (company details withheld) ====="
  INFORMDIRECT_BASE_URL="$BASE_URL" python3 -m informdirect check --redact 2>&1
  echo "check exit status: $?"
} > "$REPORT" 2>&1

echo "wrote $REPORT"
echo
tail -25 "$REPORT"

# Belt and braces: never commit the key, whatever happened above.
if [ -n "${INFORMDIRECT_API_KEY:-}" ] && grep -qF "$INFORMDIRECT_API_KEY" "$REPORT"; then
  echo
  echo "REFUSING TO PUSH: the API key appears in the report." >&2
  echo "Left at $REPORT for you to look at; nothing was committed." >&2
  exit 1
fi

git add "$REPORT" config/endpoints.json 2>/dev/null
if git diff --cached --quiet; then
  echo "nothing new to push"
else
  git commit -q -m "Live report from $BASE_URL" \
    -m "Endpoint paths and field coverage, captured from the sandbox API."
  git push -u origin "$BRANCH" && echo "pushed to $BRANCH - you can walk away now"
fi
