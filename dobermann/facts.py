"""The fact vocabulary: the common language every adapter translates its device into.

The diagnosis never talks to a router. It reads facts, and a fact means the same thing whatever
device produced it: `wan.link_up` from a FritzBox over UPnP and from a ZTE over its web API are the
same fact. An adapter may only emit names listed here (and declared in its own `provides`); anything
else is dropped by `validate()`, so a typo or a vendor-specific field can never leak into diagnosis.

A missing fact means UNKNOWN, never false. That is what lets diagnosis degrade on a router that
reports little: it reasons on what is there instead of reading "no data" as "broken".

Names are per device. When the core stores them it prefixes the node id from chain.json
(`modem.cell.rat`), so two devices of the same kind never collide.
"""
import ipaddress
import math
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Fact:
    kind: str                  # "bool" | "int" | "float" | "str" | "enum" | "ip"
    description: str
    unit: str = ""
    values: tuple = ()         # allowed values, for kind == "enum"


WAN_STATUS = ("connected", "connecting", "disconnected")
CELL_SERVICE = ("full", "limited", "none")
CELL_RAT = ("5G_SA", "5G_NSA", "LTE", "3G", "2G", "NONE")

FACTS = {
    # identity -- read once by the configurator, cheap enough to keep in every sample
    "device.vendor": Fact("str", "Produttore dell'apparato"),
    "device.model": Fact("str", "Modello dell'apparato"),
    "device.firmware": Fact("str", "Versione del firmware"),

    # the WAN side of a router: what it says about its own uplink
    "wan.status": Fact("enum", "Stato della connessione WAN", values=WAN_STATUS),
    "wan.link_up": Fact("bool", "Collegamento fisico WAN presente"),
    "wan.ip": Fact("ip", "Indirizzo IP della WAN del router"),
    "wan.uptime_s": Fact("int", "Da quanto è attiva la connessione WAN", "s"),
    "wan.last_error": Fact("str", "Ultimo errore di connessione riportato dal router"),
    "wan.rx_bytes": Fact("int", "Byte ricevuti dalla WAN (contatore, può ripartire da zero)", "B"),
    "wan.tx_bytes": Fact("int", "Byte inviati sulla WAN (contatore, può ripartire da zero)", "B"),
    "wan.max_down_bps": Fact("int", "Velocità massima del collegamento in ricezione", "bit/s"),
    "wan.max_up_bps": Fact("int", "Velocità massima del collegamento in invio", "bit/s"),

    # a cellular modem (built-in or a standalone 4G/5G CPE)
    "cell.service": Fact("enum", "Servizio della rete mobile", values=CELL_SERVICE),
    "cell.rat": Fact("enum", "Tecnologia radio in uso", values=CELL_RAT),
    "cell.rat_raw": Fact("str", "Tecnologia radio come la riporta l'apparato"),
    "cell.data_connected": Fact("bool", "Sessione dati con l'operatore attiva"),
    "cell.operator": Fact("str", "Operatore di rete mobile"),
    "cell.bars": Fact("int", "Tacche di segnale mostrate dall'apparato (0-5)"),
    "cell.rssi_dbm": Fact("float", "RSSI", "dBm"),
    "cell.lte_rsrp_dbm": Fact("float", "RSRP LTE", "dBm"),
    "cell.nr_rsrp_dbm": Fact("float", "RSRP 5G NR", "dBm"),
    "cell.sinr_db": Fact("float", "SINR", "dB"),
    "cell.cell_id": Fact("str", "Identificativo della cella servente"),
    "cell.band": Fact("str", "Banda in uso"),
    "cell.modem_state": Fact("str", "Stato interno del modem, come lo riporta l'apparato"),
}


BLANK = ("", "N/A", "n/a", "--", "null")


def is_blank(value):
    """What devices send for 'nothing here': no value, not a wrong one."""
    return value is None or (isinstance(value, str) and value.strip() in BLANK)


_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _coerce(fact, value):
    """The value as the fact's type, or None if it cannot be one."""
    if isinstance(value, str):
        value = value.strip()
    try:
        if fact.kind == "bool":
            if isinstance(value, bool):
                return value
            if isinstance(value, (int, float)):
                return bool(value)
            low = str(value).lower()
            if low in ("1", "true", "up", "yes"):
                return True
            if low in ("0", "false", "down", "no"):
                return False
            return None
        if fact.kind == "int":
            if isinstance(value, bool):
                return None
            f = float(value)
            return int(f) if math.isfinite(f) else None
        if fact.kind == "float":
            if isinstance(value, bool):
                return None
            f = float(value)
            return f if math.isfinite(f) else None      # NaN/inf would make every comparison lie
        if fact.kind == "ip":
            # shown in the panel and compared across samples: only a real address, never free text
            return str(ipaddress.ip_address(str(value)))
        if fact.kind == "enum":
            return value if value in fact.values else None
        # device text goes into the panel and the mails: no control characters from the device
        return _CONTROL.sub("", str(value)) or None
    except (TypeError, ValueError, OverflowError):
        return None


def validate(facts, allowed=None):
    """A clean copy of `facts`: only names in the vocabulary (and in `allowed`, when given), each
    coerced to its type; anything unknown, mistyped or empty is dropped. Returns (clean, dropped),
    where dropped lists the names removed, so a misbehaving adapter is visible in tests and logs."""
    clean, dropped = {}, []
    for name, value in (facts or {}).items():
        fact = FACTS.get(name)
        if fact is None or (allowed is not None and name not in allowed):
            dropped.append(name)
            continue
        if is_blank(value):
            continue
        v = _coerce(fact, value)
        if v is None:
            dropped.append(name)
            continue
        clean[name] = v
    return clean, dropped
