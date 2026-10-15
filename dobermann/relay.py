"""The local mail relay: a mail server on this machine that queues what dobermann writes and forwards
it to the user's mail provider, retrying on its own while the internet is down.

nullmailer: light, a queue on disk, one configuration file. Facts checked
on Debian 12, nullmailer 2.2: it does not listen on a port, mail is handed over with its
`sendmail` command, which any user may run; it re-reads /etc/nullmailer/remotes before every attempt,
so the panel can rewrite it without restarting anything; anyone may write to its trigger to make it
try at once. tools/install.py gives the remotes file to the dobermann user (group mail, 0640) so the
panel can write it.

Any other mail server that provides `sendmail` (Postfix, exim...) works too; dobermann then only hands
the mail over, and that server is configured outside dobermann.
"""
import os
import shutil
import subprocess

from dobermann.i18n import _

SENDMAIL = "/usr/sbin/sendmail"
NULLMAILER_REMOTES = "/etc/nullmailer/remotes"
NULLMAILER_TRIGGER = "/var/spool/nullmailer/trigger"


def detect():
    """'nullmailer', 'postfix', 'other' (some `sendmail`), or None (no relay on this machine)."""
    if os.path.isdir(os.path.dirname(NULLMAILER_REMOTES)) and os.path.exists(SENDMAIL):
        return "nullmailer"
    if shutil.which("postqueue"):
        return "postfix"
    if os.path.exists(SENDMAIL):
        return "other"
    return None


def remotes_line(smtp_cfg, password):
    """The line of /etc/nullmailer/remotes for this provider. The file separates fields with spaces, so a
    space in any of them would silently become a different option: refused instead."""
    host, user = (smtp_cfg.get("host") or "").strip(), (smtp_cfg.get("username") or "").strip()
    if not host:
        raise ValueError(_("serve il server SMTP del fornitore"))
    for label, value in ((_("server"), host), (_("utente"), user), (_("password"), password or "")):
        if any(c.isspace() for c in value):
            raise ValueError(_("{v}: non può contenere spazi (per le password per app di Gmail: scrivila senza spazi)",
                               v=label))
    parts = [host, "smtp", f"--port={int(smtp_cfg.get('port') or 587)}"]
    security = smtp_cfg.get("security") or "starttls"
    if security not in ("starttls", "ssl", "none"):
        # anything else would silently become "no encryption", with the password still sent
        raise ValueError(_("sicurezza SMTP non valida"))
    if security == "starttls":
        parts.append("--starttls")
    elif security == "ssl":
        parts.append("--ssl")
    if user:
        parts.append(f"--user={user}")
        if password:
            parts.append(f"--pass={password}")
    return " ".join(parts)


def configure_nullmailer(smtp_cfg, password, path=None):
    """Points nullmailer at the provider. Written in place: /etc/nullmailer belongs to root, so there is
    no temporary file to rename, and nullmailer reads the file only when it is about to send."""
    line = remotes_line(smtp_cfg, password)
    with open(path or NULLMAILER_REMOTES, "w") as f:
        f.write(line + "\n")


def submit(message_bytes, from_addr, to_addr, sendmail=SENDMAIL):
    """Hands a message to the relay's queue. True on success."""
    r = subprocess.run([sendmail, "-i", "-f", from_addr, "--", to_addr], input=message_bytes,
                       capture_output=True, timeout=20)
    if r.returncode != 0:
        print(_("relay: sendmail ha rifiutato la mail: {v}", v=(r.stderr or r.stdout).decode(errors="replace").strip()),
              flush=True)
    return r.returncode == 0


def flush(trigger=NULLMAILER_TRIGGER):
    """Asks the relay to try its queue now, instead of on its own retry timer. Called when the connection
    comes back, so the 'it is down' mail arrives right before the 'it is back' one."""
    try:
        if detect() == "postfix":
            subprocess.run(["postqueue", "-f"], capture_output=True, timeout=10)
            return
        fd = os.open(trigger, os.O_WRONLY | os.O_NONBLOCK)
        try:
            os.write(fd, b"\n")
        finally:
            os.close(fd)
    except Exception:
        pass
