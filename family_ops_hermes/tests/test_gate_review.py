"""Local ingress gate image review API tests (no real CCTV or actuator)."""
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp.test_utils import TestServer, TestClient
from ha_gate_dataset import GateDataset, ROI
from ha_gate_review import create_app


def jpeg():
    from PIL import Image
    image = Image.new("RGB", (640, 360), (110, 120, 130))
    out = io.BytesIO()
    image.save(out, "JPEG")
    return out.getvalue()


class GateReviewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.dataset = GateDataset(enabled=True, root=self.temp.name)
        self.dataset.save(jpeg(), label="UNKNOWN", source="periodic")
        self.sample = self.dataset.review_queue()[0]["id"]
        self.capturer = {"fail": False}
        async def fake_capture():
            if self.capturer["fail"]:
                raise RuntimeError("camera_offline")
            return jpeg(), "above_horizon"
        self.server = TestServer(create_app(self.dataset, capture_frame=fake_capture))
        self.client = TestClient(self.server)
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.temp.cleanup()

    async def test_list_photo_and_manual_label(self):
        response = await self.client.get("/api/list")
        self.assertEqual(response.status, 200)
        data = await response.json()
        self.assertEqual(data["counts"]["pending"], 1)
        self.assertEqual(data["samples"][0]["id"], self.sample)
        self.assertEqual(data["scope"], "manual_labels_only")
        self.assertNotIn("vector", json.dumps(data))
        picture = await self.client.get("/api/photo/" + self.sample)
        self.assertEqual(picture.status, 200)
        self.assertTrue((await picture.read()).startswith(b"\xff\xd8"))

        body = {"id": self.sample, "label": "OPEN"}
        denied = await self.client.post("/api/label", json=body)
        self.assertEqual(denied.status, 403)
        good = await self.client.post("/api/label", json=body,
            headers={"X-Gate-Review-CSRF": data["csrf"]})
        self.assertEqual(good.status, 200)
        record = json.loads((Path(self.temp.name) / (self.sample + ".json")).read_text())
        self.assertEqual(record["label"], "OPEN")
        self.assertTrue(record["verified"])
        self.assertEqual(record["source"], "owner_review")
        self.assertEqual(record["roi"], list(ROI))
        self.assertEqual(self.dataset.stats()["open"], 1)
        old = await self.client.get("/api/photo/" + self.sample)
        self.assertEqual(old.status, 404)
        second = await self.client.post("/api/label", json=body,
            headers={"X-Gate-Review-CSRF": data["csrf"]})
        self.assertEqual(second.status, 409)

    async def test_review_correction_and_stale_label(self):
        token = (await (await self.client.get("/api/list")).json())["csrf"]
        headers = {"X-Gate-Review-CSRF": token}
        await self.client.post("/api/label", json={"id": self.sample, "label": "OPEN"}, headers=headers)
        listing = await (await self.client.get("/api/list?mode=OPEN")).json()
        self.assertEqual(listing["samples"][0]["label"], "OPEN")
        self.assertEqual((await self.client.get("/api/photo/" + self.sample + "?reviewed=1")).status, 200)
        payload = {"id": self.sample, "label": "CLOSED", "expected": "OPEN"}
        self.assertEqual((await self.client.post("/api/label", json=payload, headers=headers)).status, 200)
        self.assertEqual(self.dataset.stats()["closed"], 1)
        self.assertTrue((Path(self.temp.name) / ".label_revision").exists())
        self.assertEqual((await self.client.post("/api/label", json=payload, headers=headers)).status, 409)
        self.assertEqual((await self.client.post("/api/label", json={"id": self.sample, "label": "UNKNOWN", "expected": "CLOSED"}, headers=headers)).status, 200)
        self.assertEqual(self.dataset.stats()["unverified"], 1)

    async def test_on_demand_capture_creates_unverified_image_then_labels(self):
        token = (await (await self.client.get("/api/list")).json())["csrf"]
        denied = await self.client.post("/api/capture")
        self.assertEqual(denied.status, 403)
        malformed = await self.client.post("/api/capture", data=b"unexpected",
            headers={"X-Gate-Review-CSRF": token})
        self.assertEqual(malformed.status, 400)
        saved = await self.client.post("/api/capture",
            headers={"X-Gate-Review-CSRF": token})
        self.assertEqual(saved.status, 200)
        data = await saved.json()
        self.assertFalse(data["verified"])
        self.assertNotEqual(data["id"], self.sample)
        record = json.loads((Path(self.temp.name) / (data["id"] + ".json")).read_text())
        self.assertEqual(record["label"], "UNKNOWN")
        self.assertFalse(record["verified"])
        self.assertEqual(record["source"], "manual_request")
        self.assertEqual(record["roi"], list(ROI))
        self.assertIsNone(self.dataset.last_routine)
        self.assertEqual(self.dataset.stats()["unverified"], 2)
        image = await self.client.get("/api/photo/" + data["id"])
        self.assertEqual(image.status, 200)
        labelled = await self.client.post("/api/label",
            json={"id": data["id"], "label": "OPEN"},
            headers={"X-Gate-Review-CSRF": token})
        self.assertEqual(labelled.status, 200)
        self.assertEqual(self.dataset.stats()["open"], 1)

    async def test_capture_cooldown_and_camera_failure(self):
        token = (await (await self.client.get("/api/list")).json())["csrf"]
        self.capturer["fail"] = True
        failed = await self.client.post("/api/capture",
            headers={"X-Gate-Review-CSRF": token})
        self.assertEqual(failed.status, 503)
        self.assertEqual(self.dataset.stats()["unverified"], 1)
        self.capturer["fail"] = False
        saved = await self.client.post("/api/capture",
            headers={"X-Gate-Review-CSRF": token})
        self.assertEqual(saved.status, 200)
        throttled = await self.client.post("/api/capture",
            headers={"X-Gate-Review-CSRF": token})
        self.assertEqual(throttled.status, 429)
        self.assertEqual(self.dataset.stats()["unverified"], 2)

    async def test_reject_invalid_labels_and_path(self):
        data = await (await self.client.get("/api/list")).json()
        for bad in ("SKIP", "OPEN_GATE", "turn_on", "closed", True, None):
            resp = await self.client.post("/api/label",
                json={"id": self.sample, "label": bad},
                headers={"X-Gate-Review-CSRF": data["csrf"]})
            self.assertEqual(resp.status, 400)
        resp = await self.client.get("/api/photo/invalid")
        self.assertEqual(resp.status, 404)
        self.assertEqual(self.dataset.stats()["unverified"], 1)

    async def test_old_roi_is_excluded_not_mislabeled(self):
        meta = Path(self.temp.name) / (self.sample + ".json")
        row = json.loads(meta.read_text())
        row["roi"] = [.3, .72, .74, .995]
        meta.write_text(json.dumps(row))
        response = await self.client.get("/api/list")
        data = await response.json()
        self.assertEqual(data["counts"]["legacy"], 1)
        self.assertEqual(data["counts"]["pending"], 0)
        self.assertEqual(data["samples"], [])
        response = await self.client.post("/api/label",
            json={"id": self.sample, "label": "CLOSED"},
            headers={"X-Gate-Review-CSRF": data["csrf"]})
        self.assertEqual(response.status, 409)
        self.assertEqual(self.dataset.classify(jpeg()), ("UNKNOWN", 0.0))

    async def test_get_html_has_nocache_and_csp(self):
        with tempfile.TemporaryDirectory() as td:
            html = Path(td) / "view.html"
            html.write_text("<script nonce='__CSP_NONCE__'></script>")
            with patch("ha_gate_review.HTML", html):
                response = await self.client.get("/")
                self.assertEqual(response.status, 200)
                self.assertIn("no-store", response.headers["Cache-Control"])
                self.assertIn("script-src 'nonce-", response.headers["Content-Security-Policy"])
                text = await response.text()
                self.assertNotIn("__CSP_NONCE__", text)
