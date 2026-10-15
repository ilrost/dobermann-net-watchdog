"""The sampling loop: measure, read the chain, classify, track incidents, store, notify.

    python3 -m dobermann run

Configuration is re-read when one of its files changes, so an edit (from the panel or by hand)
applies on the next sample without restarting the service.
"""
import os
import threading
import time

from dobermann import alerts, config, diagnosis, i18n, narrative, notify, probes, store, updates
from dobermann.i18n import _
from dobermann.chain import Chain
from dobermann.incidents import Tracker

MAINTAIN_EVERY_S = 600
DB_SIZE_ALERT_BYTES = 500 * 1024 * 1024   # a safety net, not an expected size
DB_SIZE_ALERT_COOLDOWN_S = 7 * 86400
SUMMARY_CHECK_EVERY_S = 60
SUMMARY_META = "last_summary_slot"
COUNTER_WRAP = 2 ** 32                    # UPnP byte counters are 32-bit on many routers


def _rate(prev, cur, dt, max_bps=None):
    """Bytes/s from two readings of a counter; None across a reset.

    A decrease is a 32-bit wrap only if the counter was in its last GiB and the rate that results is
    possible on this line; anything else is a reboot, where a wrap would invent a huge rate."""
    if prev is None or cur is None or dt <= 0:
        return None
    delta = cur - prev
    if delta < 0:
        if prev < COUNTER_WRAP - 2 ** 30:
            return None
        delta += COUNTER_WRAP
        if max_bps and delta * 8 / dt > max_bps * 1.1:
            return None
    return int(delta / dt)


