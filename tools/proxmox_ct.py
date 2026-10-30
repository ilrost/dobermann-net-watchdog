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
than DHCP: it is where the panel will be. Inside it: python3, ping, openssl, avahi; the code in /opt/dobermann,
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
import tempfile
import time

CODE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, CODE_DIR)
import deps  # noqa: E402
from dobermann import i18n  # noqa: E402
from dobermann.i18n import _  # noqa: E402

# the CT is Debian: everything an install with HTTPS and the local name needs (see tools/deps.py)
PACKAGES = deps.packages(deps.wanted(https=True, mdns=True), "apt")
DEFAULT_NAME = "dobermann.local"
SKIP = {".git", "__pycache__", ".pytest_cache"}


class CtError(Exception):
    pass


def _out(cmd, check=True, input=None):
    r = subprocess.run(cmd, capture_output=True, text=True, input=input)
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


def address_in_use(ip, timeout_s=1):
    """True if something already answers a ping at `ip` (without the mask)."""
    r = subprocess.run(["ping", "-c", "2", "-W", str(timeout_s), ip], capture_output=True)
    return r.returncode == 0


def bridges():
    """The Linux bridges of the host (vmbr0, ...), from `ip -j link`."""
    try:
        links = json.loads(_out(["ip", "-j", "link", "show", "type", "bridge"]))
    except (CtError, ValueError):
        return []
    return sorted(link["ifname"] for link in links if link.get("ifname", "").startswith("vmbr"))


def bridge_network(bridge):
    """The host's own address on the bridge, as an interface (192.0.2.2/24), or None."""
    try:
        for iface in json.loads(_out(["ip", "-j", "-4", "addr", "show", bridge])):
            for a in iface.get("addr_info", []):
                return ipaddress.ip_interface(f"{a['local']}/{a['prefixlen']}")
    except (CtError, ValueError, KeyError):
        pass
    return None


def storages():
    """The storages that can hold a CT's disk ('rootdir' content), from pvesm."""
    try:
        text = _out(["pvesm", "status", "--content", "rootdir"])
    except CtError:
        return []
    return [ln.split()[0] for ln in text.splitlines()[1:] if ln.strip() and ln.split()[2:3] == ["active"]]


def template_storage():
    """A storage that holds CT templates: 'local' when it does, else the first that does."""
    try:
        text = _out(["pvesm", "status", "--content", "vztmpl"])
    except CtError:
        return "local"
    names = [ln.split()[0] for ln in text.splitlines()[1:] if ln.strip() and ln.split()[2:3] == ["active"]]
    return "local" if "local" in names or not names else names[0]


def next_id():
    return int(_out(["pvesh", "get", "/cluster/nextid"]).strip())


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
         "--description", "dobermann: internet connection watchdog"],
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
    raise CtError(_("il CT {id} non raggiunge deb.debian.org dopo {t}s: controlla indirizzo, gateway e DNS", id=ctid, t=timeout))


def install_packages(ctid, out):
    out("  ..  " + _("installo i pacchetti"))
    # noninteractive: a package's installer must never wait for an answer nobody can give
    pct_exec(ctid, "export DEBIAN_FRONTEND=noninteractive; apt-get update -q >/dev/null && "
                   f"apt-get install -y -q --no-install-recommends {' '.join(PACKAGES)} >/dev/null")
    out("  ok  " + _("pacchetti installati"))


