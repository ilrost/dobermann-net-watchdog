"""The chain of devices between dobermann and the internet (chain.json), and sampling it.

    {"version": 1, "nodes": [
      {"id": "router", "role": "router", "label": "FRITZ!Box 7530 AX", "ip": "192.168.1.1",
       "adapter": "upnp_igd", "options": {...}},
      {"id": "modem", "role": "modem", "label": "ZTE MC888B", "ip": "192.168.2.1",
       "adapter": "zte_goform", "options": {...}, "lan_prefix": 24}
    ]}

Nodes are in order from dobermann outwards: the first one is the LAN gateway. A node without an
adapter is still part of the chain (dobermann knows it is there, and pings the gateway); it just
reports no facts. With no chain configured at all, the system's default gateway is the chain.
Passwords for a node live in secrets.json under "devices" -> node id, never in chain.json.
"""
import ipaddress
from concurrent.futures import ThreadPoolExecutor

from dobermann import adapters, probes

ROLES = ("router", "modem", "access", "isp_edge")


class Node:
    def __init__(self, spec, secrets=None):
        self.id = spec["id"]
        self.role = spec.get("role", "router")
        self.label = spec.get("label") or self.id
        self.ip = spec.get("ip")
        self.lan_prefix = int(spec.get("lan_prefix", 24))
        self.adapter_id = spec.get("adapter")
        self.adapter = None
        cls = adapters.get(self.adapter_id) if self.adapter_id else None
        if cls is not None and self.ip:
            self.adapter = cls(self.ip, credentials=(secrets or {}).get(self.id),
                               options=spec.get("options"))

    @property
    def provides(self):
        return self.adapter.provides if self.adapter else frozenset()

    def lan_network(self):
        """The LAN this device serves, assumed /lan_prefix around its own address."""
        try:
            return ipaddress.ip_network(f"{self.ip}/{self.lan_prefix}", strict=False)
        except (TypeError, ValueError):
            return None


class Chain:
    def __init__(self, chain_spec, secrets=None):
        devices = (secrets or {}).get("devices", {})
        self.nodes = [Node(n, devices) for n in (chain_spec or {}).get("nodes", [])]
        if not self.nodes:
            gw = probes.default_gateway()
            if gw:
                self.nodes = [Node({"id": "gateway", "role": "router", "label": "Router", "ip": gw})]

    @property
    def gateway(self):
        return self.nodes[0] if self.nodes else None

    def sample(self):
        """{node id: SampleResult} for every node with an adapter, read in parallel so one slow
        device cannot delay the others."""
        with_adapter = [n for n in self.nodes if n.adapter]
        if not with_adapter:
            return {}
        with ThreadPoolExecutor(max_workers=len(with_adapter)) as pool:
            results = pool.map(lambda n: adapters.run_sample(n.adapter), with_adapter)
            return {n.id: r for n, r in zip(with_adapter, results)}

    def wan_node(self, results):
        """The first node, outwards from dobermann, that reported its WAN state: the router whose
        WAN is the household's way out."""
        for n in self.nodes:
            r = results.get(n.id)
            if r and any(k.startswith("wan.") for k in r.facts):
                return n
        return None

    def upstream_of(self, node):
        i = self.nodes.index(node)
        return self.nodes[i + 1] if i + 1 < len(self.nodes) else None

    def uplink(self):
        """The device closest to the internet: the last node of the chain (nodes go outwards)."""
        return self.nodes[-1] if self.nodes else None

    def radio_uplink(self):
        """The uplink, when it reaches the internet over a mobile network (FWA, a 4G/5G router or modem);
        None for a wired line (fibre, DSL, cable). Decided by what its adapter can report, not by what
        it happens to answer now, so a modem that stops answering during an outage is still radio."""
        u = self.uplink()
        return u if u is not None and "cell.service" in u.provides else None

    def link_kind(self):
        return "radio" if self.radio_uplink() else "wired"

    def link_description(self):
        r = self.radio_uplink()
        return f"radio (rete mobile, tramite {r.label})" if r else "cablata (fibra, DSL o cavo)"

    def cellular_nodes(self):
        """Devices whose radio state explains the line's events: only the radio uplink. A radio device
        that is not the way out (an LTE backup on a fibre router) would only add noise."""
        r = self.radio_uplink()
        return [r] if r else []
