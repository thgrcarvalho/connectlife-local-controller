#!/usr/bin/env python3
"""Windows-side DNS proxy for the fake-cloud probe.

Run with Windows Python while WSL2 mirrored networking is enabled. Windows
can bind the LAN IP specifically even when ICS owns 0.0.0.0:53; WSL's UDP
forwarding cannot. Only the DNS service runs here. The AC's TLS connections
still go to probe.py in WSL, which has already been verified from the LAN.

Standard library only:
  python.exe windows_dns.py --bind 192.168.31.100
"""

import argparse
import datetime
import socket
import struct
import threading
from pathlib import Path

LOG_FILE = Path(__file__).with_name("windows_dns.log")
_log_lock = threading.Lock()


def log(message):
    line = f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S.%f}"[:-3] + "  " + message
    with _log_lock:
        print(line, flush=True)
        with LOG_FILE.open("a", encoding="utf-8") as stream:
            stream.write(line + "\n")


def parse_question(packet):
    if len(packet) < 17:
        raise ValueError("packet too short")
    if struct.unpack_from("!H", packet, 4)[0] != 1:
        raise ValueError("expected exactly one question")

    offset = 12
    labels = []
    while True:
        length = packet[offset]
        offset += 1
        if length == 0:
            break
        if length > 63 or offset + length > len(packet):
            raise ValueError("invalid or compressed question name")
        labels.append(packet[offset:offset + length].decode("ascii").lower())
        offset += length

    if offset + 4 > len(packet):
        raise ValueError("truncated question")
    qtype, qclass = struct.unpack_from("!HH", packet, offset)
    return ".".join(labels), qtype, qclass, offset + 4


def hijacked_response(packet, answer_ip, domains):
    name, qtype, qclass, question_end = parse_question(packet)
    if qclass != 1 or not any(name == domain or name.endswith("." + domain)
                              for domain in domains):
        return None

    type_name = {1: "A", 28: "AAAA"}.get(qtype, str(qtype))
    if qtype == 1:
        log(f"[win-dns] HIJACK {name} {type_name} -> {answer_ip}")
        answers = 1
        answer = (b"\xc0\x0c" + struct.pack("!HHIH", 1, 1, 60, 4)
                  + socket.inet_aton(answer_ip))
    else:
        # Do not let an AAAA/HTTPS lookup reveal a real cloud address.
        log(f"[win-dns] HIJACK {name} {type_name} -> empty response")
        answers = 0
        answer = b""

    request_flags = struct.unpack_from("!H", packet, 2)[0]
    response_flags = 0x8000 | 0x0080 | (request_flags & 0x0100)
    header = struct.pack("!HHHHHH",
                         struct.unpack_from("!H", packet, 0)[0],
                         response_flags, 1, answers, 0, 0)
    return header + packet[12:question_end] + answer


class DNSProxy:
    def __init__(self, bind_ip, answer_ip, upstream, domains):
        self.bind_ip = bind_ip
        self.answer_ip = answer_ip
        self.upstream = upstream
        self.domains = domains
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.bind((bind_ip, 53))

    def serve_forever(self):
        log(f"[win-dns] listening on {self.bind_ip}:53; "
            f"hijacking {', '.join(self.domains)}; upstream={self.upstream}")
        while True:
            packet, client = self.socket.recvfrom(4096)
            threading.Thread(target=self.handle, args=(packet, client),
                             daemon=True).start()

    def handle(self, packet, client):
        try:
            name, qtype, _, _ = parse_question(packet)
            log(f"[win-dns] {client[0]} asked type={qtype} {name}")
            response = hijacked_response(packet, self.answer_ip, self.domains)
        except ValueError as error:
            log(f"[win-dns] {client[0]} parse error: {error}; forwarding")
            response = None

        if response is None:
            response = self.forward(packet, client)
        if response is not None:
            self.socket.sendto(response, client)

    def forward(self, packet, client):
        upstream = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        upstream.settimeout(5)
        try:
            upstream.sendto(packet, (self.upstream, 53))
            response, _ = upstream.recvfrom(4096)
            return response
        except OSError as error:
            log(f"[win-dns] upstream failed for {client[0]}: {error}")
            return None
        finally:
            upstream.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bind", required=True, help="Windows LAN IP")
    parser.add_argument("--answer", help="IP returned for hijacked names; "
                        "defaults to --bind (the WSL probe's IP)")
    parser.add_argument("--upstream", default=None,
                        help="real DNS resolver; default is x.x.x.1 of --bind")
    parser.add_argument("--hijack", default="hijuconn.com",
                        help="comma-separated domains, subdomains included")
    args = parser.parse_args()

    upstream = args.upstream or ".".join(args.bind.split(".")[:3]) + ".1"
    domains = [item.strip().lower() for item in args.hijack.split(",")
               if item.strip()]
    DNSProxy(args.bind, args.answer or args.bind, upstream,
             domains).serve_forever()


if __name__ == "__main__":
    main()
