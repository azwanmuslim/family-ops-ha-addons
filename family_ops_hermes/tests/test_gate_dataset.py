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
            restarted = GateDataset(enabled=True, root=temp)
            self.assertFalse(restarted.capture_routine(jpeg(), "above_horizon"))
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

    def test_manual_snapshot_is_unknown_and_does_not_reset_periodic_timer(self):
        with tempfile.TemporaryDirectory() as temp:
            store = GateDataset(enabled=True, root=temp)
            sid = store.save(jpeg(), source="manual_request",
                             sun="above_horizon", return_id=True)
            self.assertRegex(sid, r"^[0-9]{8}T[0-9]{6}-[a-f0-9]{8}$")
            self.assertEqual(store.review_queue()[0]["id"], sid)
            self.assertTrue(store.due())
            self.assertEqual(store.stats()["unverified"], 1)
            self.assertEqual(store.stats()["open"], 0)
            self.assertEqual(store.stats()["closed"], 0)

    def test_adaptive_capture_stops_and_resumes_on_exact_candidate_recall(self):
        with tempfile.TemporaryDirectory() as temp:
            store = GateDataset(enabled=True, root=temp)
            good = {"status": "CANDIDATE_PASSED", "split": "event_holdout",
                    "model": "edge_gate", "candidate_scores": {
                        "mask_both": {"open_recall_pct": 90, "closed_recall_pct": 90}}}
            bad = dict(good, candidate_scores={
                "mask_both": {"open_recall_pct": 100, "closed_recall_pct": 81.2}})
            with patch.object(store, "shape_benchmark", return_value=good):
                self.assertEqual(store.capture_policy("above_horizon"), "ACCURACY_OK")
                self.assertFalse(store.capture_routine(jpeg(), "above_horizon"))
            with patch.object(store, "shape_benchmark", return_value=bad):
                self.assertEqual(store.capture_policy("above_horizon"), "COLLECTING")
                self.assertTrue(store.capture_routine(jpeg(), "above_horizon"))
                self.assertFalse(store.due("above_horizon"))
            with patch.object(store, "night_benchmark", return_value=good):
                self.assertEqual(store.capture_policy("below_horizon"), "ACCURACY_OK")
            self.assertEqual(store.capture_policy("unknown"), "LIGHT_UNKNOWN")

    def test_pending_cap_never_blocks_manual_capture(self):
        with tempfile.TemporaryDirectory() as temp:
            store = GateDataset(enabled=True, root=temp)
            with patch("ha_gate_dataset.MAX_PENDING_PERIODIC", 1):
                self.assertTrue(store.capture_routine(jpeg(), "above_horizon"))
                self.assertEqual(store.capture_policy("above_horizon"), "REVIEW_REQUIRED")
                self.assertFalse(store.due("above_horizon"))
                self.assertTrue(store.save(jpeg(), source="manual_request", sun="above_horizon"))
                row = next(r for _, r in store.entries() if r["source"] == "periodic")
                store.confirm_existing(row["id"], "CLOSED")
                self.assertEqual(store.capture_policy("above_horizon"), "COLLECTING")

    def test_review_prioritizes_shadow_evidence_without_prediction_leak(self):
        with tempfile.TemporaryDirectory() as temp:
            store = GateDataset(enabled=True, root=temp)
            with patch.object(store, "shadow_predict_crop", return_value={
                    "prediction": "OPEN", "model_id": "abcdef123456"}):
                eligible = store.save(jpeg(), sun="above_horizon", return_id=True)
            newer = store.save(jpeg(), sun="below_horizon", return_id=True)
            with patch.object(store, "shadow_stats", return_value={"model_id": "abcdef123456"}):
                queue = store.review_queue()
                self.assertEqual(queue[0]["id"], eligible)
                self.assertTrue(queue[0]["shadow_evidence"])
                self.assertEqual(queue[1]["id"], newer)
                self.assertFalse(queue[1]["shadow_evidence"])
                self.assertNotIn("shadow_prediction", queue[0])
                self.assertNotIn("shadow_model_id", queue[0])
                self.assertEqual(store.review_queue(1)[0]["id"], newer)

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

    def test_holdout_is_not_sample_percentage(self):
        # Identical contradictory labels must FAIL, despite 100% labelled images.
        with tempfile.TemporaryDirectory() as temp:
            store = GateDataset(enabled=True, root=temp)
            for _ in range(8):
                store.save(jpeg(), label="OPEN", source="owner_confirmed")
                store.save(jpeg(), label="CLOSED", source="owner_confirmed")
            report = store.evaluate()
            self.assertEqual(report["status"], "EVALUATION_FAILED")
            self.assertLess(report["accuracy_pct"], 90)
            self.assertEqual(store.classify(jpeg())[0], "UNKNOWN")
            self.assertEqual(store.classify_diagnostic(jpeg())[2]["reason"],
                             "HOLDOUT_ACCURACY_LOW")

    def test_balanced_holdout_and_legacy_crop_reuse(self):
        from PIL import Image, ImageDraw
        def frame(open_state):
            image = Image.new("RGB", (640, 360), (185, 200, 210))
            draw = ImageDraw.Draw(image)
            # High-contrast gate shape in upper crop band; different by class.
            if open_state:
                for x in range(120, 520, 32):
                    draw.rectangle((x, 130, x+7, 193), fill=(25, 25, 25))
            else:
                for y in range(135, 195, 12):
                    draw.rectangle((110, y, 548, y+6), fill=(25, 25, 25))
            out = io.BytesIO()
            image.save(out, "JPEG", quality=92)
            return out.getvalue()
        with tempfile.TemporaryDirectory() as temp:
            store = GateDataset(enabled=True, root=temp)
            opened = frame(True)
            closed = frame(False)
            for _ in range(8):
                store.save(opened, label="OPEN", source="owner_confirmed")
                store.save(closed, label="CLOSED", source="owner_confirmed")
            report = store.evaluate()
            self.assertEqual(report["status"], "READY_POC")
            self.assertEqual(report["accuracy_pct"], 100.0)
            self.assertGreaterEqual(report["open_evaluated"], 2)
            self.assertEqual(store.classify(opened)[0], "OPEN")
            self.assertEqual(store.classify(closed)[0], "CLOSED")
            self.assertEqual(store.stats()["samples"], 16)

    def test_no_actuation_or_external_export(self):
        import inspect
        source = inspect.getsource(GateDataset)
        for word in ("/services/switch", "switch.bothgate", "turn_on", "turn_off",
                     "github.com", "api.telegram.org"):
            self.assertNotIn(word, source)



