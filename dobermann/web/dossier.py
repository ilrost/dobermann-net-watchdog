"""The month's record of outages, to hand to the operator with a complaint: a page made for printing
(or saving as PDF from the browser) and the same rows as CSV.

What it says and nothing more: when the connection was down, for how long, what dobermann saw, how it
measures. No opinion on whose fault it is beyond the cause it recorded; times are this machine's local
time. By default only the outages (the "Internet assente" category); with scope=all every event,
slow-downs and IP renewals included.
"""
import html
import time
from datetime import date, datetime

from dobermann import __version__, alerts, config, diagnosis
from dobermann.chain import Chain
from dobermann.i18n import N_, _
from dobermann.web import report

SCOPES = ("outages", "all")


def month_bounds(month):
    """(start, end) epoch of the calendar month 'YYYY-MM' (local time), or ValueError."""
    d = datetime.strptime(month, "%Y-%m").date()
    nxt = d.replace(year=d.year + 1, month=1) if d.month == 12 else d.replace(month=d.month + 1)
    return datetime(d.year, d.month, 1).timestamp(), datetime(nxt.year, nxt.month, 1).timestamp()


def months_available(con, today=None):
    """['YYYY-MM', ...] from the first month with data to this one, newest first."""
    row = con.execute("SELECT min(x) FROM (SELECT min(ts) AS x FROM samples UNION SELECT min(hour_ts) FROM hourly_stats "
                      "UNION SELECT min(start_ts) FROM incidents)").fetchone()
    today = today or date.today()
    first = datetime.fromtimestamp(row[0]).date() if row and row[0] is not None else today
    out, y, m = [], today.year, today.month
    while (y, m) >= (first.year, first.month) and len(out) < 240:
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    return out


def _wanted(cause, scope):
    if cause in alerts.IGNORED_CAUSES:
        return False
    return scope == "all" or alerts.category_of(cause) == "outage"


def build(month, scope="outages", min_s=0):
    since, until = month_bounds(month)
    cfg, chain_spec = config.load(), config.load_chain()
    interval = report._interval(cfg)
    con = report._connect()
    try:
        rows = con.execute("SELECT id, start_ts, end_ts, cause, signal_note FROM incidents "
                           "WHERE start_ts >= ? AND start_ts < ? ORDER BY start_ts", (since, until)).fetchall()
        up = alerts._uptime(con, since, min(until, time.time()))
        ip_changes = con.execute("SELECT count(*) FROM ip_changes WHERE ts >= ? AND ts < ?", (since, until)).fetchone()[0]
        months = months_available(con)
    finally:
        con.close()
    events = []
    for r in rows:
        dur = max(r["end_ts"] - r["start_ts"], interval)    # a single-sample blip still lasted one interval
        if _wanted(r["cause"], scope) and dur >= min_s:
            events.append({"id": r["id"], "start": r["start_ts"], "end": r["end_ts"], "duration_s": dur,
                           "cause": r["cause"], "label": diagnosis.cause_label(r["cause"]),
                           "category": alerts.category_of(r["cause"]), "note": r["signal_note"] or ""})
    outages = [e for e in events if e["category"] == "outage"]
    chain = Chain(chain_spec)
    return {
        "month": month, "scope": scope, "min_s": min_s, "since": since, "until": until, "months": months,
        "generated_at": time.time(), "version": __version__,
        "uptime_pct": up, "ip_changes": ip_changes,
        "events": events,
        "outages_count": len(outages),
        "outages_seconds": sum(e["duration_s"] for e in outages),
        "longest": max(outages, key=lambda e: e["duration_s"]) if outages else None,
        "link": chain.link_description() if chain.nodes else None,
        "devices": [n.label for n in chain.nodes],
        "anchors": [ip for _name, ip in cfg["anchors"]],
        "interval_s": interval,
        "confirm": cfg["thresholds"]["confirm_samples"],
    }


def fmt_duration(sec):
    sec = int(round(sec))
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m {sec % 60:02d}s"
    return f"{sec // 3600}h {sec % 3600 // 60:02d}m"


def _when(ts):
    return datetime.fromtimestamp(ts).strftime("%d/%m/%Y %H:%M:%S")


def csv_rows(d):
    cols = [_("numero"), _("inizio"), _("fine"), _("durata (secondi)"), _("causa"), _("descrizione"), _("nota")]
    return cols, [[e["id"], _when(e["start"]), _when(e["end"]), round(e["duration_s"]), e["cause"], e["label"], e["note"]]
                  for e in d["events"]]


