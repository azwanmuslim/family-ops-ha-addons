#!/usr/bin/env bash
set -euo pipefail

CONFIG=/data/options.json
RUNNER_DIR=/data/actions-runner
TEMPLATE=/opt/actions-runner-template
TOKEN_HASH_FILE=/data/registration-token.sha256

repo_url="$(jq -r '.repo_url' "$CONFIG")"
registration_token="$(jq -r '.registration_token' "$CONFIG")"
runner_name="$(jq -r '.runner_name' "$CONFIG")"
labels="$(jq -r '.labels' "$CONFIG")"

mkdir -p "$RUNNER_DIR"

if [[ ! -x "$RUNNER_DIR/run.sh" ]]; then
  cp -a "$TEMPLATE/." "$RUNNER_DIR/"
fi

cd "$RUNNER_DIR"
export RUNNER_ALLOW_RUNASROOT=1

if [[ -n "$registration_token" && "$registration_token" != "null" ]]; then
  current_hash="$(printf '%s' "$registration_token" | sha256sum | awk '{print $1}')"
  saved_hash=""
  if [[ -f "$TOKEN_HASH_FILE" ]]; then
    saved_hash="$(cat "$TOKEN_HASH_FILE" 2>/dev/null || true)"
  fi

  if [[ "$current_hash" != "$saved_hash" ]]; then
    echo "New registration token detected; refreshing persisted runner identity."
    rm -f .runner .credentials .credentials_rsaparams .service
  fi
fi

if [[ ! -f .runner ]]; then
  if [[ -z "$registration_token" || "$registration_token" == "null" ]]; then
    echo "ERROR: registration_token is required for first-time registration or registration reset."
    echo "Generate one in GitHub: repository Settings > Actions > Runners > New self-hosted runner."
    exit 1
  fi

  echo "Registering runner '$runner_name' for $repo_url ..."
  ./config.sh \
    --url "$repo_url" \
    --token "$registration_token" \
    --name "$runner_name" \
    --labels "$labels" \
    --work _work \
    --unattended \
    --replace

  printf '%s' "$current_hash" > "$TOKEN_HASH_FILE"
else
  echo "Runner already registered; using persisted configuration."
fi

unset registration_token

watcher_pid=''
if [[ "$(jq -r '.memory_watch_enabled // true' "$CONFIG")" == "true" ]]; then
  echo "Starting local memory watcher..."
  /memory_watcher.sh &
  watcher_pid=$!
fi

cleanup() {
  echo "Stopping Family Ops services..."
  if [[ -n "$watcher_pid" ]]; then
    kill "$watcher_pid" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

exec ./run.sh
