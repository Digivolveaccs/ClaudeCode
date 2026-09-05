#!/usr/bin/env bash
# One-shot setup, for a machine that can actually reach Inform Direct.
#
#   export INFORMDIRECT_BASE_URL="https://api.informdirect.co.uk"
#   export INFORMDIRECT_API_KEY="<your sandbox key>"
#   ./scripts/setup.sh
#
# Confirms the endpoint paths from the live spec, then reports whether the API
# returns the fields the planner needs. Read-only: it never adds or removes a
# company (that is `verify --confirm`, which this script tells you to run next).

set -uo pipefail
cd "$(dirname "$0")/.."

BASE_URL="${INFORMDIRECT_BASE_URL:-https://api.informdirect.co.uk}"

if [ -z "${INFORMDIRECT_API_KEY:-}" ] && [ ! -f config/settings.json ]; then
  cat >&2 <<'MSG'
No credentials found.

Either export the key:
    export INFORMDIRECT_API_KEY="<sandbox key from Account > Manage API keys>"

or copy config/settings.example.json to config/settings.json and fill it in
(that path is gitignored).
MSG
  exit 1
fi

echo "==> 1/3  Confirming endpoint paths against $BASE_URL"
if python3 scripts/fetch_spec.py --spec "$BASE_URL" --write; then
  echo
else
  cat <<MSG

Could not read a spec from $BASE_URL.
Download it from SwaggerHub (Export > Download API > JSON) and run:
    python3 scripts/fetch_spec.py --spec ~/Downloads/informdirect.json --write

Carrying on with the provisional paths - the next step will show whether they work.
MSG
fi

echo "==> 2/3  Resolved settings"
python3 -m informdirect config || exit 1
echo

echo "==> 3/3  Live check"
python3 -m informdirect check
CHECK_STATUS=$?
echo

case "$CHECK_STATUS" in
  0)
    cat <<'MSG'
==> Working, and the API returns everything the planner feed needs.

Next, earn the production key - Inform Direct enable it only after seeing
successful sandbox calls to all four endpoints:

    python3 -m informdirect verify --confirm \
      --company-number <a company you can add> --auth-code <its CH auth code>

That adds then removes the company, and prints the details for the
support@informdirect.co.uk request.
MSG
    ;;
  2)
    cat <<'MSG'
==> Connected, but some fields the planner needs came back empty.

See the field coverage above. To tell a missing field from a renamed one:

    python3 -m informdirect companies --out out/feed.csv --json out/snapshot.json

then look at a company's "raw" payload in out/snapshot.json. If the dates are
there under a different name, add that name to the aliases in
informdirect/models.py and re-run.
MSG
    ;;
  *)
    cat <<'MSG'
==> Could not reach the API. Common causes, in order:

  - base_url wrong, or sandbox lives on a different host
  - auth_path / refresh_path / api_key_field do not match the real spec
    (check them against the docs; defaults are /authenticate, /refresh, apiKey)
  - the key has not been activated
MSG
    ;;
esac

exit "$CHECK_STATUS"
