#!/usr/bin/env bash
set -euo pipefail

CONFIG=/data/options.json
interval="$(jq -r '.memory_watch_interval_seconds // 30' "$CONFIG")"
high="$(jq -r '.memory_high_threshold // 85' "$CONFIG")"
enabled="$(jq -r '.memory_watch_enabled // true' "$CONFIG")"

[[ "$enabled" == "true" ]] || { echo "[ram-guard] disabled."; exit 0; }
[[ -n "${SUPERVISOR_TOKEN:-}" ]] || { echo "[ram-guard] ERROR: SUPERVISOR_TOKEN unavailable." >&2; exit 1; }

auth=(-H "Authorization: Bearer $SUPERVISOR_TOKEN")

while true; do
  mem="$(curl -fsS --connect-timeout 5 --max-time 15 "${auth[@]}"     http://supervisor/core/api/states/sensor.memory_use_percent 2>/dev/null     | jq -r '.state // empty' || true)"

  if [[ ! "$mem" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
    echo "[ram-guard] ERROR: overall RAM unavailable." >&2
    sleep "$interval"
    continue
  fi

  echo "[ram-guard] Overall host RAM: ${mem}%"
  if ! awk -v v="$mem" -v t="$high" 'BEGIN{exit !(v>=t)}'; then
    sleep "$interval"
    continue
  fi

  dow="$(TZ=Asia/Kuala_Lumpur date +%u)"
  hm="$(TZ=Asia/Kuala_Lumpur date +%H%M)"
  if (( dow >= 1 && dow <= 5 )) && { (( 10#$hm >= 630 && 10#$hm < 800 )) || (( 10#$hm >= 1830 && 10#$hm < 2000 )); }; then
    echo "[ram-guard] RAM >=${high}%, weekday MYT commute blackout active; restart deferred."
    sleep "$interval"
    continue
  fi

  echo "[ram-guard] RAM >=${high}% outside blackout; restarting HA Core."
  curl -sS --connect-timeout 5 --max-time 20 -X POST "${auth[@]}"     http://supervisor/core/restart >/dev/null 2>&1 || true

  sleep 60
  recovered=0
  for i in {1..12}; do
    if curl -fsS --connect-timeout 5 --max-time 10 "${auth[@]}"       http://supervisor/core/api/ >/dev/null 2>&1; then
      echo "[ram-guard] HA Core recovered after restart."
      recovered=1
      break
    fi
    sleep 10
  done
  if [[ "$recovered" != 1 ]]; then
    echo "[ram-guard] ERROR: HA Core failed to recover after restart." >&2
  fi

  sleep "$interval"
done
