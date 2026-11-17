"""Gate Advisor POC offline tests: no camera access, no GPS export or gate writes."""
import datetime as dt
import inspect
import unittest
from ha_gate_advisor import (
    GateAdvisor, TEMPLATES, _decode, _corr, _distance_m,
    classify_jpeg,
)


class GateAdvisorTests(unittest.TestCase):
    def test_reference_templates_are_valid_and_distinct(self):
        closed = _decode(TEMPLATES["middle"]["closed"])
        opened = _decode(TEMPLATES["middle"]["open"])
        self.assertEqual(len(closed), 256)
        self.assertEqual(len(opened), 256)
        self.assertAlmostEqual(_corr(closed, closed), 1, places=4)
        self.assertLess(_corr(closed, opened), 0.90)

    def test_unknown_image_is_not_a_gate_status(self):
        self.assertEqual(classify_jpeg(b""), ("UNKNOWN", 0.0))

    def test_presence_requires_all_known_away_and_group(self):
        fn = GateAdvisor._presence
        s = {
            "person.a": {"state": "not_home"},
            "person.b": {"state": "not_home"},
            "group.family": {"state": "not_home"},
        }
        self.assertTrue(fn(s))
        s["person.a"]["state"] = "home"
        self.assertFalse(fn(s))
        s["person.a"]["state"] = "unavailable"
        self.assertFalse(fn(s))
        s["person.a"]["state"] = "not_home"
        s["group.family"]["state"] = "home"
        self.assertFalse(fn(s))

    def test_distance_same_point_zero(self):
        self.assertAlmostEqual(_distance_m(3.1, 101.5, 3.1, 101.5), 0.0)

    def test_no_gate_service_calls_in_advisor(self):
        body = inspect.getsource(GateAdvisor)
        self.assertNotIn("/services/switch", body)
        self.assertNotIn("switch.bothgate", body)
        self.assertNotIn("turn_on", body)
        self.assertNotIn("turn_off", body)


if __name__ == "__main__":
    unittest.main()
