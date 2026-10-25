#!/usr/bin/env python3
"""Fake-cloud probe: does the Hisense AC talk TLS to a server it shouldn't trust?

Runs four things at once:
  - DNS server on :53. Queries under hijacked domains (default: hijuconn.com)
    get answered with this machine's LAN IP. Everything else is forwarded to
    the real resolver and every query is logged, so we learn exactly which
    hostnames the AC asks for.
  - Plain TCP listeners on :80 and :1883 (HTTP / MQTT).
  - TLS listeners on :443, :8883 and :8443 presenting a self-signed cert.
    The outcome of the handshake is the whole experiment:
      handshake completes  -> firmware does NOT validate the cert. We win.
      handshake fails/alert -> firmware validates. Fake-cloud route is dead.

Everything is timestamped to stdout and to probe.log next to this script.

Needs root for the low ports, e.g.:
  sudo ../.venv/bin/python probe.py --bind 192.168.31.100
"""

import argparse
import datetime
import socket
import socketserver
import ssl
import sys
import threading
from pathlib import Path

from dnslib import QTYPE, RR, A, DNSHeader, DNSRecord

LOG_FILE = Path(__file__).with_name("probe.log")
CERT_FILE = Path(__file__).with_name("server.crt")
KEY_FILE = Path(__file__).with_name("server.key")

_log_lock = threading.Lock()


def log(msg):
    line = f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S.%f}"[:-3] + "  " + msg
    with _log_lock:
        print(line, flush=True)
        with LOG_FILE.open("a") as f:
            f.write(line + "\n")


def hexdump(data, limit=1024):
    shown = data[:limit]
    out = []
    for off in range(0, len(shown), 16):
        chunk = shown[off:off + 16]
        hexs = " ".join(f"{b:02x}" for b in chunk)
        text = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        out.append(f"    {off:04x}  {hexs:<47}  {text}")
    if len(data) > limit:
        out.append(f"    ... ({len(data) - limit} more bytes)")
    return "\n".join(out) if out else "    (no data)"


# ---------------------------------------------------------------- DNS

class DNSHandler(socketserver.BaseRequestHandler):
    def handle(self):
        data, sock = self.request
        server = self.server
        try:
            request = DNSRecord.parse(data)
        except Exception as e:
            log(f"[dns] unparseable query from {self.client_address[0]}: {e}")
            return
        qname = str(request.q.qname).rstrip(".")
        qtype = QTYPE[request.q.qtype]
        client = self.client_address[0]

        hijacked = any(qname == d or qname.endswith("." + d)
                       for d in server.hijack_domains)
        if hijacked:
            log(f"[dns] {client} asked {qtype} {qname} -> HIJACKED to {server.bind_ip}")
            reply = request.reply()
            reply.add_answer(RR(qname, QTYPE.A, rdata=A(server.bind_ip), ttl=60))
            sock.sendto(reply.pack(), self.client_address)
            return

        log(f"[dns] {client} asked {qtype} {qname} -> forwarded")
        try:
            upstream = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            upstream.settimeout(5)
            upstream.sendto(data, (server.upstream, 53))
            resp, _ = upstream.recvfrom(4096)
            upstream.close()
            parsed = DNSRecord.parse(resp)
            answers = "; ".join(f"{r.rname} {QTYPE[r.rtype]} {r.rdata}"
                                for r in parsed.rr)
            if answers:
                log(f"[dns]   upstream answered: {answers}")
            sock.sendto(resp, self.client_address)
        except Exception as e:
            log(f"[dns]   forward failed: {e}")


class ThreadingUDPServer(socketserver.ThreadingUDPServer):
    daemon_threads = True
    allow_reuse_address = True


# ---------------------------------------------------------------- TCP / TLS

def dump_connection(conn, addr, tag):
    """Log whatever the client sends; answer HTTP-looking traffic with a 200."""
    conn.settimeout(10)
    try:
        data = conn.recv(65535)
    except socket.timeout:
        log(f"[{tag}] {addr[0]}:{addr[1]} connected but sent nothing in 10s")
        return
    except Exception as e:
        log(f"[{tag}] {addr[0]}:{addr[1]} read error: {e}")
        return
    log(f"[{tag}] {addr[0]}:{addr[1]} sent {len(data)} bytes:\n{hexdump(data)}")
    if data.startswith((b"GET ", b"POST ", b"PUT ", b"DELETE ", b"HEAD ", b"OPTIONS ")):
        try:
            body = b'{"msg":"ok"}'
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                         b"Content-Length: " + str(len(body)).encode() +
                         b"\r\nConnection: close\r\n\r\n" + body)
            log(f"[{tag}] replied 200 OK to keep the client talking")
        except Exception:
            pass


