#!/usr/bin/env python3
"""Suggestion-only Hermes autogate POC. No actuator/service calls, no image/GPS export.

Reference features are tiny grayscale, gate-area-only thumbnails from the
two owner-supplied 2026-11-17 daylight screenshots, not full camera images.
One closed + one open example is NOT sufficient for production safety: unknown
is the default. Night/rain/obscuration/changed camera pose must be revalidated.
"""
import asyncio
import base64
import datetime as dt
import io
import json
import math
import os
from pathlib import Path
import time
import zlib

API = "http://supervisor/core/api"
HEADERS = {"Authorization": "Bearer " + os.environ.get("SUPERVISOR_TOKEN", "")}
STATUS = Path("/tmp/hermes_gate_advisor_status.json")
CAMERA = "camera.nvr_car_porch_cam_sub"
INTERVAL_SECONDS = 120
# ROIs are normalized to the 1536x864 CCTV composition; they exclude public street.
ROIS = {
    "middle": (.39, .66, .42, .68),
    "left": (.31, .58, .37, .76),
}
# Binary feature data only (16x16 grayscale each), not source photos.
TEMPLATES = {
    "middle": {
        "closed": "eNoFwd1OglAAAOCXc8514ZbDQU1nCkZHkf8fTweGRxGDwAOIBzTX5txaF9XWhW/SA/V9f1c8RjkFuReFuigHlhHWNSl4bNrACRboWNEqX+O+rUDkQeSUhKQYPIOvDELkWkoWOZK9+FbPuYcsBUiyNI+z8Ef7zEJdlUTgBuu4plflLanns8n00d/sDu/pr+7IFMvylN/S/HApP2TzNXKtmdBPaHU+JRcTk40x4R/YXVaejvFeD4qlyg8EkBTJyveWvEaQZnnoKY4wVId9TlAMktqD7rYwx0OOaXcR9tQe0/Gle7bTajVuNXHEjiC86zEMe9NoNrk2Z77Eq390PXqH",
        "open": "eNoFwYlOggAAANCvbOXaai5Gil1LRhJ5ICUb0kRdDlCjQLkjOYSKxFaUuvLgj3pvuVnGYeC71rDb6/Otxuzdt13H0fqSzHXZJvU6erJ0TTYCj2cYmqEMVVEUzf9dT9uNBkXWVVlUDHuRrL5ZuoaXi+JAUs1gnSSxVC0VCezh/lEfz5PNXzxhMBhBhaHlRLPlavEZti8vzrKm679FXz+zeWTeluDMvuePph9hOInGEo5A4LZt+i+u57iBK17lC/DpsyHxPYG9E3SJb9WJE1nmaDx/VOYEccBVDlKKRsLHh7kC0eEpLJvZ65A5IA2kt6BrDEHRWhXMITCU2tkFgXP0psmJ/yatmhI=",
    },
    "left": {
        "closed": "eNoBAAH//uTl5+jo6Ojn6Ofm4NK4moTk5OTk4+Tl4NC4nIN5g4WV39/g3M64oIuEgo+Fbo6Ahcy6o4yGiI2Vl4+LfGJ+b3V1hpCQk5SPiYJ4d4duq4F6eIWLhYF9dXWAhpOkcK6GhG5vcHB2fYOIkZmaqW2cf5lubnh+gYyRlJufk6dmeH+Rem9/iI6Sk5egnYqeapGKhX9ofIWNjZCOjYt7jF+Ihn5/aHeAg4qMfXJrXGBRbHJ+aFpocHiDdYNejetfaVYOMFDGED?ij[aVRbv f".replace(" ",""),
        "open": "eNoBAAH//tTU1dbW19bW1tTU09HNycHT09PS0NLT1NPRzMbBurOtzczNzMvMy8bAu7i1sKmkn8jIyMXDu7Kvr62sqauurZu9ubWsqqunpqeprL/MzbaQoaKjoaKlpKOprLzQ0c++mpmipKKenqGlrrrK0c/Jwrmgn5manamxtLm/xsnJx72tmJydpqu0s7W+vLq4vbyznZmVl6GprauopaSmoKOrrZ+Vk5KYm6imkYN8cHGHlqCgd3Z8g4ycjHFoZmRlYnuLhnFzb2pldXt6dWVbWlpgZnVgYGRga4mIe3JzWE9QWW+EVFJXYHJ2d2trZFdZbH+RnGdbWmNfXWN2fX5/jaHVmJ3q",
    },
}
# POC: middle ROI is calibrated; a secondary ROI may be added after more samples.
# The left gate area is intentionally not used until a second calibration proves
# stable. Image classification NEVER authorizes gate actuation.
TEMPLATES["left"] = {}

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
    from PIL import Image, ImageOps
    if not jpeg.startswith(b"\xff\xd8") or len(jpeg) > 6000000:
        return "UNKNOWN", 0.0
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
        self.gps_history = {}
        self.last_alert = {}
        self.day = ""
        self.sent_today = 0
        self.latest = "UNKNOWN"
        self.enabled = os.environ.get("HA_GATE_ADVISOR", "false").lower() == "true"

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
        if sun.get("state") != "above_horizon":
            return "UNKNOWN",0.0
        async with session.get(API+"/camera_proxy/"+CAMERA,headers=HEADERS,timeout=23) as r:
            if r.status != 200: return "UNKNOWN",0.0
            image=await r.read()
            if len(image)>6000000: return "UNKNOWN",0.0
        return classify_jpeg(image)

    @staticmethod
    def _presence(states):
        members=[r for k,r in states.items() if k.startswith("person.")]
        if not members or len(members)<2: return False
        group=states.get("group.family",{}).get("state")
        # Fail closed: unknown person or group makes empty-home suggestion invalid.
        return group=="not_home" and all(r.get("state") not in ("home","unknown","unavailable",None) for r in members)

    def _approaching(self,states,now):
        home=(states.get("zone.home",{}).get("attributes") or {})
        hl,hn=_number(home.get("latitude")),_number(home.get("longitude"))
        if hl is None or hn is None: return None
        winner=None
        for pid,p in states.items():
            if not pid.startswith("person.") or p.get("state") in ("home","unknown","unavailable",None):
                continue
            attrs=p.get("attributes") or {}
            source=attrs.get("source")
            gps=states.get(source,{}) if isinstance(source,str) and source.startswith("device_tracker.") else {}
            gpsattrs=gps.get("attributes") or {}
            lat,lon=_number(attrs.get("latitude")),_number(attrs.get("longitude"))
            accuracy=_number(attrs.get("gps_accuracy"))
            if lat is None or lon is None:
                lat,lon=_number(gpsattrs.get("latitude")),_number(gpsattrs.get("longitude"))
                accuracy=_number(gpsattrs.get("gps_accuracy"))
            if lat is None or lon is None or (accuracy is not None and accuracy>150):
                continue
            updated=gps.get("last_updated") if gps else p.get("last_updated")
            try:
                last=dt.datetime.fromisoformat(str(updated).replace("Z","+00:00"))
                age=(dt.datetime.now(dt.timezone.utc)-last).total_seconds()
                if age< -90 or age>600: continue
            except (ValueError,TypeError):
                continue
            dist=_distance_m(lat,lon,hl,hn)
            if dist > 5000:  # don't retain distant locations
                self.gps_history.pop(pid,None)
                continue
            previous=self.gps_history.get(pid)
            self.gps_history[pid]=(now,dist)
            if not previous: continue
            dt_sec=now-previous[0]
            moved=previous[1]-dist
            if 60<=dt_sec<=900 and 120<=dist<=800 and 80<=moved<=3000:
                # No names or exact coordinates in GitHub/logs; only owner's
                # Telegram sees this short name.
                winner=str((p.get("attributes") or {}).get("friendly_name") or "Ahli keluarga")[:50]
                break
        return winner

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
                   gps_approaching=bool(arriving))
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
            message=(f"🤖 Hermes Gate Advisor (POC)\n{arriving} dikesan menghampiri rumah "
                     "berdasarkan beberapa bacaan GPS, dan CCTV nampak gate TERTUTUP. "
                     "Cadangan: pertimbangkan buka gate apabila sesuai.\n"
                     "Tiada arahan gate dihantar.")
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
