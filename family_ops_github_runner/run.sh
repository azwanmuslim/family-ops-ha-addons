#!/usr/bin/env bash
set -euo pipefail

CONFIG=/data/options.json
RUNNER_DIR=/data/actions-runner
TEMPLATE=/opt/actions-runner-template

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

if [[ ! -f .runner ]]; then
  if [[ -z "$registration_token" || "$registration_token" == "null" ]]; then
    echo "ERROR: registration_token is required for first-time registration."
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
else
  echo "Runner already registered; using persisted configuration."
fi

unset registration_token

cleanup() {
  echo "Stopping GitHub runner..."
}
trap cleanup EXIT INT TERM

exec ./run.sh
