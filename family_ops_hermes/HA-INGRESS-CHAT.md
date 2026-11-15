# Hermes chat via Home Assistant ingress

**Symptom**: dashboard Chat shows \`[session ended]\`, and the Model & tools sidebar shows \`events feed disconnected\` while Hermes gateway reports running.

This is **not** proof that Codex OAuth failed. In pinned Hermes upstream \`v2026.5.7\`:
- \`hermes_cli/web_server.py\` rejects both \`/api/pty\` and \`/api/events\` unless \`HERMES_DASHBOARD_TUI=1\` (Home Assistant option \`enable_dashboard_tui: true\`).
- \`web/src/pages/ChatPage.tsx\` and \`web/src/components/ChatSidebar.tsx\` create root-absolute WebSocket paths, losing the Home Assistant ingress prefix.

This add-on injects a tiny local JS asset into the proxied dashboard HTML to rewrite **only** these two same-origin WebSocket URLs to \`<ingress-prefix>/dashboard/api/pty\` or \`.../api/events\`. Nginx already proxies \`/dashboard/api/\` to the upstream dashboard. It does **not** open any new external ports. The script is served exclusively inside HA ingress.

After deploying v2026.5.7-24, enable \`enable_dashboard_tui: true\` and restart **only** Hermes. Verify the dashboard WebSocket, embedded Chat, and events feed separately from actual Codex inference. Do not expose auth tokens or CLI model output in GitHub issues.

Important: upstream \`hermes -z\` in v2026.5.7 returns exit code 0 even if the agent response is empty, so a failed \`CODEX_READY\` smoke test does not by itself prove a token failure.

## v25 follow-up

Use a **document-relative** script URL `ha-ingress-websocket.js` instead of constructing the script URL from `X-Ingress-Path`. The script reads the browser URL prefix (through `/dashboard`) before the SPA loads and sets `window.__HERMES_BASE_PATH__`, so both ordinary dashboard REST calls and chat WebSockets can use the real HA ingress path. The previous v24 patch could fail silently when the script URL was formed from an incomplete ingress header.
