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


    @staticmethod
    def _gps_state(distance_m, updated, accuracy=15, state="not_home"):
        # Coordinates are synthetic test fixtures, never a real family location.
        lat = 3.1 + distance_m / 111195
        return {
            "zone.home": {"attributes": {"latitude": 3.1, "longitude": 101.5}},
            "person.mama": {
                "state": state,
                "attributes": {"friendly_name": "Mama", "source": "device_tracker.mama"},
            },
            "device_tracker.mama": {
                "state": "not_home",
                "last_updated": updated.isoformat(),
                "attributes": {"latitude": lat, "longitude": 101.5,
                               "gps_accuracy": accuracy},
            },
        }

    def test_approach_requires_three_distinct_accurate_gps_updates(self):
        advisor = GateAdvisor(lambda *args: None)
        base = dt.datetime.now(dt.timezone.utc)
        self.assertIsNone(advisor._approaching(
            self._gps_state(750, base - dt.timedelta(minutes=4)), 100))
        self.assertIsNone(advisor._approaching(
            self._gps_state(540, base - dt.timedelta(minutes=2)), 220))
        result = advisor._approaching(self._gps_state(330, base), 340)
        self.assertEqual(advisor.gps_trend, "APPROACHING")
        self.assertEqual(result["name"], "Mama")
        self.assertEqual(result["samples"], 3)
        self.assertAlmostEqual(result["to_m"], 330, delta=20)
        self.assertEqual(result["minutes"], 4.0)
        self.assertLessEqual(result["accuracy_m"], 50)
        self.assertNotIn("latitude", result)
        self.assertNotIn("longitude", result)

    def test_repeated_timestamp_does_not_fake_new_fix(self):
        advisor = GateAdvisor(lambda *args: None)
        base = dt.datetime.now(dt.timezone.utc)
        t0, t1 = base - dt.timedelta(minutes=4), base - dt.timedelta(minutes=2)
        self.assertIsNone(advisor._approaching(self._gps_state(750, t0), 100))
        self.assertIsNone(advisor._approaching(self._gps_state(540, t1), 220))
        self.assertIsNone(advisor._approaching(self._gps_state(300, t1), 340))
        self.assertEqual(len(advisor.gps_history["person.mama"]), 2)
        self.assertIsNotNone(advisor._approaching(self._gps_state(330, base), 460))

    def test_gps_drift_and_reversal_do_not_approach(self):
        advisor = GateAdvisor(lambda *args: None)
        base = dt.datetime.now(dt.timezone.utc)
        for i, dist in enumerate((460, 480, 450)):
            result = advisor._approaching(
                self._gps_state(dist, base - dt.timedelta(minutes=4 - 2 * i)),
                100 + i * 120)
            self.assertIsNone(result)
        self.assertEqual(advisor.gps_trend, "STATIONARY")

        advisor = GateAdvisor(lambda *args: None)
        for i, dist in enumerate((730, 510, 570)):
            result = advisor._approaching(
                self._gps_state(dist, base - dt.timedelta(minutes=4 - 2 * i)),
                100 + i * 120)
            self.assertIsNone(result)
        self.assertEqual(advisor.gps_trend, "NEAR_HOME")

    def test_missing_inaccurate_and_stale_gps_are_rejected(self):
        base = dt.datetime.now(dt.timezone.utc)
        for accuracy in (None, 51, 151):
            advisor = GateAdvisor(lambda *args: None)
            for i, dist in enumerate((750, 540, 330)):
                result = advisor._approaching(
                    self._gps_state(dist, base - dt.timedelta(minutes=4 - 2 * i),
                                    accuracy=accuracy),
                    100 + 120 * i)
                self.assertIsNone(result)
            self.assertEqual(advisor.gps_history, {})
        stale = GateAdvisor(lambda *args: None)
        self.assertIsNone(stale._approaching(
            self._gps_state(330, base - dt.timedelta(minutes=8)), 100))
        self.assertEqual(stale.gps_history, {})

    def test_must_not_alert_when_person_at_home(self):
        advisor = GateAdvisor(lambda *args: None)
        base = dt.datetime.now(dt.timezone.utc)
        for i, dist in enumerate((750, 540, 330)):
            self.assertIsNone(advisor._approaching(
                self._gps_state(dist, base - dt.timedelta(minutes=4 - 2 * i),
                                state="home"), 100 + 120 * i))
        self.assertEqual(advisor.gps_history, {})

    def test_no_gate_service_calls_in_advisor(self):
        body = inspect.getsource(GateAdvisor)
        self.assertNotIn("/services/switch", body)
        self.assertNotIn("switch.bothgate", body)
        self.assertNotIn("turn_on", body)
        self.assertNotIn("turn_off", body)


if __name__ == "__main__":
    unittest.main()
