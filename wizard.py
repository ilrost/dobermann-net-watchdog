#!/usr/bin/env python3
"""The installation wizard: the first of two phases.

    sudo python3 wizard.py

Phase 1, here: the language, where dobermann goes (a new Proxmox container, or this machine), the
address it is reached at, HTTPS or HTTP, the programs it needs, the panel password. Then it installs.
Phase 2, in the browser: the chain of devices, the mail, when to be told. Anything skipped here can
be set there, except installing programs, which only this phase can do.

Every question has an answer ready (Enter accepts it). For a script, every answer can be given as an
option and --yes accepts the rest: see --help.

Stdlib only.
"""
import argparse
import getpass
import ipaddress
import json
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, ROOT)
from dobermann import __version__, i18n  # noqa: E402
from dobermann.i18n import _  # noqa: E402
import deps  # noqa: E402
import install  # noqa: E402
import proxmox_ct  # noqa: E402

LOGO = [
    "     _         _                                                 ",
    "  __| |  ___  | |__    ___  _ __  _ __ ___    __ _  _ __   _ __  ",
    " / _` | / _ \\ | '_ \\  / _ \\| '__|| '_ ` _ \\  / _` || '_ \\ | '_ \\ ",
    "| (_| || (_) || |_) ||  __/| |   | | | | | || (_| || | | || | | |",
    " \\__,_| \\___/ |_.__/  \\___||_|   |_| |_| |_| \\__,_||_| |_||_| |_|",
]
WIDTH = max(len(x) for x in LOGO)
NAME = proxmox_ct.DEFAULT_NAME


def banner():
    lines = [x.rstrip() for x in LOGO]
    lines.append(f"net watchdog  v{__version__}".center(WIDTH).rstrip())
    lines.append("")
    lines.append("=" * WIDTH)
    return "\n".join(lines)


class Console:
    """Questions and answers. Everything goes through here, so that tests can play the user."""

    def __init__(self, inp=input, secret=getpass.getpass, out=print, assume_yes=False):
        self.inp, self.secret, self.out, self.assume_yes = inp, secret, out, assume_yes

    def say(self, text=""):
        self.out(text)

    def section(self, title):
        self.out("")
        self.out(title)
        self.out("-" * len(title))

    def ask(self, prompt, default=None, check=None):
        """A free answer; `check` returns an error message or None."""
        while True:
            if self.assume_yes and default is not None:
                answer = default
            else:
                shown = f" [{default}]" if default not in (None, "") else ""
                answer = self.inp(f"{prompt}{shown}: ").strip() or (default or "")
            error = check(answer) if check else None
            if not error:
                return answer
            if self.assume_yes:
                raise SystemExit(f"{prompt}: {error}")
            self.out("  " + error)

    def choose(self, prompt, options, default=0):
        """One of `options` [(value, label)], by number. Returns the value."""
        for i, (_value, label) in enumerate(options, 1):
            self.out(f"  {i}) {label}")
        if self.assume_yes:
            return options[default][0]
        while True:
            answer = self.inp(f"{prompt} [{default + 1}]: ").strip()
            if not answer:
                return options[default][0]
            if answer.isdigit() and 1 <= int(answer) <= len(options):
                return options[int(answer) - 1][0]
            self.out("  " + _("scrivi un numero da 1 a {n}", n=len(options)))

    def yes(self, prompt, default=True):
        if self.assume_yes:
            return default
        hint = _("S/n") if default else _("s/N")
        yes_words = {"s", "si", "sì", "sí", "y", "yes", "j", "ja", "o", "oui"}
        while True:
            answer = self.inp(f"{prompt} [{hint}]: ").strip().lower()
            if not answer:
                return default
            if answer in yes_words:
                return True
            if answer in {"n", "no", "nein", "non"}:
                return False
            self.out("  " + hint)

    def password(self):
        """The panel password, or None to choose it later in the browser."""
        if self.assume_yes:
            return None
        while True:
            p1 = self.secret(_("Password del pannello (almeno 8 caratteri; Invio per sceglierla dopo dal browser): "))
            if not p1:
                return None
            if len(p1) < 8:
                self.out("  " + _("la password deve avere almeno 8 caratteri"))
                continue
            if p1 == self.secret(_("Ripeti: ")):
                return p1
            self.out("  " + _("le due password non coincidono, riprova"))


# --- what this machine is -----------------------------------------------------------------------

