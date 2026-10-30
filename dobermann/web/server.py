"""The web panel.

    python3 -m dobermann web

  /watchdash        today's incidents, live (auto-refresh)
  /report           history: ?period=day|week|month|year, ?date=YYYY-MM-DD for a day
  /setup            settings: notifications, chain of devices, measurements, admin password
  /export           CSV of raw samples or hourly roll-ups (?table=samples|hourly&from=&to=)
  /speed            the speed test, only when asked (POST size=MB); the latest results
  /dossier          the month's outages for the operator, to print (?month=YYYY-MM&scope=outages|all&min=s);
                    /dossier.csv the same rows
  /api/samples      raw samples for the charts' deep zoom (?from=&to= epoch, at most 6 h)
  /api/interval     GET / POST {"interval": N} the sampling interval (1-60 s)
  /login, /logout   one admin password; the first visit asks to choose it

Everything but /login and /logo.svg needs a session. The settings page writes
config.json / chain.json / secrets.json.
Passwords are never sent back to the browser: an empty password field means "leave it as it is".
"""
import csv
import html
import http.cookies
import io
import ipaddress
import json
import os
import re
import sys
import threading
import time
import urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from dobermann import __version__, adapters, alerts, config, discover, i18n, mailqueue, notify, speedtest, store, updates
from dobermann.chain import Chain
from dobermann.i18n import _
from dobermann.web import address, auth, dossier, listen, report

TEMPLATES = os.path.join(os.path.dirname(__file__), "templates")
TOOLS = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "tools")
REPORT_PERIODS = ("day", "week", "month", "year")
PAGE = """<!doctype html>
<html lang="it">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
</head>
<body>
{fragment}
</body>
</html>"""

_config_lock = threading.Lock()


def _tpl(name):
    """A template in the current language: [[text]] translated, the page's language declared. Done
    before any value is filled in, so nothing a user typed is ever taken for a text to translate."""
    with open(os.path.join(TEMPLATES, name), encoding="utf-8") as f:
        text = f.read()
    text = i18n.translate_template(text, html.escape)
    return text.replace('<html lang="it">', f'<html lang="{i18n.language()}">')


def toolbar(token=None, here="/watchdash"):
    """The icons at the top right: language, settings, user, log out."""
    items = []
    for code, name in i18n.available().items():
        cls = ' class="current"' if code == i18n.language() else ""
        items.append(f'<form method="post" action="/language"><input type="hidden" name="lang" value="{code}">'
                     f'<input type="hidden" name="next" value="{esc(here)}"><button type="submit"{cls} lang="{code}">'
                     f'{esc(name)}</button></form>')
    st = updates.load_status() or {}
    upd = ""
    if st.get("newer"):
        upd = (f'<a href="/setup#updates" class="upd" title="{esc(_("Nuova versione disponibile: {v}", v=st["latest"]))}">'
               f'{esc(st["latest"])}</a>')
    warn = ""
    if mail_trouble(mail_status()):
        warn = (f'<a href="/setup#mail" class="upd warn" title="{esc(_("Le mail non partono: dettagli nelle impostazioni"))}">'
                f'{esc(_("mail"))} ✘</a>')
    started = auth.session_started(token)
    since = _("collegato dal {v}", v=datetime.fromtimestamp(started).strftime("%d/%m/%Y %H:%M")) if started else ""
    return _fill(_tpl("toolbar.html"), {"__LANG_ITEMS__": "".join(items), "__SINCE__": esc(since), "__UPDATE__": warn + upd})


def mail_status():
    """The mail queue's state (mailqueue.status), or None when the database cannot be read."""
    try:
        con = report._connect()
        try:
            return mailqueue.status(con)
        finally:
            con.close()
    except Exception:
        return None


def mail_trouble(st, now=None):
    """True when mails are not going out: one waiting for more than 10 minutes, or a failure after the
    last delivery (in the last week)."""
    if not st:
        return False
    now = time.time() if now is None else now
    if st["waiting"] and st["oldest"] is not None and now - st["oldest"] > 600:
        return True
    err = st["last_error"]
    return bool(err and now - err[0] < 7 * 86400 and (st["last_ok"] is None or err[0] > st["last_ok"]))


def _mbps(bps):
    return "—" if bps is None else f"{bps / 1e6:.1f} Mbit/s"


def render_speed(token=None, banner=None, error=False):
    try:
        con = report._connect()
        try:
            past = speedtest.history(con)
        finally:
            con.close()
    except Exception:
        past = []
    rows = []
    for t in past:
        when = datetime.fromtimestamp(t["ts"]).strftime("%d/%m/%Y %H:%M")
        lat = "—" if t["idle_ms"] is None else f'{t["idle_ms"]:.0f} ms'
        if t["loaded_ms"] is not None:
            lat += f' → {t["loaded_ms"]:.0f} ms'
        res = (f'<td class="num">{esc(_mbps(t["down_bps"]))}</td><td class="num">{esc(_mbps(t["up_bps"]))}</td>'
               f'<td class="num">{esc(lat)}</td><td class="num">{t["bytes"] / 1e6:.0f} MB</td>')
        if t["error"]:
            res += f'<td>{esc(t["error"])}</td>'
        else:
            res += f'<td>{esc(t["server"])}</td>'
        rows.append(f"<tr><td>{esc(when)}</td>{res}</tr>")
    table = (f"""<table class="events"><thead><tr><th>{esc(_("Quando"))}</th><th>{esc(_("Download"))}</th>
<th>{esc(_("Upload"))}</th><th>{esc(_("Latenza (a riposo → sotto carico)"))}</th><th>{esc(_("Dati usati"))}</th>
<th>{esc(_("Server"))}</th></tr></thead><tbody>{"".join(rows)}</tbody></table>""" if rows else
             f'<p class="hint">{esc(_("Nessun test ancora."))}</p>')
    sizes = "".join(f'<option value="{mb}"{" selected" if mb == 25 else ""}>{esc(_("{d} MB in download, {u} MB in upload", d=mb, u=mb // 5))}</option>'
                    for mb in speedtest.SIZES_MB)
    banner_html = f'<div class="banner{" error" if error else ""}">{esc(banner)}</div>' if banner else ""
    down_url, _up = speedtest.urls(config.load())
    body = f"""<h1>{esc(_("Test di velocità"))}</h1>
<p class="hint">{esc(_("Parte solo quando lo chiedi tu. Su una linea mobile consuma i dati indicati e, per una ventina di "
                       "secondi, occupa la linea per tutta la casa. Server: {v} (vede l'IP pubblico di questa linea, come "
                       "qualsiasi sito). Durante il test dobermann non registra come eventi la lentezza che il test stesso "
                       "provoca.", v=urllib.parse.urlparse(down_url).hostname))}</p>
{banner_html}
<form class="controls" method="post" action="/speed" onsubmit="this.querySelector('button').disabled=true;this.querySelector('button').textContent='{esc(_("Test in corso…"))}'">
  <label>{esc(_("Quanti dati usare"))} <select name="size">{sizes}</select></label>
  <button type="submit">{esc(_("Avvia il test"))}</button>
</form>
<h2>{esc(_("Test fatti"))}</h2>
<div class="events-wrap">{table}</div>"""
    return _fill(_tpl("speed.html"), {"__TOOLBAR__": toolbar(token, "/speed"), "__BODY__": body})


