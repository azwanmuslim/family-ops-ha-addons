#!/usr/bin/env python3
"""Least-privilege behavior layer for the existing HA-owned Telegram bridge.

- Read live states without trusting LLM guesses.
- Permit only light/fan on/off with a short owner-only confirmation challenge.
- Never expose arbitrary service calls, lock/cover/security control or scripts.
- Provide debounced, capped proactive warnings; no automated device changes.
"""
import asyncio
import datetime as dt
import os
import re
import secrets
import time
from collections import defaultdict

API = "http://supervisor/core/api"
HEADERS = {"Authorization": "Bearer " + os.environ.get("SUPERVISOR_TOKEN", "")}
UNSAFE = re.compile(r"(gate|pagar|garage|garaj|autogate|lock|kunci|alarm|security|camera|cctv|nvr|restart|reboot|siren|pump|motor|power|server|wifi|network|router|infrared|illuminator|cloud|motion|disarm|all_switch|essential_switch|day_switch)", re.I)
WRITE = re.compile(r"^(?:(?:hai|hermes|tolong|boleh|please|cuba)\s+)*(hidupkan|nyalakan|pasang|on|turn on|light on|switch on|matikan|padamkan|tutup|off|turn off|light off|switch off|buka|bukakan|open|close|unlock|lock)\s+(.+?)[?.!]*$", re.I)
READ = {
    "sensor.memory_use_percent": "RAM",
    "sensor.processor_use": "CPU",
    "sensor.cpu_temp": "Suhu CPU",
    "sensor.disk_use_percent": "Disk",
}
LIMIT = 1800