def is_proxmox_host(which=shutil.which, exists=os.path.exists):
    return bool(which("pct")) and exists("/etc/pve")


def local_addresses():
    """[(interface, 'a.b.c.d/nn')] of this machine, loopback excluded."""
    try:
        data = json.loads(subprocess.run(["ip", "-j", "-4", "addr", "show"], capture_output=True, text=True).stdout)
    except (OSError, ValueError):
        return []
    out = []
    for iface in data:
        for a in iface.get("addr_info", []):
            if not a.get("local", "").startswith("127."):
                out.append((iface.get("ifname", "?"), f"{a['local']}/{a['prefixlen']}"))
    return out


def suggest_ct_address(host_iface, in_use=proxmox_ct.address_in_use, first=50, tries=20):
    """A free address on the host's network for the CT, e.g. 192.168.1.50/24, or None."""
    if host_iface is None:
        return None
    net = host_iface.network
    for n in range(first, first + tries):
        try:
            cand = net.network_address + n
        except ValueError:
            return None
        if cand in net and cand != host_iface.ip and cand != net.broadcast_address and not in_use(str(cand)):
            return f"{cand}/{net.prefixlen}"
    return None


def check_ct_ip(value, in_use=proxmox_ct.address_in_use):
    if value == "dhcp":
        return None
    try:
        iface = ipaddress.ip_interface(value)
    except ValueError:
        return _("serve un indirizzo con la maschera, es. 192.168.1.50/24, oppure dhcp")
    if "/" not in value or iface.version != 4:
        return _("serve un indirizzo IPv4 con la maschera, es. {v}/24", v=value.split("/")[0])
    if in_use(str(iface.ip)):
        return _("all'indirizzo {v} risponde già qualcosa: scegline uno libero", v=iface.ip)
    return None


# --- the wizard ---------------------------------------------------------------------------------

def pick_language(con, given=None):
    if given:
        return i18n.normalize(given)
    default = i18n.normalize(os.environ.get("LC_ALL") or os.environ.get("LANG"))
    con.section("Language · Lingua · Idioma · Sprache · Langue")
    langs = i18n.available()
    codes = list(langs)
    return con.choose(">", [(c, langs[c]) for c in codes], codes.index(default))


def ask_dependencies(con, wanted):
    """Installs what is missing, after asking. Returns False if a required program is still missing."""
    con.section(_("Programmi necessari"))
    mgr = deps.manager()
    lacking = deps.missing(wanted)
    for d in wanted:
        con.say(f"  {'ok' if d not in lacking else '--'}  {d}")
    if not lacking:
        con.say(_("Tutto presente."))
        return True
    if mgr is None:
        con.say(_("Non riconosco il gestore di pacchetti di questo sistema: installa a mano {v}, poi rilancia.",
                  v=", ".join(lacking)))
        return False
    con.say(_("Mancano: {v}", v=" ".join(deps.packages(lacking, mgr))))
    if not con.yes(_("Li installo ora con {v}?", v=mgr)):
        return not [d for d in lacking if not deps.optional(d)]
    failed = deps.install(lacking, mgr)
    for d in failed:
        if deps.optional(d):
            con.say("  !!  " + _("{v} non disponibile su questo sistema: le mail partiranno via SMTP diretto", v=d))
    return not [d for d in failed if not deps.optional(d)]


def plan_container(con, args):
    con.section(_("Il container"))
    bridges = proxmox_ct.bridges() or ["vmbr0"]
    bridge = args.bridge or (bridges[0] if len(bridges) == 1 else
                             con.choose(_("Rete (bridge)"), [(b, b) for b in bridges], 0))
    host_iface = proxmox_ct.bridge_network(bridge)
    ctid = args.id or int(con.ask(_("Numero del container"), str(proxmox_ct.next_id()),
                                  check=lambda v: None if v.isdigit() else _("serve un numero")))
    con.say(_("Un indirizzo fisso è meglio: è dove aprirai il pannello. Scegline uno fuori dall'intervallo che il router "
              "assegna da solo (DHCP)."))
    if args.ip and check_ct_ip(args.ip):
        raise SystemExit(f"--ip {args.ip}: {check_ct_ip(args.ip)}")
    ip = args.ip or con.ask(_("Indirizzo del container (con la maschera) o dhcp"),
                            suggest_ct_address(host_iface) or "dhcp", check=check_ct_ip)
    gw = args.gw
    if gw and not _is_ip(gw):
        raise SystemExit(f"--gw {gw}: " + _("serve un indirizzo IP"))
    if ip != "dhcp" and not gw:
        gw = con.ask(_("Gateway"), proxmox_ct.host_gateway(bridge) or "",
                     check=lambda v: None if _is_ip(v) else _("serve un indirizzo IP"))
    pools = proxmox_ct.storages() or ["local-lvm"]
    storage = args.storage or (pools[0] if len(pools) == 1 else con.choose(
        _("Dove mettere il disco del container"), [(p, p) for p in pools],
        pools.index("local-lvm") if "local-lvm" in pools else 0))
    return {"bridge": bridge, "id": ctid, "ip": ip, "gw": gw, "storage": storage}