def render_data_page(name, data, token=None, here="/watchdash"):
    # "</" would let a value close the <script> the data sits in
    page = _tpl(name).replace("<!--__TOOLBAR__-->", toolbar(token, here))
    return PAGE.replace('<html lang="it">', f'<html lang="{i18n.language()}">').format(
        fragment=page.replace("/*__DATA__*/", json.dumps(data).replace("</", "<\\/")))


def _fill(tpl, mapping):
    """One-pass substitution: a value containing another placeholder's name is never expanded again."""
    return re.sub("|".join(re.escape(k) for k in mapping), lambda m: mapping[m.group(0)], tpl)


def esc(v):
    return "" if v is None else html.escape(str(v))


def _valid_ip(s):
    try:
        ipaddress.ip_address(s)
        return True
    except ValueError:
        return False


# --- settings page ------------------------------------------------------------------------------

def _when(ts):
    return datetime.fromtimestamp(ts).strftime("%d/%m/%Y %H:%M")


def _mail_state_html(st):
    if not st:
        return ""
    lines = []
    if st["waiting"]:
        lines.append(_("{n} mail in attesa di partire (la più vecchia del {v}): dobermann riprova da solo, e subito "
                       "quando la connessione torna.", n=st["waiting"], v=_when(st["oldest"])))
    if st["last_ok"]:
        lines.append(_("Ultima mail consegnata: {v}.", v=_when(st["last_ok"])))
    err = st["last_error"]
    if err and (st["last_ok"] is None or err[0] > st["last_ok"]):
        lines.append(_("Ultimo errore ({when}): {why}", when=_when(err[0]), why=err[1]))
    if st["dropped"]:
        lines.append(_("Mail scartate perché non consegnate per 7 giorni: {v}.", v=st["dropped"]))
    if not lines:
        return ""
    cls = "banner error" if mail_trouble(st) else "note"
    return f'<div class="{cls}">' + "<br>".join(esc(x) for x in lines) + "</div>"


def _mail_section(cfg, secrets):
    m = cfg["mail"]
    has_pw = bool((secrets.get("mail") or {}).get("password"))
    smtp = m.get("smtp", {})

    def sel(cond):
        return "selected" if cond else ""
    return f"""
<section id="mail">
  <h2>{esc(_("Notifiche email"))}</h2>
  {_mail_state_html(mail_status())}
  <p class="hint">{_("dobermann manda una mail quando la connessione si interrompe e quando torna, tramite il server "
                     "SMTP del tuo fornitore di posta (i dati sono nelle sue istruzioni per i programmi di posta; per "
                     "Gmail serve una password per app). Una mail scritta mentre internet è giù resta in coda e parte "
                     "appena la connessione torna.")}</p>
  <form method="post" action="/setup">
    <label class="check"><input type="checkbox" name="enabled" value="1" {"checked" if m.get("enabled") else ""}> {esc(_("Invia le notifiche"))}</label>
    <label for="to">{esc(_("Destinatario"))}</label>
    <input type="email" id="to" name="to" value="{esc(m.get('to'))}">
    <label for="from">{esc(_("Mittente"))}</label>
    <input type="text" id="from" name="from" value="{esc(m.get('from'))}" placeholder="{esc(_("vuoto: uguale all'utente SMTP"))}">
    <div class="row">
      <div><label for="smtp_host">{esc(_("SMTP: server"))}</label><input type="text" id="smtp_host" name="smtp_host" value="{esc(smtp.get('host'))}" placeholder="{esc(_("es. smtp.tuofornitore.it"))}"></div>
      <div><label for="smtp_port">{esc(_("SMTP: porta"))}</label><input type="number" id="smtp_port" name="smtp_port" value="{esc(smtp.get('port', 587))}"></div>
    </div>
    <label for="smtp_security">{esc(_("SMTP: sicurezza"))}</label>
    <select id="smtp_security" name="smtp_security">
      <option value="starttls" {sel(smtp.get('security', 'starttls') == 'starttls')}>{esc(_("STARTTLS (di solito porta 587)"))}</option>
      <option value="ssl" {sel(smtp.get('security') == 'ssl')}>{esc(_("SSL/TLS (di solito porta 465)"))}</option>
      <option value="none" {sel(smtp.get('security') == 'none')}>{esc(_("Nessuna"))}</option>
    </select>
    <div class="row">
      <div><label for="smtp_username">{esc(_("SMTP: utente"))}</label><input type="text" id="smtp_username" name="smtp_username" value="{esc(smtp.get('username'))}" autocomplete="off"></div>
      <div><label for="smtp_password">{esc(_("SMTP: password"))}</label><input type="password" id="smtp_password" name="smtp_password" autocomplete="new-password" placeholder="{esc(_("impostata: vuoto per non cambiarla")) if has_pw else ''}"></div>
    </div>
    <p></p>
    <button type="submit" name="action" value="save_mail">{esc(_("Salva"))}</button>
    <button type="submit" name="action" value="test_mail" class="secondary">{esc(_("Invia una mail di prova"))}</button>
    <div class="note">{esc(_("La prova usa i valori scritti qui sopra, senza salvarli."))}</div>
  </form>
</section>"""


