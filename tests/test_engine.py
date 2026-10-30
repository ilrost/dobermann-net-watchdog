"""The watchdog logic: ping parsing, classification, incident tracking, and
the whole loop replayed on sample sequences shaped like real events (a carrier outage,
the 3G period after it, a routine IP renewal). No network, temp config and state dirs."""
import json
import os
import sqlite3
import tempfile
import unittest

from dobermann import config, diagnosis, probes
from dobermann.adapters.base import SampleResult
from dobermann.diagnosis import Observation, classify, close_cause
from dobermann.engine import Engine, _rate
from dobermann.incidents import Tracker

TH = config.DEFAULTS["thresholds"]


class TestPing(unittest.TestCase):
    def test_fractional_loss_is_partial_not_total(self):
        out = "3 packets transmitted, 2 received, 33.3333% packet loss, time 402ms\nrtt min/avg/max/mdev = 9.1/10.5/12.0/1.2 ms"
        self.assertEqual(probes.parse_ping_output(out), (100 / 3, 10.5))

    def test_nothing_received_has_no_rtt_even_with_a_late_duplicate(self):
        out = "3 packets transmitted, 0 received, +1 duplicates, 100% packet loss\nrtt min/avg/max/mdev = 5.0/5.0/5.0/0 ms"
        self.assertEqual(probes.parse_ping_output(out), (100.0, None))

    def test_garbage_is_total_loss(self):
        self.assertEqual(probes.parse_ping_output(""), (100.0, None))


def obs(**kw):
    base = dict(ts=0, gw_loss=0, internet_loss=0, internet_rtt=20, dns_local_ok=True, dns_ext_ok=True)
    base.update(kw)
    return Observation(**base)


DOWN = dict(internet_loss=100, internet_rtt=None, dns_local_ok=False, dns_ext_ok=False)


class TestClassify(unittest.TestCase):
    def test_order_of_the_rules(self):
        self.assertEqual(classify(obs(gw_loss=100, **DOWN), TH), "GATEWAY_UNRESPONSIVE")
        self.assertEqual(classify(obs(cells={"m": {"cell.service": "none"}}, **DOWN), TH), "CELLULAR_NO_SERVICE")
        self.assertEqual(classify(obs(wan={"wan.link_up": False}, **DOWN), TH), "LOCAL_LINK_DOWN")
        self.assertEqual(classify(obs(wan={"wan.status": "disconnected"}, **DOWN), TH), "WAN_DISCONNECTED")
        self.assertEqual(classify(obs(ip_changed_recently=True, **DOWN), TH), "IP_CHANGE_DROP")
        self.assertEqual(classify(obs(**DOWN), TH), "UPSTREAM_OUTAGE")
        self.assertEqual(classify(obs(dns_local_ok=False), TH), "DNS_FAILURE_LOCAL")
        self.assertEqual(classify(obs(dns_local_ok=False, dns_ext_ok=False), TH), "DNS_FAILURE_UPSTREAM")
        self.assertEqual(classify(obs(internet_loss=34), TH), "PACKET_LOSS")
        self.assertEqual(classify(obs(internet_rtt=300), TH), "HIGH_LATENCY")
        self.assertEqual(classify(obs(), TH), "OK")

    def test_no_service_outranks_the_link_and_ip_states_it_causes(self):
        o = obs(cells={"m": {"cell.service": "limited"}}, wan={"wan.link_up": False, "wan.status": "disconnected"},
                ip_changed_recently=True, **DOWN)
        self.assertEqual(classify(o, TH), "CELLULAR_NO_SERVICE")

    def test_fallback_lease_counts_only_below_a_cellular_modem(self):
        self.assertEqual(classify(obs(fallback_lease=True, fallback_upstream_cellular=True, **DOWN), TH),
                         "CELLULAR_NO_SERVICE")
        self.assertEqual(classify(obs(fallback_lease=True, **DOWN), TH), "UPSTREAM_OUTAGE")

    def test_no_service_needs_internet_actually_down(self):
        self.assertEqual(classify(obs(internet_rtt=300, cells={"m": {"cell.service": "none"}}), TH), "HIGH_LATENCY")

    def test_without_a_local_resolver_dns_is_judged_on_the_external_one(self):
        self.assertEqual(classify(obs(dns_local_ok=None), TH), "OK")
        self.assertEqual(classify(obs(dns_local_ok=None, dns_ext_ok=False), TH), "DNS_FAILURE_UPSTREAM")
        self.assertEqual(classify(obs(dns_ext_ok=None), TH), "OK")
        self.assertEqual(classify(obs(dns_local_ok=False, dns_ext_ok=None), TH), "DNS_FAILURE_LOCAL")
        self.assertEqual(classify(obs(dns_local_ok=None, dns_ext_ok=None), TH), "OK")


