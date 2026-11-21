"""Private gate shape benchmark: no online model switch, no gate actuation."""
import io
import json
import tempfile
import time
import unittest
from pathlib import Path

from PIL import Image, ImageDraw
from ha_gate_dataset import GateDataset
from ha_gate_structure import GateShapeProbe, _extract_all, _sessions, MODELS


def frame(open_state):
    image = Image.new("RGB", (640, 360), (145, 161, 174))
    draw = ImageDraw.Draw(image)
    draw.rectangle((10, 110, 630, 250), fill=(184, 194, 194))
    if open_state:
        for x in (110, 130, 150, 170, 190, 500, 520, 540):
            draw.rectangle((x, 135, x + 5, 220), fill=(36, 39, 44))
    else:
        for x in range(115, 545, 20):
            draw.rectangle((x, 135, x + 5, 220), fill=(36, 39, 44))
    out = io.BytesIO()
    image.save(out, format="JPEG", quality=88)
    return out.getvalue()


class ShapeProbeTests(unittest.TestCase):
    def test_extract_three_local_features(self):
        features = _extract_all(frame(True))
        self.assertEqual(set(features), set(MODELS))
        self.assertTrue(all(len(v) >= 64 for v in features.values()))
        self.assertTrue(all(all(0 <= x <= 1 for x in v)
                            for v in features.values()))

    def test_distinct_events_not_snapshot_count(self):
        rows = [{"timestamp": x} for x in (0, 120, 180, 1200, 1240, 2700)]
        groups = _sessions(rows)
        self.assertEqual([len(group) for group in groups], [3, 2, 1])

    def test_same_image_in_conflicting_classes_never_promotable(self):
        with tempfile.TemporaryDirectory() as folder:
            store = GateDataset(enabled=True, root=folder)
            photo = frame(False)
            for _ in range(8):
                store.save(photo, label="OPEN", source="owner_confirmed")
                store.save(photo, label="CLOSED", source="owner_confirmed")
            report = store.shape_benchmark()
            self.assertEqual(report["open_events"], 1)
            self.assertEqual(report["closed_events"], 1)
            self.assertFalse(report["ready_to_promote"])
            self.assertTrue(report["is_dry_run"])
            self.assertEqual(store.stats()["samples"], 16)
            self.assertEqual(store.shape_benchmark(), report)

    def test_distinct_event_holdout_and_mask_candidates(self):
        # Events are separated by >15 minutes; holdout is by event, not frame.
        with tempfile.TemporaryDirectory() as folder:
            store = GateDataset(enabled=True, root=folder)
            now = time.time()
            for i in range(9):
                for label, opened in (("OPEN", True), ("CLOSED", False)):
                    sid = store.save(frame(opened), label=label,
                                     source="owner_confirmed", return_id=True)
                    meta = Path(folder) / (sid + ".json")
                    row = json.loads(meta.read_text())
                    row["timestamp"] = now - (9 - i) * 3600
                    meta.write_text(json.dumps(row))
            report = store.shape_benchmark()
            self.assertEqual(report["open_events"], 9)
            self.assertEqual(report["closed_events"], 9)
            self.assertEqual(report["split"], "event_holdout")
            self.assertGreaterEqual(report["tested"], 4)
            self.assertIn("mask_gray", report["candidate_scores"])
            self.assertIn("mask_vertical", report["candidate_scores"])
            self.assertTrue(report["is_dry_run"])
            self.assertEqual(store.stats()["samples"], 18)

    def test_disabled_never_reads_or_creates_data(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "missing"
            store = GateDataset(enabled=False, root=root)
            report = store.shape_benchmark()
            self.assertEqual(report["status"], "DISABLED")
            self.assertFalse(root.exists())

    def test_private_benchmark_does_not_mutate_confirmed_labels(self):
        with tempfile.TemporaryDirectory() as folder:
            store = GateDataset(enabled=True, root=folder)
            for _ in range(8):
                store.save(frame(True), label="OPEN", source="owner_confirmed")
                store.save(frame(False), label="CLOSED", source="owner_confirmed")
            metadata = {p.name: p.read_bytes() for p in Path(folder).glob("*.json")}
            report = GateShapeProbe(folder).evaluate(
                [row for _, row in store.entries()])
            self.assertTrue(report["is_dry_run"])
            self.assertEqual(metadata,
                             {p.name: p.read_bytes() for p in Path(folder).glob("*.json")})
            self.assertFalse(any(p.suffix == ".model" for p in Path(folder).iterdir()))


if __name__ == "__main__":
    unittest.main()
