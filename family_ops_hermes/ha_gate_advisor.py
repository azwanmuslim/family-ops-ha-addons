#!/usr/bin/env python3
"""Suggestion-only Hermes autogate POC. No actuator/service calls, no image/GPS export.

Reference features are tiny grayscale, gate-area-only thumbnails from the
two owner-supplied 2026-11-17 daylight screenshots, not full camera images.
One closed + one open example is NOT sufficient for production safety: unknown
is the default. Night/rain/obscuration/changed camera pose must be revalidated.
"""
import asyncio
import contextlib
import base64
import datetime as dt
import io
import json
import math
import os
from pathlib import Path
import time
import zlib
from ha_gate_dataset import GateDataset

API = "http://supervisor/core/api"
HEADERS = {"Authorization": "Bearer " + os.environ.get("SUPERVISOR_TOKEN", "")}
STATUS = Path("/tmp/hermes_gate_advisor_status.json")
CAMERA = "camera.nvr_car_porch_cam_sub"
INTERVAL_SECONDS = 120
# ROIs are normalized to the 1536x864 CCTV composition; they exclude public street.
ROIS = {
    "middle": (.39, .66, .42, .68),
}
# Binary feature data only (16x16 grayscale each), not source photos.
TEMPLATES = {
    "middle": {
        "closed": "eNoFwd1OglAAAOCXc8514ZbDQU1nCkZHkf8fTweGRxGDwAOIBzTX5txaF9XWhW/SA/V9f1c8RjkFuReFuigHlhHWNSl4bNrACRboWNEqX+O+rUDkQeSUhKQYPIOvDELkWkoWOZK9+FbPuYcsBUiyNI+z8Ef7zEJdlUTgBuu4plflLanns8n00d/sDu/pr+7IFMvylN/S/HApP2TzNXKtmdBPaHU+JRcTk40x4R/YXVaejvFeD4qlyg8EkBTJyveWvEaQZnnoKY4wVId9TlAMktqD7rYwx0OOaXcR9tQe0/Gle7bTajVuNXHEjiC86zEMe9NoNrk2Z77Eq390PXqH",
        "open": "eNoFwYlOggAAANCvbOXaai5Gil1LRhJ5ICUb0kRdDlCjQLkjOYSKxFaUuvLgj3pvuVnGYeC71rDb6/Otxuzdt13H0fqSzHXZJvU6erJ0TTYCj2cYmqEMVVEUzf9dT9uNBkXWVVlUDHuRrL5ZuoaXi+JAUs1gnSSxVC0VCezh/lEfz5PNXzxhMBhBhaHlRLPlavEZti8vzrKm679FXz+zeWTeluDMvuePph9hOInGEo5A4LZt+i+u57iBK17lC/DpsyHxPYG9E3SJb9WJE1nmaDx/VOYEccBVDlKKRsLHh7kC0eEpLJvZ65A5IA2kt6BrDEHRWhXMITCU2tkFgXP0psmJ/yatmhI=",
    },
}

def _decode(encoded):
    raw = zlib.decompress(base64.b64decode(encoded, validate=True))
    if len(raw) != 256:
        raise ValueError("invalid_reference")
    return raw

def _corr(a, b):
    if len(a) != len(b):
        return 0.0
    ma = sum(a) / len(a)
    mb = sum(b) / len(b)
    aa = [v - ma for v in a]
    bb = [v - mb for v in b]
    den = math.sqrt(sum(x*x for x in aa) * sum(y*y for y in bb))
    return sum(x*y for x,y in zip(aa,bb))/den if den > 1 else 0.0

def classify_jpeg(jpeg):
    """Conservative template POC; unknown on camera changes or weak evidence."""
    if not jpeg.startswith(b"\xff\xd8") or len(jpeg) > 6000000:
        return "UNKNOWN", 0.0
    from PIL import Image, ImageOps
    with Image.open(io.BytesIO(jpeg)) as image:
        width, height = image.size
        if not (640 <= width <= 5000 and 360 <= height <= 3000 and 1.72 < width/height < 1.84):
            return "UNKNOWN", 0.0
        image = ImageOps.exif_transpose(image).convert("L")
        x1,x2,y1,y2 = ROIS["middle"]
        crop = image.crop((round(x1*width),round(y1*height),round(x2*width),round(y2*height)))
        sample = crop.resize((16,16), Image.Resampling.BILINEAR).tobytes()
    score = {k: _corr(sample,_decode(v)) for k,v in TEMPLATES["middle"].items()}
    best = max(score, key=score.get)
    gap = score[best] - score["open" if best=="closed" else "closed"]
    if score[best] < .90 or gap < .12:
        return "UNKNOWN", round(max(0.0,gap),3)
    return best.upper(), round(min(1.0,max(0.0,gap)),3)

