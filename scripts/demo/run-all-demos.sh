#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DEMO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$ROOT_DIR/backend"
BACKEND_URL="${BACKEND_URL:-http://127.0.0.1:8000}"
FRONTEND_URL="${FRONTEND_URL:-http://127.0.0.1:5173}"
BACKEND_DATA_DIR="${BACKEND_DATA_DIR:-$BACKEND_DIR/data}"
SEED_FILE="$DEMO_DIR/seed-memory.json"
SCREENSHOTS_DIR="$ROOT_DIR/screenshots/demos"

if [[ -n "${BACKEND_PYTHON:-}" ]]; then
  resolved_backend_python="$BACKEND_PYTHON"
elif [[ -x "$BACKEND_DIR/.venv/bin/python" ]]; then
  resolved_backend_python="$BACKEND_DIR/.venv/bin/python"
elif command -v python > /dev/null 2>&1; then
  resolved_backend_python="$(command -v python)"
else
  resolved_backend_python=""
fi

echo "=== Travel Agent Pro — Demo Recording ==="
echo ""

if [[ -z "$resolved_backend_python" ]]; then
  echo "ERROR: no usable python found for backend seed helper"
  echo "Set BACKEND_PYTHON or create the backend virtualenv first."
  exit 1
fi

if [[ ! -f "$SEED_FILE" ]]; then
  echo "ERROR: seed file not found: $SEED_FILE"
  exit 1
fi

echo "→ Checking backend at $BACKEND_URL..."
if ! curl -sf "$BACKEND_URL/health" > /dev/null 2>&1; then
  echo "  Backend not running."
  echo "  Start services in another terminal with: $ROOT_DIR/scripts/dev.sh"
  exit 1
fi
echo "  ✅ Backend is running"

echo "→ Checking frontend at $FRONTEND_URL..."
if ! curl -sf "$FRONTEND_URL" > /dev/null 2>&1; then
  echo "  Frontend not running."
  echo "  Start services in another terminal with: $ROOT_DIR/scripts/dev.sh"
  exit 1
fi
echo "  ✅ Frontend is running"

echo ""
echo "→ Seeding demo memory into $BACKEND_DATA_DIR..."
seed_summary="$(
  cd "$BACKEND_DIR" && \
    "$resolved_backend_python" -m memory.demo_seed \
      --seed-file "$SEED_FILE" \
      --data-dir "$BACKEND_DATA_DIR"
)"
echo "  ✅ $seed_summary"

mkdir -p "$SCREENSHOTS_DIR"

echo ""
echo "→ Running demo recording..."
test_status=0
(
  cd "$DEMO_DIR"
  npx playwright test --config=playwright.config.ts
) || test_status=$?

echo ""
echo "→ Collecting videos..."
video_count=0
while IFS= read -r video; do
  cp "$video" "$SCREENSHOTS_DIR/"
  video_count=$((video_count + 1))
done < <(find "$ROOT_DIR" -path "*/test-results/*.webm" -type f -print)

if [[ "$video_count" -gt 0 ]]; then
  echo "  ✅ Copied $video_count video(s) to $SCREENSHOTS_DIR"
else
  echo "  ⚠️  No video files found under test-results/"
fi

echo ""
echo "=== Demo Complete ==="
echo "Screenshots: $SCREENSHOTS_DIR"
echo "Videos:      $SCREENSHOTS_DIR/*.webm"
ls -la "$SCREENSHOTS_DIR" 2>/dev/null || echo "(directory empty)"

exit "$test_status"
