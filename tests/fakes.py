"""Small in-process fakes of real devices, so adapter tests never touch a real network."""
import hashlib
import json
import socket
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class _Server:
    def __init__(self, handler_cls):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        self.httpd.fake = self
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def closed_port():
    """A localhost port nothing listens on."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# --- ZTE goform ---------------------------------------------------------------------------------

class FakeZte(_Server):
    """One admin session at a time, like the real router: a new login invalidates the previous one."""

    PUBLIC = {"model_name": "MC888B", "wa_inner_version": "FW1", "network_provider_fullname": "Operatore",
              "ppp_status": "ppp_connected", "network_type": "ENDC", "mc_modem_main_state": "modem_init_complete",
              "signalbar": "4", "lte_rssi": "-61", "network_lte_rsrp": "-96", "Z5g_rsrp": "-97"}
    PRIVATE = {"wan_ipaddr": "100.64.1.2", "network_cell_id": "cella-1", "lte_snr": "7", "Z5g_SINR": "12",
               "network_lte_ca_pcell_band": "B3", "network_Z5g_CELLINFO_band": "n78"}

    def __init__(self, password="pw#1"):
        self.password = password
        self.public = dict(self.PUBLIC)
        self.session = None
        self.ld = None
        self.logins = 0
        self.counter = 0
        super().__init__(_ZteHandler)

    def owner_logs_in(self):
        self.session = "owner"


class _ZteHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, cookie=None):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        if cookie:
            self.send_header("Set-Cookie", f"stok={cookie}; Path=/")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _token(self):
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "stok":
                return v
        return None

    def do_GET(self):
        fake = self.server.fake
        url = urllib.parse.urlsplit(self.path)
        if url.path != "/goform/goform_get_cmd_process":
            self.send_error(404)
            return
        cmd = urllib.parse.parse_qs(url.query).get("cmd", [""])[0].split(",")
        if cmd == ["LD"]:
            fake.counter += 1
            fake.ld = hashlib.sha256(str(fake.counter).encode()).hexdigest().upper()
            self._json({"LD": fake.ld})
            return
        logged = fake.session is not None and self._token() == fake.session
        out = {}
        for c in cmd:
            if c == "loginfo":
                out[c] = "ok" if logged else ""
            elif c in fake.public:
                out[c] = fake.public[c]
            else:
                out[c] = fake.PRIVATE.get(c, "") if logged else ""
        self._json(out)

    def do_POST(self):
        fake = self.server.fake
        n = int(self.headers.get("Content-Length") or 0)
        form = urllib.parse.parse_qs(self.rfile.read(n).decode())
        if form.get("goformId") != ["LOGIN"]:
            self._json({"result": "failure"})
            return
        fake.logins += 1
        ad = hashlib.sha256(fake.password.encode()).hexdigest().upper()
        expected = hashlib.sha256((ad + (fake.ld or "")).encode()).hexdigest().upper()
        if form.get("password") == [expected]:
            fake.session = f"tok{fake.logins}"
            self._json({"result": "0"}, cookie=fake.session)
        else:
            self._json({"result": "3"})


# --- UPnP IGD -----------------------------------------------------------------------------------

DESCRIPTION = """<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
 <device>
  <deviceType>urn:schemas-upnp-org:device:InternetGatewayDevice:1</deviceType>
  <manufacturer>ACME</manufacturer><modelName>Router 9000</modelName>
  <deviceList><device>
   <serviceList><service>
    <serviceType>urn:schemas-upnp-org:service:WANCommonInterfaceConfig:1</serviceType>
    <controlURL>/ctl/common</controlURL>
   </service></serviceList>
   <deviceList><device><serviceList><service>
    <serviceType>urn:schemas-upnp-org:service:WANIPConnection:1</serviceType>
    <controlURL>/ctl/ip</controlURL>
   </service></serviceList></device></deviceList>
  </device></deviceList>
 </device>
</root>"""


class FakeIgd(_Server):
    def __init__(self):
        self.state = {"NewConnectionStatus": "Connected", "NewLastConnectionError": "ERROR_NONE",
                      "NewUptime": "1234", "NewExternalIPAddress": "203.0.113.7",
                      "NewPhysicalLinkStatus": "Up", "NewLayer1DownstreamMaxBitRate": "1000000000",
                      "NewLayer1UpstreamMaxBitRate": "200000000", "NewTotalBytesReceived": "5000",
                      "NewTotalBytesSent": "700"}
        self.calls = []
        super().__init__(_IgdHandler)
        self.ssdp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.ssdp.bind(("127.0.0.1", 0))
        self.ssdp_port = self.ssdp.getsockname()[1]
        threading.Thread(target=self._ssdp_loop, daemon=True).start()

    def _ssdp_loop(self):
        while True:
            try:
                data, addr = self.ssdp.recvfrom(2048)
            except OSError:
                return
            if b"M-SEARCH" in data:
                reply = (f"HTTP/1.1 200 OK\r\nLOCATION: http://127.0.0.1:{self.port}/desc.xml\r\n"
                         "ST: urn:schemas-upnp-org:device:InternetGatewayDevice:1\r\n\r\n").encode()
                self.ssdp.sendto(reply, addr)

    def close(self):
        self.ssdp.close()
        super().close()


ACTIONS = {
    "GetStatusInfo": ("NewConnectionStatus", "NewLastConnectionError", "NewUptime"),
    "GetExternalIPAddress": ("NewExternalIPAddress",),
    "GetCommonLinkProperties": ("NewPhysicalLinkStatus", "NewLayer1DownstreamMaxBitRate",
                                "NewLayer1UpstreamMaxBitRate"),
    "GetTotalBytesReceived": ("NewTotalBytesReceived",),
    "GetTotalBytesSent": ("NewTotalBytesSent",),
}


class _IgdHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="text/xml"):
        body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/desc.xml":
            self._send(200, DESCRIPTION)
        else:
            self._send(404, "")

    def do_POST(self):
        fake = self.server.fake
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        stype, _, action = self.headers.get("SOAPAction", "").strip('"').partition("#")
        fake.calls.append(action)
        if action not in ACTIONS:
            self._send(500, "")
            return
        args = "".join(f"<{k}>{fake.state[k]}</{k}>" for k in ACTIONS[action])
        self._send(200, '<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
                        f'<s:Body><u:{action}Response xmlns:u="{stype}">{args}</u:{action}Response>'
                        '</s:Body></s:Envelope>')
