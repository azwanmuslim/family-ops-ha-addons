import asyncio
import unittest
from ha_home_capabilities import HomeTools

class FakeResponse:
    def __init__(self, data=None, status=200):
        self.data=data
        self.status=status
    async def __aenter__(self): return self
    async def __aexit__(self, *args): return False
    async def json(self): return self.data
    async def read(self): return b"{}"

class FakeSession:
    def __init__(self):
        self.rows={
            "light.ruang_tamu":{"entity_id":"light.ruang_tamu","state":"off","attributes":{"friendly_name":"Lampu Ruang Tamu"}},
            "sensor.memory_use_percent":{"entity_id":"sensor.memory_use_percent","state":"43.7","attributes":{"unit_of_measurement":"%"}},
            "sensor.processor_use":{"entity_id":"sensor.processor_use","state":"21","attributes":{"unit_of_measurement":"%"}},
            "person.owner":{"entity_id":"person.owner","state":"home","attributes":{"friendly_name":"Owner"}},
            "camera.depan":{"entity_id":"camera.depan","state":"idle","attributes":{"friendly_name":"Kamera Depan"}},
            "cover.gate":{"entity_id":"cover.gate","state":"closed","attributes":{"friendly_name":"Gate Utama","device_class":"garage"}}
        }
        self.calls=[]
    def get(self,url,**kwargs):
        if url.endswith("/states"): return FakeResponse(list(self.rows.values()))
        entity=url.split("/states/",1)[-1]
        return FakeResponse(self.rows.get(entity),status=200 if entity in self.rows else 404)
    def post(self,url,**kwargs):
        self.calls.append((url,kwargs.get("json")))
        entity=kwargs.get("json",{}).get("entity_id")
        if entity in self.rows and url.endswith("/turn_on"): self.rows[entity]["state"]="on"
        if entity in self.rows and url.endswith("/turn_off"): self.rows[entity]["state"]="off"
        return FakeResponse([])

class HomeCapabilityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sent=[]
        async def capture(session,msg): self.sent.append(msg)
        self.tools=HomeTools(capture)
        self.session=FakeSession()

    async def test_read_only_live_metrics_presence_camera(self):
        result=await self.tools.handle(self.session,"/rumah")
        self.assertIn("43.7%",result)
        self.assertIn("Owner",result)
        result=await self.tools.handle(self.session,"CCTV status")
        self.assertIn("1/1",result)
        self.assertFalse(self.session.calls)

    async def test_frigate_sensor_not_mistaken_for_gate(self):
        self.session.rows["sensor.disk_use_percent_media_frigate"]={
            "entity_id":"sensor.disk_use_percent_media_frigate",
            "state":"31.6","attributes":{"friendly_name":"Disk Usage (HDD)"}
        }
        gates=self.tools.gate_items(self.session.rows)
        self.assertEqual([k for k,_ in gates],["cover.gate"])

    async def test_nvr_illuminator_excluded_from_control(self):
        self.session.rows["light.nvr_car_porch_cam_illuminator"]={
            "entity_id":"light.nvr_car_porch_cam_illuminator",
            "state":"off","attributes":{"friendly_name":"Car Porch Cam Illuminator"}
        }
        answer=await self.tools.handle(self.session,"on light.nvr_car_porch_cam_illuminator")
        self.assertIn("disekat",answer)
        self.assertFalse(self.session.calls)

    async def test_control_needs_confirmation_and_no_early_write(self):
        result=await self.tools.handle(self.session,"Hidupkan lampu ruang tamu")
        self.assertIn("/confirm",result)
        self.assertFalse(self.session.calls)
        code=self.tools.pending["code"]
        result=await self.tools.handle(self.session,"/confirm "+code)
        self.assertIn("disahkan HA",result)
        self.assertEqual(self.session.rows["light.ruang_tamu"]["state"],"on")
        self.assertEqual(len(self.session.calls),1)
        result=await self.tools.handle(self.session,"/confirm "+code)
        self.assertIn("Tiada arahan",result)
        self.assertEqual(len(self.session.calls),1)

    async def test_lock_gate_and_scripts_never_executed(self):
        for message in ["Buka gate","tutup pintu depan","Buka pintu luar","unlock kunci utama","open garage"]:
            result=await self.tools.handle(self.session,message)
            self.assertIn("keselamatan",result)
        self.assertFalse(self.session.calls)

    async def test_wrong_code_does_not_consume_confirmation(self):
        await self.tools.handle(self.session,"matikan lampu ruang tamu")
        old_pending=self.tools.pending
        result=await self.tools.handle(self.session,"/confirm WRONG")
        self.assertIn("Kod tak sepadan",result)
        self.assertEqual(old_pending,self.tools.pending)
        self.assertFalse(self.session.calls)
        accepted=await self.tools.handle(self.session,"/confirm")
        self.assertIn("disahkan HA",accepted)
        self.assertEqual(len(self.session.calls),1)

    async def test_catalog_reads_all_domains(self):
        self.session.rows["lock.smart_lock"]={"entity_id":"lock.smart_lock","state":"locked","attributes":{"friendly_name":"Smart Lock"}}
        self.session.rows["switch.doorlamp"]={"entity_id":"switch.doorlamp","state":"off","attributes":{"friendly_name":"Door Lamp"}}
        result=await self.tools.handle(self.session,"/entities")
        self.assertIn("lock: 1",result)
        self.assertIn("switch: 1",result)
        self.assertIn("sensor:",result)
        result=await self.tools.handle(self.session,"/entity lock.smart_lock")
        self.assertIn("locked",result)
        result=await self.tools.handle(self.session,"/entities switch")
        self.assertIn("switch.doorlamp",result)
        self.assertFalse(self.session.calls)

    async def test_house_lamps_are_switches(self):
        self.session.rows["switch.doorlamp"]={"entity_id":"switch.doorlamp","state":"on","attributes":{"friendly_name":"Door Lamp"}}
        self.session.rows["switch.carporchlamp"]={"entity_id":"switch.carporchlamp","state":"off","attributes":{"friendly_name":"Car Porch Lamp"}}
        self.session.rows["light.nvr_car_porch_cam_infrared"]={"entity_id":"light.nvr_car_porch_cam_infrared","state":"on","attributes":{"friendly_name":"Car Porch Cam Infrared"}}
        for question in ("Lampu mana yang sedang menyala?","Lampu ada yg terbuka?","Ada lampu yang on?"):
            answer=await self.tools.handle(self.session,question)
            self.assertIn("Door Lamp",answer)
            self.assertIn("1 ON",answer)
            self.assertNotIn("Car Porch Cam Infrared",answer)
        self.assertFalse(self.session.calls)

    async def test_rojak_door_lamp_needs_confirmation(self):
        self.session.rows["switch.doorlamp"]={"entity_id":"switch.doorlamp","state":"off","attributes":{"friendly_name":"Door Lamp"}}
        for question in ("Buka door lamp","Boleh buka lampu pintu luar ?","Light on door lamp","Hidupkan switch.doorlamp"):
            answer=await self.tools.handle(self.session,question)
            self.assertIn("/confirm",answer)
            self.assertFalse(self.session.calls)
            self.assertEqual(self.tools.pending["entity"],"switch.doorlamp")
            self.tools.pending=None
        answer=await self.tools.handle(self.session,"Buka door lamp")
        code=self.tools.pending["code"]
        confirmed=await self.tools.handle(self.session,"/confirm "+code)
        self.assertIn("disahkan HA",confirmed)
        self.assertEqual(self.session.rows["switch.doorlamp"]["state"],"on")
        self.assertEqual(len(self.session.calls),1)

    async def test_gate_and_camera_switch_stay_blocked(self):
        self.session.rows["switch.bothgate"]={"entity_id":"switch.bothgate","state":"off","attributes":{"friendly_name":"Autogate Both gate"}}
        self.session.rows["switch.nvr_cam_motion_detection"]={"entity_id":"switch.nvr_cam_motion_detection","state":"on","attributes":{"friendly_name":"Cam Motion Detection"}}
        for q in ("Buka switch.bothgate","Hidupkan switch.nvr_cam_motion_detection"):
            answer=await self.tools.handle(self.session,q)
            self.assertNotIn("/confirm",answer)
        self.assertFalse(self.session.calls)

    async def test_bilingual_living_room_switch_disambiguation(self):
        for eid, name in [
            ("switch.livingroomlamp1","Living Room Lamp 1"),
            ("switch.livingroomlamp2","Living Room Lamp 2"),
            ("switch.livingroom2lamp","Living Room 2 Living Room 2 Lamp"),
        ]:
            self.session.rows[eid]={"entity_id":eid,"state":"off","attributes":{"friendly_name":name}}
        result=await self.tools.handle(self.session,"Hidupkan lampu ruang tamu")
        self.assertIn("beberapa peranti",result)
        self.assertIn("switch.livingroomlamp1",result)
        self.assertIn("switch.livingroomlamp2",result)
        self.assertIn("switch.livingroom2lamp",result)
        self.assertFalse(self.session.calls)
        result=await self.tools.handle(self.session,"Hidupkan Living Room lamp 2")
        self.assertIn("switch.livingroomlamp2",result)
        self.assertIsNotNone(self.tools.pending)
        self.assertEqual(self.tools.pending["entity"],"switch.livingroomlamp2")
        self.tools.pending=None
        result=await self.tools.handle(self.session,"Hidupkan living room 2 lamp")
        self.assertEqual(self.tools.pending["entity"],"switch.livingroom2lamp")
        self.assertFalse(self.session.calls)

    async def test_simple_positive_acknowledgements(self):
        self.session.rows["switch.livingroomlamp2"]={
            "entity_id":"switch.livingroomlamp2","state":"off",
            "attributes":{"friendly_name":"Living Room Lamp 2"}
        }
        for ack in ("ok","yes","confirm","/confirm","ya","boleh","setuju","teruskan","ok boleh"):
            self.session.rows["switch.livingroomlamp2"]["state"]="off"
            self.tools.pending=None
            before=len(self.session.calls)
            requested=await self.tools.handle(self.session,"Hidupkan living room lamp 2")
            self.assertIn("Balas ok / yes",requested)
            self.assertEqual(len(self.session.calls),before)
            result=await self.tools.handle(self.session,ack)
            self.assertIn("disahkan HA",result,ack)
            self.assertEqual(len(self.session.calls),before+1)
            self.assertIsNone(self.tools.pending)

    async def test_unrequested_and_expired_acks_do_nothing(self):
        self.session.rows["switch.doorlamp"]={
            "entity_id":"switch.doorlamp","state":"off",
            "attributes":{"friendly_name":"Door Lamp"}
        }
        self.assertIsNone(await self.tools.handle(self.session,"ok"))
        self.assertFalse(self.session.calls)
        await self.tools.handle(self.session,"Hidupkan switch.doorlamp")
        self.tools.pending["expires"]=0
        response=await self.tools.handle(self.session,"yes")
        self.assertIn("Permintaan dah tamat",response)
        self.assertFalse(self.session.calls)
        await self.tools.handle(self.session,"Hidupkan switch.doorlamp")
        response=await self.tools.handle(self.session,"no")
        self.assertIn("dibatalkan",response)
        self.assertFalse(self.session.calls)

    async def test_unrelated_question_cancels_stale_approval(self):
        self.session.rows["switch.doorlamp"]={
            "entity_id":"switch.doorlamp","state":"off",
            "attributes":{"friendly_name":"Door Lamp"}
        }
        await self.tools.handle(self.session,"Hidupkan switch.doorlamp")
        self.assertIsNotNone(self.tools.pending)
        await self.tools.handle(self.session,"/ram")
        self.assertIsNone(self.tools.pending)
        self.assertIsNone(await self.tools.handle(self.session,"ok"))
        self.assertFalse(self.session.calls)

    async def test_numeric_selection_then_ok_needs_two_steps(self):
        for eid, name in [
            ("switch.livingroomlamp1", "Living Room Lamp 1"),
            ("switch.livingroomlamp2", "Living Room Lamp 2"),
            ("switch.livingroom2lamp", "Living Room 2 Living Room 2 Lamp"),
        ]:
            self.session.rows[eid]={"entity_id":eid,"state":"off",
                                    "attributes":{"friendly_name":name}}
        listing=await self.tools.handle(self.session,"Hidupkan lampu ruang tamu")
        self.assertIn("1.",listing)
        self.assertIn("2.",listing)
        self.assertIsNone(self.tools.pending)
        self.assertIsNotNone(self.tools.pending_selection)
        self.assertFalse(self.session.calls)
        choices=self.tools.pending_selection["entities"]
        selected=choices[1]
        requested=await self.tools.handle(self.session,"2")
        self.assertIn("🔒 Sahkan:",requested)
        self.assertIn(selected,requested)
        self.assertFalse(self.session.calls)
        result=await self.tools.handle(self.session,"ok")
        self.assertIn("disahkan HA",result)
        self.assertEqual(self.session.rows[selected]["state"],"on")
        self.assertEqual(len(self.session.calls),1)

    async def test_numeric_selection_expires_and_cancel_works(self):
        for eid, name in [
            ("switch.livingroomlamp1", "Living Room Lamp 1"),
            ("switch.livingroomlamp2", "Living Room Lamp 2"),
        ]:
            self.session.rows[eid]={"entity_id":eid,"state":"off",
                                    "attributes":{"friendly_name":name}}
        await self.tools.handle(self.session,"Hidupkan lampu ruang tamu")
        self.tools.pending_selection["expires"]=0
        expired=await self.tools.handle(self.session,"1")
        self.assertIn("tamat",expired)
        self.assertFalse(self.session.calls)
        await self.tools.handle(self.session,"Hidupkan lampu ruang tamu")
        response=await self.tools.handle(self.session,"batal")
        self.assertIn("dibatalkan",response)
        self.assertIsNone(self.tools.pending_selection)
        self.assertFalse(self.session.calls)

    async def test_proactive_requires_sustained_condition_and_caps(self):
        self.session.rows["camera.depan"]["state"]="unavailable"
        await self.tools.proactive_once(self.session)
        self.assertFalse(self.sent)
        key="camera:camera.depan"
        self.tools.active_conditions[key]-=610
        await self.tools.proactive_once(self.session)
        self.assertEqual(len(self.sent),1)
        await self.tools.proactive_once(self.session)
        self.assertEqual(len(self.sent),1)
        self.session.rows["camera.depan"]["state"]="idle"
        await self.tools.proactive_once(self.session)
        self.assertNotIn(key,self.tools.active_conditions)

if __name__=="__main__":
    unittest.main()
