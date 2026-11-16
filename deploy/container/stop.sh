#!/usr/bin/env bash
# Stop the processes started by start.sh.
cd "$(dirname "$0")/../.."
for name in bot tunnel; do
  pidfile="data/$name.pid"
  if [ -f "$pidfile" ]; then
    pid=$(cat "$pidfile")
    pkill -P "$pid" 2>/dev/null || true
    kill "$pid" 2>/dev/null || true
    rm -f "$pidfile"
  fi
done
pkill -f "python -m app" 2>/dev/null || true
echo "stopped"
