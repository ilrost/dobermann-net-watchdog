"""Command line for trying the adapter API against a real device.

    python3 -m dobermann probe 192.168.1.1                 # which adapters recognise it, then one sample
    python3 -m dobermann probe 192.168.2.1 --samples 3     # a few samples, 5 s apart
    python3 -m dobermann probe 192.168.2.1 --adapter zte_goform --password-file /etc/dobermann/zte.pw
    python3 -m dobermann facts                             # the fact vocabulary
    python3 -m dobermann run                               # the watchdog itself (what the service runs)
    python3 -m dobermann mail-test                         # send a test email with the current settings
    python3 -m dobermann web                               # the web panel (what the panel service runs)

A password is only ever read from a file (or typed at a hidden prompt with --ask-password), never
from the command line, where it would end up in the shell history and the process list.
"""
import argparse
import getpass
import json
import sys
import time

from dobermann import adapters, facts as factlib


def _print_detection(d):
    who = " ".join(x for x in (d.vendor, d.model) if x) or "?"
    fw = f", firmware {d.firmware}" if d.firmware else ""
    cls = adapters.get(d.adapter_id)
    kind = "standard" if cls.level == adapters.base.LEVEL_STANDARD else "NON ufficiale"
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

    print(f"Riconoscimento di {args.host}...")
    found = adapters.detect_all(args.host, timeout=args.timeout)
    for d in found:
        _print_detection(d)
    if not found:
        print("  nessun adattatore lo riconosce: resta sorvegliato solo dall'esterno (ping, percorso)")

    if args.adapter:
        cls = adapters.get(args.adapter)
        if cls is None:
            sys.exit(f"adattatore sconosciuto: {args.adapter} (disponibili: {', '.join(adapters.BY_ID)})")
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
        print(f"\nCampione {i + 1} con {cls.id} ({r.duration_s}s)" + (f", errore: {r.error}" if r.error else ""))
        print(json.dumps(r.facts, indent=2, ensure_ascii=False))
        if r.dropped:
            print(f"  fatti scartati (fuori contratto): {r.dropped}")
        st = adapter.status()
        if st:
            print(f"  stato adattatore: {st}")
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
    ok = notify.send(cfg["mail"], config.load_secrets(), "[dobermann] Mail di prova",
                     "Se leggi questo messaggio, le notifiche di dobermann arrivano.")
    print("inviata" if ok else "invio fallito (vedi sopra)")
    return 0 if ok else 1


def main(argv=None):
    p = argparse.ArgumentParser(prog="dobermann")
    sub = p.add_subparsers(dest="cmd", required=True)
    pr = sub.add_parser("probe", help="riconosce un apparato e ne legge i fatti")
    pr.add_argument("host")
    pr.add_argument("--adapter", help="usa questo adattatore invece del più probabile")
    pr.add_argument("--password-file")
    pr.add_argument("--ask-password", action="store_true")
    pr.add_argument("--samples", type=int, default=1)
    pr.add_argument("--interval", type=float, default=5.0)
    pr.add_argument("--timeout", type=float, default=3.0)
    pr.set_defaults(func=cmd_probe)
    sub.add_parser("facts", help="elenca il vocabolario dei fatti").set_defaults(func=cmd_facts)
    sub.add_parser("run", help="avvia il watchdog").set_defaults(func=cmd_run)
    sub.add_parser("web", help="avvia il pannello web").set_defaults(func=cmd_web)
    sub.add_parser("mail-test", help="invia una mail di prova").set_defaults(func=cmd_mail_test)
    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
