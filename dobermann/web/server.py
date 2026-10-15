"""The web panel.

    python3 -m dobermann web

  /watchdash        today's incidents, live (auto-refresh)
  /report           history: ?period=day|week|month|year, ?date=YYYY-MM-DD for a day
  /setup            settings: notifications, chain of devices, measurements, admin password
  /export           CSV of raw samples or hourly roll-ups (?table=samples|hourly&from=&to=)
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

from dobermann import adapters, alerts, config, discover, notify, relay
from dobermann.chain import Chain
from dobermann.web import auth, listen, report

TEMPLATES = os.path.join(os.path.dirname(__file__), "templates")
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
    with open(os.path.join(TEMPLATES, name), encoding="utf-8") as f:
        return f.read()


def render_data_page(name, data):
    # "</" would let a value close the <script> the data sits in
    return PAGE.format(fragment=_tpl(name).replace("/*__DATA__*/", json.dumps(data).replace("</", "<\\/")))


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

def _mail_section(cfg, secrets):
    m = cfg["mail"]
    has_pw = bool((secrets.get("mail") or {}).get("password"))
    smtp = m.get("smtp", {})
    relay_note = {
        "nullmailer": "Su questa macchina c'è nullmailer: dobermann gli scrive i dati del fornitore qui sotto quando salvi.",
        "postfix": "Su questa macchina c'è Postfix: l'inoltro al fornitore si configura in Postfix, i dati qui sotto "
                   "servono solo alla mail di prova.",
        "other": "Su questa macchina c'è un server di posta (comando sendmail): l'inoltro al fornitore si configura lì, "
                 "i dati qui sotto servono solo alla mail di prova.",
        None: "<b>Su questa macchina non c'è un server di posta</b>: installa nullmailer (apt install nullmailer, poi "
              "rilancia tools/install.py) oppure scegli SMTP diretto.",
    }[relay.detect()]

    def sel(cond):
        return "selected" if cond else ""
    return f"""
<section>
  <h2>Notifiche email</h2>
  <p class="hint">dobermann manda una mail quando la connessione si interrompe e quando torna. <b>Relay locale</b>
  (consigliato): un piccolo server di posta su questa stessa macchina tiene in coda le mail scritte mentre internet è
  giù e le inoltra al tuo fornitore di posta appena torna. <b>SMTP diretto</b>: dobermann parla direttamente con il
  server del fornitore, senza coda. In entrambi i casi servono i dati SMTP del fornitore (nelle sue istruzioni per i
  programmi di posta; per Gmail serve una password per app).</p>
  <form method="post" action="/setup">
    <label class="check"><input type="checkbox" name="enabled" value="1" {"checked" if m.get("enabled") else ""}> Invia le notifiche</label>
    <label for="to">Destinatario</label>
    <input type="email" id="to" name="to" value="{esc(m.get('to'))}">
    <label for="from">Mittente</label>
    <input type="text" id="from" name="from" value="{esc(m.get('from'))}" placeholder="vuoto: uguale all\'utente SMTP">
    <label for="transport">Invio tramite</label>
    <select id="transport" name="transport">
      <option value="relay" {sel(m.get('transport') != 'smtp')}>Relay locale (consigliato)</option>
      <option value="smtp" {sel(m.get('transport') == 'smtp')}>SMTP diretto</option>
    </select>
    <div class="note relay-only">{relay_note}</div>
    <div class="row">
      <div><label for="smtp_host">SMTP: server</label><input type="text" id="smtp_host" name="smtp_host" value="{esc(smtp.get('host'))}" placeholder="es. smtp.tuofornitore.it"></div>
      <div><label for="smtp_port">SMTP: porta</label><input type="number" id="smtp_port" name="smtp_port" value="{esc(smtp.get('port', 587))}"></div>
    </div>
    <label for="smtp_security">SMTP: sicurezza</label>
    <select id="smtp_security" name="smtp_security">
      <option value="starttls" {sel(smtp.get('security', 'starttls') == 'starttls')}>STARTTLS (di solito porta 587)</option>
      <option value="ssl" {sel(smtp.get('security') == 'ssl')}>SSL/TLS (di solito porta 465)</option>
      <option value="none" {sel(smtp.get('security') == 'none')}>Nessuna</option>
    </select>
    <div class="row">
      <div><label for="smtp_username">SMTP: utente</label><input type="text" id="smtp_username" name="smtp_username" value="{esc(smtp.get('username'))}" autocomplete="off"></div>
      <div><label for="smtp_password">SMTP: password</label><input type="password" id="smtp_password" name="smtp_password" autocomplete="new-password" placeholder="{'impostata: vuoto per non cambiarla' if has_pw else ''}"></div>
    </div>
    <label for="dashboard_url">Indirizzo di questo pannello (messo nelle mail)</label>
    <input type="text" id="dashboard_url" name="dashboard_url" value="{esc(cfg.get('dashboard_url'))}" placeholder="es. https://dobermann.lan/watchdash">
    <p></p>
    <button type="submit" name="action" value="save_mail">Salva</button>
    <button type="submit" name="action" value="test_mail" class="secondary">Invia una mail di prova</button>
    <div class="note">La prova usa i valori scritti qui sopra, senza salvarli, e si collega direttamente al server del
    fornitore anche con il relay: così un errore nei dati si vede subito, invece di restare in coda.</div>
  </form>
  <script>
  (function(){{
    const t = document.getElementById('transport');
    function sync(){{
      document.querySelectorAll('.relay-only').forEach(e => e.style.display = t.value === 'relay' ? '' : 'none');
    }}
    t.addEventListener('change', sync); sync();
  }})();
  </script>
