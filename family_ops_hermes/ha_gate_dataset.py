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
        record = {
            "id": sample_id, "timestamp": stamp,
            "label": label, "source": source, "sun": str(sun)[:24],
            "verified": source == "owner_confirmed",
            "vector": vector, "roi": list(ROI),
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

    def classify(self, jpeg):
        """Very conservative optional nearest-reference learning; never auto-label."""
        if not self.enabled:
            return "UNKNOWN", 0.0
        rows = [row for _, row in self.entries() if row.get("roi") == list(ROI) and row.get("verified")
                and row["label"] in ("OPEN", "CLOSED")
                and isinstance(row.get("vector"), list) and len(row["vector"]) == 256]
        classes = {k: [row["vector"] for row in rows if row["label"] == k][-36:]
                   for k in ("OPEN", "CLOSED")}
        if min(map(len, classes.values())) < 6:
            return "UNKNOWN", 0.0
        # Lightweight leave-one-out self-check. Do not trust an unvalidated model.
        correct, total = 0, 0
        for label in classes:
            for idx, test in enumerate(classes[label]):
                if idx % 4 != 0:
                    continue
                train = {k: (vectors[:idx] + vectors[idx+1:] if k == label else vectors)
                         for k, vectors in classes.items()}
                scores = {k: max(similarity(test, v) for v in vectors)
                          for k, vectors in train.items()}
                correct += int(max(scores, key=scores.get) == label)
                total += 1
        if total < 4 or correct / total < .90:
            return "UNKNOWN", 0.0
        try:
            _, test = feature(jpeg)
        except ValueError:
            return "UNKNOWN", 0.0
        scores = {k: max(similarity(test, v) for v in vectors)
                  for k, vectors in classes.items()}
        best = max(scores, key=scores.get)
        margin = scores[best] - scores["CLOSED" if best == "OPEN" else "OPEN"]
        if scores[best] < .93 or margin < .12:
            return "UNKNOWN", round(max(0.0, margin), 3)
        return best, round(min(1.0, margin), 3)