MONTHS = (N_("gennaio"), N_("febbraio"), N_("marzo"), N_("aprile"), N_("maggio"), N_("giugno"), N_("luglio"),
          N_("agosto"), N_("settembre"), N_("ottobre"), N_("novembre"), N_("dicembre"))


def month_label(month):
    y, m = month.split("-")
    return f"{_(MONTHS[int(m) - 1])} {y}"


def render(d):
    """The page's body: controls (not printed), then the record."""
    esc = html.escape
    sel = lambda c: " selected" if c else ""  # noqa: E731
    opts = "".join(f'<option value="{m}"{sel(m == d["month"])}>{esc(month_label(m))}</option>' for m in d["months"])
    scopes = ((("outages"), _("solo le interruzioni")), ("all", _("tutti gli eventi (anche rallentamenti e rinnovi IP)")))
    scope_opts = "".join(f'<option value="{k}"{sel(k == d["scope"])}>{esc(v)}</option>' for k, v in scopes)
    up = d["uptime_pct"]
    longest = d["longest"]
    facts = [
        (_("Periodo"), f'{_when(d["since"])[:10]} – {_when(d["until"] - 1)[:10]}'),
        (_("Connessione disponibile"), f"{up:.2f}%" if up is not None else _("nessun dato")),
        (_("Interruzioni"), str(d["outages_count"])),
        (_("Tempo senza connessione"), fmt_duration(d["outages_seconds"])),
        (_("Interruzione più lunga"), f'{fmt_duration(longest["duration_s"])} ({_when(longest["start"])})' if longest else "—"),
        (_("Cambi di IP pubblico"), str(d["ip_changes"])),
    ]
    facts_html = "".join(f"<tr><th>{esc(k)}</th><td>{esc(v)}</td></tr>" for k, v in facts)
    rows = "".join(
        f'<tr><td>{e["id"]}</td><td>{esc(_when(e["start"]))}</td><td>{esc(_when(e["end"]))}</td>'
        f'<td class="num">{esc(fmt_duration(e["duration_s"]))}</td><td>{esc(e["label"])}'
        + (f'<div class="sub">{esc(e["note"])}</div>' if e["note"] else "") + "</td></tr>"
        for e in d["events"])
    if not rows:
        rows = f'<tr><td colspan="5" class="empty">{esc(_("Nessun evento in questo mese."))}</td></tr>'
    method = _("Misure fatte da dobermann {ver} dalla rete di casa, ogni {n} secondi: ping verso il router e verso "
               "{anchors}, risposta del DNS, stato letto dagli apparati della catena ({devices}). Un evento inizia "
               "alla prima misura anomala (per le cause che una misura sola non basta a provare, dopo {c} di fila) "
               "e finisce alla prima misura di nuovo regolare. Orari locali di questa macchina.",
               ver=d["version"], n=f'{d["interval_s"]:g}', anchors=", ".join(d["anchors"]) or "—",
               devices=", ".join(d["devices"]) or "—", c=d["confirm"])
    q = f'month={d["month"]}&scope={d["scope"]}&min={int(d["min_s"])}'
    return f"""
<form class="noprint controls" method="get" action="/dossier">
  <label>{esc(_("Mese"))} <select name="month">{opts}</select></label>
  <label>{esc(_("Cosa"))} <select name="scope">{scope_opts}</select></label>
  <label>{esc(_("Durata minima (secondi)"))} <input type="number" name="min" min="0" max="86400" value="{int(d["min_s"])}"></label>
  <button type="submit">{esc(_("Mostra"))}</button>
  <button type="button" onclick="window.print()">{esc(_("Stampa o salva PDF"))}</button>
  <a href="/dossier.csv?{q}">{esc(_("Scarica CSV"))}</a>
</form>
<h1>{esc(_("Registro dei disservizi"))} — {esc(month_label(d["month"]))}</h1>
<p class="meta">{esc(_("Linea: {v}", v=d["link"] or _("non determinata")))} · {esc(_("generato il {v}", v=_when(d["generated_at"])))}</p>
<table class="facts">{facts_html}</table>
<h2>{esc(_("Eventi registrati ({v})", v=len(d["events"])))}</h2>
<table class="events">
  <thead><tr><th>#</th><th>{esc(_("Inizio"))}</th><th>{esc(_("Fine"))}</th><th>{esc(_("Durata"))}</th><th>{esc(_("Causa rilevata"))}</th></tr></thead>
  <tbody>{rows}</tbody>
</table>
<h2>{esc(_("Come sono state fatte le misure"))}</h2>
<p class="method">{esc(method)}</p>"""