</section>"""


def _alerts_section(cfg, radio):
    rule_set, s = alerts.rules(cfg["mail"]), alerts.summary_cfg(cfg["mail"])
    labels = {"immediate": "Subito", "after": "Se dura più di…", "summary": "Solo nel riepilogo", "never": "Mai"}
    rows = []
    for key in alerts.visible_categories(radio):
        cat, r = alerts.CATEGORIES[key], rule_set[key]
        opts = "".join(f'<option value="{m}" {"selected" if r["mode"] == m else ""}>{labels[m]}</option>' for m in alerts.MODES)
        minutes = f"{r['after_min']:g}"
        rows.append(f"""<tr><td>{esc(cat["label"])}</td>
      <td><select name="mode_{key}" class="rule-mode" aria-label="{esc(cat["label"])}">{opts}</select></td>
      <td><span class="rule-after"><input type="number" name="after_{key}" value="{minutes}" min="1" max="1440" step="1"
        aria-label="minuti per {esc(cat["label"])}"> minuti</span></td></tr>""")
    freq = "".join(f'<option value="{v}" {"selected" if s["frequency"] == v else ""}>{t}</option>'
                   for v, t in (("off", "Spento"), ("daily", "Ogni giorno"), ("weekly", "Ogni settimana")))
    days = "".join(f'<option value="{i}" {"selected" if s["weekday"] == i else ""}>{d}</option>'
                   for i, d in enumerate(alerts.WEEKDAYS))
    return f"""
