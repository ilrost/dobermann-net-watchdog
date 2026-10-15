#!/usr/bin/env python3
"""Create a Proxmox container with dobermann inside, or update the dobermann of one created before.
Run it on the Proxmox host, as root, from a copy of this repository.

    python3 tools/proxmox_ct.py --ip 192.168.1.50/24              # DRY RUN: shows what it would do
    python3 tools/proxmox_ct.py --ip 192.168.1.50/24 --apply      # create the CT and install
    python3 tools/proxmox_ct.py --ip dhcp --apply                 # address from the router's DHCP
    python3 tools/proxmox_ct.py --update 210 --apply              # put this copy's code into CT 210

The CT: unprivileged, Debian "standard" template (the newest one already on the host; downloaded only
when there is none, or with --template), 1 core, 256 MB,
4 GB of disk (30 days of samples take ~0.5 GB), started at boot, on the bridge of the host's own LAN (vmbr0). A fixed address is better
than DHCP: it is where the panel will be. Inside it: python3, ping, openssl, nullmailer; the code in /opt/dobermann,
then tools/install.py. Nothing outside the new CT is changed, apart from the template download.

An update installs any package added to PACKAGES since, replaces /opt/dobermann (the previous code stays in /opt/dobermann.old until the next update)
and reruns tools/install.py, which never touches the configuration or the data.

Stdlib only.
"""
import argparse
import io
import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time

CODE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# nullmailer: the local mail relay
PACKAGES = ["python3", "iputils-ping", "openssl", "ca-certificates", "nullmailer"]
SKIP = {".git", "__pycache__", ".pytest_cache"}


class CtError(Exception):
    pass


def _out(cmd, check=True):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise CtError(f"{' '.join(cmd)}: {(r.stderr or r.stdout).strip()}")
    return r.stdout


def newest_template(available_text, family="debian"):
    """The newest '<family>-<N>-standard_..._amd64.tar.zst' in `pveam available` output, or None."""
    best = None
    for name in re.findall(rf"\b({family}-(\d+)-standard_([\d.]+)-\d+_amd64\.tar\.\w+)", available_text):
        key = (int(name[1]), [int(x) for x in name[2].split(".")])
        if best is None or key > best[0]:
            best = (key, name[0])
    return best[1] if best else None


def net0(bridge, ip, gw):
    if ip == "dhcp":
        return f"name=eth0,bridge={bridge},ip=dhcp,firewall=1"
    return f"name=eth0,bridge={bridge},ip={ip},gw={gw},firewall=1"


def host_gateway(bridge):
    m = re.search(r"default via (\S+) dev " + re.escape(bridge) + r"\b", _out(["ip", "-4", "route", "show", "default"]))
    return m.group(1) if m else None


