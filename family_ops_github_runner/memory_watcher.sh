#!/usr/bin/env bash
set -euo pipefail

CONFIG=/data/options.json
STATE_DIR=/data/memory-watch
STATE_FILE="$STATE_DIR/state.json"
EVENT_DIR="$STATE_DIR/events"
mkdir -p "$EVENT_DIR"

enabled="$(jq -r '.memory_watch_enabled // true' "$CONFIG")"
interval="$(jq -r '.memory_watch_interval_seconds // 30' "$CONFIG")"
high="$(jq -r '.memory_high_threshold // 85' "$CONFIG")"
recover="$(jq -r '.memory_recovery_threshold // 70' "$CONFIG")"

if [[ "$enabled" != "true" ]]; then
  echo "Memory watcher disabled."
  exit 0
fi

ha_auth=(-H "Authorization: Bearer $SUPERVISOR_TOKEN")
sup_auth=(-H "Authorization: Bearer $SUPERVISOR_TOKEN")

snapshot() {
  local out="$1" now_ts mem used free cpu temp
  now_ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

  mem="$(curl -fsS "${ha_auth[@]}" http://supervisor/core/api/states/sensor.memory_use_percent | jq -r '.state // empty' 2>/dev/null || true)"
  used="$(curl -fsS "${ha_auth[@]}" http://supervisor/core/api/states/sensor.memory_use | jq -r '.state // empty' 2>/dev/null || true)"
  free="$(curl -fsS "${ha_auth[@]}" http://supervisor/core/api/states/sensor.memory_free | jq -r '.state // empty' 2>/dev/null || true)"
  cpu="$(curl -fsS "${ha_auth[@]}" http://supervisor/core/api/states/sensor.processor_use | jq -r '.state // empty' 2>/dev/null || true)"
  temp="$(curl -fsS "${ha_auth[@]}" http://supervisor/core/api/states/sensor.processor_temperature | jq -r '.state // empty' 2>/dev/null || true)"

  [[ "$mem" =~ ^[0-9]+([.][0-9]+)?$ ]] || return 1

  tmp_lines="$(mktemp)"
  : > "$tmp_lines"

  capture_stats() {
    local name="$1" path="$2" raw
    if raw="$(curl -fsS "${sup_auth[@]}" "http://supervisor${path}" 2>/dev/null)"; then
      printf '%s' "$raw" | jq -c --arg name "$name" '(.data // .) | {name:$name,cpu_percent:(.cpu_percent // null),memory_usage:(.memory_usage // null),memory_limit:(.memory_limit // null),memory_percent:(.memory_percent // null)}' >> "$tmp_lines"
    fi
  }

  capture_stats 'Home Assistant Core' '/core/stats'
  capture_stats 'Supervisor' '/supervisor/stats'

  if addons_raw="$(curl -fsS "${sup_auth[@]}" http://supervisor/addons 2>/dev/null)"; then
    while IFS=$'\t' read -r slug addon_name; do
      [[ -n "$slug" ]] || continue
      capture_stats "App: ${addon_name}" "/addons/${slug}/stats"
    done < <(printf '%s' "$addons_raw" | jq -r '(.data.addons // .addons // [])[] | [.slug, (.name // .slug)] | @tsv')
  fi

  containers="$(jq -s 'sort_by(.memory_usage // 0) | reverse' "$tmp_lines")"
  rm -f "$tmp_lines"

  container_total="$(printf '%s' "$containers" | jq '[.[].memory_usage // 0] | add // 0')"

  jq -n \
    --arg captured_at "$now_ts" \
    --arg mem "$mem" \
    --arg used "$used" \
    --arg free "$free" \
    --arg cpu "$cpu" \
    --arg temp "$temp" \
    --argjson containers "$containers" \
    --argjson container_total "$container_total" \
    '{captured_at:$captured_at,memory_percent:($mem|tonumber),memory_used_mib:($used|tonumber? // null),memory_free_mib:($free|tonumber? // null),cpu_percent:($cpu|tonumber? // null),cpu_temperature_c:($temp|tonumber? // null),container_total_bytes:$container_total,containers:$containers}' > "$out"
}

emit_event() {
  local type="$1" snapshot_file="$2" ts
  ts="$(date -u +%Y%m%dT%H%M%SZ)"
  jq --arg type "$type" '. + {event_type:$type}' "$snapshot_file" > "$EVENT_DIR/${ts}-${type}.json"
}

last_followup_epoch=0
while true; do
  tmp_snapshot="$(mktemp)"
  if snapshot "$tmp_snapshot"; then
    mem="$(jq -r '.memory_percent' "$tmp_snapshot")"
    state="normal"
    if [[ -f "$STATE_FILE" ]]; then
      state="$(jq -r '.state // "normal"' "$STATE_FILE" 2>/dev/null || echo normal)"
      last_followup_epoch="$(jq -r '.last_followup_epoch // 0' "$STATE_FILE" 2>/dev/null || echo 0)"
    fi

    is_high="$(awk -v v="$mem" -v t="$high" 'BEGIN{print (v>=t)?1:0}')"
    is_recovered="$(awk -v v="$mem" -v t="$recover" 'BEGIN{print (v<t)?1:0}')"
    now_epoch="$(date +%s)"

    if [[ "$is_high" == "1" && "$state" != "high" ]]; then
      emit_event high "$tmp_snapshot"
      jq -n --argjson e "$now_epoch" --argjson m "$mem" '{state:"high",last_followup_epoch:$e,last_memory_percent:$m}' > "$STATE_FILE"
      echo "Memory high detected: ${mem}%"
    elif [[ "$state" == "high" && "$is_recovered" == "1" ]]; then
      emit_event recovered "$tmp_snapshot"
      jq -n --argjson m "$mem" '{state:"normal",last_followup_epoch:0,last_memory_percent:$m}' > "$STATE_FILE"
      echo "Memory recovered: ${mem}%"
    elif [[ "$state" == "high" && $((now_epoch-last_followup_epoch)) -ge 900 ]]; then
      emit_event followup "$tmp_snapshot"
      jq -n --argjson e "$now_epoch" --argjson m "$mem" '{state:"high",last_followup_epoch:$e,last_memory_percent:$m}' > "$STATE_FILE"
      echo "Memory follow-up captured: ${mem}%"
    else
      jq -n --arg state "$state" --argjson e "$last_followup_epoch" --argjson m "$mem" '{state:$state,last_followup_epoch:$e,last_memory_percent:$m}' > "$STATE_FILE"
    fi
  fi
  rm -f "$tmp_snapshot"
  sleep "$interval"
done
