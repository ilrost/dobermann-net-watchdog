"""When to write: one rule per kind of event, and the periodic summary.

Causes are grouped into categories the user can reason about; each category has a rule:
  immediate  a mail as soon as the incident shows that cause
  after      a mail only if the incident is still open `after_min` minutes after it first showed
             that cause; one that ends sooner goes into the summary, already classified by its
             final cause (an IP renewal starts as "WAN disconnected" and is only recognised at the
             end, typically ~30 s later: this rule is what keeps it out of the mailbox)
  summary    only in the periodic summary
  never      not even there (it stays in the panel)
The "it is back" mail goes out only for an incident that had the "it is down" one.

On a real FWA line IP renewals last about half a minute, real outages many minutes: the 2-minute
default separates them.
"""
import time
from collections import OrderedDict

from dobermann import diagnosis

MODES = ("immediate", "after", "summary", "never")

CATEGORIES = OrderedDict([
    ("outage", {"label": "Internet assente",
                "causes": ("GATEWAY_UNRESPONSIVE", "CELLULAR_NO_SERVICE", "LOCAL_LINK_DOWN", "WAN_DISCONNECTED",
                           "UPSTREAM_OUTAGE", "IP_CHANGE_DROP"),
                "default": {"mode": "after", "after_min": 2}}),
    ("ip_renewal", {"label": "Rinnovo / cambio IP", "causes": ("IP_RENEWAL_CYCLE",),
                    "default": {"mode": "summary", "after_min": 2}}),
    ("slow", {"label": "Rete lenta o instabile", "causes": ("HIGH_LATENCY", "PACKET_LOSS"),
              "default": {"mode": "summary", "after_min": 5}}),
    ("legacy_rat", {"label": "Ripiego sul 3G", "causes": ("CELLULAR_3G_FALLBACK",), "radio_only": True,
                    "default": {"mode": "summary", "after_min": 5}}),
    ("dns", {"label": "DNS che non risponde", "causes": ("DNS_FAILURE_LOCAL", "DNS_FAILURE_UPSTREAM"),
             "default": {"mode": "after", "after_min": 5}}),
])
# a measurement artefact, not an event: never mailed, never summarised
IGNORED_CAUSES = {"PING_ARTIFACT"}

_CATEGORY_OF = {c: key for key, cat in CATEGORIES.items() for c in cat["causes"]}

SUMMARY_DEFAULT = {"frequency": "weekly", "weekday": 0, "hour": 8, "send_if_quiet": True}
WEEKDAYS = ("lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica")


def lower_first(label):
    """'Internet assente' -> 'internet assente', but 'DNS che non risponde' stays as it is."""
    return label[0].lower() + label[1:] if len(label) > 1 and label[1].islower() else label


def category_of(cause):
    return _CATEGORY_OF.get(cause)


def visible_categories(radio):
    """The categories that mean something on this line: the radio ones only with a radio uplink."""
    return [k for k, c in CATEGORIES.items() if radio or not c.get("radio_only")]


def rules(mail_cfg):
    """The rule of every category: the saved one where valid, the default otherwise."""
    saved = (mail_cfg or {}).get("rules") or {}
    out = {}
    for key, cat in CATEGORIES.items():
        r = dict(cat["default"])
        s = saved.get(key) or {}
        if s.get("mode") in MODES:
            r["mode"] = s["mode"]
        try:
            m = float(s.get("after_min", r["after_min"]))
            if 0 < m <= 24 * 60:
                r["after_min"] = m
        except (TypeError, ValueError):
            pass
        out[key] = r
    return out


def summary_cfg(mail_cfg):
    s = dict(SUMMARY_DEFAULT)
    saved = (mail_cfg or {}).get("summary") or {}
    if saved.get("frequency") in ("off", "daily", "weekly"):
        s["frequency"] = saved["frequency"]
    for k, lo, hi in (("weekday", 0, 6), ("hour", 0, 23)):
        try:
            v = int(saved.get(k, s[k]))
            if lo <= v <= hi:
                s[k] = v
        except (TypeError, ValueError):
            pass
    if "send_if_quiet" in saved:
        s["send_if_quiet"] = bool(saved["send_if_quiet"])
    return s


def should_alert(inc, now, rule_set):
    """(True, cause) if the open incident `inc` deserves its "it is down" mail at `now`, else (False, None)."""
    first_seen = {}
    for p in inc["phases"]:
        if p["cause"] in IGNORED_CAUSES:
            continue
        key = category_of(p["cause"])
        if key is not None and key not in first_seen:
            first_seen[key] = (p["start_ts"], p["cause"])
    for key, (since, cause) in first_seen.items():
        r = rule_set[key]
        if r["mode"] == "immediate" or (r["mode"] == "after" and now - since >= r["after_min"] * 60):
            return True, cause
    return False, None


# --- the periodic summary -----------------------------------------------------------------------

def last_slot(now, s):
    """The most recent scheduled moment at or before `now` (local time), or None when switched off."""
    if s["frequency"] == "off":
        return None
    lt = time.localtime(now)
    day_start = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, s["hour"], 0, 0, 0, 0, -1))
    if s["frequency"] == "daily":
        return day_start if day_start <= now else _shift_days(day_start, -1)
    back = (lt.tm_wday - s["weekday"]) % 7
    slot = _shift_days(day_start, -back)
    return slot if slot <= now else _shift_days(slot, -7)


