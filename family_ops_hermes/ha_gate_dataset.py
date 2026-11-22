#!/usr/bin/env python3
"""Local-only Hermes gate vision dataset. NO gate actuator, cloud, or raw frame export.

Routine frames are CROPPED before being written to persistent add-on /data.
UNVERIFIED frames never become training truth. Owner can confirm a fresh live frame.
This is a cautious incremental classifier, not a pretrained vision model.
"""
import datetime as dt
import io
import json
import math
import os
import secrets
import re
import time
from pathlib import Path

CAMERA = "camera.nvr_car_porch_cam_sub"
ROOT = Path("/data/gate_vision")
INTERVAL = 1800
RETENTION = 14 * 86400
MAX_BYTES = 80 * 1024 * 1024
MAX_FILES = 800
# Provisional camera-relative gate/driveway entrance region. Recalibrate using
# verified owner frames before considering vision safe for automatic actions.
# Calibrated on owner's full-frame OPEN and CLOSED views (same 16:9 camera pose).
# Include the whole moving gate and opening; older right-only crops remain isolated
# by the per-sample ROI metadata and must never contribute to the new classifier.
ROI = (0.16, 0.89, 0.35, 0.85)


def feature(image):
    """Small monochrome local vector; JPEG source never leaves this process."""
    from PIL import Image, ImageOps
    if not image.startswith(b"\xff\xd8") or len(image) > 6_000_000:
        raise ValueError("invalid_jpeg")
    with Image.open(io.BytesIO(image)) as raw:
        if raw.width < 640 or raw.height < 360:
            raise ValueError("invalid_size")
        gray = ImageOps.exif_transpose(raw).convert("L")
        w, h = gray.size
        x1, x2, y1, y2 = ROI
        cropped = gray.crop((round(x1*w), round(y1*h), round(x2*w), round(y2*h)))
        vector = list(cropped.resize((16, 16), Image.Resampling.BILINEAR).tobytes())
        out = io.BytesIO()
        cropped.save(out, format="JPEG", quality=76, optimize=True)
        return out.getvalue(), vector


def similarity(a, b):
    if len(a) != len(b):
        return 0.0
    am, bm = sum(a)/len(a), sum(b)/len(b)
    aa, bb = [x-am for x in a], [x-bm for x in b]
    denom = math.sqrt(sum(x*x for x in aa)*sum(y*y for y in bb))
    return sum(x*y for x, y in zip(aa, bb))/denom if denom > 1e-9 else 0.0


