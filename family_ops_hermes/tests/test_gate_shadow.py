"""Frozen shadow predictions are written before owner labels; never actuate."""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from ha_gate_dataset import GateDataset
from ha_gate_shadow import GateShadow
from test_gate_structure import frame


class ShadowTests(unittest.TestCase):
    def _store(self, folder):
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
        return store

    def test_not_frozen_without_passing_historical_benchmark(self):
        with tempfile.TemporaryDirectory() as folder:
            store = GateDataset(enabled=True, root=folder)
            status = store.shadow_predict(frame(True))
            self.assertEqual(status["prediction"], "UNKNOWN")
            self.assertEqual(status["reason"], "BASELINE_NOT_ELIGIBLE")
            self.assertFalse((Path(folder) / GateShadow.FILENAME).exists())

    def test_freezes_private_model_and_persists_prediction_before_label(self):
        with tempfile.TemporaryDirectory() as folder:
            store = self._store(folder)
            eligible = {"status": "CANDIDATE_PASSED",
                        "model": "mask_vertical", "split": "event_holdout",
                        "open_recall_pct": 100, "closed_recall_pct": 93.8}
            with patch.object(store, "shape_benchmark", return_value=eligible):
                sid = store.save(frame(True), sun="above_horizon", return_id=True)
            meta = Path(folder) / (sid + ".json")
            stored_before = json.loads(meta.read_text())
            self.assertFalse(stored_before["verified"])
            self.assertIn(stored_before["shadow_prediction"],
                          ("OPEN", "CLOSED", "UNKNOWN"))
            self.assertEqual(len(stored_before["shadow_model_id"]), 12)
            model_path = Path(folder) / GateShadow.FILENAME
            self.assertTrue(model_path.exists())
            self.assertEqual(model_path.stat().st_mode & 0o777, 0o600)
            frozen = model_path.read_bytes()
            self.assertNotIn(b"data:image", frozen)
            self.assertNotIn(b"token", frozen.lower())
            fresh = GateDataset(enabled=True, root=folder)
            next_prediction = fresh.shadow_predict(frame(False))
            self.assertEqual(next_prediction["model_id"],
                             stored_before["shadow_model_id"])
            self.assertEqual(frozen, model_path.read_bytes())  # no retraining
            store.confirm_existing(sid, "OPEN")
            stored_after = json.loads(meta.read_text())
            self.assertTrue(stored_after["verified"])
            self.assertEqual(stored_after["shadow_prediction"],
                             stored_before["shadow_prediction"])
            self.assertEqual(stored_after["shadow_model_id"],
                             stored_before["shadow_model_id"])
            result = store.shadow_stats()
            self.assertEqual(result["open_verified"], 1)
            self.assertEqual(result["closed_verified"], 0)
            self.assertFalse(result["ready_for_manual_review"])
            self.assertEqual(result["status"], "COLLECTING_LABELS")

    def test_rebuild_after_correction_uses_eligible_mask_not_global_winner(self):
        with tempfile.TemporaryDirectory() as folder:
            store = self._store(folder)
            eligible = {"status": "CANDIDATE_PASSED", "model": "edge_gate",
                        "split": "event_holdout", "open_recall_pct": 100,
                        "closed_recall_pct": 100, "candidate_scores": {
                            "mask_vertical": {"open_recall_pct": 80, "closed_recall_pct": 100},
                            "mask_both": {"open_recall_pct": 95, "closed_recall_pct": 95}}}
            legacy = dict(eligible, model="mask_vertical", candidate_scores={})
            with patch.object(store, "shape_benchmark", return_value=legacy):
                old = store.shadow_predict(frame(True))
            self.assertNotEqual(old["model_id"], "none")
            row = next(r for _, r in store.entries() if r["label"] == "OPEN")
            store.confirm_existing(row["id"], "CLOSED", expected="OPEN")
            # The old model must remain unusable after changing labels.
            failed = dict(eligible, split="time_sample_holdout")
            with patch.object(store, "shape_benchmark", return_value=failed):
                self.assertEqual(store.shadow_predict(frame(True))["model_id"], "none")
            with patch.object(store, "shape_benchmark", return_value=eligible):
                new = store.shadow_predict(frame(True))
            self.assertEqual(new["feature"], "mask_both")
            self.assertNotEqual(new["model_id"], old["model_id"])
            self.assertNotEqual(new["model_id"], "none")

    def test_provisional_day_display_keeps_strict_model_unavailable(self):
        with tempfile.TemporaryDirectory() as folder:
            store = self._store(folder)
            report = {"status": "CANDIDATE_PASSED", "model": "mask_both",
                      "split": "event_holdout", "open_recall_pct": 100,
                      "closed_recall_pct": 81.2}
            with patch.object(store, "shape_benchmark", return_value=report):
                self.assertEqual(store.shadow_predict(frame(True))["model_id"], "none")
                state, margin, diagnostic = store.active_classify(frame(True), "above_horizon")
                self.assertTrue(diagnostic["provisional"])
                self.assertNotEqual(diagnostic["model_id"], "none")
                self.assertEqual(diagnostic["model"], "mask_both")
                self.assertIn(diagnostic["reason"], ("DISPLAY_ONLY_PROVISIONAL", "DISPLAY_ONLY_LOW_CONFIDENCE"))
                self.assertFalse((Path(folder) / GateShadow.FILENAME).exists())
                self.assertTrue((Path(folder) / "gate_display_only_day_v1.model").exists())
            # Never use a daytime display model for nighttime inference.
            self.assertEqual(store.shadow_predict(frame(True), "below_horizon")["prediction"], "UNKNOWN")

    def test_night_and_invalid_frame_never_generate_predictions(self):
        with tempfile.TemporaryDirectory() as folder:
            store = self._store(folder)
            self.assertEqual(store.shadow_predict(frame(True), "below_horizon")
                             ["prediction"], "UNKNOWN")
            self.assertEqual(store.shadow_predict(b"broken")
                             ["prediction"], "UNKNOWN")
            self.assertFalse((Path(folder) / GateShadow.FILENAME).exists())

    def test_no_auto_promotion_on_apparent_high_accuracy(self):
        with tempfile.TemporaryDirectory() as folder:
            model = GateShadow(folder)
            frozen = {
                "version": GateShadow.VERSION, "id": "abcdef123456",
                "frozen_at": time.time(),
                "means": {"OPEN": [0.1] * 1152, "CLOSED": [0.9] * 1152},
                "weights": {"0": 1.0},
            }
            model.file.write_text(json.dumps(frozen))
            now = time.time()
            rows = []
            for i in range(18):
                label = "OPEN" if i < 7 else "CLOSED"
                rows.append({"label": label, "verified": True,
                             "timestamp": now - (i * 1800),
                             "shadow_model_id": frozen["id"],
                             "shadow_prediction": label})
            outcome = model.verification(rows)
            self.assertTrue(outcome["ready_for_manual_review"])
            self.assertEqual(outcome["status"], "EVIDENCE_FOR_MANUAL_REVIEW")
            self.assertNotIn("actuation", outcome)
            self.assertEqual(outcome["accuracy_pct"], 100)
            self.assertEqual(outcome["open_events"], 7)
            self.assertEqual(outcome["closed_events"], 11)


if __name__ == "__main__":
    unittest.main()
