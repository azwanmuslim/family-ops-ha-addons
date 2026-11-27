"""Local-only frozen Gate Shape POC shadow inference; no actuator or outbound calls.

A historical event-holdout candidate must pass before the reference can be
frozen. Future sample predictions are attached at capture time, BEFORE owner
labels are applied. These predictions never change after verification.
"""
import datetime as dt
import json
import math
import os
from pathlib import Path
import secrets
import time

from ha_gate_structure import _extract_all, _mask_score, _mask_prototypes, _sessions, GateShapeProbe


class GateShadow:
    VERSION = "mask_vertical_frozen_v1"
    FILENAME = "gate_shadow_mask_vertical_v1.model"

    def __init__(self, root, lighting="above_horizon"):
        self.root = Path(root)
        self.lighting = lighting
        self.base_file = self.root / (self.FILENAME if lighting == "above_horizon"
                                 else "gate_active_mask_vertical_night_v1.model")
        self.file = self.base_file
        self._night_probe = GateShapeProbe(self.root) if lighting == "below_horizon" else None
        self.model = None
        self._last_error = "NOT_INITIALIZED"

    def _revision_file(self):
        marker = self.root / ".label_revision"
        try:
            revision = marker.read_text().strip()
        except FileNotFoundError:
            return self.base_file
        if len(revision) != 24 or any(c not in "0123456789abcdef" for c in revision):
            raise ValueError("invalid_label_revision")
        return self.base_file.with_name(self.base_file.stem + "-" + revision + ".model")

    def _load(self):
        target = self._revision_file()
        if target != self.file:
            self.file = target
            self.model = None
        if self.model is not None:
            return self.model
        if not self.file.is_file():
            return None
        try:
            blob = json.loads(self.file.read_text(encoding="utf-8"))
            if (blob.get("version") != self.VERSION or
                    not isinstance(blob.get("id"), str) or
                    len(blob["id"]) != 12 or
                    not isinstance(blob.get("means"), dict) or
                    set(blob["means"]) != {"OPEN", "CLOSED"} or
                    not all(len(v) == 1152 for v in blob["means"].values())):
                self._last_error = "INVALID_FROZEN_MODEL"
                return None
            weights = blob.get("weights")
            if (not isinstance(weights, dict) or not weights or
                    any(not k.isdigit() or int(k) >= 1152 or int(k) < 0
                        or not isinstance(v, (float, int)) or v <= 0
                        for k, v in weights.items())):
                self._last_error = "INVALID_FROZEN_MODEL"
                return None
            self.model = blob
            return self.model
        except (OSError, ValueError, TypeError, KeyError):
            self._last_error = "FROZEN_MODEL_READ_ERROR"
            return None

    def _fit(self, dataset):
        # No fitting if the historical candidate has not passed event holdout.
        target = self.file
        from ha_gate_dataset import ROI
        rows = [r for _, r in dataset.entries() if
                r.get("verified") and r.get("roi") == list(ROI)
                and r.get("label") in ("OPEN", "CLOSED")]
        if self._night_probe is not None:
            rows = [r for r in rows if r.get("sun") == "below_horizon"]
            report = self._night_probe.evaluate(rows)
            # Validate this exact feature model, not a different winning candidate.
            candidate = report.get("candidate_scores", {}).get("mask_vertical", {})
            report = dict(report, model="mask_vertical",
                          open_recall_pct=candidate.get("open_recall_pct", 0),
                          closed_recall_pct=candidate.get("closed_recall_pct", 0))
        else:
            report = dataset.shape_benchmark()
        if (report.get("status") != "CANDIDATE_PASSED" or
                report.get("model") != "mask_vertical" or
                report.get("split") != "event_holdout" or
                report.get("open_recall_pct", 0) < 90 or
                report.get("closed_recall_pct", 0) < 90):
            self._last_error = "BASELINE_NOT_ELIGIBLE"
            return None
        train = {}
        for label in ("OPEN", "CLOSED"):
            values = [r for r in rows if r["label"] == label]
            if len(values) > 64:
                indexes = {round(i * (len(values) - 1) / 63) for i in range(64)}
                selected = [values[i] for i in sorted(indexes)]
            else:
                selected = values
            sessions = _sessions(selected)
            held_events = max(2, math.ceil(len(sessions) / 4))
            if len(sessions) < 4 or len(sessions) - held_events < 2:
                self._last_error = "TOO_FEW_EVENTS"
                return None
            training = [r for group in sessions[:-held_events] for r in group][-48:]
            if len(training) < 6:
                self._last_error = "TOO_FEW_TRAINING_SAMPLES"
                return None
            features = []
            for r in training:
                try:
                    features.append(_extract_all(
                        (self.root / (r["id"] + ".jpg")).read_bytes()
                    )["mask_vertical"])
                except (OSError, ValueError, TypeError):
                    continue
            if len(features) < 6:
                self._last_error = "MISSING_CROPS"
                return None
            train[label] = features
        means, weights = _mask_prototypes(train)
        if not weights:
            self._last_error = "NO_DISCRIMINATIVE_PIXELS"
            return None
        model = {
            "version": self.VERSION, "id": secrets.token_hex(6),
            "frozen_at": time.time(),
            "means": means, "weights": {str(k): v for k, v in weights.items()},
        }
        # Avoid clobbering a model created concurrently by review and advisor.
        tmp = self.root / ("." + self.FILENAME + "-" + secrets.token_hex(4))
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(model, stream, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            try:
                if target != self._revision_file():
                    return None
                os.link(tmp, target)  # Atomic create if absent; no overwrite.
            except FileExistsError:
                pass
        finally:
            tmp.unlink(missing_ok=True)
        self._last_error = "FROZEN"
        return self._load()

    def predict(self, cropped, dataset, sun="above_horizon"):
        if sun != self.lighting:
            return {"prediction": "UNKNOWN", "reason": "NIGHT_OR_UNKNOWN_LIGHT",
                    "model_id": "none"}
        try:
            model = self._load()
            if not model:
                if self.file.exists():
                    return {"prediction": "UNKNOWN", "reason": self._last_error,
                            "model_id": "none"}
                model = self._fit(dataset)
            if not model:
                return {"prediction": "UNKNOWN", "reason": self._last_error,
                        "model_id": "none"}
            vector = _extract_all(cropped)["mask_vertical"]
            means = model["means"]
            weights = {int(k): v for k, v in model["weights"].items()}
            predicted, margin = _mask_score(vector, (means, weights))
            if self.file != self._revision_file():
                return {"prediction": "UNKNOWN", "reason": "LABELS_CHANGED", "model_id": "none"}
            return {"prediction": predicted, "margin": margin,
                    "reason": ("SHADOW_ONLY" if predicted != "UNKNOWN" else "LOW_CONFIDENCE"),
                    "model_id": model["id"]}
        except (OSError, ValueError, TypeError, KeyError, IndexError):
            return {"prediction": "UNKNOWN", "reason": "SHADOW_INFERENCE_FAILED",
                    "model_id": "none"}

    def verification(self, rows):
        model = self._load()
        base = {
            "status": "AWAITING_FROZEN_MODEL" if not model else "COLLECTING_LABELS",
            "model": self.VERSION, "model_id": model["id"] if model else "none",
            "accuracy_pct": None, "open_recall_pct": None,
            "closed_recall_pct": None, "open_verified": 0,
            "closed_verified": 0, "open_events": 0, "closed_events": 0,
            "ready_for_manual_review": False,
        }
        if not model:
            return base
        counts = {}
        for label in ("OPEN", "CLOSED"):
            verified = [r for r in rows if r.get("verified")
                        and r.get("label") == label
                        and r.get("shadow_model_id") == model["id"]
                        and r.get("shadow_prediction") in ("OPEN", "CLOSED", "UNKNOWN")]
            accurate = sum(r["shadow_prediction"] == label for r in verified)
            counts[label] = (len(verified), accurate)
            base[label.lower() + "_verified"] = len(verified)
            base[label.lower() + "_events"] = len(_sessions(verified))
            if verified:
                base[label.lower() + "_recall_pct"] = round(100 * accurate / len(verified), 1)
        if min(counts[k][0] for k in counts) > 0:
            a, b = base["open_recall_pct"], base["closed_recall_pct"]
            base["accuracy_pct"] = round((a + b) / 2, 1)
        # Distinct events and per-class recalls; never auto-promote or control gate.
        if base["open_events"] >= 5 and base["closed_events"] >= 10:
            if (base["open_recall_pct"] >= 90 and base["closed_recall_pct"] >= 90):
                base["status"] = "EVIDENCE_FOR_MANUAL_REVIEW"
                base["ready_for_manual_review"] = True
            else:
                base["status"] = "SHADOW_ACCURACY_LOW"
        return base
