import unittest

from dobermann import adapters, facts as factlib
from dobermann.adapters import upnp_igd
from dobermann.adapters.base import Adapter, run_sample
from dobermann.adapters.upnp_igd import UpnpIgd
from dobermann.adapters.zte_goform import ZteGoform, classify_network_type
from tests.fakes import FakeIgd, FakeZte, closed_port


class TestFacts(unittest.TestCase):
    def test_unknown_mistyped_and_blank_values(self):
        clean, dropped = factlib.validate({
            "wan.uptime_s": "42", "cell.rssi_dbm": "-61", "wan.link_up": "Up", "cell.rat": "LTE",
            "cell.cell_id": "", "wan.ip": "N/A",            # blank: silently absent
            "cell.rat_x": "LTE", "cell.bars": "many", "wan.status": "online",   # dropped
        })
        self.assertEqual(clean, {"wan.uptime_s": 42, "cell.rssi_dbm": -61.0, "wan.link_up": True,
                                 "cell.rat": "LTE"})
        self.assertEqual(sorted(dropped), ["cell.bars", "cell.rat_x", "wan.status"])

    def test_allowed_restricts_names(self):
        clean, dropped = factlib.validate({"wan.ip": "1.2.3.4", "cell.rat": "LTE"}, allowed={"wan.ip"})
        self.assertEqual(clean, {"wan.ip": "1.2.3.4"})
        self.assertEqual(dropped, ["cell.rat"])


class TestContract(unittest.TestCase):
    def test_every_adapter_declares_only_known_facts(self):
        for cls in adapters.ADAPTERS:
            self.assertTrue(cls.id and cls.name, cls)
            self.assertLessEqual(set(cls.provides), set(factlib.FACTS), cls.id)

    def test_run_sample_survives_a_crashing_adapter(self):
        class Boom(Adapter):
            id, provides = "boom", frozenset({"wan.ip"})

            def sample(self):
                raise RuntimeError("bug")
        r = run_sample(Boom("h"))
        self.assertFalse(r.ok)
        self.assertIn("RuntimeError", r.error)

    def test_run_sample_drops_facts_outside_the_contract(self):
        class Leaky(Adapter):
            id, provides = "leaky", frozenset({"wan.ip"})

            def sample(self):
                return {"wan.ip": "1.2.3.4", "cell.rat": "LTE"}
        r = run_sample(Leaky("h"))
        self.assertEqual(r.facts, {"wan.ip": "1.2.3.4"})
        self.assertEqual(r.dropped, ["cell.rat"])


class TestZteNetworkType(unittest.TestCase):
    def test_values_seen_on_the_real_router(self):
        self.assertEqual(classify_network_type("ENDC"), ("full", "5G_NSA"))
        self.assertEqual(classify_network_type("LTE"), ("full", "LTE"))
        self.assertEqual(classify_network_type("HSPA"), ("full", "3G"))
        self.assertEqual(classify_network_type("NO_SERVICE"), ("none", "NONE"))
        self.assertEqual(classify_network_type("LIMITED_SERVICE_PS"), ("limited", "NONE"))

    def test_unknown_value_is_unknown_not_a_guess(self):
        self.assertEqual(classify_network_type("WEIRD"), (None, None))
        self.assertEqual(classify_network_type(""), (None, None))


class TestZteGoform(unittest.TestCase):
    def setUp(self):
        self.fake = FakeZte(password="pw#1")
        self.base = f"http://127.0.0.1:{self.fake.port}"
        self.now = [1000.0]

    def tearDown(self):
        self.fake.close()

    def adapter(self, password=None, **opts):
        return ZteGoform("127.0.0.1", credentials={"password": password} if password else None,
                         options={"base_url": self.base, **opts}, timeout=2, clock=lambda: self.now[0])

    def test_detect(self):
        d = ZteGoform.detect("127.0.0.1", timeout=2, base_url=self.base)
        self.assertEqual((d.vendor, d.model, d.firmware), ("ZTE", "MC888B", "FW1"))
        self.assertGreaterEqual(d.confidence, 0.9)

    def test_detect_rejects_other_devices(self):
        fake = FakeIgd()
        try:
            self.assertIsNone(ZteGoform.detect("127.0.0.1", timeout=2, base_url=f"http://127.0.0.1:{fake.port}"))
        finally:
            fake.close()

    def test_without_password_never_logs_in(self):
        r = run_sample(self.adapter())
        self.assertTrue(r.ok)
        self.assertEqual(r.facts["cell.rat"], "5G_NSA")
        self.assertEqual(r.facts["cell.service"], "full")
        self.assertTrue(r.facts["cell.data_connected"])
        self.assertNotIn("cell.cell_id", r.facts)
        self.assertEqual(self.fake.logins, 0)

    def test_no_service_is_reported(self):
        self.fake.public.update(network_type="NO_SERVICE", ppp_status="ppp_disconnected")
        f = run_sample(self.adapter()).facts
        self.assertEqual((f["cell.service"], f["cell.rat"], f["cell.data_connected"]), ("none", "NONE", False))

    def test_with_password_logs_in_once_and_adds_detail(self):
        a = self.adapter("pw#1")
        f1 = run_sample(a).facts
        f2 = run_sample(a).facts
        self.assertEqual(self.fake.logins, 1)
        self.assertEqual(f1["cell.cell_id"], "cella-1")
        self.assertEqual(f2["cell.band"], "B3 + n78")
        self.assertEqual(f2["wan.ip"], "100.64.1.2")

    def test_gives_the_session_back_when_the_owner_logs_in(self):
        a = self.adapter("pw#1", yield_s=600)
        run_sample(a)
        self.fake.owner_logs_in()
        r = run_sample(a)
        self.assertTrue(r.ok)                         # still reports the no-login facts
        self.assertNotIn("cell.cell_id", r.facts)
        self.now[0] += 300
        run_sample(a)
        self.assertEqual(self.fake.logins, 1)         # no login during the pause
        self.assertIn("in pausa", a.status()["modalità"])
        self.now[0] += 301
        self.assertIn("cell.cell_id", run_sample(a).facts)
        self.assertEqual(self.fake.logins, 2)

    def test_wrong_password_is_tried_once_then_paused(self):
        a = self.adapter("wrong", bad_password_s=3600)
        r1, r2 = run_sample(a), run_sample(a)
        self.assertTrue(r1.ok and r2.ok)
        self.assertEqual(self.fake.logins, 1)
        self.assertIn("password rifiutata", a.status()["motivo"])

    def test_unreachable_router(self):
        a = ZteGoform("127.0.0.1", options={"base_url": f"http://127.0.0.1:{closed_port()}"}, timeout=1)
        r = run_sample(a)
        self.assertFalse(r.ok)
        self.assertEqual(r.facts, {})


