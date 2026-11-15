#!/bin/bash
set -euo pipefail

NGINX_PID=""
TTYD_PID=""

shutdown() {
  if [ -n "${NGINX_PID}" ] && kill -0 "${NGINX_PID}" >/dev/null 2>&1; then
    kill -TERM "${NGINX_PID}" >/dev/null 2>&1 || true
    wait "${NGINX_PID}" 2>/dev/null || true
  fi

  if [ -n "${TTYD_PID}" ] && kill -0 "${TTYD_PID}" >/dev/null 2>&1; then
    kill -TERM "${TTYD_PID}" >/dev/null 2>&1 || true
    wait "${TTYD_PID}" 2>/dev/null || true
  fi
}

trap shutdown INT TERM

# Run an optional one-shot Codex inference before starting the gateway.
# OAuth credentials stay in the private /data directory. The answer and
# errors are never printed; only the boolean test result is emitted.
# A marker makes this check one-time even across restarts.
if [ "${CODEX_SMOKE_ONCE:-false}" = "true" ] && [ ! -e /data/.codex_smoke_v2_done ]; then
  echo 'HERMES_CODEX_SMOKE_V2: starting'
  # Mark before execution to prevent repeated model calls after restart.
  touch /data/.codex_smoke_v2_done
  chmod 600 /data/.codex_smoke_v2_done
  set +e
  timeout 100s /opt/hermes/.venv/bin/hermes chat --oneshot -q \
    'Reply with exactly CODEX_READY and nothing else. Do not use tools.' \
    > /tmp/hermes-codex-v2-private.log 2>&1
  smoke_exit=$?
  set -e

  smoke_result=fail
  smoke_category=unknown
  if [ "$smoke_exit" -eq 0 ] && grep -Eq '(^|[[:space:]])CODEX_READY([[:space:]]|$)' /tmp/hermes-codex-v2-private.log; then
    smoke_result=pass
    smoke_category=none
  elif [ "$smoke_exit" -eq 124 ]; then
    smoke_category=timeout
  elif grep -Eqi 'invalid.grant|unauthorized|not.authenticated|login.required|no.credentials|token.*(expired|invalid)|authentication.*(failed|required)' /tmp/hermes-codex-v2-private.log; then
    smoke_category=oauth
  elif grep -Eqi 'not supported when using codex|unsupported.model|model.not.found|model.*(not.available|not.supported)' /tmp/hermes-codex-v2-private.log; then
    smoke_category=model
  elif grep -Eqi 'rate.limit|quota|too.many.requests|resource.exhausted' /tmp/hermes-codex-v2-private.log; then
    smoke_category=quota
  elif grep -Eqi 'connection.(error|refused|failed)|dns.*fail|tls.*error|timed.out' /tmp/hermes-codex-v2-private.log; then
    smoke_category=network
  elif grep -Eqi 'unrecognized.arguments|invalid.choice|no.such.option|unknown.option|unknown.command' /tmp/hermes-codex-v2-private.log; then
    smoke_category=cli
  fi

  printf 'HERMES_CODEX_SMOKE_V2: result=%s category=%s exit_status=%s\n' "$smoke_result" "$smoke_category" "$smoke_exit"
  rm -f /tmp/hermes-codex-v2-private.log
fi

if [ "${ENABLE_TERMINAL:-true}" = "true" ]; then
  echo "Starting web terminal on internal port 7681"
  ttyd -W -i 127.0.0.1 -p 7681 -b /terminal bash -l &
  TTYD_PID=$!
fi

echo "Starting ingress nginx on port 9119"
nginx -g 'daemon off;' &
NGINX_PID=$!

echo "Starting Hermes gateway with dashboard on internal port 9120"
exec hermes gateway run
