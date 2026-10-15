"""The incident report in plain words (M5): WHERE it broke, WHY, the EVIDENCE, HOW LONG, WHAT TO DO.

Everything said here comes from what dobermann measured or what the chain's devices reported; a
statement whose data is missing is left out, never guessed. Radio wording (technology, signal, cell)
appears only when the chain's uplink is radio (Chain.radio_uplink): on a wired line it would be noise.
"""
import json
import time

from dobermann import alerts, diagnosis, store
from dobermann.i18n import N_, _, ngettext

OK, BROKEN, DEGRADED, UNKNOWN = "✔", "✘", "⚠", "?"

RAT_WORDS = {"5G_SA": "5G", "5G_NSA": "5G (NSA)", "LTE": "4G", "3G": "3G", "2G": "2G", "NONE": N_("senza servizio")}
RAT_RANK = {"5G_SA": 5, "5G_NSA": 5, "LTE": 4, "3G": 3, "2G": 2, "NONE": 0}

# Short headline per cause, for the subject and the first line
HEADLINES = {
    "GATEWAY_UNRESPONSIVE": N_("il router non rispondeva"),
    "CELLULAR_NO_SERVICE": N_("il modem ha perso la rete dell'operatore"),
    "LOCAL_LINK_DOWN": N_("il router ha perso il collegamento verso l'esterno"),
    "WAN_DISCONNECTED": N_("il router non riusciva a connettersi"),
    "IP_CHANGE_DROP": N_("interruzione dopo un cambio di indirizzo IP"),
    "UPSTREAM_OUTAGE": N_("guasto oltre il router"),
    "DNS_FAILURE_LOCAL": N_("il DNS locale non rispondeva"),
    "DNS_FAILURE_UPSTREAM": N_("nessun DNS rispondeva"),
    "PACKET_LOSS": N_("pacchetti persi verso internet"),
    "HIGH_LATENCY": N_("rete lenta"),
    "IP_RENEWAL_CYCLE": N_("rinnovo dell'indirizzo IP da parte dell'operatore"),
    "CELLULAR_3G_FALLBACK": N_("il modem è sceso al 3G"),
}

PHASE_WORDS = {
    "GATEWAY_UNRESPONSIVE": N_("router muto"), "CELLULAR_NO_SERVICE": N_("modem senza servizio"),
    "LOCAL_LINK_DOWN": N_("collegamento interrotto"), "WAN_DISCONNECTED": N_("connessione caduta"),
    "IP_CHANGE_DROP": N_("assestamento dopo il cambio IP"), "UPSTREAM_OUTAGE": N_("internet irraggiungibile"),
    "DNS_FAILURE_LOCAL": N_("DNS locale muto"), "DNS_FAILURE_UPSTREAM": N_("DNS muti"), "PACKET_LOSS": N_("perdite"),
    "HIGH_LATENCY": N_("latenza alta"), "CELLULAR_3G_FALLBACK": N_("3G"), "IP_RENEWAL_CYCLE": N_("rinnovo IP"),
}


def rat(t):
    return _(RAT_WORDS.get(t, t)) if t else "?"


