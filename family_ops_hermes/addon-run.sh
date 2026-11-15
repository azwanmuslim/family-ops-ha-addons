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
if [ "${CODEX_SMOKE_ONCE:-false}" = "true" ] && [ ! -e /data/.codex_smoke_v1_done ]; then
  echo 'HERMES_CODEX_SMOKE: starting'
  # Prevent automatic repeated attempts if the test fails or the app restarts.
  touch /data/.codex_smoke_v1_done
  chmod 600 /data/.codex_smoke_v1_done
  set +e
  timeout 90s /opt/hermes/.venv/bin/hermes --oneshot 'Reply with exactly CODEX_READY and no other text. Do not use any tools.' > /tmp/codex_smoke_answer.log 2>&1
  smoke_exit=$?
  set -e
  if [ "$smoke_exit" -eq 0 ] && grep -Eq '(^|[[:space:]])CODEX_READY([[:space:]]|$)' /tmp/codex_smoke_answer.log; then
    printf 'HERMES_CODEX_SMOKE: result=pass model=gpt-5.4\n'
  else
    printf 'HERMES_CODEX_SMOKE: result=fail exit_status=%s\n' "$smoke_exit"
  fi
  rm -f /tmp/codex_smoke_answer.log
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
