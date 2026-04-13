#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
FRONTEND_URL="${FRONTEND_URL:-http://127.0.0.1:5173}"
SCREENSHOTS_DIR="$ROOT_DIR/screenshots/demos"
PLAYWRIGHT_RESULTS_DIR="$ROOT_DIR/test-results"

echo "=== Travel Agent Pro — Demo Recording ==="
echo ""

echo "→ Checking frontend at $FRONTEND_URL..."
if ! curl -sf "$FRONTEND_URL" > /dev/null 2>&1; then
  echo "  Frontend not running."
  echo "  Start the frontend in another terminal with: cd $ROOT_DIR/frontend && npm run dev"
  exit 1
fi
echo "  ✅ Frontend is running"

rm -rf "$SCREENSHOTS_DIR"
find "$PLAYWRIGHT_RESULTS_DIR" -maxdepth 1 -mindepth 1 -name 'demo-*' -exec rm -rf {} + 2>/dev/null || true
mkdir -p "$SCREENSHOTS_DIR"

echo ""
echo "→ Running demo recording..."
test_status=0
(
  cd "$ROOT_DIR"
  npx playwright test scripts/demo/demo-full-flow.spec.ts --config=playwright.config.ts
) || test_status=$?

echo ""
echo "→ Collecting videos..."
video_count=0
copied_video_count=0
if [[ -d "$PLAYWRIGHT_RESULTS_DIR" ]]; then
  while IFS= read -r video; do
    cp "$video" "$SCREENSHOTS_DIR/"
    video_count=$((video_count + 1))
    copied_video_count=$((copied_video_count + 1))
  done < <(find "$PLAYWRIGHT_RESULTS_DIR" -path '*/demo-*/*.webm' -type f -print)
fi

if [[ "$video_count" -eq 0 && -d "$SCREENSHOTS_DIR" ]]; then
  while IFS= read -r existing_video; do
    [[ -n "$existing_video" ]] || continue
    video_count=$((video_count + 1))
  done < <(find "$SCREENSHOTS_DIR" -maxdepth 1 -name '*.webm' -type f -print)
fi

if [[ "$video_count" -gt 0 ]]; then
  if [[ "$copied_video_count" -gt 0 ]]; then
    echo "  ✅ Copied $copied_video_count video(s) to $SCREENSHOTS_DIR"
  else
    echo "  ✅ Found $video_count video(s) in $SCREENSHOTS_DIR"
  fi
else
  echo "  ⚠️  No video files found under test-results/"
fi

echo ""
echo "=== Demo Complete ==="
echo "Screenshots: $SCREENSHOTS_DIR"
echo "Videos:      $SCREENSHOTS_DIR/*.webm"
ls -la "$SCREENSHOTS_DIR" 2>/dev/null || echo "(directory empty)"

exit "$test_status"
