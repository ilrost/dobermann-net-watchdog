"""Where dobermann keeps its configuration and data, and what is in the configuration.

Two directories, overridable for tests or non-standard installs:
  DOBERMANN_CONFIG_DIR  (default /etc/dobermann)      config.json, chain.json, secrets.json
  DOBERMANN_STATE_DIR   (default /var/lib/dobermann)  dobermann.db

Secrets (mail and device passwords) live only in secrets.json, which save_secrets() writes with
mode 0600, never in config.json or chain.json. Nothing here has a default that belongs to one
particular network: a fresh install knows only public anchors and its own system resolver.
"""
import copy
import json
import os

DEFAULTS = {
    "language": "en",              # the panel and the mails; see i18n.LANGUAGES
    "interval_s": 5,
    # public targets everyone can ping; at least two must fail before "internet is down"
    "anchors": [["cloudflare", "1.1.1.1"], ["google", "8.8.8.8"], ["quad9", "9.9.9.9"]],
    "dns": {
        "local": None,             # None: the first nameserver in /etc/resolv.conf
        "external": "1.1.1.1",
        "test_domain": "www.google.com",
    },
    "thresholds": {
        "high_latency_ms": 150,
        "packet_loss_pct": 34,     # on the anchor average: one anchor of three fully lost
        "internet_down_pct": 66,   # on the anchor average: two anchors of three fully lost
        "confirm_samples": 2,      # consecutive samples before a noisy cause opens an incident
    },
    "retention_days": 30,
    "mail": {
        "enabled": False,
        "to": "",
        "from": "",
        # "relay": a local mail server (nullmailer) queues and forwards to the provider;
        # "smtp": dobermann talks to the provider's server itself. "smtp" below is the provider, for both.
        "transport": "relay",
        "smtp": {"host": "", "port": 587, "security": "starttls", "username": ""},
        "cooldown_s": 300,
    },
    "dashboard_url": "",
}

# A chain with nothing but the gateway: what an install knows before the configurator ran.
DEFAULT_CHAIN = {"version": 1, "nodes": []}


def config_dir():
    return os.environ.get("DOBERMANN_CONFIG_DIR", "/etc/dobermann")


def state_dir():
    return os.environ.get("DOBERMANN_STATE_DIR", "/var/lib/dobermann")


def config_file():
    return os.path.join(config_dir(), "config.json")


def chain_file():
    return os.path.join(config_dir(), "chain.json")


def secrets_file():
    return os.path.join(config_dir(), "secrets.json")


def db_file():
    return os.path.join(state_dir(), "dobermann.db")


def _merge(base, override):
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def _read_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return copy.deepcopy(default)


def load():
    """The effective configuration: defaults with config.json on top. A malformed file raises;
    running on silently-ignored settings would be worse than refusing to start."""
    return _merge(DEFAULTS, _read_json(config_file(), {}))


def load_chain():
    return _read_json(chain_file(), DEFAULT_CHAIN)


def load_secrets():
    return _read_json(secrets_file(), {})


def _atomic_write(path, data, mode=0o644):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def save(cfg):
    _atomic_write(config_file(), cfg)


def save_chain(chain):
    _atomic_write(chain_file(), chain)


def save_secrets(secrets):
    _atomic_write(secrets_file(), secrets, mode=0o600)


def mtimes():
    """Modification times of the three files, to notice an edit without restarting."""
    out = []
    for p in (config_file(), chain_file(), secrets_file()):
        try:
            out.append(os.stat(p).st_mtime_ns)
        except OSError:
            out.append(None)
    return tuple(out)


def system_resolver():
    """The first IPv4 nameserver in /etc/resolv.conf, or None."""
    try:
        with open("/etc/resolv.conf") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2 and parts[0] == "nameserver" and ":" not in parts[1]:
                    return parts[1]
    except OSError:
        pass
    return None