def install_code(ctid, out, install_args=(), password=None):
    """Copies the code into the CT and runs tools/install.py there. Returns what it reported (its RESULT
    line). The password goes through standard input, never on a command line."""
    data = code_tarball()
    fd, tmp = tempfile.mkstemp(suffix=".tar.gz", prefix="dobermann-")    # 0600, never an existing file
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    try:
        _out(["pct", "push", str(ctid), tmp, "/root/dobermann.tar.gz"])
    finally:
        os.unlink(tmp)
    out("  ok  " + _("codice copiato nel CT ({v} KB)", v=len(data) // 1024))
    pct_exec(ctid, "set -e; rm -rf /opt/dobermann.new; mkdir -p /opt/dobermann.new; "
                   "tar -xzf /root/dobermann.tar.gz -C /opt/dobermann.new --strip-components=1; "
                   "rm /root/dobermann.tar.gz; rm -rf /opt/dobermann.old; "
                   "if [ -d /opt/dobermann ]; then mv /opt/dobermann /opt/dobermann.old; fi; "
                   "mv /opt/dobermann.new /opt/dobermann")
    cmd = ["pct", "exec", str(ctid), "--", "python3", "/opt/dobermann/tools/install.py", "--apply", "--no-summary",
           *install_args]
    if password is not None:
        cmd.append("--password-stdin")
    text = _out(cmd, input=(password + "\n") if password is not None else None)
    result = {}
    for line in text.splitlines():
        if line.startswith("RESULT "):
            result = json.loads(line[len("RESULT "):])
        else:
            out(line)
    return result


def ct_bridge(ctid):
    """The bridge of the CT's first network interface, from its Proxmox configuration."""
    m = re.search(r"^net0:.*\bbridge=([^,\s]+)", _out(["pct", "config", str(ctid)]), re.M)
    return m.group(1) if m else None


def ct_address(ctid):
    try:
        for iface in json.loads(_out(["pct", "exec", str(ctid), "--", "ip", "-j", "-4", "addr", "show", "eth0"])):
            for a in iface.get("addr_info", []):
                return a["local"]
    except (CtError, ValueError, KeyError):
        pass
    return None


def install_args(args, address):
    out = ["--platform-ct", str(args.id), "--platform-bridge", args.bridge]
    if getattr(args, "language", None):
        out += ["--language", args.language]
    out.append("--http" if getattr(args, "http", False) else "--https")
    out += ["--no-name"] if getattr(args, "no_name", False) else ["--name", DEFAULT_NAME]
    if address:
        out += ["--address", address]
    return out


def run(args, out=print, password=None):
    """Returns what the install inside the CT reported ({} for a dry run), plus 'ctid' and 'address'."""
    if not shutil.which("pct"):
        raise CtError(_("`pct` non trovato: lo script va lanciato sull'host Proxmox"))
    existing = {line.split()[0] for line in _out(["pct", "list"]).splitlines()[1:] if line.strip()}

    if args.update:
        if str(args.update) not in existing:
            raise CtError(_("il CT {v} non esiste", v=args.update))
        out(_("Aggiornamento di dobermann nel CT {id} con il codice di {dir}", id=args.update, dir=CODE_DIR))
        if not args.apply:
            out("\n" + _("PROVA A VUOTO: nulla è stato cambiato. Rilancia con --apply."))
            return {}
        install_packages(args.update, out)
        extra = ["--platform-ct", str(args.update), "--platform-bridge", ct_bridge(args.update) or "vmbr0"]
        if args.language:
            extra += ["--language", args.language]
        result = install_code(args.update, out, install_args=extra)
        return dict(result, ctid=args.update, address=ct_address(args.update))

    if not args.ip:
        raise CtError(_("serve --ip: un indirizzo fisso con la maschera (es. 192.168.1.50/24) oppure dhcp"))
    if args.ip != "dhcp":
        try:
            ipaddress.ip_interface(args.ip)
        except ValueError:
            raise CtError(_("--ip {v}: serve un indirizzo con la maschera, es. 192.168.1.50/24", v=repr(args.ip))) from None
        if "/" not in args.ip:
            raise CtError(_("--ip {v}: manca la maschera, es. {ip}/24", v=repr(args.ip), ip=args.ip))
        args.gw = args.gw or host_gateway(args.bridge)
        if not args.gw:
            raise CtError(_("nessun gateway predefinito su {v}: indicalo con --gw", v=args.bridge))
        if address_in_use(args.ip.split("/")[0]):
            raise CtError(_("all'indirizzo {v} risponde già qualcosa: scegline uno libero", v=args.ip.split("/")[0]))
    args.id = args.id or next_id()
    if str(args.id) in existing:
        raise CtError(_("il CT {v} esiste già: scegli un altro --id, o usa --update {v}", v=args.id))

    args.template_storage = args.template_storage or template_storage()
    local = _out(["pveam", "list", args.template_storage])
    template_name = args.template or newest_template(local)
    have = bool(template_name) and template_name in local
    if not template_name:
        template_name = newest_template(_out(["pveam", "available", "--section", "system"]))
        if not template_name:
            raise CtError(_("nessun modello Debian standard fra quelli di `pveam available`: esegui `pveam update`"))
    template = f"{args.template_storage}:vztmpl/{template_name}"

    out(_("CT          : {id} ({name}), non privilegiato, {cores} core, {mem} MB, {disk} GB su {st}", id=args.id,
          name=args.hostname, cores=args.cores, mem=args.memory, disk=args.disk, st=args.storage))
    out(_("rete        : {v}", v=f"{args.bridge}, " + ("DHCP" if args.ip == "dhcp" else f"{args.ip} via {args.gw}")))
    out(_("modello     : {v}", v=template) + ("" if have else "  " + _("(da scaricare)")))
    out(_("pacchetti   : {v}", v=" ".join(PACKAGES)))
    out(_("codice      : {v} -> /opt/dobermann, poi tools/install.py", v=CODE_DIR))
    if not args.apply:
        out("\n" + _("PROVA A VUOTO: nulla è stato cambiato. Rilancia con --apply per creare il CT."))
        return {}

    if not have:
        out("  ..  " + _("scarico {v}", v=template_name))
        _out(["pveam", "download", args.template_storage, template_name])
    for cmd in create_plan(args, template):
        _out(cmd)
    out("  ok  " + _("CT {v} creato e avviato", v=args.id))
    try:
        wait_for_network(args.id)
        install_packages(args.id, out)
        fixed = None if args.ip == "dhcp" else args.ip.split("/")[0]
        result = install_code(args.id, out, install_args(args, fixed), password)
    except CtError as e:
        # the CT exists, starts with the host and holds its number: say so, and how to remove it
        raise CtError(_("{e}\nIl CT {v} è stato creato ma l'installazione non è finita. Per toglierlo: "
                        "pct stop {v} && pct destroy {v} --purge", e=e, v=args.id)) from None
    return dict(result, ctid=args.id, address=fixed or ct_address(args.id))


def main(argv=None):
    i18n.set_language(os.environ.get("LC_ALL") or os.environ.get("LANG"))
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ip", help=_("indirizzo/maschera del CT (es. 192.168.1.50/24) oppure dhcp"))
    ap.add_argument("--gw", help=_("gateway (predefinito: quello dell'host sul bridge)"))
    ap.add_argument("--id", type=int, help=_("numero del CT (predefinito: il primo libero)"))
    ap.add_argument("--hostname", default="dobermann")
    ap.add_argument("--bridge", default="vmbr0")
    ap.add_argument("--storage", default="local-lvm", help=_("dove sta il disco del CT"))
    ap.add_argument("--template-storage", help=_("dove stanno i modelli dei CT"))
    ap.add_argument("--template", help=_("nome del modello da usare (predefinito: il Debian più recente già sull'host)"))
    ap.add_argument("--disk", type=int, default=4, help="GB")
    ap.add_argument("--memory", type=int, default=256, help="MB")
    ap.add_argument("--cores", type=int, default=1)
    ap.add_argument("--update", type=int, metavar="ID", help=_("aggiorna il codice nel CT esistente ID"))
    ap.add_argument("--language", choices=list(i18n.available()), help=_("lingua del pannello e delle mail"))
    ap.add_argument("--http", action="store_true", help=_("pannello in HTTP invece che HTTPS"))
    ap.add_argument("--no-name", action="store_true", help=_("non annunciare il nome {v}", v=DEFAULT_NAME))
    ap.add_argument("--apply", action="store_true", help=_("esegui (altrimenti è una prova a vuoto)"))
    args = ap.parse_args(argv)
    if args.language:
        i18n.set_language(args.language)
    try:
        result = run(args)
        if result.get("address"):
            scheme = "https" if result.get("https") else "http"
            print("\n" + _("Pannello: {v}", v=f"{scheme}://{result['address']}/"))
        if result.get("fingerprint"):
            print(_("Impronta SHA-256 del certificato: {v}", v=result["fingerprint"]))
        if result.get("setup_code"):
            print(_("Codice per il primo accesso: {v}", v=result["setup_code"]))
        return 0
    except CtError as e:
        print(_("ERRORE:"), e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