<section>
  <h2>Quando avvisare</h2>
  <p class="hint">Per ogni tipo di evento: una mail <b>subito</b>; solo <b>se dura più di</b> qualche minuto (se finisce
  prima va nel riepilogo: così un rinnovo dell'IP di pochi secondi non ti scrive); <b>solo nel riepilogo</b>; oppure
  <b>mai</b>. Quando parte la mail "è giù", a guasto finito arriva anche quella "è tornata".</p>
  <form method="post" action="/setup">
    <table class="chain rules">
      <tr><th>Evento</th><th>Avviso</th><th></th></tr>
      {"".join(rows)}
    </table>
    <label for="summary_frequency">Riepilogo</label>
    <select id="summary_frequency" name="summary_frequency">{freq}</select>
    <div class="row summary-when">
      <div class="summary-weekly"><label for="summary_weekday">Giorno</label><select id="summary_weekday" name="summary_weekday">{days}</select></div>
      <div><label for="summary_hour">Ora</label><input type="number" id="summary_hour" name="summary_hour" min="0" max="23" value="{s['hour']}"></div>
    </div>
    <label class="check summary-when"><input type="checkbox" name="summary_quiet" value="1" {"checked" if s["send_if_quiet"] else ""}>
      Invialo anche se non è successo nulla (conferma che dobermann è vivo e che le mail arrivano)</label>
    <p></p>
    <button type="submit" name="action" value="save_alerts">Salva</button>
    <button type="submit" name="action" value="test_summary" class="secondary">Invia ora un riepilogo degli ultimi 7 giorni</button>
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


def _chain_section(chain_spec, secrets):
    nodes = chain_spec.get("nodes", [])
    rows = []
    for n in nodes:
        cls = adapters.get(n.get("adapter")) if n.get("adapter") else None
        if cls is None:
            how = '<span class="tag">solo dall\'esterno</span>'
        elif cls.level == adapters.base.LEVEL_STANDARD:
            how = f'<span class="tag">{esc(cls.name)}</span>'
        else:
            how = f'<span class="tag unofficial" title="Interfaccia non ufficiale: può smettere di funzionare dopo un aggiornamento del firmware">{esc(cls.name)}</span>'
        pw = ""
        if cls is not None and cls.accepts_credentials:
            has = bool((secrets.get("devices", {}).get(n["id"]) or {}).get("password"))
            pw = (f'<input type="password" name="pw_{esc(n["id"])}" autocomplete="new-password" '
                  f'placeholder="{"impostata: vuoto per non cambiarla" if has else "facoltativa"}">')
        rows.append(f"""<tr>
  <td><input type="text" name="label_{esc(n['id'])}" value="{esc(n.get('label'))}"></td>
  <td><code>{esc(n.get('ip'))}</code></td>
  <td>{how}</td>
  <td>{pw}</td>
  <td><label class="check" style="margin:0"><input type="checkbox" name="remove_{esc(n['id'])}" value="1"> togli</label></td>
</tr>""")
    line = " → ".join(["dobermann"] + [esc(n.get("label") or n["id"]) for n in nodes] + ["internet"])
    link = esc(Chain(chain_spec, secrets).link_description())
    table = (f"""<p class="chainline">{line}<br><span class="hint">Linea verso internet: {link}. Si decide dall'ultimo
  apparato della catena, quello più vicino a internet.</span></p>
  <form method="post" action="/setup">
  <table class="chain"><tr><th>Nome</th><th>Indirizzo</th><th>Lettura</th><th>Password</th><th></th></tr>
  {''.join(rows)}</table>
  <button type="submit" name="action" value="save_chain">Salva la catena</button>
  </form>""" if nodes else
             '<p class="chainline">Nessuna catena configurata: dobermann sorveglia il gateway predefinito di questa '
             'macchina, solo dall\'esterno. Premi <b>Rileva</b>.</p>')
    return f"""
<section>
  <h2>Catena verso internet</h2>
  <p class="hint">Gli apparati fra dobermann e internet, in ordine. Per ognuno dobermann sceglie il modo migliore
  per leggerlo; un apparato che non sa leggere resta sorvegliato dall'esterno (ping e misure verso internet).
  La password serve solo agli apparati che con la password danno più dettagli (il campo compare solo per loro).
  Alcuni accettano un solo accesso alla volta: dobermann allora si ritira quando entri tu.</p>
  {table}
  <form method="post" action="/setup" style="margin-top:16px">
    <label for="extra">Apparato a monte da aggiungere (facoltativo)</label>
    <input type="text" id="extra" name="extra" placeholder="es. 192.168.2.1 — un modem in bridge o IP passthrough non si vede da solo">
    <p></p>
    <button type="submit" name="action" value="detect">Rileva</button>
    <div class="note">Rileva sostituisce la catena con quella trovata ora (conserva le password già salvate).
    Può richiedere qualche secondo.</div>
  </form>
</section>"""


def _measure_section(cfg):
    th, dns = cfg["thresholds"], cfg["dns"]
    anchors = "\n".join(f"{name},{ip}" for name, ip in cfg["anchors"])

    def num(name, value, label, unit, lo, hi):
        return (f'<label for="{name}">{label}</label><div class="setting-row"><input type="number" id="{name}" '
                f'name="{name}" value="{esc(value)}" min="{lo}" max="{hi}"><span class="unit">{unit}</span></div>')
    return f"""
<section>
  <h2>Misure</h2>
  <p class="hint">Cosa misura dobermann e quando considera un problema un incidente. Si applica al campione
  successivo, senza riavvii.</p>
  <form method="post" action="/setup">
    {num("interval_s", cfg["interval_s"], "Intervallo di campionamento", "secondi", 1, 60)}
    <label for="anchors">Ancore su internet (una per riga: nome,indirizzo)</label>
    <textarea id="anchors" name="anchors">{esc(anchors)}</textarea>
    <div class="note">Internet è considerato giù quando la media delle perdite verso le ancore supera la soglia qui sotto.</div>
    <div class="row">
      <div><label for="dns_local">DNS locale</label><input type="text" id="dns_local" name="dns_local" value="{esc(dns.get('local'))}" placeholder="vuoto: quello di sistema ({esc(config.system_resolver() or 'nessuno')})"></div>
      <div><label for="dns_external">DNS esterno</label><input type="text" id="dns_external" name="dns_external" value="{esc(dns.get('external'))}"></div>
    </div>
    {num("high_latency_ms", th["high_latency_ms"], "Latenza alta oltre", "ms", 10, 5000)}
    {num("packet_loss_pct", th["packet_loss_pct"], "Perdita di pacchetti da", "% (media ancore)", 1, 99)}
    {num("internet_down_pct", th["internet_down_pct"], "Internet giù da", "% (media ancore)", 1, 100)}
    {num("confirm_samples", th["confirm_samples"], "Conferma dei problemi incerti", "campioni di fila", 1, 10)}
    {num("retention_days", cfg["retention_days"], "Conservazione dei campioni", "giorni (incidenti e riepiloghi orari restano per sempre)", 1, 365)}
    <p></p>
    <button type="submit" name="action" value="save_measure">Salva</button>
  </form>
</section>"""


def _password_section():
    return """
<section>
  <h2>Password del pannello</h2>
  <form method="post" action="/setup">
    <label for="new_password">Nuova password (almeno 8 caratteri)</label>
    <input type="password" id="new_password" name="new_password" minlength="8" autocomplete="new-password">
    <label for="new_password2">Ripeti</label>
    <input type="password" id="new_password2" name="new_password2" minlength="8" autocomplete="new-password">
    <p></p>
    <button type="submit" name="action" value="password">Cambia password</button>
    <div class="note">Gli altri browser collegati vengono disconnessi.</div>
  </form>
</section>"""


def render_setup(banner=None, error=False, notes=()):
    cfg, chain_spec, secrets = config.load(), config.load_chain(), config.load_secrets()
    banner_html = ""
    if banner:
        extra = "".join(f"<br>{esc(n)}" for n in notes)
        banner_html = f'<div class="banner{" error" if error else ""}">{esc(banner)}{extra}</div>'
    body = f"""<div class="topnav"><a href="/watchdash">← Oggi</a><a href="/logout">Esci</a></div>
<h1>Impostazioni</h1>
<p class="subtitle">Le modifiche si applicano al campione successivo, senza riavviare nulla.</p>
{banner_html}
{_chain_section(chain_spec, secrets)}
{_mail_section(cfg, secrets)}
{_alerts_section(cfg, Chain(chain_spec, secrets).radio_uplink() is not None)}
{_measure_section(cfg)}
{_password_section()}"""
    return _fill(_tpl("setup.html"), {"__BODY__": body})


class FormError(ValueError):
    pass


def _int(form, name, lo, hi):
    try:
        v = int(form.get(name, [""])[0])
    except ValueError:
        raise FormError(f"{name}: serve un numero") from None
    if not lo <= v <= hi:
        raise FormError(f"{name}: dev'essere fra {lo} e {hi}")
    return v


def _stored_mail_password(cfg, secrets, mail):
    """The saved SMTP password, but only for the server and user it was saved with. Otherwise None: a
    changed server field must never receive it (a test mail, or nullmailer, would hand it over)."""
    old, new = cfg["mail"].get("smtp", {}), mail.get("smtp", {})
    same = ((old.get("host") or "").strip().lower() == (new.get("host") or "").strip().lower()
            and (old.get("username") or "").strip() == (new.get("username") or "").strip())
    return (secrets.get("mail") or {}).get("password") if same else None


def _mail_from_form(form, cfg):
    def f(name, default=""):
        return form.get(name, [default])[0].strip()
    security = f("smtp_security", "starttls")
    if security not in ("starttls", "ssl", "none"):
        raise FormError("sicurezza SMTP non valida")
    m = dict(cfg["mail"])
    m.update(enabled=f("enabled") == "1", to=f("to"), transport="smtp" if f("transport") == "smtp" else "relay",
             smtp={"host": f("smtp_host"), "port": _int(form, "smtp_port", 1, 65535), "security": security,
                   "username": f("smtp_username")})
    m.pop("relay", None)
    m["from"] = f("from")
    if m["enabled"] and not m["to"]:
        raise FormError("per inviare le notifiche serve un destinatario")
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
            if len(recent) >= self.GLOBAL or sum(1 for _, c in recent if c == client) >= self.PER_CLIENT:
                return False
            recent.append((self.clock(), client))
            return True

    def succeeded(self, client):
        with self.lock:
            self.fails = [f for f in self._recent() if f[1] != client]


_login_throttle = LoginThrottle()


class Handler(BaseHTTPRequestHandler):
    server_version = "dobermann"

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
            self._send("400 - intestazione Host non valida", "text/plain; charset=utf-8", 400)
            return True
        self.send_response(308)
        self.send_header("Location", f"https://{host}{'' if tls_port == 443 else f':{tls_port}'}{self.path}")
        self.send_header("Content-Length", "0")
        self.end_headers()
        return True

    def _form(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 64 * 1024:
            return {}
        return urllib.parse.parse_qs(self.rfile.read(n).decode("utf-8", "replace"), keep_blank_values=True)

    def _login_page(self, error=None, status=200):
        first = not auth.is_configured()
        mapping = {
            "__TITLE__": "Benvenuto" if first else "Accedi",
            "__SUBTITLE__": "Scegli la password del pannello di dobermann." if first else "Pannello di dobermann.",
            "__PASSWORD_LABEL__": "Nuova password" if first else "Password",
            "__BUTTON_LABEL__": "Imposta password" if first else "Accedi",
            "__PASSWORD2_FIELD__": ('<label for="password2">Conferma password</label>'
                                    '<input type="password" id="password2" name="password2" required minlength="8">')
            if first else "",
            "__ERROR__": f'<div class="error">{esc(error)}</div>' if error else "",
        }
        self._send(_fill(_tpl("login.html"), mapping), status=status)

    def _path(self):
        parsed = urllib.parse.urlparse(self.path)
        return (parsed.path.rstrip("/") or "/"), urllib.parse.parse_qs(parsed.query)

    def do_GET(self):
        if self._to_https():
            return
        path, qs = self._path()
        if path == "/logo.svg":
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
            auth.destroy_session(self._token())
            self._redirect("/login", clear=True)
        elif path in ("/", "/watchdash"):
            if path == "/":
                self._redirect("/watchdash")
            else:
                self._send(render_data_page("watchdash.html", report.build_live_report()))
        elif path == "/report":
            period = qs.get("period", ["week"])[0]
            period = period if period in REPORT_PERIODS else "week"
            self._send(render_data_page("report.html", report.build_period_report(period, qs.get("date", [None])[0])))
        elif path == "/setup":
            note = qs.get("note", [None])[0]
            self._send(render_setup(banner=note))
        elif path == "/api/samples":
            try:
                a, b = float(qs["from"][0]), float(qs["to"][0])
            except (KeyError, ValueError):
                self._json({"error": "from/to (secondi epoch) richiesti"}, 400)
                return
            self._json(report.fine_samples(a, b))
        elif path == "/api/interval":
            self._json({"interval": config.load()["interval_s"], "min": 1, "max": 60})
        elif path == "/export":
            table = qs.get("table", ["samples"])[0]
            if table not in report.EXPORT_QUERIES:
                self._send("tabella non valida (samples o hourly)", "text/plain; charset=utf-8", 400)
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
            self._send("404 - pagina inesistente: prova /watchdash, /report, /setup", "text/plain; charset=utf-8", 404)

    def do_POST(self):
        if self._to_https():
            return
        path, _ = self._path()
        if path == "/login":
            self._post_login()
            return
        if not auth.check_session(self._token()):
            self._redirect("/login")
            return
        if path == "/setup":
            self._post_setup()
        elif path == "/api/interval":
            try:
                n = int(self.headers.get("Content-Length") or 0)
                v = int(json.loads(self.rfile.read(min(n, 1024)) or b"{}").get("interval"))
            except (TypeError, ValueError):
                self._json({"error": "interval deve essere un numero"}, 400)
                return
            if not 1 <= v <= 60:
                self._json({"error": "interval fra 1 e 60"}, 400)
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
        if auth.is_configured() and not _login_throttle.attempt(client):
            self._login_page("Troppi tentativi sbagliati: riprova fra un minuto.", 429)
            return
        if not auth.is_configured():
            if len(password) < 8:
                self._login_page("La password deve avere almeno 8 caratteri.", 400)
            elif password != form.get("password2", [""])[0]:
                self._login_page("Le due password non coincidono.", 400)
            else:
                auth.set_password(password)
                self._redirect("/setup", token=auth.create_session())
            return
        if auth.verify_password(password):
            _login_throttle.succeeded(client)
            self._redirect("/watchdash", token=auth.create_session())
        else:
            time.sleep(1)   # slows down guessing
            self._login_page("Password errata.", 401)

    def _post_setup(self):
        form = self._form()
        action = form.get("action", [""])[0]
        try:
            with _config_lock:
                msg, notes, token = self._apply(action, form)
        except FormError as e:
            self._send(render_setup(banner=f"Non salvato: {e}", error=True), status=400)
            return
        if action == "test_mail":
            self._send(render_setup(banner=msg, error=msg.startswith("Invio fallito")))
            return
        if notes:
            self._send(render_setup(banner=msg, notes=notes))
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
                raise FormError("server o utente SMTP sono cambiati: riscrivi la password (quella salvata vale solo "
                                "per il server e l'utente con cui è stata salvata)")
            if action == "test_mail":
                test_secrets = {"mail": {"password": pw or stored}}
                ok, why = notify.deliver(dict(mail, enabled=True, transport="smtp"), test_secrets,
                                         "[dobermann] Mail di prova",
                                         "Se leggi questo messaggio, le notifiche di dobermann arrivano.")
                if not ok:
                    print(f"mail di prova fallita: {why}", flush=True)
                return ("Mail di prova inviata: controlla la casella (e lo spam)." if ok else
                        f"Invio fallito: {why}"), (), None
            password = pw or stored
            msg = "Notifiche salvate."
            if mail["transport"] == "relay":
                kind = relay.detect()
                if kind == "nullmailer":
                    try:
                        relay.configure_nullmailer(mail["smtp"], password)
                    except ValueError as e:
                        raise FormError(str(e)) from None
                    except OSError as e:
                        raise FormError(f"non posso scrivere la configurazione di nullmailer ({e.strerror}): "
                                        "rilancia tools/install.py come root") from None
                    msg = "Notifiche salvate, nullmailer configurato."
                elif kind is None:
                    msg = "Notifiche salvate, ma su questa macchina non c'è un server di posta: le mail non partiranno."
            raw["mail"], raw["dashboard_url"] = mail, form.get("dashboard_url", [""])[0].strip()
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
                    raise FormError("scelta di avviso non valida")
                rules[key] = {"mode": mode, "after_min": _int(form, f"after_{key}", 1, 1440)}
            freq = form.get("summary_frequency", ["weekly"])[0]
            if freq not in ("off", "daily", "weekly"):
                raise FormError("frequenza del riepilogo non valida")
            raw.setdefault("mail", {})
            raw["mail"]["rules"] = rules
            raw["mail"]["summary"] = {"frequency": freq, "weekday": _int(form, "summary_weekday", 0, 6),
                                      "hour": _int(form, "summary_hour", 0, 23),
                                      "send_if_quiet": form.get("summary_quiet", [""])[0] == "1"}
            config.save(raw)
            return "Regole degli avvisi salvate.", (), None
        if action == "test_summary":
            from dobermann import store
            con = store.connect(config.db_file())
            try:
                now = time.time()
                radio = Chain(config.load_chain(), secrets).radio_uplink()
                subject, body, _ = alerts.summary_message(con, now - 7 * 86400, now, alerts.rules(cfg["mail"]),
                                                         cfg.get("dashboard_url"), title="Riepilogo di prova",
                                                         radio_node=radio.id if radio else None)
            finally:
                con.close()
            ok, why = notify.deliver(cfg["mail"], secrets, subject, body)
            if not ok:
                print(f"riepilogo di prova fallito: {why}", flush=True)
                raise FormError(f"riepilogo non inviato: {why}")
            return "Riepilogo inviato: controlla la casella.", (), None
        if action == "save_measure":
            anchors = []
            for line in form.get("anchors", [""])[0].splitlines():
                if line.strip():
                    name, _, ip = line.partition(",")
                    if not _valid_ip(ip.strip()):
                        raise FormError(f"ancora non valida: {line.strip()!r} (serve nome,indirizzo IP)")
                    anchors.append([name.strip() or ip.strip(), ip.strip()])
            if len(anchors) < 2:
                raise FormError("servono almeno due ancore")
            dns_local = form.get("dns_local", [""])[0].strip() or None
            dns_ext = form.get("dns_external", [""])[0].strip() or None
            for d in (dns_local, dns_ext):
                if d and not _valid_ip(d):
                    raise FormError(f"DNS non valido: {d!r}")
            raw.update(interval_s=_int(form, "interval_s", 1, 60), anchors=anchors,
                       retention_days=_int(form, "retention_days", 1, 365))
            raw["dns"] = dict(cfg["dns"], local=dns_local, external=dns_ext)
            raw["thresholds"] = dict(cfg["thresholds"],
                                     high_latency_ms=_int(form, "high_latency_ms", 10, 5000),
                                     packet_loss_pct=_int(form, "packet_loss_pct", 1, 99),
                                     internet_down_pct=_int(form, "internet_down_pct", 1, 100),
                                     confirm_samples=_int(form, "confirm_samples", 1, 10))
            config.save(raw)
            return "Misure salvate.", (), None
        if action == "detect":
            extra = [x for x in re.split(r"[\s,]+", form.get("extra", [""])[0]) if x]
            for x in extra:
                if not _valid_ip(x):
                    raise FormError(f"indirizzo non valido: {x!r}")
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
            labels = ", ".join(f"{n['label']} ({n['ip']})" for n in chain["nodes"]) or "nessun apparato"
            return f"Catena rilevata e salvata: {labels}.", notes, None
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
            return "Catena salvata.", (), None
        if action == "password":
            p1, p2 = form.get("new_password", [""])[0], form.get("new_password2", [""])[0]
            if len(p1) < 8:
                raise FormError("la password deve avere almeno 8 caratteri")
            if p1 != p2:
                raise FormError("le due password non coincidono")
            auth.set_password(p1)
            return "Password cambiata.", (), auth.create_session()
        raise FormError("azione sconosciuta")


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
    except listen.ListenError as e:
        sys.exit(f"dobermann web: {e}")
    http_srv = ThreadingHTTPServer((cfg["bind"], cfg["http_port"]), Handler)
    http_srv.daemon_threads = True
    http_srv.tls_port = cfg["tls_port"] if ctx is not None else None
    if ctx is not None:
        https_srv = listen.TLSServer((cfg["bind"], cfg["tls_port"]), Handler, ctx)
        https_srv.is_tls, https_srv.tls_port = True, cfg["tls_port"]
        threading.Thread(target=https_srv.serve_forever, daemon=True, name="https").start()
        print(f"dobermann web: HTTPS su {cfg['bind']}:{cfg['tls_port']}, HTTP su :{cfg['http_port']} reindirizza", flush=True)
    else:
        print(f"dobermann web: HTTP su {cfg['bind']}:{cfg['http_port']}", flush=True)
    http_srv.serve_forever()