def _shift_days(ts, days):
    lt = time.localtime(ts)
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday + days, lt.tm_hour, 0, 0, 0, 0, -1))


def period_for(slot, s):
    return (_shift_days(slot, -1 if s["frequency"] == "daily" else -7), slot)


def _fmt_dur(sec):
    sec = int(round(sec))
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m {sec % 60:02d}s"
    return f"{sec // 3600}h {sec % 3600 // 60:02d}m"


def _day(ts):
    return time.strftime("%d/%m %H:%M", time.localtime(ts))


def _uptime(con, since, until):
    ok, total = con.execute("SELECT sum(state='OK'), count(*) FROM samples WHERE ts>=? AND ts<?", (since, until)).fetchone()
    first = con.execute("SELECT min(ts) FROM samples").fetchone()[0]
    if first is not None and first > since:     # samples already past retention: the hourly summaries
        h_ok, h_total = con.execute("SELECT sum(ok_count), sum(samples_count) FROM hourly_stats WHERE hour_ts>=? AND hour_ts<?",
                                    (since, int(first // 3600) * 3600)).fetchone()
        ok, total = (ok or 0) + (h_ok or 0), (total or 0) + (h_total or 0)
    return (100.0 * ok / total) if total else None


def _period_stats(con, since, until):
    rows = con.execute("SELECT id, start_ts, end_ts, cause FROM incidents WHERE start_ts>=? AND start_ts<? ORDER BY start_ts",
                       (since, until)).fetchall()
    per = OrderedDict((k, {"count": 0, "seconds": 0.0}) for k in CATEGORIES)
    longest = None
    for iid, s, e, cause in rows:
        key = category_of(cause)
        if key is None:
            continue
        per[key]["count"] += 1
        per[key]["seconds"] += max(0.0, e - s)
        if key == "outage" or cause == "IP_RENEWAL_CYCLE":
            if longest is None or e - s > longest[2] - longest[1]:
                longest = (iid, s, e, cause)
    return per, longest


def summary_message(con, since, until, rule_set, dashboard_url="", title="Riepilogo", radio_node=None):
    """(subject, body, quiet): quiet when nothing happened in the period. `radio_node`: the chain's radio
    uplink, whose technology and signal get their own section; None on a wired line."""
    from dobermann import narrative
    per, longest = _period_stats(con, since, until)
    prev, _ = _period_stats(con, since - (until - since), since)
    ip_changes = con.execute("SELECT ts, ip_before, ip_after FROM ip_changes WHERE ts>=? AND ts<? ORDER BY ts",
                             (since, until)).fetchall()
    up = _uptime(con, since, until)
    events = sum(v["count"] for v in per.values())
    quiet = events == 0
    lines = [f"dobermann: {title.lower()} dal {_day(since)} al {_day(until)}", ""]
    lines.append(f"Connessione disponibile: {up:.2f}% del tempo" if up is not None else "Connessione disponibile: nessun dato")
    lines.append("")
    if quiet:
        lines.append("Nessun evento: tutto regolare.")
    else:
        lines.append("--- Eventi ---")
        for key, v in per.items():
            if key not in visible_categories(radio_node is not None):
                continue
            if v["count"] or prev[key]["count"]:
                p = prev[key]["count"]
                trend = f" (il periodo prima: {p})" if p != v["count"] else " (come il periodo prima)"
                total = f", in tutto {_fmt_dur(v['seconds'])}" if v["count"] else ""
                lines.append(f"  {CATEGORIES[key]['label']}: {v['count']}{total}{trend}")
        if longest:
            iid, s, e, cause = longest
            lines.append(f"  Il più lungo: #{iid} {diagnosis.CAUSE_LABELS.get(cause, cause)}, {_day(s)}, durato {_fmt_dur(e - s)}")
    if ip_changes:
        lines += ["", f"--- Cambi IP ({len(ip_changes)}) ---"]
        shown = ip_changes[-50:]
        if len(ip_changes) > len(shown):
            lines.append(f"  (i {len(ip_changes) - len(shown)} più vecchi sono nel pannello)")
        lines += [f"  {_day(ts)}  {a} → {b}" for ts, a, b in shown]
    if radio_node:
        radio = narrative.radio_summary(con, radio_node, since, until)
        if radio:
            lines += ["", "--- Rete mobile ---"] + [f"  {x}" for x in radio]
    mailed = [lower_first(CATEGORIES[k]["label"]) for k in visible_categories(radio_node is not None)
              if rule_set[k]["mode"] in ("immediate", "after")]
    lines += ["", "Avvisi immediati attivi per: " + (", ".join(mailed) if mailed else "nessuna categoria") + "."]
    if dashboard_url:
        lines.append(f"Pannello: {dashboard_url}")
    subject = f"[dobermann] {title}: " + ("tutto regolare" if quiet else f"{events} eventi") + \
              (f", disponibilità {up:.2f}%" if up is not None else "")
    return subject, "\n".join(lines), quiet