class GateDataset:
    def __init__(self, enabled=None, root=None):
        self.enabled = (os.environ.get("HA_GATE_DATASET", "false").lower() == "true"
                        if enabled is None else bool(enabled))
        self.root = Path(root) if root is not None else ROOT
        self.last_routine = None
        self.last_saved_utc = None
        self.errors = 0
        self._focused_cache = {}
        self._evaluation_cache = None
        self._shape_probe = None
        self._shadow_probe = None
        if self.enabled:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            self.root.chmod(0o700)
            self.prune()

    def due(self):
        return self.enabled and (self.last_routine is None or time.monotonic() - self.last_routine >= INTERVAL)

    def entries(self):
        if not self.enabled:
            return []
        records = []
        for meta in self.root.glob("*.json"):
            try:
                row = json.loads(meta.read_text(encoding="utf-8"))
                if (row.get("id") == meta.stem and
                    row.get("label") in ("UNKNOWN", "OPEN", "CLOSED") and
                    (self.root / (meta.stem + ".jpg")).is_file()):
                    records.append((meta, row))
            except (OSError, ValueError, TypeError):
                continue
        return sorted(records, key=lambda pair: pair[1].get("timestamp", 0))

    def prune(self):
        if not self.enabled:
            return
        records = self.entries()
        now = time.time()
        size = sum((self.root / (p.stem + ".jpg")).stat().st_size for p, _ in records)
        for meta, row in list(records):
            photo = self.root / (meta.stem + ".jpg")
            try:
                old = float(row.get("timestamp", 0)) < now - RETENTION
                oversized = len(records) > MAX_FILES or size > MAX_BYTES
                if not (old or oversized):
                    continue
                bytes_used = photo.stat().st_size
                photo.unlink(missing_ok=True)
                meta.unlink(missing_ok=True)
                size -= bytes_used
                records.remove((meta, row))
            except OSError:
                self.errors += 1
        # Remove stale orphaned image files from interrupted writes.
        for photo in self.root.glob("*.jpg"):
            if not (self.root / (photo.stem + ".json")).exists():
                try:
                    if now - photo.stat().st_mtime > 3600:
                        photo.unlink(missing_ok=True)
                except OSError:
                    pass

    def save(self, jpeg, label="UNKNOWN", source="periodic", sun="unknown", *, return_id=False):
        if not self.enabled:
            return False
        if label not in ("UNKNOWN", "OPEN", "CLOSED"):
            raise ValueError("bad_label")
        cropped, vector = feature(jpeg)
        stamp = time.time()
        stamp_id = dt.datetime.fromtimestamp(stamp, dt.timezone.utc).strftime("%Y%m%dT%H%M%S")
        sample_id = stamp_id + "-" + secrets.token_hex(4)
        shadow = self.shadow_predict_crop(cropped, sun)
        record = {
            "id": sample_id, "timestamp": stamp,
            "label": label, "source": source, "sun": str(sun)[:24],
            "verified": source == "owner_confirmed",
            "vector": vector, "roi": list(ROI),
            "shadow_prediction": shadow["prediction"],
            "shadow_model_id": shadow["model_id"],
        }
        photo = self.root / (sample_id + ".jpg")
        meta = self.root / (sample_id + ".json")
        fd = os.open(photo, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(cropped)
            meta.write_text(json.dumps(record, separators=(",", ":")), encoding="utf-8")
            meta.chmod(0o600)
        except Exception:
            photo.unlink(missing_ok=True)
            meta.unlink(missing_ok=True)
            raise
        self.last_saved_utc = dt.datetime.fromtimestamp(stamp, dt.timezone.utc).isoformat()
        self.prune()
        return sample_id if return_id else True

    def capture_routine(self, jpeg, sun):
        if not self.due():
            return False
        self.last_routine = time.monotonic()
        try:
            return self.save(jpeg, sun=sun)
        except (OSError, ValueError):
            self.errors += 1
            return False

    async def owner_confirm(self, session, label, api, headers):
        """Owner's explicit observed state labels a NEW frame, never an old sample."""
        if not self.enabled:
            return "Pengumpulan data gate belum diaktifkan."
        if label not in ("OPEN", "CLOSED"):
            return "Guna /gatelearn OPEN atau /gatelearn CLOSED selepas semak gate."
        try:
            async with session.get(api + "/camera_proxy/" + CAMERA, headers=headers, timeout=23) as response:
                if response.status != 200:
                    return "CCTV gagal capture. Tiada sampel dilabel."
                jpeg = await response.read()
            self.save(jpeg, label=label, source="owner_confirmed")
            return ("✅ Sampel CCTV terkini disahkan " + label +
                    ". Hermes akan guna data disahkan untuk penilaian classifier; gate tidak dikawal.")
        except Exception:
            self.errors += 1
            return "Gagal simpan gambar. Tiada perubahan pada gate."

    def stats(self):
        all_records = [row for _, row in self.entries()]
        records = [row for row in all_records if row.get("roi") == list(ROI)]
        latest = max((row.get('timestamp', 0) for row in records), default=None)
        last = (dt.datetime.fromtimestamp(latest, dt.timezone.utc).isoformat()
                if latest is not None else None)
        return {
            "enabled": self.enabled,
            "samples": len(records),
            "legacy_samples": len(all_records) - len(records),
            "open": sum(r["label"] == "OPEN" and r.get("verified") for r in records),
            "closed": sum(r["label"] == "CLOSED" and r.get("verified") for r in records),
            "unverified": sum(not r.get("verified") for r in records),
            "last_sample_utc": last,
            "errors": self.errors,
        }

    def summary(self):
        s = self.stats()
        if not s["enabled"]:
            return "📚 Gate learning: OFF"
        return (f"📚 Gate learning: setiap 30 minit; 14 hari lokal\n"
                f"Sampel: {s['samples']} | OPEN disahkan: {s['open']} | "
                f"CLOSED disahkan: {s['closed']} | Belum disahkan: {s['unverified']}\n"
                f"Model: {'boleh dinilai' if s['open'] >= 6 and s['closed'] >= 6 else 'belum cukup sampel disahkan'}\n"
                f"Label keadaan sebenar: /gatelearn OPEN atau /gatelearn CLOSED\n"
                "Data tidak dieksport. Tiada arahan motor pagar.")

    @staticmethod
    def _valid_sample_id(sample_id):
        return isinstance(sample_id, str) and bool(
            re.fullmatch(r"[0-9]{8}T[0-9]{6}-[a-f0-9]{8}", sample_id)
        )

    def review_queue(self, limit=100):
        """Only NEW ROI images, not already labelled or obsolete training crops."""
        if not self.enabled:
            return []
        todo = [
            {
                "id": row["id"],
                "timestamp": float(row["timestamp"]),
                "sun": str(row.get("sun", "unknown"))[:24],
            }
            for _, row in self.entries()
            if row.get("roi") == list(ROI)
            and row["label"] == "UNKNOWN"
            and not row.get("verified", False)
        ]
        todo.sort(key=lambda r: r["timestamp"], reverse=True)
        return todo[:max(1, min(int(limit), 120))]

    def review_photo(self, sample_id):
        """Return only the existing CROPPED image for current-ROI review."""
        if not self.enabled or not self._valid_sample_id(sample_id):
            raise ValueError("invalid_sample")
        meta = self.root / (sample_id + ".json")
        photo = self.root / (sample_id + ".jpg")
        try:
            row = json.loads(meta.read_text(encoding="utf-8"))
            if (row.get("id") != sample_id or row.get("roi") != list(ROI)
                    or row.get("label") != "UNKNOWN"
                    or row.get("verified") or not photo.is_file()):
                raise ValueError("sample_not_reviewable")
            data = photo.read_bytes()
            if not data.startswith(b"\xff\xd8") or len(data) > 1_500_000:
                raise ValueError("bad_photo")
            return data
        except FileNotFoundError as exc:
            raise ValueError("sample_expired") from exc

    def confirm_existing(self, sample_id, label):
        """Atomically label exactly one saved image; no camera snapshot or gate call."""
        if not self.enabled or not self._valid_sample_id(sample_id):
            raise ValueError("invalid_sample")
        if label not in ("OPEN", "CLOSED"):
            raise ValueError("invalid_label")
        meta = self.root / (sample_id + ".json")
        photo = self.root / (sample_id + ".jpg")
        try:
            row = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("sample_not_found") from exc
        if (row.get("id") != sample_id or row.get("roi") != list(ROI)
                or row.get("label") != "UNKNOWN" or row.get("verified")
                or not photo.is_file()):
            raise ValueError("sample_already_reviewed_or_old_crop")
        if float(row.get("timestamp", 0)) < time.time() - RETENTION:
            raise ValueError("sample_expired")
        row.update({
            "label": label,
            "verified": True,
            "source": "owner_review",
            "verified_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        })
        temporary = self.root / ("." + sample_id + "-" + secrets.token_hex(4) + ".tmp")
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(row, handle, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, meta)
        finally:
            temporary.unlink(missing_ok=True)
        return True

    def _focused_vector(self, crop_jpeg):
        """Upper gate band from the existing stored ROI crop; exclude parked car below.

        Old owner-confirmed photos remain usable; do NOT relabel or mutate them.
        """
        from PIL import Image
        with Image.open(io.BytesIO(crop_jpeg)) as image:
            gray = image.convert("L")
            gate = gray.crop((0, 0, gray.width, max(1, round(gray.height * 0.58))))
            return list(gate.resize((16, 16), Image.Resampling.BILINEAR).tobytes())

    @staticmethod
    def _score(vector, train):
        return {label: max((similarity(vector, ref) for ref in refs), default=0.0)
                for label, refs in train.items()}

    def evaluate(self):
        """Time-ordered, class-balanced held-out self-check; never a safety certificate.

        Use the newest 25% of each class as validation only, never references.
        This limits repeated CLOSED frames from dominating a smaller OPEN class.
        """
        if not self.enabled:
            return {"status": "DISABLED", "reason": "DATASET_DISABLED",
                    "accuracy_pct": None, "model": "none", "evaluated": 0}
        rows = [row for _, row in self.entries()
                if row.get("roi") == list(ROI) and row.get("verified")
                and row.get("label") in ("OPEN", "CLOSED")
                and isinstance(row.get("vector"), list)
                and len(row["vector"]) == 256]
        classes = {label: [r for r in rows if r["label"] == label][-36:]
                   for label in ("OPEN", "CLOSED")}
        if min(map(len, classes.values())) < 6:
            return {"status": "COLLECTING", "reason": "INSUFFICIENT_VERIFIED_SAMPLES",
                    "accuracy_pct": None, "model": "none", "evaluated": 0}
        # Equal-sized recent classes, so abundant CLOSED images cannot skew accuracy.
        n = min(map(len, classes.values()))
        selected = {k: v[-n:] for k, v in classes.items()}
        key = tuple((r["id"], r["label"]) for label in ("OPEN", "CLOSED")
                    for r in selected[label])
        if self._evaluation_cache is not None and self._evaluation_cache["key"] == key:
            return dict(self._evaluation_cache["report"])
        holdout = max(2, (n + 3) // 4)
        split = {k: (v[:-holdout], v[-holdout:]) for k, v in selected.items()}
        candidates = {"full_roi": {}, "gate_band": {}}
        for label in ("OPEN", "CLOSED"):
            for row in selected[label]:
                sid = row["id"]
                if sid not in self._focused_cache:
                    try:
                        self._focused_cache[sid] = self._focused_vector(
                            (self.root / (sid + ".jpg")).read_bytes())
                    except (OSError, ValueError):
                        self._focused_cache[sid] = None
                candidates["full_roi"][sid] = row["vector"]
                candidates["gate_band"][sid] = self._focused_cache[sid]
        results = []
        for model, vectors in candidates.items():
            train = {label: [vectors[r["id"]] for r in split[label][0]
                             if vectors[r["id"]] is not None]
                     for label in ("OPEN", "CLOSED")}
            if min(map(len, train.values())) < 4:
                continue
            correct = {"OPEN": 0, "CLOSED": 0}
            checked = {"OPEN": 0, "CLOSED": 0}
            for label in ("OPEN", "CLOSED"):
                for row in split[label][1]:
                    vector = vectors[row["id"]]
                    if vector is None:
                        continue
                    scores = self._score(vector, train)
                    other = "CLOSED" if label == "OPEN" else "OPEN"
                    predicted = max(scores, key=scores.get)
                    checked[label] += 1
                    if (predicted == label and scores[label] >= 0.93
                            and scores[label] - scores[other] >= 0.12):
                        correct[label] += 1
            total = sum(checked.values())
            if not total or min(checked.values()) < 2:
                continue
            acc = sum(correct.values()) / total
            class_min = min(correct[k] / checked[k] for k in checked)
            results.append((acc, class_min, model, train, checked))
        if not results:
            report = {"status": "EVALUATION_FAILED", "reason": "NO_VALID_HOLDOUT",
                      "accuracy_pct": 0.0, "model": "none", "evaluated": 0}
            self._evaluation_cache = {"key": key, "report": report, "train": None}
            return dict(report)
        # Accuracy wins; on a tie prefer the gate-focused model over car/driveway pixels.
        accuracy, class_min, model, train, checked = max(
            results, key=lambda x: (x[0], x[1], x[2] == "gate_band"))
        passed = accuracy >= 0.90 and class_min >= 0.80
        report = {
            "status": "READY_POC" if passed else "EVALUATION_FAILED",
            "reason": "HOLDOUT_PASSED" if passed else "HOLDOUT_ACCURACY_LOW",
            "accuracy_pct": round(accuracy * 100, 1),
            "min_class_accuracy_pct": round(class_min * 100, 1),
            "model": model,
            "evaluated": sum(checked.values()),
            "open_evaluated": checked["OPEN"],
            "closed_evaluated": checked["CLOSED"],
            "reference_per_class": n - holdout,
        }
        self._evaluation_cache = {"key": key, "report": report,
                                  "train": train if passed else None}
        # Do not grow image feature caches indefinitely over the 14-day retention.
        self._focused_cache = {sid: self._focused_cache[sid]
                               for sid in [r["id"] for k in selected for r in selected[k]]
                               if sid in self._focused_cache}
        return dict(report)

    def shape_benchmark(self):
        """Optional dry-run candidate test, never changes the active classifier."""
        if not self.enabled:
            return {"status": "DISABLED", "accuracy_pct": None,
                    "model": "none", "is_dry_run": True}
        try:
            from ha_gate_structure import GateShapeProbe
            if self._shape_probe is None:
                self._shape_probe = GateShapeProbe(self.root)
            rows = [row for _, row in self.entries()
                    if row.get("roi") == list(ROI) and row.get("verified")
                    and row.get("label") in ("OPEN", "CLOSED")]
            return self._shape_probe.evaluate(rows)
        except (OSError, ValueError, TypeError, KeyError, ImportError):
            return {"status": "DIAGNOSTIC_ERROR", "accuracy_pct": None,
                    "model": "none", "is_dry_run": True}

    def _shadow(self):
        from ha_gate_shadow import GateShadow
        if self._shadow_probe is None:
            self._shadow_probe = GateShadow(self.root)
        return self._shadow_probe

    def shadow_predict_crop(self, cropped, sun="above_horizon"):
        """Read-only shadow prediction; never changes active classifier."""
        if not self.enabled:
            return {"prediction": "UNKNOWN", "reason": "DATASET_DISABLED",
                    "model_id": "none"}
        try:
            return self._shadow().predict(cropped, self, sun)
        except (OSError, ValueError, TypeError, KeyError, ImportError):
            return {"prediction": "UNKNOWN", "reason": "SHADOW_ERROR",
                    "model_id": "none"}

    def shadow_predict(self, camera_jpeg, sun="above_horizon"):
        """Use the exact same crop as stored samples, without a motor call."""
        if not self.enabled or sun != "above_horizon":
            return {"prediction": "UNKNOWN", "reason": "NOT_DAYLIGHT_OR_DISABLED",
                    "model_id": "none"}
        try:
            cropped, _ = feature(camera_jpeg)
            return self.shadow_predict_crop(cropped, sun)
        except (OSError, ValueError, TypeError):
            return {"prediction": "UNKNOWN", "reason": "INVALID_CAMERA_FRAME",
                    "model_id": "none"}

    def shadow_stats(self):
        """Aggregate only owner-verified images scored BEFORE their labels."""
        if not self.enabled:
            return {"status": "DISABLED", "accuracy_pct": None,
                    "model_id": "none", "ready_for_manual_review": False}
        try:
            rows = [row for _, row in self.entries()
                    if row.get("roi") == list(ROI)]
            return self._shadow().verification(rows)
        except (OSError, ValueError, TypeError, KeyError, ImportError):
            return {"status": "SHADOW_STATS_ERROR", "accuracy_pct": None,
                    "model_id": "none", "ready_for_manual_review": False}

    def classify_diagnostic(self, jpeg):
        """Return label, margin and sanitized reason. No pictures or locations."""
        report = self.evaluate()
        if report["status"] != "READY_POC":
            return "UNKNOWN", 0.0, {
                "reason": report["reason"], "model": report["model"], "similarity": 0.0}
        try:
            crop, full_vector = feature(jpeg)
            vector = (self._focused_vector(crop)
                      if report["model"] == "gate_band" else full_vector)
        except (ValueError, OSError):
            return "UNKNOWN", 0.0, {
                "reason": "INVALID_CAMERA_FRAME", "model": report["model"],
                "similarity": 0.0}
        scores = self._score(vector, self._evaluation_cache["train"])
        best = max(scores, key=scores.get)
        other = "CLOSED" if best == "OPEN" else "OPEN"
        similarity_score = scores[best]
        margin = max(0.0, scores[best] - scores[other])
        if similarity_score < 0.93:
            reason = "LOW_SIMILARITY"
        elif margin < 0.12:
            reason = "AMBIGUOUS_MARGIN"
        else:
            reason = "MODEL_CONFIDENT"
        return (best if reason == "MODEL_CONFIDENT" else "UNKNOWN",
                round(margin, 3),
                {"reason": reason, "model": report["model"],
                 "similarity": round(similarity_score, 3)})

    def classify(self, jpeg):
        label, margin, _ = self.classify_diagnostic(jpeg)
        return label, margin
