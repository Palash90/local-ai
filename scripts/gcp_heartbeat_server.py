#!/usr/bin/env python3
"""Heartbeat receiver + split-horizon DNS for the GCP VM.

One process, two jobs:

1. Heartbeat receiver (HTTP, default :9863)
   The homeserver's ConnectionManager (server/features/monitoring.py) POSTs
   its current addresses every 10 seconds over the WireGuard tunnel:

       {"ipv6":        "2405:201:...",   # stable global IPv6
        "public_ipv4": "49.37...",       # WAN IPv4 (NAT'd, via ipify)
        "wifi_ipv4":   "192.168.29.x"}   # LAN IPv4 on the WiFi interface

   GET /status returns the last payload as JSON.

2. Split-horizon DNS (UDP+TCP :53)
   Answers for home.palashkantikundu.in depend on WHO asks:
     - query source IP == homeserver's latest WAN IP(s)  ->  LAN IPv4 (+AAAA)
     - everyone else                                     ->  this VM's public IP
   so same-network clients connect directly over the LAN while remote clients
   go through the nginx/WireGuard tunnel. Every other name is forwarded to
   upstream resolvers — but only for known homeserver IPs (no open resolver).

Run on the VM (port 53 needs root or CAP_NET_BIND_SERVICE):

    sudo pip3 install dnslib
    sudo python3 gcp_heartbeat_server.py [--bind 10.66.66.1] [--port 9863] \
        [--dns-bind 0.0.0.0] [--dns-port 53] [--gcp-ip 35.212.x.x]

The VM's public IP is auto-detected from the GCP metadata server unless
--gcp-ip is given. Open udp/tcp 53 in the GCP firewall.
"""

import argparse
import ipaddress
import json
import os
import threading
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from dnslib import A, AAAA, QTYPE, RCODE, RR, SOA, DNSRecord
from dnslib.server import DNSServer, BaseResolver

ZONE = "home.palashkantikundu.in"
UPSTREAMS = ["1.1.1.1", "8.8.8.8"]
ZONE_TTL = 30          # short TTL so IP changes propagate fast
WG_PEER_IP = "10.66.66.3"

_lock = threading.Lock()
_latest = {}

# Recently seen WAN IPs of the homeserver (Jio rotates them).
_recent_public_ips = {}          # ip -> last seen timestamp
_RECENT_TTL = 48 * 3600
_RECENT_MAX = 10


def _log(message, log_file=None):
    line = f"{datetime.now().isoformat()} {message}"
    print(line, flush=True)
    if log_file:
        with open(log_file, "a") as fh:
            fh.write(line + "\n")


def _valid_ip(value, version):
    try:
        return str(ipaddress.ip_address(value)) if value else None
    except ValueError:
        return None


def _remember_public_ip(ip, now):
    _recent_public_ips[ip] = now
    stale = [k for k, ts in _recent_public_ips.items() if now - ts > _RECENT_TTL]
    for k in stale:
        del _recent_public_ips[k]
    while len(_recent_public_ips) > _RECENT_MAX:
        del _recent_public_ips[min(_recent_public_ips, key=lambda k: _recent_public_ips[k])]


# ---------------------------------------------------------------------------
# DNS
# ---------------------------------------------------------------------------

def _detect_gcp_ip():
    """Public IP of this VM via the GCP metadata server."""
    try:
        req = urllib.request.Request(
            "http://metadata.google.internal/computeMetadata/v1/instance/"
            "network-interfaces/0/access-configs/0/external-ip",
            headers={"Metadata-Flavor": "Google"},
        )
        return urllib.request.urlopen(req, timeout=2).read().decode().strip()
    except Exception:
        return None


