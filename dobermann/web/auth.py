"""One admin password for the panel (PBKDF2, stdlib) and file-backed sessions, so restarting the panel
does not log anyone out. Lost the password? Run `dobermann reset-password` on the machine (or delete
admin.json in the configuration directory and choose a new one from the browser, with the code that
command prints)."""
import contextlib
import fcntl
import hashlib
import hmac
import json
import os
import secrets
import tempfile
import time

from dobermann import config

SESSION_COOKIE = "dobermann_session"
SESSION_TTL = 7 * 86400
PBKDF2_ITERATIONS = 200_000


def _file():
    return os.path.join(config.config_dir(), "admin.json")


def _load():
    try:
        with open(_file()) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"salt": None, "hash": None, "sessions": {}}


def _save(data):
    path = _file()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".admin-")   # 0600, unique per writer
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        if os.geteuid() == 0:
            st = os.stat(os.path.dirname(path))
            os.chown(tmp, st.st_uid, st.st_gid)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


@contextlib.contextmanager
def _locked():
    """admin.json is read, changed and written back by concurrent panel requests and by the
    `dobermann reset-password` command: every change happens under this lock, so none is lost (a
    session overwriting a new password, two logins dropping each other's session)."""
    path = _file() + ".lock"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    if os.geteuid() == 0:     # made by root (installer, reset-password): the panel must still open it
        st = os.stat(os.path.dirname(path))
        with contextlib.suppress(OSError):
            os.fchown(fd, st.st_uid, st.st_gid)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _hash(password, salt_hex):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), PBKDF2_ITERATIONS).hex()


def is_configured():
    return bool(_load().get("hash"))


def set_password(password):
    salt = secrets.token_hex(16)
    digest = _hash(password, salt)          # slow on purpose: outside the lock
    with _locked():
        data = _load()
        data["salt"], data["hash"] = salt, digest
        data["sessions"] = {}   # a new password logs every other browser out
        _save(data)


def verify_password(password):
    data = _load()
    return bool(data.get("hash")) and hmac.compare_digest(_hash(password, data["salt"]), data["hash"])


# --- the one-time setup code ----------------------------------------------------------------------
# When the installer did not set the password, it leaves a short code in the configuration directory
# and prints it: the first visit must give it to choose the password, so that someone else on the
# home network who opens the panel first cannot choose it instead.

SETUP_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # no 0/O, 1/I: it is read off a screen


def _code_file():
    return os.path.join(config.config_dir(), "setup-code")


def _normal_code(code):
    return "".join(c for c in (code or "").upper() if c.isalnum())


def new_setup_code():
    """Writes a fresh code (0600) and returns it as XXXX-XXXX."""
    raw = "".join(secrets.choice(SETUP_CODE_ALPHABET) for _i in range(8))
    path = _code_file()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(raw + "\n")
    os.replace(path + ".tmp", path)
    return f"{raw[:4]}-{raw[4:]}"


def setup_code_required():
    return not is_configured() and os.path.exists(_code_file())


def check_setup_code(code):
    try:
        with open(_code_file()) as f:
            want = _normal_code(f.read())
    except OSError:
        return False
    return bool(want) and hmac.compare_digest(_normal_code(code), want)


def clear_setup_code():
    try:
        os.unlink(_code_file())
    except FileNotFoundError:
        pass


def create_session():
    token = secrets.token_urlsafe(32)
    with _locked():
        data = _load()
        now = time.time()
        sessions = {t: exp for t, exp in data.get("sessions", {}).items() if exp > now}   # drop expired ones
        sessions[token] = now + SESSION_TTL
        data["sessions"] = sessions
        _save(data)
    return token


def check_session(token):
    if not token:
        return False
    exp = _load().get("sessions", {}).get(token)
    return bool(exp and exp > time.time())


def session_started(token):
    """When this session was opened (epoch), or None."""
    exp = _load().get("sessions", {}).get(token) if token else None
    return exp - SESSION_TTL if exp else None


def destroy_all_sessions():
    with _locked():
        data = _load()
        data["sessions"] = {}
        _save(data)


def destroy_session(token):
    with _locked():
        data = _load()
        if data.get("sessions", {}).pop(token, None) is not None:
            _save(data)
