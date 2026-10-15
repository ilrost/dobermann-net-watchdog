"""ZTE 4G/5G routers (CPE) through their web interface's own API, `goform`.

UNOFFICIAL (level 3): the API is undocumented and may change with a firmware update. Verified on a
ZTE MC888B, firmware BD_STDPLMC888BV1.0.0B08. The same goform API appears across
other ZTE CPE models; that is an assumption until someone tries one.

Two modes:
  * WITHOUT LOGIN (default, no password). The router answers a subset of fields to anyone on its
    LAN: data session up/down, radio technology (5G/LTE/3G/NO_SERVICE...), signal (RSSI/RSRP),
    bars, operator, model, firmware. That is enough to tell "the modem lost the mobile network"
    and "the modem fell back to 3G", and it never touches the admin session.
  * WITH LOGIN (optional password). Adds cell id, band, SINR and the WAN IP. The router allows ONE
    admin session: our login pushes the user out of the web page, and the user logging in pushes
    us out. When our session disappears we take that as "someone is using the web page" and stop
    logging in for `yield_s` seconds (default 30 min), staying on the no-login fields meanwhile.
    A rejected password stops login attempts for `bad_password_s` (default 1 h), so a wrong
    password cannot trip a lockout on the router.

Login scheme (captured from the router's own page): GET cmd=LD returns a fresh
challenge; password field = SHA256(SHA256(password).upper() + LD).upper().
"""
import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar

from dobermann.adapters.base import LEVEL_UNOFFICIAL, Adapter, Detection

GET_PATH = "/goform/goform_get_cmd_process"
SET_PATH = "/goform/goform_set_cmd_process"

# answered without login (verified on the MC888B)
PUBLIC_FIELDS = [
    "model_name", "wa_inner_version", "network_provider_fullname",
    "ppp_status", "network_type", "mc_modem_main_state", "signalbar",
    "lte_rssi", "network_lte_rsrp", "Z5g_rsrp",
]
# empty unless logged in
PRIVATE_FIELDS = [
    "loginfo", "wan_ipaddr", "network_cell_id", "lte_snr", "Z5g_SINR",
    "network_lte_ca_pcell_band", "network_Z5g_CELLINFO_band",
]

# network_type as this firmware reports it -> (service, rat). Values recorded on the MC888B in a
# month of readings: ENDC, LTE-NSA, HSPA+, HSPA, LIMITED_SERVICE_LTE/WCDMA/SA, NO_SERVICE. LTE-NSA is
# LTE on a 5G-capable anchor with no 5G leg in use. The 5G SA spellings are an assumption (no SA
# cell here), as are the other LTE/3G/2G spellings.
_RAT = {
    "ENDC": "5G_NSA", "EN-DC": "5G_NSA",
    "SA": "5G_SA", "NR": "5G_SA", "NR5G": "5G_SA", "5G": "5G_SA",
    "LTE": "LTE", "LTE-NSA": "LTE", "LTE-A": "LTE", "LTE+": "LTE", "LTE_CA": "LTE", "4G": "LTE", "4G+": "LTE",
    "HSPA": "3G", "HSPA+": "3G", "DC-HSPA+": "3G", "HSDPA": "3G", "HSUPA": "3G",
    "WCDMA": "3G", "UMTS": "3G", "3G": "3G",
    "EDGE": "2G", "GPRS": "2G", "GSM": "2G", "2G": "2G",
}
_PPP = {"ppp_connected": True, "ppp_disconnected": False, "ppp_connecting": False,
        "ppp_disconnecting": False}


def classify_network_type(raw):
    """(cell.service, cell.rat) for a network_type string; None where it cannot tell."""
    if not raw:
        return None, None
    up = raw.strip().upper()
    if up.startswith("NO_SERVICE"):
        return "none", "NONE"
    if up.startswith("LIMITED_SERVICE"):
        return "limited", "NONE"
    rat = _RAT.get(up)
    return ("full", rat) if rat else (None, None)


def to_facts(fields):
    """Facts from one goform answer (with or without login)."""
    f = fields or {}
    service, rat = classify_network_type(f.get("network_type"))
    bands = [b for b in (f.get("network_lte_ca_pcell_band"), f.get("network_Z5g_CELLINFO_band")) if b]
    return {
        "device.vendor": "ZTE",
        "device.model": f.get("model_name"),
        "device.firmware": f.get("wa_inner_version"),
        "cell.service": service,
        "cell.rat": rat,
        "cell.rat_raw": f.get("network_type"),
        "cell.data_connected": _PPP.get(f.get("ppp_status")),
        "cell.operator": f.get("network_provider_fullname"),
        "cell.bars": f.get("signalbar"),
        "cell.rssi_dbm": f.get("lte_rssi"),
        "cell.lte_rsrp_dbm": f.get("network_lte_rsrp"),
        "cell.nr_rsrp_dbm": f.get("Z5g_rsrp"),
        "cell.modem_state": f.get("mc_modem_main_state"),
        "cell.sinr_db": f.get("Z5g_SINR") or f.get("lte_snr"),
        "cell.cell_id": f.get("network_cell_id"),
        "cell.band": " + ".join(bands) or None,
        "wan.ip": f.get("wan_ipaddr"),
    }


