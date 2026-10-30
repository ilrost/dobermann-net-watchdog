"""SQLite storage: samples, device facts, incidents, IP changes, hourly roll-ups, mails waiting to go, speed tests.

Nothing here is tied to one network: no vendor-specific
columns (device data is in node_facts, as facts), no Proxmox tables. Raw samples and facts are
kept `retention_days`; hourly roll-ups, incidents and IP changes are kept for good.

node_facts is written when a device's facts change, during an incident, and otherwise at most every
FACTS_HEARTBEAT_S. Signal readings jitter on every sample: storing all of them would be most of
the database, and a 30-second resolution is plenty for "what was the modem doing around 01:12".
"""
import json
import os
import sqlite3

SCHEMA_VERSION = 1
FACTS_HEARTBEAT_S = 30
# facts that change on every reading without meaning anything changed
VOLATILE_FACTS = {"wan.rx_bytes", "wan.tx_bytes", "wan.uptime_s", "cell.rssi_dbm", "cell.lte_rsrp_dbm",
                  "cell.nr_rsrp_dbm", "cell.sinr_db", "cell.bars"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS samples (
    ts REAL PRIMARY KEY,
    gw_loss REAL, gw_rtt REAL,
    anchors_json TEXT, internet_loss REAL, internet_rtt REAL,
    dns_local_ok INTEGER, dns_local_ms REAL, dns_ext_ok INTEGER, dns_ext_ms REAL,
    wan_ip TEXT, wan_status TEXT, wan_link_up INTEGER, wan_uptime_s INTEGER, wan_last_error TEXT,
    rx_bps INTEGER, tx_bps INTEGER,
    state TEXT
);
CREATE TABLE IF NOT EXISTS node_facts (
    ts REAL, node TEXT, facts_json TEXT,
    PRIMARY KEY (node, ts)
);
CREATE TABLE IF NOT EXISTS incidents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    start_ts REAL, end_ts REAL, cause TEXT, samples_count INTEGER,
    max_gw_loss REAL, max_internet_loss REAL, max_rtt REAL,
    ip_before TEXT, ip_after TEXT, max_rx_bps INTEGER, max_tx_bps INTEGER,
    detail TEXT, phases_json TEXT, signal_note TEXT
);
CREATE TABLE IF NOT EXISTS ip_changes (ts REAL PRIMARY KEY, ip_before TEXT, ip_after TEXT);
CREATE TABLE IF NOT EXISTS hourly_stats (
    hour_ts INTEGER PRIMARY KEY, samples_count INTEGER, ok_count INTEGER,
    avg_rtt REAL, min_rtt REAL, max_rtt REAL, avg_loss REAL, max_loss REAL,
    avg_rx_bps REAL, avg_tx_bps REAL, max_rx_bps REAL, max_tx_bps REAL
);
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT, created REAL, subject TEXT, body TEXT, attempts INTEGER, next_try REAL
);
CREATE TABLE IF NOT EXISTS speedtests (
    ts REAL PRIMARY KEY, end_ts REAL, down_bps INTEGER, up_bps INTEGER, bytes INTEGER, server TEXT, error TEXT
);
CREATE INDEX IF NOT EXISTS incidents_start ON incidents (start_ts);
CREATE INDEX IF NOT EXISTS node_facts_ts ON node_facts (ts);
"""


def get_meta(con, key):
    row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def set_meta(con, key, value):
    con.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, str(value)))


def connect(path):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    con.execute("INSERT OR IGNORE INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
    con.commit()
    return con


def _b(v):
    return None if v is None else int(bool(v))


def insert_sample(con, obs, state, rx_bps, tx_bps):
    w = obs.wan
    con.execute(
        "INSERT OR REPLACE INTO samples VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (obs.ts, obs.gw_loss, obs.gw_rtt, json.dumps(obs.anchors), obs.internet_loss, obs.internet_rtt,
         _b(obs.dns_local_ok), obs.dns_local_ms, _b(obs.dns_ext_ok), obs.dns_ext_ms,
         w.get("wan.ip"), w.get("wan.status"), _b(w.get("wan.link_up")), w.get("wan.uptime_s"),
         w.get("wan.last_error"), rx_bps, tx_bps, state))


class FactsWriter:
    """Writes node_facts on change, during incidents, or on the heartbeat (see module doc)."""

    def __init__(self):
        self._last = {}   # node -> (ts, stable facts)

    def write(self, con, ts, node, facts, force=False):
        stable = {k: v for k, v in facts.items() if k not in VOLATILE_FACTS}
        last = self._last.get(node)
        if not force and last and last[1] == stable and ts - last[0] < FACTS_HEARTBEAT_S:
            return False
        con.execute("INSERT OR REPLACE INTO node_facts VALUES (?,?,?)", (ts, node, json.dumps(facts)))
        self._last[node] = (ts, stable)
        return True


def facts_before(con, node, ts):
    """The last reading strictly before `ts`: the state before an incident, not its first sample."""
    row = con.execute("SELECT facts_json FROM node_facts WHERE node=? AND ts<? ORDER BY ts DESC LIMIT 1",
                      (node, ts)).fetchone()
    return json.loads(row[0]) if row else None


def facts_after(con, node, ts, within):
    row = con.execute("SELECT facts_json FROM node_facts WHERE node=? AND ts>=? AND ts<=? ORDER BY ts LIMIT 1",
                      (node, ts, ts + within)).fetchone()
    return json.loads(row[0]) if row else None


def facts_between(con, node, start, end):
    return [json.loads(r[0]) for r in con.execute(
        "SELECT facts_json FROM node_facts WHERE node=? AND ts>=? AND ts<=? ORDER BY ts", (node, start, end))]


def insert_ip_change(con, ts, before, after):
    con.execute("INSERT OR REPLACE INTO ip_changes VALUES (?,?,?)", (ts, before, after))


def insert_incident(con, inc, cause, detail, note):
    cur = con.execute(
        """INSERT INTO incidents (start_ts, end_ts, cause, samples_count, max_gw_loss, max_internet_loss,
           max_rtt, ip_before, ip_after, max_rx_bps, max_tx_bps, detail, phases_json, signal_note)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (inc["start_ts"], inc["end_ts"], cause, inc["samples_count"], inc["max_gw_loss"],
         inc["max_internet_loss"], inc["max_rtt"], inc["ip_before"], inc["ip_after"],
         inc["max_rx_bps"], inc["max_tx_bps"], detail, json.dumps(inc["phases"]), note))
    return cur.lastrowid


