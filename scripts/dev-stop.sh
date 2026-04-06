#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

terminate_pid() {
  local pid="${1:-}"
  if [[ -z "$pid" ]] || ! kill -0 "$pid" 2>/dev/null; then
    return 0
  fi

  kill -TERM "$pid" 2>/dev/null || true

  for _ in {1..20}; do
    if ! kill -0 "$pid" 2>/dev/null; then
      return 0
    fi
    sleep 0.25
  done

  kill -KILL "$pid" 2>/dev/null || true
}

terminate_process_group() {
  local leader_pid="${1:-}"
  if [[ -z "$leader_pid" ]] || ! kill -0 "$leader_pid" 2>/dev/null; then
    return 0
  fi

  kill -TERM -- "-$leader_pid" 2>/dev/null || terminate_pid "$leader_pid"

  for _ in {1..20}; do
    if ! kill -0 "$leader_pid" 2>/dev/null; then
      return 0
    fi
    sleep 0.25
  done

  kill -KILL -- "-$leader_pid" 2>/dev/null || kill -KILL "$leader_pid" 2>/dev/null || true
}

collect_pids() {
  {
    if command -v lsof >/dev/null 2>&1; then
      lsof -ti tcp:8000 -sTCP:LISTEN 2>/dev/null || true
      lsof -ti tcp:5173 -sTCP:LISTEN 2>/dev/null || true
    fi

    ps -axo pid=,command= | awk -v root="$ROOT_DIR" '
      index($0, "bash ./scripts/dev.sh") > 0 { print $1 }
      index($0, root "/scripts/dev.sh") > 0 { print $1 }
    ' || true
  } | awk 'NF { print $1 }' | sort -u
}

pids=()
while IFS= read -r pid; do
  [[ -n "$pid" ]] && pids+=("$pid")
done < <(collect_pids)

pgids=()
for pid in "${pids[@]}"; do
  pgid="$(ps -o pgid= -p "$pid" 2>/dev/null | tr -d '[:space:]' || true)"
  [[ -n "$pgid" ]] && pgids+=("$pgid")
done

if [[ "${#pids[@]}" -eq 0 ]]; then
  echo "No local dev processes found."
  exit 0
fi

echo "Stopping local dev processes: ${pids[*]}"

for pgid in $(printf '%s\n' "${pgids[@]}" | sort -u); do
  terminate_process_group "$pgid"
done

for pid in "${pids[@]}"; do
  terminate_pid "$pid"
done

echo "Done."
