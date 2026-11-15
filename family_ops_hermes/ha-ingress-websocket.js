/*
 * Home Assistant ingress bridge for Hermes v2026.5.7's embedded Chat UI.
 *
 * The bundled ChatPage/ChatSidebar generate root-absolute WebSocket paths
 * /api/pty and /api/events even when Hermes is hosted at a reverse proxy
 * prefix. Only intercept these two same-origin Hermes chat WebSockets.
 * Everything else (including Telegram, MQTT, HA state, TLS) is untouched.
 */
(() => {
  "use strict";
  if (window.__HERMES_HA_INGRESS_WS_FIXED__) return;
  const NativeWebSocket = window.WebSocket;
  if (typeof NativeWebSocket !== "function") return;

  class HermesIngressWebSocket extends NativeWebSocket {
    constructor(url, protocols) {
      const value = new URL(String(url), window.location.href);
      const baseFromUrl = window.location.pathname.match(/^(.*\/dashboard)(?:\/|$)/);
      const base = String(window.__HERMES_BASE_PATH__ || (baseFromUrl && baseFromUrl[1]) || "").replace(/\/+$/, "");
      const rootChatPath = value.pathname === "/api/pty" || value.pathname === "/api/events";
      const sameHost = value.host === window.location.host;

      if (base && rootChatPath && sameHost) {
        value.pathname = base + value.pathname;
      }
      if (protocols === undefined) super(value.toString());
      else super(value.toString(), protocols);
    }
  }
  window.WebSocket = HermesIngressWebSocket;
  window.__HERMES_HA_INGRESS_WS_FIXED__ = true;
})();
