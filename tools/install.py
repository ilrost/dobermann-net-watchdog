#!/usr/bin/env python3
"""Install dobermann on this Linux machine (or update it), from a copy of this repository.

    python3 tools/install.py              # DRY RUN: checks the machine, shows the plan, changes nothing
    python3 tools/install.py --apply      # do it (as root)

The code stays where this copy is (/opt/dobermann in the Proxmox CT, see tools/proxmox_ct.py). What
--apply does:
  1. preflight: Python 3.11+ and `ping` are required; `openssl` (HTTPS certificate) is only a warning;
  2. creates the system user `dobermann`, which both services run as;
  3. creates the configuration directory (0750) and the data directory (0750), owned by that user;
  4. if nullmailer is installed (the local mail relay), gives its provider file /etc/nullmailer/remotes to
     that user (group mail, 0640), so that the panel can write the provider's details into it;
  5. creates a self-signed HTTPS certificate for the panel if there is none (tools/make_cert.py), so
     that the panel password and the mail password never cross the LAN in clear; --no-https skips it;
  6. writes the two systemd units and the `dobermann` command (/usr/local/bin/dobermann), then enables
     and (re)starts the services.

Running it again is how an update is installed: the units and the command are rewritten, the services
restarted, and the configuration and the data are never touched (an existing certificate is kept).
The first visit to https://<this machine>/ chooses the panel password; the rest is set from the panel.

Stdlib only.
"""
import argparse
import os
import shutil
import subprocess
import sys

CODE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import make_cert  # noqa: E402
UNIT_DIR = os.path.join(CODE_DIR, "packaging", "systemd")
SERVICES = ("dobermann", "dobermann-web")
USER = "dobermann"
MIN_PYTHON = (3, 11)


class InstallError(Exception):
    pass


def preflight():
    """([errors], [warnings]) about this machine. Pure reads."""
    errors, warnings = [], []
    if sys.version_info < MIN_PYTHON:
        errors.append(f"serve Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} o più recente, questo è {sys.version.split()[0]}")
    if not shutil.which("ping"):
        errors.append("manca `ping` (pacchetto iputils-ping): senza, ogni apparato risulterebbe irraggiungibile")
    if not shutil.which("openssl"):
        warnings.append("manca `openssl` (pacchetto openssl): serve solo per creare il certificato HTTPS")
    return errors, warnings


def render_unit(text, code_dir, config_dir, state_dir):
    return text.replace("@CODE_DIR@", code_dir).replace("@CONFIG_DIR@", config_dir).replace("@STATE_DIR@", state_dir)


def command_text(code_dir, config_dir, state_dir):
    return ("#!/bin/sh\n# Written by tools/install.py: the dobermann command line with this install's directories.\n"
            f"export DOBERMANN_CONFIG_DIR={config_dir} DOBERMANN_STATE_DIR={state_dir}\n"
            f"cd {code_dir} && exec /usr/bin/python3 -m dobermann \"$@\"\n")


def _run(cmd):
    subprocess.run(cmd, check=True)


def run(args, out=print, runner=_run):
    cfg, st = os.path.abspath(args.config_dir), os.path.abspath(args.state_dir)
    errors, warnings = preflight()
    out(f"codice      : {CODE_DIR}  (resta dov'è)")
    out(f"config      : {cfg}")
    out(f"dati        : {st}")
    out(f"utente      : {USER}")
    out(f"servizi     : {', '.join(SERVICES)} in {args.systemd_dir}")
    out(f"comando     : {args.bin_path}")
    out("HTTPS       : " + ("no (--no-https)" if args.no_https else "sì, certificato autofirmato se manca"))
    for w in warnings:
        out(f"ATTENZIONE: {w}")
    for e in errors:
        out(f"MANCA: {e}")
    if errors:
        raise InstallError("sistema i requisiti mancanti qui sopra, poi rilancia")
    if not args.apply:
        out("\nPROVA A VUOTO: nulla è stato cambiato. Rilancia con --apply per installare.")
        return 0
    if os.geteuid() != 0:
        raise InstallError("l'installazione va fatta da root")

    try:
        import pwd
        pwd.getpwnam(USER)
    except KeyError:
        runner(["useradd", "--system", "--home-dir", st, "--no-create-home", "--shell", "/usr/sbin/nologin", USER])
        out(f"  ok  utente {USER} creato")
    for d in (cfg, st):
        os.makedirs(d, mode=0o750, exist_ok=True)
        os.chmod(d, 0o750)
        runner(["chown", "-R", f"{USER}:{USER}", d])
    out(f"  ok  {cfg}, {st}")
    if os.path.exists(args.nullmailer_remotes):
        runner(["chown", f"{USER}:mail", args.nullmailer_remotes])
        os.chmod(args.nullmailer_remotes, 0o640)
        out(f"  ok  {args.nullmailer_remotes} scrivibile dal pannello")
    if not args.no_https:
        cert, key = os.path.join(cfg, "tls.crt"), os.path.join(cfg, "tls.key")
        if os.path.exists(cert) or os.path.exists(key):
            out("  ok  certificato HTTPS già presente, lasciato com'è")
        elif not shutil.which("openssl"):
            out("  !!  openssl mancante: pannello solo in HTTP")
        else:
            sans = make_cert.san_entries(make_cert.detect_names())
            make_cert.generate(cert, key, sans)
            out(f"  ok  certificato HTTPS per {', '.join(sans)}, impronta SHA-256 {make_cert.fingerprint(cert)}")
    for svc in SERVICES:
        with open(os.path.join(UNIT_DIR, svc + ".service"), encoding="utf-8") as f:
            text = render_unit(f.read(), CODE_DIR, cfg, st)
        with open(os.path.join(args.systemd_dir, svc + ".service"), "w", encoding="utf-8") as f:
            f.write(text)
    with open(args.bin_path, "w", encoding="utf-8") as f:
        f.write(command_text(CODE_DIR, cfg, st))
    os.chmod(args.bin_path, 0o755)
    runner(["systemctl", "daemon-reload"])
    for svc in SERVICES:
        runner(["systemctl", "enable", svc + ".service"])
        runner(["systemctl", "restart", svc + ".service"])
    out(f"  ok  servizi abilitati e (ri)avviati, comando {args.bin_path}")
    out("\nFatto. Apri https://<questa macchina>/ (il browser avvisa una volta del certificato autofirmato:\n"
        "confronta l'impronta qui sopra e accetta) per scegliere la password del pannello, poi Impostazioni.")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config-dir", default="/etc/dobermann")
    ap.add_argument("--state-dir", default="/var/lib/dobermann")
    ap.add_argument("--systemd-dir", default="/etc/systemd/system")
    ap.add_argument("--bin-path", default="/usr/local/bin/dobermann")
    ap.add_argument("--no-https", action="store_true", help="non creare il certificato HTTPS")
    ap.add_argument("--nullmailer-remotes", default="/etc/nullmailer/remotes")
    ap.add_argument("--apply", action="store_true", help="installa (altrimenti è una prova a vuoto)")
    args = ap.parse_args(argv)
    try:
        return run(args)
    except (InstallError, make_cert.CertError, subprocess.CalledProcessError, OSError) as e:
        print("ERRORE:", e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