def phases(*items):
    return [{"cause": c, "start_ts": s, "end_ts": e, "samples_count": 1} for c, s, e in items]


class TestCloseCause(unittest.TestCase):
    def test_outage_ending_on_a_new_ip_is_not_a_routine_renewal(self):
        cause, note = close_cause(phases(("UPSTREAM_OUTAGE", 1000, 1030), ("CELLULAR_NO_SERVICE", 1035, 1900),
                                         ("LOCAL_LINK_DOWN", 1905, 1905)), "100.64.0.1", "100.72.0.1",
                                  {"cell.rat_raw": "ENDC", "cell.lte_rsrp_dbm": -101.0},
                                  {"cell.rat_raw": "HSPA+", "cell.lte_rsrp_dbm": -117.0})
        self.assertEqual(cause, "CELLULAR_NO_SERVICE")
        self.assertIn("ENDC->HSPA+", note)

    def test_routine_renewal(self):
        ph = phases(("LOCAL_LINK_DOWN", 1000, 1005), ("WAN_DISCONNECTED", 1010, 1030))
        cause, note = close_cause(ph, "100.64.0.1", "100.64.0.2", {"cell.rat_raw": "ENDC", "cell.lte_rsrp_dbm": -95.0},
                                  {"cell.rat_raw": "ENDC", "cell.lte_rsrp_dbm": -96.0})
        self.assertEqual(cause, "IP_RENEWAL_CYCLE")
        self.assertIn("segnale stabile", note)
        # no signal measured on both sides: nothing to say about the signal
        _c, note = close_cause(ph, "100.64.0.1", "100.64.0.2", {"cell.rat_raw": "ENDC"}, {"cell.rat_raw": "ENDC"})
        self.assertIsNone(note)

    def test_long_renewal_shaped_incident_is_an_outage(self):
        """Link drops and IP settling, but hours without internet in between."""
        p = phases(("LOCAL_LINK_DOWN", 0, 5), ("IP_CHANGE_DROP", 10, 130), ("UPSTREAM_OUTAGE", 135, 4000),
                   ("WAN_DISCONNECTED", 4005, 4010), ("UPSTREAM_OUTAGE", 4015, 9000))
        self.assertEqual(close_cause(p, "10.0.0.1", "10.0.0.2")[0], "UPSTREAM_OUTAGE")
        self.assertEqual(close_cause(p[:2], "10.0.0.1", "10.0.0.2")[0], "IP_RENEWAL_CYCLE")   # 130 s: still a renewal

    def test_latency_mostly_on_3g_is_the_fallback(self):
        during = [{"cell.rat": "3G", "cell.rat_raw": r} for r in ("HSPA+", "HSPA", "HSPA+")]
        cause, note = close_cause(phases(("HIGH_LATENCY", 1000, 1010)), None, None, cell_during=during)
        self.assertEqual(cause, "CELLULAR_3G_FALLBACK")
        self.assertIn("HSPA+", note)

    def test_latency_on_4g_5g_or_without_modem_data_stays_latency(self):
        during = [{"cell.rat": "5G_NSA"}, {"cell.rat": "3G"}, {"cell.rat": "5G_NSA"}]
        self.assertEqual(close_cause(phases(("HIGH_LATENCY", 1, 2)), None, None, cell_during=during)[0], "HIGH_LATENCY")
        self.assertEqual(close_cause(phases(("PACKET_LOSS", 1, 2)), None, None)[0], "PACKET_LOSS")


