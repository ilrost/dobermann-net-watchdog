"""Proposing the chain: which devices stand between dobermann and the internet, and how to read them.

A first, simple version of the configurator, used by the panel's
"Rileva" button:
  1. the default gateway is the first device;
  2. if the gateway reports its WAN address and that address is a private LAN address (not the
     operator's CGNAT range), there is another device above it: its LAN is probed at the usual
     first address of that /24;
  3. the user can add a device by address. That is the only way to find a modem in bridge or IP
     passthrough mode: such a device is invisible from below (the traceroute goes from the router straight
     to the operator).
Each device gets the adapter that recognises it with the most confidence, if any.
"""
import ipaddress
import re

from dobermann import adapters, probes

MIN_CONFIDENCE = 0.5
# RFC 1918 only: the operator's CGNAT (100.64/10) or a public address means nothing of ours is above
LAN_RANGES = [ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")]


def _node(ip, role, used_ids, timeout):
    found = adapters.detect_all(ip, timeout=timeout)
    best = found[0] if found and found[0].confidence >= MIN_CONFIDENCE else None
    base = "router" if role == "router" else "modem"
    nid, n = base, 2
    while nid in used_ids:
        nid, n = f"{base}{n}", n + 1
    used_ids.add(nid)
    label = device_label(best.vendor, best.model) if best else None
    label = label or ("Router" if role == "router" else "Apparato a monte")
    node = {"id": nid, "role": role, "label": label, "ip": ip}
    if best:
        node["adapter"], node["options"] = best.adapter_id, best.options
    return node, best, found


def device_label(vendor, model):
    """"FRITZ!Box 7530 AX" rather than "FRITZ! GmbH FRITZ!Box 7530 AX"; "ZTE MC888B" where the model alone
    would not say whose it is."""
    if not model:
        return vendor
    brand = re.sub(r"[^a-z0-9]", "", (vendor or "").split()[0].lower()) if vendor else ""
    if not brand or brand in re.sub(r"[^a-z0-9]", "", model.lower()):
        return model
    return f"{vendor.split()[0]} {model}"


def upstream_candidate(wan_ip):
    """The address to try for a device above a router whose WAN address is `wan_ip`, or None."""
    try:
        addr = ipaddress.ip_address(wan_ip)
    except (TypeError, ValueError):
        return None
    if addr.version != 4 or not any(addr in net for net in LAN_RANGES):
        return None
    return str(next(ipaddress.ip_network(f"{wan_ip}/24", strict=False).hosts()))


def propose_chain(extra_ips=(), timeout=3.0):
    """(chain dict, notes for the user)."""
    notes, used = [], set()
    gw = probes.default_gateway()
    if not gw:
        return {"version": 1, "nodes": []}, ["Nessun gateway predefinito su questa macchina: la rete non è configurata?"]
    nodes = []
    node, best, _ = _node(gw, "router", used, timeout)
    nodes.append(node)
    if best is None:
        notes.append(f"Il router {gw} non espone un'interfaccia che dobermann sa leggere (per esempio UPnP è "
                     "disattivato): sarà sorvegliato solo dall'esterno, con ping e misure verso internet.")
    tried = {gw}
    if best is not None and not extra_ips:
        r = adapters.run_sample(adapters.get(best.adapter_id)(gw, options=best.options, timeout=timeout))
        cand = upstream_candidate(r.facts.get("wan.ip"))
        if cand and cand not in tried:
            tried.add(cand)
            up, up_best, _ = _node(cand, "modem", used, timeout)
            if up_best is not None or probes.ping(cand, count=2)[0] < 100:
                nodes.append(up)
                notes.append(f"Il router ha un indirizzo WAN privato ({r.facts['wan.ip']}): c'è un apparato a monte, "
                             f"trovato a {cand}.")
    for ip in extra_ips:
        if ip in tried:
            continue
        tried.add(ip)
        up, up_best, _ = _node(ip, "modem", used, timeout)
        nodes.append(up)
        if up_best is None:
            notes.append(f"{ip}: nessun adattatore lo riconosce; resta nella catena senza dati propri.")
    if len(nodes) == 1:
        notes.append("Se fra il router e internet c'è un modem in modalità bridge o IP passthrough, dobermann non lo "
                     "vede da solo: aggiungine l'indirizzo.")
    return {"version": 1, "nodes": nodes}, notes
