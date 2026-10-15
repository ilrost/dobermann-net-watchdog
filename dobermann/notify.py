"""Email: the only notification channel.

Two transports, chosen in config.json -> mail.transport:
  relay  a mail server on this machine (nullmailer in the CT, see relay.py) takes the message with its
         `sendmail` command, queues it and forwards it to the provider. Recommended: a mail written
         while the internet is down waits in its queue instead of being lost.
  smtp   dobermann talks to the provider's server itself (host, port, STARTTLS/SSL, login).
The provider's details are in config.json -> mail.smtp for both; the password in secrets.json ->
mail.password, never in config.json.

send() never raises and has a short timeout: reporting an outage must not stall the measurements.
The message texts are built in narrative.py (incidents) and alerts.py (the summary).
"""
import smtplib
import socket
import ssl
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid

from dobermann import relay
from dobermann.i18n import _


def sender(mail_cfg):
    """The From address: the configured one; otherwise the SMTP user when it is an address, since most
    providers (Gmail among them) only let an account send as itself; otherwise the recipient."""
    user = ((mail_cfg.get("smtp") or {}).get("username") or "").strip()
    return mail_cfg.get("from") or (user if "@" in user else "") or mail_cfg.get("to")


def explain(stage, exc, host=""):
    """What went wrong, in words the user can act on. Never contains the password: only the stage, the
    exception type and the server's own reply."""
    gmail = "gmail" in (host or "").lower() or "google" in (host or "").lower()
    app_pw = (" " + _("Per Gmail serve una password per app (16 lettere, da myaccount.google.com/apppasswords), "
                      "non la password con cui entri in Gmail.") if gmail else "")
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        reply = exc.smtp_error.decode(errors="replace") if isinstance(exc.smtp_error, bytes) else str(exc.smtp_error)
        return _("il server ha rifiutato utente o password ({v}).", v=f"{exc.smtp_code} {reply.strip()[:160]}") + app_pw
    if stage == "login" and isinstance(exc, (smtplib.SMTPServerDisconnected, ConnectionError)):
        return _("il server ha chiuso la connessione al momento del login: di solito vuol dire password non accettata.") + app_pw
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return _("il server ha rifiutato il destinatario: controlla l'indirizzo.")
    if isinstance(exc, smtplib.SMTPSenderRefused):
        return _("il server ha rifiutato il mittente {v}: di solito va lasciato vuoto o messo uguale all'utente SMTP.",
                 v=repr(exc.sender))
    if isinstance(exc, smtplib.SMTPNotSupportedError) and stage == "starttls":
        return _("il server non offre STARTTLS su questa porta: prova SSL/TLS (porta 465).")
    if isinstance(exc, socket.gaierror):
        return _("server {v} non trovato: controlla il nome (o la connessione a internet).", v=repr(host))
    if isinstance(exc, ssl.SSLError):
        return _("errore nella cifratura: probabilmente sicurezza e porta non corrispondono (STARTTLS va con 587, SSL/TLS con 465).")
    if isinstance(exc, (ConnectionRefusedError, TimeoutError, socket.timeout)):
        return _("il server non risponde su quella porta: controlla porta e sicurezza (o la connessione a internet).")
    if stage == "connect" and isinstance(exc, smtplib.SMTPServerDisconnected):
        return _("il server ha chiuso subito la connessione: probabilmente sicurezza e porta non corrispondono.")
    return f"{type(exc).__name__}: {exc}"


def deliver(mail_cfg, secrets, subject, body):
    """(True, None) if the message was handed over (to the relay or the provider), else (False, why)."""
    to_addr, from_addr = mail_cfg.get("to"), sender(mail_cfg)
    if not to_addr:
        return False, _("manca il destinatario.")
    if any(c in (v or "") for v in (subject, from_addr, to_addr) for c in "\r\n"):
        # a line break in a header would add headers of its own (a hidden Bcc, for instance)
        return False, _("destinatario, mittente o oggetto contengono un a capo.")
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"], msg["From"], msg["To"] = subject, from_addr, to_addr
    msg["Date"], msg["Message-ID"] = formatdate(localtime=True), make_msgid(domain="dobermann.local")
    if mail_cfg.get("transport") != "smtp":
        try:
            ok = relay.submit(msg.as_bytes(), from_addr, to_addr)
        except Exception as e:
            return False, _("il server di posta locale non ha accettato la mail ({v}).", v=f"{type(e).__name__}: {e}")
        return (True, None) if ok else (False, _("il server di posta locale non ha accettato la mail (dettagli nel log)."))
    s_cfg = mail_cfg.get("smtp") or {}
    host, stage = s_cfg.get("host"), "connect"
    try:
        security = s_cfg.get("security", "starttls")
        cls = smtplib.SMTP_SSL if security == "ssl" else smtplib.SMTP
        kwargs = {"context": ssl.create_default_context()} if security == "ssl" else {}
        with cls(host, int(s_cfg.get("port") or 587), timeout=10, **kwargs) as s:
            if security == "starttls":
                stage = "starttls"
                s.starttls(context=ssl.create_default_context())
            password = ((secrets or {}).get("mail") or {}).get("password")
            if s_cfg.get("username") and password:
                stage = "login"
                s.login(s_cfg["username"], password)
            stage = "send"
            s.sendmail(from_addr, [to_addr], msg.as_string())
            stage = "sent"
        return True, None
    except Exception as e:
        if stage == "sent":
            return True, None      # accepted by the server; only the goodbye (QUIT) failed
        return False, explain(stage, e, host)


def send(mail_cfg, secrets, subject, body):
    """True if the message was handed over. Never raises: the reason for a failure goes to the log."""
    try:
        ok, why = deliver(mail_cfg, secrets, subject, body)
    except Exception as e:
        ok, why = False, f"{type(e).__name__}: {e}"
    if not ok:
        print(_("invio mail fallito: {v}", v=why), flush=True)
    return ok


def flush_relay_queue():
    relay.flush()