class TestTracker(unittest.TestCase):
    def setUp(self):
        self.opened, self.closed = [], []
        self.t = Tracker(2, on_open=lambda inc, o, c: self.opened.append(c) or True,
                         on_close=lambda inc, ok: self.closed.append(inc))

    def feed(self, ts, state):
        self.t.feed(obs(ts=ts), state, 5)

    def test_single_noisy_sample_is_not_an_incident(self):
        for ts, st in ((0, "OK"), (5, "HIGH_LATENCY"), (10, "OK"), (15, "OK"), (30, "OK")):
            self.feed(ts, st)
        self.assertEqual(self.closed, [])

    def test_confirmed_noisy_cause_opens(self):
        for ts, st in ((0, "HIGH_LATENCY"), (5, "HIGH_LATENCY"), (10, "OK"), (25, "OK")):
            self.feed(ts, st)
        self.assertEqual(len(self.closed), 1)
        # the tracker asks on every sample until the answer is yes (here: yes at once); whether latency is
        # worth a mail is the user's rule, decided in alerts.py and tested there
        self.assertEqual(self.opened, ["HIGH_LATENCY"])

    def test_cascade_is_one_incident_with_phases_and_one_alert(self):
        for ts, st in ((0, "LOCAL_LINK_DOWN"), (5, "WAN_DISCONNECTED"), (10, "OK"), (15, "IP_CHANGE_DROP"),
                       (20, "OK"), (35, "OK")):
            self.feed(ts, st)
        self.assertEqual(len(self.closed), 1)
        self.assertEqual([p["cause"] for p in self.closed[0]["phases"]],
                         ["LOCAL_LINK_DOWN", "WAN_DISCONNECTED", "IP_CHANGE_DROP"])
        self.assertEqual(self.opened, ["LOCAL_LINK_DOWN"])


class TestRate(unittest.TestCase):
    def test_counter_rate_wrap_and_reset(self):
        self.assertEqual(_rate(1000, 6000, 5), 1000)
        self.assertEqual(_rate(2 ** 32 - 1000, 4000, 5), 1000)
        self.assertIsNone(_rate(5000, 100, 5))
        # a reboot after 3 GB of traffic is not a wrap
        self.assertIsNone(_rate(3_000_000_000, 5_000_000, 5))
        # a wrap that would exceed the line speed is not one either
        self.assertIsNone(_rate(2 ** 32 - 1000, 2 ** 30, 5, max_bps=100_000_000))


# --- the whole loop ---------------------------------------------------------------------------

CHAIN = {"version": 1, "nodes": [
    {"id": "router", "role": "router", "label": "Router di prova", "ip": "127.0.0.1", "adapter": "upnp_igd",
     "options": {"conn_url": "http://127.0.0.1:9/x", "conn_type": "t", "common_url": "http://127.0.0.1:9/y",
                 "common_type": "t"}},
    {"id": "modem", "role": "modem", "label": "Modem di prova", "ip": "192.168.2.1", "adapter": "zte_goform",
     "options": {"base_url": "http://127.0.0.1:9"}},
]}


def measured(internet_loss=0.0, rtt=20.0, wan=None, cell=None, gw_loss=0.0):
    m = dict(gw_loss=gw_loss, gw_rtt=1.0, anchors={"a": {"ip": "192.0.2.10", "loss": internet_loss, "rtt": rtt}},
             internet_loss=internet_loss, internet_rtt=None if internet_loss >= 100 else rtt,
             dns_local_ok=internet_loss < 100, dns_local_ms=5.0, dns_ext_ok=internet_loss < 100, dns_ext_ms=9.0)
    results = {}
    if wan is not None:
        results["router"] = SampleResult(facts=wan, ok=bool(wan))
    if cell is not None:
        results["modem"] = SampleResult(facts=cell, ok=bool(cell))
    return m, results


