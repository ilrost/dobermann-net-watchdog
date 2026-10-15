#!/usr/bin/env python3
"""Install dobermann on this Linux machine (or update it), from a copy of this repository.

Most people run the wizard (python3 wizard.py), which asks the questions and then runs this. Directly:

    python3 tools/install.py              # DRY RUN: checks the machine, shows the plan, changes nothing
    python3 tools/install.py --apply      # do it (as root)

The code stays where this copy is (/opt/dobermann in the Proxmox CT, see tools/proxmox_ct.py). What
--apply does:
  1. preflight: Python 3.11+, systemd and `ping` are required; see tools/deps.py for the full list;
  2. creates the system user `dobermann`, which the services run as;
  3. creates the configuration directory (0750) and the data directory (0750), owned by that user;
  4. if nullmailer is installed (the local mail relay), gives its provider file /etc/nullmailer/remotes to
     that user (group mail, 0640), so that the panel can write the provider's details into it;
  5. unless --http, creates a self-signed HTTPS certificate for the panel if there is none
     (tools/make_cert.py), so that the panel password and the mail password never cross the LAN in clear;
  6. with --name (e.g. dobermann.local) and avahi installed, announces that name on the home network;
  7. records the choices: language, the panel's address for the links in the mails, where it listens;
  8. the panel password: from --password-stdin, or else a one-time code the first visit must give;
  9. writes the systemd units and the `dobermann` command (/usr/local/bin/dobermann), then enables
     and (re)starts the services.

Running it again is how an update is installed: the units and the command are rewritten, the services
restarted, and the configuration and the data are never touched unless an option asks for it (an
existing certificate and an existing password are kept).

Stdlib only.
"""
import argparse
import ipaddress
import json
import os
import shutil
import socket
import subprocess
import sys

CODE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, CODE_DIR)
import deps  # noqa: E402
import make_cert  # noqa: E402
from dobermann import i18n  # noqa: E402
from dobermann.i18n import _  # noqa: E402

UNIT_DIR = os.path.join(CODE_DIR, "packaging", "systemd")
SERVICES = ("dobermann", "dobermann-web")
MDNS_SERVICE = "dobermann-mdns"
USER = "dobermann"
MIN_PYTHON = (3, 11)
AVAHI_CONF = "/etc/avahi/avahi-daemon.conf"


class InstallError(Exception):
    pass


def preflight(https=True, exists=os.path.exists, which=shutil.which):
    """([errors], [warnings]) about this machine. Pure reads."""
    errors, warnings = [], []
    if sys.version_info < MIN_PYTHON:
        errors.append(_("serve Python {v} o più recente, questo è {have}",
                        v=f"{MIN_PYTHON[0]}.{MIN_PYTHON[1]}", have=sys.version.split()[0]))
    if not deps.has_systemd(exists=exists):
        errors.append(_("manca systemd: dobermann gira come servizio systemd"))
    if not deps.present("ping", which=which, exists=exists):
        errors.append(_("manca `ping`: senza, ogni apparato risulterebbe irraggiungibile"))
    if not deps.present("ip", which=which, exists=exists):
        errors.append(_("manca `ip` (iproute2): serve per trovare il gateway e gli indirizzi"))
    if https and not deps.present("openssl", which=which, exists=exists):
        warnings.append(_("manca `openssl`: serve per creare il certificato HTTPS, senza il pannello resta in HTTP"))
    if not deps.present("nullmailer", which=which, exists=exists):
        warnings.append(_("manca nullmailer: le mail partiranno via SMTP diretto, senza coda durante i guasti"))
    return errors, warnings


def render_unit(text, code_dir, config_dir, state_dir, extra=None):
    text = text.replace("@CODE_DIR@", code_dir).replace("@CONFIG_DIR@", config_dir).replace("@STATE_DIR@", state_dir)
    for k, v in (extra or {}).items():
        text = text.replace(k, v)
    return text


def command_text(code_dir, config_dir, state_dir):
    return ("#!/bin/sh\n# Written by tools/install.py: the dobermann command line with this install's directories.\n"
            f"export DOBERMANN_CONFIG_DIR={config_dir} DOBERMANN_STATE_DIR={state_dir}\n"
            f"cd {code_dir} && exec /usr/bin/python3 -m dobermann \"$@\"\n")


