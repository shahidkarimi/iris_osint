#!/usr/bin/env bash
# Server-side release step. Run by .github/workflows/deploy.yml over SSH; also usable by hand.
#
#   deploy/deploy.sh release <image-ref> [revision]   # pull, switch, wait for health, verify; roll back on failure
#   deploy/deploy.sh rollback                         # return to the previously released image
#   deploy/deploy.sh status
#
# The released image is recorded as IRIS_IMAGE in .env (with IRIS_IMAGE_PREVIOUS for rollback), so plain
# `docker compose …` commands on the server keep using the image that is actually deployed.
set -euo pipefail
cd "$(dirname "$0")/.."

SERVICES=(db web scheduler proxy)
WAIT_TIMEOUT=${WAIT_TIMEOUT:-180}

get_env() { grep -E "^$1=" .env | tail -n1 | cut -d= -f2- || true; }
set_env() {
  if grep -qE "^$1=" .env; then sed -i "s|^$1=.*|$1=$2|" .env; else printf '%s=%s\n' "$1" "$2" >>.env; fi
}

up() { docker compose up -d --no-build --remove-orphans --wait --wait-timeout "$WAIT_TIMEOUT" "${SERVICES[@]}"; }

running_revision() {
  docker compose exec -T web python -c \
    "import json, urllib.request; print(json.load(urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=5))['revision'])"
}

rollback() {
  local previous current
  previous=$(get_env IRIS_IMAGE_PREVIOUS)
  current=$(get_env IRIS_IMAGE)
  if [[ -z "$previous" ]]; then
    echo "::error::no previous release recorded — nothing to roll back to"
    return 1
  fi
  echo "rolling back: $current -> $previous"
  set_env IRIS_IMAGE "$previous"
  set_env IRIS_IMAGE_PREVIOUS "$current"
  up
  echo "rolled back to $previous (revision $(running_revision))"
}

release() {
  local image=$1 revision=${2:-} current
  current=$(get_env IRIS_IMAGE)
  current=${current:-iris-sig45:latest} # compose default, i.e. a locally built image

  echo "::group::pull $image"
  docker pull "$image"
  echo "::endgroup::"
  if [[ -z "$revision" ]]; then
    revision=$(docker image inspect -f '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$image")
  fi

  if [[ "$current" != "$image" ]]; then
    set_env IRIS_IMAGE_PREVIOUS "$current"
    set_env IRIS_IMAGE "$image"
  fi

  echo "::group::switch services"
  if ! up; then
    echo "::endgroup::"
    echo "::error::services did not become healthy within ${WAIT_TIMEOUT}s"
    docker compose logs --tail 80 web || true
    rollback
    return 1
  fi
  # the Caddyfile is bind-mounted, so a changed file is only picked up on reload
  docker compose exec -T proxy caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile
  echo "::endgroup::"

  local running
  running=$(running_revision || true)
  if [[ "$running" != "$revision" ]]; then
    echo "::error::web reports revision '$running', expected '$revision'"
    rollback
    return 1
  fi

  # keep a week of releases locally for fast rollbacks; never touches images in use
  docker image prune -af --filter "until=168h" \
    --filter "label=org.opencontainers.image.source=$(docker image inspect -f '{{index .Config.Labels "org.opencontainers.image.source"}}' "$image")" \
    >/dev/null || true

  echo "RELEASED image=$image revision=$revision"
}

status() {
  echo "current:  $(get_env IRIS_IMAGE)"
  echo "previous: $(get_env IRIS_IMAGE_PREVIOUS)"
  docker compose ps --format '{{.Service}}: {{.Status}}'
}

case "${1:-}" in
  release) shift; release "$@" ;;
  rollback) rollback ;;
  status) status ;;
  *) echo "usage: $0 release <image-ref> [revision] | rollback | status" >&2; exit 2 ;;
esac
