"""The speed test, only when the user asks for it from the panel: never on a schedule, because on a
mobile line every test spends data and, while it runs, fills the line for everyone at home.

Download: a GET of `down_bytes` from the download URL; upload: a POST of a fifth of that. The download
stops at TIME_LIMIT_S whatever it has moved, and the speed is what moved divided by the time it took.
Latency is not measured here: the engine pings every few seconds anyway, so the panel shows its pings
of the minute before the test (idle) next to those during it (under load: how much a full line slows
everything else down). The default server is Cloudflare's public speed test
(it sees this line's public IP, like any web site); config.json -> speedtest sets another one.

While a test runs the meta key RUNNING_META holds its end time: the engine does not turn the latency
and loss the test itself causes into incidents (engine.py).
"""
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from dobermann import store
from dobermann.i18n import _

DEFAULTS = {"download_url": "https://speed.cloudflare.com/__down?bytes={bytes}",
            "upload_url": "https://speed.cloudflare.com/__up"}
SIZES_MB = (10, 25, 100)          # download; the upload is a fifth of it
TIME_LIMIT_S = 15
CHUNK = 64 * 1024
RUNNING_META = "speedtest_until"
_lock = threading.Lock()


class Busy(Exception):
    pass


def urls(cfg):
    s = cfg.get("speedtest") or {}
    return (s.get("download_url") or DEFAULTS["download_url"], s.get("upload_url") or DEFAULTS["upload_url"])


def _request(url, data=None, timeout=10):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": "dobermann"},
                                 method="POST" if data is not None else "GET")
    return urllib.request.urlopen(req, timeout=timeout)


def download(url, nbytes, opener=_request, clock=time.monotonic, limit_s=TIME_LIMIT_S):
    """(bytes moved, seconds)."""
    got = 0
    t0 = clock()
    with opener(url.format(bytes=nbytes), timeout=limit_s) as r:
        while got < nbytes and clock() - t0 < limit_s:
            chunk = r.read(CHUNK)
            if not chunk:
                break
            got += len(chunk)
    return got, clock() - t0


def upload(url, nbytes, opener=_request, clock=time.monotonic, limit_s=TIME_LIMIT_S):
    """(bytes moved, seconds). The body is sent whole: the time limit is the request's timeout."""
    t0 = clock()
    with opener(url, data=bytes(nbytes), timeout=limit_s) as r:
        r.read()
    return nbytes, clock() - t0


def run(con, cfg, down_mb, opener=None, clock=time.monotonic, now=time.time):
    """One test, recorded in the database. Busy if another one is running."""
    opener = opener or _request
    if down_mb not in SIZES_MB:
        raise ValueError(_("dimensione del test non valida"))
    if not _lock.acquire(blocking=False):
        raise Busy(_("un test di velocità è già in corso"))
    try:
        down_url, up_url = urls(cfg)
        store.set_meta(con, RUNNING_META, now() + 3 * TIME_LIMIT_S + 30)
        con.commit()
        res = {"ts": now(), "end_ts": None, "down_bps": None, "up_bps": None, "bytes": 0,
               "server": urllib.parse.urlparse(down_url).hostname, "error": None}
        try:
            n, s = download(down_url, down_mb * 1_000_000, opener=opener, clock=clock)
            res["down_bps"], res["bytes"] = int(n * 8 / s) if s > 0 else None, n
            n, s = upload(up_url, down_mb * 200_000, opener=opener, clock=clock)
            res["up_bps"], res["bytes"] = int(n * 8 / s) if s > 0 else None, res["bytes"] + n
        except (urllib.error.URLError, OSError, ValueError) as e:
            res["error"] = f"{type(e).__name__}: {getattr(e, 'reason', e)}"
        res["end_ts"] = now()
        con.execute("INSERT INTO speedtests (ts, end_ts, down_bps, up_bps, bytes, server, error) VALUES (?,?,?,?,?,?,?)",
                    (res["ts"], res["end_ts"], res["down_bps"], res["up_bps"], res["bytes"], res["server"], res["error"]))
        return res
    finally:
        store.set_meta(con, RUNNING_META, 0)
        con.commit()
        _lock.release()


def running(con, now):
    v = store.get_meta(con, RUNNING_META)
    try:
        return v is not None and float(v) > now
    except ValueError:
        return False


def _median_rtt(con, since, until):
    rtts = sorted(r[0] for r in con.execute("SELECT internet_rtt FROM samples WHERE ts >= ? AND ts < ? AND "
                                            "internet_rtt IS NOT NULL", (since, until)))
    return rtts[len(rtts) // 2] if rtts else None


def history(con, limit=20):
    """The latest tests, newest first, each with the engine's ping before (idle) and during (loaded)."""
    out = []
    for r in con.execute("SELECT ts, end_ts, down_bps, up_bps, bytes, server, error FROM speedtests "
                         "ORDER BY ts DESC LIMIT ?", (limit,)).fetchall():
        t = dict(zip(("ts", "end_ts", "down_bps", "up_bps", "bytes", "server", "error"), r))
        t["idle_ms"] = _median_rtt(con, t["ts"] - 60, t["ts"])
        t["loaded_ms"] = _median_rtt(con, t["ts"], t["end_ts"] or t["ts"])
        out.append(t)
    return out
