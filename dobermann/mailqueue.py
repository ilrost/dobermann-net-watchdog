"""dobermann's own mail queue, in the database: every notification is written here first and removed
only once the provider has taken it.

A mail written while the internet (or the provider) is down waits here and is tried again, sooner each
time the connection comes back, for up to MAX_AGE_S. The "it is down" mail then arrives right before the
"it is back" one, in that order.

Delivery runs in a thread with its own database connection: an SMTP timeout must not delay a sample.
The state the panel shows (waiting, last delivered, last error) is in the meta table.
"""
import threading
import time

from dobermann import notify, store
from dobermann.i18n import _

MAX_AGE_S = 7 * 86400
BACKOFF_S = (60, 120, 300, 600, 1800)      # then every 30 minutes
LAST_OK_META, LAST_ERROR_META, DROPPED_META = "mail_last_ok", "mail_last_error", "mail_dropped"


def add(con, subject, body, now=None):
    now = time.time() if now is None else now
    # never before the mails already waiting: "it is back" must not overtake "it is down"
    waiting = con.execute("SELECT max(next_try) FROM outbox").fetchone()[0]
    con.execute("INSERT INTO outbox (created, subject, body, attempts, next_try) VALUES (?, ?, ?, 0, ?)",
                (now, subject, body, max(now, waiting or now)))
    con.commit()


def retry_now(con, now=None):
    """The connection is back: whatever waits is tried on the next round, not at the end of its back-off."""
    con.execute("UPDATE outbox SET next_try=? WHERE next_try>?", (time.time() if now is None else now,) * 2)
    con.commit()


def due(con, now):
    return con.execute("SELECT count(*) FROM outbox WHERE next_try<=?", (now,)).fetchone()[0] > 0


def status(con):
    """What the panel shows: {"waiting": n, "oldest": ts, "last_ok": ts, "last_error": (ts, text), "dropped": n}."""
    waiting, oldest = con.execute("SELECT count(*), min(created) FROM outbox").fetchone()
    err = store.get_meta(con, LAST_ERROR_META)
    ok = store.get_meta(con, LAST_OK_META)
    ts_err, _sep, text = (err or "").partition(" ")
    return {"waiting": waiting, "oldest": oldest, "last_ok": float(ok) if ok else None,
            "last_error": (float(ts_err), text) if err else None,
            "dropped": int(store.get_meta(con, DROPPED_META) or 0)}


def deliver_due(con, mail_cfg, secrets, now=None, deliver=None):
    """Tries every mail whose turn has come, oldest first, stopping at the first failure (the next would
    most likely fail the same way, and the order "down" before "back" must hold). Returns how many left."""
    now = time.time() if now is None else now
    deliver = deliver or notify.deliver
    expired = con.execute("SELECT count(*) FROM outbox WHERE created<?", (now - MAX_AGE_S,)).fetchone()[0]
    if expired:
        con.execute("DELETE FROM outbox WHERE created<?", (now - MAX_AGE_S,))
        store.set_meta(con, DROPPED_META, int(store.get_meta(con, DROPPED_META) or 0) + expired)
        print(_("{v} mail non consegnate da più di 7 giorni: scartate", v=expired), flush=True)
    con.commit()        # no write may stay open across an SMTP call: the engine writes on its own connection
    for mid, subject, body, attempts in con.execute(
            "SELECT id, subject, body, attempts FROM outbox WHERE next_try<=? ORDER BY id", (now,)).fetchall():
        try:
            ok, why = deliver(mail_cfg, secrets, subject, body)
        except Exception as e:      # never let one mail stop the queue
            ok, why = False, f"{type(e).__name__}: {e}"
        if ok:
            con.execute("DELETE FROM outbox WHERE id=?", (mid,))
            store.set_meta(con, LAST_OK_META, now)
            con.commit()
            continue
        wait = BACKOFF_S[min(attempts, len(BACKOFF_S) - 1)]
        con.execute("UPDATE outbox SET attempts=attempts+1, next_try=? WHERE id=?", (now + wait, mid))
        # only after this one: the rest keeps its turn, so the order holds
        con.execute("UPDATE outbox SET next_try=max(next_try, ?) WHERE id>?", (now + wait, mid))
        store.set_meta(con, LAST_ERROR_META, f"{now} {why}")
        con.commit()
        print(_("invio mail fallito, nuovo tentativo fra {v} minuti: {why}", v=wait // 60, why=why), flush=True)
        break
    con.commit()
    return con.execute("SELECT count(*) FROM outbox").fetchone()[0]


class Sender:
    """One delivery round at a time, in a thread with its own connection to the database."""

    def __init__(self, db_path):
        self.db_path = db_path
        self._lock = threading.Lock()

    def kick(self, mail_cfg, secrets, wait=False):
        if not self._lock.acquire(blocking=False):
            return      # a round is already running: it will see the new mail
        try:
            t = threading.Thread(target=self._round, args=(mail_cfg, secrets), daemon=True, name="mail")
            t.start()
        except BaseException:
            self._lock.release()        # no round will run to release it
            raise
        if wait:
            t.join()

    def _round(self, mail_cfg, secrets):
        try:
            con = store.connect(self.db_path)
            try:
                deliver_due(con, mail_cfg, secrets)
            finally:
                con.close()
        except Exception as e:
            print(_("invio mail fallito: {v}", v=f"{type(e).__name__}: {e}"), flush=True)
        finally:
            self._lock.release()
