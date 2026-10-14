#!/bin/sh
# Usage: smoke.sh <link_base_url> <function_url>
#
# Two checks after a deploy (task infra:smoke). Through the site, a Session
# link that cannot be decoded must answer 404 invalid_link: the site checked
# only the token's shape, so that answer came from this backend, over the
# site's signed call, and the league was never asked. Straight at the Function
# URL, an unsigned call must answer 403: only signed calls get in.
set -eu

link_base_url=${1:?link_base_url}
function_url=${2:?function_url}

body=$(mktemp)
trap 'rm -f "$body"' EXIT

link="${link_base_url}deploy-check.ics"
# A new version can take a moment to take every call; allow a short wait.
attempt=0
while :; do
  attempt=$((attempt + 1))
  code=$(curl -sS --max-time 20 -o "$body" -w '%{http_code}' "$link" || true)
  if [ "$code" = 404 ] && grep -q '"code":"invalid_link"' "$body"; then
    break
  fi
  if [ "$attempt" -ge 6 ]; then
    echo "::error::$link answered $code, expected 404 invalid_link from the backend" >&2
    head -c 500 "$body" >&2
    echo >&2
    exit 1
  fi
  sleep 10
done
echo "$link: 404 invalid_link, from the backend through the site"

code=$(curl -sS --max-time 20 -o /dev/null -w '%{http_code}' "${function_url%/}/healthz" || true)
if [ "$code" != 403 ]; then
  echo "::error::${function_url%/}/healthz answered $code unsigned, expected 403" >&2
  exit 1
fi
echo "${function_url%/}/healthz: 403 unsigned, as it should be"
