"""Tests for opt-in local gate learning; no HA calls or actuator writes."""
import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from ha_gate_dataset import GateDataset, feature, ROI, similarity


def jpeg():
    from PIL import Image
    image = Image.new("RGB", (640, 360), (70, 80, 90))
    out = io.BytesIO()
    image.save(out, format="JPEG")
    return out.getvalue()


class DatasetTests(unittest.TestCase):
    def test_disabled_never_writes(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "store"
            store = GateDataset(enabled=False, root=path)
            self.assertFalse(store.due())
            self.assertFalse(store.capture_routine(jpeg(), "above_horizon"))
            self.assertFalse(path.exists())
            self.assertEqual(store.classify(jpeg()), ("UNKNOWN", 0.0))

    def test_crop_includes_full_gate_not_right_hand_fence_only(self):
        # Normalized X must include the left gate leaf as seen in owner screenshots,
        # and reach the right gate post; keep a safe Y band around the gate.
        self.assertLessEqual(ROI[0], 0.20)
        self.assertGreaterEqual(ROI[1], 0.85)
        self.assertLessEqual(ROI[2], 0.36)
        self.assertGreaterEqual(ROI[3], 0.84)

    def test_crop_not_full_camera_and_small(self):
        crop, vector = feature(jpeg())
        self.assertEqual(len(vector), 256)
        from PIL import Image
        with Image.open(io.BytesIO(crop)) as image:
            self.assertLess(image.width, 640)
            self.assertLess(image.height, 360)
            self.assertGreater(image.width, 100)
        self.assertLess(len(crop), len(jpeg()) * 3)
        self.assertLess(ROI[2], ROI[3])

    def test_routine_has_unknown_label_and_throttle(self):
        with tempfile.TemporaryDirectory() as temp:
            store = GateDataset(enabled=True, root=temp)
            self.assertTrue(store.capture_routine(jpeg(), "above_horizon"))
            self.assertFalse(store.capture_routine(jpeg(), "above_horizon"))
            stats = store.stats()
            self.assertEqual(stats["samples"], 1)
            self.assertEqual(stats["unverified"], 1)
            self.assertEqual(stats["open"], 0)
            self.assertEqual(stats["closed"], 0)
            self.assertIn("OPEN", store.summary())
            self.assertEqual(store.classify(jpeg()), ("UNKNOWN", 0.0))
            photos = list(Path(temp).glob("*.jpg"))
            self.assertEqual(len(photos), 1)
            self.assertEqual(photos[0].stat().st_mode & 0o777, 0o600)
            metas = list(Path(temp).glob("*.json"))
            self.assertFalse(json.loads(metas[0].read_text())["verified"])

    def test_owner_verified_distinct_from_unverified(self):
        with tempfile.TemporaryDirectory() as temp:
            store = GateDataset(enabled=True, root=temp)
            store.save(jpeg(), label="OPEN", source="owner_confirmed")
            stats = store.stats()
            self.assertEqual(stats["open"], 1)
            self.assertEqual(stats["unverified"], 0)
            self.assertEqual(store.classify(jpeg()), ("UNKNOWN", 0.0))

    def test_retention_evicts_old_image_and_meta(self):
        with tempfile.TemporaryDirectory() as temp:
            store = GateDataset(enabled=True, root=temp)
            store.save(jpeg(), label="CLOSED", source="owner_confirmed")
            meta = next(Path(temp).glob("*.json"))
            record = json.loads(meta.read_text())
            record["timestamp"] = time.time() - (16 * 86400)
            meta.write_text(json.dumps(record))
            store.prune()
            self.assertEqual(store.stats()["samples"], 0)
            self.assertEqual(list(Path(temp).glob("*.jpg")), [])

    def test_invalid_jpeg_does_not_create_sample(self):
        with tempfile.TemporaryDirectory() as temp:
            store = GateDataset(enabled=True, root=temp)
            self.assertFalse(store.capture_routine(b"invalid", "night"))
            self.assertEqual(store.stats()["samples"], 0)

    def test_scores_are_not_labels(self):
        a = [1]*128 + [200]*128
        b = [1]*128 + [200]*128
        self.assertGreater(similarity(a, b), 0.99)

    def test_no_actuation_or_external_export(self):
        import inspect
        source = inspect.getsource(GateDataset)
        for word in ("/services/switch", "switch.bothgate", "turn_on", "turn_off",
                     "github.com", "api.telegram.org"):
            self.assertNotIn(word, source)


if __name__ == "__main__":
    unittest.main()