def web_env_text(old, bind):
    """web.env with DOBERMANN_BIND set to `bind` (None: removed), every other line kept."""
    lines = [ln for ln in (old or "").splitlines() if not ln.strip().startswith("DOBERMANN_BIND=")]
    if bind:
        lines.append(f"DOBERMANN_BIND={bind}")
    return "\n".join(lines) + "\n" if lines else ""


def panel_url(https, host):
    return f"{'https' if https else 'http'}://{host}/watchdash"


def mdns_label(name):
    return (name or "").split(".")[0].lower()


def valid_name(name):
    return bool(make_cert._NAME_OK.match(name or "")) and name.lower().endswith(".local")


def _run(cmd):
    subprocess.run(cmd, check=True)


def _chown_like_dir(path, d):
    st = os.stat(d)
    try:
        os.chown(path, st.st_uid, st.st_gid)
    except PermissionError:
        pass


def _read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return None


def _write(path, text, mode=0o644):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(path, mode)


def _in_container():
    r = subprocess.run(["systemd-detect-virt", "--container"], capture_output=True, text=True)
    return r.returncode == 0 and r.stdout.strip() not in ("", "none")


def avahi_conf_for_container(text):
    """avahi-daemon cannot set its process limit inside an unprivileged container and refuses to start:
    the `rlimit-nproc` line is commented out. Returns the new text, or None when nothing changes."""
    out, changed = [], False
    for ln in text.splitlines():
        if ln.strip().startswith("rlimit-nproc"):
            out.append("#" + ln + "   # commented out by dobermann: not allowed in a container")
            changed = True
        else:
            out.append(ln)
    return "\n".join(out) + "\n" if changed else None


def update_config(cfg_dir, language=None, url=None, platform=None, install=None):
    """Merges the installer's choices into config.json, leaving everything else as it is."""
    path = os.path.join(cfg_dir, "config.json")
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    except FileNotFoundError:
        cfg = {}
    changed = False
    for key, value in (("language", language), ("dashboard_url", url), ("platform", platform), ("install", install)):
        if value is not None and cfg.get(key) != value:
            cfg[key] = value
            changed = True
    if changed:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
        _chown_like_dir(path, cfg_dir)
    return changed


