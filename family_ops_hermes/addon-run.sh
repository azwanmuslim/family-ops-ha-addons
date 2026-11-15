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

# One-time, direct Codex proof independent of HA ingress and browser WebSockets.
# Output is an allowlisted diagnostic line only; credentials and model text stay private.
if [ "${CODEX_SMOKE_ONCE:-false}" = "true" ] && [ ! -e /data/.codex_direct_v3_done ]; then
  touch /data/.codex_direct_v3_done
  chmod 600 /data/.codex_direct_v3_done
  echo 'HERMES_CODEX_DIRECT_V3: starting'
  set +e
  timeout 105s /opt/hermes/.venv/bin/python /codex-direct-check.py
  diag_rc=$?
  set -e
  if [ "$diag_rc" -ne 0 ]; then
    echo "HERMES_CODEX_DIRECT_V3: diagnostic_error=true return_code=$diag_rc"
  fi
fi

# Optional one-time fallback model diagnostic. Token and model text stay local.
if [ "${CODEX_SMOKE_ONCE:-false}" = "true" ] && [ ! -e /data/.codex_provider_v4_done ]; then
  touch /data/.codex_provider_v4_done
  chmod 600 /data/.codex_provider_v4_done
  echo 'HERMES_CODEX_PROVIDER_V4: starting'
  set +e
  timeout 105s /opt/hermes/.venv/bin/python /codex-provider-probe-v4.py
  probe_rc=$?
  set -e
  if [ "$probe_rc" -ne 0 ]; then
    echo "HERMES_CODEX_PROVIDER_V4: diagnostic_error=true return_code=$probe_rc"
  fi
fi

# Query actual authorized Codex model catalogue and test one permitted model.
# One-time marker prevents repeated API calls across restarts.
if [ "${CODEX_SMOKE_ONCE:-false}" = "true" ] && [ ! -e /data/.codex_discovery_v5_done ]; then
  touch /data/.codex_discovery_v5_done
  chmod 600 /data/.codex_discovery_v5_done
  echo 'HERMES_CODEX_DISCOVERY_V5: starting'
  set +e
  timeout 105s /opt/hermes/.venv/bin/python /codex-model-discovery-v5.py
  probe_rc=$?
  set -e
  if [ "$probe_rc" -ne 0 ]; then
    echo "HERMES_CODEX_DISCOVERY_V5: diagnostic_error=true return_code=$probe_rc"
  fi
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
