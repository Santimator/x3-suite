"""Experiment: does a CrossPoint reader announce itself when it enters File
Transfer, so the bot could just listen instead of pinging?

    python3 mdns_watch.py [--iface 192.168.1.10] [--minutes 30]

Stdlib only, listens and never sends to the network — except one unicast
`hello` to a reader it has just heard, to show the announcement is enough to
identify it. Run it on the bot's machine, then put each reader into File
Transfer (and out, and in again) and watch.

Why it should work: on joining a network the firmware calls `MDNS.begin
("crosspoint")` (CrossPointWebServerActivity.cpp, `restartMdns`), and an mDNS
responder starting up multicasts probes for its name and then unsolicited
announcements of it (RFC 6762 §8) to 224.0.0.251:5353. Whether they reach this
machine over the WiFi is what is being tested — multicast over WiFi is lossy.

Every minute it also prints how much mDNS the rest of the network already
sends, which is the yardstick for "is a ping every N seconds a lot".
"""
import argparse
import collections
import re
import socket
import struct
import sys
import time

GROUP, PORT = "224.0.0.251", 5353
_REPLY = re.compile(rb"^crosspoint \(on ([^)]*)\)")


def name_at(data: bytes, off: int) -> tuple:
    """A DNS name at `off`, following compression pointers: (name, next offset)."""
    labels, jumped, end = [], False, off
    for _ in range(64):
        n = data[off]
        if n & 0xC0 == 0xC0:
            if not jumped:
                end = off + 2
            off, jumped = ((n & 0x3F) << 8) | data[off + 1], True
            continue
        off += 1
        if n == 0:
            break
        labels.append(data[off:off + n].decode(errors="replace"))
        off += n
    return ".".join(labels), (end if jumped else off)


def parse(data: bytes) -> dict:
    """The parts of an mDNS packet worth looking at: is it a response, which
    names it asks about or states, and any A records (name -> IPv4)."""
    _, flags, qd, an, ns, ar = struct.unpack("!6H", data[:12])
    off, names, a = 12, [], {}
    for _ in range(qd):
        nm, off = name_at(data, off)
        names.append(nm)
        off += 4
    for _ in range(an + ns + ar):
        nm, off = name_at(data, off)
        rtype, _, _, rdlen = struct.unpack("!HHIH", data[off:off + 10])
        off += 10
        names.append(nm)
        if rtype == 1 and rdlen == 4:
            a[nm] = socket.inet_ntoa(data[off:off + 4])
        off += rdlen
    return {"response": bool(flags & 0x8000), "names": names, "a": a}


def hello(ip: str) -> str | None:
    """The firmware's discovery ping, unicast: the hostname, which carries the MAC."""
    for _ in range(3):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(1.0)
        try:
            s.sendto(b"hello", (ip, 8134))
            m = _REPLY.match(s.recvfrom(256)[0])
            if m:
                return m.group(1).decode(errors="replace")
        except OSError:
            pass
        finally:
            s.close()
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--iface", default="0.0.0.0",
                    help="this machine's LAN address, if it has more than one network")
    ap.add_argument("--minutes", type=float, default=30)
    args = ap.parse_args()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    try:
        sock.bind(("", PORT))
    except OSError as e:
        print(f"cannot listen on {PORT}: {e} (another mDNS daemon holding it "
              "without sharing?)", file=sys.stderr)
        return 1
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                    socket.inet_aton(GROUP) + socket.inet_aton(args.iface))
    sock.settimeout(1.0)

    stop = time.time() + args.minutes * 60
    minute, packets, hosts = time.time() + 60, 0, set()
    seen = {}                                  # ip -> last time we said hello
    print(f"listening on {GROUP}:{PORT} for {args.minutes:g} min — "
          "now put a reader into File Transfer", flush=True)
    while time.time() < stop:
        try:
            data, (src, _) = sock.recvfrom(9000)
        except socket.timeout:
            data = None
        now = time.time()
        if now >= minute:
            print(f"{time.strftime('%H:%M:%S')}  background: {packets} mDNS packets "
                  f"in the last minute, from {len(hosts)} hosts", flush=True)
            minute, packets, hosts = now + 60, 0, set()
        if not data:
            continue
        packets += 1
        hosts.add(src)
        try:
            p = parse(data)
        except (IndexError, struct.error):
            continue
        ours = [n for n in p["names"] if "crosspoint" in n.lower()]
        if not ours:
            continue
        kind = "announce" if p["response"] else "probe/query"
        ip = next((v for k, v in p["a"].items() if "crosspoint" in k.lower()), src)
        print(f"{time.strftime('%H:%M:%S')}  {kind:11} from {src}: "
              f"{', '.join(sorted(set(ours)))}  A={p['a'] or '-'}", flush=True)
        if p["response"] and now - seen.get(ip, 0) > 30:
            seen[ip] = now
            print(f"          hello {ip} -> {hello(ip)}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
