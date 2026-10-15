"""Command line for trying the adapter API against a real device.

    python3 -m dobermann probe 192.168.1.1                 # which adapters recognise it, then one sample
    python3 -m dobermann probe 192.168.2.1 --samples 3     # a few samples, 5 s apart
    python3 -m dobermann probe 192.168.2.1 --adapter zte_goform --password-file /etc/dobermann/zte.pw
    python3 -m dobermann facts                             # the fact vocabulary
    python3 -m dobermann run                               # the watchdog itself (what the service runs)
    python3 -m dobermann mail-test                         # send a test email with the current settings
    python3 -m dobermann web                               # the web panel (what the panel service runs)
    python3 -m dobermann mdns dobermann.local              # announce a name on the LAN (the mdns service)
    python3 -m dobermann reset-password                    # choose a new panel password here
    python3 -m dobermann reset-password --code             # or clear it and get a code for the browser
    python3 -m dobermann update                            # install the newest version from GitHub (root)
    python3 -m dobermann update --check                    # only say whether there is one

A password is only ever read from a file (or typed at a hidden prompt with --ask-password), never
from the command line, where it would end up in the shell history and the process list.
"""
import argparse
import getpass
import json
import sys
import time

from dobermann import adapters, facts as factlib
from dobermann import i18n
from dobermann.i18n import _


def _print_detection(d):
    who = " ".join(x for x in (d.vendor, d.model) if x) or "?"
    fw = f", firmware {d.firmware}" if d.firmware else ""
    cls = adapters.get(d.adapter_id)
    kind = _("standard") if cls.level == adapters.base.LEVEL_STANDARD else _("NON ufficiale")
    print(f"  {d.adapter_id:<12} {d.confidence:.2f}  {who}{fw}  [{kind}]")
    for n in d.notes:
        print(f"               - {n}")


def cmd_probe(args):
    password = None
    if args.password_file:
        with open(args.password_file) as f:
            password = f.read().strip()
    elif args.ask_password:
        password = getpass.getpass("password: ")

    print(_("Riconoscimento di {v}...", v=args.host))
    found = adapters.detect_all(args.host, timeout=args.timeout)
    for d in found:
        _print_detection(d)
    if not found:
        print("  " + _("nessun adattatore lo riconosce: resta sorvegliato solo dall'esterno (ping, percorso)"))

    if args.adapter:
        cls = adapters.get(args.adapter)
        if cls is None:
            sys.exit(_("adattatore sconosciuto: {v} (disponibili: {all})", v=args.adapter, all=", ".join(adapters.BY_ID)))
        det = next((d for d in found if d.adapter_id == args.adapter), None)
    elif found:
        det = found[0]
        cls = adapters.get(det.adapter_id)
    else:
        return 1

    adapter = cls(args.host, credentials={"password": password} if password else None,
                  options=det.options if det else None, timeout=args.timeout)
    for i in range(args.samples):
        if i:
            time.sleep(args.interval)
        r = adapters.run_sample(adapter)
        print("\n" + _("Campione {n} con {a} ({s}s)", n=i + 1, a=cls.id, s=r.duration_s)
              + (", " + _("errore: {v}", v=r.error) if r.error else ""))
        print(json.dumps(r.facts, indent=2, ensure_ascii=False))
        if r.dropped:
            print("  " + _("fatti scartati (fuori contratto): {v}", v=r.dropped))
        st = adapter.status()
        if st:
            print("  " + _("stato adattatore: {v}", v=st))
    return 0 if r.ok else 1


def cmd_facts(_args):
    for name, f in factlib.FACTS.items():
        extra = f" [{f.unit}]" if f.unit else ""
        values = f" ({', '.join(f.values)})" if f.values else ""
        print(f"{name:<22} {f.kind:<5} {f.description}{extra}{values}")
    return 0


def cmd_run(_args):
    from dobermann.engine import Engine
    Engine().run()


def cmd_web(_args):
    from dobermann.web import server
    server.main()


def cmd_mail_test(_args):
    from dobermann import config, notify
    cfg = config.load()
    ok = notify.send(cfg["mail"], config.load_secrets(), "[dobermann] " + _("Mail di prova"),
                     _("Se leggi questo messaggio, le notifiche di dobermann arrivano."))
    print(_("inviata") if ok else _("invio fallito (vedi sopra)"))
    return 0 if ok else 1


def primary_address():
    """The IPv4 address this machine's default route leaves from, or None."""
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))      # no packet is sent: this only picks the route
            return s.getsockname()[0]
    except OSError:
        return None