class TestUpnpIgd(unittest.TestCase):
    def setUp(self):
        self.fake = FakeIgd()

    def tearDown(self):
        self.fake.close()

    def detect(self):
        return UpnpIgd.detect("127.0.0.1", timeout=2, ssdp_addr=("127.0.0.1", self.fake.ssdp_port), known=())

    def test_detect_through_ssdp(self):
        d = self.detect()
        self.assertEqual((d.vendor, d.model), ("ACME", "Router 9000"))
        self.assertEqual(d.confidence, 0.95)
        self.assertEqual(d.options["conn_url"], f"http://127.0.0.1:{self.fake.port}/ctl/ip")

    def test_sample(self):
        d = self.detect()
        r = run_sample(UpnpIgd("127.0.0.1", options=d.options, timeout=2))
        self.assertEqual(r.facts, {
            "device.vendor": "ACME", "device.model": "Router 9000", "wan.status": "connected",
            "wan.uptime_s": 1234, "wan.ip": "203.0.113.7", "wan.link_up": True,
            "wan.max_down_bps": 1000000000, "wan.max_up_bps": 200000000,
            "wan.rx_bytes": 5000, "wan.tx_bytes": 700})

    def test_wan_down(self):
        d = self.detect()
        self.fake.state.update(NewConnectionStatus="Disconnected", NewExternalIPAddress="0.0.0.0",
                               NewPhysicalLinkStatus="Down", NewLastConnectionError="ERROR_NO_CARRIER")
        f = run_sample(UpnpIgd("127.0.0.1", options=d.options, timeout=2)).facts
        self.assertEqual(f["wan.status"], "disconnected")
        self.assertFalse(f["wan.link_up"])
        self.assertEqual(f["wan.last_error"], "ERROR_NO_CARRIER")
        self.assertNotIn("wan.ip", f)

    def test_device_without_wan_traffic_is_not_proposed_as_router(self):
        self.fake.state.update(NewTotalBytesReceived="0", NewLayer1DownstreamMaxBitRate="0")
        d = self.detect()
        self.assertLessEqual(d.confidence, 0.3)
        self.assertTrue(d.notes)

    def test_router_gone(self):
        d = self.detect()
        a = UpnpIgd("127.0.0.1", options=d.options, timeout=1)
        self.fake.close()
        self.fake = FakeIgd()      # tearDown closes this one
        r = run_sample(a)
        self.assertFalse(r.ok)

    def test_description_with_urlbase_and_ppp(self):
        xml = ('<root xmlns="urn:schemas-upnp-org:device-1-0"><URLBase>http://10.0.0.1:5000/</URLBase>'
               '<device><manufacturer>X</manufacturer><modelName>Y</modelName><serviceList><service>'
               '<serviceType>urn:schemas-upnp-org:service:WANPPPConnection:1</serviceType>'
               '<controlURL>ctl/ppp</controlURL></service></serviceList></device></root>')
        info = upnp_igd.parse_description(xml, "http://10.0.0.1:5000/rootDesc.xml")
        self.assertEqual(info["conn_url"], "http://10.0.0.1:5000/ctl/ppp")
        self.assertIsNone(upnp_igd.parse_description("<root/>", "http://x/"))


class TestDiscover(unittest.TestCase):
    def test_labels_and_upstream_candidate(self):
        from dobermann.discover import device_label, upstream_candidate
        self.assertEqual(device_label("FRITZ! GmbH", "FRITZ!Box 7530 AX"), "FRITZ!Box 7530 AX")
        self.assertEqual(device_label("ZTE", "MC888B"), "ZTE MC888B")
        self.assertEqual(upstream_candidate("192.168.2.100"), "192.168.2.1")
        self.assertIsNone(upstream_candidate("100.64.1.2"))      # the operator's CGNAT, not a device of ours
        self.assertIsNone(upstream_candidate("8.8.4.4"))         # public: nothing above the router
        self.assertIsNone(upstream_candidate(None))


class TestRegistry(unittest.TestCase):
    def test_nothing_recognises_a_closed_host(self):
        # 192.0.2.1 is TEST-NET-1: never routed, so every detect() must give up quickly
        self.assertEqual(adapters.detect_all("192.0.2.1", timeout=1), [])


class TestFactTypes(unittest.TestCase):
    def test_wan_ip_must_be_an_address(self):
        from dobermann import facts
        clean, dropped = facts.validate({"wan.ip": "<img src=x onerror=alert(1)>"})
        self.assertEqual((clean, dropped), ({}, ["wan.ip"]))
        self.assertEqual(facts.validate({"wan.ip": " 100.64.1.2 "})[0], {"wan.ip": "100.64.1.2"})


if __name__ == "__main__":
    unittest.main()