def wan(ip, link=True, status="connected"):
    return {"wan.ip": ip, "wan.link_up": link, "wan.status": status}


def cell(rat_raw, rat, service="full", rsrp=-100.0):
    return {"cell.rat_raw": rat_raw, "cell.rat": rat, "cell.service": service, "cell.lte_rsrp_dbm": rsrp}


class EngineCase(unittest.TestCase):
    chain = CHAIN

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = {k: os.environ.get(k) for k in ("DOBERMANN_CONFIG_DIR", "DOBERMANN_STATE_DIR")}
        os.environ["DOBERMANN_CONFIG_DIR"] = os.path.join(self.tmp.name, "etc")
        os.environ["DOBERMANN_STATE_DIR"] = os.path.join(self.tmp.name, "var")
        config.save_chain(self.chain)
        config.save({"dns": {"local": "192.0.2.53"}})
        self.engine = Engine(async_mail=False)
        self.ts = 1_000_000.0

    def tearDown(self):
        self.engine.con.close()
        for k, v in self.env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def run_samples(self, n, **kw):
        for _ in range(n):
            self.engine.step(ts=self.ts, measured=measured(**kw))
            self.ts += 5

    def incidents(self):
        con = sqlite3.connect(config.db_file())
        rows = con.execute("SELECT cause, ip_before, ip_after, signal_note FROM incidents ORDER BY id").fetchall()
        con.close()
        return rows