def _alerts_section(cfg, radio):
    rule_set, s = alerts.rules(cfg["mail"]), alerts.summary_cfg(cfg["mail"])
    labels = {"immediate": _("Subito"), "after": _("Se dura più di…"), "summary": _("Solo nel riepilogo"),
              "never": _("Mai")}
    rows = []
    for key in alerts.visible_categories(radio):
        label, r = alerts.category_label(key), rule_set[key]
        opts = "".join(f'<option value="{m}" {"selected" if r["mode"] == m else ""}>{esc(labels[m])}</option>' for m in alerts.MODES)
        minutes = f"{r['after_min']:g}"
        rows.append(f"""<tr><td>{esc(label)}</td>
      <td><select name="mode_{key}" class="rule-mode" aria-label="{esc(label)}">{opts}</select></td>
      <td><span class="rule-after"><input type="number" name="after_{key}" value="{minutes}" min="1" max="1440" step="1"
        aria-label="{esc(_("minuti per {v}", v=label))}"> {esc(_("minuti"))}</span></td></tr>""")
    freq = "".join(f'<option value="{v}" {"selected" if s["frequency"] == v else ""}>{esc(t)}</option>'
                   for v, t in (("off", _("Spento")), ("daily", _("Ogni giorno")), ("weekly", _("Ogni settimana"))))
    days = "".join(f'<option value="{i}" {"selected" if s["weekday"] == i else ""}>{esc(_(d))}</option>'
                   for i, d in enumerate(alerts.WEEKDAYS))
    return f"""
<section>
  <h2>{esc(_("Quando avvisare"))}</h2>
  <p class="hint">{_("Per ogni tipo di evento: una mail <b>subito</b>; solo <b>se dura più di</b> qualche minuto (se "
                     "finisce prima va nel riepilogo: così un rinnovo dell'IP di pochi secondi non ti scrive); <b>solo "
                     "nel riepilogo</b>; oppure <b>mai</b>. Quando parte la mail “è giù”, a guasto finito arriva "
                     "anche quella “è tornata”.")}</p>
  <form method="post" action="/setup">
    <table class="chain rules">
      <tr><th>{esc(_("Evento"))}</th><th>{esc(_("Avviso"))}</th><th></th></tr>
      {"".join(rows)}
    </table>
    <label for="summary_frequency">{esc(_("Riepilogo"))}</label>
    <select id="summary_frequency" name="summary_frequency">{freq}</select>
    <div class="row summary-when">
      <div class="summary-weekly"><label for="summary_weekday">{esc(_("Giorno"))}</label><select id="summary_weekday" name="summary_weekday">{days}</select></div>
      <div><label for="summary_hour">{esc(_("Ora"))}</label><input type="number" id="summary_hour" name="summary_hour" min="0" max="23" value="{s['hour']}"></div>
    </div>
    <label class="check summary-when"><input type="checkbox" name="summary_quiet" value="1" {"checked" if s["send_if_quiet"] else ""}>
      {esc(_("Invialo anche se non è successo nulla (conferma che dobermann è vivo e che le mail arrivano)"))}</label>
    <p></p>
    <button type="submit" name="action" value="save_alerts">{esc(_("Salva"))}</button>
    <button type="submit" name="action" value="test_summary" class="secondary">{esc(_("Invia ora un riepilogo degli ultimi 7 giorni"))}</button>
  </form>
  <script>
  (function(){{
    document.querySelectorAll('.rule-mode').forEach(sel => {{
      const after = sel.closest('tr').querySelector('.rule-after');
      const sync = () => after.style.display = sel.value === 'after' ? '' : 'none';
      sel.addEventListener('change', sync); sync();
    }});
    const f = document.getElementById('summary_frequency');
    const syncF = () => {{
      document.querySelectorAll('.summary-when').forEach(e => e.style.display = f.value === 'off' ? 'none' : '');
      document.querySelectorAll('.summary-weekly').forEach(e => e.style.display = f.value === 'weekly' ? '' : 'none');
    }};
    f.addEventListener('change', syncF); syncF();
  }})();
  </script>
</section>"""


def node_status():
    """{node id: {label: value}} as the engine last wrote it (engine.NODE_STATUS_META), if recent."""
    try:
        con = report._connect()
        try:
            row = con.execute("SELECT value FROM meta WHERE key='node_status'").fetchone()
        finally:
            con.close()
        data = json.loads(row[0]) if row else {}
        return (data.get("nodes") or {}) if time.time() - data.get("ts", 0) < 600 else {}
    except Exception:
        return {}


def _chain_section(chain_spec, secrets):
    nodes = chain_spec.get("nodes", [])
    states = node_status()
    rows = []
    for n in nodes:
        cls = adapters.get(n.get("adapter")) if n.get("adapter") else None
        if cls is None:
            how = '<span class="tag">' + esc(_("solo dall'esterno")) + '</span>'
        elif cls.level == adapters.base.LEVEL_STANDARD:
            how = f'<span class="tag">{esc(cls.name)}</span>'
        else:
            tip = _("Interfaccia non ufficiale: può smettere di funzionare dopo un aggiornamento del firmware")
            how = f'<span class="tag unofficial" title="{esc(tip)}">{esc(cls.name)}</span>'
        pw = ""
        if cls is not None and cls.accepts_credentials:
            has = bool((secrets.get("devices", {}).get(n["id"]) or {}).get("password"))
            hint = _("impostata: vuoto per non cambiarla") if has else _("facoltativa")
            pw = f'<input type="password" name="pw_{esc(n["id"])}" autocomplete="new-password" placeholder="{esc(hint)}">'
            st = states.get(n["id"])
            if isinstance(st, dict) and st:
                pw += '<div class="note">' + esc(" · ".join(f"{k}: {v}" for k, v in st.items())) + "</div>"
        rows.append(f"""<tr>
  <td><input type="text" name="label_{esc(n['id'])}" value="{esc(n.get('label'))}"></td>
  <td><code>{esc(n.get('ip'))}</code></td>
  <td>{how}</td>
  <td>{pw}</td>
  <td><label class="check" style="margin:0"><input type="checkbox" name="remove_{esc(n['id'])}" value="1"> {esc(_("togli"))}</label></td>
</tr>""")
    line = " → ".join(["dobermann"] + [esc(n.get("label") or n["id"]) for n in nodes] + ["internet"])
    link = Chain(chain_spec, secrets).link_description()
    table = (f"""<p class="chainline">{line}<br><span class="hint">{esc(_("Linea verso internet: {v}. Si decide dall'ultimo apparato della catena, quello più vicino a internet.", v=link))}</span></p>
  <form method="post" action="/setup">
  <table class="chain"><tr><th>{esc(_("Nome"))}</th><th>{esc(_("Indirizzo"))}</th><th>{esc(_("Lettura"))}</th><th>{esc(_("Password"))}</th><th></th></tr>
  {''.join(rows)}</table>
  <button type="submit" name="action" value="save_chain">{esc(_("Salva la catena"))}</button>
  </form>""" if nodes else
             '<p class="chainline">' + _("Nessuna catena configurata: dobermann sorveglia il gateway predefinito di "
                                         "questa macchina, solo dall'esterno. Premi <b>Rileva</b>.") + '</p>')
    return f"""
<section>
  <h2>{esc(_("Catena verso internet"))}</h2>
  <p class="hint">{esc(_("Gli apparati fra dobermann e internet, in ordine. Per ognuno dobermann sceglie il modo migliore "
                         "per leggerlo; un apparato che non sa leggere resta sorvegliato dall'esterno (ping e misure verso "
                         "internet). La password serve solo agli apparati che con la password danno più dettagli (il campo "
                         "compare solo per loro). Alcuni accettano un solo accesso alla volta: dobermann allora si ritira "
                         "quando entri tu."))}</p>
  {table}
  <form method="post" action="/setup" style="margin-top:16px">
    <label for="extra">{esc(_("Apparato a monte da aggiungere (facoltativo)"))}</label>
    <input type="text" id="extra" name="extra" placeholder="{esc(_("es. 192.168.2.1 — un modem in bridge o IP passthrough non si vede da solo"))}">
    <p></p>
    <button type="submit" name="action" value="detect">{esc(_("Rileva"))}</button>
    <div class="note">{esc(_("Rileva sostituisce la catena con quella trovata ora (conserva le password già salvate). Può "
                             "richiedere qualche secondo."))}</div>
  </form>
</section>"""


