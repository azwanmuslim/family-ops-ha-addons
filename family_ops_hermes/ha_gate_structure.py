"""Read-only local gate-shape benchmark over retained, owner-verified JPEG crops.

No motor control, cloud calls, photo/vector export, or destructive edits.
A result is diagnostic ONLY: no model is activated by this module.
"""
import io
import math
from pathlib import Path

from PIL import Image, ImageOps

MODELS = ("edge_gate", "edge_center", "edge_luma")


def _extract_all(jpeg):
    with Image.open(io.BytesIO(jpeg)) as opened:
        gray = ImageOps.autocontrast(opened.convert("L"), cutoff=1)
        width, height = gray.size
        rects = {
            "edge_gate": (0, 0, width, round(height * 0.64)),
            "edge_center": (round(width * .14), 0, round(width * .87), round(height * .60)),
            "edge_luma": (round(width * .06), 0, round(width * .94), round(height * .58)),
        }
        result = {}
        for mode, rect in rects.items():
            img = gray.crop(rect).resize((48, 24), Image.Resampling.BILINEAR)
            pixel = list(img.getdata())
            # Fixed 8x4 grid of local horizontal/vertical line contrast.
            # Metal gate bars should form a persistent spatial edge pattern.
            sum_x = [0.0] * 32
            sum_y = [0.0] * 32
            sum_l = [0.0] * 32
            count = [0] * 32
            for y in range(1, 23):
                for x in range(1, 47):
                    i = (min(3, y // 6) * 8) + min(7, x // 6)
                    v = pixel[y * 48 + x]
                    sum_x[i] += abs(pixel[y * 48 + x + 1] - pixel[y * 48 + x - 1])
                    sum_y[i] += abs(pixel[(y + 1) * 48 + x] - pixel[(y - 1) * 48 + x])
                    sum_l[i] += v
                    count[i] += 1
            gx = [sum_x[i] / max(1, count[i]) for i in range(32)]
            gy = [sum_y[i] / max(1, count[i]) for i in range(32)]
            gl = [sum_l[i] / max(1, count[i]) for i in range(32)]
            # Normalize contrast independently to reduce sun/shadow changes.
            mx, my = max(1.0, max(gx)), max(1.0, max(gy))
            gx = [round(v / mx, 4) for v in gx]
            gy = [round(v / my, 4) for v in gy]
            # Include coarse brightness only in the third candidate.
            features = gx + gy
            if mode == "edge_luma":
                low, high = min(gl), max(gl)
                features += [round((v - low) / max(1, high - low), 4) for v in gl]
            result[mode] = features
        return result


def _cos(a, b):
    if len(a) != len(b):
        return 0.0
    ma, mb = sum(a) / len(a), sum(b) / len(b)
    dot, aa, bb = 0.0, 0.0, 0.0
    for x, y in zip(a, b):
        x, y = x - ma, y - mb
        dot += x * y
        aa += x * x
        bb += y * y
    return dot / math.sqrt(aa * bb) if aa * bb > 1e-12 else 0.0


def _sessions(rows, gap_seconds=900):
    groups = []
    for row in sorted(rows, key=lambda r: float(r.get("timestamp", 0))):
        t = float(row.get("timestamp", 0))
        if not groups or t - float(groups[-1][-1].get("timestamp", 0)) > gap_seconds:
            groups.append([])
        groups[-1].append(row)
    return groups


class GateShapeProbe:
    """Benchmark candidates on private data, publishing only aggregate scores."""

    def __init__(self, root):
        self.root = Path(root)
        self._cache = {}
        self._last_key = None
        self._last_report = None

    def evaluate(self, rows):
        classes = {label: [row for row in rows
                           if row.get("verified") and row.get("label") == label]
                   for label in ("OPEN", "CLOSED")}
        groups = {k: _sessions(v) for k, v in classes.items()}
        event_counts = {k: len(v) for k, v in groups.items()}
        base = {"status": "INSUFFICIENT_SAMPLES", "model": "none",
                "accuracy_pct": None, "open_recall_pct": None,
                "closed_recall_pct": None, "open_events": event_counts["OPEN"],
                "closed_events": event_counts["CLOSED"], "tested": 0,
                "split": "none", "ready_to_promote": False, "is_dry_run": True}
        if min(map(len, classes.values())) < 6:
            return base
        # Re-use only the latest 40 verified crops of each class to limit CPU/I/O.
        selected = {k: v[-40:] for k, v in classes.items()}
        key = tuple((r["id"], r["label"], r.get("timestamp"))
                    for label in ("OPEN", "CLOSED") for r in selected[label])
        if key == self._last_key and self._last_report is not None:
            return dict(self._last_report)
        split = {}
        proper_holdout = True
        for label in ("OPEN", "CLOSED"):
            sessions = _sessions(selected[label])
            if len(sessions) >= 3 and len(sessions[-1]) >= 2 and sum(
                    len(g) for g in sessions[:-1]) >= 6:
                training = [r for group in sessions[:-1] for r in group]
                testing = sessions[-1][-8:]
            else:
                proper_holdout = False
                n = max(2, math.ceil(len(selected[label]) / 4))
                training = selected[label][:-n]
                testing = selected[label][-n:]
            split[label] = (training[-32:], testing)
        features = {}
        failed_images = 0
        for label in ("OPEN", "CLOSED"):
            for row in split[label][0] + split[label][1]:
                sid = row["id"]
                if sid not in self._cache:
                    try:
                        self._cache[sid] = _extract_all(
                            (self.root / (sid + ".jpg")).read_bytes())
                    except (OSError, ValueError, TypeError):
                        self._cache[sid] = None
                        failed_images += 1
                features[sid] = self._cache[sid]
        results = []
        for model in MODELS:
            prototypes = {}
            for label in ("OPEN", "CLOSED"):
                train = [features[row["id"]][model]
                         for row in split[label][0] if features[row["id"]]]
                if len(train) < 4:
                    break
                prototypes[label] = [sum(v[i] for v in train) / len(train)
                                     for i in range(len(train[0]))]
            if len(prototypes) < 2:
                continue
            checked, correct = {"OPEN": 0, "CLOSED": 0}, {"OPEN": 0, "CLOSED": 0}
            for label in ("OPEN", "CLOSED"):
                for row in split[label][1]:
                    sample = features[row["id"]]
                    if not sample:
                        continue
                    scores = {k: _cos(sample[model], vec)
                              for k, vec in prototypes.items()}
                    best = max(scores, key=scores.get)
                    other = "CLOSED" if best == "OPEN" else "OPEN"
                    checked[label] += 1
                    if (best == label and scores[best] >= .82 and
                            scores[best] - scores[other] >= .045):
                        correct[label] += 1
            if min(checked.values()) < 2:
                continue
            recall = {k: 100 * correct[k] / checked[k] for k in checked}
            total = sum(checked.values())
            accuracy = 100 * sum(correct.values()) / total
            results.append((min(recall.values()), accuracy, model, recall, total))
        if results:
            best = max(results, key=lambda r: (r[0], r[1], r[2] == "edge_gate"))
            minimum, accuracy, model, recall, total = best
            # No promotion from repeated snapshots of one opening event.
            eligible = proper_holdout and accuracy >= 90 and minimum >= 80
            base.update({
                "status": ("CANDIDATE_PASSED" if eligible
                           else "EXPLORATORY_ONLY" if not proper_holdout
                           else "CANDIDATE_FAILED"),
                "model": model,
                "accuracy_pct": round(accuracy, 1),
                "open_recall_pct": round(recall["OPEN"], 1),
                "closed_recall_pct": round(recall["CLOSED"], 1),
                "tested": total,
                "split": "event_holdout" if proper_holdout else "time_sample_holdout",
                "ready_to_promote": eligible,
            })
        else:
            base["status"] = "INSUFFICIENT_VALID_TESTS"
        base["failed_images"] = failed_images
        self._last_key, self._last_report = key, dict(base)
        keep = {r["id"] for label in ("OPEN", "CLOSED")
                for part in split[label] for r in part}
        self._cache = {k: v for k, v in self._cache.items() if k in keep}
        return dict(base)