def tls_listener(port, bind_ip):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1  # old IoT TLS stacks
    ctx.load_cert_chain(CERT_FILE, KEY_FILE)
    try:
        ctx.set_ciphers("ALL:@SECLEVEL=0")  # accept whatever the device offers
    except ssl.SSLError:
        pass

    def sni_cb(ssock, server_name, _ctx):
        log(f"[tls:{port}] SNI from {ssock.getpeername()[0]}: {server_name!r}")

    ctx.set_servername_callback(sni_cb)

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((bind_ip, port))
    srv.listen(16)
    log(f"[tls:{port}] listening on {bind_ip}:{port}")
    while True:
        raw, addr = srv.accept()
        def run(raw=raw, addr=addr):
            try:
                conn = ctx.wrap_socket(raw, server_side=True)
            except ssl.SSLError as e:
                if "HTTP_REQUEST" in str(e):
                    log(f"[tls:{port}] {addr[0]} spoke PLAIN HTTP to the TLS port")
                else:
                    log(f"[tls:{port}] *** HANDSHAKE FAILED from {addr[0]}: {e} "
                        f"(device likely rejected our certificate)")
                raw.close()
                return
            except Exception as e:
                log(f"[tls:{port}] handshake error from {addr[0]}: {e}")
                raw.close()
                return
            log(f"[tls:{port}] *** HANDSHAKE OK from {addr[0]}:{addr[1]} "
                f"version={conn.version()} cipher={conn.cipher()[0]} "
                f"ALPN={conn.selected_alpn_protocol()!r} "
                f"-- DEVICE DID NOT VALIDATE THE CERT ***")
            try:
                dump_connection(conn, addr, f"tls:{port}")
            finally:
                conn.close()
        threading.Thread(target=run, daemon=True).start()


def plain_listener(port, bind_ip, tag):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((bind_ip, port))
    srv.listen(16)
    log(f"[{tag}] listening on {bind_ip}:{port}")
    while True:
        conn, addr = srv.accept()
        log(f"[{tag}] connection from {addr[0]}:{addr[1]}")
        threading.Thread(target=dump_connection, args=(conn, addr, tag),
                         daemon=True).start()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bind", required=True,
                    help="this machine's LAN IP (what hijacked DNS answers with)")
    ap.add_argument("--upstream", default=None,
                    help="DNS resolver to forward non-hijacked queries to "
                         "(default: x.x.x.1 of --bind)")
    ap.add_argument("--hijack", default="hijuconn.com",
                    help="comma-separated domains to hijack (subdomains included)")
    ap.add_argument("--hijack-all", action="store_true",
                    help="answer every A query with --bind (do not do this with "
                         "other devices using this DNS)")
    args = ap.parse_args()

    if not CERT_FILE.exists():
        sys.exit(f"cert missing: run ./gen_cert.sh first")

    if args.hijack_all:
        domains = [""]  # "" endswith-matches everything
        log("[dns] HIJACKING ALL QUERIES")
    else:
        domains = [d.strip().lower() for d in args.hijack.split(",") if d.strip()]
        log(f"[dns] hijacking: {', '.join(domains)}")

    upstream = args.upstream or ".".join(args.bind.split(".")[:3]) + ".1"
    dns = ThreadingUDPServer((args.bind, 53), DNSHandler)
    dns.bind_ip = args.bind
    dns.upstream = upstream
    dns.hijack_domains = domains
    threading.Thread(target=dns.serve_forever, daemon=True).start()
    log(f"[dns] listening on {args.bind}:53, upstream={upstream}")

    for port in (443, 8883, 8443):
        threading.Thread(target=tls_listener, args=(port, args.bind),
                         daemon=True).start()
    for port, tag in ((80, "http:80"), (1883, "mqtt:1883")):
        threading.Thread(target=plain_listener, args=(port, args.bind, tag),
                         daemon=True).start()

    log("probe up. Point the AC's DNS here and power-cycle it. Ctrl-C to stop.")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        log("stopping")


if __name__ == "__main__":
    main()