def cmd_mdns(args):
    import os
    addr = primary_address()
    if not addr:
        sys.exit(_("nessun indirizzo IPv4 su cui annunciare {v}", v=args.name))
    os.execvp("avahi-publish-address", ["avahi-publish-address", "-R", args.name, addr])


def _chown_like_config_dir(path):
    import os
    from dobermann import config
    st = os.stat(config.config_dir())
    try:
        os.chown(path, st.st_uid, st.st_gid)
    except PermissionError:
        pass


def cmd_reset_password(args):
    import os
    from dobermann import config
    from dobermann.web import auth
    if args.code:
        try:
            os.unlink(os.path.join(config.config_dir(), "admin.json"))
        except FileNotFoundError:
            pass
        code = auth.new_setup_code()
        _chown_like_config_dir(os.path.join(config.config_dir(), "setup-code"))
        print(_("Password tolta. Apri il pannello e scegline una nuova con questo codice: {v}", v=code))
        return 0
    p1 = getpass.getpass(_("Nuova password del pannello (almeno 8 caratteri): "))
    if len(p1) < 8:
        sys.exit(_("la password deve avere almeno 8 caratteri"))
    if p1 != getpass.getpass(_("Ripeti: ")):
        sys.exit(_("le due password non coincidono"))
    auth.set_password(p1)
    auth.clear_setup_code()
    _chown_like_config_dir(os.path.join(config.config_dir(), "admin.json"))
    print(_("Password cambiata: gli altri browser collegati sono stati disconnessi."))
    return 0


def cmd_update(args):
    from dobermann import updates
    try:
        st = updates.check()
    except updates.UpdateError as e:
        print(_("ERRORE: {v}", v=e))
        return 1
    try:
        updates.save_status(dict(st, ts=time.time(), error=None))
    except OSError:
        pass
    if not st["newer"]:
        print(_("dobermann {v} è già l'ultima versione.", v=st["current"]))
        return 0
    print(_("Versione installata: {a}. Disponibile: {b}.", a=st["current"], b=st["latest"]))
    print(_("Cosa cambia: {v}", v=st["compare_url"]))
    if args.check:
        return 0
    try:
        answer = "y" if args.yes else input(_("Aggiorno adesso? [s/N] ")).strip().lower()
    except EOFError:      # no terminal to ask on (pct exec): --yes is how to say yes there
        answer = ""
    if answer not in ("s", "si", "sì", "y", "yes", "j", "ja", "o", "oui"):
        print(_("Nulla è stato cambiato."))
        return 0
    try:
        ok = updates.apply(st["latest"])
    except updates.UpdateError as e:
        print(_("ERRORE: {v}", v=e))
        return 1
    return 0 if ok else 1


def main(argv=None):
    # the language of the install, as for the panel and the mails
    try:
        from dobermann import config
        i18n.set_language(config.load().get("language"))
    except (OSError, ValueError):
        pass
    p = argparse.ArgumentParser(prog="dobermann")
    sub = p.add_subparsers(dest="cmd", required=True)
    pr = sub.add_parser("probe", help=_("riconosce un apparato e ne legge i fatti"))
    pr.add_argument("host")
    pr.add_argument("--adapter", help=_("usa questo adattatore invece del più probabile"))
    pr.add_argument("--password-file")
    pr.add_argument("--ask-password", action="store_true")
    pr.add_argument("--samples", type=int, default=1)
    pr.add_argument("--interval", type=float, default=5.0)
    pr.add_argument("--timeout", type=float, default=3.0)
    pr.set_defaults(func=cmd_probe)
    sub.add_parser("facts", help=_("elenca il vocabolario dei fatti")).set_defaults(func=cmd_facts)
    sub.add_parser("run", help=_("avvia il watchdog")).set_defaults(func=cmd_run)
    sub.add_parser("web", help=_("avvia il pannello web")).set_defaults(func=cmd_web)
    sub.add_parser("mail-test", help=_("invia una mail di prova")).set_defaults(func=cmd_mail_test)
    md = sub.add_parser("mdns", help=_("annuncia un nome .local sulla rete di casa"))
    md.add_argument("name")
    md.set_defaults(func=cmd_mdns)
    rp = sub.add_parser("reset-password", help=_("nuova password del pannello"))
    rp.add_argument("--code", action="store_true", help=_("toglie la password e stampa un codice per sceglierla dal browser"))
    rp.set_defaults(func=cmd_reset_password)
    up = sub.add_parser("update", help=_("installa l'ultima versione da GitHub (da root)"))
    up.add_argument("--check", action="store_true", help=_("dice solo se c'è una versione nuova"))
    up.add_argument("--yes", action="store_true", help=_("non chiede conferma"))
    up.set_defaults(func=cmd_update)
    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