def _measure_section(cfg):
    th, dns = cfg["thresholds"], cfg["dns"]
    anchors = "\n".join(f"{name},{ip}" for name, ip in cfg["anchors"])

    def num(name, value, label, unit, lo, hi):
        return (f'<label for="{name}">{esc(label)}</label><div class="setting-row"><input type="number" id="{name}" '
                f'name="{name}" value="{esc(value)}" min="{lo}" max="{hi}"><span class="unit">{esc(unit)}</span></div>')
    return f"""
<section>
  <h2>{esc(_("Misure"))}</h2>
  <p class="hint">{esc(_("Cosa misura dobermann e quando considera un problema un incidente. Si applica al campione "
                         "successivo, senza riavvii."))}</p>
  <form method="post" action="/setup">
    {num("interval_s", cfg["interval_s"], _("Intervallo di campionamento"), _("secondi"), 1, 60)}
    <label for="anchors">{esc(_("Ancore su internet (una per riga: nome,indirizzo)"))}</label>
    <textarea id="anchors" name="anchors">{esc(anchors)}</textarea>
    <div class="note">{esc(_("Internet è considerato giù quando la media delle perdite verso le ancore supera la soglia qui sotto."))}</div>
    <div class="row">
      <div><label for="dns_local">{esc(_("DNS locale"))}</label><input type="text" id="dns_local" name="dns_local" value="{esc(dns.get('local'))}" placeholder="{esc(_("vuoto: quello di sistema ({v})", v=config.system_resolver() or _("nessuno")))}"></div>
      <div><label for="dns_external">{esc(_("DNS esterno"))}</label><input type="text" id="dns_external" name="dns_external" value="{esc(dns.get('external'))}"></div>
    </div>
    {num("high_latency_ms", th["high_latency_ms"], _("Latenza alta oltre"), "ms", 10, 5000)}
    {num("packet_loss_pct", th["packet_loss_pct"], _("Perdita di pacchetti da"), _("% (media ancore)"), 1, 99)}
    {num("internet_down_pct", th["internet_down_pct"], _("Internet giù da"), _("% (media ancore)"), 1, 100)}
    {num("confirm_samples", th["confirm_samples"], _("Conferma dei problemi incerti"), _("campioni di fila"), 1, 10)}
    {num("retention_days", cfg["retention_days"], _("Conservazione dei campioni"), _("giorni (incidenti e riepiloghi orari restano per sempre)"), 1, 365)}
    <p></p>
    <button type="submit" name="action" value="save_measure">{esc(_("Salva"))}</button>
  </form>
</section>"""


def _address_section(cfg, commands=None):
    here = address.machine_address() or "?"
    inst = cfg.get("install") or {}
    https = os.path.exists(os.path.join(config.config_dir(), "tls.crt"))
    scheme = "https" if https else "http"
    name = inst.get("name")
    reach = [f"{scheme}://{here}/"] + ([f"{scheme}://{name}/"] if name else [])
    plat = address.detect_platform(cfg)
    where = {"proxmox-lxc": _("container Proxmox {v}", v=plat.get("ctid")), "networkmanager": "NetworkManager",
             "ifupdown": "ifupdown (/etc/network/interfaces)", "networkd": "systemd-networkd",
             "netplan": "netplan"}.get(
        plat["kind"], _("non riconosciuta"))
    result = ""
    if commands:
        where_run, lines = commands
        result = (f'<div class="banner">{esc(_("Ultimo passo, da fare {v}:", v=where_run))}'
                  f'<pre class="cmds">{esc(chr(10).join(lines))}</pre>'
                  f'{esc(_("Poi apri il pannello al nuovo indirizzo. Il nome locale segue da solo."))}</div>')
    return f"""
<section id="address">
  <h2>{esc(_("Indirizzo di dobermann"))}</h2>
  <p class="hint">{esc(_("Dove si raggiunge il pannello. Rete gestita da: {v}.", v=where))}</p>
  <p class="chainline">{"<br>".join(f"<code>{esc(u)}</code>" for u in reach)}</p>
  {result}
  <form method="post" action="/setup">
    <label for="dashboard_url">{esc(_("Indirizzo di questo pannello (messo nelle mail)"))}</label>
    <input type="text" id="dashboard_url" name="dashboard_url" value="{esc(cfg.get('dashboard_url'))}" placeholder="{scheme}://{esc(here)}/watchdash">
    <button type="submit" name="action" value="save_link">{esc(_("Salva"))}</button>
  </form>
  <form method="post" action="/setup" style="margin-top:16px">
    <div class="row">
      <div><label for="new_ip">{esc(_("Nuovo indirizzo IP (con la maschera)"))}</label><input type="text" id="new_ip" name="new_ip" placeholder="192.168.1.60/24"></div>
      <div><label for="new_gw">{esc(_("Gateway"))}</label><input type="text" id="new_gw" name="new_gw" value="{esc(address.default_gateway() or '')}"></div>
    </div>
    <button type="submit" name="action" value="change_ip" class="secondary">{esc(_("Prepara il cambio di indirizzo"))}</button>
    <div class="note">{esc(_("dobermann aggiorna il certificato HTTPS (valido per il vecchio e il nuovo indirizzo) e il link nelle mail, poi ti mostra il comando da lanciare per cambiare l'indirizzo della macchina: il pannello non ha i permessi per farlo da solo."))}</div>
  </form>
</section>"""


def _updates_section(cfg):
    st = updates.load_status() or {}
    on = (cfg.get("updates") or {}).get("check", True)
    if st.get("newer"):
        plat = cfg.get("platform") or {}
        if plat.get("kind") == "proxmox-lxc":
            where, cmd = _("Per aggiornare, dall'host Proxmox, come root:"), f"pct exec {plat['ctid']} -- /usr/local/bin/dobermann update --yes"
        else:
            where, cmd = _("Per aggiornare, su questa macchina:"), "sudo dobermann update"
        state = (f'<div class="banner">{esc(_("È disponibile la versione {v}.", v=st["latest"]))} '
                 f'<a href="{esc(st["compare_url"])}" target="_blank" rel="noopener">{esc(_("Cosa cambia"))}</a><br>'
                 f'{esc(where)}<pre class="cmds">{esc(cmd)}</pre></div>')
    elif st.get("error"):
        state = f'<p class="note">{esc(_("Ultimo controllo non riuscito: {v}", v=st["error"]))}</p>'
    elif st.get("ts"):
        state = f'<p class="note">{esc(_("Nessuna versione nuova (controllato il {v}).", v=datetime.fromtimestamp(st["ts"]).strftime("%d/%m/%Y %H:%M")))}</p>'
    else:
        state = f'<p class="note">{esc(_("Non ancora controllato."))}</p>'
    return f"""
<section id="updates">
  <h2>{esc(_("Aggiornamenti"))}</h2>
  <p class="hint">{esc(_("Versione installata: {v}. Una volta al giorno dobermann chiede a GitHub se c'è una versione nuova (manda solo la propria versione) e te lo dice qui e in alto. Non aggiorna da solo: l'aggiornamento lo lanci tu.", v=__version__))}</p>
  {state}
  <form method="post" action="/setup">
    <label class="check"><input type="checkbox" name="updates_check" value="1" {"checked" if on else ""}> {esc(_("Controlla ogni giorno se c'è una versione nuova"))}</label>
    <button type="submit" name="action" value="save_updates">{esc(_("Salva"))}</button>
    <button type="submit" name="action" value="check_updates" class="secondary">{esc(_("Controlla ora"))}</button>
  </form>
</section>"""


