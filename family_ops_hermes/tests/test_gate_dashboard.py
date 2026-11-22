"""Gate dashboard exports safe stats only, never imagery, GPS or actuator calls."""
import asyncio
import io
import json
import tempfile
import unittest

from ha_gate_advisor import GateAdvisor
from ha_gate_dataset import GateDataset


def test_jpeg():
    from PIL import Image
    image = Image.new("RGB", (640, 360), color=(100, 130, 100))
    out = io.BytesIO()
    image.save(out, "JPEG")
    return out.getvalue()


class FakeResponse:
    status = 201
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        return False
    async def read(self):
        return b""


class FakeSession:
    def __init__(self):
        self.sent = []
    def post(self, path, **kwargs):
        self.sent.append((path, kwargs["json"]))
        return FakeResponse()


class GateDashboardTests(unittest.IsolatedAsyncioTestCase):
    async def test_progress_and_virtual_sensor_states(self):
        with tempfile.TemporaryDirectory() as td:
            g = GateAdvisor(lambda *_: None)
            g.dataset = GateDataset(enabled=True, root=td)
            g.latest = "UNKNOWN"
            v = g.dashboard_states()
            self.assertEqual(v["sensor.hermes_gate_learning"]["state"], "COLLECTING")
            self.assertEqual(v["sensor.hermes_gate_training_progress"]["state"], "0")
            self.assertEqual(v["sensor.hermes_gate_evaluation"]["state"], "unknown")
            self.assertEqual(v["sensor.hermes_gate_shape_accuracy"]["state"], "unknown")
            self.assertEqual(v["sensor.hermes_gate_shadow_visual"]["state"], "UNKNOWN")
            self.assertEqual(v["sensor.hermes_gate_shadow_validation"]["state"], "unknown")
            self.assertFalse(v["sensor.hermes_gate_shadow_visual"]["attributes"]["controls_actuator"])
            self.assertTrue(v["sensor.hermes_gate_shape_accuracy"]["attributes"]["dry_run_only"])
            self.assertEqual(v["sensor.hermes_gate_detection_reason"]["state"], "NOT_YET_CHECKED")
            self.assertEqual(v["sensor.hermes_gate_visual"]["state"], "UNKNOWN")
            g.dataset.save(test_jpeg(), label="OPEN", source="owner_confirmed")
            g.dataset.save(test_jpeg(), label="CLOSED", source="owner_confirmed")
            v = g.dashboard_states()
            self.assertEqual(v["sensor.hermes_gate_training_progress"]["state"], "17")
            self.assertEqual(v["sensor.hermes_gate_open_verified"]["state"], "1")
            self.assertEqual(v["sensor.hermes_gate_closed_verified"]["state"], "1")
            self.assertNotEqual(v["sensor.hermes_gate_last_capture"]["state"], "unknown")
            snapshot = json.dumps(v).lower()
            for forbidden in ("latitude", "longitude", "token", "camera_proxy", "jpg", "base64", "switch.bothgate"):
                self.assertNotIn(forbidden, snapshot)

    async def test_presence_called_from_running_instance(self):
        # Regression: advisor.check_once invokes self._presence(states).
        g = GateAdvisor(lambda *_: None)
        states = {"person.a": {"state": "not_home"},
                  "person.b": {"state": "not_home"},
                  "group.family": {"state": "not_home"}}
        self.assertTrue(g._presence(states))
        states["person.b"]["state"] = "unknown"
        self.assertFalse(g._presence(states))

    async def test_posts_only_when_changed_or_due(self):
        with tempfile.TemporaryDirectory() as td:
            g = GateAdvisor(lambda *_: None)
            g.dataset = GateDataset(enabled=True, root=td)
            s = FakeSession()
            await g.publish_dashboard(s)
            self.assertEqual(len(s.sent), 14)
            await g.publish_dashboard(s)
            self.assertEqual(len(s.sent), 14)
            g.latest = "OPEN"
            await g.publish_dashboard(s, 0.1)
            self.assertEqual(len(s.sent), 28)
            self.assertTrue(all("/states/sensor.hermes_gate_" in path for path, _ in s.sent))
            self.assertFalse(any("/services/" in path for path, _ in s.sent))

    async def test_dataset_capture_time_survives_restart(self):
        with tempfile.TemporaryDirectory() as td:
            a = GateDataset(enabled=True, root=td)
            a.save(test_jpeg())
            b = GateDataset(enabled=True, root=td)
            self.assertIsNotNone(b.stats()["last_sample_utc"])