def saved_config(cfg_dir):
    try:
        with open(os.path.join(cfg_dir, "config.json"), encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def saved_choices(cfg_dir):
    return saved_config(cfg_dir).get("install") or {}


def resolve_choices(args, saved):
    """HTTPS, local name and listen address: what the options say, else what the last install chose,
    else the defaults. An update with no options therefore keeps the install as it was."""
    https = args.https if args.https is not None else saved.get("https", True)
    name = None if args.no_name else (args.name if args.name is not None else saved.get("name"))
    bind = args.bind if args.bind is not None else saved.get("bind")
    return https, name or None, bind or None


def run(args, out=print, runner=_run, password=None, summary=True):
    cfg, st = os.path.abspath(args.config_dir), os.path.abspath(args.state_dir)
    https, args.name, args.bind = resolve_choices(args, saved_choices(cfg))
    if args.language:
        i18n.set_language(args.language)
    if args.bind:
        try:
            ipaddress.ip_address(args.bind)
        except ValueError:
            raise InstallError(_("--bind {v!r}: serve un indirizzo IP", v=args.bind)) from None
    if args.name and not valid_name(args.name):
        raise InstallError(_("--name {v!r}: serve un nome che finisce in .local, es. dobermann.local", v=args.name))
    errors, warnings = preflight(https=https)
    out(_("codice      : {v}  (resta dov'è)", v=CODE_DIR))
    out(_("config      : {v}", v=cfg))
    out(_("dati        : {v}", v=st))
    extra_mdns = bool(args.name) and mdns_label(args.name) != socket.gethostname().split(".")[0].lower()
    out(_("servizi     : {v}", v=", ".join(SERVICES) + (f", {MDNS_SERVICE}" if extra_mdns else "")))
    out(_("comando     : {v}", v=args.bin_path))
    out(_("pannello    : {v}", v=("HTTPS" if https else "HTTP") + (f", {args.bind}" if args.bind else "")
                                    + (f", {args.name}" if args.name else "")))
    for w in warnings:
        out(_("ATTENZIONE: {v}", v=w))
    for e in errors:
        out(_("MANCA: {v}", v=e))
    if errors:
        raise InstallError(_("sistema i requisiti mancanti qui sopra, poi rilancia"))
    if not args.apply:
        out("\n" + _("PROVA A VUOTO: nulla è stato cambiato. Rilancia con --apply per installare."))
        return 0
    if os.geteuid() != 0:
        raise InstallError(_("l'installazione va fatta da root"))

    try:
        import pwd
        pwd.getpwnam(USER)
    except KeyError:
        runner(["useradd", "--system", "--home-dir", st, "--no-create-home", "--shell", "/usr/sbin/nologin", USER])
        out("  ok  " + _("utente {v} creato", v=USER))
    for d in (cfg, st):
        os.makedirs(d, mode=0o750, exist_ok=True)
        os.chmod(d, 0o750)
        runner(["chown", "-R", f"{USER}:{USER}", d])
    out(f"  ok  {cfg}, {st}")
    if os.path.exists(args.nullmailer_remotes):
        runner(["chown", f"{USER}:mail", args.nullmailer_remotes])
        os.chmod(args.nullmailer_remotes, 0o640)
        out("  ok  " + _("{v} scrivibile dal pannello", v=args.nullmailer_remotes))

    cert, key = os.path.join(cfg, "tls.crt"), os.path.join(cfg, "tls.key")
    fingerprint = None
    if not https:
        # the panel speaks HTTPS whenever both files exist: HTTP means putting them aside
        for p in (cert, key):
            if os.path.exists(p):
                os.replace(p, p + ".off")
                out("  ok  " + _("{v} messo da parte ({v}.off): pannello in HTTP", v=p))
    if https:
        for p in (cert, key):
            if not os.path.exists(p) and os.path.exists(p + ".off"):
                os.replace(p + ".off", p)
        if os.path.exists(cert) or os.path.exists(key):
            out("  ok  " + _("certificato HTTPS già presente, lasciato com'è"))
            if os.path.exists(cert):
                fingerprint = make_cert.fingerprint(cert)
        elif not shutil.which("openssl"):
            out("  !!  " + _("openssl mancante: pannello solo in HTTP"))
            https = False
        else:
            names = make_cert.detect_names() + ([args.name] if args.name else [])
            sans = make_cert.san_entries(names)
            make_cert.generate(cert, key, sans)
            fingerprint = make_cert.fingerprint(cert)
            out("  ok  " + _("certificato HTTPS per {names}", names=", ".join(sans)))

    # the panel's address: where it listens (web.env) and the link the mails carry (config.json)
    env_path = os.path.join(cfg, "web.env")
    old_env = _read(env_path)
    new_env = web_env_text(old_env, args.bind)
    if new_env != (old_env or ""):
        _write(env_path, new_env, 0o644)
        _chown_like_dir(env_path, cfg)
    # the link changes only when an address is given; an update keeps the one in use (or set in the panel)
    url = panel_url(https, args.address) if args.address else None
    if url is None and not (saved_config(cfg).get("dashboard_url")) and (args.name or args.bind):
        url = panel_url(https, args.name or args.bind)
    platform = ({"kind": "proxmox-lxc", "ctid": args.platform_ct, "bridge": args.platform_bridge or "vmbr0"}
                if args.platform_ct else None)
    choices = {"https": https, "name": args.name, "bind": args.bind}
    if update_config(cfg, language=args.language, url=url, platform=platform, install=choices):
        out("  ok  " + _("scelte salvate in {v}", v=os.path.join(cfg, "config.json")))

    # the panel password, or the code that protects the first visit
    os.environ["DOBERMANN_CONFIG_DIR"] = cfg
    from dobermann.web import auth
    setup_code = None
    if password:
        auth.set_password(password)
        auth.clear_setup_code()
        _chown_like_dir(os.path.join(cfg, "admin.json"), cfg)
        out("  ok  " + _("password del pannello impostata"))
    elif not auth.is_configured():
        setup_code = auth.new_setup_code()
        _chown_like_dir(os.path.join(cfg, "setup-code"), cfg)

    units = list(SERVICES)
    mdns_needed = False
    if args.name:
        if not deps.present("avahi"):
            out("  !!  " + _("avahi mancante: il nome {v} non verrà annunciato", v=args.name))
        else:
            if os.path.exists(AVAHI_CONF) and _in_container():
                new = avahi_conf_for_container(_read(AVAHI_CONF) or "")
                if new:
                    _write(AVAHI_CONF, new)
            # avahi already announces <hostname>.local; a second announcement of the same name would clash
            mdns_needed = mdns_label(args.name) != socket.gethostname().split(".")[0].lower()
            runner(["systemctl", "enable", "avahi-daemon.service"])
            runner(["systemctl", "restart", "avahi-daemon.service"])
            if mdns_needed:
                units.append(MDNS_SERVICE)
            out("  ok  " + _("nome {v} annunciato sulla rete di casa", v=args.name))
    for svc in units:
        with open(os.path.join(UNIT_DIR, svc + ".service"), encoding="utf-8") as f:
            text = render_unit(f.read(), CODE_DIR, cfg, st, {"@MDNS_NAME@": args.name or ""})
        with open(os.path.join(args.systemd_dir, svc + ".service"), "w", encoding="utf-8") as f:
            f.write(text)
    if not mdns_needed and os.path.exists(os.path.join(args.systemd_dir, MDNS_SERVICE + ".service")):
        runner(["systemctl", "disable", "--now", MDNS_SERVICE + ".service"])
        os.unlink(os.path.join(args.systemd_dir, MDNS_SERVICE + ".service"))
    with open(args.bin_path, "w", encoding="utf-8") as f:
        f.write(command_text(CODE_DIR, cfg, st))
    os.chmod(args.bin_path, 0o755)
    runner(["systemctl", "daemon-reload"])
    for svc in units:
        runner(["systemctl", "enable", svc + ".service"])
        runner(["systemctl", "restart", svc + ".service"])
    out("  ok  " + _("servizi abilitati e (ri)avviati, comando {v}", v=args.bin_path))

    # the last lines are for the person at the keyboard; RESULT is for the wizard on a Proxmox host
    if summary:
        out("")
        if fingerprint:
            out(_("Impronta SHA-256 del certificato (confrontala nel browser alla prima visita):"))
            out("  " + fingerprint)
        if setup_code:
            out(_("Codice per il primo accesso (serve per scegliere la password dal browser): {v}", v=setup_code))
    out("RESULT " + json.dumps({"https": https, "fingerprint": fingerprint, "setup_code": setup_code,
                                "name": args.name if args.name else None}))
    return 0


def parser():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config-dir", default="/etc/dobermann")
    ap.add_argument("--state-dir", default="/var/lib/dobermann")
    ap.add_argument("--systemd-dir", default="/etc/systemd/system")
    ap.add_argument("--bin-path", default="/usr/local/bin/dobermann")
    ap.add_argument("--language", choices=list(i18n.available()), help=_("lingua del pannello e delle mail"))
    ap.add_argument("--https", dest="https", action="store_true", default=None, help=_("pannello in HTTPS (predefinito)"))
    ap.add_argument("--http", "--no-https", dest="https", action="store_false", help=_("pannello in HTTP, senza certificato"))
    ap.add_argument("--bind", help=_("indirizzo su cui ascolta il pannello (vuoto: tutti, il predefinito)"))
    ap.add_argument("--address", help=_("indirizzo con cui si raggiunge il pannello, per i link nelle mail"))
    ap.add_argument("--name", help=_("nome locale da annunciare, es. dobermann.local (serve avahi)"))
    ap.add_argument("--no-name", action="store_true", help=_("non annunciare nessun nome locale"))
    ap.add_argument("--password-stdin", action="store_true", help=_("legge la password del pannello dallo standard input"))
    ap.add_argument("--platform-ct", type=int, metavar="ID", help=argparse.SUPPRESS)
    ap.add_argument("--no-summary", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--platform-bridge", help=argparse.SUPPRESS)
    ap.add_argument("--nullmailer-remotes", default="/etc/nullmailer/remotes")
    ap.add_argument("--apply", action="store_true", help=_("installa (altrimenti è una prova a vuoto)"))
    return ap


def main(argv=None):
    i18n.set_language(os.environ.get("LC_ALL") or os.environ.get("LANG"))
    args = parser().parse_args(argv)
    password = None
    if args.password_stdin:
        password = sys.stdin.readline().rstrip("\n")
        if len(password) < 8:
            print(_("ERRORE: la password deve avere almeno 8 caratteri"), file=sys.stderr)
            return 1
    try:
        return run(args, password=password, summary=not args.no_summary)
    except (InstallError, make_cert.CertError, subprocess.CalledProcessError, OSError) as e:
        print(_("ERRORE:"), e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