def _password_section():
    return f"""
<section id="password">
  <h2>{esc(_("Password del pannello"))}</h2>
  <form method="post" action="/setup">
    <label for="new_password">{esc(_("Nuova password (almeno 8 caratteri)"))}</label>
    <input type="password" id="new_password" name="new_password" minlength="8" autocomplete="new-password">
    <label for="new_password2">{esc(_("Ripeti"))}</label>
    <input type="password" id="new_password2" name="new_password2" minlength="8" autocomplete="new-password">
    <p></p>
    <button type="submit" name="action" value="password">{esc(_("Cambia password"))}</button>
    <div class="note">{esc(_("Gli altri browser collegati vengono disconnessi."))}</div>
  </form>
</section>"""


def render_setup(banner=None, error=False, notes=(), token=None, commands=None):
    cfg, chain_spec, secrets = config.load(), config.load_chain(), config.load_secrets()
    banner_html = ""
    if banner:
        extra = "".join(f"<br>{esc(n)}" for n in notes)
        banner_html = f'<div class="banner{" error" if error else ""}">{esc(banner)}{extra}</div>'
    body = f"""<div class="topnav"><a href="/watchdash">← {esc(_("Oggi"))}</a>{toolbar(token, "/setup")}</div>
<h1>{esc(_("Impostazioni"))}</h1>
<p class="subtitle">{esc(_("Le modifiche si applicano al campione successivo, senza riavviare nulla."))}</p>
{banner_html}
{_chain_section(chain_spec, secrets)}
{_mail_section(cfg, secrets)}
{_alerts_section(cfg, Chain(chain_spec, secrets).radio_uplink() is not None)}
{_measure_section(cfg)}
{_address_section(cfg, commands)}
{_updates_section(cfg)}
{_password_section()}"""
    return _fill(_tpl("setup.html"), {"__BODY__": body})


class FormError(ValueError):
    pass


def _int(form, name, lo, hi):
    try:
        v = int(form.get(name, [""])[0])
    except ValueError:
        raise FormError(_("{v}: serve un numero", v=name)) from None
    if not lo <= v <= hi:
        raise FormError(_("{v}: dev'essere fra {lo} e {hi}", v=name, lo=lo, hi=hi))
    return v


def _stored_mail_password(cfg, secrets, mail):
    """The saved SMTP password, but only for the server and user it was saved with. Otherwise None: a
    changed server field must never receive it (a test mail would hand it over)."""
    old, new = cfg["mail"].get("smtp", {}), mail.get("smtp", {})
    same = ((old.get("host") or "").strip().lower() == (new.get("host") or "").strip().lower()
            and (old.get("username") or "").strip() == (new.get("username") or "").strip())
    return (secrets.get("mail") or {}).get("password") if same else None


def _mail_from_form(form, cfg):
    def f(name, default=""):
        return form.get(name, [default])[0].strip()
    security = f("smtp_security", "starttls")
    if security not in ("starttls", "ssl", "none"):
        raise FormError(_("sicurezza SMTP non valida"))
    m = dict(cfg["mail"])
    m.update(enabled=f("enabled") == "1", to=f("to"),
             smtp={"host": f("smtp_host"), "port": _int(form, "smtp_port", 1, 65535), "security": security,
                   "username": f("smtp_username")})
    m.pop("relay", None)
    m.pop("transport", None)      # from the versions that also had a local relay
    m["from"] = f("from")
    if any(c in m["to"] + m["from"] + m["smtp"]["username"] for c in "\r\n"):
        raise FormError(_("destinatario, mittente o oggetto contengono un a capo."))
    if m["enabled"] and not m["to"]:
        raise FormError(_("per inviare le notifiche serve un destinatario"))
    return m


# --- request handling ---------------------------------------------------------------------------

class LoginThrottle:
    """Wrong panel passwords. Every attempt is counted BEFORE the password is checked, in the same locked
    step as the limit test, so parallel requests cannot all slip past it. After PER_CLIENT attempts from
    one address within WINDOW_S that address waits; after GLOBAL from all addresses together everyone
    does. GLOBAL is a backstop against many addresses, set well above PER_CLIENT so that a single
    machine cannot lock the user out (it would need GLOBAL / PER_CLIENT addresses)."""
    WINDOW_S, PER_CLIENT, GLOBAL = 60, 5, 50

    def __init__(self, clock=time.monotonic):
        self.clock, self.lock, self.fails = clock, threading.Lock(), []   # [(ts, client)]

    def _recent(self):
        cut = self.clock() - self.WINDOW_S
        self.fails = [f for f in self.fails if f[0] > cut]
        return self.fails

    def attempt(self, client):
        """False if this client must wait; otherwise counts the attempt as a failure until succeeded()."""
        with self.lock:
            recent = self._recent()
            if len(recent) >= self.GLOBAL or sum(1 for _ts, c in recent if c == client) >= self.PER_CLIENT:
                return False
            recent.append((self.clock(), client))
            return True

    def succeeded(self, client):
        with self.lock:
            self.fails = [f for f in self._recent() if f[1] != client]


_login_throttle = LoginThrottle()


# where /language may send the browser back to: a path of this panel, nothing that a browser could read
# as another host ("//", "/\\") and nothing that could end the Location header (CR, LF)
_SAFE_NEXT = re.compile(r"^/(?![/\\])[A-Za-z0-9_\-./?=&%]*\Z")


