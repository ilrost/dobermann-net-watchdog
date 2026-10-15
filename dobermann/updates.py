"""New versions: the daily check, and `dobermann update`.

A version is a git tag vMAJOR.MINOR.PATCH on GitHub. The check asks GitHub's API for the tags (one
request, nothing about this install is sent but the program's version in the User-Agent) and keeps
the answer in the data directory, where the panel reads it. It runs once a day from the watchdog and
can be turned off in the panel (config.json -> updates.check).

`dobermann update` (as root, on the machine dobermann runs on) downloads the newest version's archive
over HTTPS, checks it, puts it in place of the code (the previous code stays in <code>.old), reruns
tools/install.py, which never touches configuration or data, and checks that the services come back.
If they do not, it puts the previous code back. No git needed. A copy that is a git checkout is left
to git: it is told to `git pull`.
"""
import contextlib
import fcntl
import http.client
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request

from dobermann import __version__, config
from dobermann.i18n import _

REPO = "ilrost/dobermann-net-watchdog"
TAGS_URL = f"https://api.github.com/repos/{REPO}/tags?per_page=100"
ARCHIVE_URL = "https://codeload.github.com/" + REPO + "/tar.gz/refs/tags/{tag}"
COMPARE_URL = "https://github.com/" + REPO + "/compare/v{a}...{tag}"
CHECK_EVERY_S = 86400
MAX_ARCHIVE = 20 * 1024 * 1024
SERVICES = ("dobermann", "dobermann-web")
CODE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")


class UpdateError(Exception):
    pass


def version_tuple(v):
    m = _TAG.match(v if v.startswith("v") else "v" + v)
    return tuple(int(x) for x in m.groups()) if m else None


def newest(tags):
    """The highest vX.Y.Z among tag names, or None."""
    best = None
    for t in tags:
        vt = version_tuple(t)
        if vt and (best is None or vt > best[0]):
            best = (vt, t)
    return best[1] if best else None


