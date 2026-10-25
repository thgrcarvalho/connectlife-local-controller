#!/usr/bin/env python3
"""Logging TLS relay between the AC and the real ConnectLife device link.

The AC accepts our certificate, so we can terminate its TLS connection and
open a second, certificate-verified TLS connection to Hisense. Payloads are
forwarded unchanged in both directions and written to per-connection binary
capture files. Raw payload logging is disabled unless --log-payloads is set.

Resolve the cloud through a DNS server that is not being hijacked; otherwise
the relay could connect back to itself. A verified static IP is an optional fallback.

  sudo ../.venv/bin/python relay.py --bind 192.168.31.100 \
      --upstream-dns 1.1.1.1
"""

import argparse
import datetime
import itertools
import json
import socket
import ssl
import sys
import threading
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
WINDOWS_DEPS = BASE_DIR / ".windows-deps"
if WINDOWS_DEPS.is_dir():
    sys.path.insert(0, str(WINDOWS_DEPS))

from dnslib import DNSRecord, QTYPE

import replay_server
from replay_server import ACState, control_listener, load_frames, parse_frame

LOG_FILE = BASE_DIR / "relay.log"
CERT_FILE = BASE_DIR / "server.crt"
KEY_FILE = BASE_DIR / "server.key"
CAPTURE_DIR = BASE_DIR / "captures"

LOG_PAYLOADS = False
_log_lock = threading.Lock()
_connection_ids = itertools.count(1)


def log(message):
    line = f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S.%f}"[:-3] + "  " + message
    with _log_lock:
        print(line, flush=True)
        with LOG_FILE.open("a", encoding="utf-8") as stream:
            stream.write(line + "\n")


def payload_text(data, limit=4096):
    escapes = {9: "\\t", 10: "\\n", 13: "\\r"}
    text = "".join(escapes.get(byte, chr(byte) if 32 <= byte < 127
                               else f"\\x{byte:02x}") for byte in data[:limit])
    if len(data) > limit:
        text += f"... ({len(data) - limit} more bytes)"
    return text


def pipe(source, destination, direction, connection_id, capture, state):
    buffer = b""
    try:
        while True:
            try:
                data = source.recv(65535)
            except ssl.SSLZeroReturnError:
                break
            if not data:
                break
            capture.write(data)
            capture.flush()
            message = f"[{connection_id:04d}] {direction} {len(data)} bytes"
            if LOG_PAYLOADS:
                message += f": {payload_text(data)}"
            log(message)
            with state.write_lock:
                destination.sendall(data)
            buffer += data
            while b"\r\n" in buffer:
                raw, buffer = buffer.split(b"\r\n", 1)
                try:
                    obj = parse_frame(raw + b"\r\n")
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if obj.get("id") == 201:
                    state.acknowledge(obj)
                elif obj.get("id") == 903:
                    state.update_status(obj)
    except (OSError, ssl.SSLError) as error:
        log(f"[{connection_id:04d}] {direction} relay error: {error}")
    finally:
        try:
            destination.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def resolve_upstream(host, dns_server, timeout=5):
    query = DNSRecord.question(host, "A")
    response = DNSRecord.parse(query.send(dns_server, 53, timeout=timeout))
    addresses = [str(record.rdata) for record in response.rr if record.rtype == QTYPE.A]
    if not addresses:
        raise OSError(f"no A record returned for {host}")
    return addresses[0]


def handle_client(raw_client, address, server_context, args, state):
    connection_id = next(_connection_ids)
    peer = f"{address[0]}:{address[1]}"
    try:
        client = server_context.wrap_socket(raw_client, server_side=True)
    except (OSError, ssl.SSLError) as error:
        log(f"[{connection_id:04d}] {peer} TLS accept failed: {error}")
        raw_client.close()
        return

    log(f"[{connection_id:04d}] {peer} client TLS OK: "
        f"{client.version()} {client.cipher()[0]}")
    try:
        upstream_ip = args.upstream_ip
        if args.upstream_dns:
            try:
                upstream_ip = resolve_upstream(args.upstream_host, args.upstream_dns)
            except Exception as error:
                log(f"[{connection_id:04d}] dynamic upstream resolution failed: "
                    f"{type(error).__name__}")
        if not upstream_ip:
            raise OSError("no upstream address available")
        upstream_socket = socket.create_connection(
            (upstream_ip, args.upstream_port), timeout=10)
        upstream_context = ssl.create_default_context()
        upstream = upstream_context.wrap_socket(
            upstream_socket, server_hostname=args.upstream_host)
        upstream.settimeout(None)
        log(f"[{connection_id:04d}] connected to real "
            f"{args.upstream_host} ({upstream_ip}) via {upstream.version()}")
    except (OSError, ssl.SSLError) as error:
        log(f"[{connection_id:04d}] upstream connection failed: {error}")
        client.close()
        return

    CAPTURE_DIR.mkdir(exist_ok=True)
    capture_base = CAPTURE_DIR / (
        f"{datetime.datetime.now():%Y%m%d-%H%M%S}-{connection_id:04d}")
    c2s_file = capture_base.with_suffix(".c2s.bin").open("ab")
    s2c_file = capture_base.with_suffix(".s2c.bin").open("ab")
    log(f"[{connection_id:04d}] capturing to {capture_base}.*.bin")
    state.capture = s2c_file
    state.attach(client)

    downstream = threading.Thread(
        target=pipe, args=(upstream, client, "cloud -> AC", connection_id, s2c_file, state),
        daemon=True)
    downstream.start()
    pipe(client, upstream, "AC -> cloud", connection_id, c2s_file, state)
    downstream.join(timeout=5)

    state.detach(client)
    for stream in (client, upstream, c2s_file, s2c_file):
        try:
            stream.close()
        except OSError:
            pass
    log(f"[{connection_id:04d}] {peer} connection closed")


