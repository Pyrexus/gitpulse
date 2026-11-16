#!/usr/bin/env bash
# Run GitPulse on a host without Docker/systemd and without inbound ports.
# Public HTTPS comes from a Cloudflare Tunnel:
#   * TUNNEL_TOKEN set in .env → named tunnel (stable hostname, recommended)
#   * otherwise                → quick tunnel (*.trycloudflare.com, changes on restart;
#                                the bot re-points its webhooks automatically)
set -euo pipefail
cd "$(dirname "$0")/../.."
ROOT=$(pwd)
LOGS="$ROOT/data/logs"
mkdir -p "$LOGS"

"$ROOT/deploy/container/stop.sh" >/dev/null 2>&1 || true

sudo service postgresql start >/dev/null

TUNNEL_TOKEN=$(grep -E '^TUNNEL_TOKEN=' .env | cut -d= -f2- || true)
if [ -n "$TUNNEL_TOKEN" ]; then
  nohup cloudflared tunnel --no-autoupdate run --token "$TUNNEL_TOKEN" >"$LOGS/tunnel.log" 2>&1 &
  echo $! >"$ROOT/data/tunnel.pid"
else
  nohup cloudflared tunnel --no-autoupdate --url http://127.0.0.1:8080 >"$LOGS/tunnel.log" 2>&1 &
  echo $! >"$ROOT/data/tunnel.pid"
  for _ in $(seq 1 60); do
    URL=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$LOGS/tunnel.log" | head -1 || true)
    [ -n "$URL" ] && break
    sleep 1
  done
  [ -z "${URL:-}" ] && { echo "tunnel did not come up, see $LOGS/tunnel.log"; exit 1; }
  sed -i "s#^PUBLIC_URL=.*#PUBLIC_URL=$URL#" .env
  echo "Public URL: $URL"
fi

nohup bash -c '
  while true; do
    .venv/bin/alembic upgrade head && .venv/bin/python -m app
    echo "bot exited ($?) — restarting in 3s"; sleep 3
  done' >>"$LOGS/bot.log" 2>&1 &
echo $! >"$ROOT/data/bot.pid"
echo "GitPulse started. Logs: tail -f $LOGS/bot.log"
