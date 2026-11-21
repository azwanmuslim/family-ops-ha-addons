"""Read-only local gate-shape benchmark over retained, owner-verified JPEG crops.

No motor control, cloud calls, photo/vector export, or destructive edits.
A result is diagnostic ONLY: no model is activated by this module.
"""
import io
import math
from pathlib import Path

from PIL import Image, ImageOps

MODELS = ("edge_gate", "edge_center", "edge_luma", "mask_gray", "mask_vertical", "mask_both")


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
        # Pixel-level structural templates; statistical feature selection happens
        # ONLY on training events, never on the withheld validation events.
        base = gray.crop((round(width * .06), 0, round(width * .94),
                          round(height * .66))).resize((48, 24), Image.Resampling.BILINEAR)
        values = list(base.getdata())
        luma = [round(v / 255, 4) for v in values]
        vertical = []
        horizontal = []
        for y in range(24):
            for x in range(48):
                pos = y * 48 + x
                l = values[y * 48 + max(0, x - 1)]
                r = values[y * 48 + min(47, x + 1)]
                t = values[max(0, y - 1) * 48 + x]
                b = values[min(23, y + 1) * 48 + x]
                vertical.append(round(abs(l - r) / 255, 4))
                horizontal.append(round(abs(t - b) / 255, 4))
        result["mask_gray"] = luma
        result["mask_vertical"] = vertical
        result["mask_both"] = [(v + h) * 0.5 for v, h in zip(vertical, horizontal)]
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


def _mask_prototypes(by_label):
    """Learn only features reliably different across classes; no holdout use."""
    arrays = [by_label["OPEN"], by_label["CLOSED"]]
    nfeatures = len(arrays[0][0])
    mean = {}
    variance = {}
    for label, frames in by_label.items():
        count = len(frames)
        mean[label] = [sum(v[i] for v in frames) / count for i in range(nfeatures)]
        variance[label] = [
            sum((v[i] - mean[label][i]) ** 2 for v in frames) / count
            for i in range(nfeatures)]
    importance = []
    for i in range(nfeatures):
        contrast = abs(mean["OPEN"][i] - mean["CLOSED"][i])
        noise = math.sqrt((variance["OPEN"][i] + variance["CLOSED"][i]) / 2)
        importance.append(contrast / (0.08 + noise))
    top_count = max(48, nfeatures // 8)
    top = sorted(range(nfeatures), key=lambda i: importance[i], reverse=True)[:top_count]
    weights = {i: min(12.0, importance[i] ** 2) for i in top if importance[i] > 0.01}
    return mean, weights


def _mask_prediction(sample, prototypes):
    means, weights = prototypes
    if not weights:
        return "UNKNOWN"
    distance = {
        label: sum(w * abs(sample[i] - center[i]) for i, w in weights.items())
        / max(1e-9, sum(weights.values()))
        for label, center in means.items()
    }
    winner = min(distance, key=distance.get)
    loser = "CLOSED" if winner == "OPEN" else "OPEN"
    margin = (distance[loser] - distance[winner]) / max(
        1e-8, distance[loser] + distance[winner])
    return winner if margin >= .08 and distance[winner] <= .34 else "UNKNOWN"


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
        # Stratified in time: historical CLOSED data should cover many lighting
        # conditions rather than just the latest ~20 hours.
        selected = {}
        for label, records in classes.items():
            if len(records) > 64:
                indexes = {round(i * (len(records) - 1) / 63) for i in range(64)}
                selected[label] = [records[i] for i in sorted(indexes)]
            else:
                selected[label] = list(records)
        key = tuple((r["id"], r["label"], r.get("timestamp"))
                    for label in ("OPEN", "CLOSED") for r in selected[label])
        if key == self._last_key and self._last_report is not None:
            return dict(self._last_report)
        split = {}
        proper_holdout = True
        for label in ("OPEN", "CLOSED"):
            sessions = _sessions(selected[label])
            n_events = max(2, math.ceil(len(sessions) / 4))
            if len(sessions) >= 4 and len(sessions) - n_events >= 2:
                training = [r for group in sessions[:-n_events] for r in group]
                testing = [r for group in sessions[-n_events:] for r in group]
                if len(training) < 6 or len(testing) < 2:
                    proper_holdout = False
            else:
                proper_holdout = False
                n = max(2, math.ceil(len(selected[label]) / 4))
                training = selected[label][:-n]
                testing = selected[label][-n:]
            split[label] = (training[-48:], testing[-24:])
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
        candidate_scores = {}
        for model in MODELS:
            by_label = {}
            for label in ("OPEN", "CLOSED"):
                train = [features[row["id"]][model]
                         for row in split[label][0] if features[row["id"]]]
                if len(train) < 4:
                    break
                by_label[label] = train
            if len(by_label) < 2:
                continue
            if model.startswith("mask_"):
                prototypes = _mask_prototypes(by_label)
            else:
                prototypes = {
                    label: [sum(v[i] for v in train) / len(train)
                            for i in range(len(train[0]))]
                    for label, train in by_label.items()
                }
            checked, correct = {"OPEN": 0, "CLOSED": 0}, {"OPEN": 0, "CLOSED": 0}
            for label in ("OPEN", "CLOSED"):
                for row in split[label][1]:
                    sample = features[row["id"]]
                    if not sample:
                        continue
                    if model.startswith("mask_"):
                        predicted = _mask_prediction(sample[model], prototypes)
                    else:
                        scores = {k: _cos(sample[model], vec)
                                  for k, vec in prototypes.items()}
                        best = max(scores, key=scores.get)
                        other = "CLOSED" if best == "OPEN" else "OPEN"
                        predicted = (best if scores[best] >= .82 and
                                     scores[best] - scores[other] >= .045 else "UNKNOWN")
                    checked[label] += 1
                    if predicted == label:
                        correct[label] += 1
            if min(checked.values()) < 2:
                continue
            recall = {k: 100 * correct[k] / checked[k] for k in checked}
            total = sum(checked.values())
            balanced = (recall["OPEN"] + recall["CLOSED"]) / 2
            candidate_scores[model] = {
                "balanced_accuracy_pct": round(balanced, 1),
                "open_recall_pct": round(recall["OPEN"], 1),
                "closed_recall_pct": round(recall["CLOSED"], 1),
            }
            results.append((min(recall.values()), balanced, model, recall, total))
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
                "metric": "balanced_accuracy",
                "ready_to_promote": eligible,
            })
        else:
            base["status"] = "INSUFFICIENT_VALID_TESTS"
        base["candidate_scores"] = candidate_scores
        base["failed_images"] = failed_images
        self._last_key, self._last_report = key, dict(base)
        keep = {r["id"] for label in ("OPEN", "CLOSED")
                for part in split[label] for r in part}
        self._cache = {k: v for k, v in self._cache.items() if k in keep}
        return dict(base)