def _is_ip(v):
    try:
        ipaddress.ip_address(v)
        return True
    except ValueError:
        return False


def plan_local(con, args):
    con.section(_("Indirizzo del pannello"))
    addrs = local_addresses()
    if args.bind is not None:
        bind = args.bind or None
    elif len(addrs) <= 1:
        bind = None
    else:
        options = [(None, _("tutti gli indirizzi di questa macchina (consigliato)"))]
        options += [(a.split("/")[0], f"{a.split('/')[0]}  ({ifname})") for ifname, a in addrs]
        bind = con.choose(_("Su quale indirizzo deve rispondere il pannello?"), options, 0)
    from dobermann.__main__ import primary_address
    return {"bind": bind, "address": bind or primary_address()}


def summary(con, plan):
    con.section(_("Riepilogo"))
    if plan["mode"] == "ct":
        c = plan["ct"]
        con.say(_("  dove        : nuovo container LXC {id} su {storage}, rete {bridge}", **c))
        con.say(_("  indirizzo   : {v}", v=c["ip"] if c["ip"] == "dhcp" else f"{c['ip']} via {c['gw']}"))
    else:
        con.say(_("  dove        : questa macchina"))
        con.say(_("  indirizzo   : {v}", v=plan["local"]["address"] or "?"))
    con.say(_("  nome locale : {v}", v=NAME if plan["mdns"] else _("no")))
    con.say(_("  pannello    : {v}", v="HTTPS" if plan["https"] else "HTTP"))
    con.say(_("  lingua      : {v}", v=i18n.LANGUAGES[plan["language"]]))
    if plan["password"]:
        pw = _("scelta ora")
    elif plan["mode"] == "here" and os.path.exists(os.path.join(install.parser().get_default("config_dir"), "admin.json")):
        pw = _("già impostata, resta com'è")
    else:
        pw = _("da scegliere nel browser, con un codice")
    con.say(_("  password    : {v}", v=pw))


def execute(con, plan, args):
    """Installs. Returns (result dict, address)."""
    lines = []

    def out(text=""):
        lines.append(text)
        if not text.startswith("RESULT "):
            con.say(text)
    if plan["mode"] == "ct":
        c = plan["ct"]
        ns = argparse.Namespace(id=c["id"], hostname="dobermann", cores=1, memory=256, storage=c["storage"],
                                disk=4, bridge=c["bridge"], ip=c["ip"], gw=c["gw"],
                                template_storage=proxmox_ct.template_storage(),
                                template=None, update=None, apply=True, language=plan["language"],
                                http=not plan["https"], no_name=not plan["mdns"])
        result = proxmox_ct.run(ns, out=out, password=plan["password"])
        return result, result.get("address")
    argv = ["--apply", "--language", plan["language"], "--https" if plan["https"] else "--http"]
    argv += ["--name", NAME] if plan["mdns"] else ["--no-name"]
    argv += ["--bind", plan["local"]["bind"] or ""]
    if plan["local"]["address"]:
        argv += ["--address", plan["local"]["address"]]
    install.run(install.parser().parse_args(argv), out=out, password=plan["password"], summary=False)
    result = {}
    for line in lines:
        if line.startswith("RESULT "):
            result = json.loads(line[len("RESULT "):])
    return result, plan["local"]["address"]