class HomeResolver(BaseResolver):
    """Zone answers driven by heartbeat state; everything else forwarded."""

    def __init__(self, gcp_ip):
        self.gcp_ip = gcp_ip

    def _snapshot(self):
        with _lock:
            return (
                set(_recent_public_ips),
                _latest.get("wifi_ipv4"),
                _latest.get("ipv6"),
            )

    def _soa(self, reply):
        reply.add_auth(RR(
            ZONE, QTYPE.SOA,
            rdata=SOA(f"ns.{ZONE}", f"hostmaster.{ZONE}", (2024010101, 300, 60, 600, 30)),
            ttl=ZONE_TTL,
        ))

    def resolve(self, request, handler):
        client = handler.client_address[0]
        reply = request.reply()
        name = str(request.q.qname).rstrip(".").lower()
        qtype = QTYPE[request.q.qtype]

        local_ips, wifi_ip, server_v6 = self._snapshot()
        is_local_client = client in local_ips

        if name == ZONE:
            if qtype == "A":
                target = wifi_ip if (is_local_client and wifi_ip) else self.gcp_ip
                if target:
                    reply.add_answer(RR(request.q.qname, QTYPE.A,
                                        rdata=A(target), ttl=ZONE_TTL))
                else:
                    self._soa(reply)
            elif qtype == "AAAA":
                # Always hand out the home server's global IPv6 — local or remote,
                # it's directly reachable either way.
                if server_v6:
                    reply.add_answer(RR(request.q.qname, QTYPE.AAAA,
                                        rdata=AAAA(server_v6), ttl=ZONE_TTL))
                else:
                    self._soa(reply)
            else:
                self._soa(reply)
            return reply

        # Recursion only for the homeserver itself (no open resolver).
        if not (is_local_client or client == WG_PEER_IP):
            reply.header.rcode = RCODE.REFUSED
            return reply

        try:
            return DNSRecord.parse(request.send(self._upstream(), 53, timeout=3))
        except Exception:
            reply.header.rcode = RCODE.SERVFAIL
            return reply

    @staticmethod
    def _upstream():
        return UPSTREAMS[0]


# ---------------------------------------------------------------------------
# Heartbeat HTTP receiver
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/heartbeat":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            self.send_error(400, "invalid JSON")
            return

        record = {
            "ipv6": _valid_ip(data.get("ipv6"), 6),
            "public_ipv4": _valid_ip(data.get("public_ipv4"), 4),
            "wifi_ipv4": _valid_ip(data.get("wifi_ipv4"), 4),
            "remote_addr": self.client_address[0],
            "received_at": datetime.now(timezone.utc).isoformat(),
        }
        with _lock:
            _latest.clear()
            _latest.update(record)

        if record["public_ipv4"]:
            now = datetime.now(timezone.utc).timestamp()
            with _lock:
                _remember_public_ip(record["public_ipv4"], now)

        _log(f"heartbeat from {record['remote_addr']}: "
             f"v6={record['ipv6']} pub_v4={record['public_ipv4']} wifi_v4={record['wifi_ipv4']}",
             self.server.log_file)
        self.send_response(200)
        self.end_headers()

    def do_GET(self):
        if self.path != "/status":
            self.send_error(404)
            return
        with _lock:
            body = json.dumps(_latest).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass  # request lines are logged by _log() instead


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bind", default="10.66.66.1",
                        help="heartbeat listener address (default: WireGuard IP)")
    parser.add_argument("--port", type=int, default=9863)
    parser.add_argument("--log", default=os.path.expanduser("~/heartbeat.log"),
                        help="append received heartbeats to this file")
    parser.add_argument("--dns-bind", default="0.0.0.0",
                        help="DNS listener address (default: all interfaces)")
    parser.add_argument("--dns-port", type=int, default=53,
                        help="DNS port, 0 disables the DNS server")
    parser.add_argument("--gcp-ip", default=None,
                        help="this VM's public IPv4 (default: auto-detect)")
    args = parser.parse_args()

    gcp_ip = args.gcp_ip or _detect_gcp_ip()

    if args.dns_port:
        resolver = HomeResolver(gcp_ip)
        udp = DNSServer(resolver, port=args.dns_port, address=args.dns_bind)
        tcp = DNSServer(resolver, port=args.dns_port, address=args.dns_bind, tcp=True)
        udp.start_thread()
        tcp.start_thread()
        if not udp.isAlive() or not tcp.isAlive():
            raise SystemExit(f"could not bind DNS on {args.dns_bind}:{args.dns_port}")
        print(f"DNS listening on {args.dns_bind}:{args.dns_port} "
              f"(zone={ZONE}, tunnel_ip={gcp_ip})", flush=True)

    httpd = ThreadingHTTPServer((args.bind, args.port), Handler)
    httpd.log_file = args.log
    print(f"Heartbeat receiver listening on http://{args.bind}:{args.port}", flush=True)
    try:
        httpd.serve_forever()
    finally:
        if args.dns_port:
            udp.stop()
            tcp.stop()


if __name__ == "__main__":
    main()
