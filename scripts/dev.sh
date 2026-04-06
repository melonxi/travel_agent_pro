#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_DIR="$ROOT_DIR/backend"
FRONTEND_DIR="$ROOT_DIR/frontend"

if [[ ! -f "$BACKEND_DIR/.venv/bin/activate" ]]; then
  echo "Missing backend virtualenv: $BACKEND_DIR/.venv/bin/activate" >&2
  exit 1
fi

if command -v lsof >/dev/null 2>&1; then
  if lsof -ti tcp:8000 >/dev/null 2>&1; then
    echo "Port 8000 is already in use. Stop the existing backend first." >&2
    exit 1
  fi

  if lsof -ti tcp:5173 >/dev/null 2>&1; then
    echo "Port 5173 is already in use. Stop the existing frontend first." >&2
    exit 1
  fi
fi

cleanup() {
  local exit_code=$?
  trap - EXIT INT TERM

  if [[ -n "${BACKEND_PID:-}" ]] && kill -0 "$BACKEND_PID" 2>/dev/null; then
    kill "$BACKEND_PID" 2>/dev/null || true
  fi

  if [[ -n "${FRONTEND_PID:-}" ]] && kill -0 "$FRONTEND_PID" 2>/dev/null; then
    kill "$FRONTEND_PID" 2>/dev/null || true
  fi

  wait "${BACKEND_PID:-}" "${FRONTEND_PID:-}" 2>/dev/null || true
  exit "$exit_code"
}

trap cleanup EXIT INT TERM

echo "Starting backend on http://127.0.0.1:8000"
(
  cd "$BACKEND_DIR"
  source .venv/bin/activate
  exec uvicorn main:app --reload --port 8000
) &
BACKEND_PID=$!

echo "Starting frontend on http://127.0.0.1:5173"
(
  cd "$FRONTEND_DIR"
  exec npm run dev -- --host 127.0.0.1 --strictPort
) &
FRONTEND_PID=$!

while true; do
  if ! kill -0 "$BACKEND_PID" 2>/dev/null; then
    wait "$BACKEND_PID" 2>/dev/null || true
    exit 1
  fi

  if ! kill -0 "$FRONTEND_PID" 2>/dev/null; then
    wait "$FRONTEND_PID" 2>/dev/null || true
    exit 1
  fi

  sleep 1
done