def code_tarball(code_dir=CODE_DIR):
    """This copy of the repository as a .tar.gz in memory, without git data or caches."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(code_dir, arcname="dobermann",
                filter=lambda ti: None if (set(ti.name.split("/")) & SKIP or ti.name.endswith(".pyc")) else ti)
    return buf.getvalue()


def create_plan(args, template):
    """The pct commands that create and start the CT."""
    return [
        ["pct", "create", str(args.id), template,
         "--hostname", args.hostname, "--unprivileged", "1", "--features", "nesting=1",
         "--cores", str(args.cores), "--memory", str(args.memory), "--swap", str(args.memory),
         "--rootfs", f"{args.storage}:{args.disk}", "--net0", net0(args.bridge, args.ip, args.gw),
         "--ostype", "debian", "--timezone", "host", "--onboot", "1", "--start", "1",
         "--description", "dobermann-net-watchdog: sorveglianza della connessione a internet"],
    ]


def pct_exec(ctid, script):
    return _out(["pct", "exec", str(ctid), "--", "sh", "-c", script])


def wait_for_network(ctid, timeout=120):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = subprocess.run(["pct", "exec", str(ctid), "--", "getent", "hosts", "deb.debian.org"], capture_output=True)
        if r.returncode == 0:
            return
        time.sleep(3)
    raise CtError(f"il CT {ctid} non raggiunge deb.debian.org dopo {timeout}s: controlla indirizzo, gateway e DNS")


def install_packages(ctid, out):
    out("  ..  installo i pacchetti")
    # noninteractive: nullmailer's installer otherwise waits for an answer nobody can give
    pct_exec(ctid, "export DEBIAN_FRONTEND=noninteractive; apt-get update -q >/dev/null && "
                   f"apt-get install -y -q --no-install-recommends {' '.join(PACKAGES)} >/dev/null")
    out("  ok  pacchetti installati")


def install_code(ctid, out):
    data = code_tarball()
    tmp = f"/tmp/dobermann-{os.getpid()}.tar.gz"
    with open(tmp, "wb") as f:
        f.write(data)
    try:
        _out(["pct", "push", str(ctid), tmp, "/root/dobermann.tar.gz"])
    finally:
        os.unlink(tmp)
    out(f"  ok  codice copiato nel CT ({len(data) // 1024} KB)")
    pct_exec(ctid, "set -e; rm -rf /opt/dobermann.new; mkdir -p /opt/dobermann.new; "
                   "tar -xzf /root/dobermann.tar.gz -C /opt/dobermann.new --strip-components=1; "
                   "rm /root/dobermann.tar.gz; rm -rf /opt/dobermann.old; "
                   "if [ -d /opt/dobermann ]; then mv /opt/dobermann /opt/dobermann.old; fi; "
                   "mv /opt/dobermann.new /opt/dobermann")
    out(pct_exec(ctid, "python3 /opt/dobermann/tools/install.py --apply").rstrip())


def ct_address(ctid):
    try:
        for iface in json.loads(_out(["pct", "exec", str(ctid), "--", "ip", "-j", "-4", "addr", "show", "eth0"])):
            for a in iface.get("addr_info", []):
                return a["local"]
    except (CtError, ValueError, KeyError):
        pass
    return None


def run(args, out=print):
    if not shutil.which("pct"):
        raise CtError("`pct` non trovato: lo script va lanciato sull'host Proxmox")
    existing = {line.split()[0] for line in _out(["pct", "list"]).splitlines()[1:] if line.strip()}

    if args.update:
        if str(args.update) not in existing:
            raise CtError(f"il CT {args.update} non esiste")
        out(f"Aggiornamento di dobermann nel CT {args.update} con il codice di {CODE_DIR}")
        if not args.apply:
            out("\nPROVA A VUOTO: nulla è stato cambiato. Rilancia con --apply.")
            return 0
        install_packages(args.update, out)
        install_code(args.update, out)
        return 0

    if not args.ip:
        raise CtError("serve --ip: un indirizzo fisso con la maschera (es. 192.168.1.50/24) oppure dhcp")
    if args.ip != "dhcp":
        try:
            ipaddress.ip_interface(args.ip)
        except ValueError:
            raise CtError(f"--ip {args.ip!r}: serve un indirizzo con la maschera, es. 192.168.1.50/24") from None
        if "/" not in args.ip:
            raise CtError(f"--ip {args.ip!r}: manca la maschera, es. {args.ip}/24")
        args.gw = args.gw or host_gateway(args.bridge)
        if not args.gw:
            raise CtError(f"nessun gateway predefinito su {args.bridge}: indicalo con --gw")
    args.id = args.id or int(_out(["pvesh", "get", "/cluster/nextid"]).strip())
    if str(args.id) in existing:
        raise CtError(f"il CT {args.id} esiste già: scegli un altro --id, o usa --update {args.id}")

    local = _out(["pveam", "list", args.template_storage])
    template_name = args.template or newest_template(local)
    have = bool(template_name) and template_name in local
    if not template_name:
        template_name = newest_template(_out(["pveam", "available", "--section", "system"]))
        if not template_name:
            raise CtError("nessun modello Debian standard fra quelli di `pveam available`: esegui `pveam update`")
    template = f"{args.template_storage}:vztmpl/{template_name}"

    out(f"CT          : {args.id} ({args.hostname}), non privilegiato, {args.cores} core, {args.memory} MB, "
        f"{args.disk} GB su {args.storage}")
    out(f"rete        : {args.bridge}, " + ("DHCP" if args.ip == "dhcp" else f"{args.ip} via {args.gw}"))
    out(f"modello     : {template}" + ("" if have else "  (da scaricare)"))
    out(f"pacchetti   : {' '.join(PACKAGES)}")
    out(f"codice      : {CODE_DIR} -> /opt/dobermann, poi tools/install.py")
    if not args.apply:
        out("\nPROVA A VUOTO: nulla è stato cambiato. Rilancia con --apply per creare il CT.")
        return 0

    if not have:
        out(f"  ..  scarico {template_name}")
        _out(["pveam", "download", args.template_storage, template_name])
    for cmd in create_plan(args, template):
        _out(cmd)
    out(f"  ok  CT {args.id} creato e avviato")
    wait_for_network(args.id)
    install_packages(args.id, out)
    install_code(args.id, out)
    addr = ct_address(args.id) or "<indirizzo del CT>"
    out(f"\nPannello: https://{addr}/  (alla prima visita scegli la password)")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ip", help="indirizzo/maschera del CT (es. 192.168.1.50/24) oppure dhcp")
    ap.add_argument("--gw", help="gateway (predefinito: quello dell'host sul bridge)")
    ap.add_argument("--id", type=int, help="numero del CT (predefinito: il primo libero)")
    ap.add_argument("--hostname", default="dobermann")
    ap.add_argument("--bridge", default="vmbr0")
    ap.add_argument("--storage", default="local-lvm", help="dove sta il disco del CT")
    ap.add_argument("--template-storage", default="local", help="dove stanno i modelli dei CT")
    ap.add_argument("--template", help="nome del modello da usare (predefinito: il Debian più recente già sull'host)")
    ap.add_argument("--disk", type=int, default=4, help="GB")
    ap.add_argument("--memory", type=int, default=256, help="MB")
    ap.add_argument("--cores", type=int, default=1)
    ap.add_argument("--update", type=int, metavar="ID", help="aggiorna il codice nel CT esistente ID")
    ap.add_argument("--apply", action="store_true", help="esegui (altrimenti è una prova a vuoto)")
    args = ap.parse_args(argv)
    try:
        return run(args)
    except CtError as e:
        print("ERRORE:", e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