def load_command_library(capture, command_map):
    command_frames = {}
    if not command_map:
        return command_frames
    command_index = json.loads(command_map.read_text(encoding="utf-8"))
    if not isinstance(command_index, dict):
        raise ValueError("command map must be a JSON object")
    capture_commands = {}
    for name, entry in command_index.items():
        command_capture = capture
        index = entry
        if isinstance(entry, dict):
            command_capture = CAPTURE_DIR / entry["capture"]
            index = entry["index"]
        if not isinstance(name, str) or not isinstance(index, int) or index < 0:
            raise ValueError("command map must contain names and nonnegative indices")
        if command_capture is None:
            raise ValueError("each command needs a capture or a default --capture")
        if command_capture not in capture_commands:
            capture_commands[command_capture] = [
                frame for frame in load_frames(command_capture.with_suffix(".s2c.bin"))
                if parse_frame(frame).get("id") == 201]
        command_frames[name] = capture_commands[command_capture][index]
    return command_frames


def main():
    global LOG_FILE, LOG_PAYLOADS
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bind", required=True, help="LAN IP to listen on")
    parser.add_argument("--listen-port", type=int, default=443)
    parser.add_argument("--upstream-host", default="ac-eu-link.hijuconn.com",
                        help="real hostname, used for SNI and certificate checks")
    parser.add_argument("--upstream-ip",
                        help="optional fallback cloud IP, resolved outside the hijack")
    parser.add_argument("--upstream-dns",
                        help="non-hijacked DNS resolver used for dynamic cloud lookups")
    parser.add_argument("--upstream-port", type=int, default=443)
    parser.add_argument("--capture", type=Path,
                        help="capture basename for optional command injection")
    parser.add_argument("--command-map", type=Path,
                        help="JSON object mapping command names to capture indices")
    parser.add_argument("--control-port", type=int, default=8765)
    parser.add_argument("--log-file", type=Path, default=LOG_FILE)
    parser.add_argument("--log-payloads", action="store_true",
                        help="log raw payloads, including sensitive session data")
    args = parser.parse_args()
    if not args.upstream_dns and not args.upstream_ip:
        parser.error("either --upstream-dns or --upstream-ip is required")
    LOG_FILE = args.log_file
    LOG_PAYLOADS = args.log_payloads
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CAPTURE_DIR.mkdir(exist_ok=True)
    replay_server.LOG_FILE = args.log_file

    try:
        command_frames = load_command_library(args.capture, args.command_map)
    except (IndexError, KeyError, TypeError, ValueError) as error:
        parser.error(str(error))

    state = ACState(command_frames)
    if command_frames:
        threading.Thread(target=control_listener, args=(state, args.control_port),
                         daemon=True).start()

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1
    context.load_cert_chain(CERT_FILE, KEY_FILE)
    try:
        context.set_ciphers("ALL:@SECLEVEL=0")
    except ssl.SSLError:
        pass

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((args.bind, args.listen_port))
    listener.listen(16)
    log(f"listening on {args.bind}:{args.listen_port}; relaying to "
        f"{args.upstream_host}:{args.upstream_port} with dynamic resolution "
        f"{'enabled' if args.upstream_dns else 'disabled'}")

    try:
        while True:
            raw_client, address = listener.accept()
            threading.Thread(target=handle_client,
                             args=(raw_client, address, context, args, state),
                             daemon=True).start()
    except KeyboardInterrupt:
        log("stopping")


if __name__ == "__main__":
    main()
