"""The plain-words report: where, why, evidence, how often; radio only on a radio uplink."""
import json
import os
import tempfile
import unittest

from dobermann import alerts, narrative, store
from dobermann.chain import Chain

FIBRE = Chain({"nodes": [{"id": "router", "label": "Router", "ip": "192.168.1.1", "adapter": "upnp_igd"}]})
FWA = Chain({"nodes": [{"id": "router", "label": "FRITZ!Box", "ip": "192.168.1.1", "adapter": "upnp_igd"},
                       {"id": "modem", "label": "ZTE", "ip": "192.168.2.1", "adapter": "zte_goform"}]})


class TestWhere(unittest.TestCase):
    def test_marks(self):
        line, sentence = narrative.where("GATEWAY_UNRESPONSIVE", FWA)
        self.assertTrue(line.startswith("dobermann ✔ → FRITZ!Box ✘ → ZTE ?"))
        self.assertIn("in casa", sentence)
        line, _ = narrative.where("LOCAL_LINK_DOWN", FWA)
        self.assertIn("FRITZ!Box ✔ → ZTE ✘", line)
        line, sentence = narrative.where("LOCAL_LINK_DOWN", FIBRE)
        self.assertIn("Router ✔ → operatore ✘", line)
        self.assertIn("la linea dell'operatore", sentence)
        line, _ = narrative.where("DNS_FAILURE_LOCAL", FIBRE)
        self.assertNotIn("✘", line)

    def test_radio_only_with_radio_uplink(self):
        self.assertEqual(FWA.link_kind(), "radio")
        self.assertEqual(FIBRE.link_kind(), "wired")
        self.assertNotIn("legacy_rat", alerts.visible_categories(False))
        self.assertIn("legacy_rat", alerts.visible_categories(True))
        self.assertNotIn("modem", narrative.advice("PACKET_LOSS", FIBRE))
        self.assertIn("modem", narrative.advice("PACKET_LOSS", FWA))


class TestLongIncidents(unittest.TestCase):
    def test_long_renewal_is_not_called_routine_and_sequences_are_summarised(self):
        phases = []
        for i in range(10):
            phases.append({"cause": "WAN_DISCONNECTED", "start_ts": i * 1000, "end_ts": i * 1000 + 5})
            phases.append({"cause": "UPSTREAM_OUTAGE", "start_ts": i * 1000 + 10, "end_ts": i * 1000 + 900})
        text = narrative.why("IP_RENEWAL_CYCLE", FWA, phases, duration=24000)
        self.assertNotIn("routine", text)
        self.assertIn("molto più di un rinnovo normale", text)
        self.assertIn("20 fasi: internet irraggiungibile 10 volte", text)
        self.assertIn("routine", narrative.why("IP_RENEWAL_CYCLE", FWA, phases[:2], duration=30))
        self.assertIn("segnala all'operatore", narrative.advice("IP_RENEWAL_CYCLE", FWA, duration=24000))
        self.assertIn("guasto dell'operatore", narrative.headline("IP_RENEWAL_CYCLE", 24000))
        self.assertNotIn("rinnovato", narrative.where("IP_RENEWAL_CYCLE", FWA, duration=24000)[1])

    def test_long_radio_sequence(self):
        during = [(i * 600, {"cell.rat": "LTE" if i % 2 else "5G_NSA", "cell.service": "full"}) for i in range(20)]
        lines = narrative.radio_story({"cell.rat": "5G_NSA"}, during, {"cell.rat": "5G_NSA"})
        self.assertIn("durante 5G (NSA) per brevi tratti (10 volte), 4G per brevi tratti (10 volte)", lines[0])


class TestRadioStory(unittest.TestCase):
    def test_sequence_signal_cell(self):
        before = {"cell.rat": "5G_NSA", "cell.service": "full", "cell.lte_rsrp_dbm": -97.0, "cell.cell_id": "a"}
        during = [(0, {"cell.rat": "NONE", "cell.service": "limited"}), (300, {"cell.rat": "NONE", "cell.service": "limited"}),
                  (305, {"cell.rat": "3G", "cell.service": "full"})]
        after = {"cell.rat": "3G", "cell.service": "full", "cell.lte_rsrp_dbm": -110.0, "cell.cell_id": "b"}
        lines = narrative.radio_story(before, during, after)
        self.assertEqual(lines[0], "rete mobile: prima 5G (NSA), durante senza servizio 5 minuti → 3G, dopo 3G")
        self.assertIn("(calo marcato)", lines[1])
        self.assertEqual(lines[2], "cella: cambiata")
        steady = {"cell.rat": "LTE", "cell.service": "full"}
        self.assertEqual(narrative.radio_story(steady, [(0, steady)], steady), ["rete mobile: sempre 4G"])


class TestSummaryAndRecurrence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.con = store.connect(os.path.join(self.tmp.name, "d.db"))

    def test_radio_summary(self):
        for i in range(100):
            rat = "LTE" if 40 <= i < 50 else "5G_NSA"
            self.con.execute("INSERT INTO node_facts VALUES (?, 'modem', ?)", (1000 + i * 30, json.dumps(
                {"cell.rat": rat, "cell.service": "full", "cell.nr_rsrp_dbm": -100.0 - (i == 7) * 20})))
        lines = narrative.radio_summary(self.con, "modem", 0, 10_000)
        self.assertEqual(lines[0], "Tempo per tecnologia: 5G (NSA) 89.9%, 4G 10.1%")
        self.assertEqual(lines[1], "Cadute su una tecnologia peggiore durate almeno un minuto: 1")
        self.assertEqual(lines[2], "Segnale 5G: tipico -100 dBm, peggiore -120 dBm")
        _, body, _ = alerts.summary_message(self.con, 0, 10_000, alerts.rules({}), radio_node="modem")
        self.assertIn("--- Rete mobile ---", body)
        _, body, _ = alerts.summary_message(self.con, 0, 10_000, alerts.rules({}))
        self.assertNotIn("Rete mobile", body)
        self.assertNotIn("3g", body.lower())

    def test_recurrence_and_night(self):
        import time
        local = lambda d, h: time.mktime((2026, 3, d, h, 0, 0, 0, 0, -1))   # local time, whatever the zone
        for i, hour in enumerate((2, 3, 4)):
            self.con.execute("INSERT INTO incidents (id, start_ts, end_ts, cause) VALUES (?, ?, ?, 'UPSTREAM_OUTAGE')",
                             (i + 1, local(10 + i, hour), local(10 + i, hour) + 60))
        text = narrative.recurrence(self.con, 99, "WAN_DISCONNECTED", local(14, 1))
        self.assertIn("3 negli ultimi 7 giorni", text)
        self.assertIn("internet assente", text)
        self.assertIn("soprattutto di notte", text)
        self.assertIn("Primo evento", narrative.recurrence(self.con, 99, "HIGH_LATENCY", local(14, 1)))


if __name__ == "__main__":
    unittest.main()
