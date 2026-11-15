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
        self.assertIn("tak jumpa",answer)
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
        for message in ["Buka gate","tutup pintu depan","unlock kunci utama","open garage"]:
            result=await self.tools.handle(self.session,message)
            self.assertIn("keselamatan",result)
        self.assertFalse(self.session.calls)

    async def test_wrong_code_consumes_confirmation(self):
        await self.tools.handle(self.session,"matikan lampu ruang tamu")
        result=await self.tools.handle(self.session,"/confirm WRONG")
        self.assertIn("kod salah",result)
        self.assertIsNone(self.tools.pending)
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