class Engine:
    def __init__(self, db_path=None, async_mail=True, update_fetch=None):
        self.con = store.connect(db_path or config.db_file())
        self.facts_writer = store.FactsWriter()
        self.async_mail = async_mail
        self._mtimes = None
        self._results = {}
        self._bad_mtimes = None
        self.tracker = None
        self._reload()
        self.tracker = Tracker(self.cfg["thresholds"]["confirm_samples"], self._on_open, self._on_close)
        self.prev_ip = None
        self.ip_change_until = 0.0
        self.prev_bytes = None          # (ts, rx, tx)
        self.last_alert_ts = 0.0
        self.last_maintain_ts = 0.0
        self.db_alert_ts = 0.0
        self.summary_check_ts = 0.0
        self.update_fetch = update_fetch
        self.update_check_ts = (updates.load_status() or {}).get("ts", 0.0)   # a restart does not ask again
        self.outbox = []                # (subject, body) sent, for tests and logs

    # -- configuration --------------------------------------------------------------------------

    def _reload(self):
        """All or nothing: a file that fails to load leaves the previous configuration running, and
        the edit is retried on the next sample."""
        mtimes = config.mtimes()
        cfg = config.load()
        i18n.set_language(cfg.get("language"))
        secrets = config.load_secrets()
        chain = Chain(config.load_chain(), secrets)
        self.cfg, self.secrets, self.chain = cfg, secrets, chain
        self.dns = {"local": cfg["dns"]["local"] or config.system_resolver(),
                    "external": cfg["dns"]["external"] or None}
        self._mtimes = mtimes
        if self.tracker is not None:
            self.tracker.confirm = self.cfg["thresholds"]["confirm_samples"]

    def interval(self):
        try:
            return max(1.0, min(60.0, float(self.cfg["interval_s"])))
        except (TypeError, ValueError):
            return float(config.DEFAULTS["interval_s"])

    # -- notifications --------------------------------------------------------------------------

    def _mail(self, subject, body):
        self.outbox.append((subject, body))
        mail_cfg = self.cfg["mail"]
        if not mail_cfg.get("enabled"):
            return
        if self.async_mail:   # an SMTP timeout must not delay the next sample
            threading.Thread(target=notify.send, args=(mail_cfg, self.secrets, subject, body), daemon=True).start()
        else:
            notify.send(mail_cfg, self.secrets, subject, body)

    def _on_open(self, inc, obs, cause):
        due, cause = alerts.should_alert(inc, obs.ts, alerts.rules(self.cfg["mail"]))
        if not due:
            return False    # not (yet) worth a mail under the user's rule for this kind of event
        if obs.ts - self.last_alert_ts <= self.cfg["mail"]["cooldown_s"]:
            return False    # a flapping connection: asked again on the next sample
        since = next(p["start_ts"] for p in inc["phases"] if p["cause"] == cause)
        subject, body = narrative.open_report(cause, inc, obs, self.chain, self._results, self.dns,
                                              self.cfg.get("dashboard_url"), since=since)
        self._mail(subject, body)
        self.last_alert_ts = obs.ts
        return True

    def _on_close(self, inc, ok_since):
        before = after = None
        during = []
        for n in self.chain.cellular_nodes():
            before = store.facts_before(self.con, n.id, inc["start_ts"])
            after = store.facts_after(self.con, n.id, inc["end_ts"], diagnosis.SIGNAL_LOOKAHEAD_S)
            during = store.facts_between(self.con, n.id, inc["start_ts"], inc["end_ts"])
            break    # one cellular modem per chain is the only case seen so far
        cause, note = diagnosis.close_cause(inc["phases"], inc["ip_before"], inc["ip_after"], before, after, during)
        dur = inc["end_ts"] - inc["start_ts"]
        detail = _("{n} campioni, durata {d}s", n=inc["samples_count"], d=f"{dur:.0f}")
        if len(inc["phases"]) > 1:
            detail += " -- " + " → ".join(
                f"{p['cause']}({max(p['end_ts'] - p['start_ts'], self.interval()):.0f}s)" for p in inc["phases"])
        if note:
            detail += f" -- {note}"
        inc_id = store.insert_incident(self.con, inc, cause, detail, note)
        self.con.commit()
        if inc["alerted"]:
            notify.flush_relay_queue()
            self._mail(*narrative.close_report(self.con, inc_id, cause, inc, ok_since, note, self.chain, self.dns,
                                               self.cfg.get("dashboard_url")))
            self.last_alert_ts = ok_since

    # -- one sample -----------------------------------------------------------------------------

    def measure(self, ts):
        """Everything dobermann measures itself (level 1) for one sample, plus the chain's facts."""
        gw = self.chain.gateway
        gw_loss, gw_rtt = probes.ping(gw.ip) if gw else (0.0, None)
        anchors = {}
        for name, ip in self.cfg["anchors"]:
            loss, rtt = probes.ping(ip)
            anchors[name] = {"ip": ip, "loss": loss, "rtt": rtt}
        rtts = [a["rtt"] for a in anchors.values() if a["rtt"] is not None]
        dom = self.cfg["dns"]["test_domain"]
        local_ok, local_ms = probes.dns_query(self.dns["local"], dom) if self.dns["local"] else (None, None)
        ext_ok, ext_ms = probes.dns_query(self.dns["external"], dom) if self.dns["external"] else (None, None)
        results = self.chain.sample()
        return dict(gw_loss=gw_loss, gw_rtt=gw_rtt, anchors=anchors,
                    internet_loss=sum(a["loss"] for a in anchors.values()) / max(1, len(anchors)),
                    internet_rtt=sum(rtts) / len(rtts) if rtts else None,
                    dns_local_ok=local_ok, dns_local_ms=local_ms, dns_ext_ok=ext_ok, dns_ext_ms=ext_ms), results

    def step(self, ts=None, measured=None):
        """One sample. `measured` (from measure()) can be injected by tests."""
        mtimes = config.mtimes()
        if mtimes != self._mtimes:
            try:
                self._reload()
            except (OSError, ValueError) as e:
                # keep measuring with the previous configuration; say so once per broken edit
                if self._bad_mtimes != mtimes:
                    print(_("configurazione non valida, resta quella precedente: {v}", v=f"{type(e).__name__}: {e}"),
                          flush=True)
                    self._bad_mtimes = mtimes
        ts = time.time() if ts is None else ts
        m, results = measured if measured is not None else self.measure(ts)
        self._results = results

        wan_node = self.chain.wan_node(results)
        wan = results[wan_node.id].facts if wan_node else {}
        cells = {n.id: results[n.id].facts for n in self.chain.cellular_nodes() if n.id in results}
        fallback = fallback_cell = False
        if wan_node and wan.get("wan.ip"):
            up = self.chain.upstream_of(wan_node)
            if up is not None and diagnosis.in_network(wan["wan.ip"], up.lan_network()):
                fallback, fallback_cell = True, "cell.service" in up.provides

        wan_ip = wan.get("wan.ip")
        if self.prev_ip is not None and wan_ip is not None and wan_ip != self.prev_ip:
            store.insert_ip_change(self.con, ts, self.prev_ip, wan_ip)
            self.ip_change_until = ts + diagnosis.IP_CHANGE_WINDOW_S
        if wan_ip is not None:
            self.prev_ip = wan_ip

        obs = diagnosis.Observation(ts=ts, wan=wan, cells=cells, fallback_lease=fallback,
                                    fallback_upstream_cellular=fallback_cell,
                                    ip_changed_recently=ts < self.ip_change_until, **m)
        state = diagnosis.classify(obs, self.cfg["thresholds"])

        rx_bps = tx_bps = None
        rx, tx = wan.get("wan.rx_bytes"), wan.get("wan.tx_bytes")
        if self.prev_bytes is not None:
            pts, prx, ptx = self.prev_bytes
            rx_bps = _rate(prx, rx, ts - pts, wan.get("wan.max_down_bps"))
            tx_bps = _rate(ptx, tx, ts - pts, wan.get("wan.max_up_bps"))
        self.prev_bytes = (ts, rx, tx) if rx is not None or tx is not None else None

        store.insert_sample(self.con, obs, state, rx_bps, tx_bps)
        for node_id, r in results.items():
            if r.facts:
                self.facts_writer.write(self.con, ts, node_id, r.facts, force=state != "OK")
        self.tracker.feed(obs, state, self.interval(), wan_ip, rx_bps, tx_bps)
        self.con.commit()

        if ts - self.last_maintain_ts > MAINTAIN_EVERY_S:
            store.maintain(self.con, ts, self.cfg["retention_days"])
            self.last_maintain_ts = ts
            self._check_db_size(ts)
        if ts - self.summary_check_ts >= SUMMARY_CHECK_EVERY_S:
            self.summary_check_ts = ts
            self._maybe_summary(ts)
        if updates.due(self.cfg, ts, self.update_check_ts):
            self.update_check_ts = ts
            # in a thread: GitHub slow or unreachable (the internet may be down) must not delay a sample
            kw = {"fetch": self.update_fetch} if self.update_fetch else {}
            threading.Thread(target=updates.check_and_save, kwargs=dict(kw, now=ts), daemon=True,
                             name="update-check").start()
        return state

    def _maybe_summary(self, ts):
        """The periodic summary, once per scheduled slot. The slot sent last is kept in the database, so a
        restart neither repeats nor skips one; on the very first run the current slot only becomes the
        starting point (a summary of a period dobermann did not watch would say nothing true)."""
        s = alerts.summary_cfg(self.cfg["mail"])
        slot = alerts.last_slot(ts, s)
        if slot is None:
            return
        last = store.get_meta(self.con, SUMMARY_META)
        if last is not None and float(last) >= slot:
            return
        store.set_meta(self.con, SUMMARY_META, slot)
        self.con.commit()
        if last is None:
            return
        since, until = alerts.period_for(slot, s)
        title = _("Riepilogo del giorno") if s["frequency"] == "daily" else _("Riepilogo della settimana")
        radio = self.chain.radio_uplink()
        subject, body, quiet = alerts.summary_message(self.con, since, until, alerts.rules(self.cfg["mail"]),
                                                      self.cfg.get("dashboard_url"), title=title,
                                                      radio_node=radio.id if radio else None)
        if not quiet or s["send_if_quiet"]:
            self._mail(subject, body)

    def _check_db_size(self, ts):
        try:
            size = os.path.getsize(self.con.execute("PRAGMA database_list").fetchone()[2])
        except (OSError, TypeError):
            return
        if size > DB_SIZE_ALERT_BYTES and ts - self.db_alert_ts > DB_SIZE_ALERT_COOLDOWN_S:
            self._mail("[dobermann] " + _("Database oltre la soglia"),
                       _("Il database ha superato {limit}MB (ora {size}MB) nonostante la conservazione a {days} giorni.",
                         limit=f"{DB_SIZE_ALERT_BYTES / 1e6:.0f}", size=f"{size / 1e6:.1f}",
                         days=self.cfg["retention_days"]))
            self.db_alert_ts = ts

    def run(self):
        print(_("dobermann: catena {v}", v=[(n.label, n.ip, n.adapter_id) for n in self.chain.nodes]), flush=True)
        while True:
            t0 = time.monotonic()
            try:
                self.step()
            except Exception as e:   # keep watching: one bad sample (or a bad config edit) must not end the service
                print(_("campione fallito: {v}", v=f"{type(e).__name__}: {e}"), flush=True)
            time.sleep(max(0.2, self.interval() - (time.monotonic() - t0)))