class TestEngineReplay(EngineCase):
    def test_carrier_outage_then_3g(self):
        """A carrier outage in miniature: the modem loses the network and leases the router a local
        address; it comes back on a far 3G cell with high latency for a while."""
        config.save({"dns": {"local": "192.0.2.53"}, "mail": {"rules": {"outage": {"mode": "immediate"}}}})
        self.run_samples(4, wan=wan("100.64.0.1"), cell=cell("ENDC", "5G_NSA", rsrp=-101.0))
        self.run_samples(3, internet_loss=100, wan=wan("192.168.2.100"),
                         cell=cell("LIMITED_SERVICE_LTE", "NONE", "limited"))
        self.run_samples(2, internet_loss=100, wan=wan("192.168.2.100"), cell={})   # modem not answering
        self.run_samples(4, wan=wan("100.72.0.1"), cell=cell("HSPA+", "3G", rsrp=-117.0))
        self.run_samples(6, rtt=300, wan=wan("100.72.0.1"), cell=cell("HSPA+", "3G", rsrp=-117.0))
        self.run_samples(4, wan=wan("100.72.0.1"), cell=cell("HSPA+", "3G", rsrp=-117.0))

        rows = self.incidents()
        self.assertEqual([r[0] for r in rows], ["CELLULAR_NO_SERVICE", "CELLULAR_3G_FALLBACK"])
        self.assertEqual(rows[0][1:3], ("100.64.0.1", "100.72.0.1"))
        self.assertIn("LTE RSRP -101->-117dBm", rows[0][3])
        subjects = [s for s, _ in self.engine.outbox]
        self.assertEqual(len(subjects), 2)
        self.assertIn("ha perso la rete dell'operatore", subjects[0])
        self.assertIn("Risolto", subjects[1])
        opening, report = self.engine.outbox[0][1], self.engine.outbox[1][1]
        self.assertIn("Router di prova ✔ → Modem di prova ✔ → operatore ✘", opening)
        self.assertIn("Modem di prova: senza servizio", opening)
        for section in ("DOVE", "PERCHÉ", "PROVE", "QUANTO", "COSA FARE"):
            self.assertIn(section, report)
        self.assertIn("rete mobile: prima 5G (NSA), durante senza servizio", report)
        self.assertIn("segnale 4G: -101 dBm prima, -117 dBm dopo (calo marcato)", report)
        self.assertIn("indirizzo IP: 100.64.0.1 → 100.72.0.1", report)

    def test_default_rules_short_drop_is_silent_long_one_mails(self):
        self.run_samples(3, wan=wan("100.64.0.1"), cell=cell("ENDC", "5G_NSA"))
        # a 30 s renewal: starts as "WAN disconnected", ends with a new IP -> no mail
        self.run_samples(6, internet_loss=100, wan=wan(None, link=False, status="disconnected"), cell=cell("ENDC", "5G_NSA"))
        self.run_samples(5, wan=wan("100.64.0.2"), cell=cell("ENDC", "5G_NSA"))
        self.assertEqual(self.engine.outbox, [])
        # an outage that lasts 3 minutes -> one mail once it passed 2 minutes, then "back"
        self.run_samples(36, internet_loss=100, wan=wan(None, link=False, status="disconnected"), cell=cell("ENDC", "5G_NSA"))
        self.run_samples(5, wan=wan("100.64.0.3"), cell=cell("ENDC", "5G_NSA"))
        subjects = [s for s, _ in self.engine.outbox]
        self.assertEqual(len(subjects), 2, subjects)
        self.assertIn("ancora in corso", self.engine.outbox[0][1])
        self.assertIn("Risolto", subjects[1])

    def test_mails_wait_in_the_queue_while_the_provider_is_down_and_keep_their_order(self):
        from unittest import mock
        from dobermann import mailqueue
        config.save({"dns": {"local": "192.0.2.53"},
                     "mail": {"enabled": True, "to": "io@example.org", "rules": {"outage": {"mode": "immediate"}}}})
        taken, provider_up = [], [False]

        def deliver(mail_cfg, secrets, subject, body):
            if not provider_up[0]:
                return False, "server non raggiungibile"
            taken.append(subject)
            return True, None
        with mock.patch.object(mailqueue.notify, "deliver", deliver), mock.patch("builtins.print"):
            self.run_samples(2, wan=wan("100.64.0.1"))
            self.run_samples(4, internet_loss=100, wan=wan(None, link=False, status="disconnected"))
            self.assertEqual(taken, [])                         # the "down" mail could not go
            provider_up[0] = True
            self.run_samples(6, wan=wan("100.64.0.1"))          # back: the queue is tried at once
        self.assertEqual(len(taken), 2, taken)
        self.assertNotIn("Risolto", taken[0])
        self.assertIn("Risolto", taken[1])
        self.assertEqual(mailqueue.status(self.engine.con)["waiting"], 0)

    def test_the_slowness_of_a_speed_test_is_not_an_event(self):
        import time as _t
        from dobermann import speedtest, store
        store.set_meta(self.engine.con, speedtest.RUNNING_META, _t.time() + 60)
        self.engine.con.commit()
        self.ts = _t.time()
        self.run_samples(6, rtt=400, wan=wan("100.64.0.1"))
        store.set_meta(self.engine.con, speedtest.RUNNING_META, 0)
        self.run_samples(6, rtt=400, wan=wan("100.64.0.1"))   # without a test, the same latency is one
        self.run_samples(6, wan=wan("100.64.0.1"))
        self.assertEqual([r[0] for r in self.incidents()], ["HIGH_LATENCY"])

    def test_summary_once_per_slot_and_not_on_first_run(self):
        import time as _t
        config.save({"dns": {"local": "192.0.2.53"}, "mail": {"summary": {"frequency": "daily", "hour": 8}}})
        lt = _t.localtime(self.ts)
        self.ts = _t.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 7, 58, 0, 0, 0, -1))
        self.run_samples(1, wan=wan("100.64.0.1"))
        self.assertEqual(self.engine.outbox, [])            # first run: only the starting point
        self.ts += 3 * 60                                   # 08:01
        self.run_samples(30, wan=wan("100.64.0.1"))         # many checks within the same slot
        subjects = [s for s, _ in self.engine.outbox]
        self.assertEqual(len(subjects), 1, subjects)
        self.assertIn("Riepilogo del giorno", subjects[0])
        self.assertIn("tutto regolare", subjects[0])

    def test_default_rules_short_drop_is_silent_long_one_mails(self):
        self.run_samples(3, wan=wan("100.64.0.1"), cell=cell("ENDC", "5G_NSA"))
        # a 30 s renewal: starts as "WAN disconnected", ends with a new IP -> no mail
        self.run_samples(6, internet_loss=100, wan=wan(None, link=False, status="disconnected"), cell=cell("ENDC", "5G_NSA"))
        self.run_samples(5, wan=wan("100.64.0.2"), cell=cell("ENDC", "5G_NSA"))
        self.assertEqual(self.engine.outbox, [])
        # an outage that lasts 3 minutes -> one mail once it passed 2 minutes, then "back"
        self.run_samples(36, internet_loss=100, wan=wan(None, link=False, status="disconnected"), cell=cell("ENDC", "5G_NSA"))
        self.run_samples(5, wan=wan("100.64.0.3"), cell=cell("ENDC", "5G_NSA"))
        subjects = [s for s, _ in self.engine.outbox]
        self.assertEqual(len(subjects), 2, subjects)
        self.assertIn("ancora in corso", self.engine.outbox[0][1])
        self.assertIn("Risolto", subjects[1])

    def test_summary_once_per_slot_and_not_on_first_run(self):
        import time as _t
        config.save({"dns": {"local": "192.0.2.53"}, "mail": {"summary": {"frequency": "daily", "hour": 8}}})
        lt = _t.localtime(self.ts)
        self.ts = _t.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 7, 58, 0, 0, 0, -1))
        self.run_samples(1, wan=wan("100.64.0.1"))
        self.assertEqual(self.engine.outbox, [])            # first run: only the starting point
        self.ts += 3 * 60                                   # 08:01
        self.run_samples(30, wan=wan("100.64.0.1"))         # many checks within the same slot
        subjects = [s for s, _ in self.engine.outbox]
        self.assertEqual(len(subjects), 1, subjects)
        self.assertIn("Riepilogo del giorno", subjects[0])
        self.assertIn("tutto regolare", subjects[0])

    def test_routine_renewal(self):
        self.run_samples(3, wan=wan("100.64.0.1"), cell=cell("ENDC", "5G_NSA"))
        self.run_samples(2, internet_loss=100, wan=wan(None, link=False, status="disconnected"),
                         cell=cell("ENDC", "5G_NSA"))
        self.run_samples(5, wan=wan("100.64.0.2"), cell=cell("ENDC", "5G_NSA"))
        rows = self.incidents()
        self.assertEqual([r[0] for r in rows], ["IP_RENEWAL_CYCLE"])
        self.assertIn("segnale stabile", rows[0][3])

    def test_bad_config_edit_keeps_running(self):
        config.save({"dns": {"local": "192.0.2.53"}, "interval_s": "5s"})
        self.run_samples(1, wan=wan("100.64.0.1"))
        self.assertEqual(self.engine.interval(), 5.0)
        with open(config.chain_file(), "w") as f:
            f.write("{broken")
        self.assertEqual(self.engine.step(ts=self.ts, measured=measured()), "OK")   # still sampling
        self.ts += 5
        self.assertEqual(len(self.engine.chain.nodes), 2)      # previous chain still in use
        config.save_chain(CHAIN)
        self.run_samples(1, wan=wan("100.64.0.1"))             # and the fixed edit applies

    def test_config_edit_applies_without_restart(self):
        self.run_samples(1, wan=wan("100.64.0.1"))
        config.save({"dns": {"local": "192.0.2.53"}, "interval_s": 10})
        self.run_samples(1, wan=wan("100.64.0.1"))
        self.assertEqual(self.engine.interval(), 10)