def fmt_dur(sec):
    sec = int(round(max(0, sec)))
    if sec < 60:
        return ngettext("{n} secondo", "{n} secondi", sec)
    if sec < 3600:
        m, s = divmod(sec, 60)
        text = ngettext("{n} minuto", "{n} minuti", m)
        return _("{a} e {s} s", a=text, s=s) if s and m < 10 else text
    h, m = divmod(sec // 60, 60)
    text = ngettext("{n} ora", "{n} ore", h)
    return _("{a} e {b}", a=text, b=ngettext("{n} minuto", "{n} minuti", m)) if m else text


def _hm(ts):
    return time.strftime("%H:%M", time.localtime(ts))


def _day_hm(ts):
    return time.strftime("%d/%m %H:%M", time.localtime(ts))


# --- where -------------------------------------------------------------------------------------

def headline(cause, duration=None):
    if cause == "IP_RENEWAL_CYCLE" and duration is not None and duration > RENEWAL_MAX_S:
        return _("guasto dell'operatore, finito con un nuovo indirizzo IP")
    return _(HEADLINES[cause]) if cause in HEADLINES else diagnosis.cause_label(cause)


def where(cause, chain, gw_answered=True, uplink_answered=None, duration=None):
    """(chain line with marks, one sentence). Positions: dobermann, every node, the operator, internet."""
    nodes = chain.nodes
    names = ["dobermann"] + [n.label for n in nodes] + [_("operatore"), "internet"]
    marks = [OK] + [OK] * len(nodes) + [UNKNOWN, UNKNOWN]
    op, net = len(names) - 2, len(names) - 1
    radio = chain.radio_uplink()
    wan_i = 1                                    # the router reporting the WAN is the first device
    sentence = ""
    if cause == "GATEWAY_UNRESPONSIVE" and nodes:
        marks[1] = BROKEN
        marks[2:] = [UNKNOWN] * (len(marks) - 2)
        sentence = _("Il guasto era in casa, fra questo apparecchio e {v}.", v=nodes[0].label)
    elif cause in ("LOCAL_LINK_DOWN", "WAN_DISCONNECTED") and nodes:
        nxt = wan_i + 1
        marks[nxt] = BROKEN
        marks[nxt + 1:] = [UNKNOWN] * (len(marks) - nxt - 1)
        what = names[nxt] if nxt < op else _("la linea dell'operatore")
        sentence = _("Il guasto era fra {a} e {b}.", a=nodes[0].label, b=what)
    elif cause == "CELLULAR_NO_SERVICE":
        if radio is not None and uplink_answered is False:
            marks[1 + nodes.index(radio)] = UNKNOWN
        marks[op], marks[net] = BROKEN, UNKNOWN
        sentence = (_("Il guasto era fra {v} e l'operatore: in casa tutto funzionava.", v=radio.label) if radio
                    else _("Il guasto era fra il modem e l'operatore."))
    elif cause in ("UPSTREAM_OUTAGE", "IP_CHANGE_DROP", "IP_RENEWAL_CYCLE"):
        marks[op], marks[net] = BROKEN, UNKNOWN
        long_renewal = cause == "IP_RENEWAL_CYCLE" and duration is not None and duration > RENEWAL_MAX_S
        sentence = (_("In casa tutto funzionava: l'interruzione era dell'operatore o più in là.")
                    if cause == "UPSTREAM_OUTAGE" or long_renewal
                    else _("In casa tutto funzionava: è l'operatore che ha rinnovato la connessione."))
    elif cause in ("PACKET_LOSS", "HIGH_LATENCY", "CELLULAR_3G_FALLBACK"):
        marks[op], marks[net] = DEGRADED, OK
        sentence = _("La connessione c'era ma era degradata oltre il router.")
    elif cause in ("DNS_FAILURE_LOCAL", "DNS_FAILURE_UPSTREAM"):
        marks[op], marks[net] = OK, OK
        sentence = (_("Internet funzionava: era il server DNS locale a non rispondere.") if cause == "DNS_FAILURE_LOCAL"
                    else _("La rete c'era, ma nessun server DNS rispondeva."))
    if not gw_answered and cause != "GATEWAY_UNRESPONSIVE" and nodes:
        marks[1] = DEGRADED
    line = " → ".join(f"{n} {m}" for n, m in zip(names, marks))
    return line, sentence


# --- why and what to do ------------------------------------------------------------------------

DOWNGRADE_MIN_S = 60
MAX_SEQUENCE = 6          # longer phase or technology sequences are summarised per kind
RENEWAL_MAX_S = diagnosis.RENEWAL_MAX_S   # kept for incidents classified before the rule existed


def why(cause, chain, inc_phases, duration=None):
    radio = chain.radio_uplink()
    up = chain.uplink()
    gw_name = chain.gateway.label if chain.gateway else _("Il router")
    modem = radio.label if radio else _("Il modem")
    texts = {
        "GATEWAY_UNRESPONSIVE": lambda: _("{v} non rispondeva ai ping: era spento, bloccato, oppure questo apparecchio "
                                          "aveva perso la rete di casa.", v=gw_name),
        "CELLULAR_NO_SERVICE": lambda: _("{v} non aveva una cella dell'operatore utilizzabile, quindi nessun collegamento "
                                         "a internet.", v=modem),
        "LOCAL_LINK_DOWN": lambda: _("Il router segnalava il collegamento verso l'esterno assente: cavo, alimentazione o "
                                     "apparato a monte."),
        "WAN_DISCONNECTED": lambda: _("Il collegamento fisico c'era, ma il router non otteneva la connessione "
                                      "dall'operatore."),
        "IP_CHANGE_DROP": lambda: _("L'operatore ha cambiato l'indirizzo IP pubblico e la connessione si è interrotta "
                                    "mentre si assestava."),
        "UPSTREAM_OUTAGE": lambda: _("Router e apparati rispondevano e la linea risultava su, ma internet era "
                                     "irraggiungibile."),
        "DNS_FAILURE_LOCAL": lambda: _("Il server DNS di casa non rispondeva: i siti non si aprivano per nome, anche se "
                                       "internet c'era."),
        "DNS_FAILURE_UPSTREAM": lambda: _("Nessun server DNS rispondeva, né quello di casa né quello esterno."),
        "PACKET_LOSS": lambda: _("Una parte dei pacchetti verso internet andava persa."),
        "HIGH_LATENCY": lambda: _("Le risposte da internet arrivavano molto in ritardo."),
        "IP_RENEWAL_CYCLE": lambda: _("L'operatore ha rinnovato l'indirizzo IP pubblico: una breve riconnessione di "
                                      "routine."),
        "CELLULAR_3G_FALLBACK": lambda: _("{v} è passato al 3G, più lento, per mancanza di copertura migliore.", v=modem),
    }
    text = texts[cause]() if cause in texts else diagnosis.cause_label(cause)
    if cause == "IP_RENEWAL_CYCLE" and duration is not None and duration > RENEWAL_MAX_S:
        text = _("L'indirizzo IP pubblico è cambiato, ma la connessione è rimasta disturbata per {v}: molto più di un "
                 "rinnovo normale (pochi secondi). Probabile guasto dell'operatore, terminato con un nuovo indirizzo.",
                 v=fmt_dur(duration))
    if 1 < len(inc_phases) <= MAX_SEQUENCE:
        seq = " → ".join(f"{phase_word(p['cause'])} ({fmt_dur(max(p['end_ts'] - p['start_ts'], 5))})"
                         for p in inc_phases)
        text += "\n" + _("Sequenza: {v}.", v=seq)
    elif len(inc_phases) > MAX_SEQUENCE:
        totals = {}
        for p in inc_phases:
            n, t = totals.get(p["cause"], (0, 0))
            totals[p["cause"]] = (n + 1, t + max(p["end_ts"] - p["start_ts"], 5))
        parts = [ngettext("{what} {n} volta, in tutto {d}", "{what} {n} volte, in tutto {d}", n, what=phase_word(c),
                          d=fmt_dur(t))
                 for c, (n, t) in sorted(totals.items(), key=lambda kv: -kv[1][1])]
        text += "\n" + _("{n} fasi: {v}.", n=len(inc_phases), v="; ".join(parts))
    if up is not None and not radio and cause in ("PACKET_LOSS", "HIGH_LATENCY"):
        text += " " + _("Su una linea cablata di solito è congestione dell'operatore o un grosso trasferimento in casa.")
    return text


def phase_word(cause):
    return _(PHASE_WORDS[cause]) if cause in PHASE_WORDS else cause


def advice(cause, chain, dns_local=None, duration=None):
    radio = chain.radio_uplink()
    gw = chain.gateway.label if chain.gateway else _("il router")
    if cause == "GATEWAY_UNRESPONSIVE":
        return _("Il problema è in casa. Controlla che {v} sia acceso e il collegamento (cavo o Wi-Fi) fra questo "
                 "apparecchio e il router. Se si ripete, verifica l'alimentazione del router.", v=gw)
    if cause == "LOCAL_LINK_DOWN":
        nxt = chain.upstream_of(chain.gateway) if chain.gateway else None
        if nxt is not None:
            return _("Controlla il cavo fra {a} e {b} e che {b} sia alimentato.", a=gw, b=nxt.label)
        return _("Controlla il cavo della linea e le spie del router; se il guasto continua, chiama l'operatore.")
    if cause == "CELLULAR_NO_SERVICE":
        return _("Niente da fare in casa: la rete mobile non era disponibile. Se si ripete, segnala all'operatore "
                 "giorno e ora; se capita spesso, prova a spostare il modem più in alto o vicino a una finestra.")
    if cause in ("WAN_DISCONNECTED", "UPSTREAM_OUTAGE"):
        return _("In casa non c'è niente da fare. Se dura o si ripete, segnala all'operatore giorno e ora.")
    if cause in ("IP_RENEWAL_CYCLE", "IP_CHANGE_DROP"):
        if duration is not None and duration > RENEWAL_MAX_S:
            return _("In casa non c'è niente da fare. Se si ripete, segnala all'operatore giorno e ora.")
        return _("Niente da fare: è il rinnovo periodico dell'indirizzo da parte dell'operatore.")
    if cause == "DNS_FAILURE_LOCAL":
        if dns_local:
            return _("Controlla il server DNS di casa ({v}): è acceso, risponde?", v=dns_local)
        return _("Controlla il server DNS di casa: è acceso, risponde?")
    if cause == "DNS_FAILURE_UPSTREAM":
        return _("Di solito è un problema dell'operatore. Se si ripete, imposta nel router un DNS pubblico diverso.")
    if cause in ("PACKET_LOSS", "HIGH_LATENCY"):
        if radio:
            return _("Su una linea radio dipende spesso da segnale o celle affollate: se è frequente, prova a "
                     "riposizionare il modem.")
        return _("Se capita spesso, controlla se in quel momento in casa c'era un grosso trasferimento; altrimenti "
                 "segnala all'operatore.")
    if cause == "CELLULAR_3G_FALLBACK":
        return _("Niente da fare se capita di rado. Se è frequente, prova a riposizionare il modem per una copertura "
                 "4G/5G migliore.")
    return ""


# --- evidence ----------------------------------------------------------------------------------

def _tech(f):
    if not f:
        return None
    if f.get("cell.service") in diagnosis.NO_SERVICE:
        return "NONE"
    return f.get("cell.rat")


def radio_story(before, during, after):
    """Lines about the radio uplink across an incident: technology sequence, signal, cell."""
    lines = []
    seq = []                                   # [(tech, first ts, last ts)]
    for ts, f in during:
        t = _tech(f)
        if t is None:
            continue
        if seq and seq[-1][0] == t:
            seq[-1][2] = ts
        else:
            seq.append([t, ts, ts])
    tb, ta = _tech(before), _tech(after)
    seen = {t for t in [tb, ta] + [x[0] for x in seq] if t}
    if len(seen) == 1:
        t = seen.pop()
        lines.append(_("rete mobile: sempre {v}", v=rat(t)))
    elif seen:
        parts = [_("prima {v}", v=rat(tb))] if tb else []
        if 0 < len(seq) <= MAX_SEQUENCE:
            parts.append(_("durante {v}", v=" → ".join(
                rat(t) + (f" {fmt_dur(b - a)}" if b - a >= 5 else "") for t, a, b in seq)))
        elif seq:
            totals = {}
            for t, a, b in seq:
                n, d = totals.get(t, (0, 0))
                totals[t] = (n + 1, d + (b - a))
            parts.append(_("durante {v}", v=", ".join(
                f"{rat(t)} " + (fmt_dur(d) if d >= 60 else _("per brevi tratti"))
                + " (" + ngettext("{n} volta", "{n} volte", n) + ")"
                for t, (n, d) in sorted(totals.items(), key=lambda kv: -kv[1][1]))))
        if ta:
            parts.append(_("dopo {v}", v=rat(ta)))
        lines.append(_("rete mobile: {v}", v=", ".join(parts)))
    for key, gen in (("cell.lte_rsrp_dbm", "4G"), ("cell.nr_rsrp_dbm", "5G")):
        b, a = (before or {}).get(key), (after or {}).get(key)
        if b is not None and a is not None:
            lines.append(_("segnale {g}: {b} dBm prima, {a} dBm dopo", g=gen, b=f"{b:.0f}", a=f"{a:.0f}")
                         + (" " + _("(calo marcato)") if a <= b - diagnosis.RSRP_DROP_DB else ""))
    cb, ca = (before or {}).get("cell.cell_id"), (after or {}).get("cell.cell_id")
    if cb and ca:
        lines.append(_("cella: cambiata") if cb != ca else _("cella: la stessa prima e dopo"))
    return lines


def evidence(con, inc, chain, dns):
    """Facts measured during the incident, as short sentences."""
    s, e = inc["start_ts"], inc["end_ts"]
    rows = con.execute("SELECT gw_loss, internet_loss, internet_rtt, wan_status, wan_link_up, dns_local_ok, dns_ext_ok "
                       "FROM samples WHERE ts >= ? AND ts <= ? ORDER BY ts", (s, e)).fetchall()
    out = []
    gw = chain.gateway
    if gw is not None and rows:
        gl = [r[0] for r in rows if r[0] is not None]
        if gl:
            out.append(_("{v} rispondeva sempre (perdita massima {p}%)", v=gw.label, p=f"{max(gl):.0f}") if max(gl) < 100
                       else _("{v} non rispondeva ({n} campioni su {t})", v=gw.label,
                              n=sum(1 for x in gl if x >= 100), t=len(gl)))
    if rows:
        down = [r for r in rows if r[4] == 0]
        disc = [r for r in rows if r[3] == "disconnected"]
        who = gw.label if gw else _("il router")
        if down:
            out.append(_("{v} segnalava il collegamento verso l'esterno assente ({n} campioni)", v=who, n=len(down)))
        elif disc:
            out.append(_("{v} segnalava la connessione disconnessa ({n} campioni)", v=who, n=len(disc)))
        il = [r[1] for r in rows if r[1] is not None]
        if il:
            if max(il) >= 100:
                out.append(_("internet irraggiungibile: nessuna risposta dai server di riferimento"))
            elif max(il) > 0:
                out.append(_("perdita verso internet fino al {v}%", v=f"{max(il):.0f}"))
        rt = [r[2] for r in rows if r[2] is not None]
        if rt and inc.get("max_rtt"):
            out.append(_("latenza fino a {v} ms", v=f"{inc['max_rtt']:.0f}"))
        if dns.get("local") and any(r[5] == 0 for r in rows):
            out.append(_("DNS locale ({v}) senza risposta in {n} campioni", v=dns["local"],
                         n=sum(1 for r in rows if r[5] == 0)))
    if inc.get("ip_before") and inc.get("ip_after") and inc["ip_before"] != inc["ip_after"]:
        out.append(_("indirizzo IP: {a} → {b}", a=inc["ip_before"], b=inc["ip_after"]))
    radio = chain.radio_uplink()
    if radio is not None:
        before = store.facts_before(con, radio.id, s)
        after = store.facts_after(con, radio.id, e, diagnosis.SIGNAL_LOOKAHEAD_S)
        during = [(ts, json.loads(f)) for ts, f in con.execute(
            "SELECT ts, facts_json FROM node_facts WHERE node=? AND ts>=? AND ts<=? ORDER BY ts", (radio.id, s, e))]
        out += radio_story(before, during, after)
    return out


# --- how long, how often -----------------------------------------------------------------------

def recurrence(con, inc_id, cause, start_ts):
    key = alerts.category_of(cause)
    causes = alerts.CATEGORIES[key]["causes"] if key else (cause,)
    marks = ",".join("?" * len(causes))
    rows = con.execute(f"SELECT start_ts FROM incidents WHERE cause IN ({marks}) AND start_ts >= ? AND start_ts < ? "
                       f"AND id != ?", (*causes, start_ts - 30 * 86400, start_ts, inc_id or -1)).fetchall()
    n30 = len(rows)
    n7 = sum(1 for (t,) in rows if t >= start_ts - 7 * 86400)
    label = alerts.lower_first(alerts.category_label(key)) if key else _("questo tipo")
    if n30 == 0:
        return _("Primo evento di tipo \"{v}\" negli ultimi 30 giorni.", v=label)
    text = _("Eventi dello stesso tipo ({v}) prima di questo: {n7} negli ultimi 7 giorni, {n30} negli ultimi 30.",
             v=label, n7=n7, n30=n30)
    hours = [time.localtime(t).tm_hour for (t,) in rows] + [time.localtime(start_ts).tm_hour]
    night = sum(1 for h in hours if h < 6)
    if len(hours) >= 3 and night * 3 >= len(hours) * 2:
        text += " " + _("Capitano soprattutto di notte (fra mezzanotte e le 6).")
    return text


# --- the two mails -----------------------------------------------------------------------------

def _footer(dashboard_url, inc_id=None):
    if not dashboard_url:
        return []
    if inc_id:
        return ["", _("Pannello: {url} (incidente #{n})", url=dashboard_url, n=inc_id)]
    return ["", _("Pannello: {url}", url=dashboard_url)]


def close_report(con, inc_id, cause, inc, ok_since, note, chain, dns, dashboard_url=""):
    """(subject, body) of the mail sent when an alerted incident is over."""
    dur = inc["end_ts"] - inc["start_ts"]
    cat = alerts.category_of(cause)
    head = headline(cause, dur)
    title = alerts.category_label(cat) if cat else _("Incidente")
    gw_answered = (inc.get("max_gw_loss") or 0) < 100
    line, sentence = where(cause, chain, gw_answered, duration=dur)
    lines = [_("{title} per {d}: {head}.", title=title, d=fmt_dur(dur), head=head), "",
             _("DOVE"), f"  {line}", f"  {sentence}" if sentence else None, "",
             _("PERCHÉ"), f"  {why(cause, chain, inc['phases'], dur)}".replace("\n", "\n  "), ""]
    ev = evidence(con, inc, chain, dns)
    if ev:
        lines += [_("PROVE")] + [f"  - {x}" for x in ev] + [""]
    lines += [_("QUANTO"), f"  {_day_hm(inc['start_ts'])} → {_hm(ok_since)}, {fmt_dur(dur)}.",
              f"  {recurrence(con, inc_id, cause, inc['start_ts'])}", ""]
    tip = advice(cause, chain, dns.get("local"), dur)
    if tip:
        lines += [_("COSA FARE"), f"  {tip}"]
    lines += _footer(dashboard_url, inc_id)
    subject = "[dobermann] " + _("Risolto: {title} per {d}, {head}", title=alerts.lower_first(title), d=fmt_dur(dur),
                                 head=head)
    return subject, "\n".join(x for x in lines if x is not None)


def open_report(cause, inc, obs, chain, results, dns, dashboard_url="", since=None):
    """(subject, body) of the mail sent while the incident is still going on."""
    since = since if since is not None else inc["start_ts"]
    cat = alerts.category_of(cause)
    title = alerts.category_label(cat) if cat else _("Problema di connessione")
    head = headline(cause)
    radio = chain.radio_uplink()
    uplink_answered = None
    if radio is not None:
        r = results.get(radio.id)
        uplink_answered = bool(r and r.facts)
    line, sentence = where(cause, chain, obs.gw_loss < 100, uplink_answered)
    lasted = obs.ts - since
    when = (_("Iniziato alle {a}, ancora in corso alle {b} ({d}).", a=_hm(since), b=_hm(obs.ts), d=fmt_dur(lasted))
            if lasted >= 60 else _("Rilevato alle {v}.", v=_hm(obs.ts)))
    lines = [f"{title}: {head}.", when, "", _("DOVE"), f"  {line}", f"  {sentence}" if sentence else None, "",
             _("PERCHÉ"), f"  {why(cause, chain, [])}", "", _("PROVE (ultimo campione)")]
    gw = chain.gateway
    if gw is not None:
        lines.append(f"  - {gw.label}: " + (_("risponde") if obs.gw_loss < 100 else _("non risponde"))
                     + (f" ({obs.gw_rtt:.0f} ms)" if obs.gw_rtt else ""))
    if obs.wan.get("wan.link_up") is False:
        lines.append("  - " + _("collegamento verso l'esterno assente"))
    elif obs.wan.get("wan.status") and obs.wan["wan.status"] != "connected":
        lines.append("  - " + _("connessione del router: {v}", v=obs.wan["wan.status"]))
    if obs.internet_loss >= 100:
        net = _("nessuna risposta dai server di riferimento")
    else:
        net = _("perdita {v}%", v=f"{obs.internet_loss:.0f}")
        if obs.internet_rtt:
            net += ", " + _("latenza {v} ms", v=f"{obs.internet_rtt:.0f}")
    lines.append(f"  - internet: {net}")
    for label, server, ok in ((_("DNS locale"), dns.get("local"), obs.dns_local_ok),
                              (_("DNS esterno"), dns.get("external"), obs.dns_ext_ok)):
        if server and ok is False:
            lines.append("  - " + _("{v} ({s}) non risponde", v=label, s=server))
    if radio is not None:
        f = (results.get(radio.id).facts if results.get(radio.id) else None) or {}
        if f:
            t = _tech(f)
            sig = ", ".join(f"{lab} {f[k]:.0f} dBm" for k, lab in (("cell.lte_rsrp_dbm", "4G"), ("cell.nr_rsrp_dbm", "5G"))
                            if f.get(k) is not None)
            lines.append(f"  - {radio.label}: {rat(t)}" + (", " + _("segnale {v}", v=sig) if sig else ""))
        else:
            lines.append(f"  - {radio.label}: " + _("non risponde"))
    tip = advice(cause, chain, dns.get("local"))
    if tip:
        lines += ["", _("COSA FARE"), f"  {tip}"]
    lines.append("")
    lines.append(_("Quando la connessione torna arriva il referto completo. Se internet è giù, questa mail parte al "
                   "ritorno della linea."))
    lines += _footer(dashboard_url)
    subject = f"[dobermann] {title}: {head}"
    return subject, "\n".join(x for x in lines if x is not None)


# --- radio in the summary ----------------------------------------------------------------------

def radio_summary(con, node_id, since, until):
    """Lines for the periodic summary: time per technology, downgrades, signal, cells."""
    rows = con.execute("SELECT ts, facts_json FROM node_facts WHERE node=? AND ts>=? AND ts<? ORDER BY ts",
                       (node_id, since, until)).fetchall()
    if len(rows) < 2:
        return []
    share, prev = {}, None
    segments = []                              # [tech, first ts, last ts], consecutive readings merged
    lte, nr, cells = [], [], set()
    for ts, fj in rows:
        f = json.loads(fj)
        t = _tech(f)
        if prev is not None and prev[1] is not None:
            share[prev[1]] = share.get(prev[1], 0) + min(ts - prev[0], 300)   # a gap in readings is not time on that tech
        prev = (ts, t)
        if t is not None:
            if segments and segments[-1][0] == t:
                segments[-1][2] = ts
            else:
                if segments:
                    segments[-1][2] = ts
                segments.append([t, ts, ts])
        if f.get("cell.lte_rsrp_dbm") is not None:
            lte.append(f["cell.lte_rsrp_dbm"])
        if f.get("cell.nr_rsrp_dbm") is not None:
            nr.append(f["cell.nr_rsrp_dbm"])
        if f.get("cell.cell_id"):
            cells.add(f["cell.cell_id"])
    tot = sum(share.values())
    if not tot:
        return []
    merged = {}
    for t, v in share.items():
        w = rat(t)
        merged[w] = merged.get(w, 0) + v
    out = [_("Tempo per tecnologia: {v}", v=", ".join(f"{w} {100 * v / tot:.1f}%" for w, v in
                                                       sorted(merged.items(), key=lambda kv: -kv[1])))]
    # a drop that lasts: 5G NSA modems report LTE for an instant all the time (dozens of times a
    # week, a fraction of a percent of the time), which says nothing about the line
    downs = sum(1 for a, b in zip(segments, segments[1:])
                if RAT_RANK.get(b[0], 0) < RAT_RANK.get(a[0], 0) and b[2] - b[1] >= DOWNGRADE_MIN_S)
    out.append(_("Cadute su una tecnologia peggiore durate almeno un minuto: {v}", v=downs))
    for gen, vals in (("4G", lte), ("5G", nr)):
        if vals:
            vals = sorted(vals)
            out.append(_("Segnale {g}: tipico {a} dBm, peggiore {b} dBm", g=gen, a=f"{vals[len(vals) // 2]:.0f}",
                         b=f"{vals[0]:.0f}"))
    if cells:
        out.append(_("Celle diverse usate: {v}", v=len(cells)))
    return out