def finish(con, plan, result, address):
    con.say("")
    con.say("=" * WIDTH)
    con.say(_("dobermann è installato."))
    scheme = "https" if result.get("https", plan["https"]) else "http"
    con.say("")
    con.say(_("Apri il pannello:"))
    if address:
        con.say(f"  {scheme}://{address}/")
    if result.get("name"):
        con.say(f"  {scheme}://{result['name']}/   " + _("(funziona su quasi tutti i dispositivi, non sempre su Android)"))
    if result.get("fingerprint"):
        con.say("")
        con.say(_("Il browser avviserà che il certificato non è riconosciuto: è normale, l'ha creato dobermann."))
        con.say(_("Accettalo solo se la sua impronta SHA-256 è questa:"))
        con.say("  " + result["fingerprint"])
    if result.get("setup_code"):
        con.say("")
        con.say(_("Alla prima visita scegli la password del pannello con questo codice: {v}", v=result["setup_code"]))
    con.say("")
    con.say(_("Completa la configurazione dal browser, in Impostazioni: la catena degli apparati, le notifiche email, "
              "quando avvisare."))


def parser():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--language", choices=list(i18n.available()))
    ap.add_argument("--where", choices=("ct", "here"), help=_("ct: un nuovo container Proxmox; here: questa macchina"))
    ap.add_argument("--ip", help=_("container: indirizzo/maschera o dhcp"))
    ap.add_argument("--gw", help=_("container: gateway"))
    ap.add_argument("--bridge", help=_("container: bridge di rete"))
    ap.add_argument("--storage", help=_("container: dove mettere il disco"))
    ap.add_argument("--id", type=int, help=_("container: numero"))
    ap.add_argument("--bind", help=_("questa macchina: indirizzo su cui ascolta il pannello (vuoto: tutti)"))
    ap.add_argument("--http", action="store_true", help=_("pannello in HTTP invece che HTTPS"))
    ap.add_argument("--no-name", action="store_true", help=_("non annunciare il nome {v}", v=NAME))
    ap.add_argument("--password-stdin", action="store_true", help=_("legge la password del pannello dallo standard input"))
    ap.add_argument("--yes", action="store_true", help=_("accetta le risposte proposte senza chiedere"))
    return ap


def main(argv=None, con=None):
    i18n.set_language(os.environ.get("LC_ALL") or os.environ.get("LANG"))
    args = parser().parse_args(argv)
    con = con or Console(assume_yes=args.yes)
    con.say(banner())
    language = pick_language(con, args.language)
    i18n.set_language(language)
    if os.geteuid() != 0:
        con.say(_("Il wizard installa programmi e servizi: lancialo da root (sudo python3 wizard.py)."))
        return 1
    if not deps.has_systemd():
        con.say(_("Questo sistema non usa systemd: dobermann non è supportato qui."))
        return 1

    mode = args.where
    if mode is None:
        if is_proxmox_host():
            con.section(_("Dove installare"))
            mode = con.choose(">", [("ct", _("in un nuovo container LXC (consigliato)")),
                                    ("here", _("su questo host Proxmox"))], 0)
        else:
            mode = "here"
    plan = {"mode": mode, "language": language}
    if mode == "ct":
        plan["ct"] = plan_container(con, args)
    else:
        plan["local"] = plan_local(con, args)
    plan["mdns"] = not args.no_name and con.yes(
        _("Annunciare anche il nome {v} sulla rete di casa (oltre all'indirizzo)?", v=NAME))

    con.section(_("Pannello"))
    plan["https"] = not args.http and con.choose(">", [
        (True, _("HTTPS: password e dati cifrati in rete (consigliato)")),
        (False, _("HTTP: senza cifratura, solo se c'è già un reverse proxy davanti")),
    ], 0)

    if mode == "here" and not ask_dependencies(con, deps.wanted(https=plan["https"], mdns=plan["mdns"])):
        con.say(_("Mancano programmi indispensabili: installali e rilancia il wizard."))
        return 1

    con.section(_("Password del pannello"))
    if args.password_stdin:
        plan["password"] = sys.stdin.readline().rstrip("\n") or None
        if plan["password"] is not None and len(plan["password"]) < 8:
            con.say(_("la password deve avere almeno 8 caratteri"))
            return 1
    else:
        plan["password"] = con.password()

    summary(con, plan)
    if not con.yes(_("Procedo con l'installazione?")):
        con.say(_("Nulla è stato cambiato."))
        return 0
    con.say("")
    try:
        result, address = execute(con, plan, args)
    except (proxmox_ct.CtError, install.InstallError, subprocess.CalledProcessError, OSError) as e:
        con.say(_("ERRORE: {v}", v=e))
        return 1
    finish(con, plan, result, address)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (KeyboardInterrupt, EOFError):
        print()
        sys.exit(130)
