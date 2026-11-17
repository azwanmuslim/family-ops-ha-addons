#!/usr/bin/env python3
"""Admin HA ingress-only Gate Vision manual labelling of EXISTING crop files.

No public host port, no camera access, no GPS, no switch/cover/motor calls.
HA Supervisor authenticates the ingress; CSRF protects state-changing POSTs.
"""
import hmac
import json
import os
from pathlib import Path
import secrets

from aiohttp import web
from ha_gate_dataset import GateDataset

PORT = 9131
HTML = Path("/usr/share/nginx/html/gate-review.html")


def create_app(dataset=None):
    dataset = dataset if dataset is not None else GateDataset()
    csrf = secrets.token_urlsafe(32)
    app = web.Application(client_max_size=2048)
    app["dataset"] = dataset

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

    import asyncio
    app["label_lock"] = asyncio.Lock()
    app.router.add_get("/", page)
    app.router.add_get("/api/list", listing)
    app.router.add_get("/api/photo/{sample_id}", photo)
    app.router.add_post("/api/label", label)
    return app


if __name__ == "__main__":
    if os.environ.get("HA_GATE_DATASET", "false").lower() != "true":
        raise SystemExit("Gate dataset option disabled: no review server started")
    web.run_app(create_app(), host="127.0.0.1", port=PORT,
                access_log=None, print=None)
