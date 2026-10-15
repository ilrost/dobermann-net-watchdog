"""Changing the address dobermann is reached at.

The panel runs without privileges, so it cannot change this machine's network itself (in a Proxmox
container it could not anyway: the host owns the container's network and rewrites it on every start).
What it does: everything that is its own (the HTTPS certificate, the link in the mails, the address
it listens on) and, for the network, the exact commands to run where they belong, for the platform it
is on. Nothing here runs a command as root.
"""
import glob
import ipaddress
import os
import shlex
import shutil
import subprocess

from dobermann.i18n import _


def machine_address():
    """The IPv4 address this machine's default route leaves from, or None."""
    from dobermann.__main__ import primary_address
    return primary_address()


def _default_route():
    try:
        return subprocess.run(["ip", "-4", "route", "show", "default"], capture_output=True, text=True,
                              timeout=5).stdout.split()
    except (OSError, subprocess.SubprocessError):
        return []


def default_gateway():
    parts = _default_route()
    return parts[parts.index("via") + 1] if "via" in parts else None


def default_device():
    """The network interface of the default route (eth0, ens18, ...), or None."""
    parts = _default_route()
    return parts[parts.index("dev") + 1] if "dev" in parts else None


def interface_mac(ifname="eth0"):
    try:
        with open(f"/sys/class/net/{ifname}/address") as f:
            return f.read().strip().upper()
    except OSError:
        return None


def parse_new(ip_text, gw_text):
    """(interface, gateway) from the form, or raises ValueError with a message for the user."""
    ip_text, gw_text = (ip_text or "").strip(), (gw_text or "").strip()
    try:
        iface = ipaddress.ip_interface(ip_text)
    except ValueError:
        raise ValueError(_("serve un indirizzo con la maschera, es. 192.168.1.50/24")) from None
    if "/" not in ip_text or iface.version != 4:
        raise ValueError(_("serve un indirizzo IPv4 con la maschera, es. {v}/24", v=ip_text.split("/")[0]))
    try:
        gw = ipaddress.ip_address(gw_text)
    except ValueError:
        raise ValueError(_("gateway non valido: {v!r}", v=gw_text)) from None
    if gw not in iface.network:
        raise ValueError(_("il gateway {gw} non è nella rete {net}", gw=gw, net=iface.network))
    if iface.ip in (iface.network.network_address, iface.network.broadcast_address):
        raise ValueError(_("{v} non è un indirizzo utilizzabile in {net}", v=iface.ip, net=iface.network))
    if gw == iface.ip:
        raise ValueError(_("il gateway non può essere l'indirizzo stesso"))
    return iface, gw


def _active_nm_connection(device=None):
    """(connection name, device) of NetworkManager's connection on the default route, or None."""
    if not shutil.which("nmcli"):
        return None
    device = device or default_device()
    try:
        out = subprocess.run(["nmcli", "-t", "-f", "NAME,DEVICE", "connection", "show", "--active"],
                             capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    found = []
    for line in out.stdout.splitlines():
        name, _sep, dev = line.rpartition(":")
        if name and dev and dev != "lo":
            found.append((name.replace("\\:", ":"), dev))
    for name, dev in found:
        if dev == device:
            return name, dev
    return found[0] if found and device is None else None


def _ifupdown_has(device, path="/etc/network/interfaces"):
    """True if the interfaces file really configures `device` (on netplan or networkd systems the file
    often exists with nothing but the loopback)."""
    try:
        with open(path) as f:
            return any(ln.split()[:2] == ["iface", device] for ln in f if ln.strip())
    except OSError:
        return False


def detect_platform(cfg, nm=_active_nm_connection, device=None, has_ifupdown=_ifupdown_has,
                    netplan=lambda: glob.glob("/etc/netplan/*.yaml"),
                    networkd=lambda: glob.glob("/etc/systemd/network/*.network")):
    """What manages this machine's network: proxmox-lxc (from the installer's record), networkmanager,
    netplan, networkd, ifupdown, or unknown. Each with the interface of the default route."""
    p = cfg.get("platform") or {}
    if p.get("kind") == "proxmox-lxc" and p.get("ctid"):
        return dict(p)
    device = device or default_device() or "eth0"
    found = nm()
    if found:
        return {"kind": "networkmanager", "connection": found[0], "device": found[1]}
    if netplan():
        return {"kind": "netplan", "device": device}
    if networkd():
        return {"kind": "networkd", "device": device}
    if has_ifupdown(device):
        return {"kind": "ifupdown", "device": device}
    return {"kind": "unknown", "device": device}


def commands(platform, iface, gw, mac=None, extra_restarts=()):
    """(where, [command lines]) to run for the new address."""
    kind = platform.get("kind")
    restarts = [f"systemctl restart {s}" for s in extra_restarts]
    if kind == "proxmox-lxc":
        opts = ["name=eth0", f"bridge={platform.get('bridge') or 'vmbr0'}", "firewall=1", f"gw={gw}"]
        if mac:
            opts.append(f"hwaddr={mac}")      # keep the MAC: a DHCP reservation or a router rule may use it
        opts += [f"ip={iface.with_prefixlen}", "type=veth"]
        return (_("sull'host Proxmox, come root"),
                [f"pct set {platform['ctid']} -net0 {','.join(opts)}", f"pct reboot {platform['ctid']}"])
    dev = platform.get("device") or "eth0"
    if kind == "networkmanager":
        con = shlex.quote(platform["connection"])     # a connection name is text anyone could have chosen
        return (_("su questa macchina, come root"),
                [f"nmcli connection modify {con} ipv4.method manual ipv4.addresses {iface.with_prefixlen} "
                 f"ipv4.gateway {gw}", f"nmcli connection up {con}"] + restarts)
    if kind == "netplan":
        return (_("su questa macchina, come root: nel file di netplan in /etc/netplan, alla voce dell'interfaccia {v}",
                  v=dev),
                [f"{dev}:", "  dhcp4: false", f"  addresses: [{iface.with_prefixlen}]",
                 "  routes:", f"    - to: default", f"      via: {gw}", "# " + _("poi:"), "netplan apply"] + restarts)
    if kind == "ifupdown":
        return (_("su questa macchina, come root: in /etc/network/interfaces, nella sezione dell'interfaccia di rete"),
                [f"iface {dev} inet static", f"    address {iface.with_prefixlen}", f"    gateway {gw}",
                 "# " + _("poi:"), "systemctl restart networking"] + restarts)
    if kind == "networkd":
        return (_("su questa macchina, come root: nel file .network dell'interfaccia, in /etc/systemd/network"),
                ["[Network]", f"Address={iface.with_prefixlen}", f"Gateway={gw}",
                 "# " + _("poi:"), "networkctl reload"] + restarts)
    return (_("nella configurazione di rete di questa macchina (non riconosco chi la gestisce)"),
            [f"address {iface.with_prefixlen}", f"gateway {gw}"] + restarts)


def cert_names(old_ip, new_ip, name):
    """The certificate must answer for the new address and still for the old one until the change."""
    out = []
    for v in (str(new_ip), old_ip, name):
        if v and v not in out:
            out.append(v)
    return out
