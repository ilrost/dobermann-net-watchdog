#!/usr/bin/env python3
"""Create a self-signed HTTPS certificate for the web panel.

    python3 tools/make_cert.py                       # names/addresses detected from this machine
    python3 tools/make_cert.py --san dobermann.lan --san 192.168.1.50
    python3 tools/make_cert.py --force               # replace an existing certificate

Writes tls.crt (0644) and tls.key (0600) into the configuration directory (DOBERMANN_CONFIG_DIR,
default /etc/dobermann), owned by whoever owns that directory (the service user), so that the panel can
read the key even when this is run as root. The panel switches to HTTPS the next time it starts and
both files exist (systemctl restart dobermann-web); deleting them switches it back. Needs the `openssl` binary (there is no certificate generation in the Python
standard library).

Browsers will warn about a self-signed certificate once per device: compare the SHA-256 fingerprint
printed here with the one the browser shows, then accept. To avoid the warning altogether put a
reverse proxy with your own CA in front instead.

Valid 825 days, RSA 2048, with subjectAltName for every name/address given (modern browsers ignore
the common name). It is a leaf certificate (CA:FALSE, serverAuth only), so trusting it on a client does
not let it vouch for any other site. Stdlib only.
"""
import argparse
import ipaddress
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dobermann import config, i18n  # noqa: E402
from dobermann.i18n import _  # noqa: E402

DAYS = 825
_NAME_OK = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")


class CertError(Exception):
    pass


def detect_names():
    """This machine's hostname plus its non-loopback IPv4 addresses (from `ip`, when available)."""
    found = []
    host = socket.gethostname()
    if host:
        found.append(host)
    try:
        out = subprocess.run(["ip", "-4", "-o", "addr", "show"], capture_output=True, text=True, timeout=5).stdout
        for m in re.finditer(r"inet (\d+\.\d+\.\d+\.\d+)/", out):
            if not m.group(1).startswith("127."):
                found.append(m.group(1))
    except (OSError, subprocess.SubprocessError):
        pass
    return found


def san_entries(names):
    """['IP:1.2.3.4', 'DNS:host', ...] with each name validated: a value that is neither an IP address
    nor a plain hostname is rejected (it is going onto a command line)."""
    entries, seen = [], set()
    for raw in names:
        name = raw.strip()
        if not name or name in seen:
            continue
        seen.add(name)
        try:
            entries.append(f"IP:{ipaddress.ip_address(name)}")
        except ValueError:
            if not _NAME_OK.match(name):
                raise CertError(_("non è un nome o un indirizzo IP valido: {v}", v=repr(raw))) from None
            entries.append(f"DNS:{name}")
    if not entries:
        raise CertError(_("nessun nome o indirizzo da mettere nel certificato: usa --san"))
    return entries


def generate(cert_path, key_path, sans, run=subprocess.run):
    openssl = shutil.which("openssl")
    if not openssl:
        raise CertError(_("manca `openssl` (pacchetto openssl): serve per creare il certificato"))
    os.makedirs(os.path.dirname(cert_path), exist_ok=True)
    cn = next((s.split(":", 1)[1] for s in sans if s.startswith("DNS:")), sans[0].split(":", 1)[1])
    # write next to the destination, then rename: a failure never leaves a certificate without its key
    tmp_dir = tempfile.mkdtemp(dir=os.path.dirname(cert_path), prefix=".tls-")
    try:
        tmp_key, tmp_crt = os.path.join(tmp_dir, "key"), os.path.join(tmp_dir, "crt")
        res = run([openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-sha256", "-days", str(DAYS),
                   "-keyout", tmp_key, "-out", tmp_crt, "-subj", f"/CN={cn}/O=dobermann",
                   "-addext", "subjectAltName=" + ",".join(sans),
                   # a LEAF, not an authority: `openssl req -x509` defaults to CA:TRUE, and a certificate a user
                   # imports as trusted (Windows Root store, browsers) must not be able to vouch for other sites
                   "-addext", "basicConstraints=critical,CA:FALSE",
                   "-addext", "keyUsage=critical,digitalSignature,keyEncipherment",
                   "-addext", "extendedKeyUsage=serverAuth"],
                  capture_output=True, text=True, timeout=60)
        if res.returncode != 0:
            raise CertError(_("openssl non è riuscito: {v}", v=(res.stderr or res.stdout).strip().splitlines()[-1]))
        os.chmod(tmp_key, 0o600)
        os.chmod(tmp_crt, 0o644)
        owner = os.stat(os.path.dirname(cert_path))
        if os.geteuid() == 0:
            for p in (tmp_key, tmp_crt):
                os.chown(p, owner.st_uid, owner.st_gid)
        os.replace(tmp_key, key_path)
        os.replace(tmp_crt, cert_path)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def fingerprint(cert_path):
    openssl = shutil.which("openssl")
    res = subprocess.run([openssl, "x509", "-in", cert_path, "-noout", "-fingerprint", "-sha256"],
                         capture_output=True, text=True, timeout=10)
    return res.stdout.strip().split("=", 1)[-1] if res.returncode == 0 else "?"


def main(argv=None):
    i18n.set_language(os.environ.get("LC_ALL") or os.environ.get("LANG"))
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--san", action="append", default=[],
                    help=_("un nome o un indirizzo IP per cui il certificato deve valere (ripetibile; predefinito: quelli rilevati)"))
    ap.add_argument("--force", action="store_true", help=_("sostituisce certificato e chiave esistenti"))
    args = ap.parse_args(argv)
    cert, key = os.path.join(config.config_dir(), "tls.crt"), os.path.join(config.config_dir(), "tls.key")
    try:
        if (os.path.exists(cert) or os.path.exists(key)) and not args.force:
            raise CertError(_("{a} o {b} esiste già: usa --force per sostituirli", a=cert, b=key))
        sans = san_entries(args.san or detect_names())
        generate(cert, key, sans)
    except CertError as e:
        print(_("ERRORE:"), e, file=sys.stderr)
        return 1
    print(_("certificato: {v}", v=cert))
    print(_("chiave     : {v} (0600)", v=key))
    print(_("valido per : {v} ({n} giorni)", v=", ".join(sans), n=DAYS))
    print(f"SHA-256    : {fingerprint(cert)}")
    print("\n" + _("Riavvia il pannello per passare al nuovo certificato (systemctl restart dobermann-web)."))
    return 0


if __name__ == "__main__":
    sys.exit(main())