def count_incidents(con, cause, since):
    return con.execute("SELECT count(*) FROM incidents WHERE cause=? AND start_ts>=?", (cause, since)).fetchone()[0]


def rollup_hour(con, hour_ts):
    row = con.execute(
        """SELECT count(*), sum(state='OK'), avg(internet_rtt), min(internet_rtt), max(internet_rtt),
                  avg(internet_loss), max(internet_loss), avg(rx_bps), avg(tx_bps), max(rx_bps), max(tx_bps)
           FROM samples WHERE ts >= ? AND ts < ?""", (hour_ts, hour_ts + 3600)).fetchone()
    if row and row[0]:
        con.execute("INSERT OR REPLACE INTO hourly_stats VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (hour_ts, *row))


def maintain(con, now, retention_days):
    """Rolls finished hours up and drops raw rows past retention. Cheap when there is nothing to do."""
    now_hour = int(now // 3600) * 3600
    last = con.execute("SELECT max(hour_ts) FROM hourly_stats").fetchone()[0]
    first = con.execute("SELECT min(ts) FROM samples").fetchone()[0]
    if last is not None:
        start = int(last) + 3600
    elif first is not None:
        start = int(first // 3600) * 3600
    else:
        start = now_hour
    for h in range(start, now_hour, 3600):
        rollup_hour(con, h)
    cutoff = now - retention_days * 86400
    con.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))
    con.execute("DELETE FROM node_facts WHERE ts < ?", (cutoff,))
    con.commit()