class ZteGoform(Adapter):
    id = "zte_goform"
    name = "ZTE 4G/5G (interfaccia web, non ufficiale)"
    level = LEVEL_UNOFFICIAL
    accepts_credentials = True
    single_session = True
    provides = frozenset({
        "device.vendor", "device.model", "device.firmware",
        "cell.service", "cell.rat", "cell.rat_raw", "cell.data_connected", "cell.operator",
        "cell.bars", "cell.rssi_dbm", "cell.lte_rsrp_dbm", "cell.nr_rsrp_dbm", "cell.modem_state",
        "cell.sinr_db", "cell.cell_id", "cell.band", "wan.ip",
    })

    YIELD_S = 1800
    BAD_PASSWORD_S = 3600

    def __init__(self, host, credentials=None, options=None, timeout=4.0, clock=time.monotonic):
        super().__init__(host, credentials, options, timeout)
        self.base = (self.options.get("base_url") or f"http://{host}").rstrip("/")
        self.yield_s = self.options.get("yield_s", self.YIELD_S)
        self.bad_password_s = self.options.get("bad_password_s", self.BAD_PASSWORD_S)
        self._clock = clock
        self._opener = None          # logged-in session, or None
        self._paused_until = 0.0
        self._pause_reason = None

    # -- HTTP -----------------------------------------------------------------------------------

    @staticmethod
    def _headers(base):
        return {"Referer": f"{base}/index.html", "X-Requested-With": "XMLHttpRequest",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"}

    @classmethod
    def _get(cls, base, fields, opener=None, timeout=4.0):
        q = urllib.parse.urlencode({"multi_data": "1", "isTest": "false", "cmd": ",".join(fields),
                                    "_": str(int(time.time() * 1000))})
        req = urllib.request.Request(f"{base}{GET_PATH}?{q}", headers=cls._headers(base))
        with (opener or urllib.request.build_opener()).open(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        if not isinstance(data, dict):
            raise ValueError("risposta goform inattesa")
        return data

    # -- detection ------------------------------------------------------------------------------

    @classmethod
    def detect(cls, host, timeout=3.0, base_url=None):
        base = (base_url or f"http://{host}").rstrip("/")
        try:
            data = cls._get(base, ["model_name", "wa_inner_version", "network_type", "ppp_status"],
                            timeout=timeout)
        except (urllib.error.URLError, OSError, ValueError):
            return None
        if "network_type" not in data:
            return None
        model, fw = data.get("model_name") or None, data.get("wa_inner_version") or None
        confidence = 0.9 if (model or data.get("network_type")) else 0.6
        notes = ["Interfaccia non ufficiale: può smettere di funzionare dopo un aggiornamento del firmware.",
                 "Senza password legge servizio, tecnologia radio e segnale. Con la password aggiunge "
                 "cella, banda e SINR, ma il router ammette un solo accesso: dobermann si ritira quando "
                 "entri tu nella pagina del router."]
        return Detection(cls.id, confidence, vendor="ZTE", model=model, firmware=fw, notes=notes,
                         options={"base_url": base})

    # -- login ----------------------------------------------------------------------------------

    def _password(self):
        return (self.credentials.get("password") or "").strip()

    def _paused(self):
        return self._clock() < self._paused_until

    def _pause(self, seconds, reason):
        self._opener = None
        self._paused_until = self._clock() + seconds
        self._pause_reason = reason

    def _fetch_ld(self, opener):
        """The login challenge. Seen both as {"LD": "<hex>"} and as a bare string."""
        q = urllib.parse.urlencode({"isTest": "false", "cmd": "LD", "_": str(int(time.time() * 1000))})
        req = urllib.request.Request(f"{self.base}{GET_PATH}?{q}", headers=self._headers(self.base))
        with opener.open(req, timeout=self.timeout) as r:
            raw = r.read().decode("utf-8", "replace").strip()
        try:
            data = json.loads(raw)
        except ValueError:
            return raw.strip('"')
        return (data.get("LD") or "") if isinstance(data, dict) else str(data)

    def _login(self):
        """True if logged in. A rejected password pauses logins; a network error does not."""
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
        try:
            ld = self._fetch_ld(opener)
            if not ld:
                return False
            ad = hashlib.sha256(self._password().encode()).hexdigest().upper()
            body = urllib.parse.urlencode({
                "isTest": "false", "goformId": "LOGIN",
                "password": hashlib.sha256((ad + ld).encode()).hexdigest().upper(),
            }).encode()
            req = urllib.request.Request(f"{self.base}{SET_PATH}", data=body, method="POST",
                                         headers=self._headers(self.base))
            with opener.open(req, timeout=self.timeout) as r:
                result = json.loads(r.read().decode("utf-8", "replace")).get("result")
        except (urllib.error.URLError, OSError, ValueError, AttributeError):
            return False
        if str(result) == "0":
            self._opener = opener
            self._pause_reason = None
            return True
        self._pause(self.bad_password_s, f"password rifiutata dal router (codice {result})")
        return False

    # -- sampling -------------------------------------------------------------------------------

    def sample(self):
        want_login = bool(self._password()) and not self._paused()
        if want_login and self._opener is None:
            self._login()
        if self._opener is not None:
            try:
                data = self._get(self.base, PUBLIC_FIELDS + PRIVATE_FIELDS, self._opener, self.timeout)
            except (urllib.error.URLError, OSError, ValueError):
                self._opener = None   # router unreachable; log in again next time, no pause
                return {}
            if data.get("loginfo") == "ok":
                return to_facts(data)
            # our session is gone: the user most likely opened the router's web page
            self._pause(self.yield_s, "sessione lasciata al proprietario (accesso alla pagina del router)")
            return to_facts({k: data.get(k) for k in PUBLIC_FIELDS})
        try:
            return to_facts(self._get(self.base, PUBLIC_FIELDS, timeout=self.timeout))
        except (urllib.error.URLError, OSError, ValueError):
            return {}

    def status(self):
        if not self._password():
            return {"modalità": "senza login"}
        if self._paused():
            left = int(self._paused_until - self._clock())
            return {"modalità": "senza login (in pausa)", "motivo": self._pause_reason,
                    "riprova tra": f"{left // 60} min"}
        return {"modalità": "con login" if self._opener else "con login (non ancora collegato)"}
