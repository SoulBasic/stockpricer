#!/bin/bash
# Deploys stockpricer from this checkout to a Portainer-managed Docker host,
# without a registry:
#   1. unit tests
#   2. build context tar (Dockerfile + sources only, nothing else leaves the Mac)
#   3. Portainer API: build image stockpricer:<git sha> on the Docker host
#   4. create or update the Portainer stack "stockpricer" (docker-compose.yml
#      without `build`, image = the one just built); a Git-based stack of the
#      same name (the old GHCR setup) is replaced once
#   5. wait for /health and show the container log tail
#   6. remove old stockpricer:* images, keeping the current and previous one
#
# Settings come from .env.deploy (gitignored) or the environment:
#   PORTAINER_URL       e.g. https://192.168.1.7:9443
#   PORTAINER_TOKEN     Portainer → My account → Access tokens
#   PORTAINER_ENDPOINT  Portainer environment name (default: local)
#   STACK_ENV           stack variables passed to docker-compose.yml
#                       (default: "BIND_ADDR=0.0.0.0 TRUST_PROXY=1")
#
#   deploy/portainer.sh               test, build, deploy
#   deploy/portainer.sh --skip-tests  skip the unit tests
set -euo pipefail

cd "$(dirname "$0")/.."
JQ=$(command -v jq || echo /opt/homebrew/bin/jq)
PY=/usr/bin/python3

SKIP_TESTS=0
for arg in "$@"; do
  case "$arg" in
    --skip-tests) SKIP_TESTS=1 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

