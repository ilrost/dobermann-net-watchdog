"""One admin password for the panel (PBKDF2, stdlib) and file-backed sessions, so restarting the panel
does not log anyone out. Lost the password? Delete admin.json in
the configuration directory and choose a new one from the browser."""
import hashlib
import hmac
import json
import os
import secrets
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
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def _hash(password, salt_hex):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), PBKDF2_ITERATIONS).hex()


def is_configured():
    return bool(_load().get("hash"))


def set_password(password):
    data = _load()
    data["salt"] = secrets.token_hex(16)
    data["hash"] = _hash(password, data["salt"])
    data["sessions"] = {}   # a new password logs every other browser out
    _save(data)


def verify_password(password):
    data = _load()
    return bool(data.get("hash")) and hmac.compare_digest(_hash(password, data["salt"]), data["hash"])


def create_session():
    data = _load()
    now = time.time()
    sessions = {t: exp for t, exp in data.get("sessions", {}).items() if exp > now}   # drop expired ones
    token = secrets.token_urlsafe(32)
    sessions[token] = now + SESSION_TTL
    data["sessions"] = sessions
    _save(data)
    return token


def check_session(token):
    if not token:
        return False
    exp = _load().get("sessions", {}).get(token)
    return bool(exp and exp > time.time())


def destroy_session(token):
    data = _load()
    if data.get("sessions", {}).pop(token, None) is not None:
        _save(data)