class HomeTools:
    def __init__(self, owner_send):
        self.owner_send = owner_send
        self.pending = None
        self.active_conditions = {}
        self.alerted_conditions = set()
        self.alert_day = None
        self.alert_count = 0
        self.alert_recent = {}

    async def states(self, session):
        async with session.get(API + "/states", headers=HEADERS, timeout=18) as resp:
            if resp.status != 200:
                raise RuntimeError("ha_read_unavailable")
            data = await resp.json()
        if not isinstance(data, list):
            raise RuntimeError("ha_invalid_states")
        return {str(row.get("entity_id")): row for row in data if isinstance(row, dict)}

    async def entity(self, session, entity_id):
        if not re.fullmatch(r"[a-z_]+\.[a-z0-9_]+", entity_id):
            raise RuntimeError("bad_entity")
        async with session.get(API + "/states/" + entity_id, headers=HEADERS, timeout=15) as resp:
            if resp.status != 200:
                raise RuntimeError("ha_entity_unavailable")
            data = await resp.json()
        return data

    @staticmethod
    def friendly(row):
        return str((row.get("attributes") or {}).get("friendly_name") or row.get("entity_id") or "Device").strip()[:70]

    @staticmethod
    def state(row):
        return str(row.get("state") or "unknown")

    @staticmethod
    def gate_items(states):
        results = []
        for entity_id, row in states.items():
            domain = entity_id.split(".",1)[0]
            if domain not in ("cover", "binary_sensor", "sensor"):
                continue
            label = (entity_id + " " + HomeTools.friendly(row)).lower()
            attr = row.get("attributes") or {}
            words = re.sub(r"[^a-z0-9]+", " ", label)
            if re.search(r"\b(?:gate|autogate|garage|garaj|pagar)\b", words) or (
                domain == "binary_sensor" and attr.get("device_class") == "garage_door"
            ):
                results.append((entity_id, row))
        return results

    @staticmethod
    def camera_items(states):
        return [(k, v) for k, v in states.items() if k.startswith("camera.")]

    def metrics(self, states):
        parts = []
        for entity_id, label in READ.items():
            if entity_id not in states:
                continue
            row = states[entity_id]
            unit = (row.get("attributes") or {}).get("unit_of_measurement") or ""
            value = self.state(row)
            if value in ("unknown","unavailable"):
                continue
            parts.append(f"{label}: {value}{unit}")
        return " | ".join(parts) or "Sensor RAM/CPU/disk belum tersedia."

    def summary(self, states):
        people = [(self.friendly(row),self.state(row)) for entity,row in states.items() if entity.startswith("person.")]
        cams = self.camera_items(states)
        available = sum(self.state(r) not in ("unavailable","unknown") for _,r in cams)
        home = [name for name, state in people if state=="home"]
        people_text = (", ".join(home[:8]) if home else "tiada person ditandakan home") if people else "tiada data person"
        return (f"🏠 Ringkasan HA sekarang\n"
                f"Orang di rumah: {people_text}\n"
                f"CCTV: {available}/{len(cams)} entity tersedia (bukan ujian stream)\n"
                f"{self.metrics(states)}")

    def camera_summary(self, states):
        cams = self.camera_items(states)
        if not cams:
            return "Belum ada camera entity dalam Home Assistant."
        missing = [self.friendly(row) for _,row in cams if self.state(row) in ("unavailable","unknown")]
        available = len(cams) - len(missing)
        out = f"📷 CCTV: {available}/{len(cams)} entity tersedia."
        if missing:
            out += "\nTidak tersedia: " + ", ".join(missing[:6])
            if len(missing)>6: out += f" (+{len(missing)-6} lagi)"
        return out + "\nNota: Entity tersedia tidak menjamin RTSP/stream sedang berfungsi."

    def gate_summary(self, states):
        gates = self.gate_items(states)
        if not gates:
            return "Aku tak jumpa sensor/entity gate dalam HA. Belum boleh sahkan gate tutup atau buka."
        return "🚧 Status gate:\n" + "\n".join(
            f"• {self.friendly(row)}: {self.state(row)}" for _,row in gates[:8]
        ) + "\nSemakan sahaja; tiada arahan buka/tutup dihantar."

    def light_summary(self, states):
        # Real household lights are commonly switch.*, not light.*.
        lights=[(eid,row) for eid,row in states.items()
                if ((eid.startswith("light.") or
                     (eid.startswith("switch.") and re.search(r"lamp\d*$",eid,re.I)))
                    and not UNSAFE.search(eid+" "+self.friendly(row)))]
        on=[self.friendly(row) for _,row in lights if self.state(row)=="on"]
        unknown=[1 for _,row in lights if self.state(row) in ("unknown","unavailable")]
        result=f"💡 Lampu rumah: {len(on)} ON daripada {len(lights)} entity lampu (switch + light)."
        result+=("\nSedang ON: "+", ".join(on[:15])) if on else "\nTiada lampu rumah yang ditanda ON."
        if unknown: result+=f"\n{len(unknown)} lampu tak dapat dibaca."
        return result+"\nLampu infrared kamera tidak dikira lampu rumah."

    @staticmethod
    def safe_actuator(entity_id,row):
        domain=entity_id.split(".",1)[0]
        if domain not in ("light","fan","switch"): return False
        if UNSAFE.search(entity_id+" "+HomeTools.friendly(row)): return False
        if domain=="switch" and not re.search(r"(lamp\d*|fan\d*|ac)$",entity_id,re.I): return False
        return HomeTools.state(row) not in ("unknown","unavailable")

    def full_inventory(self,states):
        counts={}
        for entity in states:
            domain=entity.split(".",1)[0]
            counts[domain]=counts.get(domain,0)+1
        result=", ".join(f"{k}: {v}" for k,v in sorted(counts.items(),key=lambda x:(-x[1],x[0])))
        return f"HA live: {len(states)} entity, {len(counts)} domain.\n{result}\n\nGuna /entities switch, /entities sensor atau /entity switch.doorlamp untuk butiran."

    def entity_list(self,states,query,page=1):
        query=query.strip().lower()
        domain_exact=any(k.startswith(query+".") for k in states)
        filtered=[(k,r) for k,r in states.items() if
                   (k.startswith(query+".") if domain_exact else
                    query in (k+" "+self.friendly(r)).lower())]
        if not filtered: return "Tiada entity sepadan. Cuba /entities."
        page=max(1,min(int(page),max(1,(len(filtered)+14)//15)))
        items=filtered[(page-1)*15:page*15]
        return (f"🔎 {len(filtered)} entity untuk '{query}', page {page}.\n"+
                "\n".join(f"• {self.friendly(r)}: {self.state(r)} ({k})" for k,r in items)[:3400])

    def exact_entity(self,states,name):
        target=re.sub(r"^(lampu|light|kipas|fan|aircond|ac|suis|switch)\s+","",name.strip(),flags=re.I).lower().strip()
        norm=lambda x:" ".join(re.sub(r"[^a-z0-9]+"," ",str(x).lower().replace("_"," ")).split())
        aliases={"switch.doorlamp":{"door lamp","doorlamp","lampu pintu luar","pintu luar","lampu pintu","lampu depan"},
                 "switch.carporchlamp":{"car porch lamp","lampu porch","lampu car porch","porch lamp","lampu parking"},
                 "switch.drykitchenlamp":{"lampu dapur kering","dry kitchen lamp"},
                 "switch.wetkitchenlamp":{"lampu dapur basah","wet kitchen lamp"}}
        requested=norm(target)
        candidates=[]
        for eid,row in states.items():
            friendly=self.friendly(row)
            keys={norm(eid),norm(eid.split(".",1)[-1]),norm(friendly)}
            keys.update(norm(alias) for alias in aliases.get(eid,set()))
            if requested in keys: candidates.append((eid,row))
        return candidates

    def entity_state(self,states,name):
        matches=self.exact_entity(states,name)
        if len(matches)==1:
            eid,row=matches[0]
            unit=(row.get("attributes") or {}).get("unit_of_measurement") or ""
            return f"{self.friendly(row)}: {self.state(row)}{unit if len(str(unit))<15 else ''} ({eid})"
        if len(matches)>1:
            return "Ada beberapa entity nama serupa:\n"+"\n".join(f"• {self.friendly(r)} ({eid}): {self.state(r)}" for eid,r in matches[:12])
        return self.entity_list(states,name)


    def exact_safe_target(self,target,states):
        matches=self.exact_entity(states,target)
        safe=[(eid,row) for eid,row in matches if self.safe_actuator(eid,row)]
        if len(safe)==1 and len(matches)==1:
            return safe[0],None
        if len(matches)>1:
            return None,"Nama entity tak unik. Gunakan entity_id penuh; contoh switch.doorlamp."
        return None,"Entity tak dijumpai atau kawalan disekat. Cuba /entities switch atau /entity switch.doorlamp."

    async def handle(self, session, prompt):
        text = str(prompt).strip()
        lower=text.lower()
        if not text: return "/ai <soalan> untuk bersembang dengan Hermes."
        if lower.startswith(("/confirm","confirm ","sahkan ","/sahkan")):
            code=text.split(maxsplit=1)[-1].strip().upper() if " " in text else ""
            pending=self.pending
            self.pending=None  # one attempt, then invalidate even if wrong
            if not pending or time.monotonic()>pending["expires"] or code != pending["code"]:
                return "Tiada arahan sah yang menunggu, kod salah atau sudah tamat (90 saat)."
            entity_id,domain,service=pending["entity"],pending["domain"],pending["service"]
            if domain not in ("light","fan","switch") or service not in ("turn_on","turn_off"):
                return "Arahan disekat oleh safety policy."
            try:
                current=await self.entity(session, entity_id)
                if not self.safe_actuator(entity_id,current):
                    return "Arahan disekat oleh safety policy."
                async with session.post(API+"/services/"+domain+"/"+service,
                    headers=HEADERS, json={"entity_id":entity_id},timeout=20) as resp:
                    if resp.status != 200: return "HA menolak arahan. Tiada pengesahan tindakan."
                    await resp.read()
                await asyncio.sleep(1)
                newer=await self.entity(session,entity_id)
                expected="on" if service=="turn_on" else "off"
                actual=self.state(newer)
                return (f"✅ {self.friendly(newer)}: {actual.upper()} (disahkan HA)." if actual==expected
                        else f"HA terima arahan, tapi status sekarang {actual}. Sila semak peranti.")
            except Exception:
                return "HA tidak dapat mengesahkan tindakan. Sila semak aplikasi HA."
        if lower in ("/cancel","cancel","batal","/batal"):
            self.pending=None
            return "Arahan kawalan dibatalkan."
        if lower in ("/hahelp","/homehelp","/rumahhelp"):
            return ("Hermes rumah:\n/rumah — ringkasan\n/ram — prestasi RPi4\n/cctv — status entity kamera\n"
                    "/gate — sensor pagar\n/lampu — light/fan\n"
                    "Hidupkan lampu <nama tepat> → minta /confirm KOD.\n"
                    "Gate/kunci/alarm tidak dikawal melalui AI.")
        # Prevent a model from construing unsafe device-control language as authorization.
        m=WRITE.match(text)
        if m:
            verb,target=m.groups()
            if (any(term in (target+" "+verb).lower() for term in ("gate","autogate","garaj","garage","pagar","kunci","lock","alarm","cctv","camera","router","reboot")) or
                ("pintu" in target.lower() and not any(x in target.lower() for x in ("lampu","lamp","light","pintu luar")))):
                return ("Demi keselamatan, Hermes tak dibenarkan kawal gate, kunci, alarm "
                        "atau sistem kritikal. Boleh semak status melalui /gate.")
            verb=verb.lower()
            if verb not in ("hidupkan","nyalakan","pasang","on","turn on","light on","switch on","matikan","padamkan","tutup","off","turn off","light off","switch off","buka","bukakan"):
                return "Arahan kawalan ini belum dibenarkan. Hanya light/fan ON/OFF."
            try: states=await self.states(session)
            except Exception: return "Tak dapat akses status HA sekarang. Tiada arahan dihantar."
            item,error=self.exact_safe_target(target,states)
            if error: return error
            entity,row=item
            domain=entity.split(".",1)[0]
            service="turn_off" if verb in ("matikan","padamkan","tutup","off","turn off","light off","switch off") else "turn_on"
            code=secrets.token_hex(3).upper()
            self.pending={"code":code,"entity":entity,"domain":domain,"service":service,"expires":time.monotonic()+90}
            label=self.friendly(row)
            action="MATIKAN" if service=="turn_off" else "HIDUPKAN"
            return (f"🔒 Sahkan: {action} {label} ({entity})?\n"
                    f"Balas /confirm {code} dalam 90 saat. /cancel untuk batal.")
        # Dynamic catalogue; every HA domain is readable (not automatically controllable).
        if lower in ("/entities","/devices","senarai entity","semua entity","list entities","tunjuk semua entity"):
            try: return self.full_inventory(await self.states(session))
            except Exception: return "Tak dapat ambil katalog HA live."
        m=re.fullmatch(r"/(?:entities|devices)\s+([a-z0-9_\-. ]+)",lower)
        if m:
            tokens=m.group(1).rsplit(" ",1)
            page=int(tokens[1]) if len(tokens)==2 and tokens[1].isdigit() else 1
            query=tokens[0] if len(tokens)==2 and tokens[1].isdigit() else m.group(1)
            try: return self.entity_list(await self.states(session),query,page)
            except Exception: return "Tak dapat ambil senarai HA."
        m=re.fullmatch(r"/(?:entity|entiti)\s+([a-z_]+\.[a-z0-9_]+)",lower)
        if m:
            try: return self.entity_state(await self.states(session),m.group(1))
            except Exception: return "Tak dapat baca entity HA."
        m=re.match(r"^(?:cari|search|senarai|list|tunjuk|show)\s+([a-z][a-z0-9 _-]+)$",lower)
        if m:
            try: return self.entity_list(await self.states(session),m.group(1))
            except Exception: return "Tak dapat cari entity HA."
        entity_match=re.search(r"\b[a-z_]+\.[a-z0-9_]+\b",lower)
        if entity_match:
            try: return self.entity_state(await self.states(session),entity_match.group(0))
            except Exception: return "Tak dapat baca status entity HA."
        if (re.search(r"\b(lampu|lights?|lamps?)\b",lower) and
            re.search(r"(mana|menyala|nyala|on|off|terbuka|hidup|padam|status|ada|berapa|senarai|suis|switch)",lower)):
            try: return self.light_summary(await self.states(session))
            except Exception: return "Tak dapat baca lampu HA live."
        if re.search(r"\b(gate|autogate|pagar)\b",lower) and re.search(r"\b(status|keadaan|tutup|buka)\b",lower):
            try: return self.gate_summary(await self.states(session))
            except Exception: return "Tidak dapat baca status gate HA."
        m=re.match(r"^(?:(?:apa|bagaimana|boleh semak|tolong semak|check)\s+)?(?:status|keadaan|bacaan|state|nilai)\s+(.+?)\s*[?.!]*$",lower)
        if m:
            try: return self.entity_state(await self.states(session),m.group(1))
            except Exception: return "Tak dapat baca status HA."
        kind=None
        if lower in ("/rumah","/home","/statusrumah") or re.search(r"(status\s+rumah|ringkasan\s+rumah|siapa.*(?:rumah|home)|orang.*(?:rumah|home))",lower): kind="home"
        elif lower in ("/ram","/cpu","/prestasi") or re.search(r"\b(ram|cpu|memory|memori|disk|storage|storaj|suhu\s+(?:cpu|rpi|pi))\b",lower): kind="metrics"
        elif lower in ("/cctv","/camera") or re.search(r"\b(cctv|kamera|camera|nvr|rtsp)\b",lower): kind="camera"
        elif lower in ("/gate","/pagar") or re.search(r"\b(gate|autogate|garaj|garage|pagar)\b",lower): kind="gate"
        elif lower in ("/lampu","/lights","/peranti") or re.search(r"(lampu.*(?:on|hidup|status)|(?:status|berapa).*lampu|senarai lampu)",lower): kind="lights"
        if kind:
            try: states=await self.states(session)
            except Exception: return "Maaf, HA Core tak dapat berikan bacaan live sekarang."
            return {"home":self.summary,"metrics":self.metrics,"camera":self.camera_summary,
                    "gate":self.gate_summary,"lights":self.light_summary}[kind](states)
        return None

    async def proactive_once(self, session):
        """Poll current HA states with stable windows; never take device action."""
        try: states=await self.states(session)
        except Exception: return
        now=time.monotonic()
        conditions={}
        ram=states.get("sensor.memory_use_percent")
        if ram:
            try:
                val=float(self.state(ram))
                if 85 <= val <= 100:
                    conditions["ram:high"]=("RAM HA tinggi", f"{val:.1f}%", 300)
            except ValueError: pass
        for entity,row in self.camera_items(states):
            if self.state(row) in ("unavailable","unknown"):
                conditions["camera:"+entity]=("Kamera HA tidak tersedia",self.friendly(row),600)
        for entity,row in self.gate_items(states):
            domain=entity.split(".",1)[0]
            cls=(row.get("attributes") or {}).get("device_class")
            is_open=(domain=="cover" and self.state(row)=="open") or (
                domain=="binary_sensor" and cls=="garage_door" and self.state(row)=="on")
            if is_open:
                conditions["gate:"+entity]=("Gate masih terbuka", self.friendly(row),600)
        for key in list(self.active_conditions):
            if key not in conditions:
                self.active_conditions.pop(key,None)
                self.alerted_conditions.discard(key)
        day=dt.datetime.now(dt.timezone.utc).date()
        if day != self.alert_day:
            self.alert_day=day
            self.alert_count=0
        for key,(title,description,duration) in sorted(conditions.items(),key=lambda kv:(not kv[0].startswith("gate:"),kv[0])):
            self.active_conditions.setdefault(key,now)
            if key in self.alerted_conditions or now-self.active_conditions[key]<duration:
                continue
            if self.alert_count >= 3: continue
            message=f"🤖 Hermes Alert: {title}\n{description}\nKeadaan berterusan sekurang-kurangnya {duration//60} minit. Semak di HA."
            try:
                await self.owner_send(session,message)
                self.alerted_conditions.add(key)
                self.alert_count += 1
            except Exception:
                pass

    async def proactive_loop(self, session):
        await asyncio.sleep(40)
        while True:
            await self.proactive_once(session)
            await asyncio.sleep(120)