# .env.deploy without overriding the environment; split at the first "=" only.
if [ -f .env.deploy ]; then
  while IFS= read -r line || [ -n "$line" ]; do
    [[ "$line" =~ ^[A-Za-z_][A-Za-z0-9_]*= ]] || continue
    key=${line%%=*}
    value=${line#*=}
    if [ -z "${!key:-}" ]; then export "$key=$value"; fi
  done < .env.deploy
fi
: "${PORTAINER_URL:?set PORTAINER_URL in .env.deploy}"
: "${PORTAINER_TOKEN:?set PORTAINER_TOKEN in .env.deploy}"
: "${PORTAINER_ENDPOINT:=local}"
: "${STACK_ENV:=BIND_ADDR=0.0.0.0 TRUST_PROXY=1}"
STACK=stockpricer
API="$PORTAINER_URL/api"
# Portainer on the LAN uses its self-signed certificate.
pcurl() { curl -sSk --fail-with-body -H "X-API-Key: $PORTAINER_TOKEN" "$@"; }
step() { printf '\n\033[1m%s\033[0m\n' "$*"; }

# ---- 1. tests
if [ "$SKIP_TESTS" = 0 ]; then
  step "unit tests"
  "$PY" -m unittest -q test_stock_server
fi

# ---- 2. build context
TAG=$(git rev-parse --short HEAD)
if [ -n "$(git status --porcelain -- stock_server.py q.py Dockerfile docker-compose.yml)" ]; then
  TAG="$TAG-dirty-$(date +%Y%m%d%H%M%S)"
fi
IMAGE="stockpricer:$TAG"
CTX=$(mktemp -d)
trap 'rm -rf "$CTX"' EXIT
# No macOS extended attributes / AppleDouble files: Docker on Linux rejects them.
COPYFILE_DISABLE=1 tar --no-xattrs --no-mac-metadata -cf "$CTX/context.tar" Dockerfile stock_server.py q.py

# ---- 3. image on the Docker host
if ! ENDPOINTS=$(pcurl "$API/endpoints" 2>&1); then
  echo "Portainer refused the request ($ENDPOINTS). Check PORTAINER_URL and PORTAINER_TOKEN." >&2
  exit 1
fi
EID=$(echo "$ENDPOINTS" | "$JQ" -r --arg n "$PORTAINER_ENDPOINT" '.[] | select(.Name == $n) | .Id')
[ -n "$EID" ] || { echo "Portainer environment \"$PORTAINER_ENDPOINT\" not found" >&2; exit 1; }
DOCKER="$API/endpoints/$EID/docker"
step "building image $IMAGE on Portainer environment \"$PORTAINER_ENDPOINT\" ($EID)"
BUILD_LOG=$(curl -sSk -H "X-API-Key: $PORTAINER_TOKEN" -X POST "$DOCKER/build?t=$IMAGE&rm=1&forcerm=1" \
  -H "Content-Type: application/x-tar" --data-binary @"$CTX/context.tar" -w '\n%{http_code}')
BUILD_STATUS=${BUILD_LOG##*$'\n'}
BUILD_LOG=${BUILD_LOG%$'\n'*}
if [ "$BUILD_STATUS" != 200 ] || echo "$BUILD_LOG" | grep -q '"errorDetail"'; then
  echo "image build failed (HTTP $BUILD_STATUS):" >&2
  echo "$BUILD_LOG" | "$JQ" -r 'select(.error) | .error' 2>/dev/null || echo "$BUILD_LOG"
  exit 1
fi
echo "$BUILD_LOG" | "$JQ" -r 'select(.stream) | .stream' 2>/dev/null | grep -E "^(Step|Successfully)" || true

# ---- 4. stack
COMPOSE=$(sed '/^[[:space:]]*build:[[:space:]]*\.[[:space:]]*$/d' docker-compose.yml)
ENV_JSON=$("$JQ" -n --arg img "$IMAGE" --arg extra "$STACK_ENV" '
  [{name: "STOCKPRICER_IMAGE", value: $img}] +
  [$extra | splits(" +") | select(length > 0) | capture("^(?<name>[^=]+)=(?<value>.*)$")]')
STACK_JSON=$(pcurl "$API/stacks" | "$JQ" -c --arg n "$STACK" --argjson e "$EID" \
  '[.[] | select(.Name == $n and .EndpointId == $e)][0] // empty')
PREVIOUS=""
if [ -n "$STACK_JSON" ] && [ "$(echo "$STACK_JSON" | "$JQ" -r '.GitConfig != null')" = true ]; then
  # One-time migration from the Git/GHCR stack: Portainer can't turn a Git
  # stack into a file stack, so remove it (its container goes with it) and
  # create the new one right away. Nothing is stored in the container.
  SID=$(echo "$STACK_JSON" | "$JQ" -r .Id)
  step "replacing Git-based stack $STACK ($SID) with a local-image stack"
  pcurl -X DELETE "$API/stacks/$SID?endpointId=$EID" >/dev/null
  STACK_JSON=""
fi
if [ -z "$STACK_JSON" ]; then
  step "creating stack $STACK"
  BODY=$("$JQ" -n --arg n "$STACK" --arg c "$COMPOSE" --argjson env "$ENV_JSON" \
    '{name: $n, stackFileContent: $c, env: $env, fromAppTemplate: false}')
  pcurl -X POST "$API/stacks/create/standalone/string?endpointId=$EID" \
    -H "Content-Type: application/json" -d "$BODY" >/dev/null
else
  SID=$(echo "$STACK_JSON" | "$JQ" -r .Id)
  PREVIOUS=$(echo "$STACK_JSON" | "$JQ" -r '.Env[]? | select(.name == "STOCKPRICER_IMAGE") | .value')
  step "updating stack $STACK ($SID): $PREVIOUS → $IMAGE"
  BODY=$("$JQ" -n --arg c "$COMPOSE" --argjson env "$ENV_JSON" \
    '{stackFileContent: $c, env: $env, prune: true, pullImage: false}')
  pcurl -X PUT "$API/stacks/$SID?endpointId=$EID" -H "Content-Type: application/json" -d "$BODY" >/dev/null
fi

# ---- 5. health and logs
HOST=$(echo "$PORTAINER_URL" | sed -E 's#^https?://([^:/]+).*#\1#')
PORT=$(echo "$STACK_ENV" | tr ' ' '\n' | sed -n 's/^PORT=//p')
PORT=${PORT:-8849}
step "waiting for http://$HOST:$PORT/health"
ok=0
for _ in $(seq 1 30); do
  if curl -sf --max-time 2 "http://$HOST:$PORT/health" >/dev/null; then ok=1; break; fi
  sleep 1
done
CID=$(pcurl "$DOCKER/containers/json?all=1" | "$JQ" -r --arg p "$STACK" \
  '[.[] | select(.Labels["com.docker.compose.project"] == $p)][0].Id // empty')
if [ -n "$CID" ]; then
  # Docker multiplexes stdout/stderr with an 8-byte header per frame.
  pcurl "$DOCKER/containers/$CID/logs?stdout=1&stderr=1&tail=8" | "$PY" -c '
import sys
data = sys.stdin.buffer.read()
out, i = [], 0
while i + 8 <= len(data) and data[i] in (0, 1, 2):
    n = int.from_bytes(data[i+4:i+8], "big"); out.append(data[i+8:i+8+n]); i += 8 + n
sys.stdout.write((b"".join(out) if out else data).decode("utf-8", "replace"))'
fi
if [ "$ok" = 1 ]; then
  echo "stockpricer $TAG is up on http://$HOST:$PORT"
else
  echo "stockpricer did not answer /health within 30 s (see the log above)" >&2
  exit 1
fi

# ---- 6. old images (keep the current and the previous one)
for ref in $(pcurl "$DOCKER/images/json" | "$JQ" -r '.[].RepoTags[]? | select(startswith("stockpricer:"))'); do
  if [ "$ref" != "$IMAGE" ] && [ "$ref" != "$PREVIOUS" ]; then
    pcurl -X DELETE "$DOCKER/images/$ref" >/dev/null 2>&1 && echo "removed old image $ref" || true
  fi
done
