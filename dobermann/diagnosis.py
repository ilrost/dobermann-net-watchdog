"""From measurements and facts to a cause: per sample (classify) and per incident (close_cause).

The rules, their order and their thresholds were checked against months of real incidents,
including a carrier outage. They read facts from the chain, never vendor fields, so they work for
any router. A rule
whose facts are missing simply does not fire, and a less specific one further down does.
"""
import ipaddress
from dataclasses import dataclass, field

CAUSE_LABELS = {
    "GATEWAY_UNRESPONSIVE": "Il router non risponde sulla rete locale",
    "CELLULAR_NO_SERVICE": "Il modem mobile non ha servizio dall'operatore (nessuna cella utilizzabile)",
    "LOCAL_LINK_DOWN": "Il router ha perso il collegamento fisico verso l'esterno (cavo, linea o apparato a monte)",
    "WAN_DISCONNECTED": "Il router segnala la connessione a internet disconnessa",
    "IP_CHANGE_DROP": "Interruzione dopo un cambio di IP pubblico",
    "UPSTREAM_OUTAGE": "Internet irraggiungibile oltre il router (operatore o apparato a monte)",
    "DNS_FAILURE_LOCAL": "Il DNS locale non risponde (internet funziona)",
    "DNS_FAILURE_UPSTREAM": "Nessun server DNS risponde",
    "PACKET_LOSS": "Perdita di pacchetti verso internet",
    "HIGH_LATENCY": "Latenza alta verso internet",
    "IP_RENEWAL_CYCLE": "Riconnessione per rinnovo dell'IP pubblico (routine, non un guasto)",
    "CELLULAR_3G_FALLBACK": "Il modem mobile è ripiegato su 3G/2G (latenza o perdite da copertura)",
    "PING_ARTIFACT": "Artefatto di misura, non un guasto (solo nello storico importato)",
}

# Most fundamental first. When one continuous problem passes through several causes (link down,
# then WAN renegotiating, then the fresh IP settling), the incident's cause is the highest one
# seen: the root, of which the others are consequences.
CAUSE_PRIORITY = [
    "GATEWAY_UNRESPONSIVE", "CELLULAR_NO_SERVICE", "LOCAL_LINK_DOWN", "WAN_DISCONNECTED", "IP_CHANGE_DROP",
    "UPSTREAM_OUTAGE", "DNS_FAILURE_LOCAL", "DNS_FAILURE_UPSTREAM", "PACKET_LOSS", "HIGH_LATENCY",
]

# Which causes are worth an email, and when, is the user's choice per kind of event: see alerts.py.

# Derived from reachability statistics alone, which can flip on one borderline sample: they open an
# incident only after confirm_samples in a row. The others come from a state a device reported.
NOISY_CAUSES = {"UPSTREAM_OUTAGE", "DNS_FAILURE_LOCAL", "DNS_FAILURE_UPSTREAM", "PACKET_LOSS", "HIGH_LATENCY"}

NO_SERVICE = ("none", "limited")
LEGACY_RATS = ("3G", "2G")
RSRP_DROP_DB = 6          # a real handover or coverage change, not sample noise
SIGNAL_LOOKAHEAD_S = 90   # how long after recovery to look for the modem's first reading
IP_CHANGE_WINDOW_S = 120  # an outage this soon after an IP change is attributed to it
# An operator's IP renewal is short: on a real FWA line about half a minute, rarely over a minute,
# none between 2 and 5 minutes. One that "lasts" hours is an outage that happened to end on a new IP: past this, a renewal-shaped incident is not one.
RENEWAL_MAX_S = 5 * 60


@dataclass
class Observation:
    """One sample: what dobermann measured plus what the chain reported."""
    ts: float
    gw_loss: float
    gw_rtt: float = None
    anchors: dict = field(default_factory=dict)   # name -> {"ip", "loss", "rtt"}
    internet_loss: float = 100.0
    internet_rtt: float = None
    dns_local_ok: bool = None   # None: no local resolver to test
    dns_local_ms: float = None
    dns_ext_ok: bool = None     # None: no external resolver to test
    dns_ext_ms: float = None
    wan: dict = field(default_factory=dict)       # facts of the chain's WAN router
    cells: dict = field(default_factory=dict)     # node id -> facts, for cellular nodes
    fallback_lease: bool = False                  # WAN router got an address from the device above it
    fallback_upstream_cellular: bool = False
    ip_changed_recently: bool = False


def in_network(ip, network):
    try:
        return network is not None and ipaddress.ip_address(ip) in network
    except (TypeError, ValueError):
        return False


def no_cellular_service(obs):
    """A cellular modem says it has no usable service, or the router behind it holds a lease from the
    modem's own LAN. In IP passthrough the router normally gets the operator's address; when the
    modem loses the network it hands out a local one instead, which still tells the story when the modem itself cannot be read."""
    if any(f.get("cell.service") in NO_SERVICE for f in obs.cells.values()):
        return True
    return obs.fallback_lease and obs.fallback_upstream_cellular


