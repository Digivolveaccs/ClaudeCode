#!/usr/bin/env bash
# Find the API's OpenAPI spec and push it to the branch, so the rest of the
# setup can be finished from anywhere.
#
#   ./scripts/grab_spec.sh
#
# Saves every probe response under config/spec-probe/ whether or not a spec is
# found, so a failed hunt still carries enough to work out the real path.
# Sends no credentials unless INFORMDIRECT_API_KEY is set.

set -uo pipefail
cd "$(dirname "$0")/.."

BASE="${INFORMDIRECT_BASE_URL:-https://api.informdirect.co.uk}"
OUT=config/spec-probe
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
mkdir -p "$OUT"

AUTH=()
if [ -n "${INFORMDIRECT_API_KEY:-}" ]; then
  AUTH=(-H "X-Api-Key: ${INFORMDIRECT_API_KEY}")
fi

echo "==> Probing $BASE"
for path in \
  /swagger/v1/swagger.json /swagger/v1/swagger.yaml /openapi.json \
  /openapi/v1.json /swagger.json /v1/swagger.json /api-docs \
  /api/swagger.json /swagger/swagger-ui-init.js /swagger/index.html /swagger /
do
  name="$(echo "$path" | sed 's#^/##; s#/#_#g')"
  [ -z "$name" ] && name="root"
  code=$(curl -s -L --max-time 20 "${AUTH[@]}" \
           -o "$OUT/$name" -w '%{http_code}' "$BASE$path" 2>/dev/null)
  size=$(wc -c < "$OUT/$name" 2>/dev/null | tr -d ' ')
  echo "  $code  ${size}b  $path"
done

# Strip anything that is obviously an error page rather than content.
for f in "$OUT"/*; do
  if grep -qi "resource you are looking for has been removed" "$f" 2>/dev/null; then
    rm -f "$f"
  fi
done

echo
echo "==> Trying the importer (it validates before accepting)"
python3 scripts/fetch_spec.py --spec "$BASE" --write && FOUND=1 || FOUND=0

echo
echo "==> Pushing what we have"
git add -A "$OUT" config/endpoints.json 2>/dev/null
if git diff --cached --quiet; then
  echo "nothing new to push"
else
  git commit -q -m "Spec probe results from $BASE" \
    -m "Raw responses under config/spec-probe/ for offline analysis."
  git push -u origin "$BRANCH" && echo "pushed to $BRANCH"
fi

if [ "$FOUND" = "1" ]; then
  echo
  echo "Spec found and config/endpoints.json updated. Nothing else needed from you."
else
  echo
  echo "No spec at the public paths. The probe responses were pushed anyway -"
  echo "they are usually enough to work out the real location."
fi