class OwnerConfirmTests(unittest.IsolatedAsyncioTestCase):
    async def test_confirmed_capture_preserves_sun_and_prelabel_shadow(self):
        from unittest.mock import AsyncMock, MagicMock
        with tempfile.TemporaryDirectory() as temp:
            store = GateDataset(enabled=True, root=temp)
            camera = MagicMock()
            camera.status = 200
            camera.read = AsyncMock(return_value=jpeg())
            sun = MagicMock()
            sun.status = 200
            sun.json = AsyncMock(return_value={"state": "above_horizon"})
            def context(response):
                cm = MagicMock()
                cm.__aenter__ = AsyncMock(return_value=response)
                cm.__aexit__ = AsyncMock(return_value=False)
                return cm
            session = MagicMock()
            session.get.side_effect = [context(camera), context(sun)]
            with patch.object(store, "shadow_predict_crop", return_value={
                    "prediction": "OPEN", "model_id": "abcdef123456"}) as predict:
                await store.owner_confirm(session, "OPEN", "http://ha", {})
                predict.assert_called_once()
                self.assertEqual(predict.call_args.args[1], "above_horizon")
            row = store.entries()[0][1]
            self.assertEqual(row["sun"], "above_horizon")
            self.assertTrue(row["verified"])
            self.assertEqual(row["shadow_model_id"], "abcdef123456")
            self.assertEqual(row["shadow_prediction"], "OPEN")

    async def test_sun_failure_still_saves_owner_label(self):
        from unittest.mock import AsyncMock, MagicMock
        with tempfile.TemporaryDirectory() as temp:
            store = GateDataset(enabled=True, root=temp)
            camera = MagicMock()
            camera.status = 200
            camera.read = AsyncMock(return_value=jpeg())
            cm = MagicMock()
            cm.__aenter__ = AsyncMock(return_value=camera)
            cm.__aexit__ = AsyncMock(return_value=False)
            session = MagicMock()
            session.get.side_effect = [cm, TimeoutError()]
            await store.owner_confirm(session, "CLOSED", "http://ha", {})
            row = store.entries()[0][1]
            self.assertEqual(row["sun"], "unknown")
            self.assertEqual(row["label"], "CLOSED")
            self.assertTrue(row["verified"])
            self.assertEqual(row["shadow_prediction"], "UNKNOWN")


if __name__ == "__main__":
    unittest.main()