def classify(obs, thresholds):
    internet_down = obs.internet_loss >= thresholds["internet_down_pct"]
    if obs.gw_loss >= 100:
        return "GATEWAY_UNRESPONSIVE"
    if internet_down and no_cellular_service(obs):
        # before the link/WAN states: a modem without service also drops its LAN port and
        # re-leases the router, and those are consequences here
        return "CELLULAR_NO_SERVICE"
    if internet_down and obs.wan.get("wan.link_up") is False:
        return "LOCAL_LINK_DOWN"
    if internet_down and obs.wan.get("wan.status") in ("disconnected", "connecting"):
        return "WAN_DISCONNECTED"
    if internet_down and obs.ip_changed_recently:
        return "IP_CHANGE_DROP"
    if internet_down:
        return "UPSTREAM_OUTAGE"
    # a resolver not configured (None) is not tested, so it is neither working nor failing
    if obs.dns_local_ok is False and obs.dns_ext_ok is not False:
        return "DNS_FAILURE_LOCAL"
    if obs.dns_ext_ok is False and obs.dns_local_ok is not True:
        return "DNS_FAILURE_UPSTREAM"
    if obs.internet_loss >= thresholds["packet_loss_pct"]:
        return "PACKET_LOSS"
    if obs.internet_rtt and obs.internet_rtt > thresholds["high_latency_ms"]:
        return "HIGH_LATENCY"
    return "OK"


def primary_cause(phases):
    def rank(cause):
        return CAUSE_PRIORITY.index(cause) if cause in CAUSE_PRIORITY else len(CAUSE_PRIORITY)
    return min((p["cause"] for p in phases), key=rank)


def span(phases):
    return phases[-1]["end_ts"] - phases[0]["start_ts"] if phases else 0


def dominant_cause(phases):
    """The cause that lasted longest; ties go to the more fundamental one."""
    totals = {}
    for p in phases:
        totals[p["cause"]] = totals.get(p["cause"], 0) + max(p["end_ts"] - p["start_ts"], 0)
    return max(totals, key=lambda c: (totals[c], -CAUSE_PRIORITY.index(c) if c in CAUSE_PRIORITY else -99))


def is_ip_renewal_cycle(phases, ip_before, ip_after):
    """A routine reconnect by the operator: a link/WAN renegotiation that ended on a genuinely new IP.
    GATEWAY_UNRESPONSIVE, CELLULAR_NO_SERVICE and UPSTREAM_OUTAGE are never routine, even when the
    IP changed along the way (a carrier outage can end on a new IP)."""
    if not ip_before or not ip_after or ip_before == ip_after:
        return False
    return primary_cause(phases) in ("LOCAL_LINK_DOWN", "WAN_DISCONNECTED", "IP_CHANGE_DROP")


def renewal_or_outage(phases):
    """For a renewal-shaped incident: IP_RENEWAL_CYCLE if short enough to be one, else what dominated it."""
    return "IP_RENEWAL_CYCLE" if span(phases) <= RENEWAL_MAX_S else dominant_cause(phases)


def signal_note(before, after):
    """Compares the modem's radio state just before an incident with its first reading after.
    `before`/`after` are cell facts or None. A clear drop or a technology change points at the radio
    side (handover, coverage); a stable signal at a session refresh by the operator."""
    if not before or not after:
        return None
    findings = []
    for key, name in (("cell.lte_rsrp_dbm", "LTE RSRP"), ("cell.nr_rsrp_dbm", "5G RSRP")):
        b, a = before.get(key), after.get(key)
        if b is not None and a is not None and b - a >= RSRP_DROP_DB:
            findings.append(f"{name} {b:.0f}->{a:.0f}dBm")
    bt, at = before.get("cell.rat_raw"), after.get("cell.rat_raw")
    if bt and at and bt != at:
        findings.append(f"modalità rete {bt}->{at}")
    if findings:
        return "possibile causa radio (cambio cella/handover): " + ", ".join(findings)
    if bt and at:
        return "segnale stabile prima e dopo: probabile rinnovo di sessione lato operatore, non un problema di copertura"
    return None


def legacy_rat(readings):
    """The 3G/2G technology the modem was on for most of `readings` (cell facts), or None."""
    if not readings:
        return None
    legacy = [r.get("cell.rat_raw") or r.get("cell.rat") for r in readings if r.get("cell.rat") in LEGACY_RATS]
    if 2 * len(legacy) < len(readings):
        return None
    return max(set(legacy), key=legacy.count)


SIGNAL_NOTE_CAUSES = ("CELLULAR_NO_SERVICE", "LOCAL_LINK_DOWN", "WAN_DISCONNECTED", "UPSTREAM_OUTAGE",
                      "IP_CHANGE_DROP", "IP_RENEWAL_CYCLE")


def close_cause(phases, ip_before, ip_after, cell_before=None, cell_after=None, cell_during=None):
    """(cause, note) for a finished incident."""
    cause = primary_cause(phases)
    if is_ip_renewal_cycle(phases, ip_before, ip_after):
        cause = renewal_or_outage(phases)
    note = signal_note(cell_before, cell_after) if cause in SIGNAL_NOTE_CAUSES else None
    if cause in ("HIGH_LATENCY", "PACKET_LOSS"):
        rat = legacy_rat(cell_during)
        if rat:
            cause = "CELLULAR_3G_FALLBACK"
            note = f"modem su rete {rat} (3G/2G) invece di 4G/5G: copertura, non un guasto locale"
    return cause, note
