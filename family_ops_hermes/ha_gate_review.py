#!/usr/bin/env python3
"""Private HA ingress Gate Vision capture and owner-reviewed labelling.

On-demand snapshots use one fixed HA camera and local cropped storage only.
No public host port, GPS, switch, cover or motor calls.
Supervisor ingress authenticates; CSRF protects state-changing POSTs.
"""
import asyncio
import hmac
import json
import os
from pathlib import Path
import secrets
import time

from aiohttp import ClientSession, ClientTimeout, web
from ha_gate_dataset import CAMERA, GateDataset

PORT = 9131
HTML = Path("/usr/share/nginx/html/gate-review.html")
API = "http://supervisor/core/api"
CAPTURE_COOLDOWN_SECONDS = 3


async def capture_from_ha():
    """Get a fresh frame only from the configured local HA camera."""
    token = os.environ.get("SUPERVISOR_TOKEN", "")
    if not token:
        raise RuntimeError("supervisor_token_unavailable")
    auth = {"Authorization": "Bearer " + token}
    frame_headers = {**auth, "Cache-Control": "no-cache", "Pragma": "no-cache"}
    async with ClientSession(timeout=ClientTimeout(total=26)) as session:
        async with session.get(
            API + "/camera_proxy/" + CAMERA, headers=frame_headers,
            params={"_": str(time.time_ns())},
        ) as response:
            if response.status != 200:
                raise RuntimeError("camera_capture_failed")
            if response.content_length is not None and response.content_length > 6_000_000:
                raise ValueError("camera_image_too_large")
            jpeg = await response.read()
        # Optional metadata, never a prerequisite for a short-lived OPEN event.
        sun = "unknown"
        try:
            async with session.get(API + "/states/sun.sun", headers=auth, timeout=3) as response:
                if response.status == 200:
                    state = await response.json()
                    sun = str(state.get("state", "unknown"))[:24]
        except Exception:
            pass
    return jpeg, sun


def create_app(dataset=None, capture_frame=None):
    dataset = dataset if dataset is not None else GateDataset()
    csrf = secrets.token_urlsafe(32)
    app = web.Application(client_max_size=2048)
    app["dataset"] = dataset
    app["capture_frame"] = capture_frame if capture_frame is not None else capture_from_ha
    app["last_manual_capture"] = None
    app["capture_lock"] = asyncio.Lock()

    async def page(request):
        nonce = secrets.token_urlsafe(20)
        source = HTML.read_text(encoding="utf-8")
        body = source.replace("__CSP_NONCE__", nonce)
        response = web.Response(text=body, content_type="text/html", charset="utf-8")
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; "
            "script-src 'nonce-" + nonce + "'; connect-src 'self'; "
            "base-uri 'none'; form-action 'none'; frame-ancestors 'self'"
        )
        return response

    async def listing(request):
        stats = dataset.stats()
        return web.json_response(
            {
                "enabled": dataset.enabled,
                "counts": {
                    "pending": stats["unverified"],
                    "open": stats["open"],
                    "closed": stats["closed"],
                    "legacy": stats.get("legacy_samples", 0),
                },
                "samples": dataset.review_queue(120),
                "csrf": csrf,
                "scope": "manual_labels_only",
            },
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )

    async def photo(request):
        sid = request.match_info["sample_id"]
        try:
            jpeg = dataset.review_photo(sid)
        except (ValueError, OSError):
            raise web.HTTPNotFound(text="Gambar tiada atau sudah disahkan") from None
        return web.Response(body=jpeg, content_type="image/jpeg", headers={
            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
        })

    async def label(request):
        provided = request.headers.get("X-Gate-Review-CSRF", "")
        if not hmac.compare_digest(provided, csrf):
            raise web.HTTPForbidden(text="Sesi tidak sah")
        if request.content_type != "application/json":
            raise web.HTTPUnsupportedMediaType()
        try:
            payload = await request.json()
        except (ValueError, json.JSONDecodeError):
            raise web.HTTPBadRequest(text="JSON tidak sah") from None
        if not isinstance(payload, dict) or set(payload) != {"id", "label"}:
            raise web.HTTPBadRequest(text="Permintaan tidak sah")
        sid, value = payload["id"], payload["label"]
        if not isinstance(sid, str) or value not in ("OPEN", "CLOSED"):
            raise web.HTTPBadRequest(text="Label mesti OPEN atau CLOSED")
        async with app["label_lock"]:
            try:
                dataset.confirm_existing(sid, value)
            except ValueError:
                raise web.HTTPConflict(text="Sampel sudah disahkan, luput, atau crop lama") from None
        return web.json_response({"ok": True, "label": value},
                                 headers={"Cache-Control": "no-store"})

    async def capture(request):
        if not hmac.compare_digest(request.headers.get("X-Gate-Review-CSRF", ""), csrf):
            raise web.HTTPForbidden(text="Sesi tidak sah")
        if not dataset.enabled:
            return web.json_response({"error": "Gate learning belum diaktifkan."}, status=503)
        if request.content_length not in (None, 0):
            raise web.HTTPBadRequest(text="Snapshot tidak memerlukan input")
        async with app["capture_lock"]:
            last = app["last_manual_capture"]
            if last is not None and time.monotonic() - last < CAPTURE_COOLDOWN_SECONDS:
                return web.json_response(
                    {"error": "Terlalu cepat. Cuba semula beberapa saat lagi."},
                    status=429, headers={"Cache-Control": "no-store"})
            try:
                jpeg, sun = await app["capture_frame"]()
                sample_id = dataset.save(
                    jpeg, source="manual_request", sun=sun, return_id=True)
                if not isinstance(sample_id, str):
                    raise RuntimeError("dataset_disabled")
            except Exception:
                dataset.errors += 1
                return web.json_response(
                    {"error": "Snapshot CCTV gagal. Tiada sampel disimpan."},
                    status=503, headers={"Cache-Control": "no-store"})
            app["last_manual_capture"] = time.monotonic()
        return web.json_response(
            {"ok": True, "id": sample_id, "verified": False},
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    app["label_lock"] = asyncio.Lock()
    app.router.add_get("/", page)
    app.router.add_get("/api/list", listing)
    app.router.add_get("/api/photo/{sample_id}", photo)
    app.router.add_post("/api/label", label)
    app.router.add_post("/api/capture", capture)
    return app


if __name__ == "__main__":
    if os.environ.get("HA_GATE_DATASET", "false").lower() != "true":
        raise SystemExit("Gate dataset option disabled: no review server started")
    web.run_app(create_app(), host="127.0.0.1", port=PORT,
                access_log=None, print=None)
