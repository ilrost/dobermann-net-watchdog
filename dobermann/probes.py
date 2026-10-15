"""Level 1 probes: what dobermann measures itself, on any network, without credentials.

ping and the DNS query have run for months of 5-second samples; the parsing notes below are
what that taught.
"""
import os
import random
import re
import socket
import struct
import subprocess
import time

# Loss comes from the transmitted/received COUNTERS, not from the printed percentage: current
# iputils prints a fractional loss ("33.3333% packet loss" for 1 of 3), which an integer regex
# misread as 3333 and clamped to 100, so every partial loss became total loss.
PING_RE_COUNTS = re.compile(r"(\d+) packets transmitted, (\d+) (?:packets )?received")
PING_RE_LOSS = re.compile(r"([\d.]+)% packet loss")  # fallback only, when the counters are missing
PING_RE_RTT = re.compile(r"= [\d.]+/([\d.]+)/")


def parse_ping_output(out):
    """(loss_pct, rtt_ms or None) from iputils ping's summary."""
    counts = PING_RE_COUNTS.search(out)
    if counts:
        transmitted, received = int(counts.group(1)), int(counts.group(2))
        if transmitted == 0:
            return 100.0, None
        loss = (transmitted - received) * 100.0 / transmitted
    else:
        received = None
        loss_m = PING_RE_LOSS.search(out)
        try:
            loss = float(loss_m.group(1)) if loss_m else 100.0
        except ValueError:
            loss = 100.0
    loss = max(0.0, min(100.0, loss))
    rtt_m = PING_RE_RTT.search(out)
    rtt = float(rtt_m.group(1)) if rtt_m else None
    # a late duplicate reply is folded into the rtt statistics but does not count as received:
    # with nothing received, a timing must never read as "reachable"
    if received == 0 or loss >= 100.0:
        rtt = None
    return loss, rtt


def ping(host, count=3, timeout=1):
    try:
        out = subprocess.run(
            ["ping", "-c", str(count), "-W", str(timeout), "-i", "0.2", host],
            capture_output=True, text=True, timeout=count * timeout + 3,
            # the summary is parsed as English: under another locale every host would read as lost
            env={**os.environ, "LC_ALL": "C"},
        ).stdout
    except Exception:
        return 100.0, None
    return parse_ping_output(out)


def dns_query(server, domain, timeout=2):
    """A minimal A-record query over UDP. (ok, elapsed_ms or None)."""
    if not server:
        return False, None
    txid = random.getrandbits(16)
    header = struct.pack(">HHHHHH", txid, 0x0100, 1, 0, 0, 0)
    labels = [p for p in domain.rstrip(".").split(".") if p]     # "example.com." is example.com
    qname = b"".join(bytes([len(p)]) + p.encode() for p in labels) + b"\x00"
    packet = header + qname + struct.pack(">HH", 1, 1)  # type A, class IN
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    t0 = time.monotonic()
    try:
        s.connect((server, 53))         # the kernel then drops datagrams from anyone else
        s.send(packet)
        data = s.recv(512)
        elapsed = (time.monotonic() - t0) * 1000
        # an answer (QR bit) to this question (random id), not something that just looks like one
        if len(data) >= 12 and data[0:2] == struct.pack(">H", txid) and data[2] & 0x80:
            ancount = struct.unpack(">H", data[6:8])[0]
            rcode = struct.unpack(">H", data[2:4])[0] & 0x000F
            return (rcode == 0 and ancount > 0), elapsed
        return False, elapsed
    except Exception:
        return False, None
    finally:
        s.close()


def default_gateway():
    """The IPv4 default gateway from the kernel's routing table, or None."""
    try:
        with open("/proc/net/route") as f:
            next(f)
            for line in f:
                parts = line.split()
                if len(parts) >= 3 and parts[1] == "00000000" and int(parts[3], 16) & 2:
                    return socket.inet_ntoa(struct.pack("<L", int(parts[2], 16)))
    except (OSError, ValueError, StopIteration):
        pass
    return None
