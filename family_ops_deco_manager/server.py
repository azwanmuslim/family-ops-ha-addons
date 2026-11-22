"""Home Assistant-only Deco configuration audit. No mutation endpoints or LAN port publishing."""
import hmac
import ipaddress
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

from deco_local.tmp import DecoClient

OPTIONS_PATH = Path("/data/options.json")
PORT = 18787
LOCK = threading.Lock()
CACHE = {}
CACHE_TTL = 45
SECTIONS = {
    "qos": ("qos_bandwidth", "dpi_qos", "network_quality"),
    "wifi": ("wifi_bands_info", "wifi_bands", "ieee_802_11r", "beamforming", "wireless_link"),
    "network": ("wan_lan_status", "operation_mode", "dns_servers", "dhcp_server", "upnp", "ipv6"),
    "mesh": ("device_list", "device_resource_info", "firmware"),
}
SECTIONS["all"] = tuple(dict.fromkeys(n for group in SECTIONS.values() for n in group))
REDACT_KEYS = {
    "password", "passwd", "pwd", "psk", "passphrase", "key",
    "private_key", "token", "stok", "session", "cookie", "ssid",
    "mac", "mac_addr", "mac_address", "bssid", "client_name",
    "client_mac", "device_name", "hostname", "email", "account", "serial",
    "serial_number", "wan_ip", "external_ip", "public_ip",
}


def scrub(obj):
    if isinstance(obj, dict):
        return {
            str(k): ("[redacted]" if str(k).lower() in REDACT_KEYS or
                     any(part in str(k).lower() for part in
                         ("password", "secret", "private_key", "auth_token", "credential"))
                     else scrub(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [scrub(v) for v in obj]
    return obj


def options():
    cfg = json.loads(OPTIONS_PATH.read_text(encoding="utf-8"))
    host = str(cfg.get("deco_host") or "").strip()
    ip = ipaddress.ip_address(host)
    if ip.version != 4 or not ip.is_private or ip.is_loopback or ip.is_link_local:
        raise ValueError("Deco host must be a private LAN IPv4 address")
    return {
        "host": host,
        "email": str(cfg.get("deco_email") or "").strip(),
        "password": str(cfg.get("deco_password") or ""),
        "api_key": str(cfg.get("api_key") or ""),
    }


def read_section(section, cfg):
    now = time.monotonic()
    with LOCK:
        if section in CACHE and now - CACHE[section]["at"] < CACHE_TTL:
            return dict(CACHE[section]["data"], cached=True)
        if not cfg["email"] or not cfg["password"]:
            return {"ok": False, "error": "TP-Link ID email/password not set in Home Assistant add-on Options"}
        try:
            with DecoClient(cfg["host"], cfg["email"], cfg["password"], timeout=8.0) as deco:
                raw = deco.snapshot(list(SECTIONS[section]), gap=0.8)
            cleaned = {}
            for name, item in raw.items():
                cleaned[name] = {
                    "ok": bool(item.get("ok")),
                    "ms": item.get("ms"),
                }
                if item.get("ok"):
                    cleaned[name]["result"] = scrub(item.get("result"))
                else:
                    cleaned[name]["error"] = str(item.get("error", "unsupported"))[:250]
            result = {
                "ok": True,
                "section": section,
                "read_only": True,
                "at_utc": datetime.now(timezone.utc).isoformat(),
                "data": cleaned,
            }
            CACHE[section] = {"at": time.monotonic(), "data": result}
            return result
        except Exception as exc:
            logging.warning("Deco read failed: %s", type(exc).__name__)
            return {"ok": False, "section": section, "error": type(exc).__name__,
                    "detail": "Connection/authentication failed; verify router and credentials locally"}


class Handler(BaseHTTPRequestHandler):
    server_version = "DecoManager/0.1"
    sys_version = ""

    def send_json(self, status, data):
        raw = json.dumps(data, default=str, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        try:
            cfg = options()
        except (ValueError, OSError, KeyError, json.JSONDecodeError):
            return self.send_json(503, {"ok": False, "error": "Add-on options unavailable"})
        supplied = self.headers.get("X-API-Key", "")
        if not cfg["api_key"] or not supplied or not hmac.compare_digest(cfg["api_key"], supplied):
            return self.send_json(403, {"ok": False, "error": "Forbidden"})
        url = urlsplit(self.path)
        if url.path == "/health":
            return self.send_json(200, {
                "ok": True, "read_only": True,
                "credentials_configured": bool(cfg["email"] and cfg["password"]),
                "sections": list(SECTIONS.keys()),
                "host": cfg["host"],
            })
        if url.path == "/audit":
            section = parse_qs(url.query).get("section", ["all"])[0]
            if section not in SECTIONS:
                return self.send_json(400, {"ok": False, "error": "Invalid audit section"})
            result = read_section(section, cfg)
            return self.send_json(200 if result.get("ok") else 503, result)
        return self.send_json(404, {"ok": False, "error": "Not found"})

    def do_POST(self):
        return self.send_json(405, {"ok": False, "error": "Writes disabled"})

    def log_message(self, fmt, *args):
        # No request headers, credentials or router response are ever logged.
        logging.info("request %s", self.command)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    os.makedirs("/data/deco-local-state", exist_ok=True)
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    logging.info("Deco Manager API listening on internal Docker network, read-only")
    server.serve_forever()