def _distance_m(lat, lon, home_lat, home_lon):
    r = 6371000.0
    a = math.sin(math.radians((lat-home_lat)/2))**2
    a += math.cos(math.radians(lat))*math.cos(math.radians(home_lat))*math.sin(math.radians((lon-home_lon)/2))**2
    return 2*r*math.asin(min(1,math.sqrt(a)))

def _number(v):
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError,ValueError):
        return None

class GateAdvisor:
    def __init__(self, owner_send):
        self.owner_send = owner_send
        self.visual_history = []
        self.gps_history = {}  # Per-person, last three accurate GPS observations (no coordinates)
        self.gps_trend = "UNKNOWN"
        self.last_alert = {}
        self.day = ""
        self.sent_today = 0
        self.latest = "UNKNOWN"
        self.enabled = os.environ.get("HA_GATE_ADVISOR", "false").lower() == "true"
        self.dataset = GateDataset()
        self.dashboard_signature = None
        self.dashboard_publish_at = 0.0

    def _save(self, **fields):
        safe = {
            "mode":"suggestion_only","enabled":self.enabled,
            "visual_state":self.latest,"actuator_calls":0,
            "raw_images_exported":False,"raw_gps_exported":False,
            "last_check_utc":dt.datetime.now(dt.timezone.utc).isoformat(),
        }
        safe.update(fields)
        tmp=STATUS.with_suffix(".tmp")
        tmp.write_text(json.dumps(safe, separators=(",",":"))+"\n",encoding="utf-8")
        tmp.chmod(0o600)
        tmp.replace(STATUS)

    def dashboard_states(self, confidence=0.0):
        """Whitelist safe numeric learning stats and visual state only."""
        s = self.dataset.stats()
        ready = min(s["open"], 6) + min(s["closed"], 6)
        progress = round(100 * ready / 12)
        last_capture = s.get("last_sample_utc") or "unknown"
        return {
            "sensor.hermes_gate_learning": {
                "state": ("COLLECTING" if s["enabled"] and ready < 12 else
                          "EVALUATION_PENDING" if s["enabled"] else "DISABLED"),
                "attributes": {
                    "friendly_name": "Hermes Gate Learning",
                    "icon": "mdi:brain",
                    "capture_interval_minutes": 30,
                    "retention_days": 14,
                    "mode": "suggestion_only",
                    "model_accuracy_verified": False,
                },
            },
            "sensor.hermes_gate_samples": {
                "state": str(s["samples"]),
                "attributes": {"friendly_name": "Gate Samples", "icon": "mdi:image-multiple"},
            },
            "sensor.hermes_gate_open_verified": {
                "state": str(s["open"]),
                "attributes": {"friendly_name": "OPEN Disahkan", "icon": "mdi:gate-open"},
            },
            "sensor.hermes_gate_closed_verified": {
                "state": str(s["closed"]),
                "attributes": {"friendly_name": "CLOSED Disahkan", "icon": "mdi:gate"},
            },
            "sensor.hermes_gate_unverified": {
                "state": str(s["unverified"]),
                "attributes": {"friendly_name": "Belum Disahkan", "icon": "mdi:help-circle-outline"},
            },
            "sensor.hermes_gate_training_progress": {
                "state": str(progress),
                "attributes": {"friendly_name": "Cukup Sampel Disahkan",
                               "icon": "mdi:progress-check", "unit_of_measurement": "%"},
            },
            "sensor.hermes_gate_last_capture": {
                "state": last_capture,
                "attributes": {"friendly_name": "Capture Terakhir", "icon": "mdi:camera-clock"},
            },
            "sensor.hermes_gate_visual": {
                "state": self.latest,
                "attributes": {"friendly_name": "Visual Gate (POC)", "icon": "mdi:gate",
                               "confidence_margin": round(float(confidence), 3),
                               "is_physical_sensor": False},
            },
            "sensor.hermes_gate_collection_errors": {
                "state": str(s["errors"]),
                "attributes": {"friendly_name": "Capture Errors", "icon": "mdi:alert-circle-outline"},
            },
        }

    async def publish_dashboard(self, session, confidence=0.0):
        """Push virtual HA states; no external network and NO service/actuator calls."""
        if not self.dataset.enabled:
            return
        payloads = self.dashboard_states(confidence)
        signature = json.dumps(payloads, sort_keys=True)
        now = time.monotonic()
        if signature == self.dashboard_signature and now - self.dashboard_publish_at < 600:
            return
        for entity_id, payload in payloads.items():
            async with session.post(API + "/states/" + entity_id,
                                    headers=HEADERS, json=payload, timeout=18) as resp:
                if resp.status not in (200, 201):
                    raise RuntimeError("dashboard_state_write_failed")
                await resp.read()
        self.dashboard_signature = signature
        self.dashboard_publish_at = now

    async def _states(self,session):
        async with session.get(API+"/states",headers=HEADERS,timeout=18) as r:
            if r.status != 200: raise RuntimeError("states_http")
            data=await r.json()
        if not isinstance(data,list): raise RuntimeError("states_payload")
        return {x.get("entity_id"):x for x in data if isinstance(x,dict)}

    async def _visual(self,session,states):
        cam=states.get(CAMERA,{})
        if cam.get("state") in ("unknown","unavailable",None):
            return "UNKNOWN",0.0
        sun=states.get("sun.sun",{})
        sunlight = sun.get("state") == "above_horizon"
        # Night frames are saved every 30 min, but night classification stays UNKNOWN.
        if not sunlight and not self.dataset.due():
            return "UNKNOWN",0.0
        async with session.get(API+"/camera_proxy/"+CAMERA,headers=HEADERS,timeout=23) as r:
            if r.status != 200: return "UNKNOWN",0.0
            image=await r.read()
            if len(image)>6000000: return "UNKNOWN",0.0
        if self.dataset.due():
            if self.dataset.capture_routine(image, sun.get("state", "unknown")):
                print("HERMES_GATE_DATASET: sample_saved=true", flush=True)
        if not sunlight:
            return "UNKNOWN",0.0
        learned, margin = self.dataset.classify(image)
        if learned in ("OPEN", "CLOSED"):
            return learned, margin
        return classify_jpeg(image)

    @staticmethod
    def _presence(states):
        members=[r for k,r in states.items() if k.startswith("person.")]
        if not members or len(members)<2: return False
        group=states.get("group.family",{}).get("state")
        # Fail closed: unknown person or group makes empty-home suggestion invalid.
        return group=="not_home" and all(r.get("state") not in ("home","unknown","unavailable",None) for r in members)

    def _approaching(self, states, now):
        """Only three distinct, recent, accurate GPS fixes can imply approach.

        Keep short-lived per-person distances, not raw coordinates. A nearby
        location alone (or a repeat of an unchanged HA timestamp) is not motion.
        """
        self.gps_trend = "UNKNOWN"
        home = (states.get("zone.home", {}).get("attributes") or {})
        hl, hn = _number(home.get("latitude")), _number(home.get("longitude"))
        if hl is None or hn is None:
            self.gps_history.clear()
            return None

        for pid, person in states.items():
            if not pid.startswith("person."):
                continue
            if person.get("state") in ("home", "unknown", "unavailable", None):
                self.gps_history.pop(pid, None)
                continue

            attrs = person.get("attributes") or {}
            source = attrs.get("source")
            tracker = (states.get(source, {}) if isinstance(source, str)
                       and source.startswith("device_tracker.") else {})
            gpsattrs = tracker.get("attributes") or {}
            # Match coordinates, accuracy and timestamp to the same source.
            tracker_lat = _number(gpsattrs.get("latitude"))
            tracker_lon = _number(gpsattrs.get("longitude"))
            if tracker_lat is not None and tracker_lon is not None:
                lat, lon = tracker_lat, tracker_lon
                accuracy = _number(gpsattrs.get("gps_accuracy"))
                updated = tracker.get("last_updated")
            else:
                lat = _number(attrs.get("latitude"))
                lon = _number(attrs.get("longitude"))
                accuracy = _number(attrs.get("gps_accuracy"))
                updated = person.get("last_updated")

            # Missing/poor accuracy is not evidence of movement.
            if (lat is None or lon is None or accuracy is None
                    or not 0 <= accuracy <= 50):
                self.gps_history.pop(pid, None)
                continue
            try:
                fix_time = dt.datetime.fromisoformat(str(updated).replace("Z", "+00:00"))
                if fix_time.tzinfo is None:
                    raise ValueError("gps_timestamp_without_timezone")
                age = (dt.datetime.now(dt.timezone.utc) - fix_time).total_seconds()
                if not -60 <= age <= 300:
                    self.gps_history.pop(pid, None)
                    continue
            except (ValueError, TypeError):
                self.gps_history.pop(pid, None)
                continue

            distance = _distance_m(lat, lon, hl, hn)
            if distance > 5000:
                self.gps_history.pop(pid, None)
                continue
            history = self.gps_history.get(pid, [])
            if history and fix_time <= history[-1]["updated"]:
                # An unchanged source timestamp is NOT another GPS sample.
                if distance <= 800 and self.gps_trend == "UNKNOWN":
                    self.gps_trend = "NEAR_HOME"
                continue
            if history and (fix_time - history[-1]["updated"]).total_seconds() > 600:
                history = []
            history = (history + [{"updated": fix_time, "distance": distance,
                                  "accuracy": accuracy}])[-3:]
            self.gps_history[pid] = history
            if distance <= 800 and self.gps_trend == "UNKNOWN":
                self.gps_trend = "NEAR_HOME"
            if len(history) < 3:
                continue

            first, middle, latest = history
            elapsed = (latest["updated"] - first["updated"]).total_seconds()
            gaps = [(middle["updated"] - first["updated"]).total_seconds(),
                    (latest["updated"] - middle["updated"]).total_seconds()]
            decreases = [first["distance"] - middle["distance"],
                         middle["distance"] - latest["distance"]]
            # Three stable nearby fixes should never become 'approaching'.
            if (distance <= 800 and
                    max(s["distance"] for s in history) -
                    min(s["distance"] for s in history) <= 60):
                self.gps_trend = "STATIONARY"
                continue
            if not (120 <= distance <= 800 and 120 <= elapsed <= 900
                    and all(45 <= gap <= 600 for gap in gaps)
                    and all(step >= 60 for step in decreases)
                    and sum(decreases) >= 150):
                continue

            self.gps_trend = "APPROACHING"
            return {
                "name": str(attrs.get("friendly_name") or "Ahli keluarga")[:50],
                "from_m": round(first["distance"] / 10) * 10,
                "to_m": round(distance / 10) * 10,
                "minutes": round(elapsed / 60, 1),
                "accuracy_m": round(accuracy),
                "samples": 3,
            }
        return None

    async def check_once(self,session):
        now=time.monotonic()
        date=dt.datetime.now().strftime("%Y-%m-%d")
        if date!=self.day:
            self.day=date
            self.sent_today=0
        try:
            states=await self._states(session)
            current, confidence=await self._visual(session,states)
        except Exception:
            self.latest="UNKNOWN"
            self.visual_history=[]
            self._save(reason="sensor_read_failed")
            with contextlib.suppress(Exception):
                await self.publish_dashboard(session)
            return
        self.latest=current
        self.visual_history.append((now,current))
        self.visual_history=self.visual_history[-4:]
        recent=self.visual_history[-3:]
        stable=(len(recent)==3 and recent[-1][0]-recent[0][0]>=210
                and current in ("CLOSED","OPEN") and all(s==current for _,s in recent))
        away=self._presence(states)
        arriving=self._approaching(states,now)
        self._save(stable=stable,confidence_margin=confidence,people_all_away=away,
                   gps_approaching=bool(arriving), gps_trend=self.gps_trend)
        with contextlib.suppress(Exception):
            await self.publish_dashboard(session, confidence)
        if not self.enabled or not stable or self.sent_today>=4:
            return
        key=None
        message=""
        if current=="OPEN" and away:
            key="open_when_away"
            message=("🤖 Hermes Gate Advisor (POC)\nCCTV nampak gate TERBUKA dan semua "
                     "presence keluarga di HA menunjukkan away. Cadangan: semak dan tutup gate "
                     "jika laluan selamat.\nNota: GPS bukan bukti rumah benar-benar kosong. "
                     "Tiada arahan gate dihantar.")
        elif current=="CLOSED" and arriving:
            key="closed_on_approach"
            message=(f"🤖 Hermes Gate Advisor (POC)\n"
                     f"{arriving['name']} dikesan menghampiri rumah (3 bacaan GPS baharu).\n"
                     f"Jarak GPS anggaran: {arriving['from_m']}m → {arriving['to_m']}m "
                     f"dalam {arriving['minutes']} minit; ketepatan terkini "
                     f"±{arriving['accuracy_m']}m. Ini jarak garis lurus, bukan laluan jalan.\n"
                     "CCTV nampak gate TERTUTUP. Cadangan: pertimbangkan buka gate "
                     "apabila sesuai.\nTiada arahan gate dihantar.")
        if not key: return
        if now-self.last_alert.get(key, -1e12)<3600: return
        try:
            await self.owner_send(session,message)
        except Exception:
            return
        self.last_alert[key]=now
        self.sent_today+=1
        self._save(stable=stable,last_suggestion=key,notifications_today=self.sent_today)

    async def loop(self,session):
        if not self.enabled:
            self._save(reason="disabled_by_option")
            return
        await asyncio.sleep(60)
        while True:
            await self.check_once(session)
            await asyncio.sleep(INTERVAL_SECONDS)
