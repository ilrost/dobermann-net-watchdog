"""The data behind the 'Oggi' and 'Storico' pages, from dobermann.db.

Windows: today; a past day; week/month/year to date. Raw samples exist for `retention_days`; older periods come from the hourly roll-ups.
Radio signal for the charts comes from node_facts of the chain's radio uplink only: with a wired line
there is no signal chart, even if some other device in the chain has a radio.
"""
import json
import os
import sqlite3
import time
from collections import defaultdict
from datetime import datetime, timedelta

from dobermann import config, store
from dobermann.chain import Chain

MAX_FINE_SPAN_S = 6 * 3600
SIGNAL_KEYS = (("lte_rsrp", "cell.lte_rsrp_dbm"), ("nr_rsrp", "cell.nr_rsrp_dbm"))


def _connect():
    if not os.path.exists(config.db_file()):   # the panel started before the first sample: an empty database
        store.connect(config.db_file()).close()
    con = sqlite3.connect(f"file:{config.db_file()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _interval(cfg):
    try:
        return max(1.0, min(60.0, float(cfg["interval_s"])))
    except (TypeError, ValueError):
        return float(config.DEFAULTS["interval_s"])


def radio_node_id(chain_spec):
    r = Chain(chain_spec).radio_uplink()
    return r.id if r else None


def _signal_rows(con, since, until, node=None):
    """[(ts, {"lte_rsrp": .., "nr_rsrp": ..})] from the radio uplink `node` (none: no rows)."""
    out = []
    if node is None:
        return out
    for r in con.execute("SELECT ts, facts_json FROM node_facts WHERE node = ? AND ts >= ? AND ts < ? ORDER BY ts",
                         (node, since, until)):
        f = json.loads(r["facts_json"])
        if "cell.rat" in f or "cell.lte_rsrp_dbm" in f:
            out.append((r["ts"], {k: f.get(fk) for k, fk in SIGNAL_KEYS}))
    return out


def source_info(cfg, chain_spec):
    nodes = chain_spec.get("nodes", [])
    return {
        "chain": " → ".join(["dobermann"] + [f"{n.get('label') or n['id']} ({n.get('ip')})" for n in nodes] + ["internet"])
        if nodes else None,
        "anchors": [ip for _name, ip in cfg["anchors"]],
        "dns_local": cfg["dns"]["local"] or config.system_resolver(),
        "dns_external": cfg["dns"]["external"],
        "link": Chain(chain_spec).link_description() if nodes else None,
    }


def _window_report(since, until, fill_until=None):
    now = time.time()
    until = min(until, now)
    display_end = fill_until if fill_until is not None else until
    cfg = config.load()
    chain_spec = config.load_chain()
    interval = _interval(cfg)
    retention = cfg["retention_days"]
    raw_start = min(max(since, now - retention * 86400), until)

    con = _connect()
    samples = con.execute("SELECT * FROM samples WHERE ts >= ? AND ts < ? ORDER BY ts", (raw_start, until)).fetchall()
    older_hours = con.execute("SELECT * FROM hourly_stats WHERE hour_ts >= ? AND hour_ts < ? ORDER BY hour_ts",
                              (since, raw_start)).fetchall()
    incidents = con.execute("SELECT * FROM incidents WHERE end_ts >= ? AND start_ts < ? ORDER BY start_ts",
                            (since, until)).fetchall()
    ip_changes = con.execute("SELECT * FROM ip_changes WHERE ts >= ? AND ts < ? ORDER BY ts", (since, until)).fetchall()
    signal = _signal_rows(con, raw_start, until, radio_node_id(chain_spec))
    con.close()

    total = len(samples) + sum(h["samples_count"] for h in older_hours)
    ok = sum(1 for s in samples if s["state"] == "OK") + sum(h["ok_count"] or 0 for h in older_hours)

    by_cause = defaultdict(lambda: {"count": 0, "total_seconds": 0})
    by_hour = defaultdict(lambda: {"count": 0, "total_seconds": 0})
    by_day = defaultdict(lambda: {"count": 0, "total_seconds": 0})
    by_month = defaultdict(lambda: {"count": 0, "total_seconds": 0})
    incidents_out, real_count = [], 0
    for inc in incidents:
        dur = max(inc["end_ts"] - inc["start_ts"], interval)   # a single-sample blip still lasted one interval
        phases = json.loads(inc["phases_json"]) if inc["phases_json"] else []
        incidents_out.append({**dict(inc), "duration_s": dur, "phases": phases})
        if inc["cause"] != "PING_ARTIFACT":   # a known false positive in imported history
            real_count += 1
            by_cause[inc["cause"]]["count"] += 1
            by_cause[inc["cause"]]["total_seconds"] += dur
        lt = time.localtime(inc["start_ts"])
        for bucket, key in ((by_hour, lt.tm_hour), (by_day, time.strftime("%Y-%m-%d", lt)),
                            (by_month, time.strftime("%Y-%m", lt))):
            bucket[key]["count"] += 1
            bucket[key]["total_seconds"] += dur

    def zero_fill_days(start_ts, end_ts):
        d0 = datetime.fromtimestamp(start_ts).date()
        d1 = datetime.fromtimestamp(max(end_ts - 1, start_ts)).date()
        out, d = [], d0
        while d <= d1 and len(out) < 400:
            k = d.isoformat()
            out.append({"day": k, "count": by_day[k]["count"], "total_seconds": round(by_day[k]["total_seconds"])})
            d += timedelta(days=1)
        return out

    def zero_fill_months(start_ts, end_ts):
        d0 = datetime.fromtimestamp(start_ts).date().replace(day=1)
        d1 = datetime.fromtimestamp(max(end_ts - 1, start_ts)).date().replace(day=1)
        out, y, m = [], d0.year, d0.month
        while (y, m) <= (d1.year, d1.month) and len(out) < 120:
            k = f"{y:04d}-{m:02d}"
            out.append({"month": k, "count": by_month[k]["count"], "total_seconds": round(by_month[k]["total_seconds"])})
            m += 1
            if m > 12:
                m, y = 1, y + 1
        return out

    def avg(lst):
        return round(sum(lst) / len(lst), 2) if lst else None

    timeline = []
    for h in older_hours:
        timeline.append({
            "ts": h["hour_ts"],
            "rtt_ms": round(h["avg_rtt"], 2) if h["avg_rtt"] is not None else None,
            "loss_pct": round(h["avg_loss"], 2) if h["avg_loss"] is not None else 0,
            "recv_bps": h["avg_rx_bps"], "send_bps": h["avg_tx_bps"],
            "incident_ratio": round(1 - (h["ok_count"] or 0) / h["samples_count"], 2) if h["samples_count"] else 0,
            "lte_rsrp": None, "nr_rsrp": None,
        })
    bucket_s = 300
    buckets = defaultdict(lambda: {"rtt": [], "loss": [], "recv": [], "send": [], "states": [], "lte_rsrp": [], "nr_rsrp": []})
    for s in samples:
        b = buckets[int(s["ts"] // bucket_s) * bucket_s]
        if s["internet_rtt"] is not None:
            b["rtt"].append(s["internet_rtt"])
        b["loss"].append(s["internet_loss"] or 0)
        if s["rx_bps"] is not None:
            b["recv"].append(s["rx_bps"])
        if s["tx_bps"] is not None:
            b["send"].append(s["tx_bps"])
        b["states"].append(s["state"])
    for ts, vals in signal:
        b = buckets[int(ts // bucket_s) * bucket_s]
        for k, v in vals.items():
            if v is not None:
                b[k].append(v)
    for t in sorted(buckets):
        v = buckets[t]
        timeline.append({
            "ts": t, "rtt_ms": avg(v["rtt"]), "loss_pct": avg(v["loss"]),
            "recv_bps": avg(v["recv"]), "send_bps": avg(v["send"]),
            "incident_ratio": round(sum(1 for st in v["states"] if st != "OK") / len(v["states"]), 2) if v["states"] else 0,
            "lte_rsrp": avg(v["lte_rsrp"]), "nr_rsrp": avg(v["nr_rsrp"]),
        })

    return {
        "generated_at": now,
        "window_hours": round((until - since) / 3600, 4),
        "raw_retention_days": retention,
        "raw_detail_available": raw_start < until,
        "current_interval_s": interval,
        "range_start": since,
        "range_end": until,
        "total_samples": total,
        "uptime_pct": round(ok / total * 100, 3) if total else None,
        "current_wan_ip": samples[-1]["wan_ip"] if samples else None,
        "current_wan_status": samples[-1]["wan_status"] if samples else None,
        "by_cause": dict(by_cause),
        "by_hour": [{"hour": h, "count": by_hour[h]["count"], "total_seconds": round(by_hour[h]["total_seconds"])}
                    for h in range(24)],
        "by_day": zero_fill_days(since, display_end),
        "by_month": zero_fill_months(since, display_end),
        "incidents": incidents_out,
        "real_incidents_count": real_count,
        "ip_changes": [dict(c) for c in ip_changes],
        "timeline": timeline,
        "has_cellular": bool(signal),
        "source": source_info(cfg, chain_spec),
    }


def _day_bounds(day):
    start = datetime(day.year, day.month, day.day).timestamp()
    return start, start + 86400


def build_live_report():
    """Today's calendar day, for 'Oggi'. A calendar day, not the last 24 h: the hour axis stays 0-23
    and yesterday's incidents do not leak in."""
    since, until = _day_bounds(datetime.now().date())
    rep = _window_report(since, until)
    rep["period"] = "live"
    return rep


def build_day_report(date_str=None):
    """One past day, navigable between the first day with data and yesterday."""
    con = _connect()
    row = con.execute("SELECT min(x) FROM (SELECT min(ts) AS x FROM samples UNION SELECT min(hour_ts) FROM hourly_stats)").fetchone()
    con.close()
    min_day = datetime.fromtimestamp(row[0] if row and row[0] is not None else time.time()).date()
    max_day = max(min_day, datetime.now().date() - timedelta(days=1))
    day = max_day
    if date_str:
        try:
            day = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            pass
    day = max(min_day, min(day, max_day))
    rep = _window_report(*_day_bounds(day))
    rep.update(period="day", day=day.isoformat(),
               prev_day=(day - timedelta(days=1)).isoformat() if day > min_day else None,
               next_day=(day + timedelta(days=1)).isoformat() if day < max_day else None)
    return rep


def build_period_report(period, date=None):
    """week/month/year: the calendar period up to yesterday, with the chart's empty slots kept."""
    if period == "day":
        return build_day_report(date)
    today = datetime.now().date()
    if period == "month":
        start = today.replace(day=1)
        end = start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1)
    elif period == "year":
        start = today.replace(month=1, day=1)
        end = start.replace(year=start.year + 1)
    else:
        period = "week"
        start = today - timedelta(days=today.weekday())
        end = start + timedelta(days=7)
    rep = _window_report(_day_bounds(start)[0], _day_bounds(today)[0], fill_until=_day_bounds(end)[0])
    rep["period"] = period
    return rep


def fine_samples(from_ts, to_ts):
    """Raw samples for the chart's deep zoom (at most 6 h), signal carried forward from node_facts."""
    to_ts = min(to_ts, from_ts + MAX_FINE_SPAN_S)
    con = _connect()
    rows = con.execute("SELECT ts, internet_rtt, internet_loss, rx_bps, tx_bps FROM samples "
                       "WHERE ts >= ? AND ts <= ? ORDER BY ts", (from_ts, to_ts)).fetchall()
    signal = _signal_rows(con, from_ts - 60, to_ts + 1, radio_node_id(config.load_chain()))
    con.close()
    out, i, last = [], 0, {}
    for r in rows:
        while i < len(signal) and signal[i][0] <= r["ts"]:
            last = signal[i][1]
            i += 1
        out.append({"ts": r["ts"], "rtt_ms": r["internet_rtt"], "loss_pct": r["internet_loss"],
                    "recv_bps": r["rx_bps"], "send_bps": r["tx_bps"],
                    "lte_rsrp": last.get("lte_rsrp"), "nr_rsrp": last.get("nr_rsrp")})
    return out


EXPORT_QUERIES = {
    "samples": ("SELECT ts, gw_loss, gw_rtt, internet_loss, internet_rtt, dns_local_ok, dns_ext_ok, wan_ip, "
                "wan_status, wan_link_up, rx_bps, tx_bps, state FROM samples WHERE ts >= ? AND ts < ? ORDER BY ts"),
    "hourly": ("SELECT hour_ts, samples_count, ok_count, avg_rtt, min_rtt, max_rtt, avg_loss, max_loss, "
               "avg_rx_bps, avg_tx_bps, max_rx_bps, max_tx_bps FROM hourly_stats "
               "WHERE hour_ts >= ? AND hour_ts < ? ORDER BY hour_ts"),
}


def export_rows(table, from_ts, to_ts):
    con = _connect()
    cur = con.execute(EXPORT_QUERIES[table], (from_ts, to_ts))
    cols = [d[0] for d in cur.description]
    rows = cur.fetchall()
    con.close()
    return cols, rows
