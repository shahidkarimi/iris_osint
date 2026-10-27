#!/usr/bin/env bash
# Run the harvester on this machine, writing into the server's database through an SSH tunnel.
#
# Needed because JLL's CDN (Akamai) answers 403 to the server's datacenter IP; the site is
# publicly served to normal networks. The DB password is read from the server's .env and never printed.
#
#   IRIS_SERVER=root@<server-ip> scripts/remote-harvest.sh harvest            # incremental live harvest
#   IRIS_SERVER=root@<server-ip> scripts/remote-harvest.sh harvest --full     # re-fetch everything
#   IRIS_SERVER=root@<server-ip> scripts/remote-harvest.sh reprocess          # re-apply rules, no fetching
#   IRIS_SERVER=root@<server-ip> scripts/remote-harvest.sh report --out samples/quality_report.md
set -euo pipefail

: "${IRIS_SERVER:?set IRIS_SERVER, e.g. root@203.0.113.10}"
REMOTE_DIR="${IRIS_REMOTE_DIR:-/opt/iris-sig45}"
LOCAL_PORT="${IRIS_TUNNEL_PORT:-15432}"
cd "$(dirname "$0")/.."

[ -x .venv/bin/jll-harvester ] || { echo "run 'make install' first" >&2; exit 1; }

# one SSH connection for all values (the server's firewall rate-limits SSH connections)
# shellcheck disable=SC2029 # REMOTE_DIR is meant to expand locally
read -r USER_NAME DB_NAME PW < <(ssh "$IRIS_SERVER" \
  "cd $REMOTE_DIR && . ./.env && printf '%s %s %s\n' \"\${POSTGRES_USER:-iris}\" \"\${POSTGRES_DB:-iris}\" \"\$POSTGRES_PASSWORD\"")

ssh -N -o ExitOnForwardFailure=yes -L "${LOCAL_PORT}:127.0.0.1:5432" "$IRIS_SERVER" &
TUNNEL=$!
trap 'kill $TUNNEL 2>/dev/null || true' EXIT
sleep 2

export IRIS_DATABASE_URL="postgresql://${USER_NAME:-iris}:${PW}@127.0.0.1:${LOCAL_PORT}/${DB_NAME:-iris}"
.venv/bin/jll-harvester "${@:-harvest}"