RADIO_WORDS = ("5G", "4G", "3G", "rete mobile", "segnale", "cella", "modem", "Modem")


class TestWiredLine(EngineCase):
    """A fibre router alone: the report must never talk about radio."""
    chain = {"version": 1, "nodes": [CHAIN["nodes"][0]]}

    def test_wired_report_has_no_radio_words(self):
        config.save({"dns": {"local": "192.0.2.53"}, "mail": {"rules": {"outage": {"mode": "immediate"}}}})
        self.assertEqual(self.engine.chain.link_kind(), "wired")
        self.run_samples(3, wan=wan("100.64.0.1"))
        self.run_samples(4, internet_loss=100, wan=wan("100.64.0.1"))
        self.run_samples(4, wan=wan("100.64.0.1"))
        self.assertEqual(len(self.engine.outbox), 2)
        for subject, body in self.engine.outbox:
            for w in RADIO_WORDS:
                self.assertNotIn(w, subject + body, (w, body))
        self.assertIn("Router di prova ✔ → operatore ✘", self.engine.outbox[1][1])


class TestRadioDeviceNotUplink(EngineCase):
    """A radio device that is not the way out (e.g. an LTE backup before the router) is not the line."""
    chain = {"version": 1, "nodes": [CHAIN["nodes"][1], dict(CHAIN["nodes"][0], id="router2")]}

    def test_not_radio(self):
        self.assertEqual(self.engine.chain.link_kind(), "wired")
        self.assertEqual(self.engine.chain.cellular_nodes(), [])