class Handler(BaseHTTPRequestHandler):
    server_version = "dobermann"
    timeout = 30        # a client that connects and stalls frees its thread

    def log_message(self, fmt, *args):
        pass

    def _send(self, body, content_type="text/html; charset=utf-8", status=200, cache="no-store", headers=()):
        body = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Frame-Options", "DENY")
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, status=200):
        self._send(json.dumps(obj), "application/json; charset=utf-8", status)

    def _token(self):
        jar = http.cookies.SimpleCookie()
        try:
            jar.load(self.headers.get("Cookie") or "")
        except http.cookies.CookieError:
            return None
        m = jar.get(auth.SESSION_COOKIE)
        return m.value if m else None

    def _redirect(self, location, token=None, clear=False):
        self.send_response(303)
        self.send_header("Location", location)
        secure = "; Secure" if getattr(self.server, "is_tls", False) else ""
        if token:
            self.send_header("Set-Cookie", f"{auth.SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax; "
                                           f"Max-Age={auth.SESSION_TTL}{secure}")
        if clear:
            self.send_header("Set-Cookie", f"{auth.SESSION_COOKIE}=; Path=/; HttpOnly; Max-Age=0{secure}")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _to_https(self):
        tls_port = getattr(self.server, "tls_port", None)
        if tls_port is None or getattr(self.server, "is_tls", False):
            return False
        host = listen.host_for_redirect(self.headers.get("Host"))
        if host is None:
            self._send("400 - " + _("intestazione Host non valida"), "text/plain; charset=utf-8", 400)
            return True
        self.send_response(308)
        self.send_header("Location", f"https://{host}{'' if tls_port == 443 else f':{tls_port}'}{self.path}")
        self.send_header("Content-Length", "0")
        self.end_headers()
        return True

    def _body(self, limit):
        """The request body, or None when Content-Length is missing, malformed, negative or over `limit`
        (a negative length would otherwise mean "read until the client hangs up")."""
        raw = (self.headers.get("Content-Length") or "0").strip()
        # ASCII digits only and few of them: int() refuses strings over 4300 digits with an exception
        if not re.fullmatch(r"[0-9]{1,9}", raw) or int(raw) > limit:
            return None
        return self.rfile.read(int(raw))

    def _form(self):
        body = self._body(64 * 1024)
        if body is None:
            return {}
        return urllib.parse.parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True)

    def _login_page(self, error=None, status=200):
        first = not auth.is_configured()
        mapping = {
            "__TITLE__": esc(_("Benvenuto") if first else _("Accedi")),
            "__SUBTITLE__": esc(_("Scegli la password del pannello di dobermann.") if first else _("Pannello di dobermann.")),
            "__PASSWORD_LABEL__": esc(_("Nuova password") if first else _("Password")),
            "__BUTTON_LABEL__": esc(_("Imposta password") if first else _("Accedi")),
            "__PASSWORD2_FIELD__": (f'<label for="password2">{esc(_("Conferma password"))}</label>'
                                    '<input type="password" id="password2" name="password2" required minlength="8">')
            if first else "",
            "__CODE_FIELD__": ('<label for="setup_code">' + esc(_("Codice di installazione (stampato alla fine dell'installazione)"))
                               + '</label><input type="text" id="setup_code" name="setup_code" '
                               'required autocomplete="off" autocapitalize="characters" spellcheck="false">')
            if first and auth.setup_code_required() else "",
            "__ERROR__": f'<div class="error">{esc(error)}</div>' if error else "",
        }
        self._send(_fill(_tpl("login.html"), mapping), status=status)

    def _path(self):
        parsed = urllib.parse.urlparse(self.path)
        return (parsed.path.rstrip("/") or "/"), urllib.parse.parse_qs(parsed.query)

    def _set_language(self):
        try:
            i18n.set_language(config.load().get("language"))
        except (OSError, ValueError):
            i18n.set_language(None)

    def do_GET(self):
        self._set_language()
        if self._to_https():
            return
        path, qs = self._path()
        if path in ("/logo.svg", "/favicon.ico"):
            with open(os.path.join(TEMPLATES, "logo.svg"), "rb") as f:
                self._send(f.read(), "image/svg+xml", cache="public, max-age=86400")
            return
        authed = auth.check_session(self._token())
        if path == "/login":
            if authed:
                self._redirect("/watchdash")
            else:
                self._login_page()
            return
        if not authed:
            self._redirect("/login")
            return
        if path == "/logout":
            self._redirect("/watchdash")      # logging out is a POST: a link on another site must not do it
        elif path in ("/", "/watchdash"):
            if path == "/":
                self._redirect("/watchdash")
            else:
                self._send(render_data_page("watchdash.html", report.build_live_report(), self._token(), "/watchdash"))
        elif path == "/report":
            period = qs.get("period", ["week"])[0]
            period = period if period in REPORT_PERIODS else "week"
            self._send(render_data_page("report.html", report.build_period_report(period, qs.get("date", [None])[0]),
                                        self._token(), self.path))
        elif path == "/setup":
            note = qs.get("note", [None])[0]
            self._send(render_setup(token=self._token(), banner=note))
        elif path == "/api/samples":
            try:
                a, b = float(qs["from"][0]), float(qs["to"][0])
            except (KeyError, ValueError):
                self._json({"error": _("from/to (secondi epoch) richiesti")}, 400)
                return
            self._json(report.fine_samples(a, b))
        elif path == "/api/interval":
            self._json({"interval": config.load()["interval_s"], "min": 1, "max": 60})
        elif path == "/speed":
            self._send(render_speed(self._token()))
        elif path in ("/dossier", "/dossier.csv"):
            month = qs.get("month", [datetime.now().strftime("%Y-%m")])[0]
            scope = qs.get("scope", ["outages"])[0]
            try:
                dossier.month_bounds(month)
                min_s = max(0, min(86400, int(qs.get("min", ["0"])[0])))
            except ValueError:
                self._send(_("mese non valido (AAAA-MM)"), "text/plain; charset=utf-8", 400)
                return
            d = dossier.build(month, scope if scope in dossier.SCOPES else "outages", min_s)
            if path == "/dossier.csv":
                cols, rows = dossier.csv_rows(d)
                buf = io.StringIO()
                w = csv.writer(buf)
                w.writerow(cols)
                w.writerows(rows)
                self._send(buf.getvalue(), "text/csv; charset=utf-8",
                           headers=[("Content-Disposition", f'attachment; filename="dobermann-{month}.csv"')])
            else:
                self._send(_fill(_tpl("dossier.html"), {"__TOOLBAR__": toolbar(self._token(), self.path),
                                                        "__BODY__": dossier.render(d)}))
        elif path == "/export":
            table = qs.get("table", ["samples"])[0]
            if table not in report.EXPORT_QUERIES:
                self._send(_("tabella non valida (samples o hourly)"), "text/plain; charset=utf-8", 400)
                return
            now = time.time()

            def day(s, default):
                try:
                    return datetime.strptime(s, "%Y-%m-%d").timestamp()
                except (TypeError, ValueError):
                    return default
            cols, rows = report.export_rows(table, day(qs.get("from", [None])[0], now - 30 * 86400),
                                            day(qs.get("to", [None])[0], now) + 86400)
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(cols)
            w.writerows(rows)
            self._send(buf.getvalue(), "text/csv; charset=utf-8",
                       headers=[("Content-Disposition", f'attachment; filename="dobermann-{table}.csv"')])
        else:
            self._send("404 - " + _("pagina inesistente: prova /watchdash, /report, /setup"), "text/plain; charset=utf-8", 404)

    def do_POST(self):
        self._set_language()
        if self._to_https():
            return
        path, _qs = self._path()
        if path == "/login":
            self._post_login()
            return
        if not auth.check_session(self._token()):
            self._redirect("/login")
            return
        if path == "/setup":
            self._post_setup()
        elif path == "/language":
            form = self._form()
            lang = form.get("lang", [""])[0]
            if lang in i18n.available():
                with _config_lock:
                    raw = _raw_config()
                    raw["language"] = lang
                    config.save(raw)
            nxt = form.get("next", ["/watchdash"])[0]
            self._redirect(nxt if _SAFE_NEXT.match(nxt) else "/watchdash")
        elif path == "/speed":
            self._post_speed()
        elif path == "/logout":
            auth.destroy_session(self._token())
            self._redirect("/login", clear=True)
        elif path == "/logout-all":
            auth.destroy_all_sessions()
            self._redirect("/login", clear=True)
        elif path == "/api/interval":
            try:
                data = json.loads(self._body(1024) or b"{}")
                v = int(data.get("interval") if isinstance(data, dict) else None)
            except (TypeError, ValueError):
                self._json({"error": _("interval deve essere un numero")}, 400)
                return
            if not 1 <= v <= 60:
                self._json({"error": _("interval fra 1 e 60")}, 400)
                return
            with _config_lock:
                cfg = json.loads(json.dumps(_raw_config()))
                cfg["interval_s"] = v
                config.save(cfg)
            self._json({"interval": v})
        else:
            self._send("404", "text/plain; charset=utf-8", 404)

    def _post_login(self):
        form = self._form()
        password = form.get("password", [""])[0]
        client = self.client_address[0]
        guarded = auth.is_configured() or auth.setup_code_required()
        if guarded and not _login_throttle.attempt(client):
            self._login_page(_("Troppi tentativi sbagliati: riprova fra un minuto."), 429)
            return
        if not auth.is_configured():
            if auth.setup_code_required() and not auth.check_setup_code(form.get("setup_code", [""])[0]):
                time.sleep(1)
                self._login_page(_("Codice di installazione errato."), 401)
            elif len(password) < 8:
                self._login_page(_("La password deve avere almeno 8 caratteri."), 400)
            elif password != form.get("password2", [""])[0]:
                self._login_page(_("Le due password non coincidono."), 400)
            else:
                auth.set_password(password)
                auth.clear_setup_code()
                _login_throttle.succeeded(client)
                self._redirect("/setup", token=auth.create_session())
            return
        if auth.verify_password(password):
            _login_throttle.succeeded(client)
            self._redirect("/watchdash", token=auth.create_session())
        else:
            time.sleep(1)   # slows down guessing
            self._login_page(_("Password errata."), 401)

    def _post_speed(self):
        try:
            size = int(self._form().get("size", ["0"])[0])
        except ValueError:
            size = 0
        try:
            con = store.connect(config.db_file())
            try:
                res = speedtest.run(con, config.load(), size)
            finally:
                con.close()
        except (speedtest.Busy, ValueError) as e:
            self._send(render_speed(self._token(), str(e), error=True), status=409)
            return
        if res["error"]:
            self._send(render_speed(self._token(), _("Test non riuscito: {v}", v=res["error"]), error=True))
        else:
            self._send(render_speed(self._token(), _("Download {d}, upload {u}.", d=_mbps(res["down_bps"]),
                                                     u=_mbps(res["up_bps"]))))

    def _post_setup(self):
        form = self._form()
        action = form.get("action", [""])[0]
        try:
            with _config_lock:
                msg, notes, token = self._apply(action, form)
                if action == "change_ip":
                    self._send(render_setup(token=self._token(), banner=msg, commands=notes))
                    return
        except FormError as e:
            self._send(render_setup(token=self._token(), banner=_("Non salvato: {v}", v=e), error=True), status=400)
            return
        if action in ("test_mail", "check_updates"):
            failed = msg.startswith(_("Invio fallito:")) or msg.startswith(_("Ultimo controllo non riuscito: {v}", v=""))
            self._send(render_setup(token=self._token(), banner=msg, error=failed))
            return
        if notes:
            self._send(render_setup(token=self._token(), banner=msg, notes=notes))
            return
        self._redirect("/setup?note=" + urllib.parse.quote(msg), token=token)

    def _apply(self, action, form):
        """(message, notes, new session token or None)."""
        cfg, raw, secrets = config.load(), _raw_config(), config.load_secrets()
        if action in ("save_mail", "test_mail"):
            mail = _mail_from_form(form, cfg)
            pw = form.get("smtp_password", [""])[0]
            stored = _stored_mail_password(cfg, secrets, mail)
            if not pw and stored is None and mail["smtp"].get("username") and (secrets.get("mail") or {}).get("password"):
                raise FormError(_("server o utente SMTP sono cambiati: riscrivi la password (quella salvata vale solo "
                                  "per il server e l'utente con cui è stata salvata)"))
            if action == "test_mail":
                test_secrets = {"mail": {"password": pw or stored}}
                ok, why = notify.deliver(dict(mail, enabled=True), test_secrets,
                                         "[dobermann] " + _("Mail di prova"),
                                         _("Se leggi questo messaggio, le notifiche di dobermann arrivano."))
                if not ok:
                    print(_("mail di prova fallita: {v}", v=why), flush=True)
                return (_("Mail di prova inviata: controlla la casella (e lo spam).") if ok else
                        _("Invio fallito:") + f" {why}"), (), None
            msg = _("Notifiche salvate.")
            raw["mail"] = mail
            config.save(raw)
            if pw:
                secrets.setdefault("mail", {})["password"] = pw
                config.save_secrets(secrets)
            return msg, (), None
        if action == "save_alerts":
            rules = {}
            for key in alerts.CATEGORIES:
                if f"mode_{key}" not in form:      # a category not shown on this line keeps its rule
                    if key in (cfg["mail"].get("rules") or {}):
                        rules[key] = cfg["mail"]["rules"][key]
                    continue
                mode = form.get(f"mode_{key}", [""])[0]
                if mode not in alerts.MODES:
                    raise FormError(_("scelta di avviso non valida"))
                rules[key] = {"mode": mode, "after_min": _int(form, f"after_{key}", 1, 1440)}
            freq = form.get("summary_frequency", ["weekly"])[0]
            if freq not in ("off", "daily", "weekly"):
                raise FormError(_("frequenza del riepilogo non valida"))
            raw.setdefault("mail", {})
            raw["mail"]["rules"] = rules
            raw["mail"]["summary"] = {"frequency": freq, "weekday": _int(form, "summary_weekday", 0, 6),
                                      "hour": _int(form, "summary_hour", 0, 23),
                                      "send_if_quiet": form.get("summary_quiet", [""])[0] == "1"}
            config.save(raw)
            return _("Regole degli avvisi salvate."), (), None
        if action == "test_summary":
            from dobermann import store
            con = store.connect(config.db_file())
            try:
                now = time.time()
                radio = Chain(config.load_chain(), secrets).radio_uplink()
                subject, body, _quiet = alerts.summary_message(con, now - 7 * 86400, now, alerts.rules(cfg["mail"]),
                                                         cfg.get("dashboard_url"), title=_("Riepilogo di prova"),
                                                         radio_node=radio.id if radio else None)
            finally:
                con.close()
            ok, why = notify.deliver(cfg["mail"], secrets, subject, body)
            if not ok:
                print(_("riepilogo di prova fallito: {v}", v=why), flush=True)
                raise FormError(_("riepilogo non inviato: {v}", v=why))
            return _("Riepilogo inviato: controlla la casella."), (), None
        if action == "save_measure":
            anchors = []
            for line in form.get("anchors", [""])[0].splitlines():
                if line.strip():
                    name, _sep, ip = line.partition(",")
                    if not _valid_ip(ip.strip()):
                        raise FormError(_("ancora non valida: {v} (serve nome,indirizzo IP)", v=repr(line.strip())))
                    anchors.append([name.strip() or ip.strip(), ip.strip()])
            if len(anchors) < 2:
                raise FormError(_("servono almeno due ancore"))
            dns_local = form.get("dns_local", [""])[0].strip() or None
            dns_ext = form.get("dns_external", [""])[0].strip() or None
            for d in (dns_local, dns_ext):
                if d and not _valid_ip(d):
                    raise FormError(_("DNS non valido: {v}", v=repr(d)))
            raw.update(interval_s=_int(form, "interval_s", 1, 60), anchors=anchors,
                       retention_days=_int(form, "retention_days", 1, 365))
            raw["dns"] = dict(cfg["dns"], local=dns_local, external=dns_ext)
            raw["thresholds"] = dict(cfg["thresholds"],
                                     high_latency_ms=_int(form, "high_latency_ms", 10, 5000),
                                     packet_loss_pct=_int(form, "packet_loss_pct", 1, 99),
                                     internet_down_pct=_int(form, "internet_down_pct", 1, 100),
                                     confirm_samples=_int(form, "confirm_samples", 1, 10))
            config.save(raw)
            return _("Misure salvate."), (), None
        if action == "detect":
            extra = [x for x in re.split(r"[\s,]+", form.get("extra", [""])[0]) if x]
            for x in extra:
                if not _valid_ip(x):
                    raise FormError(_("indirizzo non valido: {v}", v=repr(x)))
            chain, notes = discover.propose_chain(extra)
            # a saved device password follows its device: same node id AND same address
            old_ip = {n["id"]: n.get("ip") for n in config.load_chain().get("nodes", [])}
            new_ip = {n["id"]: n.get("ip") for n in chain["nodes"]}
            devices = secrets.get("devices", {})
            for nid in list(devices):
                if nid not in new_ip or new_ip[nid] != old_ip.get(nid):
                    devices.pop(nid)
            config.save_chain(chain)
            config.save_secrets(secrets)
            labels = ", ".join(f"{n['label']} ({n['ip']})" for n in chain["nodes"]) or _("nessun apparato")
            return _("Catena rilevata e salvata: {v}.", v=labels), notes, None
        if action == "save_chain":
            chain = config.load_chain()
            kept = []
            devices = secrets.setdefault("devices", {})
            for n in chain.get("nodes", []):
                if form.get(f"remove_{n['id']}", [""])[0] == "1":
                    devices.pop(n["id"], None)
                    continue
                label = form.get(f"label_{n['id']}", [""])[0].strip()
                if label:
                    n["label"] = label[:80]
                pw = form.get(f"pw_{n['id']}", [""])[0]
                if pw:
                    devices.setdefault(n["id"], {})["password"] = pw
                kept.append(n)
            chain["nodes"] = kept
            config.save_chain(chain)
            config.save_secrets(secrets)
            return _("Catena salvata."), (), None
        if action == "save_link":
            url = form.get("dashboard_url", [""])[0].strip()
            if url and not re.match(r"^https?://[^\s/]+(/\S*)?$", url):
                raise FormError(_("l'indirizzo del pannello deve iniziare con http:// o https://"))
            raw["dashboard_url"] = url
            config.save(raw)
            return _("Indirizzo del pannello salvato."), (), None
        if action == "change_ip":
            return _change_ip(form, cfg, raw)
        if action == "save_updates":
            raw["updates"] = dict(cfg.get("updates") or {}, check=form.get("updates_check", [""])[0] == "1")
            config.save(raw)
            return _("Impostazione salvata."), (), None
        if action == "check_updates":
            st = updates.check_and_save()
            if st.get("error"):
                return _("Ultimo controllo non riuscito: {v}", v=st["error"]), (), None
            return (_("È disponibile la versione {v}.", v=st["latest"]) if st["newer"]
                    else _("dobermann {v} è già l'ultima versione.", v=__version__)), (), None
        if action == "password":
            p1, p2 = form.get("new_password", [""])[0], form.get("new_password2", [""])[0]
            if len(p1) < 8:
                raise FormError(_("la password deve avere almeno 8 caratteri"))
            if p1 != p2:
                raise FormError(_("le due password non coincidono"))
            auth.set_password(p1)
            return _("Password cambiata."), (), auth.create_session()
        raise FormError(_("azione sconosciuta"))


