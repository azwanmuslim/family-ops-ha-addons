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
        self.pending_selection = None
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

    async def gate_summary_live(self, session, states):
        advisor = getattr(self, "gate_advisor", None)
        if advisor is not None:
            try:
                advisor.latest, margin = await advisor._visual(session, states)
                await advisor.publish_dashboard(session, margin)
            except Exception:
                advisor.latest = "UNKNOWN"
        return self.gate_summary(states)

    def gate_summary(self, states):
        advisor = getattr(self, "gate_advisor", None)
        visual = ("📷 Visual gate (anggaran CCTV): " + advisor.latest + "\n"
                  if advisor is not None else "📷 Visual gate: belum tersedia\n")
        if advisor is not None and getattr(advisor, "vision_diagnostic", {}).get("provisional"):
            visual += "⚠️ Anggaran POC sementara; paparan sahaja, bukan keputusan sah atau cadangan tindakan.\n"
        gates = self.gate_items(states)
        learning = ("\n" + advisor.dataset.summary()) if advisor is not None else ""
        if not gates:
            return visual + "Tiada sensor fizikal open/closed disahkan. Switch.bothgate ialah pulse, BUKAN sensor gate. Cadangan sahaja." + learning
        return (visual + "🚧 Sensor gate:\n" + "\n".join(
            f"• {self.friendly(row)}: {self.state(row)}" for _,row in gates[:8]
        ) + "\nSemakan sahaja; tiada arahan buka/tutup dihantar." + learning)

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

    @staticmethod
    def normalize_name(raw):
        """Normalize colloquial BM/English names for LOOKUP, not HA registry renames."""
        text = str(raw).lower().strip()
        if "." in text and re.fullmatch(r"[a-z_]+\.[a-z0-9_]+", text):
            text = text.split(".", 1)[1]
        text = text.replace("_", " ")
        # Preserve room index vs lamp index: livingroom2lamp != livingroomlamp2.
        text = re.sub(r"\blivingroom(\d*)lamp(\d*)\b", r"living room \1 lamp \2", text)
        text = re.sub(r"\blivingroom(\d*)fan(\d*)\b", r"living room \1 fan \2", text)
        text = re.sub(r"\blivingroom(\d*)\b", r"living room \1", text)
        text = re.sub(r"\bmasterbedroom\b|\bmasterbed\b", "master bedroom", text)
        text = re.sub(r"\bdoorlamp\b", "door lamp", text)
        text = re.sub(r"\bcarporchlamp\b", "car porch lamp", text)
        text = re.sub(r"\b(ruang\s+tamu|ruang\s+depan)\b", "living room", text)
        text = re.sub(r"\b(bilik\s+(?:tidur\s+)?utama|bilik\s+master)\b", "master bedroom", text)
        text = re.sub(r"\bbilik\s+(?:tidur\s+)?([123])\b", r"bedroom \1", text)
        text = re.sub(r"\bdapur\s+basah\b", "wet kitchen", text)
        text = re.sub(r"\bdapur\s+kering\b", "dry kitchen", text)
        text = re.sub(r"\b(pintu\s+luar)\b", "door", text)
        text = re.sub(r"\b(lampu|lights?)\b", "lamp", text)
        text = re.sub(r"\b(kipas)\b", "fan", text)
        text = re.sub(r"\b(air\s*cond|aircond|penghawa\s+dingin)\b", "ac", text)
        text = re.sub(r"\bliving\s+room\s+2\s+living\s+room\s+2\b", "living room 2", text)
        return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())

    def exact_entity(self, states, name):
        text = str(name).lower().strip()
        # A fully qualified entity ID always wins. This also prevents fuzzy
        # lookups resolving an unsafe entity with a similar friendly name.
        if text in states:
            return [(text, states[text])]
        domain_hint = None
        m = re.match(r"^(switch|light|fan|climate)\s+(.+)$", text)
        if m:
            domain_hint, text = m.groups()
        wanted = self.normalize_name(text)
        if not wanted:
            return []
        aliases = {
            "switch.doorlamp": {"door lamp", "lampu pintu luar", "lampu pintu", "lampu depan"},
            "switch.carporchlamp": {"car porch lamp", "lampu porch", "lampu car porch", "lampu parking"},
            "switch.drykitchenlamp": {"lampu dapur kering", "dry kitchen lamp"},
            "switch.wetkitchenlamp": {"lampu dapur basah", "wet kitchen lamp"},
        }
        catalog = []
        for entity_id, row in states.items():
            if domain_hint and not entity_id.startswith(domain_hint + "."):
                continue
            names = {
                self.normalize_name(entity_id),
                self.normalize_name(self.friendly(row)),
            }
            names.update(self.normalize_name(alias) for alias in aliases.get(entity_id, set()))
            catalog.append((entity_id, row, names))
        exact = [(entity_id, row) for entity_id, row, names in catalog if wanted in names]
        # A numbered specific fixture wins an exact match; an unnumbered
        # room/fixture phrase (e.g. "lampu ruang tamu") must still inspect
        # every numbered lamp in that room and ask which one to control.
        tokens = set(wanted.split())
        if exact and (any(token.isdigit() for token in tokens) or len(tokens) < 2):
            return exact
        if len(tokens) < 2:
            return exact
        similar = [(entity_id, row) for entity_id, row, names in catalog
                   if any(tokens.issubset(set(alias.split())) for alias in names)]
        return similar if len(similar) > len(exact) else exact

    def entity_state(self, states, name):
        matches = self.exact_entity(states, name)
        if len(matches) == 1:
            eid, row = matches[0]
            unit = (row.get("attributes") or {}).get("unit_of_measurement") or ""
            return f"{self.friendly(row)}: {self.state(row)}{unit if len(str(unit)) < 15 else ''} ({eid})"
        if len(matches) > 1:
            return ("Ada beberapa entity yang sepadan:\n"
                    + "\n".join(f"• {self.friendly(r)} ({eid}): {self.state(r)}" for eid, r in matches[:12]))
        return self.entity_list(states, name)

    def exact_safe_target(self, target, states):
        matches = self.exact_entity(states, target)
        safe = [(eid, row) for eid, row in matches if self.safe_actuator(eid, row)]
        if len(safe) == 1 and len(matches) == 1:
            return safe[0], None
        if len(safe) > 1:
            options = "\n".join(
                f"• {self.friendly(row)} — {entity_id}"
                for entity_id, row in safe[:10]
            )
            return None, ("Ada beberapa peranti sepadan. Pilih satu yang tepat:\n"
                          + options + "\nContoh: Hidupkan switch.livingroomlamp2")
        if matches:
            return None, "Peranti ini boleh dibaca, tetapi kawalannya disekat atas faktor keselamatan."
        return None, "Entity tak dijumpai. Cuba /entities switch atau sebut nama penuh peranti."

    def propose_control(self, entity_id, row, service):
        if not self.safe_actuator(entity_id, row):
            return "Kawalan peranti ini tidak dibenarkan."
        code = secrets.token_hex(3).upper()
        self.pending = {
            "code": code, "entity": entity_id,
            "domain": entity_id.split(".", 1)[0],
            "service": service, "expires": time.monotonic() + 180,
        }
        action = "MATIKAN" if service == "turn_off" else "HIDUPKAN"
        return (f"🔒 Sahkan: {action} {self.friendly(row)} ({entity_id})?\n"
                "Balas ok / yes / confirm / /confirm dalam 3 minit. Balas batal untuk batalkan.")

    async def handle(self, session, prompt):
        text = str(prompt).strip()
        lower=text.lower()
        if not text: return "/ai <soalan> untuk bersembang dengan Hermes."
        if lower == "/gatedata":
            advisor = getattr(self, "gate_advisor", None)
            return advisor.dataset.summary() if advisor else "Gate learning belum tersedia."
        if lower.startswith("/gatelearn"):
            advisor = getattr(self, "gate_advisor", None)
            if advisor is None:
                return "Gate learning belum tersedia."
            parts = lower.split()
            if len(parts) != 2 or parts[1] not in ("open", "closed"):
                return "Guna /gatelearn OPEN atau /gatelearn CLOSED ketika status gate disahkan secara visual."
            # An owner-initiated fresh camera capture; never toggles the gate.
            self.pending = None
            self.pending_selection = None
            return await advisor.dataset.owner_confirm(session, parts[1].upper(), API, HEADERS)
        # A numbered reply selects a candidate, but NEVER performs the action.
        # A second explicit positive acknowledgement is still required.
        selection = self.pending_selection
        if selection:
            if time.monotonic() >= selection["expires"]:
                self.pending_selection = None
                return "Pilihan dah tamat. Nyatakan arahan sekali lagi."
            # Telegram user may reply "2", "No 2", "pilihan 2", "#2" or "2.".
            # Never infer from a message containing MULTIPLE choices.
            compact = re.sub(r"[ \t]+", " ", lower).strip()
            choice_match = re.fullmatch(
                r"(?:no\.?|nombor|number|num\.?|pilihan|option|pilih|yang|#)?\s*([1-9])[.)]?\s*",
                compact,
            )
            if choice_match:
                index = int(choice_match.group(1)) - 1
                if not 0 <= index < len(selection["entities"]):
                    return ("Pilihan nombor itu tiada. Balas satu nombor sahaja, "
                            "atau 'batal' untuk hentikan.")
                self.pending_selection = None
                entity_id = selection["entities"][index]
                try:
                    row = await self.entity(session, entity_id)
                    if not self.safe_actuator(entity_id, row):
                        return "Peranti itu tidak boleh dikawal sekarang."
                except Exception:
                    return "Gagal semak peranti HA. Tiada arahan dihantar."
                return self.propose_control(entity_id, row, selection["service"])
            if WRITE.match(text):
                # A new explicit device command supersedes the previous menu.
                self.pending_selection = None
            elif re.search(r"\d", lower):
                return ("Aku nampak nombor pilihan yang tak jelas. "
                        "Balas SATU nombor sahaja, contoh '2' atau 'No 2'. "
                        "Pilihan asal masih tersedia.")
            elif lower not in ("/cancel", "cancel", "batal", "/batal"):
                self.pending_selection = None
        # While a control is awaiting YES, an accidental extra digit must not
        # cancel that approval or become a free-form model hallucination.
        if self.pending and re.fullmatch(r"(?:no\.?|nombor|number|num\.?|pilihan|option|pilih|yang|#)?\s*[1-9][.)]?", lower.strip()):
            return ("Aku dah pilih peranti. Untuk teruskan balas 'ok' atau "
                    "'confirm', atau 'batal' untuk hentikan. Belum ada tindakan dibuat.")
        # A reply to a specific outstanding owner request can be a simple "ok".
        # No other text can approve an action, and no device is changed by a
        # model response. /confirm without a code MUST NOT consume the request.
        ack = re.sub(r"[.?!]+$", "", lower).strip()
        yes = {
            "/confirm", "confirm", "/sahkan", "sahkan",
            "ok", "okay", "ok boleh", "okay boleh", "baik", "baiklah",
            "yes", "yes boleh", "ya", "y", "yup", "yep", "iya",
            "boleh", "setuju", "betul", "teruskan", "proceed",
        }
        m_code = re.fullmatch(r"/?(?:confirm|sahkan)\s+(\S+)", ack)
        is_confirmation = ack in yes or bool(m_code)
        if is_confirmation:
            pending = self.pending
            if pending is None:
                return ("Tiada arahan kawalan menunggu. Nyatakan peranti dahulu."
                        if ack.startswith(("/confirm", "/sahkan", "confirm ", "sahkan "))
                        else None)
            if time.monotonic() >= pending["expires"]:
                self.pending = None
                return "Permintaan dah tamat. Sila hantar arahan sekali lagi."
            if m_code and m_code.group(1).upper() != pending["code"]:
                # Wrong code must NOT cancel the request.
                return "Kod tak sepadan. Cukup balas 'ok' atau '/confirm' sebelum tamat."
            # Consume exactly once BEFORE any network request to avoid retries
            # accidentally toggling an actuator twice.
            self.pending = None
            entity_id, domain, service = pending["entity"], pending["domain"], pending["service"]
            if domain not in ("light", "fan", "switch") or service not in ("turn_on", "turn_off"):
                return "Arahan disekat oleh safety policy."
            try:
                current = await self.entity(session, entity_id)
                if not self.safe_actuator(entity_id, current):
                    return "Arahan disekat oleh safety policy."
                async with session.post(API + "/services/" + domain + "/" + service,
                    headers=HEADERS, json={"entity_id": entity_id}, timeout=20) as resp:
                    if resp.status != 200:
                        return "HA menolak arahan. Tiada pengesahan tindakan."
                    await resp.read()
                await asyncio.sleep(1)
                newer = await self.entity(session, entity_id)
                expected = "on" if service == "turn_on" else "off"
                actual = self.state(newer)
                return (f"✅ {self.friendly(newer)}: {actual.upper()} (disahkan HA)."
                        if actual == expected else
                        f"HA terima arahan, tetapi status sekarang {actual}. Sila semak peranti.")
            except Exception:
                return "HA tidak dapat mengesahkan tindakan. Sila semak aplikasi HA."
        if self.pending and ack in ("no", "nope", "tidak", "tak", "tak jadi", "jangan", "tak payah"):
            self.pending = None
            return "Ok, arahan dibatalkan. Tiada peranti diubah."
        # Any different question cancels the previous pending permission,
        # so an unrelated future "ok" cannot approve an old action.
        if self.pending and ack not in ("/cancel", "cancel", "batal", "/batal"):
            self.pending = None
        if lower in ("/cancel","cancel","batal","/batal"):
            self.pending=None
            self.pending_selection=None
            return "Arahan kawalan dibatalkan."
        if lower in ("/hahelp","/homehelp","/rumahhelp"):
            return ("Hermes rumah:\n/rumah — ringkasan\n/ram — prestasi RPi4\n/cctv — status entity kamera\n"
                    "/gate — sensor pagar\n/gatedata — sampel\n/gatelearn OPEN/CLOSED — sahkan gambar semasa\n/lampu — light/fan\n"
                    "Hidupkan lampu ruang tamu → pilih lampu tepat → balas ok/yes/confirm.\n"
                    "Gate/kunci/alarm tidak dikawal melalui AI.")
        # Prevent a model from construing unsafe device-control language as authorization.
        m=WRITE.match(text)
        if m:
            verb,target=m.groups()
            if (any(term in (target+" "+verb).lower() for term in ("gate","autogate","garaj","garage","pagar","kunci","lock","alarm","cctv","camera","router","reboot")) or
                ("pintu" in target.lower() and not any(x in target.lower() for x in ("lampu","lamp","light")))):
                return ("Demi keselamatan, Hermes tak dibenarkan kawal gate, kunci, alarm "
                        "atau sistem kritikal. Boleh semak status melalui /gate.")
            verb=verb.lower()
            if verb not in ("hidupkan","nyalakan","pasang","on","turn on","light on","switch on","matikan","padamkan","tutup","off","turn off","light off","switch off","buka","bukakan"):
                return "Arahan kawalan ini belum dibenarkan. Hanya light/fan ON/OFF."
            try: states=await self.states(session)
            except Exception: return "Tak dapat akses status HA sekarang. Tiada arahan dihantar."
            service="turn_off" if verb in ("matikan","padamkan","tutup","off","turn off","light off","switch off") else "turn_on"
            item,error=self.exact_safe_target(target,states)
            if error:
                safe=[(eid,row) for eid,row in self.exact_entity(states,target)
                      if self.safe_actuator(eid,row)]
                if len(safe)>1:
                    self.pending_selection={
                        "entities":[eid for eid,_ in safe[:9]],
                        "service":service,
                        "expires":time.monotonic()+120,
                    }
                    numbered="\n".join(f"{i}. {self.friendly(row)} ({eid})"
                                       for i,(eid,row) in enumerate(safe[:9],1))
                    return ("Ada beberapa peranti yang sepadan:\n"+numbered+
                            "\nBalas nombor pilihan dalam 2 minit. Lepas tu aku minta confirm.")
                return error
            entity,row=item
            return self.propose_control(entity,row,service)
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
            try: return await self.gate_summary_live(session, await self.states(session))
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
            if kind == "gate":
                return await self.gate_summary_live(session, states)
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
