"""Notification rules per kind of event, the summary's schedule and its text."""
import os
import tempfile
import time
import unittest

from dobermann import alerts, store


def inc(*phases):
    return {"phases": [{"cause": c, "start_ts": s, "end_ts": e, "samples_count": 1} for c, s, e in phases]}


class TestRules(unittest.TestCase):
    def test_defaults_and_bad_values(self):
        r = alerts.rules({"rules": {"outage": {"mode": "bogus", "after_min": -3}, "dns": {"mode": "never"}}})
        self.assertEqual(r["outage"], {"mode": "after", "after_min": 2})
        self.assertEqual(r["dns"]["mode"], "never")
        self.assertEqual(r["ip_renewal"]["mode"], "summary")

    def test_should_alert(self):
        r = alerts.rules({})
        renewal = inc(("WAN_DISCONNECTED", 0, 10), ("IP_CHANGE_DROP", 10, 30))
        self.assertEqual(alerts.should_alert(renewal, 30, r), (False, None))
        self.assertEqual(alerts.should_alert(renewal, 120, r), (True, "WAN_DISCONNECTED"))
        slow = inc(("HIGH_LATENCY", 0, 600))
        self.assertEqual(alerts.should_alert(slow, 600, r), (False, None))
        r["slow"] = {"mode": "immediate", "after_min": 5}
        self.assertEqual(alerts.should_alert(slow, 0, r), (True, "HIGH_LATENCY"))
        self.assertEqual(alerts.should_alert(inc(("PING_ARTIFACT", 0, 999)), 999, alerts.rules({})), (False, None))

    def test_slots(self):
        s = alerts.summary_cfg({"summary": {"frequency": "weekly", "weekday": 0, "hour": 8}})
        wed = time.mktime((2026, 10, 14, 21, 0, 0, 0, 0, -1))       # Wednesday
        slot = alerts.last_slot(wed, s)
        self.assertEqual(time.localtime(slot)[:5], (2026, 10, 12, 8, 0))
        since, until = alerts.period_for(slot, s)
        self.assertEqual(time.localtime(since)[:5], (2026, 10, 5, 8, 0))
        mon_early = time.mktime((2026, 10, 12, 7, 0, 0, 0, 0, -1))
        self.assertEqual(time.localtime(alerts.last_slot(mon_early, s))[:3], (2026, 10, 5))
        self.assertIsNone(alerts.last_slot(wed, alerts.summary_cfg({"summary": {"frequency": "off"}})))


class TestSummary(unittest.TestCase):
    def test_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = store.connect(os.path.join(tmp, "d.db"))
            for i in range(100):
                con.execute("INSERT INTO samples (ts, state) VALUES (?, ?)", (1000 + i, "OK" if i else "UPSTREAM_OUTAGE"))
            con.execute("INSERT INTO incidents (id, start_ts, end_ts, cause, phases_json) VALUES (7, 1010, 1040, 'IP_RENEWAL_CYCLE', '[]')")
            con.execute("INSERT INTO incidents (id, start_ts, end_ts, cause, phases_json) VALUES (8, 1050, 1350, 'UPSTREAM_OUTAGE', '[]')")
            con.execute("INSERT INTO ip_changes VALUES (1020, '10.0.0.1', '10.0.0.2')")
            subject, body, quiet = alerts.summary_message(con, 1000, 2000, alerts.rules({}), "https://x/watchdash")
            self.assertFalse(quiet)
            self.assertIn("2 eventi", subject)
            self.assertIn("99.00%", subject)
            self.assertIn("Rinnovo / cambio IP: 1", body)
            self.assertIn("Il più lungo: #8", body)
            self.assertIn("10.0.0.1 → 10.0.0.2", body)
            self.assertIn("internet assente, DNS che non risponde", body)
            _, body, quiet = alerts.summary_message(con, 5000, 6000, alerts.rules({}))
            self.assertTrue(quiet)
            self.assertIn("tutto regolare", body)


if __name__ == "__main__":
    unittest.main()