class TestEngineWithoutAdapters(EngineCase):
    """The same outage on a router dobermann cannot read: less precise, still correct."""
    chain = {"version": 1, "nodes": [{"id": "router", "role": "router", "label": "Router", "ip": "127.0.0.1"}]}

    def test_outage_is_reported_as_beyond_the_router(self):
        self.run_samples(3)
        self.run_samples(4, internet_loss=100)
        self.run_samples(4)
        self.assertEqual([r[0] for r in self.incidents()], ["UPSTREAM_OUTAGE"])

    def test_dead_router(self):
        self.run_samples(2)
        self.run_samples(2, internet_loss=100, gw_loss=100)
        self.run_samples(4)
        self.assertEqual([r[0] for r in self.incidents()], ["GATEWAY_UNRESPONSIVE"])


class TestNoSecretsInConfig(EngineCase):
    def test_secrets_file_is_private(self):
        config.save_secrets({"mail": {"password": "x"}})
        self.assertEqual(os.stat(config.secrets_file()).st_mode & 0o777, 0o600)
        with open(config.config_file()) as f:
            self.assertNotIn("password", json.load(f).get("mail", {}))


if __name__ == "__main__":
    unittest.main()


class TestTrackerEdges(unittest.TestCase):
    def obs(self, ts, gw=0.0, net=0.0):
        return Observation(ts=ts, gw_loss=gw, internet_loss=net)

    def test_ip_reported_late_still_counts_and_blips_do_not_hold_an_incident(self):
        from dobermann.incidents import Tracker
        closed = []
        t = Tracker(confirm_samples=3, on_close=lambda inc, ok: closed.append(inc))
        t.feed(self.obs(0), "OK", 5, wan_ip="198.51.100.1")
        t.feed(self.obs(5, gw=100, net=100), "GATEWAY_UNRESPONSIVE", 5)
        t.feed(self.obs(10), "OK", 5, wan_ip=None)            # line back, no IP yet
        t.feed(self.obs(15), "OK", 5, wan_ip="198.51.100.2")  # the new IP, still within grace
        noisy = sorted(diagnosis.NOISY_CAUSES)
        for i in range(6):                                    # after the grace: blips that never confirm
            t.feed(self.obs(30 + 5 * i, net=50), noisy[i % 2] if len(noisy) > 1 else noisy[0], 5)
        self.assertEqual(len(closed), 1)
        self.assertEqual((closed[0]["ip_before"], closed[0]["ip_after"]), ("198.51.100.1", "198.51.100.2"))

    def test_the_alert_rule_sees_the_current_sample_in_the_totals(self):
        from dobermann.incidents import Tracker
        seen = []
        t = Tracker(on_open=lambda inc, obs, cause: seen.append((inc["samples_count"], inc["max_gw_loss"])) or True)
        t.feed(self.obs(0, gw=100, net=100), "GATEWAY_UNRESPONSIVE", 5)
        self.assertEqual(seen, [(1, 100)])
