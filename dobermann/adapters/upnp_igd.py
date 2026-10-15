"""Any router that speaks UPnP IGD (Internet Gateway Device), the closest thing to a common interface
home routers have. Read-only standard calls, usually without a password: WAN connection state,
external IP, uptime, last error, physical link, line speed, byte counters.

Nothing vendor-specific here: the control URLs come from the router's own device description,
found by SSDP discovery. Verified on a FRITZ!Box 7530 AX, which only answers SSDP sent
to the multicast address, not to its own IP; both are tried.

Some routers ship with UPnP turned off, and some operators disable it. Then this adapter simply does
not detect the router, and dobermann falls back to what it measures from outside.
"""
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

from dobermann.adapters.base import LEVEL_STANDARD, Adapter, Detection

SSDP_ADDR = ("239.255.255.250", 1900)
IGD_TYPES = ("urn:schemas-upnp-org:device:InternetGatewayDevice:1",
             "urn:schemas-upnp-org:device:InternetGatewayDevice:2")
CONN_PREFIXES = ("urn:schemas-upnp-org:service:WANIPConnection:",
                 "urn:schemas-upnp-org:service:WANPPPConnection:")
COMMON_PREFIX = "urn:schemas-upnp-org:service:WANCommonInterfaceConfig:"
# description URLs known to exist when SSDP is blocked or filtered; tried last
KNOWN_DESCRIPTIONS = ("http://{host}:49000/igddesc.xml",)   # AVM FRITZ!Box / FRITZ!Repeater

_STATUS = {"connected": "connected", "connecting": "connecting", "pendingdisconnect": "connecting",
           "disconnected": "disconnected", "disconnecting": "disconnected",
           "unconfigured": "disconnected"}
# anything but Up counts as down ("Initializing" while a line trains)
_LINK = {"up": True, "down": False, "initializing": False, "unavailable": False}