def _change_ip(form, cfg, raw):
    """(message, (where, commands), None): the panel's own part of an address change, done here."""
    try:
        iface, gw = address.parse_new(form.get("new_ip", [""])[0], form.get("new_gw", [""])[0])
    except ValueError as e:
        raise FormError(str(e)) from None
    old = address.machine_address()
    cfg_dir = config.config_dir()
    cert, key = os.path.join(cfg_dir, "tls.crt"), os.path.join(cfg_dir, "tls.key")
    https = os.path.exists(cert)
    name = (cfg.get("install") or {}).get("name")
    done = []
    if https:
        if TOOLS not in sys.path:
            sys.path.insert(0, TOOLS)
        import make_cert
        names = address.cert_names(old, iface.ip, name)
        try:
            make_cert.generate(cert, key, make_cert.san_entries(names))
        except (make_cert.CertError, OSError) as e:
            raise FormError(_("non riesco a rifare il certificato HTTPS: {v}", v=e)) from None
        if _tls_ctx is not None:
            _tls_ctx.load_cert_chain(cert, key)    # new connections get it at once, no restart
        done.append(_("certificato HTTPS rifatto per {v} (impronta {fp})", v=", ".join(names),
                      fp=make_cert.fingerprint(cert)))
    raw["dashboard_url"] = f"{'https' if https else 'http'}://{iface.ip}/watchdash"
    config.save(raw)
    done.append(_("link nelle mail: {v}", v=raw["dashboard_url"]))
    restarts = []
    env_path = os.path.join(cfg_dir, "web.env")
    try:
        with open(env_path) as f:
            env = f.read()
    except FileNotFoundError:
        env = ""
    if old and f"DOBERMANN_BIND={old}" in env:
        with open(env_path, "w") as f:
            f.write(env.replace(f"DOBERMANN_BIND={old}", f"DOBERMANN_BIND={iface.ip}"))
        restarts.append("dobermann-web")
    if os.path.exists("/etc/systemd/system/dobermann-mdns.service"):
        restarts.append("dobermann-mdns")
    plat = address.detect_platform(cfg)
    cmds = address.commands(plat, iface, gw, mac=address.interface_mac() if plat["kind"] == "proxmox-lxc" else None,
                            extra_restarts=restarts)
    return _("Pronto per {v}: {done}.", v=iface.ip, done="; ".join(done)), cmds, None