def _get(url, timeout, limit=1024 * 1024):
    req = urllib.request.Request(url, headers={"User-Agent": f"dobermann/{__version__}",
                                               "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read(limit + 1)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise UpdateError(_("nessuna versione raggiungibile su GitHub (repository privato o senza versioni)")) from None
        raise UpdateError(_("GitHub ha risposto {v}", v=e.code)) from None
    except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
        raise UpdateError(_("GitHub non raggiungibile: {v}", v=getattr(e, "reason", e))) from None
    if len(data) > limit:
        raise UpdateError(_("risposta di GitHub troppo grande"))
    return data


def check(fetch=_get, timeout=10):
    """{'current', 'latest', 'newer', 'compare_url'}; raises UpdateError."""
    try:
        tags = [t["name"] for t in json.loads(fetch(TAGS_URL, timeout))]
    except (ValueError, TypeError, KeyError):
        raise UpdateError(_("risposta di GitHub non leggibile")) from None
    latest = newest(tags)
    newer = bool(latest) and version_tuple(latest) > version_tuple(__version__)
    return {"current": __version__, "latest": latest, "newer": newer,
            "compare_url": COMPARE_URL.format(a=__version__, tag=latest) if newer else None}


# --- the daily check, kept for the panel ----------------------------------------------------------

def status_file():
    return os.path.join(config.state_dir(), "update-check.json")


def load_status():
    try:
        with open(status_file()) as f:
            st = json.load(f)
    except (OSError, ValueError):
        return None
    # an update installed since the check: what was "newer" may be this version now
    if st.get("latest") and version_tuple(st["latest"]) and version_tuple(st["latest"]) <= version_tuple(__version__):
        st["newer"] = False
    return st


def save_status(st):
    path = status_file()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f)
    os.replace(tmp, path)


def check_and_save(now=None, fetch=_get):
    """The daily check: never raises, the outcome (or the error) is what the panel shows."""
    now = time.time() if now is None else now
    try:
        st = dict(check(fetch=fetch), ts=now, error=None)
    except UpdateError as e:
        old = load_status() or {}
        st = {"current": __version__, "latest": old.get("latest"), "newer": old.get("newer", False),
              "compare_url": old.get("compare_url"), "ts": now, "error": str(e)}
    try:
        save_status(st)
    except OSError:
        pass
    return st


def due(cfg, now, last_ts):
    return bool((cfg.get("updates") or {}).get("check", True)) and now - (last_ts or 0) >= CHECK_EVERY_S


# --- dobermann update ---------------------------------------------------------------------------

def safe_members(tar):
    """The archive's members, refused if any could land outside the target: absolute paths, '..',
    links, devices. (Python 3.11.2, Debian 12, has no tarfile extraction filter.)"""
    out = []
    for m in tar.getmembers():
        parts = m.name.split("/")
        if m.name.startswith("/") or ".." in parts or not (m.isfile() or m.isdir()):
            raise UpdateError(_("archivio non valido: {v}", v=m.name))
        out.append(m)
    return out


def unpack(data, dest):
    """Extracts a GitHub archive (one top folder) into dest; returns the folder with the code."""
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            members = safe_members(tar)
            tar.extractall(dest, members=members)
    except (tarfile.TarError, EOFError) as e:
        raise UpdateError(_("archivio non valido: {v}", v=e)) from None
    tops = {m.name.split("/")[0] for m in members}
    if len(tops) != 1:
        raise UpdateError(_("archivio non valido: {v}", v=", ".join(sorted(tops))))
    root = os.path.join(dest, tops.pop())
    for need in ("dobermann/__init__.py", "tools/install.py", "wizard.py"):
        if not os.path.isfile(os.path.join(root, need)):
            raise UpdateError(_("archivio non valido: manca {v}", v=need))
    return root


def _services_up(runner, wait_s=8):
    time.sleep(wait_s)
    return all(runner(["systemctl", "is-active", "--quiet", s + ".service"]).returncode == 0 for s in SERVICES)


def _run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


@contextlib.contextmanager
def _one_at_a_time(lock_path):
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise UpdateError(_("un altro aggiornamento è già in corso")) from None
        yield
    finally:
        os.close(fd)


def apply(tag, code_dir=CODE_DIR, fetch=_get, runner=_run, out=print, wait_s=8, lock_path=None):
    """Replaces the code with version `tag`. Returns True if the new version is running."""
    code_dir = code_dir.rstrip("/")
    if not os.path.isdir(code_dir):
        # an update cut off between the two renames leaves only <code>.old: never delete that one
        raise UpdateError(_("{v} non esiste: se c'è {v}.old, rimettilo al suo posto (mv {v}.old {v}) e riprova",
                            v=code_dir))
    if os.path.isdir(os.path.join(code_dir, ".git")):
        raise UpdateError(_("{v} è una copia git: aggiorna con `git pull`, poi `sudo python3 tools/install.py --apply`",
                            v=code_dir))
    if os.geteuid() != 0:
        raise UpdateError(_("l'aggiornamento va fatto da root"))
    with _one_at_a_time(lock_path or os.path.join(os.path.dirname(code_dir), ".dobermann-update.lock")):
        return _apply(tag, code_dir, fetch, runner, out, wait_s)


def _apply(tag, code_dir, fetch, runner, out, wait_s):
    out("  ..  " + _("scarico {v}", v=tag))
    data = fetch(ARCHIVE_URL.format(tag=tag), 60, limit=MAX_ARCHIVE)
    parent = os.path.dirname(code_dir)
    tmp = tempfile.mkdtemp(prefix=".dobermann-update-", dir=parent)
    old = code_dir + ".old"
    try:
        new_root = unpack(data, tmp)
        out("  ok  " + _("archivio verificato"))
        if os.path.exists(old):
            shutil.rmtree(old)
        os.replace(code_dir, old)
        os.replace(new_root, code_dir)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    install = [sys.executable, os.path.join(code_dir, "tools", "install.py"), "--apply", "--no-summary"]
    r = runner(install)
    if r.returncode == 0 and _services_up(runner, wait_s):
        out("  ok  " + _("versione {v} installata e in funzione", v=tag))
        return True
    out("  !!  " + _("la nuova versione non è partita: rimetto quella di prima"))
    for line in ((r.stdout or "") + (r.stderr or "")).strip().splitlines()[-5:]:
        out("      " + line)
    shutil.rmtree(code_dir, ignore_errors=True)
    os.replace(old, code_dir)
    back = runner([sys.executable, os.path.join(code_dir, "tools", "install.py"), "--apply", "--no-summary"])
    if back.returncode == 0 and _services_up(runner, wait_s):
        out("  ok  " + _("la versione di prima è di nuovo in funzione"))
    else:
        out("  !!  " + _("anche la versione di prima non riparte: controlla con `systemctl status dobermann dobermann-web`"))
    return False