def ssdp_locations(host, timeout=2.0, ssdp_addr=SSDP_ADDR):
    """Description URLs that `host` announces for an IGD. Asks on the multicast group and directly,
    and keeps only answers coming from `host`."""
    found = []
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    except OSError:
        return found
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        sock.settimeout(0.3)
        for st in IGD_TYPES:
            msg = (f"M-SEARCH * HTTP/1.1\r\nHOST: {ssdp_addr[0]}:{ssdp_addr[1]}\r\n"
                   f'MAN: "ssdp:discover"\r\nMX: 1\r\nST: {st}\r\n\r\n').encode()
            for dest in (ssdp_addr, (host, ssdp_addr[1])):
                try:
                    sock.sendto(msg, dest)
                except OSError:
                    pass
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                data, addr = sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            if addr[0] != host:
                continue
            m = re.search(r"^location:\s*(\S+)", data.decode("utf-8", "replace"), re.I | re.M)
            if m and m.group(1) not in found:
                found.append(m.group(1))
    finally:
        sock.close()
    return found


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def parse_description(xml_text, location):
    """{vendor, model, conn_type, conn_url, common_type, common_url} from an IGD description, or None
    when it describes no WAN connection service."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None
    base = None
    for el in root.iter():
        if _local(el.tag) == "URLBase" and (el.text or "").strip():
            base = el.text.strip()
    if not base:
        p = urllib.parse.urlsplit(location)
        base = f"{p.scheme}://{p.netloc}/"
    info = {"vendor": None, "model": None}
    for el in root.iter():
        if _local(el.tag) == "device":
            for child in el:
                if _local(child.tag) == "manufacturer" and info["vendor"] is None:
                    info["vendor"] = (child.text or "").strip() or None
                if _local(child.tag) == "modelName" and info["model"] is None:
                    info["model"] = (child.text or "").strip() or None
            break
    for el in root.iter():
        if _local(el.tag) != "service":
            continue
        fields = {_local(c.tag): (c.text or "").strip() for c in el}
        stype, curl = fields.get("serviceType", ""), fields.get("controlURL", "")
        if not curl:
            continue
        url = urllib.parse.urljoin(base, curl)
        if stype.startswith(CONN_PREFIXES) and "conn_url" not in info:
            info["conn_type"], info["conn_url"] = stype, url
        elif stype.startswith(COMMON_PREFIX) and "common_url" not in info:
            info["common_type"], info["common_url"] = stype, url
    return info if ("conn_url" in info or "common_url" in info) else None


def _fetch(url, timeout):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def soap(url, service_type, action, timeout=4.0):
    """{name: value} of the response arguments, or None on any failure."""
    body = ('<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
            f'<u:{escape(action)} xmlns:u="{escape(service_type)}"/></s:Body></s:Envelope>').encode()
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": 'text/xml; charset="utf-8"', "SOAPAction": f'"{service_type}#{action}"'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            root = ET.fromstring(r.read())
    except (urllib.error.URLError, OSError, ET.ParseError, ValueError):
        return None
    for el in root.iter():
        if _local(el.tag) == f"{action}Response":
            return {_local(c.tag): (c.text or "").strip() for c in el}
    return None


def discover(host, timeout=3.0, ssdp_addr=SSDP_ADDR, known=KNOWN_DESCRIPTIONS):
    """(location, parsed description) for the first IGD description `host` serves, or (None, None)."""
    candidates = ssdp_locations(host, timeout=min(timeout, 2.0), ssdp_addr=ssdp_addr)
    candidates += [k.format(host=host) for k in known if k.format(host=host) not in candidates]
    for loc in candidates:
        try:
            info = parse_description(_fetch(loc, timeout), loc)
        except (urllib.error.URLError, OSError, ValueError):
            continue
        if info:
            return loc, info
    return None, None


class UpnpIgd(Adapter):
    id = "upnp_igd"
    name = "Router UPnP (standard)"
    level = LEVEL_STANDARD
    provides = frozenset({
        "device.vendor", "device.model",
        "wan.status", "wan.link_up", "wan.ip", "wan.uptime_s", "wan.last_error",
        "wan.rx_bytes", "wan.tx_bytes", "wan.max_down_bps", "wan.max_up_bps",
    })

    @classmethod
    def detect(cls, host, timeout=3.0, ssdp_addr=SSDP_ADDR, known=KNOWN_DESCRIPTIONS):
        loc, info = discover(host, timeout, ssdp_addr, known)
        if not info:
            return None
        notes = []
        confidence = 0.95 if info.get("conn_url") else 0.7
        if info.get("conn_url"):
            st = soap(info["conn_url"], info["conn_type"], "GetStatusInfo", timeout)
            if st is None:
                confidence = 0.6
                notes.append("Descrizione UPnP presente, ma il router non risponde alle richieste di stato.")
        if info.get("common_url"):
            rx = soap(info["common_url"], info["common_type"], "GetTotalBytesReceived", timeout) or {}
            link = soap(info["common_url"], info["common_type"], "GetCommonLinkProperties", timeout) or {}
            if rx.get("NewTotalBytesReceived") == "0" and link.get("NewLayer1DownstreamMaxBitRate") in ("0", None):
                # e.g. a FRITZ!Repeater: same UPnP stack as the router, but no WAN of its own
                confidence = min(confidence, 0.3)
                notes.append("Espone UPnP ma non ha traffico WAN: probabilmente non è il router verso "
                             "internet (es. repeater o access point).")
        options = {"location": loc, **info}
        return Detection(cls.id, confidence, vendor=info.get("vendor"), model=info.get("model"),
                         notes=notes, options=options)

    def __init__(self, host, credentials=None, options=None, timeout=4.0):
        super().__init__(host, credentials, options, timeout)
        self._info = None
        if self.options.get("conn_url") or self.options.get("common_url"):
            self._info = dict(self.options)
        self._vendor, self._model = self.options.get("vendor"), self.options.get("model")

    def _ensure(self):
        if self._info is None:
            loc, info = discover(self.host, self.timeout,
                                 known=([self.options["location"]] if self.options.get("location") else ())
                                 + KNOWN_DESCRIPTIONS)
            if info:
                self._vendor, self._model = info.get("vendor"), info.get("model")
                self._info = {"location": loc, **info}
        return self._info

    def sample(self):
        info = self._ensure()
        if not info:
            return {}
        facts = {"device.vendor": self._vendor, "device.model": self._model}
        answered = False
        if info.get("conn_url"):
            st = soap(info["conn_url"], info["conn_type"], "GetStatusInfo", self.timeout)
            if st is not None:
                answered = True
                facts["wan.status"] = _STATUS.get(st.get("NewConnectionStatus", "").lower())
                err = st.get("NewLastConnectionError")
                facts["wan.last_error"] = None if err in (None, "", "ERROR_NONE") else err
                facts["wan.uptime_s"] = st.get("NewUptime")
            ip = soap(info["conn_url"], info["conn_type"], "GetExternalIPAddress", self.timeout)
            if ip is not None:
                answered = True
                addr = ip.get("NewExternalIPAddress")
                facts["wan.ip"] = None if addr in (None, "", "0.0.0.0") else addr
        if info.get("common_url"):
            link = soap(info["common_url"], info["common_type"], "GetCommonLinkProperties", self.timeout)
            if link is not None:
                answered = True
                facts["wan.link_up"] = _LINK.get(link.get("NewPhysicalLinkStatus", "").lower())
                facts["wan.max_down_bps"] = link.get("NewLayer1DownstreamMaxBitRate")
                facts["wan.max_up_bps"] = link.get("NewLayer1UpstreamMaxBitRate")
            for action, key, fact in (("GetTotalBytesReceived", "NewTotalBytesReceived", "wan.rx_bytes"),
                                      ("GetTotalBytesSent", "NewTotalBytesSent", "wan.tx_bytes")):
                r = soap(info["common_url"], info["common_type"], action, self.timeout)
                if r is not None:
                    answered = True
                    facts[fact] = r.get(key)
        if not answered:
            self._info = None   # the router may have moved its UPnP port (reboot): discover again
            return {}
        return facts
