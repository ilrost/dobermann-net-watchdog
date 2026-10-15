"""Where the panel listens and whether it speaks HTTPS.

  DOBERMANN_BIND       address to listen on (default 0.0.0.0; 127.0.0.1 behind a reverse proxy)
  DOBERMANN_PORT       HTTP port (default 80)
  DOBERMANN_TLS_PORT   HTTPS port, used only when TLS is on (default 443)
  tls.crt + tls.key    in the configuration directory. Both present: HTTPS on, and the HTTP port only
                       redirects. Only one present: refuse to start, rather than silently serve plain
                       HTTP to someone who meant to have HTTPS.
"""
import http.server
import ipaddress
import os
import re
import ssl

DEFAULT_BIND = "0.0.0.0"
DEFAULT_HTTP_PORT = 80
DEFAULT_TLS_PORT = 443
HANDSHAKE_TIMEOUT_S = 10
_HOST_OK = re.compile(r"^(\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9.-]+)(:\d{1,5})?$")


class ListenError(Exception):
    pass


def _port(env, name, default):
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ListenError(f"{name}={raw!r} non è un numero") from None
    if not 1 <= value <= 65535:
        raise ListenError(f"{name}={value} è fuori da 1-65535")
    return value


def resolve(cert_path, key_path, env=os.environ, exists=os.path.exists):
    """dict(bind, http_port, tls, tls_port, cert, key). Raises ListenError on anything ambiguous."""
    bind = (env.get("DOBERMANN_BIND") or "").strip() or DEFAULT_BIND
    try:
        ipaddress.ip_address(bind)
    except ValueError:
        raise ListenError(f"DOBERMANN_BIND={bind!r} non è un indirizzo IP") from None
    http_port = _port(env, "DOBERMANN_PORT", DEFAULT_HTTP_PORT)
    tls_port = _port(env, "DOBERMANN_TLS_PORT", DEFAULT_TLS_PORT)
    have_cert, have_key = exists(cert_path), exists(key_path)
    if have_cert != have_key:
        raise ListenError(f"per HTTPS servono entrambi i file, ne esiste uno solo: manca "
                          f"{key_path if have_cert else cert_path}")
    tls = have_cert and have_key
    if tls and http_port == tls_port:
        raise ListenError(f"le porte HTTP e HTTPS sono entrambe {http_port}")
    return {"bind": bind, "http_port": http_port, "tls": tls, "tls_port": tls_port, "cert": cert_path, "key": key_path}


def make_ssl_context(cert_path, key_path):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    try:
        ctx.load_cert_chain(cert_path, key_path)
    except (ssl.SSLError, OSError) as e:
        raise ListenError(f"impossibile caricare il certificato HTTPS {cert_path} / chiave {key_path}: {e}") from e
    return ctx


def host_for_redirect(host_header):
    """The host part of the request's Host header, or None if it is not a plain hostname / IPv4 / [IPv6]:
    it comes from the client and must never go into a Location header unchecked."""
    host_header = (host_header or "").strip()
    if not _HOST_OK.match(host_header):
        return None
    if host_header.startswith("["):
        return host_header[:host_header.index("]") + 1]
    return host_header.split(":")[0]


class TLSServer(http.server.ThreadingHTTPServer):
    """The TLS handshake happens in the per-connection thread, not in accept(): a client that connects
    and says nothing must not stall everyone else."""
    daemon_threads = True

    def __init__(self, address, handler, ctx):
        super().__init__(address, handler)
        self.ctx = ctx

    def finish_request(self, request, client_address):
        try:
            request.settimeout(HANDSHAKE_TIMEOUT_S)
            conn = self.ctx.wrap_socket(request, server_side=True)
        except (ssl.SSLError, OSError):
            return
        try:
            conn.settimeout(None)
            self.RequestHandlerClass(conn, client_address, self)
        finally:
            try:
                conn.close()
            except OSError:
                pass