_tls_ctx = None


def _raw_config():
    """config.json as written (not merged with defaults), so saving does not freeze every default."""
    try:
        with open(config.config_file()) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def main():
    try:
        cfg = listen.resolve(os.path.join(config.config_dir(), "tls.crt"), os.path.join(config.config_dir(), "tls.key"))
        ctx = listen.make_ssl_context(cfg["cert"], cfg["key"]) if cfg["tls"] else None
        global _tls_ctx
        _tls_ctx = ctx
    except listen.ListenError as e:
        sys.exit(f"dobermann web: {e}")
    http_srv = ThreadingHTTPServer((cfg["bind"], cfg["http_port"]), Handler)
    http_srv.daemon_threads = True
    http_srv.tls_port = cfg["tls_port"] if ctx is not None else None
    if ctx is not None:
        https_srv = listen.TLSServer((cfg["bind"], cfg["tls_port"]), Handler, ctx)
        https_srv.is_tls, https_srv.tls_port = True, cfg["tls_port"]
        threading.Thread(target=https_srv.serve_forever, daemon=True, name="https").start()
        print(_("dobermann web: HTTPS su {a}, HTTP su :{p} reindirizza", a=f"{cfg['bind']}:{cfg['tls_port']}",
                p=cfg["http_port"]), flush=True)
    else:
        print(_("dobermann web: HTTP su {a}", a=f"{cfg['bind']}:{cfg['http_port']}"), flush=True)
    http_srv.serve_forever()
